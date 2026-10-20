#!/usr/bin/env python3
"""recover_interrupted: what a redeploy cut short is put back or swept at boot."""

import os
import time
import unittest
from unittest import mock

from test_installer_base import InstallerTestCase, installer


class RecoveryTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.ensure_layout()

    def listing(self, *parts):
        return sorted(os.listdir(self.path(*parts)))

    def test_interrupted_pack_swap_puts_the_old_instance_back(self):
        self.write("instances/.old-my-pack-0a1b2c3d/mods/a.jar", b"jar")
        result = installer.recover_interrupted()
        self.assertTrue(os.path.isfile(self.path("instances", "my-pack", "mods", "a.jar")))
        self.assertEqual(self.listing("instances"), ["my-pack"])
        self.assertEqual(len(result["recovered"]), 1)

    def test_finished_pack_swap_drops_the_old_copy_but_rescues_worlds(self):
        self.write("instances/mypack/mods/new.jar", b"new")
        self.write("instances/.old-mypack-0a1b2c3d/mods/old.jar", b"old")
        self.write("instances/.old-mypack-0a1b2c3d/worlds/legacy/level.dat", b"L")
        installer.recover_interrupted()
        self.assertEqual(self.listing("instances"), ["mypack"])
        self.assertTrue(os.path.isfile(self.path("worlds", "legacy", "level.dat")))

    def test_staging_folders_are_removed(self):
        for rel in (
            "instances/.staging-p-abc/mods/x.jar",
            "instances/.unpack-p-abc/modrinth.index.json",
            "instances/.trash-p-0a1b2c3d/x",
            "worlds/.trash-old-0a1b2c3d/level.dat",
        ):
            self.write(rel, b"x" * 10)
        result = installer.recover_interrupted()
        self.assertEqual(self.listing("instances"), [])
        self.assertEqual(self.listing("worlds"), [])
        self.assertEqual(result["removed"], 4)
        self.assertEqual(result["bytes"], 40)

    def test_restore_cut_short_after_moving_the_world_aside(self):
        self.write("worlds/.restore-abc/TARGET", "survival")
        self.write("worlds/.restore-abc/old/level.dat", b"original")
        self.write("worlds/.restore-abc/world/level.dat", b"half restored")
        installer.recover_interrupted()
        self.assertEqual(self.listing("worlds"), ["survival"])
        with open(self.path("worlds", "survival", "level.dat"), "rb") as handle:
            self.assertEqual(handle.read(), b"original")

    def test_restore_that_finished_drops_the_replaced_copy(self):
        self.make_world("survival")
        self.write("worlds/.restore-abc/TARGET", "survival")
        self.write("worlds/.restore-abc/old/level.dat", b"replaced (backed up as pre-restore)")
        installer.recover_interrupted()
        self.assertEqual(self.listing("worlds"), ["survival"])

    def test_restore_without_a_target_keeps_the_world(self):
        self.write("worlds/.restore-abc/old/level.dat", b"original")
        result = installer.recover_interrupted()
        names = self.listing("worlds")
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^world-recovered-\d{8}$")
        self.assertTrue(installer.NAME_RE.match(names[0]))
        self.assertIn(names[0], result["recovered"][0])

    def test_temp_files_and_uploads_are_removed(self):
        self.make_world("survival")
        keep = [
            "backups/survival-20260101-000000.tar.gz",
            "modpacks/.held-" + "a" * 32 + ".zip",
            "manager.json",
            ".uuid-cache.json",
            "jars/vanilla-1.21.1.jar",
        ]
        drop = [
            "backups/.survival-20260101-000000.tar.gz.tmp",
            "modpacks/.download-x1y2z3.pack",
            "modpacks/.upload-0a1b2c3d4e5f.part",
            "modpacks/.mypack-0a1b2c3d.upload.mrpack",
            "modpacks/mypack.upload.zip",
            ".manager.json.abc12345.tmp",
            "jars/.vanilla-1.21.1.jar.x_y12345.part",
            "worlds/survival/.railpowered.json.abcdefgh.tmp",
        ]
        for rel in keep + drop:
            self.write(rel, b"x")
        expired = self.path("modpacks", ".held-" + "b" * 32 + ".mrpack")
        self.write("modpacks/.held-" + "b" * 32 + ".mrpack", b"x")
        old = time.time() - installer.HELD_UPLOAD_TTL - 10
        os.utime(expired, (old, old))
        result = installer.recover_interrupted()
        for rel in keep:
            self.assertTrue(os.path.exists(self.path(*rel.split("/"))), rel)
        for rel in drop:
            self.assertFalse(os.path.exists(self.path(*rel.split("/"))), rel)
        self.assertFalse(os.path.exists(expired))
        self.assertEqual(result["removed"], len(drop) + 1)

    def test_nothing_to_do_is_quiet(self):
        self.make_world("survival")
        self.assertEqual(installer.recover_interrupted(), {"recovered": [], "removed": 0, "bytes": 0})


class RestoreMarkerTests(InstallerTestCase):
    def test_restore_staging_names_its_target(self):
        installer.ensure_layout()
        self.make_world("survival")
        backup = installer.create_backup("survival")["name"]
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            parent = os.path.dirname(src)
            if os.path.basename(parent).startswith(".restore-") and os.path.basename(src) == "world":
                with open(os.path.join(parent, installer.RESTORE_TARGET_FILE), encoding="utf-8") as handle:
                    seen["target"] = handle.read()
            return real_replace(src, dst)

        with mock.patch.object(installer.os, "replace", side_effect=spy):
            installer.restore_backup(backup, target="copy")
        self.assertEqual(seen["target"], "copy")
        self.assertEqual(sorted(n for n in os.listdir(self.path("worlds")) if n.startswith(".")), [])


if __name__ == "__main__":
    unittest.main()
