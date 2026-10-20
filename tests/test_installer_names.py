#!/usr/bin/env python3
"""Name validation and path safety (dot names must never reach the volume root)."""

import os
import unittest

from test_installer_base import InstallerTestCase, can_symlink, installer


class NameRuleTests(unittest.TestCase):
    def test_accepts_ordinary_names(self):
        for name in ("world", "vanilla-1_2_5", "My.World", "a", "A" * 32, "1.20.1"):
            with self.subTest(name=name):
                self.assertTrue(installer.NAME_RE.match(name))

    def test_rejects_dot_and_unsafe_names(self):
        for name in ("", ".", "..", ".hidden", "-dash", "_under", "../etc", "a/b", "a\\b",
                     "has space", "A" * 33, "c:x", "x\0y"):
            with self.subTest(name=name):
                self.assertIsNone(installer.NAME_RE.match(name))

    def test_world_name_alias(self):
        self.assertIs(installer.WORLD_NAME_RE, installer.NAME_RE)

    def test_validate_name_error_is_plain_english(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.validate_name("..", "world")
        self.assertEqual(ctx.exception.code, "invalid_name")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("letters, numbers", str(ctx.exception))


class SafeChildTests(InstallerTestCase):
    def test_rejects_parent_and_self(self):
        parent = self.path("worlds")
        os.makedirs(parent)
        for name in ("", ".", "..", "a/b", "a\\b", "x\0y", "c:evil", os.path.abspath(self.tmp)):
            with self.subTest(name=name), self.assertRaises(installer.InstallError):
                installer._safe_child(parent, name)

    def test_accepts_plain_child(self):
        parent = self.path("worlds")
        os.makedirs(parent)
        self.assertEqual(installer._safe_child(parent, "world"), os.path.join(parent, "world"))

    @unittest.skipUnless(can_symlink(), "needs symlink privilege")
    def test_rejects_symlink_escaping_parent(self):
        parent = self.path("worlds")
        os.makedirs(parent)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(parent, "evil"), target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            installer._safe_child(parent, "evil")


class DotNamesNeverTouchTheVolumeTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.load_config()
        self.make_world("world")
        self.write("server.properties", "motd=keep\n")

    def assert_volume_intact(self):
        self.assertTrue(os.path.isfile(self.path("manager.json")))
        self.assertTrue(os.path.isfile(self.path("server.properties")))
        self.assertTrue(os.path.isfile(self.path("worlds", "world", "level.dat")))

    def test_world_operations_refuse_dot_names(self):
        for name in (".", "..", "../..", ""):
            with self.subTest(name=name):
                for call in (installer.world_path, installer.create_world, installer.delete_world):
                    with self.assertRaises(installer.InstallError):
                        call(name)
        self.assert_volume_intact()

    def test_setup_and_backup_operations_refuse_dot_names(self):
        with self.assertRaises(installer.InstallError):
            installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "."})
        with self.assertRaises(installer.InstallError):
            installer.create_backup("..")
        backup = installer.create_backup("world")
        with self.assertRaises(installer.InstallError):
            installer.restore_backup(backup["name"], target="..")
        for bad in ("../manager.json", "..", ".", "x.tar.gz/..", "manager.json"):
            with self.subTest(name=bad), self.assertRaises(installer.InstallError) as ctx:
                installer.backup_file(bad)
            self.assertEqual(ctx.exception.code, "backup_not_found")
        self.assert_volume_intact()

    def test_modpack_names_refuse_dots_and_reserved_prefixes(self):
        archive = self.make_zip({"server.jar": b"x"})
        for name in (".", "..", "fabric-1.20.1", "Forge-x"):
            with self.subTest(name=name), self.assertRaises(installer.InstallError) as ctx:
                installer.install_modpack_archive(archive, name)
            self.assertEqual(ctx.exception.code, "invalid_name")
        self.assertEqual(os.listdir(self.path("instances")), [])
        self.assert_volume_intact()

    def test_list_worlds_hides_invalid_folder_names(self):
        os.makedirs(self.path("worlds", ".restore-abc"))
        os.makedirs(self.path("worlds", "has space"))
        self.assertEqual([w["name"] for w in installer.list_worlds()], ["world"])


if __name__ == "__main__":
    unittest.main()
