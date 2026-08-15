#!/usr/bin/env python3
"""
Vanilla Minecraft Server Manager

- Sleep proxy on port 25565: auto-starts the server when a player connects
- Monitors player activity from server logs
- Auto-stops after configurable idle timeout to save Railway costs
- Admin-only web panel on the HTTP port for status/logs/controls
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
MC_DIR = "/server/data"
MC_JAR = os.environ.get("MC_JAR", "server.jar")
MC_VERSION = os.environ.get("MINECRAFT_VERSION", "26.2")
MC_PORT = 25565
WEB_PORT = int(os.environ.get("PORT", 8080))
MAX_MEMORY = os.environ.get("MC_MAX_MEMORY", "1G")
MIN_MEMORY = os.environ.get("MC_MIN_MEMORY", "512M")
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))  # 10 min default
AUTO_START = os.environ.get("AUTO_START", "false").lower() == "true"
TEMPLATE_DIR = "/server/templates"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
_ADMIN_KEY_FROM_ENV = bool(ADMIN_KEY)
if not ADMIN_KEY:
    ADMIN_KEY = base64.urlsafe_b64encode(os.urandom(18)).decode()
ADMIN_TOKEN = hashlib.sha256(ADMIN_KEY.encode()).hexdigest()[:32]
MC_PUBLIC_ADDRESS = os.environ.get("MC_PUBLIC_ADDRESS", "").strip()

LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Admin Login</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:#1a1a2e;color:#eee;
         display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:#16213e;border-radius:8px;padding:32px;width:340px}
    h2{color:#4ecca3;margin-bottom:16px;text-align:center}
    input{width:100%;padding:10px;border-radius:6px;border:1px solid #333;
          background:#0f3460;color:#eee;font-size:1rem;margin-bottom:14px}
    input:focus{outline:none;border-color:#4ecca3}
    button{width:100%;padding:10px;border-radius:6px;border:none;
           background:#4ecca3;color:#111;font-weight:600;font-size:1rem;cursor:pointer}
    button:hover{opacity:.9}
    .error{color:#e94560;font-size:.9rem;margin-bottom:12px;text-align:center}
    .links{text-align:center;margin-top:16px}
    .links a{color:#4ecca3;font-size:.9rem}
  </style>
</head>
<body>
  <div class="card">
    <h2>Admin Login</h2>
    {error}
    <form method="POST" action="/api/login">
      <input type="password" name="key" placeholder="Admin key" autofocus>
      <button type="submit">Login</button>
    </form>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server state (thread-safe)
# ---------------------------------------------------------------------------
class ServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self._running = False
        self._starting = False
        self._stopping = False
        self.players: set[str] = set()
        self.last_activity: float = time.time()
        self.start_time: float | None = None
        self._log_lines: list[str] = []
        self._max_log_lines = 300

    # -- Thread-safe property access --

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


# ---------------------------------------------------------------------------
# Sleep Proxy — listens on MC port when server is off
# ---------------------------------------------------------------------------
class SleepProxy:
    """
    When the Minecraft server is stopped, this proxy binds to port 25565.
    Status pings get a sleeping MOTD. Login attempts get a modern Java
    disconnect message and wake the MC server.
    """

    def __init__(self, port: int, on_wake: Callable[[], None]) -> None:
        self.port = port
        self.on_wake = on_wake
        self._socket: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def active(self):
        return self._running

    def start(self):
        if self._running:
            return

        # Retry binding in case the port isn't released yet
        for attempt in range(10):
            sock: socket.socket | None = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(1.0)
                sock.bind(("0.0.0.0", self.port))
                sock.listen(5)
                self._socket = sock
                break
            except OSError as e:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if attempt < 9:
                    time.sleep(1)
                else:
                    state.add_log(f"Sleep proxy: failed to bind port {self.port}: {e}")
                    return

        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        state.add_log(f"Sleep proxy active on port {self.port} — waiting for players")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def _accept_loop(self) -> None:
        while self._running:
            srv = self._socket
            if srv is None:
                break
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(client, addr), daemon=True
            ).start()

    def _handle_client(self, client, addr):
        wake_server = True
        try:
            client.settimeout(2.0)
            state.add_log(f"Connection from {addr[0]} — server is sleeping")
            if state.starting:
                msg = "Server is starting up! Reconnect in ~20 seconds."
            else:
                msg = "Server is waking up! Reconnect in ~30 seconds."

            protocol_version, next_state = self._read_handshake(client)
            if next_state == 1:
                wake_server = False
                self._send_status_response(client, protocol_version)
            else:
                self._send_login_disconnect(client, msg)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

        if wake_server:
            self.on_wake()

    @classmethod
    def _read_handshake(cls, sock: socket.socket) -> tuple[int | None, int | None]:
        data = cls._read_packet(sock)
        packet_id, offset = cls._read_varint_from(data, 0)
        if packet_id != 0:
            return None, None

        protocol_version, offset = cls._read_varint_from(data, offset)
        address_len, offset = cls._read_varint_from(data, offset)
        offset += address_len
        offset += 2  # server port
        next_state, _ = cls._read_varint_from(data, offset)
        return protocol_version, next_state

    @classmethod
    def _send_status_response(
        cls, sock: socket.socket, protocol_version: int | None
    ) -> None:
        status = {
            "version": {
                "name": MC_VERSION,
                "protocol": protocol_version or 0,
            },
            "players": {
                "max": 20,
                "online": 0,
                "sample": [],
            },
            "description": {
                "text": "Server sleeping - join to wake",
            },
            "previewsChat": False,
            "enforcesSecureChat": False,
        }
        cls._send_packet(sock, 0x00, cls._pack_string(json.dumps(status)))

        try:
            sock.settimeout(1.0)
            ping = cls._read_packet(sock)
            packet_id, offset = cls._read_varint_from(ping, 0)
            if packet_id == 0x01:
                cls._send_packet(sock, 0x01, ping[offset:])
        except Exception:
            pass

    @classmethod
    def _send_login_disconnect(cls, sock: socket.socket, message: str) -> None:
        component = json.dumps({"text": message}, separators=(",", ":"))
        cls._send_packet(sock, 0x00, cls._pack_string(component))

    @classmethod
    def _read_packet(cls, sock: socket.socket) -> bytes:
        length = cls._read_varint(sock)
        return cls._recv_exact(sock, length)

    @staticmethod
    def _recv_exact(sock: socket.socket, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = sock.recv(length - len(chunks))
            if not chunk:
                raise EOFError("socket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    @classmethod
    def _read_varint(cls, sock: socket.socket) -> int:
        value = 0
        for position in range(5):
            byte = cls._recv_exact(sock, 1)[0]
            value |= (byte & 0x7F) << (7 * position)
            if not byte & 0x80:
                return value
        raise ValueError("VarInt is too large")

    @staticmethod
    def _read_varint_from(data: bytes, offset: int) -> tuple[int, int]:
        value = 0
        for position in range(5):
            if offset >= len(data):
                raise EOFError("buffer ended while reading VarInt")
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << (7 * position)
            if not byte & 0x80:
                return value, offset
        raise ValueError("VarInt is too large")

    @classmethod
    def _send_packet(cls, sock: socket.socket, packet_id: int, payload: bytes) -> None:
        packet = cls._pack_varint(packet_id) + payload
        sock.sendall(cls._pack_varint(len(packet)) + packet)

    @staticmethod
    def _pack_varint(value: int) -> bytes:
        out = bytearray()
        while True:
            temp = value & 0x7F
            value >>= 7
            if value:
                temp |= 0x80
            out.append(temp)
            if not value:
                return bytes(out)

    @classmethod
    def _pack_string(cls, value: str) -> bytes:
        encoded = value.encode("utf-8")
        return cls._pack_varint(len(encoded)) + encoded


# ---------------------------------------------------------------------------
# Wake-up coordination
# ---------------------------------------------------------------------------
_waking = threading.Event()


def request_wake():
    """Called by the sleep proxy when a player connects."""
    if state.running or state.starting or _waking.is_set():
        return
    _waking.set()
    threading.Thread(target=_do_wake, daemon=True).start()


def _do_wake():
    """Stop the proxy, free the port, start the MC server."""
    try:
        state.add_log("Player detected! Waking up server...")
        sleep_proxy.stop()
        time.sleep(0.5)  # let the OS release the port
        start_server()
    finally:
        _waking.clear()


sleep_proxy = SleepProxy(MC_PORT, request_wake)


# ---------------------------------------------------------------------------
# Minecraft process management
# ---------------------------------------------------------------------------
def start_server():
    """Start the Minecraft server process."""
    with state.lock:
        if state.running or state.starting:
            return False
        state.starting = True

    state.players.clear()
    state.last_activity = time.time()
    state.add_log(f"Starting Minecraft {MC_VERSION} vanilla server...")

    try:
        state.process = subprocess.Popen(
            [
                "java",
                f"-Xmx{MAX_MEMORY}",
                f"-Xms{MIN_MEMORY}",
                "-jar",
                MC_JAR,
                "nogui",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=MC_DIR,
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
        # Server failed — re-enable sleep proxy so players can still trigger retries
        sleep_proxy.start()
        return False


def stop_server():
    """Gracefully stop the Minecraft server, then re-enable the sleep proxy."""
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

    # Re-enable sleep proxy so the next player connection wakes the server
    time.sleep(0.5)
    sleep_proxy.start()
    return True


def send_command(cmd):
    """Send a console command to the running MC server."""
    if state.running and state.process and state.process.stdin:
        try:
            state.process.stdin.write(cmd + "\n")
            state.process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False
    return False


# ---------------------------------------------------------------------------
# Log monitoring
# ---------------------------------------------------------------------------
JOIN_PATTERN = re.compile(r"\b([A-Za-z0-9_]{1,16}) joined the game\b")
LEAVE_PATTERN = re.compile(r"\b([A-Za-z0-9_]{1,16}) left the game\b")
DONE_PATTERN = re.compile(r"Done \([\d.]+s\)")


def _monitor_logs():
    """Read MC server stdout line-by-line and track player events."""
    proc = state.process
    if proc is None or proc.stdout is None:
        return
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break  # EOF — process exited
            line = line.rstrip("\n\r")
            if not line:
                continue

            state.add_log(line)

            # Server finished booting
            if DONE_PATTERN.search(line):
                state.add_log(">> Server is ready for connections!")
                state.last_activity = time.time()
                continue

            # Player joined
            m = JOIN_PATTERN.search(line)
            if m:
                player = m.group(1)
                state.players.add(player)
                state.last_activity = time.time()
                continue

            # Player left
            m = LEAVE_PATTERN.search(line)
            if m:
                player = m.group(1)
                state.players.discard(player)
                state.last_activity = time.time()
                continue

    except Exception as e:
        state.add_log(f"Log monitor error: {e}")

    # If the process exited on its own (crash, not via stop_server), clean up
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


# ---------------------------------------------------------------------------
# Idle auto-shutdown
# ---------------------------------------------------------------------------
def _idle_checker():
    """Stop the server after IDLE_TIMEOUT seconds with no players."""
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


# ---------------------------------------------------------------------------
# Admin web panel
# ---------------------------------------------------------------------------
class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self):
        # --- Public routes ---
        if self.path == "/health":
            self._send_json({"status": "ok"})
        elif self.path == "/api/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", "admin_session=; Path=/; Max-Age=0")
            self.send_header("Location", "/")
            self.end_headers()
        # --- Admin panel (login wall) ---
        elif self.path in ("/", ""):
            if self._is_admin():
                self._serve_panel()
            else:
                self._send_login()
        # --- Admin API (401 if not authed) ---
        elif self.path.startswith("/api/"):
            if not self._is_admin():
                self._send_json({"error": "unauthorized"}, 401)
                return
            if self.path == "/api/status":
                self._serve_status()
            elif self.path == "/api/logs":
                self._serve_logs()
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        # Login is always accessible
        if self.path == "/api/login":
            self._do_login()
            return
        # All other POST routes require auth
        if not self._is_admin():
            self._send_json({"error": "unauthorized"}, 401)
            return
        if self.path == "/api/start":
            if sleep_proxy.active:
                sleep_proxy.stop()
                time.sleep(0.5)
            ok = start_server()
            self._send_json({"success": ok})
        elif self.path == "/api/stop":
            ok = stop_server()
            self._send_json({"success": ok})
        elif self.path == "/api/command":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else ""
            try:
                data = json.loads(body)
                cmd = data.get("command", "")
            except (json.JSONDecodeError, AttributeError):
                cmd = body.strip()
            ok = send_command(cmd) if cmd else False
            self._send_json({"success": ok})
        else:
            self.send_error(404)

    # -- auth --

    def _is_admin(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                return part[len("admin_session="):] == ADMIN_TOKEN
        return False

    def _send_login(self, error=""):
        replacement = f'<p class="error">{error}</p>' if error else ""
        self._send_html(LOGIN_HTML.replace("{error}", replacement))

    def _do_login(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        params = parse_qs(body)
        key = params.get("key", [""])[0]
        if key == ADMIN_KEY:
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"admin_session={ADMIN_TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800",
            )
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._send_login("Incorrect admin key.")

    # -- helpers --

    def _serve_panel(self):
        self._serve_template("index.html")

    def _serve_template(self, name):
        try:
            with open(os.path.join(TEMPLATE_DIR, name)) as f:
                html = f.read()
            if name == "index.html":
                html = html.replace(
                    "__MC_PUBLIC_ADDRESS_JSON__",
                    json.dumps(MC_PUBLIC_ADDRESS),
                )
            self._send_html(html)
        except FileNotFoundError:
            self.send_error(404, "Template not found")

    def _serve_status(self):
        uptime = int(time.time() - state.start_time) if state.start_time else None
        idle_secs = 0
        if state.running and state.player_count == 0:
            idle_secs = int(time.time() - state.last_activity)

        self._send_json(
            {
                "running": state.running,
                "starting": state.starting,
                "stopping": state.stopping,
                "players": sorted(state.players),
                "player_count": state.player_count,
                "uptime": uptime,
                "idle_timeout": IDLE_TIMEOUT,
                "idle_seconds": idle_secs,
                "proxy_active": sleep_proxy.active,
            }
        )

    def _serve_logs(self):
        self._send_json({"logs": state.get_logs(100)})

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    def handle_signal(sig, frame):
        state.add_log("Shutdown signal received.")
        sleep_proxy.stop()
        if state.running:
            # stop_server would re-enable proxy, but we're shutting down
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

    # Background threads
    threading.Thread(target=_idle_checker, daemon=True).start()

    # Either auto-start the MC server or start the sleep proxy
    if AUTO_START:
        start_server()
    else:
        sleep_proxy.start()

    # Admin web panel (keep this URL to yourself)
    HTTPServer.allow_reuse_address = True
    http = HTTPServer(("0.0.0.0", WEB_PORT), PanelHandler)
    if _ADMIN_KEY_FROM_ENV:
        state.add_log("Admin panel protected (using ADMIN_KEY env var)")
    else:
        state.add_log(f"Admin panel key (auto-generated): {ADMIN_KEY}")
        state.add_log("Tip: Set ADMIN_KEY env var in Railway for a persistent key")
    state.add_log(f"Admin panel on port {WEB_PORT}")

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


if __name__ == "__main__":
    main()
