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


class HttpServerTests(unittest.TestCase):
    def test_panel_uses_threaded_server(self):
        from manager import ThreadingHTTPServer as imported

        self.assertIs(imported, ThreadingHTTPServer)


if __name__ == "__main__":
    unittest.main()
