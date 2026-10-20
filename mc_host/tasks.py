"""One-at-a-time background tasks (install, switch, backup, restore, prepare).

A task validates nothing itself: routes validate synchronously, then call
`submit`, which raises TaskBusy if another task is running. The runner keeps
the most recently finished task for KEEP_SECONDS so the dashboard can show
how it ended. When any task ends, `on_finish` runs; the server controller
uses it to re-arm the sleep proxy (contract §0 invariant).
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

import installer

KEEP_SECONDS = 300
GENERIC_FAILURE = "Something went wrong. The log has the details."


class TaskBusy(Exception):
    def __init__(self, task: "Task") -> None:
        super().__init__(f"Wait for “{task.label}” to finish first.")
        self.task = task


@dataclass
class Task:
    id: str
    kind: str
    label: str
    state: str = "running"
    error: str | None = None
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None
    result: Any = None
    # Why it failed, for the dashboard to act on (e.g. "world_downgrade" with
    # the world and versions in data, so it can offer "back up and open
    # anyway"), and a note to show when it finished (a warning, not an error).
    code: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "error": self.error,
            "code": self.code,
            "data": dict(self.data),
            "note": self.note,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskContext:
    """Handed to the task function so it can update its label."""

    def __init__(self, runner: "TaskRunner", task: Task) -> None:
        self._runner = runner
        self.task = task

    def set_label(self, label: str) -> None:
        with self._runner.lock:
            self.task.label = label

    def set_note(self, note: str | None) -> None:
        with self._runner.lock:
            self.task.note = note


class TaskRunner:
    def __init__(
        self,
        on_finish: Callable[[Task], None] | None = None,
        log: Callable[..., Any] | None = None,
        keep_seconds: int = KEEP_SECONDS,
    ) -> None:
        self.lock = threading.RLock()
        self.on_finish = on_finish
        self._log_fn = log
        self.keep_seconds = keep_seconds
        self.current: Task | None = None
        self.last: Task | None = None
        self._ids = itertools.count(1)
        self._idle = threading.Event()
        self._idle.set()

    def _log(self, text: str, level: str = "info") -> None:
        if self._log_fn is None:
            from .state import state

            state.add_log(text, level)
        else:
            self._log_fn(text, level)

    # -- queries -----------------------------------------------------------
    def running(self) -> Task | None:
        with self.lock:
            return self.current

    def is_busy(self) -> bool:
        with self.lock:
            return self.current is not None

    def snapshot(self, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        with self.lock:
            if self.current is not None:
                return self.current.to_dict()
            last = self.last
            if last is not None and last.finished_at is not None:
                if now - last.finished_at <= self.keep_seconds:
                    return last.to_dict()
        return None

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    # -- running -----------------------------------------------------------
    def submit(
        self,
        kind: str,
        label: str,
        fn: Callable[[TaskContext], Any],
        on_success: Callable[[Task], None] | None = None,
        guard: Callable[[], Any] | None = None,
    ) -> Task:
        """Start ``fn`` in the background. ``guard`` runs under the task lock
        right before the task is created and may raise to refuse it (the
        server controller uses it so tasks never interleave with a start or
        stop that is in flight)."""
        with self.lock:
            if self.current is not None:
                raise TaskBusy(self.current)
            if guard is not None:
                guard()
            task = Task(id=f"t{next(self._ids)}", kind=kind, label=label)
            self.current = task
            self._idle.clear()
        thread = threading.Thread(
            target=self._run, args=(task, fn, on_success), name=f"task-{task.id}", daemon=True
        )
        thread.start()
        return task

    def _run(
        self,
        task: Task,
        fn: Callable[[TaskContext], Any],
        on_success: Callable[[Task], None] | None,
    ) -> None:
        self._log(f"{task.label}…")
        try:
            self._execute(task, fn, on_success)
        finally:
            self._idle.set()

    def _execute(
        self,
        task: Task,
        fn: Callable[[TaskContext], Any],
        on_success: Callable[[Task], None] | None,
    ) -> None:
        from .process import ServerError  # process imports this module

        try:
            task.result = fn(TaskContext(self, task))
        except installer.InstallError as exc:
            self._finish(task, "failed", str(exc) or GENERIC_FAILURE, exc)
        except ServerError as exc:  # e.g. "The server is already stopping."
            self._finish(task, "failed", str(exc) or GENERIC_FAILURE, exc)
        except Exception as exc:  # unexpected: keep the traceback out of the UI
            print(traceback.format_exc(), flush=True)
            self._log(f"{task.label} failed unexpectedly: {type(exc).__name__}", "error")
            self._finish(task, "failed", GENERIC_FAILURE)
        else:
            self._finish(task, "done", None)
            if on_success is not None:
                try:
                    on_success(task)
                except Exception:
                    print(traceback.format_exc(), flush=True)

    def _finish(
        self, task: Task, result: str, error: str | None, exc: BaseException | None = None
    ) -> None:
        with self.lock:
            task.state = result
            task.error = error
            if exc is not None:
                task.code = str(getattr(exc, "code", None) or "") or None
                data = getattr(exc, "data", None)
                task.data = dict(data) if isinstance(data, dict) else {}
            task.finished_at = int(time.time())
            self.last = task
            self.current = None
        if result == "done":
            self._log(f"{task.label}: done")
        else:
            self._log(f"{task.label}: failed. {error}", "error")
        if self.on_finish is not None:
            try:
                self.on_finish(task)
            except Exception:
                print(traceback.format_exc(), flush=True)
