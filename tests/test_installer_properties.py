#!/usr/bin/env python3
"""server.properties preservation and the dashboard settings API."""

import os
import unittest
from unittest import mock

from test_installer_base import InstallerTestCase, installer


class PropertiesTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.load_config()

    def set_version(self, version):
        cfg = installer.load_config()
        cfg["profiles"][0]["minecraft_version"] = version
        installer.save_config(cfg)

    def props(self):
        return installer.read_properties()

    def apply(self, version, level="world", run_dir=None):
        installer._apply_managed_properties(version, level, run_dir or self.data)


class ManagedKeysTests(PropertiesTestCase):
    def test_only_managed_keys_change(self):
        original = (
            "# my notes\n"
            "motd=My server\n"
            "custom-key=1\n"
            "difficulty=hard\n"
            "level-name=world\n"
            "server-port=25577\n"
            "view-distance=12\n"
        )
        self.write("server.properties", original)
        self.apply("1.20.1", "creative")
        text = self.read("server.properties")
        self.assertTrue(text.startswith("# my notes\nmotd=My server\ncustom-key=1\ndifficulty=hard\n"))
        props = self.props()
        self.assertEqual(props["level-name"], "worlds/creative")
        self.assertEqual(props["server-port"], "25565")
        self.assertEqual(props["view-distance"], "12")  # user value kept
        self.assertEqual(props["simulation-distance"], "6")  # missing -> default

    def test_fresh_file_gets_friendly_defaults(self):
        self.apply("26.3")
        props = self.props()
        self.assertEqual(props["motd"], "A Railpowered Minecraft server")
        self.assertEqual(props["view-distance"], "8")
        self.assertEqual(props["simulation-distance"], "6")
        self.assertEqual(props["online-mode"], "true")
        self.assertTrue(self.read("server.properties").startswith("# Minecraft server settings"))

    def test_copy_into_instance_folder(self):
        run_dir = self.path("instances", "fabric-1.20.1")
        os.makedirs(run_dir)
        self.write("server.properties", "motd=Shared\n")
        self.apply("1.20.1", "modded", run_dir)
        with open(os.path.join(run_dir, "server.properties"), encoding="latin-1") as handle:
            self.assertIn("motd=Shared", handle.read())

    def test_value_formats_follow_the_version(self):
        self.write("server.properties", "difficulty=hard\ngamemode=creative\nlevel-type=minecraft:flat\n")
        self.apply("1.12.2")
        props = self.props()
        self.assertEqual((props["difficulty"], props["gamemode"], props["level-type"]), ("3", "1", "FLAT"))
        self.assertNotIn("simulation-distance", props)
        self.apply("1.16.5")
        self.assertEqual(self.props()["level-type"], "flat")
        self.apply("1.20.1")
        props = self.props()
        self.assertEqual((props["difficulty"], props["gamemode"], props["level-type"]),
                         ("hard", "creative", "minecraft:flat"))

    def test_legacy_versions_force_offline_and_restore_choice(self):
        self.write("server.properties", "online-mode=true\n")
        self.apply("1.2.5")
        self.assertEqual(self.props()["online-mode"], "false")
        self.assertEqual(installer.load_config()["settings"]["legacy_saved_online_mode"], "true")
        self.apply("1.20.1")
        self.assertEqual(self.props()["online-mode"], "true")
        self.assertNotIn("legacy_saved_online_mode", installer.load_config()["settings"])

    def test_explicit_offline_choice_survives_legacy_round_trip(self):
        self.write("server.properties", "online-mode=false\n")
        self.apply("1.6.4")
        self.apply("1.20.1")
        self.assertEqual(self.props()["online-mode"], "false")

    def test_unicode_and_escapes_round_trip(self):
        installer.write_properties({"motd": "Café \u2615 \\o/"})
        self.assertIn("\\u00e9", self.read("server.properties"))
        self.assertEqual(self.props()["motd"], "Café \u2615 \\o/")

    def test_utf8_file_written_by_minecraft_reads_correctly(self):
        # Minecraft 1.20+ rewrites server.properties as UTF-8 on start.
        with open(self.path("server.properties"), "wb") as handle:
            handle.write("#Minecraft server properties\nmotd=Café SMP\nlevel-type=minecraft\\:normal\n".encode())
        self.assertEqual(self.props()["motd"], "Café SMP")
        installer.write_properties({"pvp": "false"})
        with open(self.path("server.properties"), "rb") as handle:
            raw = handle.read()
        self.assertTrue(raw.isascii(), raw)  # readable as ISO-8859-1 and UTF-8 alike
        self.assertIn(b"motd=Caf\\u00e9 SMP", raw)
        self.assertEqual(self.props()["motd"], "Café SMP")

    def test_characters_beyond_the_bmp_use_surrogate_pairs(self):
        installer.write_properties({"motd": "Pick \U0001F4A5"})
        self.assertIn("\\ud83d\\udca5", self.read("server.properties"))
        self.assertEqual(self.props()["motd"], "Pick \U0001F4A5")


class SettingsApiTests(PropertiesTestCase):
    def test_shape_and_schema(self):
        self.set_version("1.20.1")
        settings = installer.get_settings()
        self.assertEqual(set(settings) - {"minecraft_version"},
                         {"properties", "railpowered", "restart_required", "schema"})
        expected = {"motd", "difficulty", "gamemode", "hardcore", "pvp", "max-players", "view-distance",
                    "simulation-distance", "spawn-protection", "allow-flight", "allow-nether",
                    "level-seed", "level-type", "online-mode", "enable-command-block"}
        self.assertEqual(set(settings["schema"]), expected)
        self.assertEqual(set(settings["properties"]), expected)
        for key, item in settings["schema"].items():
            with self.subTest(key=key):
                self.assertIn(item["type"], ("string", "int", "bool", "enum"))
                self.assertTrue(item["label"])
                self.assertTrue(item["help"])
                if item["type"] == "int":
                    self.assertLess(item["min"], item["max"])
                if item["type"] == "enum":
                    self.assertTrue(item["options"])
        self.assertEqual(settings["properties"]["view-distance"], 8)
        self.assertIs(settings["properties"]["pvp"], True)
        self.assertEqual(settings["railpowered"], {"idle_timeout_minutes": 10, "auto_start": False})
        self.assertFalse(settings["restart_required"])

    def test_old_versions_hide_unsupported_keys(self):
        self.set_version("1.12.2")
        schema = installer.get_settings()["schema"]
        self.assertNotIn("simulation-distance", schema)
        self.assertEqual(schema["view-distance"]["max"], 15)
        self.set_version("1.7.10")
        self.assertNotIn("spectator", installer.get_settings()["schema"]["gamemode"]["options"])
        self.set_version("1.2.5")
        settings = installer.get_settings()
        self.assertNotIn("online-mode", settings["schema"])
        self.assertNotIn("enable-command-block", settings["schema"])

    def test_numeric_legacy_values_read_as_names(self):
        self.set_version("1.12.2")
        self.write("server.properties", "difficulty=2\ngamemode=0\n")
        props = installer.get_settings()["properties"]
        self.assertEqual((props["difficulty"], props["gamemode"]), ("normal", "survival"))
        installer.update_settings({"difficulty": "hard"})
        self.assertEqual(self.props()["difficulty"], "3")

    def test_invalid_values_are_rejected_and_nothing_is_written(self):
        self.set_version("1.12.2")
        self.write("server.properties", "motd=keep\nview-distance=8\n")
        bad = [
            {"view-distance": 99},
            {"view-distance": "abc"},
            {"max-players": True},
            {"max-players": 2.5},
            {"pvp": "maybe"},
            {"difficulty": "insane"},
            {"motd": "two\nlines"},
            {"motd": "x" * 200},
            {"server-port": 1},
            {"level-name": "elsewhere"},
            {"simulation-distance": 6},
            {"motd": "fine", "view-distance": 0},
        ]
        for props in bad:
            with self.subTest(props=props), self.assertRaises(installer.InstallError) as ctx:
                installer.update_settings(props)
            self.assertEqual(ctx.exception.code, "invalid_setting")
            self.assertEqual(ctx.exception.status, 400)
        for railpowered in ({"idle_timeout_minutes": 2000}, {"idle_timeout_minutes": "5"},
                            {"auto_start": "yes"}, {"unknown": 1}):
            with self.subTest(railpowered=railpowered), self.assertRaises(installer.InstallError):
                installer.update_settings({}, railpowered)
        self.assertEqual(self.read("server.properties"), "motd=keep\nview-distance=8\n")
        self.assertEqual(installer.load_config()["settings"], {})

    def test_valid_update_keeps_other_lines(self):
        self.set_version("1.20.1")
        self.write("server.properties", "# notes\nmotd=old\nmods-extra=1\n")
        run_dir = self.path("instances", "fabric-1.20.1")
        os.makedirs(run_dir)
        cfg = installer.load_config()
        cfg["profiles"][0]["type"] = "fabric"
        installer.save_config(cfg)
        with open(os.path.join(run_dir, "server.properties"), "w", encoding="utf-8") as handle:
            handle.write("motd=old\n")
        result = installer.update_settings(
            {"motd": "New", "view-distance": "10", "hardcore": True, "difficulty": "Hard"},
            {"idle_timeout_minutes": 30},
        )
        self.assertTrue(result["ok"])
        text = self.read("server.properties")
        self.assertTrue(text.startswith("# notes\nmotd=New\nmods-extra=1\n"))
        props = self.props()
        self.assertEqual((props["view-distance"], props["hardcore"], props["difficulty"]), ("10", "true", "hard"))
        self.assertEqual(installer.read_properties(os.path.join(run_dir, "server.properties"))["motd"], "New")
        self.assertEqual(installer.railpowered_settings()["idle_timeout_minutes"], 30)

    def test_restart_required_after_change(self):
        self.vanilla_jar("1.20.1")
        self.set_version("1.20.1")
        installer.prepare_runtime()
        self.assertFalse(installer.get_settings()["restart_required"])
        result = installer.update_settings({"pvp": False})
        self.assertTrue(result["restart_required"])
        self.assertTrue(installer.get_settings()["restart_required"])

    def test_minecraft_rewriting_the_file_is_not_a_change(self):
        self.vanilla_jar("1.20.1")
        self.set_version("1.20.1")
        installer.update_settings({"motd": "Café"})
        installer.prepare_runtime()
        # What the server does on boot: rewrite every key, in UTF-8, with ':' escaped.
        props = installer.read_properties()
        text = "#Minecraft server properties\n" + "".join(
            f"{k}={v.replace(':', chr(92) + ':')}\n" for k, v in props.items()
        )
        with open(self.path("server.properties"), "wb") as handle:
            handle.write(text.encode("utf-8"))
        self.assertFalse(installer.get_settings()["restart_required"])
        installer.set_whitelist_enabled(True)  # applied live with `whitelist on`
        self.assertFalse(installer.get_settings()["restart_required"])

    def test_railway_sleep_timer_is_capped_to_what_the_dashboard_accepts(self):
        os.environ["IDLE_TIMEOUT"] = str(2 * 24 * 3600)
        current = installer.get_settings()["railpowered"]
        self.assertEqual(current["idle_timeout_minutes"], installer.IDLE_MAX_MINUTES)
        # Saving the form unchanged (it sends every field) must go through.
        self.assertTrue(installer.update_settings({"motd": "x"}, current)["ok"])

    def test_whitelist_toggle_puts_back_what_minecraft_wrote_over(self):
        self.vanilla_jar("1.20.1")
        self.set_version("1.20.1")
        self.write("server.properties", "motd=Old\npvp=true\nhand-edited=1\n")
        installer.prepare_runtime()
        at_start = installer.read_properties()  # what the running server holds
        installer.update_settings({"motd": "New"})
        self.write("server.properties", "hand-edited=2\n")
        keep = installer.read_properties()  # read just before `whitelist on`
        # Vanilla stores its in-memory properties while handling the command.
        installer.write_properties({**at_start, "white-list": "true"})
        installer.set_whitelist_enabled(True, keep=keep)
        props = self.props()
        self.assertEqual((props["motd"], props["hand-edited"]), ("New", "2"))
        self.assertEqual((props["white-list"], props["enforce-whitelist"]), ("true", "true"))
        self.assertTrue(installer.get_settings()["restart_required"])

    def test_in_game_whitelist_toggle_cannot_lose_saved_settings(self):
        self.vanilla_jar("1.20.1")
        self.set_version("1.20.1")
        installer.prepare_runtime()
        installer.update_settings({"motd": "New", "pvp": False})
        # An op types /whitelist on: Minecraft rewrites the file from memory.
        installer.write_properties({"motd": "A Railpowered Minecraft server", "pvp": "true",
                                    "white-list": "true"})
        settings = installer.get_settings()
        self.assertEqual(settings["properties"]["motd"], "New")
        self.assertIs(settings["properties"]["pvp"], False)
        self.assertTrue(settings["restart_required"])
        self.assertTrue(installer.apply_pending_properties())  # the server stopped
        props = self.props()
        self.assertEqual((props["motd"], props["pvp"], props["white-list"]), ("New", "false", "true"))
        self.assertNotIn(installer.PENDING_PROPERTIES, installer.load_config()["settings"])
        self.assertFalse(installer.apply_pending_properties())

    def test_next_start_applies_saved_settings(self):
        self.vanilla_jar("1.20.1")
        self.set_version("1.20.1")
        installer.update_settings({"motd": "Saved"})
        installer.write_properties({"motd": "Clobbered"})
        installer.prepare_runtime()
        self.assertEqual(self.props()["motd"], "Saved")
        self.assertNotIn(installer.PENDING_PROPERTIES, installer.load_config()["settings"])
        self.assertFalse(installer.get_settings()["restart_required"])

    def test_pending_settings_are_sanitized(self):
        cfg = installer.load_config()
        cfg["settings"][installer.PENDING_PROPERTIES] = {
            "motd": "ok", "level-name": "elsewhere", "white-list": "true", "pvp": "a\nb", "max-players": 5,
        }
        installer.save_config(cfg)
        installer._reset_caches()
        self.assertEqual(installer.load_config()["settings"][installer.PENDING_PROPERTIES], {"motd": "ok"})

    def test_failed_switch_leaves_shared_settings_as_they_were(self):
        self.vanilla_jar("1.20.1")
        self.vanilla_jar("1.2.5")
        self.set_version("1.20.1")
        self.write("server.properties", "online-mode=true\nmotd=Keep\ndifficulty=hard\n")
        installer.prepare_runtime()
        before = self.read("server.properties")
        cfg = installer.load_config()
        cfg["profiles"].append({"id": "old", "name": "Old", "type": "vanilla", "minecraft_version": "1.2.5",
                                "level_name": "old", "instance": None})
        installer.save_config(cfg)
        # Preparing a pre-1.7 setup forces offline mode and numeric enums, then fails late.
        with mock.patch.object(installer, "_remember_prepared_properties", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                installer.activate_setup("old")
        self.assertEqual(self.read("server.properties"), before)
        self.assertNotIn("legacy_saved_online_mode", installer.load_config()["settings"])
        self.assertEqual(installer.load_config()["active_profile"], "default")

    def test_whitelist_toggle_writes_both_keys(self):
        installer.set_whitelist_enabled(True)
        props = self.props()
        self.assertEqual((props["white-list"], props["enforce-whitelist"]), ("true", "true"))
        self.assertTrue(installer.players()["whitelist_enabled"])
        installer.set_whitelist_enabled(False)
        self.assertFalse(installer.players()["whitelist_enabled"])
        with self.assertRaises(installer.InstallError):
            installer.set_whitelist_enabled("yes")


if __name__ == "__main__":
    unittest.main()
