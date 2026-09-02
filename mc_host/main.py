"""Process entry: signals, runtime prepare, sleep proxy, admin HTTP server."""

from __future__ import annotations

import signal
import sys
import threading

from http.server import ThreadingHTTPServer

import installer

from .config import ADMIN_KEY, ADMIN_KEY_FROM_ENV, AUTO_START, MC_PUBLIC_ADDRESS, WEB_PORT
from .panel import PanelHandler
from .process import idle_checker, send_command, sleep_proxy, start_server
from .state import state


def main():
    def handle_signal(sig, frame):
        state.add_log("Shutdown signal received.")
        sleep_proxy.stop()
        if state.running:
            with state.lock:
                state.stopping = True
            try:
                if state.process and state.process.stdin:
                    state.process.stdin.write("stop\n")
                    state.process.stdin.flush()
                if state.process:
                    state.process.wait(timeout=30)
            except Exception:
                if state.process:
                    try:
                        state.process.kill()
                    except OSError:
                        pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    installer.ensure_layout()
    try:
        installer.prepare_runtime()
        state.add_log("Runtime files are ready")
    except Exception as exc:
        state.add_log(f"Runtime prepare warning: {exc}")

    threading.Thread(target=idle_checker, daemon=True).start()
    if AUTO_START:
        start_server()
    else:
        sleep_proxy.start()

    ThreadingHTTPServer.allow_reuse_address = True
    http = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), PanelHandler)
    if ADMIN_KEY_FROM_ENV:
        state.add_log("Log in with ADMIN_KEY from Railway Variables")
    else:
        state.add_log(f"Dashboard key (saved on the volume): {ADMIN_KEY}")
    if MC_PUBLIC_ADDRESS:
        state.add_log(f"Join address: {MC_PUBLIC_ADDRESS}")
    else:
        state.add_log("Join address appears when Railway TCP proxy 25565 is attached")
    state.add_log(f"Dashboard on port {WEB_PORT}")

    try:
        http.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        sleep_proxy.stop()
        if state.running and state.process:
            try:
                send_command("stop")
                state.process.wait(timeout=15)
            except Exception:
                pass
        http.shutdown()
