#!/usr/bin/env python3
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import installer
from manager import SleepProxy


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


if __name__ == "__main__":
    unittest.main()
