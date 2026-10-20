"""Minecraft process lifecycle: phases, start/stop, wake-on-join, idle sleep.

Phases (state.phase): sleeping -> starting -> online -> stopping -> sleeping,
or -> crashed when the process exits on its own or a start fails. The public
phase adds "preparing" while a background task runs and Minecraft is down.

Invariant (contract §0): whenever Minecraft is not running, the sleep proxy
owns port 25565. `ensure_listener()` enforces it and is called after every
stop, crash, failed start, task end and setup switch. The only place the
proxy is released is the hand-off right before the JVM is spawned, done
under the same port lock, so the two can never interleave.
"""

from __future__ import annotations

import collections
import re
import subprocess
import threading
import time
import traceback
from typing import Any, Callable

import installer

from . import config
from . import installer_api as ix
from .proxy import MODE_CRASHED, MODE_SLEEPING, MODE_STARTING, MODE_UPDATING, SleepProxy
from .state import CRASHED, ONLINE, PREPARING, SLEEPING, STARTING, STOPPING, ServerState
from .state import state as default_state
from .tasks import TaskRunner

# -- log line patterns ------------------------------------------------------
# Server messages follow "]: " (1.7+: "[12:00:00] [Server thread/INFO]: ") or
# "[INFO] " (1.2.5-1.6). Chat lines put "<name>", "[name]" or "* name" right
# after that prefix, so anchoring the player name there makes them unspoofable.
_PREFIX = r"(?:\]: |\[INFO\] )"
# 26.x logs system messages (joins, leaves, command replies) as
# "System chat: <text>". Player chat never starts with it.
_SYS = r"(?:System chat: )?"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DONE_RE = re.compile(_PREFIX + r"Done \(\d")
JOIN_RES = (
    re.compile(
        r"\]: " + _SYS + r"([A-Za-z0-9_]{1,16})(?: \(formerly known as [A-Za-z0-9_]{1,16}\))? joined the game$"
    ),
    re.compile(r"\[INFO\] ([A-Za-z0-9_]{1,16}) ?\[/[^\]]*\] logged in with entity id"),
)
LEAVE_RES = (
    re.compile(r"\]: " + _SYS + r"([A-Za-z0-9_]{1,16}) left the game$"),
    re.compile(r"\[INFO\] ([A-Za-z0-9_]{1,16}) lost connection: "),
)
SAVED_RE = re.compile(_PREFIX + _SYS + r"(?:Saved the game|Saved the world|Save complete\.?)$")
LIST_COUNT_RE = re.compile(
    _PREFIX + _SYS + r"There are (\d+)(?: of a max(?: of)? |/)(\d+) players online:(.*)$"
)
# Reply to `whitelist on|off` (1.13+, 1.7-1.12, and 1.6.4 wording).
WHITELIST_RE = re.compile(
    _PREFIX + _SYS + r"(?:Whitelist is (?:now|already) turned (?:on|off)|Turned (?:on|off) (?:the whitelist|white-listing))"
)
LIST_LEGACY_RE = re.compile(r"\[INFO\] Connected players: ?(.*)$")
LIST_NAMES_RE = re.compile(_PREFIX + _SYS + r"(.*)$")
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}\Z")
_GC_FLAG = re.compile(r"^-XX:\+Use\w*GC$")
# Shutdown budget: STOP_TIMEOUT (<= 45s) + KILL_WAIT + MONITOR_JOIN must stay
# under Railway's drainingSeconds (60, railway.toml) so SIGKILL never lands
# mid-save.
KILL_WAIT = 5
MONITOR_JOIN = 2
# After a start fails (or crashes before it is ready), joins don't wake the
# server again for this long; persistent failures wait for the owner.
WAKE_BACKOFF = 300
_STACK_FRAME = re.compile(r"^\s+(at |\.\.\. \d+ more)")
_THROWABLE = re.compile(r"(?:^|[\s:])(?:[\w$]+\.)+[\w$]*(?:Exception|Error)(?::|$)")


# -- errors ----------------------------------------------------------------
class ServerError(Exception):
    code = "server_error"
    status = 409

    def __init__(
        self,
        message: str,
        code: str | None = None,
        status: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status
        # Extra JSON fields for the API (e.g. world/versions for world_downgrade).
        self.data: dict[str, Any] = dict(data or {})


class ServerBusy(ServerError):
    code = "busy"


class AlreadyRunning(ServerError):
    code = "already_running"


class NotRunning(ServerError):
    code = "not_running"


class StartFailed(ServerError):
    code = "start_failed"
    status = 500


# Start failures that the next join can't fix: waking again would only fail
# the same way, so the sleep proxy tells players to ask the owner instead.
PERSISTENT_START_FAILURES = frozenset(
    {"world_downgrade", "java_missing", "unsupported_pack", "instance_not_found", "invalid_version"}
)


def idle_should_stop(
    phase: str,
    player_count: int,
    idle_since: float | None,
    timeout: int,
    now: float,
    busy: bool = False,
) -> bool:
    """Pure idle decision: only an online, empty server past its timeout sleeps."""
    if busy or phase != ONLINE or player_count > 0 or idle_since is None:
        return False
    if timeout <= 0:
        return False
    return now - idle_since >= timeout


def memory_flags(existing: list[str]) -> list[str]:
    """Heap, GC and Log4Shell flags the installer's JVM args don't already set."""
    added: list[str] = []
    if not any(arg.startswith("-Xmx") for arg in existing):
        added += [f"-Xmx{config.MAX_MEMORY}", f"-Xms{config.MIN_MEMORY}"]
    if not any(_GC_FLAG.match(arg) for arg in existing):
        added += ["-XX:+UseG1GC", "-XX:MaxGCPauseMillis=200"]
    no_lookups = "-Dlog4j2.formatMsgNoLookups=true"
    if no_lookups not in existing:
        added.append(no_lookups)
    return added


def build_command(spec: Any) -> list[str]:
    """Java command line for an installer.RuntimeSpec plus our memory flags."""
    existing = [str(arg) for arg in spec.jvm_args]
    return [str(arg) for arg in spec.build_command(memory_flags(existing))]


class _Waiter:
    def __init__(self, regex: re.Pattern[str]) -> None:
        self.regex = regex
        self.event = threading.Event()
        self.line: str | None = None


class _ListWaiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.names: list[str] = []
        self.expect_names = False

    def done(self, names: list[str]) -> None:
        self.names = names
        self.event.set()


def _parse_names(text: str) -> list[str]:
    return [name.strip() for name in text.split(",") if PLAYER_RE.match(name.strip())]


def _friendly_start_error(exc: BaseException) -> str:
    if isinstance(exc, installer.InstallError):
        return str(exc) or "The server files could not be prepared."
    if isinstance(exc, FileNotFoundError):
        return "Java could not be found for this Minecraft version. Check the image build."
    if isinstance(exc, PermissionError):
        return "Railpowered could not run Java (permission denied)."
    return "Minecraft could not start. The log has the details."


class ServerController:
    def __init__(
        self,
        state: ServerState | None = None,
        tasks: TaskRunner | None = None,
        proxy: Any = None,
        port: int = config.MC_PORT,
        popen: Callable[..., Any] = subprocess.Popen,
        stop_timeout: float | None = None,
    ) -> None:
        self.state = state or ServerState()
        self.tasks = tasks or TaskRunner(log=self.log)
        self.tasks.on_finish = self._on_task_finish
        self.proxy = proxy or SleepProxy(port, self.request_wake, info=self.proxy_info, log=self.log)
        self.popen = popen
        self.stop_timeout = config.STOP_TIMEOUT if stop_timeout is None else stop_timeout
        self._port_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._list_waiter: _ListWaiter | None = None
        self._waiters_lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._stopping_proc: Any = None
        self._wake_lock = threading.Lock()
        self._shutting_down = False
        self._restarting = False
        # (when, code) of the last start that failed before Minecraft was ready.
        self._failed_start: tuple[float, str] | None = None
        self._timed_out: Any = None  # the process stopped by the start watchdog
        self._whitelist_error_logged = False
        self._recent: collections.deque[str] = collections.deque(maxlen=60)
        self._idle_cache: tuple[float, int] = (0.0, 0)
        self._info_cache: tuple[float, dict[str, Any]] = (0.0, {})

    # -- logging -----------------------------------------------------------
    def log(self, text: str, level: str = "info") -> None:
        self.state.add_log(text, level)

    # -- phase -------------------------------------------------------------
    def public_phase(self) -> str:
        with self.state.lock:
            phase = self.state.phase
        if phase in (SLEEPING, CRASHED) and self.tasks.is_busy():
            return PREPARING
        return phase

    def is_running(self) -> bool:
        with self.state.lock:
            return self.state.process is not None

    def _preparing(self) -> bool:
        """start() is installing files: phase STARTING, Java not spawned yet."""
        with self.state.lock:
            return self.state.phase == STARTING and self.state.process is None

    def lifecycle_active(self) -> bool:
        """Running, or on its way up or down (prepare window included)."""
        with self.state.lock:
            return self.state.process is not None or self.state.phase in (STARTING, STOPPING)

    def ensure_quiet(self) -> None:
        """Refuse (409 busy) while the server is starting or stopping.

        Passed to TaskRunner.submit as its guard, so the check and the task
        creation happen under the task lock that start() and stop() take too:
        a switch, restore or backup can never interleave with them."""
        with self.state.lock:
            phase, proc = self.state.phase, self.state.process
        if phase == STOPPING:
            raise ServerBusy("The server is stopping. Try again in a moment.")
        if phase == STARTING and proc is None:
            raise ServerBusy("The server is starting. Try again once it's online.")

    def _active_setup_field(self, key: str) -> str | None:
        try:
            value = ix.active_setup().get(key)
        except Exception:
            return None
        return str(value) if value else None

    def current_world(self) -> str | None:
        """The world Minecraft has open, or is about to open while starting."""
        with self.state.lock:
            proc, runtime, phase = self.state.process, self.state.runtime, self.state.phase
        if proc is None:
            return self._active_setup_field("level_name") if phase == STARTING else None
        return getattr(runtime, "level_name", None) if runtime is not None else None

    def running_instance(self) -> str | None:
        """The modpack instance the running (or starting) server uses, if any."""
        with self.state.lock:
            proc, runtime, phase = self.state.process, self.state.runtime, self.state.phase
        if proc is None:
            return self._active_setup_field("instance") if phase == STARTING else None
        return getattr(runtime, "instance", None) if runtime is not None else None

    def settle(self, timeout: float | None = None) -> bool:
        """Wait until no start-prepare or stop is in flight. Returns True when
        the stop that was in flight belonged to a restart (so whoever waited
        should bring the server back up)."""
        limit = timeout if timeout is not None else installer.INSTALL_TIMEOUT
        deadline = time.monotonic() + limit
        resume = False
        while time.monotonic() < deadline:
            with self.state.lock:
                phase, proc = self.state.phase, self.state.process
            resume = resume or self._restarting
            if phase != STOPPING and not (phase == STARTING and proc is None):
                return resume
            time.sleep(0.05)
        return resume

    def clear_crash(self, note: str | None = None) -> bool:
        """Forget a crash that no longer applies (another setup or world is
        in place now). Sleeping stays sleeping: nothing is started."""
        with self.state.lock:
            if self.state.process is not None or self.state.phase != CRASHED:
                self._failed_start = None
                return False
            self.state.phase = SLEEPING
            self.state.last_exit = None
        self._failed_start = None
        self._info_cache = (0.0, {})
        if note:
            self.log(note)
        return True

    def wake_blocked(self, now: float | None = None) -> bool:
        """True when a join should not try to start the server again."""
        with self.state.lock:
            if self.state.phase != CRASHED:
                return False
        failed = self._failed_start
        if failed is None:
            return False
        at, code = failed
        now = time.time() if now is None else now
        return code in PERSISTENT_START_FAILURES or now - at < WAKE_BACKOFF

    def _wake_info(self, now: float) -> dict[str, Any]:
        """How a join treats the crashed server (see wake_blocked), for the
        dashboard: wake "join" (a join starts it), "later" (not before
        wake_at) or "manual" (only a start from the dashboard helps)."""
        failed = self._failed_start
        if failed is None:
            return {"wake": "join", "wake_at": None}
        at, code = failed
        if code in PERSISTENT_START_FAILURES:
            return {"wake": "manual", "wake_at": None}
        if now - at < WAKE_BACKOFF:
            return {"wake": "later", "wake_at": int(at + WAKE_BACKOFF)}
        return {"wake": "join", "wake_at": None}

    def _active_setup_id(self) -> str | None:
        return self._active_setup_field("id")

    # -- the proxy invariant ----------------------------------------------
    def ensure_listener(self) -> bool:
        """Re-arm the sleep proxy unless Minecraft owns (or is taking) the port."""
        with self._port_lock:
            if self._shutting_down:
                return False
            with self.state.lock:
                if self.state.process is not None or self.state.port_handed_off:
                    return False
            return bool(self.proxy.start())

    def _on_task_finish(self, _task: Any) -> None:
        self._info_cache = (0.0, {})
        self.ensure_listener()

    # -- start -------------------------------------------------------------
    def start(
        self,
        *,
        ignore_task: bool = False,
        reason: str | None = None,
        allow_downgrade: bool = False,
    ) -> Any:
        """Prepare and launch Minecraft. Returns the RuntimeSpec.

        Raises ServerBusy / AlreadyRunning (409) or StartFailed (500; 409
        with code world_downgrade and its data when the world has been
        played on a newer version: retry with allow_downgrade to back it up
        and open it anyway).
        """
        if self._shutting_down:
            raise ServerBusy("Railpowered is shutting down.")
        # Under the task lock: a task can't be submitted between the check
        # and phase=STARTING (TaskRunner.submit runs ensure_quiet under it).
        with self.tasks.lock:
            if not ignore_task:
                busy = self.tasks.running()
                if busy is not None:
                    raise ServerBusy(f"Wait for “{busy.label}” to finish first.")
            with self.state.lock:
                if self.state.phase in (STARTING, ONLINE, STOPPING):
                    raise AlreadyRunning(
                        "The server is already stopping." if self.state.phase == STOPPING
                        else "The server is already running."
                    )
                self.state.phase = STARTING
                self.state.port_handed_off = False
                self.state.players.clear()
                self.state.idle_since = None
                self.state.online_since = None
        self._info_cache = (0.0, {})
        self.log(f"Starting Minecraft{f' ({reason})' if reason else ''}…")
        try:
            spec = ix.prepare_runtime(allow_downgrade=True) if allow_downgrade else ix.prepare_runtime()
            command = build_command(spec)
            # os.environ minus ADMIN_KEY and other secrets, plus JAVA_HOME.
            env = ix.child_env(spec.env)
        except Exception as exc:
            raise self._start_failed(exc) from exc

        with self._port_lock:
            if self._shutting_down:
                # SIGTERM arrived while preparing: don't spawn a JVM that the
                # container is about to kill mid-load.
                with self.state.lock:
                    self.state.phase = SLEEPING
                    self.state.port_handed_off = False
                self.log("Start cancelled: Railpowered is shutting down.")
                raise ServerBusy("Railpowered is shutting down.")
            with self.state.lock:
                self.state.port_handed_off = True
            self.proxy.stop()
            try:
                proc = self.popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=spec.cwd,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except Exception as exc:
                raise self._start_failed(exc) from exc
            with self.state.lock:
                self.state.process = proc
                self.state.runtime = spec
                self.state.start_time = time.time()
                self._recent.clear()
        thread = threading.Thread(
            target=self._monitor, args=(proc,), name="minecraft-log", daemon=True
        )
        self._monitor_thread = thread
        thread.start()
        java = getattr(spec, "java_major", "?")
        self.log(
            f"Launched {getattr(spec, 'type', 'minecraft')} {getattr(spec, 'version', '')} "
            f"(Java {java}, world {getattr(spec, 'level_name', '?')}, "
            f"heap {config.MIN_MEMORY}–{config.MAX_MEMORY}). Waiting for it to load…"
        )
        return spec

    def _start_failed(self, exc: BaseException) -> StartFailed:
        message = _friendly_start_error(exc)
        cause = ix.error_code(exc) if isinstance(exc, installer.InstallError) else None
        if not isinstance(exc, installer.InstallError):
            print(traceback.format_exc(), flush=True)
        if isinstance(exc, FileNotFoundError):
            cause = "java_missing"
        setup_id = self._active_setup_id()
        with self.state.lock:
            self.state.phase = CRASHED
            self.state.process = None
            self.state.port_handed_off = False
            self.state.last_exit = {
                "code": None, "at": int(time.time()), "reason": message, "setup_id": setup_id,
            }
        self._failed_start = (time.time(), cause or "start_failed")
        self._info_cache = (0.0, {})
        self.log(f"Could not start Minecraft: {message}", "error")
        self.ensure_listener()
        if cause == "world_downgrade":
            # Keep what the dashboard needs to offer "back up and open anyway".
            return StartFailed(message, code=cause, status=409, data=ix.error_data(exc))
        return StartFailed(message)

    # -- stop --------------------------------------------------------------
    def stop(self, *, ignore_task: bool = False, reason: str | None = None) -> bool:
        """Save and stop Minecraft, then re-arm the proxy. Blocks until it exits."""
        with self.tasks.lock:
            if not ignore_task:
                busy = self.tasks.running()
                if busy is not None:
                    raise ServerBusy(f"Wait for “{busy.label}” to finish first.")
            with self.state.lock:
                proc = self.state.process
                phase = self.state.phase
                if phase == STOPPING:
                    raise AlreadyRunning("The server is already stopping.", "already_stopping")
                if proc is None:
                    if phase == STARTING:
                        raise ServerBusy("The server is still preparing its files. Try again in a moment.")
                    raise NotRunning("The server isn't running.")
                self.state.phase = STOPPING
                self._stopping_proc = proc
                spec = self.state.runtime
        self.log(f"Stopping Minecraft{f' ({reason})' if reason else ''}; saving the world…")
        legacy = ix.is_legacy(getattr(spec, "version", None))
        self._write(proc, "save-all" if legacy else "save-all flush")
        self._write(proc, "stop")
        try:
            proc.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            self.log(
                f"Minecraft did not stop within {int(self.stop_timeout)}s; forcing it to exit.",
                "warn",
            )
            try:
                proc.kill()
                proc.wait(timeout=KILL_WAIT)
            except (OSError, subprocess.TimeoutExpired):
                pass
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=MONITOR_JOIN)
        self._finish_exit(proc, expected=True)
        return True

    def restart(self) -> Any:
        self._restarting = True
        try:
            if self.is_running():
                self.stop(reason="restart")
            return self.start(reason="restart")
        finally:
            self._restarting = False

    def shutdown(self) -> None:
        """Graceful exit for SIGTERM: save and stop, then release the port for good.

        Returns only once Java has exited (or was killed after the grace
        period), including when a stop was already under way (idle sleep,
        restart, switch): the container ends right after this returns."""
        self._shutting_down = True
        # An in-flight start either finished spawning Java (stopped below) or
        # will see _shutting_down under this lock and not spawn it.
        with self._port_lock:
            pass
        try:
            with self.state.lock:
                proc, phase = self.state.process, self.state.phase
            if proc is not None and phase != STOPPING:
                try:
                    self.stop(ignore_task=True, reason="shutting down")
                except ServerError:
                    pass
            self._wait_for_exit()
        except Exception:
            print(traceback.format_exc(), flush=True)
        with self._port_lock:
            self.proxy.stop()

    def _wait_for_exit(self) -> None:
        with self.state.lock:
            proc = self._stopping_proc or self.state.process
        if proc is None:
            return
        try:
            proc.wait(timeout=self.stop_timeout + KILL_WAIT)
        except subprocess.TimeoutExpired:
            self.log("Minecraft is still running at shutdown; forcing it to exit.", "warn")
            try:
                proc.kill()
                proc.wait(timeout=KILL_WAIT)
            except (OSError, subprocess.TimeoutExpired):
                pass
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=MONITOR_JOIN)

    def _finish_exit(self, proc: Any, expected: bool) -> None:
        code = proc.poll()
        timed_out = self._timed_out is proc
        with self.state.lock:
            if self.state.process is not proc:
                return  # already finalised by the other thread
            reached_online = self.state.phase in (ONLINE, STOPPING) and self.state.online_since
            setup_id = getattr(self.state.runtime, "setup_id", None) or None
            self.state.process = None
            self.state.port_handed_off = False
            self.state.players.clear()
            self.state.start_time = None
            self.state.online_since = None
            self.state.idle_since = None
            if self._stopping_proc is proc:
                self._stopping_proc = None
            if timed_out:
                self._timed_out = None
                self.state.phase = CRASHED
                minutes = max(1, config.START_TIMEOUT // 60)
                self.state.last_exit = {
                    "code": code, "at": int(time.time()), "setup_id": setup_id,
                    "reason": f"Minecraft didn't finish starting within {minutes} minutes, so it "
                              f"was stopped. Last line: {self._exit_reason()}",
                }
            elif expected:
                self.state.phase = SLEEPING
            else:
                self.state.phase = CRASHED
                reason = self._exit_reason()
                if not reached_online:
                    reason = f"Crashed while starting: {reason}"
                self.state.last_exit = {
                    "code": code, "at": int(time.time()), "reason": reason, "setup_id": setup_id,
                }
        if timed_out or (not expected and not reached_online):
            self._failed_start = (time.time(), "start_timeout" if timed_out else "crashed_starting")
        if timed_out:
            self.log("Minecraft was stopped because it never finished starting.", "error")
        elif expected:
            self.log("Minecraft stopped. Sleeping until someone joins.")
        else:
            self.log(f"Minecraft exited unexpectedly (exit code {code}).", "error")
        try:
            ix.sync_access_lists_from_runtime()
        except Exception as exc:
            self.log(f"Could not sync the op and whitelist files: {exc}", "warn")
        try:
            # Settings saved while it ran, in case Minecraft wrote over them.
            ix.apply_pending_properties()
        except Exception as exc:
            self.log(f"Could not re-apply saved settings: {exc}", "warn")
        self._info_cache = (0.0, {})
        self.ensure_listener()

    def _exit_reason(self) -> str:
        """The most telling recent line: a throwable, else an error, else the last line."""
        lines = [line for line in self._recent if line.strip()]

        def message(line: str) -> str:
            text = line.split("]: ", 1)[-1] if "]: " in line else line
            return text.strip()[:300]

        candidates = [line for line in lines if not _STACK_FRAME.match(line)]
        for line in reversed(candidates):
            if _THROWABLE.search(line):
                return message(line)
        for line in reversed(candidates):
            if any(marker in line for marker in ("/ERROR]", "/FATAL]", "[SEVERE]")):
                return message(line)
        if candidates:
            return message(candidates[-1])
        return "Minecraft exited without printing a reason."

    # -- console -----------------------------------------------------------
    def _write(self, proc: Any, command: str) -> bool:
        try:
            with self._write_lock:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def send_command(self, command: str) -> bool:
        with self.state.lock:
            proc = self.state.process
            phase = self.state.phase
        if proc is None or phase not in (STARTING, ONLINE):
            return False
        return self._write(proc, command)

    def set_whitelist(self, enabled: bool, timeout: float = 5.0) -> None:
        """Turn the whitelist on or off, live when Minecraft is running.

        Minecraft saves its in-memory server.properties while it handles
        `whitelist on|off`, which would overwrite keys written just before
        (enforce-whitelist went back to false). So the command goes first and
        the installer writes the file once Minecraft has confirmed, putting
        back every other key Minecraft overwrote (settings saved since the
        server started)."""
        keep = ix.read_properties()
        waiter = self.expect(WHITELIST_RE)
        try:
            if self.send_command("whitelist on" if enabled else "whitelist off"):
                if not waiter.event.wait(timeout):
                    self.log("Minecraft didn't confirm the whitelist change; saving it anyway.", "warn")
        finally:
            self._forget(waiter)
        ix.set_whitelist_enabled(enabled, keep=keep)

    def expect(self, regex: re.Pattern[str]) -> _Waiter:
        waiter = _Waiter(regex)
        with self._waiters_lock:
            self._waiters.append(waiter)
        return waiter

    def _forget(self, waiter: _Waiter) -> None:
        with self._waiters_lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    # -- log monitor ---------------------------------------------------------
    def _monitor(self, proc: Any) -> None:
        stream = proc.stdout
        try:
            while stream is not None:
                raw = stream.readline()
                if not raw:
                    break
                try:
                    self._handle_line(proc, raw)
                except Exception:
                    print(traceback.format_exc(), flush=True)
        except (OSError, ValueError):
            pass
        try:
            proc.wait()
        except Exception:
            pass
        with self.state.lock:
            expected = self._stopping_proc is proc
        if not expected:
            self._finish_exit(proc, expected=False)

    def _handle_line(self, proc: Any, raw: str) -> None:
        line = _ANSI.sub("", raw).rstrip()
        if not line:
            return
        self.state.add_minecraft_line(line)
        self._recent.append(line)
        if self._waiters:
            with self._waiters_lock:
                matched = [w for w in self._waiters if w.regex.search(line)]
                for waiter in matched:
                    self._waiters.remove(waiter)
            for waiter in matched:
                waiter.line = line
                waiter.event.set()
        if self._list_waiter is not None:
            self._check_list(line)
        with self.state.lock:
            if self.state.process is not proc:
                return
            phase = self.state.phase
        if phase == STARTING and DONE_RE.search(line):
            self._mark_online(proc)
            return
        for regex in JOIN_RES:
            match = regex.search(line)
            if match:
                with self.state.lock:
                    self.state.players.add(match.group(1))
                    self.state.idle_since = None
                return
        for regex in LEAVE_RES:
            match = regex.search(line)
            if match:
                name = match.group(1)
                with self.state.lock:
                    self.state.players = {p for p in self.state.players if p.lower() != name.lower()}
                    if not self.state.players and self.state.phase == ONLINE:
                        self.state.idle_since = time.time()
                return

    def _mark_online(self, proc: Any) -> None:
        now = time.time()
        with self.state.lock:
            if self.state.process is not proc or self.state.phase != STARTING:
                return
            self.state.phase = ONLINE
            self.state.online_since = now
            self.state.idle_since = None if self.state.players else now
            self.state.last_exit = None
            spec = self.state.runtime
        self._failed_start = None
        self.log("Minecraft is ready. Players can join now.")
        try:
            ix.record_world_version(
                str(getattr(spec, "level_name", "")),
                str(getattr(spec, "version", "")),
                str(getattr(spec, "type", "vanilla")),
            )
        except Exception as exc:
            self.log(f"Could not record the world's version: {exc}", "warn")

    def _check_list(self, line: str) -> None:
        waiter = self._list_waiter
        if waiter is None:
            return
        if waiter.expect_names:
            match = LIST_NAMES_RE.search(line)
            waiter.done(_parse_names(match.group(1)) if match else [])
            return
        match = LIST_COUNT_RE.search(line)
        if match:
            count = int(match.group(1))
            names = _parse_names(match.group(3))
            if count == 0 or names:
                waiter.done(names)
            else:
                waiter.expect_names = True
            return
        legacy = LIST_LEGACY_RE.search(line)
        if legacy:
            waiter.done(_parse_names(legacy.group(1)))

    def reconcile_players(self, timeout: float = 3.0) -> list[str] | None:
        """Ask the server who is online ("list"); None when it doesn't answer."""
        with self.state.lock:
            proc = self.state.process
        if proc is None:
            return None
        waiter = _ListWaiter()
        self._list_waiter = waiter
        try:
            if not self._write(proc, "list"):
                return None
            if waiter.event.wait(timeout):
                return waiter.names
            return None
        finally:
            self._list_waiter = None

    # -- idle sleep ------------------------------------------------------------
    def idle_timeout_seconds(self) -> int:
        cached_at, value = self._idle_cache
        if time.time() - cached_at < 10:
            return value
        try:
            minutes = ix.railpowered_settings().get("idle_timeout_minutes", 0)
            value = max(0, int(round(float(minutes) * 60)))
        except Exception:
            value = config.IDLE_TIMEOUT
        self._idle_cache = (time.time(), value)
        return value

    def invalidate_settings(self) -> None:
        self._idle_cache = (0.0, 0)
        self._info_cache = (0.0, {})

    def seconds_until_sleep(self, now: float | None = None) -> int | None:
        timeout = self.idle_timeout_seconds()
        now = time.time() if now is None else now
        with self.state.lock:
            if (
                timeout <= 0
                or self.state.phase != ONLINE
                or self.state.players
                or self.state.idle_since is None
            ):
                return None
            return max(0, int(timeout - (now - self.state.idle_since)))

    def idle_tick(self, now: float | None = None) -> bool:
        """One idle check. Returns True when it stopped the server."""
        now = time.time() if now is None else now
        if self._start_watchdog(now):
            return True
        timeout = self.idle_timeout_seconds()
        busy = self.tasks.is_busy()  # never take the task lock inside state.lock
        with self.state.lock:
            should = idle_should_stop(
                self.state.phase,
                len(self.state.players),
                self.state.idle_since,
                timeout,
                now,
                busy=busy,
            )
        if not should:
            return False
        names = self.reconcile_players()
        if names:
            with self.state.lock:
                self.state.players = set(names)
                self.state.idle_since = None
            self.log(f"Idle check found players online ({', '.join(names)}); staying up.")
            return False
        minutes = max(1, timeout // 60)
        self.log(
            f"No players for {minutes} minute{'s' if minutes != 1 else ''}; "
            "going to sleep. The next join wakes it up."
        )
        try:
            return self.stop(reason="idle")
        except ServerError:
            return False

    def _start_watchdog(self, now: float) -> bool:
        """Stop a server that never printed "Done (" within START_TIMEOUT."""
        limit = config.START_TIMEOUT
        with self.state.lock:
            proc, phase, started = self.state.process, self.state.phase, self.state.start_time
        if limit <= 0 or proc is None or phase != STARTING or started is None or now - started < limit:
            return False
        recent = [line for line in self._recent if line.strip()][-5:]
        self.log(
            f"Minecraft hasn't finished starting after {int(limit // 60)} minutes; stopping it. "
            "Its last lines are above.",
            "error",
        )
        for line in recent:
            self.log(f"  {line[:300]}", "warn")
        self._timed_out = proc
        try:
            return self.stop(ignore_task=True, reason="start timed out")
        except ServerError:
            self._timed_out = None
            return False

    def idle_loop(self, interval: float = 5.0) -> None:
        while True:
            time.sleep(interval)
            try:
                self.idle_tick()
            except Exception:
                print(traceback.format_exc(), flush=True)

    # -- wake-on-join ----------------------------------------------------------
    def request_wake(self, name: str | None = None) -> bool:
        with self.state.lock:
            if self.state.phase in (STARTING, ONLINE, STOPPING):
                return False
        if self.tasks.is_busy() or self.wake_blocked():
            return False
        if not self._wake_lock.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                who = name if name and PLAYER_RE.match(name) else "a player"
                self.start(reason=f"{who} joined")
            except ServerError:
                pass
            except Exception:
                print(traceback.format_exc(), flush=True)
            finally:
                self._wake_lock.release()

        threading.Thread(target=run, name="wake", daemon=True).start()
        return True

    def proxy_info(self) -> dict[str, Any]:
        """What the sleeping proxy shows. Cached briefly; never touches the network."""
        with self.state.lock:
            phase = self.state.phase
        if self.tasks.is_busy():
            mode = MODE_UPDATING
        elif phase == STARTING:
            mode = MODE_STARTING
        elif self.wake_blocked():
            mode = MODE_CRASHED
        else:
            mode = MODE_SLEEPING
        cached_at, info = self._info_cache
        if not info or time.time() - cached_at > 5:
            info = {}
            whitelist_on = False
            try:
                props = ix.read_properties()  # already unescaped
                info["motd"] = props.get("motd", "") or "A Minecraft Server"
                try:
                    info["max_players"] = int(props.get("max-players", "20"))
                except ValueError:
                    info["max_players"] = 20
                # Minecraft reads the flag case-insensitively; so must we.
                whitelist_on = props.get("white-list", "false").strip().lower() == "true"
                # With the whitelist on, nobody wakes the server until the
                # list has actually been read (fail closed).
                info["allowed"] = set() if whitelist_on else None
                setup = ix.active_setup()
                info["version_name"] = setup.get("resolved_version") or ""
                if whitelist_on:
                    lists = ix.players()
                    info["allowed"] = {
                        entry["name"].lower() for entry in lists["ops"] + lists["whitelist"]
                    }
                self._whitelist_error_logged = False
            except Exception as exc:
                if whitelist_on and not self._whitelist_error_logged:
                    self._whitelist_error_logged = True
                    self.log(f"Couldn't read the whitelist for the sleep proxy ({exc}); "
                             "joins won't wake the server until it can.", "warn")
            self._info_cache = (time.time(), info)
        return {**info, "mode": mode}

    # -- backups -----------------------------------------------------------------
    def backup_world(self, world: str) -> dict[str, Any]:
        """Back up a world; a running world is flushed with save-off/save-on."""
        self.settle()
        with self.state.lock:
            proc = self.state.process
            phase = self.state.phase
            spec = self.state.runtime
        live = proc is not None and getattr(spec, "level_name", None) == world
        if not live:
            return ix.create_backup(world)
        if phase != ONLINE:
            raise ix.install_error(
                "The server is still starting. Back up again once it's online.", "busy"
            )
        legacy = ix.is_legacy(getattr(spec, "version", None))

        def before_tar() -> None:
            # Stop autosaves, flush everything to disk, then archive.
            self._write(proc, "save-off")
            waiter = self.expect(SAVED_RE)
            self._write(proc, "save-all" if legacy else "save-all flush")
            if not waiter.event.wait(90):
                self._forget(waiter)
                self.log("The save didn't confirm within 90s; backing up anyway.", "warn")

        def after_tar() -> None:
            with self.state.lock:
                still_running = self.state.process is proc
            if still_running:
                self._write(proc, "save-on")

        # The installer runs after_tar in a finally, so save-on always follows.
        return ix.create_backup(world, before_tar=before_tar, after_tar=after_tar)

    # -- status ----------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        phase = self.public_phase()
        with self.state.lock:
            players = sorted(self.state.players, key=str.lower)
            start_time = self.state.start_time
            last_exit = dict(self.state.last_exit) if self.state.last_exit else None
            runtime = self.state.runtime if self.state.process is not None else None
        if last_exit is not None:
            last_exit.update(self._wake_info(now))
        return {
            "phase": phase,
            "players": players,
            "uptime": int(now - start_time) if start_time else None,
            "idle": {
                "timeout_seconds": self.idle_timeout_seconds(),
                "seconds_until_sleep": self.seconds_until_sleep(now),
            },
            "last_exit": last_exit,
            "runtime": runtime,
        }


# -- module singletons (production wiring) -------------------------------------
state = default_state
tasks = TaskRunner()
controller = ServerController(state=state, tasks=tasks, port=config.MC_PORT)
sleep_proxy = controller.proxy


def ensure_listener() -> bool:
    return controller.ensure_listener()


def request_wake(name: str | None = None) -> bool:
    return controller.request_wake(name)


def start_server() -> Any:
    return controller.start()


def stop_server() -> bool:
    return controller.stop()


def send_command(command: str) -> bool:
    return controller.send_command(command)


def idle_checker() -> None:
    controller.idle_loop()
