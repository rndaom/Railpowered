#!/usr/bin/env python3
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from manager import SleepProxy, ThreadingHTTPServer


class LegacyProtocolTests(unittest.TestCase):
    def test_pack_legacy_string(self):
        packed = SleepProxy._pack_legacy_string("hi§0§20")
        self.assertEqual(packed[:2], struct.pack(">h", 7))
        self.assertEqual(packed[2:], "hi§0§20".encode("utf-16-be"))


class HttpServerTests(unittest.TestCase):
    def test_panel_uses_threaded_server(self):
        from manager import ThreadingHTTPServer as imported

        self.assertIs(imported, ThreadingHTTPServer)

    def test_manager_reexports_split_modules(self):
        from mc_host.panel import PanelHandler
        from mc_host.process import sleep_proxy, start_server
        from manager import SleepProxy as exported
        from mc_host.proxy import SleepProxy as packaged

        self.assertIs(exported, packaged)
        self.assertTrue(hasattr(PanelHandler, "do_GET"))
        self.assertTrue(callable(start_server))
        self.assertFalse(sleep_proxy.active)

    # The end-to-end dashboard API (login, sessions, status, setups) is covered by
    # tests/test_panel.py against a real server on an ephemeral port.


class RailwayRuntimeConfigTests(unittest.TestCase):
    def test_public_address_prefers_override(self):
        from mc_host.config import resolve_public_address

        self.assertEqual(
            resolve_public_address(
                {
                    "MC_PUBLIC_ADDRESS": "play.example:25565",
                    "RAILWAY_TCP_PROXY_DOMAIN": "x.proxy.rlwy.net",
                    "RAILWAY_TCP_PROXY_PORT": "11105",
                }
            ),
            "play.example:25565",
        )

    def test_public_address_uses_railway_tcp_proxy(self):
        from mc_host.config import resolve_public_address

        self.assertEqual(
            resolve_public_address(
                {
                    "RAILWAY_TCP_PROXY_DOMAIN": "roundhouse.proxy.rlwy.net",
                    "RAILWAY_TCP_PROXY_PORT": "11105",
                }
            ),
            "roundhouse.proxy.rlwy.net:11105",
        )

    def test_public_address_ignores_unevaluated_template(self):
        from mc_host.config import resolve_public_address

        self.assertEqual(
            resolve_public_address(
                {
                    "MC_PUBLIC_ADDRESS": "${{Minecraft.RAILWAY_TCP_PROXY_DOMAIN}}",
                    "RAILWAY_TCP_PROXY_DOMAIN": "host.proxy.rlwy.net",
                    "RAILWAY_TCP_PROXY_PORT": "2000",
                }
            ),
            "host.proxy.rlwy.net:2000",
        )

    def test_web_port_ignores_minecraft_port_collision(self):
        from mc_host.config import resolve_web_port

        self.assertEqual(resolve_web_port({}), 8080)
        self.assertEqual(resolve_web_port({"PORT": "3000"}), 3000)
        self.assertEqual(resolve_web_port({"PORT": "25565"}), 8080)
        self.assertEqual(resolve_web_port({"WEB_PORT": "9090", "PORT": "25565"}), 9090)

    def test_admin_key_persists_on_volume(self):
        from mc_host.config import resolve_admin_key

        tmp = tempfile.TemporaryDirectory()
        try:
            first, from_env = resolve_admin_key(tmp.name, {})
            self.assertFalse(from_env)
            self.assertTrue(first)
            second, again_from_env = resolve_admin_key(tmp.name, {})
            self.assertFalse(again_from_env)
            self.assertEqual(first, second)
            chosen, env_set = resolve_admin_key(tmp.name, {"ADMIN_KEY": "chosen-key"})
            self.assertTrue(env_set)
            self.assertEqual(chosen, "chosen-key")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
