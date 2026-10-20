"""Sleep proxy wire protocol, tested as a real client over loopback sockets."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import unittest

import runtime_support  # noqa: F401  (sys.path)

from mc_host.proxy import MESSAGES, SleepProxy


def varint(value: int) -> bytes:
    return SleepProxy._pack_varint(value)


def mc_string(text: str) -> bytes:
    data = text.encode("utf-8")
    return varint(len(data)) + data


def packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = varint(packet_id) + payload
    return varint(len(body)) + body


def handshake(next_state: int, protocol: int = 763, host: str = "play.example.net") -> bytes:
    return packet(0x00, varint(protocol) + mc_string(host) + struct.pack(">H", 25565) + varint(next_state))


def login_start(name: str) -> bytes:
    return packet(0x00, mc_string(name) + b"\x00" * 16)


def legacy_string(text: str) -> bytes:
    encoded = text.encode("utf-16-be")
    return struct.pack(">h", len(encoded) // 2) + encoded


class Client:
    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise EOFError("closed early")
            data += chunk
        return data

    def read_varint(self) -> int:
        value = 0
        for position in range(5):
            byte = self.recv_exact(1)[0]
            value |= (byte & 0x7F) << (7 * position)
            if not byte & 0x80:
                return value
        raise ValueError("varint too long")

    def read_packet(self) -> tuple[int, bytes]:
        length = self.read_varint()
        body = self.recv_exact(length)
        packet_id, offset = SleepProxy._read_varint_from(body, 0)
        return packet_id, body[offset:]

    def read_string_packet(self) -> tuple[int, str]:
        packet_id, payload = self.read_packet()
        length, offset = SleepProxy._read_varint_from(payload, 0)
        return packet_id, payload[offset : offset + length].decode("utf-8")

    def read_legacy_kick(self) -> str:
        self.assert_byte(0xFF)
        (length,) = struct.unpack(">h", self.recv_exact(2))
        return self.recv_exact(length * 2).decode("utf-16-be")

    def assert_byte(self, expected: int) -> None:
        got = self.recv_exact(1)[0]
        if got != expected:
            raise AssertionError(f"expected 0x{expected:02x}, got 0x{got:02x}")

    def read_until_eof(self) -> bytes:
        """Reads to EOF; raises ConnectionResetError if the server sent a RST."""
        data = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                return data
            data += chunk

    def close(self) -> None:
        self.sock.close()


class ProxyTestCase(unittest.TestCase):
    info: dict = {}

    def setUp(self) -> None:
        self.wakes: list = []
        self.woke = threading.Event()
        self.info = {
            "mode": "sleeping",
            "motd": "Friends SMP",
            "max_players": 12,
            "version_name": "1.20.1",
            "allowed": None,
        }
        self.logs: list = []

        def wake(name=None):
            self.wakes.append(name)
            self.woke.set()

        self.proxy = SleepProxy(
            0,
            wake,
            info=lambda: dict(self.info),
            host="127.0.0.1",
            log=lambda text, level="info": self.logs.append(text),
        )
        self.assertTrue(self.proxy.start())
        self.addCleanup(self.proxy.stop)
        self.port = self.proxy.bound_port

    def client(self) -> Client:
        client = Client(self.port)
        self.addCleanup(client.close)
        return client

    def assert_no_wake(self) -> None:
        time.sleep(0.15)
        self.assertEqual(self.wakes, [])


class ModernStatusTests(ProxyTestCase):
    def test_status_then_ping_gets_pong_and_does_not_wake(self):
        client = self.client()
        client.send(handshake(1) + packet(0x00))
        packet_id, text = client.read_string_packet()
        self.assertEqual(packet_id, 0x00)
        status = json.loads(text)
        self.assertEqual(status["version"], {"name": "1.20.1", "protocol": 763})
        self.assertEqual(status["players"]["max"], 12)
        self.assertEqual(status["players"]["online"], 0)
        self.assertEqual(status["description"]["text"], "Friends SMP")
        self.assertIn("join to wake", status["description"]["extra"][0]["text"])
        payload = struct.pack(">q", 1234567890123)
        client.send(packet(0x01, payload))
        pong_id, pong = client.read_packet()
        self.assertEqual(pong_id, 0x01)
        self.assertEqual(pong, payload)
        self.assertEqual(client.read_until_eof(), b"")
        self.assert_no_wake()
        self.assertEqual(self.logs[1:], [], "status pings must not flood the log")

    def test_unknown_version_echoes_client_protocol(self):
        self.info["version_name"] = "26.3"
        client = self.client()
        client.send(handshake(1, protocol=999) + packet(0x00))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["version"], {"name": "26.3", "protocol": 999})

    def test_legacy_setup_reports_incompatible_protocol(self):
        self.info["version_name"] = "1.6.4"
        client = self.client()
        client.send(handshake(1, protocol=763) + packet(0x00))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["version"], {"name": "1.6.4", "protocol": 0})

    def test_status_while_updating_says_so(self):
        self.info["mode"] = "updating"
        client = self.client()
        client.send(handshake(1) + packet(0x00))
        _id, text = client.read_string_packet()
        self.assertIn("Updating", json.loads(text)["description"]["extra"][0]["text"])

    def test_multibyte_lengths_and_254_byte_handshake_are_modern(self):
        # A handshake of exactly 254 bytes starts with FE 01 00, like a
        # legacy ping prefix; it must still be parsed as a modern handshake.
        raw = next(
            candidate
            for candidate in (handshake(1, host="h" * n) for n in range(200, 260))
            if candidate[:3] == b"\xfe\x01\x00"
        )
        client = self.client()
        client.send(raw + packet(0x00))
        packet_id, text = client.read_string_packet()
        self.assertEqual(packet_id, 0x00)
        self.assertEqual(json.loads(text)["version"]["name"], "1.20.1")


class ModernLoginTests(ProxyTestCase):
    def test_login_receives_kick_message_intact_and_wakes(self):
        client = self.client()
        # Handshake + Login Start + a stray extra packet: the proxy must drain
        # all of it so closing does not reset the connection.
        client.send(handshake(2) + login_start("Steve") + packet(0x02, b"x" * 40))
        packet_id, text = client.read_string_packet()
        self.assertEqual(packet_id, 0x00)
        self.assertEqual(json.loads(text), {"text": MESSAGES["sleeping"]})
        self.assertEqual(client.read_until_eof(), b"")
        self.assertTrue(self.woke.wait(3))
        self.assertEqual(self.wakes, ["Steve"])

    def test_late_client_bytes_do_not_reset_the_connection(self):
        # Bytes that arrive after the kick was sent must be drained, not
        # answered with a TCP reset that discards the kick on the client.
        client = self.client()
        client.send(handshake(2) + login_start("Steve"))
        time.sleep(0.2)
        client.send(packet(0x02, b"late" * 10))
        time.sleep(0.2)
        packet_id, text = client.read_string_packet()
        self.assertEqual(json.loads(text), {"text": MESSAGES["sleeping"]})
        self.assertEqual(client.read_until_eof(), b"")

    def test_login_while_updating_does_not_wake(self):
        self.info["mode"] = "updating"
        client = self.client()
        client.send(handshake(2) + login_start("Steve"))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["text"], "Server is updating, try again in a minute.")
        client.read_until_eof()
        self.assert_no_wake()

    def test_login_while_starting_does_not_wake_again(self):
        self.info["mode"] = "starting"
        client = self.client()
        client.send(handshake(2) + login_start("Steve"))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["text"], MESSAGES["starting"])
        self.assert_no_wake()

    def test_whitelist_gates_wake(self):
        self.info["allowed"] = {"alex"}
        client = self.client()
        client.send(handshake(2) + login_start("Steve"))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["text"], MESSAGES["not_whitelisted"])
        self.assert_no_wake()
        other = self.client()
        other.send(handshake(2) + login_start("Alex"))
        _id, text = other.read_string_packet()
        self.assertEqual(json.loads(text)["text"], MESSAGES["sleeping"])
        self.assertTrue(self.woke.wait(3))
        self.assertEqual(self.wakes, ["Alex"])

    def test_login_after_a_failed_start_asks_for_the_owner(self):
        self.info["mode"] = "crashed"
        client = self.client()
        client.send(handshake(2) + login_start("Steve"))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["text"], MESSAGES["crashed"])
        self.assert_no_wake()

    def test_transfer_intent_counts_as_login(self):
        client = self.client()
        client.send(handshake(3) + login_start("Steve"))
        client.read_string_packet()
        self.assertTrue(self.woke.wait(3))


class LegacyTests(ProxyTestCase):
    def test_pre_1_4_ping_uses_old_format(self):
        client = self.client()
        client.send(b"\xfe")
        reply = client.read_legacy_kick()
        self.assertEqual(reply, "Friends SMP (sleeping)§0§12")
        self.assert_no_wake()

    def test_1_4_ping_uses_section_one_format(self):
        self.info["version_name"] = "1.4.7"
        client = self.client()
        client.send(b"\xfe\x01")
        fields = client.read_legacy_kick().split("\x00")
        self.assertEqual(fields, ["§1", "51", "1.4.7", "Friends SMP (sleeping)", "0", "12"])

    def test_1_6_ping_with_pinghost_payload(self):
        self.info["version_name"] = "1.6.4"
        client = self.client()
        host = legacy_string("localhost")
        data = struct.pack(">b", 78) + host + struct.pack(">i", 25565)
        client.send(b"\xfe\x01\xfa" + legacy_string("MC|PingHost") + struct.pack(">h", len(data)) + data)
        fields = client.read_legacy_kick().split("\x00")
        self.assertEqual(fields[1:3], ["78", "1.6.4"])
        self.assertEqual(client.read_until_eof(), b"")
        self.assert_no_wake()

    def test_modern_setup_shows_as_incompatible_to_legacy_clients(self):
        client = self.client()
        client.send(b"\xfe\x01")
        fields = client.read_legacy_kick().split("\x00")
        self.assertEqual(fields[1:3], ["127", "1.20.1"])

    def test_1_6_login_kicks_and_wakes(self):
        client = self.client()
        client.send(
            b"\x02" + bytes([78]) + legacy_string("Steve") + legacy_string("localhost")
            + struct.pack(">i", 25565)
        )
        self.assertEqual(client.read_legacy_kick(), MESSAGES["sleeping"])
        self.assertEqual(client.read_until_eof(), b"")
        self.assertTrue(self.woke.wait(3))
        self.assertEqual(self.wakes, ["Steve"])

    def test_1_2_5_login_parses_name(self):
        client = self.client()
        client.send(b"\x02" + legacy_string("Steve;localhost:25565"))
        self.assertEqual(client.read_legacy_kick(), MESSAGES["sleeping"])
        self.assertTrue(self.woke.wait(3))
        self.assertEqual(self.wakes, ["Steve"])

    def test_legacy_login_while_updating(self):
        self.info["mode"] = "updating"
        client = self.client()
        client.send(b"\x02" + legacy_string("Steve;localhost:25565"))
        self.assertEqual(client.read_legacy_kick(), MESSAGES["updating"])
        self.assert_no_wake()


class RobustnessTests(ProxyTestCase):
    def test_garbage_is_dropped_without_waking(self):
        client = self.client()
        client.send(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        client.sock.shutdown(socket.SHUT_WR)
        client.read_until_eof()
        self.assert_no_wake()
        # Still serving afterwards.
        again = self.client()
        again.send(handshake(1) + packet(0x00))
        self.assertEqual(again.read_string_packet()[0], 0x00)

    def test_oversized_length_is_rejected(self):
        client = self.client()
        client.send(b"\xff\xff\xff\xff\x0f")
        client.read_until_eof()
        self.assert_no_wake()

    def test_varint_longer_than_five_bytes_is_rejected(self):
        client = self.client()
        client.send(b"\x80\x80\x80\x80\x80\x01")
        client.read_until_eof()
        self.assert_no_wake()

    def test_non_handshake_packet_is_ignored(self):
        client = self.client()
        client.send(packet(0x05, b"abc"))
        client.read_until_eof()
        self.assert_no_wake()

    def test_empty_connection_does_not_wake(self):
        client = self.client()
        client.sock.shutdown(socket.SHUT_WR)
        client.read_until_eof()
        self.assert_no_wake()

    def test_truncated_handshake_does_not_wake(self):
        client = self.client()
        raw = handshake(2)
        client.send(raw[:-3])
        client.sock.shutdown(socket.SHUT_WR)
        client.read_until_eof()
        self.assert_no_wake()

    def test_handler_cap_drops_excess_connections(self):
        self.proxy.stop()
        capped = SleepProxy(0, lambda name=None: None, host="127.0.0.1", log=lambda *a: None)
        capped.MAX_HANDLERS = 1
        capped._slots = threading.BoundedSemaphore(1)
        self.assertTrue(capped.start())
        self.addCleanup(capped.stop)
        idle = Client(capped.bound_port)  # holds the only slot (sends nothing)
        self.addCleanup(idle.close)
        time.sleep(0.2)
        extra = Client(capped.bound_port)
        self.addCleanup(extra.close)
        started = time.monotonic()
        try:
            self.assertEqual(extra.read_until_eof(), b"")
        except ConnectionResetError:
            pass
        self.assertLess(time.monotonic() - started, 2.0)

    def test_silent_connections_give_their_slot_back_quickly(self):
        self.assertGreaterEqual(SleepProxy.MAX_HANDLERS, 128)
        idle = self.client()  # connects and never sends a byte
        started = time.monotonic()
        try:
            self.assertEqual(idle.read_until_eof(), b"")
        except ConnectionResetError:
            pass
        # The first-byte window (2 s), and no 1 s drain for a silent socket.
        self.assertLess(time.monotonic() - started, SleepProxy.FIRST_BYTE_TIMEOUT + 0.8)
        self.assert_no_wake()

    def test_stop_releases_port_and_start_is_idempotent(self):
        self.assertTrue(self.proxy.start())  # already running: no second bind
        port = self.port
        self.proxy.stop()
        self.assertFalse(self.proxy.active)
        rebound = socket.socket()
        self.addCleanup(rebound.close)
        rebound.bind(("127.0.0.1", port))

    def test_status_provider_errors_fall_back_to_defaults(self):
        def broken():
            raise RuntimeError("config unreadable")

        self.proxy.info_provider = broken
        client = self.client()
        client.send(handshake(1) + packet(0x00))
        _id, text = client.read_string_packet()
        self.assertEqual(json.loads(text)["players"]["max"], 20)


class PackingTests(unittest.TestCase):
    def test_varint_roundtrip(self):
        for value in (0, 1, 127, 128, 255, 25565, 2097151, 2147483647):
            packed = SleepProxy._pack_varint(value)
            self.assertEqual(SleepProxy._read_varint_from(packed, 0), (value, len(packed)))

    def test_legacy_string_counts_utf16_units(self):
        packed = SleepProxy._pack_legacy_string("hi§0")
        self.assertEqual(packed[:2], struct.pack(">h", 4))


if __name__ == "__main__":
    unittest.main()
