"""Sleep proxy: owns port 25565 whenever Minecraft is not running.

It speaks just enough of the Minecraft protocol to
- answer server-list pings (modern 1.7+ status + ping/pong, 1.4-1.6 and
  pre-1.4 legacy pings) with the real MOTD, version and slot count, and
- answer a login with a kick message ("waking up", "starting", "updating")
  and, when appropriate, ask the controller to start the server.

Every connection is read through a small buffered reader with a hard
deadline and packet-size limits, the number of concurrent handlers is capped,
and sockets are half-closed and drained before close so the client receives
the kick text instead of a TCP reset.
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
import time
from typing import Any, Callable

MODE_SLEEPING = "sleeping"
MODE_STARTING = "starting"
MODE_UPDATING = "updating"
MODE_CRASHED = "crashed"  # the last start failed: joins don't wake it
MODE_RESTING = "resting"  # woken lately with nobody joining: joins wait a while

MESSAGES = {
    MODE_SLEEPING: "The server is waking up! Try joining again in a minute.",
    MODE_STARTING: "The server is starting up. Try joining again in a minute.",
    MODE_UPDATING: "Server is updating, try again in a minute.",
    MODE_CRASHED: "The server couldn't start. Ask the owner to check the dashboard.",
    MODE_RESTING: "The server was woken recently but nobody joined, so it's resting for a "
                  "while. Try again later, or ask the owner to start it.",
    "not_whitelisted": "You are not whitelisted on this server.",
}
MOTD_SUFFIX = {
    MODE_SLEEPING: "Sleeping - join to wake it up",
    MODE_STARTING: "Starting up - try again in a minute",
    MODE_UPDATING: "Updating - try again in a minute",
    MODE_CRASHED: "Couldn't start - ask the owner",
    MODE_RESTING: "Sleeping - joins wake it again later",
}

# Release -> protocol number, used so a sleeping server shows its real version
# in the server list. Unknown versions echo the client's protocol instead.
MODERN_PROTOCOLS = {
    "1.7.2": 4, "1.7.4": 4, "1.7.5": 4, "1.7.6": 5, "1.7.10": 5,
    "1.8": 47, "1.8.8": 47, "1.8.9": 47,
    "1.9.4": 110, "1.10.2": 210, "1.11.2": 316,
    "1.12": 335, "1.12.1": 338, "1.12.2": 340,
    "1.13.2": 404, "1.14.4": 498, "1.15.2": 578,
    "1.16.1": 736, "1.16.2": 751, "1.16.3": 753, "1.16.4": 754, "1.16.5": 754,
    "1.17": 755, "1.17.1": 756, "1.18": 757, "1.18.1": 757, "1.18.2": 758,
    "1.19": 759, "1.19.2": 760, "1.19.3": 761, "1.19.4": 762,
    "1.20": 763, "1.20.1": 763, "1.20.2": 764, "1.20.3": 765, "1.20.4": 765,
    "1.20.5": 766, "1.20.6": 766, "1.21": 767, "1.21.1": 767,
    "1.21.2": 768, "1.21.3": 768, "1.21.4": 769, "1.21.5": 770, "1.21.6": 771,
    "1.21.7": 772, "1.21.8": 772, "1.21.9": 773, "1.21.10": 773, "1.21.11": 774,
}
LEGACY_PROTOCOLS = {
    "1.2.5": 29, "1.3.1": 39, "1.3.2": 39, "1.4.2": 47, "1.4.4": 49, "1.4.5": 49,
    "1.4.6": 51, "1.4.7": 51, "1.5": 60, "1.5.1": 60, "1.5.2": 61,
    "1.6.1": 73, "1.6.2": 74, "1.6.4": 78,
}

_PLAYER_NAME = re.compile(r"^[A-Za-z0-9_]{1,16}\Z")
_VERSION_PARTS = re.compile(r"^(\d+)\.(\d+)")


def _is_pre_netty(version: str) -> bool:
    match = _VERSION_PARTS.match(version or "")
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) < (1, 7)


def modern_protocol_for(version: str, client_protocol: int | None) -> int:
    if version in MODERN_PROTOCOLS:
        return MODERN_PROTOCOLS[version]
    if _is_pre_netty(version):
        return 0  # a modern client can never join a pre-1.7 server
    return client_protocol if client_protocol is not None else 0


def legacy_protocol_for(version: str) -> int:
    if version in LEGACY_PROTOCOLS:
        return LEGACY_PROTOCOLS[version]
    return 127  # not a legacy server: the client shows the version as incompatible


def default_info() -> dict[str, Any]:
    return {
        "mode": MODE_SLEEPING,
        "motd": "A Minecraft Server",
        "max_players": 20,
        "version_name": "",
        "allowed": None,  # set of lowercase names when the whitelist is on
    }


class _Closed(Exception):
    pass


class _Conn:
    """Buffered reads with an absolute deadline and bounded sizes."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self.sock = sock
        self.deadline = deadline
        self.buf = bytearray()
        self.eof = False
        self.received = 0

    def _recv_some(self, until: float) -> bool:
        remaining = min(until, self.deadline) - time.monotonic()
        if remaining <= 0 or self.eof:
            return False
        self.sock.settimeout(max(0.01, remaining))
        try:
            chunk = self.sock.recv(4096)
        except (socket.timeout, BlockingIOError):
            return False
        except OSError:
            self.eof = True
            return False
        if not chunk:
            self.eof = True
            return False
        self.buf.extend(chunk)
        self.received += len(chunk)
        return True

    def peek(self, count: int, wait: float) -> bytes:
        """Up to `count` bytes, waiting at most `wait` seconds. Never raises."""
        until = time.monotonic() + wait
        while len(self.buf) < count and time.monotonic() < until and not self.eof:
            if not self._recv_some(until):
                if self.eof:
                    break
        return bytes(self.buf[:count])

    def take(self, count: int) -> bytes:
        until = self.deadline
        while len(self.buf) < count:
            if not self._recv_some(until):
                raise _Closed("connection ended or timed out")
        data = bytes(self.buf[:count])
        del self.buf[:count]
        return data

    def available(self, wait: float, limit: int = 1024) -> bytes:
        """Whatever arrives within `wait` seconds (bounded), consumed."""
        until = time.monotonic() + wait
        while len(self.buf) < limit and time.monotonic() < until and not self.eof:
            if not self._recv_some(until):
                break
        data = bytes(self.buf[:limit])
        del self.buf[:limit]
        return data

    def read_varint(self) -> int:
        value = 0
        for position in range(5):
            byte = self.take(1)[0]
            value |= (byte & 0x7F) << (7 * position)
            if not byte & 0x80:
                return value
        raise ValueError("VarInt is too large")

    def read_packet(self, max_length: int) -> bytes:
        length = self.read_varint()
        if length <= 0 or length > max_length:
            raise ValueError(f"packet length {length} out of range")
        return self.take(length)


class SleepProxy:
    # Handlers are tiny threads; idle sockets give up their slot after
    # FIRST_BYTE_TIMEOUT. There is no per-IP cap: on Railway every player
    # arrives from the TCP proxy's address.
    MAX_HANDLERS = 128
    CONNECTION_DEADLINE = 6.0
    FIRST_BYTE_TIMEOUT = 2.0
    # A real client sends its whole handshake (and the status or login
    # packet after it) in one go, so once the first byte is in, the rest of
    # the exchange gets this long: a trickling socket can't hold a slot for
    # the full CONNECTION_DEADLINE.
    AFTER_FIRST_BYTE = 2.0
    MAX_HANDSHAKE = 4096
    MAX_LOGIN_START = 1024

    def __init__(
        self,
        port: int,
        on_wake: Callable[..., Any],
        info: Callable[[], dict[str, Any]] | None = None,
        host: str = "0.0.0.0",
        log: Callable[..., Any] | None = None,
    ) -> None:
        self.port = port
        self.host = host
        self.on_wake = on_wake
        self.info_provider = info or default_info
        self._log_fn = log
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._slots = threading.BoundedSemaphore(self.MAX_HANDLERS)
        self.bound_port: int | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._running

    def _log(self, text: str, level: str = "info") -> None:
        if self._log_fn is None:
            from .state import state

            state.add_log(text, level)
        else:
            self._log_fn(text, level)

    def start(self, attempts: int = 10, retry_delay: float = 1.0) -> bool:
        """Bind the port (idempotent). Returns True when listening."""
        with self._lock:
            if self._running:
                return True
            sock: socket.socket | None = None
            error: OSError | None = None
            for attempt in range(attempts):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    if os.name != "nt":
                        # On Windows SO_REUSEADDR allows double binds.
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((self.host, self.port))
                    sock.listen(16)
                    sock.settimeout(0.5)
                    break
                except OSError as exc:
                    error = exc
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                        sock = None
                    if attempt < attempts - 1:
                        time.sleep(retry_delay)
            if sock is None:
                self._log(
                    f"Sleep proxy could not listen on port {self.port}: {error}", "error"
                )
                return False
            self._socket = sock
            self.bound_port = sock.getsockname()[1]
            self._running = True
            self._thread = threading.Thread(
                target=self._accept_loop, args=(sock,), name="sleep-proxy", daemon=True
            )
            self._thread.start()
        self._log(f"Sleep proxy listening on port {self.bound_port}; a join wakes the server")
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            sock, self._socket = self._socket, None
            thread, self._thread = self._thread, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _accept_loop(self, srv: socket.socket) -> None:
        while self._running and self._socket is srv:
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                try:
                    client.close()
                except OSError:
                    pass
                continue
            threading.Thread(
                target=self._run_handler, args=(client, addr), daemon=True
            ).start()

    def _run_handler(self, client: socket.socket, addr: Any) -> None:
        try:
            self._handle_client(client, addr)
        finally:
            self._slots.release()

    # -- connection handling ----------------------------------------------
    def _info(self) -> dict[str, Any]:
        info = default_info()
        try:
            provided = self.info_provider() or {}
            info.update(provided)
        except Exception:  # never let a status lookup break the proxy
            pass
        return info

    def _handle_client(self, client: socket.socket, addr: Any = None) -> None:
        wake_name: str | None = None
        wake = False
        conn = _Conn(client, time.monotonic() + self.CONNECTION_DEADLINE)
        try:
            # Real clients send their handshake at once; a silent socket only
            # holds a slot, so it gets a short first-byte window.
            first = conn.peek(1, self.FIRST_BYTE_TIMEOUT)
            if not first:
                return
            conn.deadline = min(conn.deadline, time.monotonic() + self.AFTER_FIRST_BYTE)
            if first[0] == 0xFE:
                head = conn.peek(3, 0.3)
                if len(head) >= 3 and head[1] == 0x01 and head[2] == 0x00:
                    wake, wake_name = self._handle_modern(conn)
                else:
                    self._handle_legacy_ping(conn)
            elif first[0] == 0x02:
                wake, wake_name = self._handle_legacy_login(conn)
            else:
                wake, wake_name = self._handle_modern(conn)
        except Exception:
            wake = False
        finally:
            # Nothing to drain from a client that sent nothing or already left.
            self._graceful_close(client, drain=conn.received > 0 and not conn.eof)
        if wake:
            try:
                self.on_wake(wake_name)
            except Exception as exc:
                self._log(f"Could not wake the server: {exc}", "error")

    @staticmethod
    def _graceful_close(client: socket.socket, drain: bool = True) -> None:
        """Half-close, then drain so unread client bytes never trigger a RST."""
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        until = time.monotonic() + (1.0 if drain else 0.0)
        try:
            while time.monotonic() < until:
                client.settimeout(max(0.01, until - time.monotonic()))
                if not client.recv(4096):
                    break
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass

    def _login_decision(self, info: dict[str, Any], name: str | None) -> tuple[str, bool]:
        """(kick message, should wake) for a login attempt."""
        mode = info.get("mode") or MODE_SLEEPING
        if mode != MODE_SLEEPING:
            return MESSAGES.get(mode, MESSAGES[MODE_UPDATING]), False
        allowed = info.get("allowed")
        if allowed is not None:
            if not name or name.lower() not in allowed:
                return MESSAGES["not_whitelisted"], False
        return MESSAGES[MODE_SLEEPING], True

    def _log_wake(self, name: str | None) -> None:
        who = name if name and _PLAYER_NAME.match(name) else "a player"
        self._log(f"Join attempt by {who} while sleeping; waking the server")

    def _handle_modern(self, conn: _Conn) -> tuple[bool, str | None]:
        data = conn.read_packet(self.MAX_HANDSHAKE)
        packet_id, offset = self._read_varint_from(data, 0)
        if packet_id != 0x00:
            return False, None
        protocol, offset = self._read_varint_from(data, offset)
        host_len, offset = self._read_varint_from(data, offset)
        offset += host_len + 2  # server address + unsigned short port
        next_state, _ = self._read_varint_from(data, offset)
        if next_state == 1:
            self._serve_status(conn, protocol)
            return False, None
        if next_state not in (2, 3):
            return False, None
        name = None
        try:
            login = conn.read_packet(self.MAX_LOGIN_START)
            login_id, login_offset = self._read_varint_from(login, 0)
            if login_id == 0x00:
                name_len, login_offset = self._read_varint_from(login, login_offset)
                if 0 < name_len <= 64:
                    name = login[login_offset : login_offset + name_len].decode(
                        "utf-8", errors="replace"
                    )
        except Exception:
            name = None
        message, wake = self._login_decision(self._info(), name)
        self._send_login_disconnect(conn.sock, message)
        if wake:
            self._log_wake(name)
        return wake, name

    def _serve_status(self, conn: _Conn, client_protocol: int) -> None:
        """Status Request -> Response, then Ping -> Pong (in that order)."""
        sent = False
        for _ in range(4):
            try:
                packet = conn.read_packet(64)
            except Exception:
                return
            packet_id, offset = self._read_varint_from(packet, 0)
            if packet_id == 0x00 and not sent:
                self._send_status_response(conn.sock, client_protocol)
                sent = True
            elif packet_id == 0x01:
                self._send_packet(conn.sock, 0x01, packet[offset:offset + 8])
                return
            else:
                return

    def _status_payload(self, client_protocol: int | None) -> dict[str, Any]:
        info = self._info()
        mode = info.get("mode") or MODE_SLEEPING
        version = str(info.get("version_name") or "")
        motd = str(info.get("motd") or "A Minecraft Server")
        return {
            "version": {
                "name": version or "Railpowered",
                "protocol": modern_protocol_for(version, client_protocol),
            },
            "players": {
                "max": int(info.get("max_players") or 20),
                "online": 0,
                "sample": [],
            },
            "description": {
                "text": motd,
                "extra": [
                    {"text": "\n" + MOTD_SUFFIX.get(mode, MOTD_SUFFIX[MODE_SLEEPING]),
                     "color": "gray"}
                ],
            },
            "enforcesSecureChat": False,
            "previewsChat": False,
        }

    def _send_status_response(self, sock: socket.socket, client_protocol: int | None) -> None:
        payload = json.dumps(self._status_payload(client_protocol), separators=(",", ":"))
        self._send_packet(sock, 0x00, self._pack_string(payload))

    def _handle_legacy_ping(self, conn: _Conn) -> None:
        data = conn.available(0.3, limit=512)
        info = self._info()
        mode = info.get("mode") or MODE_SLEEPING
        version = str(info.get("version_name") or "")
        motd = str(info.get("motd") or "A Minecraft Server")
        max_players = int(info.get("max_players") or 20)
        if len(data) >= 2 and data[1] == 0x01:
            # 1.4-1.6 clients: "§1\0protocol\0version\0motd\0online\0max".
            label = f"{motd} ({mode})"
            reply = "§1\x00{}\x00{}\x00{}\x000\x00{}".format(
                legacy_protocol_for(version), version or "Railpowered", label, max_players
            )
        else:
            # Beta 1.8 - 1.3 clients: "motd§online§max" (no § allowed in motd).
            plain = re.sub("§.?", "", motd)
            reply = f"{plain} ({mode})§0§{max_players}"
        self._send_legacy_kick(conn.sock, reply)

    def _handle_legacy_login(self, conn: _Conn) -> tuple[bool, str | None]:
        data = conn.available(0.5, limit=512)
        name = self._legacy_username(data)
        message, wake = self._login_decision(self._info(), name)
        self._send_legacy_kick(conn.sock, message)
        if wake:
            self._log_wake(name)
        return wake, name

    @staticmethod
    def _legacy_username(data: bytes) -> str | None:
        """Parse the 0x02 handshake of 1.2.5 ("user;host:port") or 1.3-1.6."""
        try:
            if len(data) < 4 or data[0] != 0x02:
                return None
            offset = 1
            if data[1] != 0x00:
                offset = 2  # 1.3+: one protocol byte before the username
            (length,) = struct.unpack(">h", data[offset : offset + 2])
            if length <= 0 or length > 64:
                return None
            raw = data[offset + 2 : offset + 2 + length * 2].decode("utf-16-be")
            return raw.split(";", 1)[0] or None
        except (struct.error, UnicodeDecodeError):
            return None

    # -- packet helpers (static so tests can build packets) ---------------
    @classmethod
    def _send_legacy_kick(cls, sock: socket.socket, message: str) -> None:
        sock.sendall(b"\xff" + cls._pack_legacy_string(message))

    @staticmethod
    def _pack_legacy_string(value: str) -> bytes:
        encoded = value.encode("utf-16-be")
        return struct.pack(">h", len(encoded) // 2) + encoded

    @classmethod
    def _send_login_disconnect(cls, sock: socket.socket, message: str) -> None:
        component = json.dumps({"text": message}, separators=(",", ":"))
        cls._send_packet(sock, 0x00, cls._pack_string(component))

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
        value &= 0xFFFFFFFF
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
