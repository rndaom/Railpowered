"""Process entry: dashboard first, then the sleep proxy, then a background prepare.

Boot order matters on Railway: /health must answer within the healthcheck
window even when the first deploy downloads Forge, so the HTTP server starts
before any runtime work, and preparing the server runs as the "prepare" task
(the dashboard shows it, and the proxy answers joins with "updating").

SIGTERM only sets a flag. The main thread notices it, saves and stops
Minecraft (save-all flush, stop, up to STOP_TIMEOUT seconds), releases the
port and exits, inside Railway's drainingSeconds window.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from typing import Any

import installer

from . import auth, config
from . import installer_api as ix
from .panel import PanelHandler, PanelServer
from .process import controller, tasks
from .state import state

_flags = {"shutdown": False}


def _on_signal(signum: int, _frame: Any) -> None:
    # No locks, no logging: this runs between bytecodes on the main thread.
    _flags["shutdown"] = True


def request_shutdown() -> None:
    _flags["shutdown"] = True


def announce_admin_key() -> None:
    key, source = config.admin_key_info()
    path = config.admin_key_path()
    if source == "generated":
        # stdout only (never the dashboard log), and only on the boot that
        # created the key.
        rule = "=" * 68
        print(
            "\n".join(
                [
                    rule,
                    "Railpowered created a dashboard key. It is shown only this once:",
                    "",
                    f"    {key}",
                    "",
                    f"It is saved on the volume at {path}.",
                    "To choose your own key, set the ADMIN_KEY variable in Railway.",
                    rule,
                ]
            ),
            flush=True,
        )
        state.add_log(f"A new dashboard key was created and printed once to the deploy log ({path}).")
    elif source == "volume":
        state.add_log(
            f"Dashboard key: the one saved at {path}. Set ADMIN_KEY in Railway to choose your own."
        )
    else:
        state.add_log("Dashboard key: ADMIN_KEY from Railway Variables.")
        if len(key) < config.MIN_RECOMMENDED_KEY_LENGTH:
            state.add_log(
                f"ADMIN_KEY is shorter than {config.MIN_RECOMMENDED_KEY_LENGTH} characters. "
                "A longer key is much harder to guess.",
                "warn",
            )


def _note_env_changes() -> None:
    """SERVER_TYPE / MINECRAFT_VERSION only seed the first setup. When they
    change later, say so in the log instead of ignoring them silently."""
    try:
        pending = installer.pending_env_setup()
        if pending is not None:
            state.add_log(
                "SERVER_TYPE / MINECRAFT_VERSION changed in Railway, but they only choose the "
                "first setup. Switch versions from the dashboard's Setups page instead.",
                "warn",
            )
            installer.acknowledge_env_setup()
    except Exception as exc:
        state.add_log(f"Could not compare the Railway variables with the saved setup: {exc}", "warn")
    for warning in installer.config_warnings(clear=True):
        state.add_log(warning, "warn")


def _prepare_runtime() -> None:
    spec = ix.prepare_runtime()
    state.add_log(
        f"Server files are ready: {getattr(spec, 'type', 'minecraft')} "
        f"{getattr(spec, 'version', '')} on Java {getattr(spec, 'java_major', '?')}."
    )


def _auto_start(_task: Any) -> None:
    try:
        controller.start(reason="AUTO_START")
    except Exception as exc:
        state.add_log(f"Automatic start did not happen: {exc}", "warn")


def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    for warning in config.CONFIG_WARNINGS:
        state.add_log(warning, "warn")
    # Installer progress ("Downloading Minecraft 1.21.1...") goes to the dashboard log.
    installer.set_log_handler(state.add_log)
    installer.ensure_layout()
    # Before anything can start a task: put back what a redeploy cut short
    # (a pack swap, a restore) and reclaim hidden staging and temp files.
    try:
        installer.recover_interrupted()
    except Exception as exc:  # never keep the server from booting
        state.add_log(f"Couldn't clean up after interrupted work: {exc}", "warn")
    _note_env_changes()
    announce_admin_key()
    auth.ensure_session_secret()

    try:
        http = PanelServer(("0.0.0.0", config.WEB_PORT), PanelHandler)
    except OSError as exc:
        state.add_log(f"Could not open the dashboard port {config.WEB_PORT}: {exc}", "error")
        sys.exit(1)
    threading.Thread(target=http.serve_forever, name="dashboard", daemon=True).start()
    state.add_log(f"Dashboard listening on port {config.WEB_PORT}.")
    if config.MC_PUBLIC_ADDRESS:
        state.add_log(f"Join address: {config.MC_PUBLIC_ADDRESS}")
    else:
        state.add_log("The join address appears once a Railway TCP proxy points at port 25565.")
    try:
        settings = ix.railpowered_settings()
    except Exception:
        settings = {"idle_timeout_minutes": config.IDLE_TIMEOUT // 60, "auto_start": config.AUTO_START}
    idle = settings.get("idle_timeout_minutes") or 0
    state.add_log(
        f"Heap {config.MIN_MEMORY}-{config.MAX_MEMORY}; sleep timer "
        + (f"{idle} min" if idle else "off")
        + f"; stop grace {config.STOP_TIMEOUT}s."
    )

    controller.ensure_listener()
    threading.Thread(target=controller.idle_loop, name="idle", daemon=True).start()

    auto_start = bool(settings.get("auto_start"))
    tasks.submit(
        "prepare",
        "Preparing the server files",
        lambda _ctx: _prepare_runtime(),
        on_success=_auto_start if auto_start else None,
    )

    while not _flags["shutdown"]:
        time.sleep(0.5)

    state.add_log("Shutdown requested; saving the world and stopping Minecraft…")
    controller.shutdown()
    http.shutdown()
    http.server_close()
    state.add_log("Railpowered stopped.")
