"""A small Minecraft Java Edition client for the end-to-end checks.

It speaks only what a launcher says before anyone plays:

- ``status``: the 1.7+ server-list ping (handshake with next_state=1, status
  request, then ping/pong), which every modern server and the sleep proxy
  answer;
- ``legacy_ping``: the 1.6 server-list ping (``FE 01 FA ...``);
- ``login``: a handshake with next_state=2 plus Login Start, returning what
  the server answered first (a disconnect with its reason, an encryption
  request from an online-mode server, compression or login success).

Standard library only, so it runs on a bare CI runner and inside the image.
It is also a command-line tool (used by .github/scripts/smoke_test.sh):

    python3 tests/e2e/mcproto.py status HOST PORT [--wait SECONDS]
    python3 tests/e2e/mcproto.py legacy HOST PORT [--wait SECONDS]
    python3 tests/e2e/mcproto.py login HOST PORT NAME [--wait SECONDS]

``status`` prints the server's JSON; ``legacy`` and ``login`` print one JSON
object. The exit code is 0 on an answer and 1 when the server never gave one.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# 1.21.1. Servers answer a status ping whatever the number; the sleep proxy
# echoes it back for versions it has no table entry for.
DEFAULT_PROTOCOL = 767
MAX_PACKET = 2 * 1024 * 1024


class ProtocolError(Exception):
    """The server answered, but not with what the protocol allows."""


# -- encoding ---------------------------------------------------------------
def pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            return bytes(out)


def unpack_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """(value, new offset). Values are signed 32-bit, as on the wire."""
    value = 0
    for position in range(5):
        if offset >= len(data):
            raise ProtocolError("a VarInt was cut off")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (7 * position)
        if not byte & 0x80:
            return (value - (1 << 32) if value & (1 << 31) else value), offset
    raise ProtocolError("a VarInt was longer than 5 bytes")


def pack_string(text: str) -> bytes:
    data = text.encode("utf-8")
    return pack_varint(len(data)) + data


def unpack_string(data: bytes, offset: int = 0) -> tuple[str, int]:
    length, offset = unpack_varint(data, offset)
    if length < 0 or offset + length > len(data):
        raise ProtocolError("a string ran past the end of its packet")
    return data[offset : offset + length].decode("utf-8", errors="replace"), offset + length


def packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = pack_varint(packet_id) + payload
    return pack_varint(len(body)) + body


def handshake(host: str, port: int, next_state: int, protocol: int = DEFAULT_PROTOCOL) -> bytes:
    return packet(0x00, pack_varint(protocol) + pack_string(host) + struct.pack(">H", port) + pack_varint(next_state))


def login_start(name: str) -> bytes:
    # Name, then the player UUID that 1.20.2+ expects (any value will do
    # before authentication); older servers stop reading after the name.
    return packet(0x00, pack_string(name) + bytes(16))


def legacy_ping_request(host: str, port: int, protocol: int = 78) -> bytes:
    """The 1.6 client's ping: FE 01 FA "MC|PingHost" <data>."""
    channel = "MC|PingHost".encode("utf-16-be")
    host_bytes = host.encode("utf-16-be")
    data = bytes([protocol]) + struct.pack(">h", len(host_bytes) // 2) + host_bytes + struct.pack(">i", port)
    return (
        b"\xfe\x01\xfa"
        + struct.pack(">h", len(channel) // 2)
        + channel
        + struct.pack(">h", len(data))
        + data
    )


def text_of(component: Any) -> str:
    """Plain text of a chat component (a string, or {"text", "extra", ...})."""
    if component is None:
        return ""
    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(text_of(item) for item in component)
    if isinstance(component, dict):
        own = component.get("text")
        if own is None:
            own = component.get("translate", "")
        return str(own) + "".join(text_of(item) for item in component.get("extra") or [])
    return str(component)


# -- the connection -------------------------------------------------------------
class Connection:
    """One TCP connection with an overall deadline."""

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.deadline = time.monotonic() + timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the server stopped answering")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("the server closed the connection before answering")
            data.extend(chunk)
        return bytes(data)

    def read_varint(self) -> int:
        value = 0
        for position in range(5):
            byte = self.recv_exact(1)[0]
            value |= (byte & 0x7F) << (7 * position)
            if not byte & 0x80:
                return value
        raise ProtocolError("a VarInt was longer than 5 bytes")

    def read_packet(self) -> tuple[int, bytes]:
        length = self.read_varint()
        if length <= 0 or length > MAX_PACKET:
            raise ProtocolError(f"packet length {length} is out of range")
        body = self.recv_exact(length)
        packet_id, offset = unpack_varint(body)
        return packet_id, body[offset:]


# -- the three exchanges ------------------------------------------------------------
@dataclass
class Status:
    data: dict[str, Any]
    latency_ms: float

    @property
    def version_name(self) -> str:
        return str((self.data.get("version") or {}).get("name", ""))

    @property
    def protocol(self) -> int | None:
        value = (self.data.get("version") or {}).get("protocol")
        return value if isinstance(value, int) else None

    @property
    def description(self) -> str:
        return text_of(self.data.get("description"))

    @property
    def max_players(self) -> int | None:
        value = (self.data.get("players") or {}).get("max")
        return value if isinstance(value, int) else None


def status(host: str, port: int, timeout: float = 10.0, protocol: int = DEFAULT_PROTOCOL) -> Status:
    """Server-list ping. Raises OSError when nobody listens, ProtocolError on nonsense."""
    with Connection(host, port, timeout) as conn:
        conn.send(handshake(host, port, 1, protocol) + packet(0x00))
        packet_id, payload = conn.read_packet()
        if packet_id != 0x00:
            raise ProtocolError(f"expected a status response (0x00), got 0x{packet_id:02x}")
        text, _ = unpack_string(payload)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProtocolError(f"the status response is not JSON: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise ProtocolError("the status response is not a JSON object")
        token = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
        sent = time.perf_counter()
        conn.send(packet(0x01, struct.pack(">q", token)))
        packet_id, payload = conn.read_packet()
        latency = (time.perf_counter() - sent) * 1000
        if packet_id != 0x01 or payload[:8] != struct.pack(">q", token):
            raise ProtocolError("the pong did not echo the ping")
        return Status(data, round(latency, 1))


@dataclass
class LegacyStatus:
    protocol: int
    version: str
    motd: str
    online: int
    max_players: int


def legacy_ping(host: str, port: int, timeout: float = 10.0) -> LegacyStatus:
    with Connection(host, port, timeout) as conn:
        conn.send(legacy_ping_request(host, port))
        if conn.recv_exact(1) != b"\xff":
            raise ProtocolError("the legacy ping reply does not start with 0xFF")
        (length,) = struct.unpack(">h", conn.recv_exact(2))
        if length <= 0:
            raise ProtocolError("the legacy ping reply is empty")
        text = conn.recv_exact(length * 2).decode("utf-16-be", errors="replace")
    fields = text.split("\x00")
    if len(fields) != 6 or fields[0] != "§1":
        raise ProtocolError(f"unexpected legacy ping reply: {text!r}")
    try:
        return LegacyStatus(int(fields[1]), fields[2], fields[3], int(fields[4]), int(fields[5]))
    except ValueError as exc:
        raise ProtocolError(f"unexpected legacy ping reply: {text!r}") from exc


@dataclass
class LoginReply:
    """What the server said first after Login Start.

    kind: "disconnect" (reason holds the kick text), "encryption" (an
    online-mode server asking the client to authenticate), "compression"
    or "success" (an offline-mode server letting us in)."""

    kind: str
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


_LOGIN_KINDS = {0x01: "encryption", 0x02: "success", 0x03: "compression"}


def login(host: str, port: int, name: str, timeout: float = 10.0, protocol: int = DEFAULT_PROTOCOL) -> LoginReply:
    with Connection(host, port, timeout) as conn:
        conn.send(handshake(host, port, 2, protocol) + login_start(name))
        packet_id, payload = conn.read_packet()
    if packet_id == 0x00:
        text, _ = unpack_string(payload)
        try:
            component = json.loads(text)
        except ValueError:
            component = text
        raw = component if isinstance(component, dict) else {"text": component}
        return LoginReply("disconnect", text_of(component), raw)
    if packet_id in _LOGIN_KINDS:
        return LoginReply(_LOGIN_KINDS[packet_id])
    raise ProtocolError(f"unexpected login packet 0x{packet_id:02x}")


def retry(action: Any, wait: float, interval: float = 1.0) -> Any:
    """Call ``action()`` until it stops raising OSError (nobody listening yet) or ``wait`` runs out."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return action()
        except OSError:
            if time.monotonic() + interval > deadline:
                raise
            time.sleep(interval)


# -- command line -------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ping or log in to a Minecraft Java server.")
    parser.add_argument("command", choices=("status", "legacy", "login"))
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("name", nargs="?", default="RailpoweredCI", help="player name for login")
    parser.add_argument("--wait", type=float, default=0.0, help="keep retrying a refused connection this long (seconds)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-connection timeout (seconds)")
    args = parser.parse_args(argv)

    def run() -> Any:
        if args.command == "status":
            return status(args.host, args.port, args.timeout)
        if args.command == "legacy":
            return legacy_ping(args.host, args.port, args.timeout)
        return login(args.host, args.port, args.name, args.timeout)

    try:
        result = retry(run, args.wait)
    except (OSError, ProtocolError) as exc:
        print(f"{args.command} {args.host}:{args.port} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if isinstance(result, Status):
        print(json.dumps(result.data, ensure_ascii=False))
    elif isinstance(result, LegacyStatus):
        print(json.dumps(result.__dict__, ensure_ascii=False))
    else:
        print(json.dumps({"kind": result.kind, "reason": result.reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        # MOTDs carry section signs and arbitrary Unicode.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.exit(main())
