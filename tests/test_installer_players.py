#!/usr/bin/env python3
"""Players: Mojang lookups with a UUID cache, list formats per era, sync-back."""

import json
import os
import time
import unittest
import urllib.error
from unittest import mock

from test_installer_base import InstallerTestCase, installer

NOTCH_ONLINE = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NOTCH_OFFLINE = "b50ad385-829d-3141-a216-7e7d7539ba7f"


class PlayersTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.load_config()
        self.set_setup("vanilla", "1.20.1")
        self.write("server.properties", "online-mode=true\n")

    def set_setup(self, server_type, version):
        cfg = installer.load_config()
        cfg["profiles"][0].update({"type": server_type, "minecraft_version": version})
        installer.save_config(cfg)

    def mojang(self, name, body=None, status=200, which=0):
        url = installer.MOJANG_PROFILE_URLS[which].format(name=name)
        if isinstance(body, BaseException):
            self.net.add_error(url, body)
        else:
            self.net.add(url, body if body is not None else b"", status=status)

    def names(self, kind):
        return [e["name"] for e in installer.players()[kind]]


class UuidTests(PlayersTestCase):
    def test_offline_uuid_known_answer(self):
        self.assertEqual(installer.offline_uuid("Notch"), NOTCH_OFFLINE)
        self.assertNotEqual(installer.offline_uuid("notch"), NOTCH_OFFLINE)

    def test_dashed_uuid(self):
        self.assertEqual(installer.dashed_uuid("069a79f444e94726a5befca90e38aaf5"), NOTCH_ONLINE)
        with self.assertRaises(installer.InstallError):
            installer.dashed_uuid("nope")


class OnlineModeTests(PlayersTestCase):
    def test_lookup_uses_exact_name_and_caches(self):
        self.mojang("notch", {"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"})
        added = installer.add_player("ops", "notch")
        self.assertEqual(added, {"name": "Notch", "uuid": NOTCH_ONLINE})
        ops = self.read_json("ops.json")
        self.assertEqual(ops[0]["uuid"], NOTCH_ONLINE)
        self.assertEqual(ops[0]["level"], 4)
        cache = self.read_json(".uuid-cache.json")
        self.assertEqual(cache["notch"]["uuid"], NOTCH_ONLINE)
        calls = len(self.net.calls)
        installer.add_player("whitelist", "NOTCH")
        self.assertEqual(len(self.net.calls), calls)  # served from the cache
        self.assertEqual(self.read_json("whitelist.json")[0]["uuid"], NOTCH_ONLINE)

    def test_unknown_account_is_an_error_not_an_offline_uuid(self):
        for status in (204, 404):
            with self.subTest(status=status):
                self.net.routes.clear()
                self.mojang("Ghosty", b"", status=status)
                with self.assertRaises(installer.InstallError) as ctx:
                    installer.add_player("whitelist", "Ghosty")
                self.assertEqual(ctx.exception.code, "player_not_found")
                self.assertIn("No Minecraft account", str(ctx.exception))
        self.assertFalse(os.path.exists(self.path("whitelist.json")))

    def test_network_failure_is_an_error(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.add_player("ops", "Steve")
        self.assertEqual(ctx.exception.code, "mojang_unreachable")
        self.assertEqual(ctx.exception.status, 502)
        self.assertFalse(os.path.exists(self.path("ops.json")))

    def test_rate_limit_is_an_error(self):
        self.mojang("Steve", b"slow down", status=429)
        self.mojang("Steve", b"slow down", status=429, which=1)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.add_player("ops", "Steve")
        self.assertEqual(ctx.exception.code, "mojang_unreachable")

    def test_fallback_endpoint(self):
        self.mojang("Steve", urllib.error.URLError("down"))
        self.mojang("Steve", {"id": "8667ba71b85a4004af54457a9734eed7", "name": "Steve"}, which=1)
        self.assertEqual(installer.add_player("ops", "Steve")["uuid"], "8667ba71-b85a-4004-af54-457a9734eed7")

    def test_odd_name_in_mojang_reply_is_not_trusted(self):
        self.mojang("Steve", {"id": "8667ba71b85a4004af54457a9734eed7", "name": "Steve\nop Mallory"})
        added = installer.add_player("ops", "Steve")
        self.assertEqual(added["name"], "Steve")
        self.assertEqual([e["name"] for e in self.read_json("ops.json")], ["Steve"])

    def test_expired_cache_is_refreshed(self):
        self.write(".uuid-cache.json", json.dumps(
            {"notch": {"name": "Notch", "uuid": NOTCH_ONLINE, "at": int(time.time()) - 40 * 86400}}))
        with self.assertRaises(installer.InstallError):
            installer.add_player("ops", "Notch")
        self.write(".uuid-cache.json", json.dumps(
            {"notch": {"name": "Notch", "uuid": NOTCH_ONLINE, "at": int(time.time())}}))
        self.assertEqual(installer.add_player("ops", "Notch")["uuid"], NOTCH_ONLINE)


class OfflineModeTests(PlayersTestCase):
    def test_offline_mode_uses_offline_uuid_without_network(self):
        self.write("server.properties", "online-mode=false\n")
        added = installer.add_player("whitelist", "Notch")
        self.assertEqual(added["uuid"], NOTCH_OFFLINE)
        self.assertEqual(self.net.calls, [])
        self.assertFalse(installer.players()["online_mode"])

    def test_legacy_versions_are_always_offline(self):
        self.set_setup("vanilla", "1.2.5")
        added = installer.add_player("ops", "Notch")
        self.assertEqual(added["uuid"], NOTCH_OFFLINE)
        self.assertEqual(self.net.calls, [])
        self.assertEqual(self.read("ops.txt"), "Notch\n")


class ListManagementTests(PlayersTestCase):
    def setUp(self):
        super().setUp()
        self.write("server.properties", "online-mode=false\n")

    def test_invalid_names_and_kinds(self):
        for name in ("", "bad name", "x" * 17, "../x"):
            with self.subTest(name=name), self.assertRaises(installer.InstallError) as ctx:
                installer.add_player("ops", name)
            self.assertEqual(ctx.exception.code, "invalid_player_name")
        with self.assertRaises(installer.InstallError):
            installer.add_player("banned", "Steve")

    def test_add_is_case_insensitive_and_remove_works(self):
        installer.add_player("whitelist", "Steve")
        self.assertEqual(installer.add_player("whitelist", "steve")["name"], "Steve")
        self.assertEqual(self.names("whitelist"), ["Steve"])
        self.assertTrue(installer.remove_player("whitelist", "STEVE"))
        self.assertFalse(installer.remove_player("whitelist", "Steve"))
        self.assertEqual(self.names("whitelist"), [])

    def test_unknown_fields_are_preserved(self):
        self.write("ops.json", json.dumps([
            {"uuid": NOTCH_OFFLINE, "name": "Notch", "level": 2, "bypassesPlayerLimit": True, "extra": "x"}]))
        installer.add_player("ops", "Steve")
        ops = {e["name"]: e for e in self.read_json("ops.json")}
        self.assertEqual(ops["Notch"]["level"], 2)
        self.assertTrue(ops["Notch"]["bypassesPlayerLimit"])
        self.assertEqual(ops["Notch"]["extra"], "x")

    def test_txt_lists_are_imported(self):
        self.write("white-list.txt", "# old list\nAlex\nsteve\nbad name\n")
        self.assertEqual(self.names("whitelist"), ["Alex", "steve"])


class ListFormatTests(PlayersTestCase):
    def setUp(self):
        super().setUp()
        self.write("server.properties", "online-mode=false\n")
        self.write("ops.json", json.dumps([{"uuid": installer.offline_uuid("Alex"), "name": "Alex", "level": 4}]))
        self.write("whitelist.json", json.dumps([{"uuid": installer.offline_uuid("Bob"), "name": "Bob"}]))
        self.run_dir = self.path("instances", "x")
        os.makedirs(self.run_dir)

    def run_file(self, name):
        with open(os.path.join(self.run_dir, name), encoding="utf-8") as handle:
            return handle.read()

    def test_before_1_7_6_uses_txt(self):
        installer._render_access_lists("1.7.5", self.run_dir)
        self.assertEqual(self.run_file("ops.txt"), "Alex\n")
        self.assertEqual(self.run_file("white-list.txt"), "Bob\n")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "ops.json")))

    def test_from_1_7_6_uses_json(self):
        for version in ("1.7.6", "1.12.2", "26.3"):
            with self.subTest(version=version):
                installer._render_access_lists(version, self.run_dir)
                ops = json.loads(self.run_file("ops.json"))
                self.assertEqual(ops[0]["name"], "Alex")
                self.assertEqual(ops[0]["uuid"], installer.offline_uuid("Alex"))
                self.assertEqual(json.loads(self.run_file("whitelist.json"))[0]["name"], "Bob")

    def test_online_mode_upgrades_offline_uuids_when_possible(self):
        self.write("server.properties", "online-mode=true\n")
        self.mojang("Alex", {"id": "11111111222233334444555555555555", "name": "Alex"})
        warnings = installer._render_access_lists("1.20.1", self.run_dir, network=True)
        self.assertEqual(json.loads(self.run_file("ops.json"))[0]["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(self.read_json("ops.json")[0]["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(len(warnings), 1)  # Bob couldn't be checked; kept as-is
        self.assertEqual(json.loads(self.run_file("whitelist.json"))[0]["name"], "Bob")


class SyncBackTests(PlayersTestCase):
    def setUp(self):
        super().setUp()
        self.write("server.properties", "online-mode=false\nwhite-list=false\n")
        self.write("whitelist.json", "[]")

    def make_older(self, *rels):
        past = time.time() - 600
        for rel in rels:
            os.utime(self.path(*rel.split("/")), (past, past))

    def test_in_game_changes_in_instance_are_kept(self):
        self.set_setup("fabric", "1.20.1")
        run_dir = self.path("instances", "fabric-1.20.1")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "whitelist.json"), "w", encoding="utf-8") as handle:
            json.dump([{"uuid": installer.offline_uuid("Alex"), "name": "Alex"}], handle)
        with open(os.path.join(run_dir, "server.properties"), "w", encoding="utf-8") as handle:
            handle.write("white-list=true\nenforce-whitelist=true\n")
        self.make_older("whitelist.json", "server.properties")
        data = installer.players()
        self.assertEqual([e["name"] for e in data["whitelist"]], ["Alex"])
        self.assertTrue(data["whitelist_enabled"])
        self.assertEqual(self.read_json("whitelist.json")[0]["name"], "Alex")

    def test_older_runtime_copy_does_not_overwrite_dashboard_changes(self):
        self.set_setup("fabric", "1.20.1")
        run_dir = self.path("instances", "fabric-1.20.1")
        os.makedirs(run_dir)
        stale = os.path.join(run_dir, "whitelist.json")
        with open(stale, "w", encoding="utf-8") as handle:
            json.dump([{"uuid": installer.offline_uuid("Old"), "name": "Old"}], handle)
        past = time.time() - 600
        os.utime(stale, (past, past))
        installer.add_player("whitelist", "New")
        self.assertEqual(self.names("whitelist"), ["New"])

    def test_legacy_txt_changes_are_imported(self):
        self.set_setup("vanilla", "1.6.4")
        self.write("white-list.txt", "Alex\nBob\n")
        self.make_older("whitelist.json")
        self.assertEqual(self.names("whitelist"), ["Alex", "Bob"])

    def test_sync_failure_never_blocks_reading(self):
        with mock.patch.object(installer, "sync_access_lists_from_runtime", side_effect=OSError("busy")):
            self.assertEqual(installer.players()["whitelist"], [])


if __name__ == "__main__":
    unittest.main()
