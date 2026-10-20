"""Shared Minecraft process state and the dashboard log buffer.

Every field is read and written under `state.lock` (an RLock, so a signal
handler or nested helper on the same thread can never deadlock on it).
Lifecycle changes go through ServerController in process.py; this module
only stores data.
"""

from __future__ import annotations

import collections
import re
import subprocess
import threading
import time
from typing import Any

# Internal lifecycle phases. The public `status.phase` adds "preparing",
# which is derived from the task runner (see ServerController.public_phase).
SLEEPING = "sleeping"
STARTING = "starting"
ONLINE = "online"
STOPPING = "stopping"
CRASHED = "crashed"
PREPARING = "preparing"

LOG_CAPACITY = 1000

_MC_TIMESTAMP = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] ")
_LEGACY_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}) ")
_ERROR_MARKERS = ("/ERROR]", "/FATAL]", "[SEVERE]", "[ERROR]", "[FATAL]")
_WARN_MARKERS = ("/WARN]", "[WARNING]", "[WARN]")


def classify_level(text: str) -> str:
    """Map a Minecraft console line to info|warn|error."""
    head = text[:120]
    if any(marker in head for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in head for marker in _WARN_MARKERS):
        return "warn"
    stripped = text.lstrip()
    if stripped.startswith(("Exception in thread", "Caused by:", "java.lang.")):
        return "error"
    return "info"


class ServerState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.phase: str = SLEEPING
        self.process: subprocess.Popen[str] | None = None
        # True once the sleep proxy has released port 25565 for this start.
        self.port_handed_off = False
        self.players: set[str] = set()
        self.start_time: float | None = None
        self.online_since: float | None = None
        # When the server last became empty while online (idle timer start).
        self.idle_since: float | None = None
        self.last_exit: dict[str, Any] | None = None
        self.runtime: Any = None  # installer.RuntimeSpec of the running/last start
        self._log: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=LOG_CAPACITY
        )
        self._seq = 0
        self.echo_logs = True

    # -- players -----------------------------------------------------------
    @property
    def player_count(self) -> int:
        with self.lock:
            return len(self.players)

    def player_list(self) -> list[str]:
        with self.lock:
            return sorted(self.players, key=str.lower)

    # -- logs --------------------------------------------------------------
    def add_log(self, text: str, level: str = "info", source: str = "panel") -> int:
        """Record a Railpowered line. Never put secrets here: admins can read it."""
        text = str(text).rstrip("\r\n")
        return self._append(time.strftime("%H:%M:%S"), text, level, source)

    def add_minecraft_line(self, raw: str) -> int:
        """Record one line of Minecraft output, reusing its own timestamp."""
        text = raw.rstrip("\r\n")
        stamp = time.strftime("%H:%M:%S")
        match = _MC_TIMESTAMP.match(text)
        if match:
            stamp = match.group(1)
            text = text[match.end():]
        else:
            legacy = _LEGACY_TIMESTAMP.match(text)
            if legacy:
                stamp = legacy.group(1)
                text = text[legacy.end():]
        return self._append(stamp, text, classify_level(text), "minecraft", raw)

    def _append(
        self, stamp: str, text: str, level: str, source: str, raw: str | None = None
    ) -> int:
        if level not in ("info", "warn", "error"):
            level = "info"
        with self.lock:
            self._seq += 1
            seq = self._seq
            self._log.append(
                {"seq": seq, "t": stamp, "text": text, "level": level, "source": source}
            )
        if self.echo_logs:
            if source == "minecraft":
                line = raw if raw is not None else text
            else:
                line = f"[{stamp}] [Railpowered] {text}"
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                pass
        return seq

    def logs_after(self, after: int | None = None, limit: int = LOG_CAPACITY) -> dict[str, Any]:
        with self.lock:
            if after is None:
                lines = list(self._log)
            else:
                lines = [entry for entry in self._log if entry["seq"] > after]
            last_seq = self._seq
        if limit and len(lines) > limit:
            lines = lines[-limit:]
        return {"lines": [dict(item) for item in lines], "last_seq": last_seq}

    def get_logs(self, count: int = 100) -> list[str]:
        """Plain text lines (kept for scripts and older callers)."""
        with self.lock:
            entries = list(self._log)[-count:]
        return [f"[{item['t']}] {item['text']}" for item in entries]


state = ServerState()
