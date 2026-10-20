#!/usr/bin/env python3
"""Backups and worlds: world-only archives, safe restore, cleanup on failure."""

import io
import json
import os
import re
import tarfile
import types
import unittest
from unittest import mock

from test_installer_base import InstallerTestCase, installer


class BackupTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.load_config()
        self.make_world("world", last_version="1.21.1", extra={"data/a.txt": "v1", "session.lock": "lock"})
        self.write("server.properties", "motd=before\n")
        self.write("ops.json", json.dumps([{"uuid": installer.offline_uuid("Steve"), "name": "Steve", "level": 4}]))

    def world_file(self, *parts):
        with open(self.path("worlds", *parts), encoding="utf-8") as handle:
            return handle.read()

    def backup_files(self):
        return sorted(os.listdir(self.path("backups")))


class CreateBackupTests(BackupTestCase):
    def test_archive_holds_only_the_world_and_metadata(self):
        result = installer.create_backup("world")
        self.assertRegex(result["name"], r"^world-\d{8}-\d{6}\.tar\.gz$")
        members = self.tar_members(self.path("backups", result["name"]))
        self.assertEqual(members[0], installer.BACKUP_META_FILE)
        self.assertTrue(all(m == "world" or m.startswith("world/") for m in members[1:]))
        self.assertIn("world/level.dat", members)
        self.assertIn("world/data/a.txt", members)
        self.assertNotIn("world/session.lock", members)
        for name in ("manager.json", "server.properties", "ops.json"):
            self.assertFalse(any(m.endswith(name) for m in members))
        with tarfile.open(self.path("backups", result["name"]), "r:gz") as archive:
            meta = json.load(archive.extractfile(installer.BACKUP_META_FILE))
        self.assertEqual(meta["world"], "world")
        self.assertEqual(meta["last_version"], "1.21.1")
        for key in ("name", "world", "size_bytes", "created_at", "minecraft_version"):
            self.assertIn(key, result)

    def test_default_world_is_the_active_one_and_names_are_unique(self):
        first = installer.create_backup()
        second = installer.create_backup()
        self.assertEqual(first["world"], "world")
        self.assertNotEqual(first["name"], second["name"])

    def test_hooks_run_and_failures_leave_nothing(self):
        calls = []
        installer.create_backup("world", before_tar=lambda: calls.append("before"),
                                after_tar=lambda: calls.append("after"))
        self.assertEqual(calls, ["before", "after"])
        for name in os.listdir(self.path("backups")):
            os.unlink(self.path("backups", name))

        calls.clear()

        def boom():
            raise RuntimeError("save-off failed")

        with self.assertRaises(RuntimeError):
            installer.create_backup("world", before_tar=boom, after_tar=lambda: calls.append("after"))
        self.assertEqual(calls, ["after"])
        self.assertEqual(self.backup_files(), [])

        with mock.patch.object(installer.tarfile, "open", side_effect=OSError("No space left on device")):
            with self.assertRaises(installer.InstallError) as ctx:
                installer.create_backup("world")
        self.assertEqual(ctx.exception.code, "backup_failed")
        self.assertEqual(self.backup_files(), [])

    def test_disk_full_is_refused_up_front(self):
        usage = types.SimpleNamespace(total=10 * 1024**3, used=10 * 1024**3, free=1024)
        with mock.patch.object(installer.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(installer.InstallError) as ctx:
                installer.create_backup("world")
        self.assertEqual(ctx.exception.code, "disk_full")
        self.assertEqual(ctx.exception.status, 507)
        self.assertEqual(self.backup_files(), [])

    def test_missing_world(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.create_backup("ghost")
        self.assertEqual(ctx.exception.code, "world_not_found")

    def test_retention_prunes_only_automatic_backups(self):
        os.environ["BACKUP_KEEP"] = "2"
        installer.create_backup("world")
        for _ in range(4):
            newest = installer.create_backup("world", kind="auto")["name"]
        kinds = [b["kind"] for b in installer.list_backups()]
        self.assertIn(newest, [b["name"] for b in installer.list_backups()])
        self.assertEqual(kinds.count("auto"), 2)
        self.assertEqual(kinds.count("manual"), 1)


class RestoreTests(BackupTestCase):
    def setUp(self):
        super().setUp()
        self.backup = installer.create_backup("world")["name"]
        self.write("worlds/world/new.txt", "added later")
        self.write("worlds/world/data/a.txt", "v2")
        self.write("server.properties", "motd=after\n")
        self.write("ops.json", "[]")
        self.manager_before = self.read("manager.json")

    def test_restore_replaces_world_and_keeps_everything_else(self):
        result = installer.restore_backup(self.backup)
        self.assertEqual(result["world"], "world")
        self.assertEqual(self.world_file("world", "data", "a.txt"), "v1")
        self.assertFalse(os.path.exists(self.path("worlds", "world", "new.txt")))  # replaced, not merged
        self.assertEqual(self.read("server.properties"), "motd=after\n")
        self.assertEqual(self.read("ops.json"), "[]")
        self.assertEqual(self.read("manager.json"), self.manager_before)
        self.assertRegex(result["pre_restore_backup"], r"^world-pre-restore-\d{8}-\d{6}(-\d+)?\.tar\.gz$")
        installer.restore_backup(result["pre_restore_backup"])  # undo the restore
        self.assertEqual(self.world_file("world", "data", "a.txt"), "v2")
        self.assertEqual(self.world_file("world", "new.txt"), "added later")
        self.assertFalse([n for n in os.listdir(self.path("worlds")) if n.startswith(".")])

    def test_restoring_a_newer_world_into_the_active_setup_warns(self):
        self.assertIsNone(installer.restore_backup(self.backup)["downgrade_warning"])
        cfg = installer.load_config()
        cfg["profiles"][0]["minecraft_version"] = "1.20.1"
        installer.save_config(cfg)
        warning = installer.restore_backup(self.backup)["downgrade_warning"]
        self.assertEqual((warning["newest_version"], warning["version"]), ("1.21.1", "1.20.1"))
        self.assertIn("confirm", warning["message"])
        self.assertIsNone(installer.restore_backup(self.backup, target="copy")["downgrade_warning"])

    def test_restore_into_new_world_name(self):
        result = installer.restore_backup(self.backup, target="copy")
        self.assertEqual(result["world"], "copy")
        self.assertIsNone(result["pre_restore_backup"])
        self.assertEqual(self.world_file("copy", "data", "a.txt"), "v1")
        self.assertEqual(self.world_file("world", "data", "a.txt"), "v2")
        self.assertEqual(installer.world_meta("copy")["last_version"], "1.21.1")

    def test_unknown_and_damaged_backups(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.restore_backup("nope.tar.gz")
        self.assertEqual(ctx.exception.code, "backup_not_found")
        self.write("backups/broken-20260101-000000.tar.gz", b"not gzip")
        listed = {b["name"]: b for b in installer.list_backups()}
        self.assertTrue(listed["broken-20260101-000000.tar.gz"]["damaged"])
        with self.assertRaises(installer.InstallError) as ctx:
            installer.restore_backup("broken-20260101-000000.tar.gz")
        self.assertEqual(ctx.exception.code, "invalid_backup")
        self.assertEqual(self.world_file("world", "data", "a.txt"), "v2")

    def test_unsafe_members_are_refused(self):
        path = self.path("backups", "evil-20260101-000000.tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            payload = json.dumps({"world": "evil"}).encode()
            info = tarfile.TarInfo(installer.BACKUP_META_FILE)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            body = b"x"
            info = tarfile.TarInfo("evil/../../escape.txt")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        with self.assertRaises(installer.InstallError) as ctx:
            installer.restore_backup("evil-20260101-000000.tar.gz")
        self.assertEqual(ctx.exception.code, "invalid_backup")
        self.assertFalse(os.path.exists(os.path.join(self.data, "escape.txt")))
        self.assertFalse(os.path.exists(self.path("worlds", "evil")))

    def test_v1_archive_restores_world_only(self):
        name = "20250101T000000Z_legacy.tar.gz"
        path = self.path("backups", name)
        with tarfile.open(path, "w:gz") as archive:
            for arcname, body in (
                ("worlds/legacy/level.dat", b"old-level"),
                ("manager.json", b'{"type": "forge"}'),
                ("server.properties", b"motd=from-backup\n"),
                ("ops.json", b"[]"),
            ):
                info = tarfile.TarInfo(arcname)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        listed = {b["name"]: b for b in installer.list_backups()}
        self.assertEqual(listed[name]["world"], "legacy")
        self.assertEqual(listed[name]["created_at"], "2025-01-01T00:00:00Z")
        installer.restore_backup(name)
        with open(self.path("worlds", "legacy", "level.dat"), "rb") as handle:
            self.assertEqual(handle.read(), b"old-level")
        self.assertEqual(self.read("server.properties"), "motd=after\n")
        self.assertEqual(self.read("manager.json"), self.manager_before)


class BackupListingTests(BackupTestCase):
    def test_list_delete_and_file(self):
        name = installer.create_backup("world")["name"]
        self.write("backups/.hidden.tar.gz.tmp", b"partial")
        self.write("backups/notes.txt", "x")
        items = installer.list_backups()
        self.assertEqual([b["name"] for b in items], [name])
        self.assertEqual(items[0]["minecraft_version"], "1.21.1")
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T", items[0]["created_at"]))
        self.assertEqual(installer.backup_file(name), self.path("backups", name))
        installer.delete_backup(name)
        self.assertEqual(installer.list_backups(), [])
        with self.assertRaises(installer.InstallError):
            installer.delete_backup(name)

    def test_disk_usage(self):
        usage = installer.disk_usage()
        self.assertEqual(set(usage), {"used_bytes", "total_bytes", "free_bytes"})
        self.assertGreater(usage["total_bytes"], 0)


class WorldTests(BackupTestCase):
    def test_list_worlds(self):
        self.make_world("creative")
        cfg = installer.load_config()
        cfg["profiles"].append({"id": "c", "name": "Creative", "type": "vanilla",
                                "minecraft_version": "1.20.1", "level_name": "creative", "instance": None})
        installer.save_config(cfg)
        worlds = {w["name"]: w for w in installer.list_worlds()}
        self.assertEqual(set(worlds), {"world", "creative"})
        self.assertTrue(worlds["world"]["active"])
        self.assertEqual(worlds["world"]["last_version"], "1.21.1")
        self.assertEqual(worlds["creative"]["used_by"], ["Creative"])
        self.assertGreater(worlds["creative"]["size_bytes"], 0)

    def test_delete_world_removes_its_setups_and_refuses_active(self):
        self.make_world("creative")
        cfg = installer.load_config()
        cfg["profiles"].append({"id": "c", "name": "Creative", "type": "vanilla",
                                "minecraft_version": "1.20.1", "level_name": "creative", "instance": None})
        installer.save_config(cfg)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.delete_world("world")
        self.assertEqual(ctx.exception.code, "active_world")
        self.assertEqual(installer.delete_world("creative"), ["Creative"])
        self.assertFalse(os.path.exists(self.path("worlds", "creative")))
        self.assertEqual([n for n in os.listdir(self.path("worlds")) if n.startswith(".")], [])
        self.assertEqual([s["id"] for s in installer.list_setups()], ["default"])
        with self.assertRaises(installer.InstallError) as ctx:
            installer.delete_world("creative")
        self.assertEqual(ctx.exception.code, "world_not_found")

    def test_create_world(self):
        self.assertEqual(installer.create_world("fresh"), "fresh")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.create_world("fresh")
        self.assertEqual(ctx.exception.code, "world_exists")


if __name__ == "__main__":
    unittest.main()
