"""The end-to-end Minecraft client, checked against the real sleep proxy.

The e2e job trusts mcproto.py to tell a working proxy from a broken one, so
it gets the same scrutiny as the proxy: every exchange runs over loopback
against an in-process SleepProxy (no Docker, no network).
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time
import unittest

import runtime_support  # noqa: F401  (sys.path)

from mc_host.proxy import MESSAGES, SleepProxy

from . import driver, mcproto


class ProxyClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wakes: list = []
        self.info = {
            "mode": "sleeping",
            "motd": "Friends SMP",
            "max_players": 12,
            "version_name": "1.21.1",
            "allowed": None,
        }
        self.proxy = SleepProxy(
            0,
            self.wakes.append,
            info=lambda: dict(self.info),
            host="127.0.0.1",
            log=lambda *_args, **_kwargs: None,
        )
        self.assertTrue(self.proxy.start())
        self.addCleanup(self.proxy.stop)
        self.port = self.proxy.bound_port

    def test_status_ping_reads_the_proxy_reply(self) -> None:
        reply = mcproto.status("127.0.0.1", self.port)
        self.assertEqual(reply.version_name, "1.21.1")
        self.assertEqual(reply.protocol, 767)
        self.assertEqual(reply.max_players, 12)
        self.assertIn("Friends SMP", reply.description)
        self.assertIn(driver.SLEEPING_MOTD, reply.description)
        self.assertGreaterEqual(reply.latency_ms, 0)
        self.assertEqual(self.wakes, [])

    def test_updating_mode_is_visible_in_the_motd_and_the_kick(self) -> None:
        self.info["mode"] = "updating"
        self.assertIn(driver.UPDATING_MOTD, mcproto.status("127.0.0.1", self.port).description)
        kicked = mcproto.login("127.0.0.1", self.port, "Steve")
        self.assertEqual(kicked.kind, "disconnect")
        self.assertEqual(kicked.reason, MESSAGES["updating"])
        self.assertIn("updating", kicked.reason)
        self.assertEqual(self.wakes, [])

    def test_legacy_ping(self) -> None:
        reply = mcproto.legacy_ping("127.0.0.1", self.port)
        self.assertEqual(reply.version, "1.21.1")
        self.assertEqual(reply.motd, "Friends SMP (sleeping)")
        self.assertEqual((reply.online, reply.max_players), (0, 12))

    def test_login_while_sleeping_is_kicked_and_wakes(self) -> None:
        reply = mcproto.login("127.0.0.1", self.port, driver.PLAYER)
        self.assertEqual(reply.kind, "disconnect")
        self.assertEqual(reply.reason, MESSAGES["sleeping"])
        self.assertIn("waking up", reply.reason)
        for _ in range(100):
            if self.wakes:
                break
            time.sleep(0.02)
        self.assertEqual(self.wakes, [driver.PLAYER])

    def test_cli_prints_json(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mcproto.main(["status", "127.0.0.1", str(self.port)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["version"]["name"], "1.21.1")


class FakeServerTest(unittest.TestCase):
    """Replies the proxy never sends, from a one-shot fake server."""

    def serve_once(self, reply: bytes) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def run() -> None:
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(reply)

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    def test_online_mode_server_asks_for_encryption(self) -> None:
        port = self.serve_once(mcproto.packet(0x01, mcproto.pack_string("") + b"\x00"))
        self.assertEqual(mcproto.login("127.0.0.1", port, "Steve").kind, "encryption")

    def test_status_that_is_not_json_is_a_protocol_error(self) -> None:
        port = self.serve_once(mcproto.packet(0x00, mcproto.pack_string("hello")))
        with self.assertRaises(mcproto.ProtocolError):
            mcproto.status("127.0.0.1", port)

    def test_closed_connection_is_an_os_error(self) -> None:
        port = self.serve_once(b"")
        with self.assertRaises(OSError):
            mcproto.status("127.0.0.1", port, timeout=2)

    def test_nobody_listening_fails_after_the_wait(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(OSError):
            mcproto.retry(lambda: mcproto.status("127.0.0.1", port, timeout=1), wait=0.3, interval=0.1)


class EncodingTest(unittest.TestCase):
    def test_varints_round_trip_like_the_proxy(self) -> None:
        for value in (0, 1, 127, 128, 255, 25565, 2097151, 2147483647, -1):
            packed = mcproto.pack_varint(value)
            self.assertEqual(packed, SleepProxy._pack_varint(value))
            self.assertEqual(mcproto.unpack_varint(packed), (value, len(packed)))

    def test_text_of_flattens_components(self) -> None:
        component = {"text": "A", "extra": [{"text": "\nB", "color": "gray"}, "C"]}
        self.assertEqual(mcproto.text_of(component), "A\nBC")
        self.assertEqual(mcproto.text_of({"translate": "multiplayer.disconnect.outdated_client"}),
                         "multiplayer.disconnect.outdated_client")
        self.assertEqual(mcproto.text_of("plain"), "plain")


class DriverHelpersTest(unittest.TestCase):
    def test_every_step_name_is_unique_per_scenario(self) -> None:
        for steps in driver.SCENARIOS.values():
            names = [step.name for step in steps]
            self.assertEqual(len(names), len(set(names)))

    def test_unknown_skip_is_rejected(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            driver.main(["--url", "http://127.0.0.1:1", "--key", "k", "--skip", "nope"])

    def test_find_line(self) -> None:
        lines = ["[Server thread/INFO]: System chat: There are 0 of a max of 20 players online:"]
        self.assertTrue(driver.find_line(lines, r"There are 0(?: of a max(?: of)? |/)\d+ players online"))
        self.assertIsNone(driver.find_line(lines, r"joined the game"))
