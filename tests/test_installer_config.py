#!/usr/bin/env python3
"""manager.json: atomic writes, corruption recovery, v1 migration, env handling."""

import json
import os
import threading
import unittest
from unittest import mock

from test_installer_base import ROOT, InstallerTestCase, installer


class FreshVolumeTests(InstallerTestCase):
    def test_seeds_latest_vanilla_and_backup_copy(self):
        cfg = installer.load_config()
        self.assertEqual(cfg["schema"], 2)
        self.assertEqual(cfg["active_profile"], "default")
        setup = cfg["profiles"][0]
        self.assertEqual(setup["name"], "Latest vanilla")
        self.assertEqual(setup["minecraft_version"], "latest")
        self.assertEqual(setup["level_name"], "world")
        self.assertEqual(cfg["minecraft_version"], "latest")  # v1 mirror
        self.assertEqual(self.read_json("manager.json"), self.read_json("manager.json.bak"))
        self.assertEqual(self.net.calls, [])

    def test_layout_does_not_create_a_world(self):
        installer.ensure_layout()
        installer.load_config()
        self.assertEqual(os.listdir(self.path("worlds")), [])

    def test_load_returns_private_copy(self):
        cfg = installer.load_config()
        cfg["profiles"].clear()
        self.assertEqual(len(installer.load_config()["profiles"]), 1)


class AtomicWriteTests(InstallerTestCase):
    def test_failed_replace_keeps_old_file_and_no_temp(self):
        installer.load_config()
        before = self.read("manager.json")
        cfg = installer.load_config()
        cfg["profiles"][0]["name"] = "Renamed"
        with mock.patch.object(installer.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                installer.save_config(cfg)
        self.assertEqual(self.read("manager.json"), before)
        leftovers = [n for n in os.listdir(self.data) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_concurrent_saves_leave_valid_json(self):
        installer.load_config()
        errors = []

        def worker(index):
            try:
                for step in range(10):
                    def apply(cfg, value=f"name {index}-{step}"):
                        cfg["profiles"][0]["name"] = value
                    installer._mutate_config(apply)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        data = self.read_json("manager.json")
        self.assertTrue(data["profiles"][0]["name"].startswith("name "))


class CorruptionRecoveryTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.load_config()
        cfg = installer.load_config()
        cfg["profiles"].append(
            {"id": "keep", "name": "Keep me", "type": "vanilla", "minecraft_version": "1.20.1",
             "level_name": "other", "instance": None}
        )
        installer.save_config(cfg)
        installer._reset_caches()

    def corrupt(self):
        with open(self.path("manager.json"), "w", encoding="utf-8") as handle:
            handle.write('{"profiles": [ truncated')

    def test_restores_from_backup_and_keeps_damaged_copy(self):
        self.corrupt()
        cfg = installer.load_config()
        self.assertIn("keep", [s["id"] for s in cfg["profiles"]])
        kept = [n for n in os.listdir(self.data) if n.startswith("manager.json.corrupt-")]
        self.assertEqual(len(kept), 1)
        with open(self.path(kept[0]), encoding="utf-8") as handle:
            self.assertIn("truncated", handle.read())
        self.assertTrue(any("restored the last good copy" in w for w in installer.config_warnings()))
        self.assertIn("keep", [s["id"] for s in self.read_json("manager.json")["profiles"]])

    def test_without_backup_starts_fresh_but_never_destroys_the_file(self):
        os.unlink(self.path("manager.json.bak"))
        self.corrupt()
        cfg = installer.load_config()
        self.assertEqual([s["id"] for s in cfg["profiles"]], ["default"])
        kept = [n for n in os.listdir(self.data) if n.startswith("manager.json.corrupt-")]
        self.assertEqual(len(kept), 1)
        self.assertTrue(any("no backup copy" in w for w in installer.config_warnings()))

    def test_unreadable_file_raises_instead_of_reseeding(self):
        real_open = open

        def failing_open(path, *args, **kwargs):
            if str(path) == installer.paths()["config"]:
                raise PermissionError("locked")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", failing_open):
            with self.assertRaises(installer.InstallError) as ctx:
                installer.load_config()
        self.assertEqual(ctx.exception.code, "config_unreadable")
        self.assertIn("keep", [s["id"] for s in self.read_json("manager.json")["profiles"]])

    def test_invalid_entries_are_dropped_not_fatal(self):
        cfg = self.read_json("manager.json")
        cfg["profiles"].append({"id": "..", "type": "vanilla", "minecraft_version": "1.20.1"})
        cfg["profiles"].append({"id": "bad", "type": "paper", "minecraft_version": "1.20.1"})
        cfg["active_profile"] = "missing"
        with open(self.path("manager.json"), "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        loaded = installer.load_config()
        self.assertEqual({s["id"] for s in loaded["profiles"]}, {"default", "keep"})
        self.assertIn(loaded["active_profile"], {"default", "keep"})


class V1MigrationTests(InstallerTestCase):
    def test_repository_manager_json_loads(self):
        with open(os.path.join(ROOT, "manager.json"), encoding="utf-8") as handle:
            self.write("manager.json", handle.read())
        cfg = installer.load_config()
        self.assertEqual(cfg["active_profile"], "default")
        self.assertEqual(cfg["profiles"][0]["minecraft_version"], "latest")

    def test_v1_pinned_latest_stays_latest_and_world_is_protected(self):
        v1 = {
            "type": "vanilla",
            "minecraft_version": "26.2",
            "level_name": "world",
            "modpack": None,
            "instance": None,
            "active_profile": "default",
            "profiles": [
                {"id": "default", "name": "Latest vanilla", "type": "vanilla",
                 "minecraft_version": "latest", "level_name": "world", "modpack": None, "instance": None}
            ],
        }
        self.write("manager.json", json.dumps(v1))
        self.make_world("world")
        cfg = installer.load_config()
        setup = cfg["profiles"][0]
        self.assertEqual(setup["minecraft_version"], "latest")
        self.assertEqual(setup["resolved_version"], "26.2")
        self.assertEqual(cfg["schema"], 2)
        self.assertEqual(installer.world_meta("world")["last_version"], "26.2")
        self.assertTrue(os.path.isfile(self.path("manager.json.bak")))

    def test_v1_without_profiles_and_modpack_field(self):
        os.makedirs(self.path("instances", "demo"))
        v1 = {"type": "modpack", "minecraft_version": "1.20.1", "level_name": "world", "modpack": "demo"}
        self.write("manager.json", json.dumps(v1))
        cfg = installer.load_config()
        setup = cfg["profiles"][0]
        self.assertEqual(setup["type"], "modpack")
        self.assertEqual(setup["instance"], "demo")
        self.assertEqual(cfg["modpack"], "demo")

    def test_v1_world_without_level_dat_gets_no_sidecar(self):
        os.makedirs(self.path("worlds", "world"))
        self.write("manager.json", json.dumps({"type": "vanilla", "minecraft_version": "26.2", "level_name": "world"}))
        installer.load_config()
        self.assertEqual(installer.world_meta("world"), {})


class EnvironmentTests(InstallerTestCase):
    def test_first_boot_uses_server_type_and_version(self):
        os.environ["SERVER_TYPE"] = "fabric"
        os.environ["MINECRAFT_VERSION"] = "1.20.1"
        setup = installer.load_config()["profiles"][0]
        self.assertEqual((setup["type"], setup["minecraft_version"]), ("fabric", "1.20.1"))
        self.assertEqual(setup["name"], "Fabric 1.20.1")
        self.assertIsNone(installer.env_changed())

    def test_invalid_env_is_reported_not_silently_ignored(self):
        os.environ["SERVER_TYPE"] = "paper"
        setup = installer.load_config()["profiles"][0]
        self.assertEqual(setup["type"], "vanilla")
        self.assertTrue(any("SERVER_TYPE='paper'" in w for w in installer.config_warnings()))

    def test_later_env_change_is_reported_once_and_switches_nothing(self):
        installer.load_config()
        os.environ["SERVER_TYPE"] = "fabric"
        os.environ["MINECRAFT_VERSION"] = "1.21.1"
        asked = installer.env_changed()
        self.assertEqual((asked["type"], asked["minecraft_version"]), ("fabric", "1.21.1"))
        self.assertEqual(installer.active_setup()["type"], "vanilla")
        installer.acknowledge_env_setup()
        self.assertIsNone(installer.env_changed())

    def test_env_matching_the_active_setup_is_not_a_change(self):
        installer.load_config()
        os.environ["SERVER_TYPE"] = "vanilla"
        self.assertIsNone(installer.env_changed())

    def test_railpowered_settings_from_env_and_overrides(self):
        os.environ["IDLE_TIMEOUT"] = "90"
        os.environ["AUTO_START"] = "true"
        self.assertEqual(
            installer.railpowered_settings(), {"idle_timeout_minutes": 2, "auto_start": True}
        )
        os.environ["IDLE_TIMEOUT"] = "nonsense"
        self.assertEqual(installer.railpowered_settings()["idle_timeout_minutes"], 10)
        installer.update_settings({}, {"idle_timeout_minutes": 0, "auto_start": False})
        self.assertEqual(
            installer.railpowered_settings(), {"idle_timeout_minutes": 0, "auto_start": False}
        )
        self.assertEqual(self.read_json("manager.json")["settings"]["idle_timeout_minutes"], 0)


if __name__ == "__main__":
    unittest.main()
