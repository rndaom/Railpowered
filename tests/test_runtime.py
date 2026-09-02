#!/usr/bin/env python3
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import installer
from manager import SleepProxy, ThreadingHTTPServer


class VersionTests(unittest.TestCase):
    def test_parse_old(self):
        self.assertEqual(installer.parse_version("1.2.5"), (1, 2, 5))

    def test_parse_year(self):
        self.assertEqual(installer.parse_version("26.2")[0], 26)
        self.assertTrue(installer.is_year_version("26.2"))

    def test_legacy_flags(self):
        self.assertTrue(installer.is_legacy_protocol("1.2.5"))
        self.assertTrue(installer.uses_legacy_files("1.2.5"))
        self.assertFalse(installer.is_legacy_protocol("1.8.9"))
        self.assertEqual(installer.java_major_for_version("1.2.5"), 8)
        self.assertIn(installer.java_major_for_version("1.21.1"), (21, 25))


class WorldNameTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(installer.WORLD_NAME_RE.match("world"))
        self.assertTrue(installer.WORLD_NAME_RE.match("vanilla-1_2_5"))
        self.assertFalse(installer.WORLD_NAME_RE.match("../etc"))
        self.assertFalse(installer.WORLD_NAME_RE.match("has space"))


class LegacyProtocolTests(unittest.TestCase):
    def test_pack_legacy_string(self):
        packed = SleepProxy._pack_legacy_string("hi§0§20")
        self.assertEqual(packed[:2], struct.pack(">h", 7))
        self.assertEqual(packed[2:], "hi§0§20".encode("utf-16-be"))


class BackupWorldTests(unittest.TestCase):
    def setUp(self):
        self._old_data = installer.DATA_DIR
        self._tmp = tempfile.TemporaryDirectory()
        installer.DATA_DIR = self._tmp.name

    def tearDown(self):
        installer.DATA_DIR = self._old_data
        self._tmp.cleanup()

    def test_backup_roundtrip(self):
        installer.ensure_layout()
        world = installer.world_path("world")
        os.makedirs(world, exist_ok=True)
        with open(os.path.join(world, "level.dat"), "w", encoding="utf-8") as handle:
            handle.write("dummy")
        backup = installer.create_backup("world")
        self.assertTrue(backup["name"].endswith(".tar.gz"))
        os.remove(os.path.join(world, "level.dat"))
        restored = installer.restore_backup(backup["name"], select=True)
        self.assertEqual(restored["world"], "world")
        self.assertTrue(os.path.isfile(os.path.join(world, "level.dat")))


class DataDirTest(unittest.TestCase):
    def setUp(self):
        self._old_data = installer.DATA_DIR
        self._tmp = tempfile.TemporaryDirectory()
        installer.DATA_DIR = self._tmp.name

    def tearDown(self):
        installer.DATA_DIR = self._old_data
        self._tmp.cleanup()


class WorldLinkTests(DataDirTest):
    def test_vanilla_level_name_stays_under_shared_worlds(self):
        installer.ensure_layout()
        props = installer.properties_for(
            {"type": "vanilla", "minecraft_version": "1.2.5", "level_name": "alpha"}
        )
        self.assertEqual(props["level-name"], "worlds/alpha")

    def test_instance_worlds_symlink_is_shared(self):
        installer.ensure_layout()
        cfg = {
            "type": "fabric",
            "minecraft_version": "1.20.1",
            "level_name": "modern",
        }
        dest = installer.instance_dir("fabric", "1.20.1")
        os.makedirs(dest, exist_ok=True)
        installer.save_config(cfg)
        installer._relink_instance_world(cfg)
        link = os.path.join(dest, "worlds")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(
            os.path.realpath(link), os.path.realpath(installer.paths()["worlds"])
        )
        marker = os.path.join(dest, "worlds", "modern", "level.dat")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("ok")
        self.assertTrue(
            os.path.isfile(os.path.join(installer.paths()["worlds"], "modern", "level.dat"))
        )
        props = installer.properties_for(cfg)
        self.assertEqual(props["level-name"], "worlds/modern")

    def test_migrates_instance_local_worlds_folder(self):
        installer.ensure_layout()
        cfg = {
            "type": "fabric",
            "minecraft_version": "1.20.1",
            "level_name": "modern",
        }
        dest = installer.instance_dir("fabric", "1.20.1")
        local = os.path.join(dest, "worlds", "stuck")
        os.makedirs(local)
        with open(os.path.join(local, "level.dat"), "w", encoding="utf-8") as handle:
            handle.write("moved")
        installer._relink_instance_world(cfg)
        self.assertTrue(os.path.islink(os.path.join(dest, "worlds")))
        self.assertTrue(
            os.path.isfile(os.path.join(installer.paths()["worlds"], "stuck", "level.dat"))
        )


class UuidTests(unittest.TestCase):
    def test_offline_uuid_is_stable_and_not_zero(self):
        first = installer.offline_uuid("Steve")
        self.assertEqual(first, installer.offline_uuid("Steve"))
        self.assertNotEqual(first, installer.offline_uuid("Alex"))
        self.assertNotIn("00000000-0000-0000-0000-000000000000", first)
        self.assertEqual(len(first), 36)
        self.assertEqual(first[14], "3")

    def test_write_json_uses_offline_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "whitelist.json")
            installer.write_name_file(path, ["Steve"], True, online=False)
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload[0]["name"], "Steve")
            self.assertEqual(payload[0]["uuid"], installer.offline_uuid("Steve"))


class LoaderPinTests(unittest.TestCase):
    def test_env_pin_wins(self):
        old = installer.FABRIC_LOADER_PIN
        installer.FABRIC_LOADER_PIN = "0.16.14"
        try:
            self.assertEqual(installer.fabric_loader_for("26.2"), "0.16.14")
        finally:
            installer.FABRIC_LOADER_PIN = old

    def test_meta_stable_loader(self):
        old = installer.FABRIC_LOADER_PIN
        installer.FABRIC_LOADER_PIN = ""
        try:
            with mock.patch.object(
                installer,
                "_http_json",
                return_value=[
                    {"loader": {"version": "0.19.5", "stable": True}},
                    {"loader": {"version": "0.18.0", "stable": True}},
                ],
            ):
                self.assertEqual(installer.fabric_loader_for("26.2"), "0.19.5")
        finally:
            installer.FABRIC_LOADER_PIN = old

    def test_year_version_fallback(self):
        old = installer.FABRIC_LOADER_PIN
        installer.FABRIC_LOADER_PIN = ""
        try:
            with mock.patch.object(
                installer, "_http_json", side_effect=installer.InstallError("offline")
            ):
                self.assertEqual(installer.fabric_loader_for("26.2"), "0.19.3")
                self.assertEqual(installer.fabric_loader_for("1.20.1"), "0.16.14")
        finally:
            installer.FABRIC_LOADER_PIN = old


class MrpackLoaderTests(DataDirTest):
    def test_installs_fabric_from_dependencies(self):
        installer.ensure_layout()
        dest = installer.instance_dir("modpack", "demo", name="demo")
        os.makedirs(dest)
        seen = {}

        def fake_fabric(version, dest=None, loader=None):
            seen["version"] = version
            seen["dest"] = dest
            seen["loader"] = loader
            os.makedirs(dest, exist_ok=True)
            with open(
                os.path.join(dest, "fabric-server-launch.jar"), "w", encoding="utf-8"
            ) as handle:
                handle.write("jar")
            return dest

        with mock.patch.object(installer, "install_fabric", side_effect=fake_fabric):
            installer._install_modpack_loader(
                dest, {"minecraft": "1.20.1", "fabric-loader": "0.16.9"}
            )
        self.assertEqual(seen["version"], "1.20.1")
        self.assertEqual(seen["loader"], "0.16.9")
        self.assertTrue(os.path.isfile(os.path.join(dest, "fabric-server-launch.jar")))

    def test_rejects_quilt(self):
        installer.ensure_layout()
        dest = installer.instance_dir("modpack", "demo", name="demo")
        os.makedirs(dest)
        with self.assertRaises(installer.InstallError):
            installer._install_modpack_loader(
                dest, {"minecraft": "1.20.1", "quilt-loader": "0.1"}
            )

    def test_mrpack_archive_calls_loader(self):
        installer.ensure_layout()
        pack = os.path.join(self._tmp.name, "pack.mrpack")
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr(
                "modrinth.index.json",
                json.dumps(
                    {
                        "name": "Demo",
                        "dependencies": {
                            "minecraft": "1.20.1",
                            "fabric-loader": "0.16.9",
                        },
                        "files": [],
                    }
                ),
            )

        def fake_fabric(version, dest=None, loader=None):
            os.makedirs(dest, exist_ok=True)
            with open(
                os.path.join(dest, "fabric-server-launch.jar"), "w", encoding="utf-8"
            ) as handle:
                handle.write("jar")
            return dest

        with mock.patch.object(installer, "install_fabric", side_effect=fake_fabric):
            cfg = installer.install_modpack_archive(pack, "demo")
        self.assertEqual(cfg["minecraft_version"], "1.20.1")
        self.assertEqual(cfg["type"], "modpack")
        launch = os.path.join(
            installer.instance_dir("modpack", "demo", name="demo"),
            "fabric-server-launch.jar",
        )
        self.assertTrue(os.path.isfile(launch))


class AccessListTests(DataDirTest):
    def test_modern_lists_use_json(self):
        installer.ensure_layout()
        installer.write_properties({"online-mode": "false", "white-list": "true"})
        installer.save_config(
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "world"}
        )
        names = installer.update_name_list("whitelist", "Steve", add=True)
        self.assertEqual(names, ["Steve"])
        path = os.path.join(installer.DATA_DIR, "whitelist.json")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload[0]["uuid"], installer.offline_uuid("Steve"))

    def test_syncs_lists_into_instance(self):
        installer.ensure_layout()
        cfg = {
            "type": "fabric",
            "minecraft_version": "1.20.1",
            "level_name": "world",
        }
        installer.save_config(cfg)
        dest = installer.instance_dir("fabric", "1.20.1")
        os.makedirs(dest, exist_ok=True)
        installer.update_name_list("whitelist", "Alex", add=True)
        copied = os.path.join(dest, "whitelist.json")
        self.assertTrue(os.path.isfile(copied))


class ProfileTests(DataDirTest):
    def test_seeds_default_setup(self):
        installer.ensure_layout()
        cfg = installer.load_config()
        self.assertEqual(cfg["active_profile"], "default")
        self.assertEqual(len(cfg["profiles"]), 1)
        self.assertEqual(cfg["profiles"][0]["name"], "Latest vanilla")
        self.assertEqual(cfg["profiles"][0]["type"], "vanilla")
        self.assertEqual(cfg["profiles"][0]["level_name"], "world")

    def test_apply_creates_second_setup_and_switch_restores_default(self):
        installer.ensure_layout()
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
        profiles = installer.list_profiles()
        self.assertEqual(len(profiles), 2)
        fabric_id = next(item["id"] for item in profiles if item["type"] == "fabric")
        default_id = next(item["id"] for item in profiles if item["id"] == "default")
        installer.activate_profile(fabric_id)
        cfg = installer.load_config()
        self.assertEqual(cfg["type"], "fabric")
        self.assertEqual(cfg["level_name"], "sky")
        installer.activate_profile(default_id)
        cfg = installer.load_config()
        self.assertEqual(cfg["type"], "vanilla")
        self.assertEqual(cfg["level_name"], "world")
        self.assertEqual(cfg["active_profile"], "default")

    def test_same_fingerprint_reuses_setup(self):
        installer.ensure_layout()
        cfg = installer.load_config()
        first_id = cfg["active_profile"]
        installer.upsert_profile(cfg)
        installer.save_config(cfg)
        self.assertEqual(len(installer.list_profiles()), 1)
        self.assertEqual(installer.load_config()["active_profile"], first_id)

    def test_cannot_delete_active_or_last_setup(self):
        installer.ensure_layout()
        installer.load_config()
        with self.assertRaises(installer.InstallError):
            installer.delete_profile("default")
        fabric = installer.load_config()
        fabric.update(
            {"type": "fabric", "minecraft_version": "1.20.1", "level_name": "sky"}
        )
        installer.upsert_profile(fabric)
        installer.save_config(fabric)
        fabric_id = next(
            item["id"] for item in installer.list_profiles() if item["type"] == "fabric"
        )
        with self.assertRaises(installer.InstallError):
            installer.delete_profile(fabric_id)
        installer.activate_profile("default")
        installer.delete_profile(fabric_id)
        self.assertEqual(len(installer.list_profiles()), 1)

    def test_apply_server_keeps_previous_setup(self):
        installer.ensure_layout()
        installer.load_config()
        cfg = installer.load_config()
        cfg.update(
            {
                "type": "fabric",
                "minecraft_version": "1.20.1",
                "level_name": "sky",
            }
        )
        fake = installer.RuntimeSpec(
            java_bin="/bin/true",
            cwd=installer.DATA_DIR,
            jar="server.jar",
            version="1.20.1",
            type="fabric",
            level_name="sky",
            java_major=21,
        )
        with mock.patch.object(installer, "prepare_runtime", return_value=fake):
            installer.apply_server(cfg)
        types = {item["type"] for item in installer.list_profiles()}
        self.assertEqual(types, {"vanilla", "fabric"})
        self.assertEqual(installer.load_config()["active_profile"] != "default", True)
        installer.activate_profile("default")
        self.assertEqual(installer.load_config()["type"], "vanilla")
        self.assertEqual(installer.load_config()["level_name"], "world")

    def test_rename_and_delete_world_drops_setup(self):
        installer.ensure_layout()
        installer.load_config()
        installer.create_world("creative")
        installer.select_world("creative")
        profiles = installer.list_profiles()
        self.assertEqual(len(profiles), 2)
        creative = next(item for item in profiles if item["level_name"] == "creative")
        installer.rename_profile(creative["id"], "Creative world")
        self.assertEqual(
            installer.get_profile(installer.load_config(), creative["id"])["name"],
            "Creative world",
        )
        installer.activate_profile("default")
        installer.delete_world("creative")
        self.assertFalse(
            any(item["level_name"] == "creative" for item in installer.list_profiles())
        )


class LatestDefaultTests(DataDirTest):
    def test_default_config_is_latest_vanilla(self):
        cfg = installer.default_config()
        self.assertEqual(cfg["type"], "vanilla")
        self.assertEqual(cfg["minecraft_version"], "latest")
        self.assertEqual(cfg["level_name"], "world")

    def test_resolve_latest(self):
        installer.clear_latest_cache()
        with mock.patch.object(
            installer,
            "fetch_version_manifest",
            return_value={"latest": {"release": "26.2"}, "versions": []},
        ):
            self.assertEqual(installer.resolve_minecraft_version("latest"), "26.2")
            self.assertEqual(installer.resolve_minecraft_version(""), "26.2")
            self.assertEqual(installer.resolve_minecraft_version("1.20.1"), "1.20.1")

    def test_modern_property_defaults(self):
        installer.ensure_layout()
        installer.clear_latest_cache()
        with mock.patch.object(installer, "latest_release", return_value="26.2"):
            props = installer.properties_for(
                {
                    "type": "vanilla",
                    "minecraft_version": "latest",
                    "level_name": "world",
                }
            )
        self.assertEqual(props["online-mode"], "true")
        self.assertEqual(props["white-list"], "false")
        self.assertEqual(props["level-name"], "worlds/world")
        self.assertEqual(props["motd"], "A Minecraft Server")


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

        old_data = installer.DATA_DIR
        tmp = tempfile.TemporaryDirectory()
        installer.DATA_DIR = tmp.name
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
            self.assertEqual(payload["product"], "Powered Rail")
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
            installer.DATA_DIR = old_data
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
