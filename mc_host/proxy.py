"""Dual-protocol sleep proxy for modern (1.7+) and legacy (1.2.5–1.6) clients."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Callable

import installer

from .state import state


class SleepProxy:
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

            first = client.recv(1, socket.MSG_PEEK)
            if first and first[0] in (0xFE, 0x02):
                wake_server = self._handle_legacy(client, first[0], msg)
            else:
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

    def _handle_legacy(self, sock: socket.socket, first_byte: int, message: str) -> bool:
        if first_byte == 0xFE:
            try:
                sock.recv(3)
            except OSError:
                pass
            self._send_legacy_kick(sock, self._legacy_motd())
            return False
        try:
            sock.recv(1)
        except OSError:
            pass
        self._send_legacy_kick(sock, message)
        return True

    @staticmethod
    def _legacy_motd() -> str:
        cfg = installer.load_config()
        version = cfg.get("minecraft_version") or "1.2.5"
        return f"{version} sleeping - join to wake§0§20"

    @classmethod
    def _send_legacy_kick(cls, sock: socket.socket, message: str) -> None:
        sock.sendall(b"\xff" + cls._pack_legacy_string(message))

    @staticmethod
    def _pack_legacy_string(value: str) -> bytes:
        encoded = value.encode("utf-16-be")
        return struct.pack(">h", len(value)) + encoded

    @classmethod
    def _read_handshake(cls, sock: socket.socket) -> tuple[int | None, int | None]:
        data = cls._read_packet(sock)
        packet_id, offset = cls._read_varint_from(data, 0)
        if packet_id != 0:
            return None, None
        protocol_version, offset = cls._read_varint_from(data, offset)
        address_len, offset = cls._read_varint_from(data, offset)
        offset += address_len
        offset += 2
        next_state, _ = cls._read_varint_from(data, offset)
        return protocol_version, next_state

    @classmethod
    def _send_status_response(
        cls, sock: socket.socket, protocol_version: int | None
    ) -> None:
        cfg = installer.load_config()
        version = str(cfg.get("minecraft_version") or "1.2.5")
        status = {
            "version": {
                "name": version,
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
