"""Shared fixtures for the runtime tests (proxy, process, panel).

Import this before mc_host: it puts the repo root on sys.path. RuntimeTestCase
gives every test an empty temporary data dir, a fixed ADMIN_KEY, quiet logs
and a network guard that fails any non-loopback DNS lookup, so no test can
reach Mojang by accident.
"""

from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import installer  # noqa: E402

from mc_host.state import state as global_state  # noqa: E402

TEST_KEY = "test-admin-key-0123456789"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "", None}


def _guarded_getaddrinfo(original):
    def guard(host, *args, **kwargs):
        if host not in _LOOPBACK:
            raise socket.gaierror(f"network access is disabled in tests ({host})")
        return original(host, *args, **kwargs)

    return guard


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = tmp.name
        patcher = mock.patch.object(installer, "DATA_DIR", tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"ADMIN_KEY": TEST_KEY})
        env.start()
        self.addCleanup(env.stop)
        for name in ("SERVER_TYPE", "MINECRAFT_VERSION"):
            os.environ.pop(name, None)
        guard = mock.patch.object(
            socket, "getaddrinfo", _guarded_getaddrinfo(socket.getaddrinfo)
        )
        guard.start()
        self.addCleanup(guard.stop)
        installer.clear_latest_cache()
        self.addCleanup(installer.clear_latest_cache)
        old_echo = global_state.echo_logs
        global_state.echo_logs = False
        self.addCleanup(setattr, global_state, "echo_logs", old_echo)
        installer.ensure_layout()

    def write_properties(self, **values: str) -> None:
        installer.write_properties({key.replace("_", "-"): value for key, value in values.items()})


class FakeProxy:
    """Stands in for SleepProxy in controller tests."""

    def __init__(self) -> None:
        self.active = False
        self.starts = 0
        self.stops = 0
        self.lock = threading.Lock()

    def start(self) -> bool:
        with self.lock:
            self.starts += 1
            self.active = True
        return True

    def stop(self) -> None:
        with self.lock:
            self.stops += 1
            self.active = False


class _FakeStdin:
    def __init__(self, proc: "FakeProcess") -> None:
        self.proc = proc

    def write(self, text: str) -> int:
        if self.proc.returncode is not None:
            raise BrokenPipeError("process exited")
        for command in text.splitlines():
            self.proc.on_command(command)
        return len(text)

    def flush(self) -> None:
        pass


class FakeProcess:
    """A Minecraft JVM double: feed it log lines, it answers a few commands."""

    def __init__(self, stops_on_command: bool = True, list_reply: str | None = None) -> None:
        self.stdin = _FakeStdin(self)
        self.stdout = self
        self.returncode: int | None = None
        self.commands: list[str] = []
        self.stops_on_command = stops_on_command
        self.list_reply = list_reply
        self._lines: queue.Queue[str] = queue.Queue()
        self._exited = threading.Event()

    # stdout
    def readline(self) -> str:
        return self._lines.get()

    def emit(self, line: str) -> None:
        self._lines.put(line + "\n")

    # lifecycle
    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self._exited.set()
            self._lines.put("")

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("java", timeout)
        return self.returncode  # type: ignore[return-value]

    def kill(self) -> None:
        self.exit(-9)

    # console
    def on_command(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("save-all"):
            self.emit("[12:00:01] [Server thread/INFO]: Saved the game")
        elif command == "list" and self.list_reply is not None:
            self.emit(self.list_reply)
        elif command == "stop" and self.stops_on_command:
            self.emit("[12:00:02] [Server thread/INFO]: Stopping the server")
            threading.Timer(0.05, self.exit, args=(0,)).start()


def fake_spec(**overrides):
    """A real installer.RuntimeSpec with test defaults (no install happens)."""
    values = dict(
        java_bin="/opt/java/21/bin/java",
        cwd=installer.DATA_DIR,
        jar="server.jar",
        extra_args=["nogui"],
        jvm_args=["-Djava.awt.headless=true"],
        prefix_args=[],
        version="1.20.1",
        type="vanilla",
        level_name="world",
        java_major=21,
        env={},
    )
    values.update(overrides)
    return installer.RuntimeSpec(**values)
