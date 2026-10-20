#!/usr/bin/env python3
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import installer
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

    def test_health_login_and_status(self):
        import http.cookiejar
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request

        from mc_host.config import ADMIN_KEY
        from mc_host.panel import PanelHandler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            mock.patch.object(installer, "DATA_DIR", tmp.name),
            mock.patch.object(
                installer, "_urlopen", side_effect=OSError("network disabled in tests")
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        installer._reset_caches()
        self.addCleanup(installer._reset_caches)
        installer.ensure_layout()
        installer.save_config(
            {
                "type": "vanilla",
                "minecraft_version": "26.2",
                "level_name": "world",
            }
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), PanelHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            with urllib.request.urlopen(f"{base}/health") as resp:
                self.assertEqual(json.loads(resp.read()), {"status": "ok"})
            try:
                urllib.request.urlopen(f"{base}/api/status")
                self.fail("expected unauthorized status")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401)
                exc.read()
                exc.close()

            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login = urllib.request.Request(
                f"{base}/api/login",
                data=urllib.parse.urlencode({"key": ADMIN_KEY}).encode(),
                method="POST",
            )
            with opener.open(login) as resp:
                self.assertEqual(resp.status, 200)
            with opener.open(f"{base}/api/status") as resp:
                payload = json.loads(resp.read())
            self.assertIn("running", payload)
            self.assertIn("installing", payload)
            self.assertEqual(payload["minecraft_version"], "26.2")
            self.assertEqual(payload["product"], "Railpowered")
            self.assertFalse(payload["legacy"])
            self.assertFalse(payload["running"])
            with opener.open(f"{base}/api/profiles") as resp:
                profiles = json.loads(resp.read())
            self.assertTrue(profiles["profiles"])
            self.assertEqual(profiles["profiles"][0]["id"], "default")

            fabric = installer.load_config()
            fabric.update(
                {
                    "type": "fabric",
                    "minecraft_version": "1.20.1",
                    "level_name": "sky",
                }
            )
            installer.upsert_profile(fabric)
            installer.save_config(fabric)
            fabric_id = next(
                item["id"]
                for item in installer.list_profiles()
                if item["type"] == "fabric"
            )
            fake = installer.RuntimeSpec(
                java_bin="/bin/true",
                cwd=installer.DATA_DIR,
                jar="server.jar",
                version="26.2",
                type="vanilla",
                level_name="world",
                java_major=25,
            )
            with mock.patch.object(installer, "prepare_runtime", return_value=fake):
                with mock.patch("mc_host.panel.start_server", return_value=True):
                    use = urllib.request.Request(
                        f"{base}/api/profiles/default/use",
                        data=b"{}",
                        method="POST",
                    )
                    with opener.open(use) as resp:
                        switched = json.loads(resp.read())
            self.assertTrue(switched["success"])
            self.assertTrue(switched["started"])
            self.assertEqual(installer.load_config()["type"], "vanilla")
            self.assertEqual(installer.load_config()["level_name"], "world")
            self.assertTrue(any(item["id"] == fabric_id for item in installer.list_profiles()))
        finally:
            server.shutdown()
            server.server_close()


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
