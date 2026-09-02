"""Minecraft process start/stop, console, idle auto-stop, and wake wiring."""

from __future__ import annotations

import re
import subprocess
import threading
import time

import installer

from .config import IDLE_TIMEOUT, MAX_MEMORY, MC_PORT, MIN_MEMORY
from .proxy import SleepProxy
from .state import state

_waking = threading.Event()


def request_wake():
    if state.running or state.starting or _waking.is_set():
        return
    _waking.set()
    threading.Thread(target=_do_wake, daemon=True).start()


def _do_wake():
    try:
        state.add_log("Player detected! Waking up server...")
        sleep_proxy.stop()
        time.sleep(0.5)
        start_server()
    finally:
        _waking.clear()


sleep_proxy = SleepProxy(MC_PORT, request_wake)

JOIN_PATTERNS = [
    re.compile(r"\b([A-Za-z0-9_]{1,16}) joined the game\b"),
    re.compile(r"\b([A-Za-z0-9_]{1,16}) \[[^\]]*\] logged in"),
]
LEAVE_PATTERNS = [
    re.compile(r"\b([A-Za-z0-9_]{1,16}) left the game\b"),
    re.compile(r"\b([A-Za-z0-9_]{1,16}) lost connection"),
]
DONE_PATTERN = re.compile(r"Done \(")


def start_server():
    with state.lock:
        if state.running or state.starting or state.installing:
            return False
        state.starting = True

    state.players.clear()
    state.last_activity = time.time()

    try:
        spec = installer.prepare_runtime()
        state.runtime = spec
        command = [
            spec.java_bin,
            f"-Xmx{MAX_MEMORY}",
            f"-Xms{MIN_MEMORY}",
            *spec.jvm_args,
            *spec.prefix_args,
        ]
        if spec.jar:
            command.extend(["-jar", spec.jar])
        command.extend(spec.extra_args)
        state.add_log(
            f"Starting {spec.type} Minecraft {spec.version} "
            f"(Java {spec.java_major}, world {spec.level_name})..."
        )
        state.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=spec.cwd,
            bufsize=1,
            universal_newlines=True,
        )
        threading.Thread(target=_monitor_logs, daemon=True).start()
        with state.lock:
            state.running = True
            state.starting = False
            state.start_time = time.time()
        return True
    except Exception as e:
        state.add_log(f"Failed to start server: {e}")
        with state.lock:
            state.starting = False
            state.runtime = None
        sleep_proxy.start()
        return False


def stop_server():
    with state.lock:
        if not state.running or state.stopping:
            return False
        state.stopping = True

    state.add_log("Stopping Minecraft server...")
    try:
        if state.process and state.process.stdin:
            state.process.stdin.write("stop\n")
            state.process.stdin.flush()
        if state.process:
            state.process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        state.add_log("Server didn't stop in time — force killing")
        if state.process:
            state.process.kill()
            try:
                state.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    except (BrokenPipeError, OSError) as e:
        state.add_log(f"Error sending stop command: {e}")
        if state.process:
            try:
                state.process.kill()
            except OSError:
                pass

    with state.lock:
        state.running = False
        state.stopping = False
        state.players.clear()
        state.start_time = None

    state.add_log("Server stopped.")
    time.sleep(0.5)
    sleep_proxy.start()
    return True


def send_command(cmd):
    if state.running and state.process and state.process.stdin:
        try:
            state.process.stdin.write(cmd + "\n")
            state.process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False
    return False


def require_stopped(lock_install: bool = False) -> None:
    with state.lock:
        if state.running or state.starting or state.stopping or state.installing:
            raise installer.InstallError("Stop the server before changing this setting")
        if lock_install:
            state.installing = True


def clear_installing() -> None:
    with state.lock:
        state.installing = False


def _monitor_logs():
    proc = state.process
    if proc is None or proc.stdout is None:
        return
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\n\r")
            if not line:
                continue
            state.add_log(line)
            if DONE_PATTERN.search(line):
                state.add_log(">> Server is ready for connections!")
                state.last_activity = time.time()
                continue
            for pattern in JOIN_PATTERNS:
                match = pattern.search(line)
                if match:
                    state.players.add(match.group(1))
                    state.last_activity = time.time()
                    break
            else:
                for pattern in LEAVE_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        state.players.discard(match.group(1))
                        state.last_activity = time.time()
                        break
    except Exception as e:
        state.add_log(f"Log monitor error: {e}")

    with state.lock:
        was_stopping = state.stopping
        if not was_stopping:
            state.running = False
            state.starting = False
            state.start_time = None
    if not was_stopping:
        state.add_log("Server process exited unexpectedly.")
        time.sleep(0.5)
        sleep_proxy.start()


def idle_checker():
    while True:
        time.sleep(30)
        if not state.running or state.stopping:
            continue
        if state.player_count > 0:
            continue
        idle_secs = time.time() - state.last_activity
        if idle_secs >= IDLE_TIMEOUT:
            state.add_log(
                f"No players for {int(idle_secs)}s (limit {IDLE_TIMEOUT}s). "
                "Auto-stopping to save resources..."
            )
            stop_server()
