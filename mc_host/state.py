"""Shared Minecraft process state."""

from __future__ import annotations

import subprocess
import threading
import time

import installer


class ServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self._running = False
        self._starting = False
        self._stopping = False
        self._installing = False
        self.players: set[str] = set()
        self.last_activity: float = time.time()
        self.start_time: float | None = None
        self._log_lines: list[str] = []
        self._max_log_lines = 300
        self.runtime: installer.RuntimeSpec | None = None

    @property
    def running(self):
        return self._running

    @running.setter
    def running(self, v):
        self._running = v

    @property
    def starting(self):
        return self._starting

    @starting.setter
    def starting(self, v):
        self._starting = v

    @property
    def stopping(self):
        return self._stopping

    @stopping.setter
    def stopping(self, v):
        self._stopping = v

    @property
    def installing(self):
        return self._installing

    @installing.setter
    def installing(self, v):
        self._installing = v

    @property
    def player_count(self):
        return len(self.players)

    def add_log(self, line):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {line}"
        with self.lock:
            self._log_lines.append(entry)
            if len(self._log_lines) > self._max_log_lines:
                self._log_lines = self._log_lines[-self._max_log_lines :]
        print(entry, flush=True)

    def get_logs(self, count=100):
        with self.lock:
            return list(self._log_lines[-count:])


state = ServerState()
