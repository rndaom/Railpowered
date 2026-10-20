"""start.sh must seed a fresh volume and never move or delete user data.

The script runs for real against a temporary data dir (MC_DIR) and a fake
image dir (RAILPOWERED_APP_DIR) whose manager.py is a stub that records it ran.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_SH = os.path.join(ROOT, "start.sh")
BASH = shutil.which("bash")

STUB_MANAGER = """\
import os, sys
with open(os.environ["STUB_OUT"], "w", encoding="utf-8") as handle:
    handle.write("ran\\n")
"""


def snapshot(root: str) -> dict[str, str]:
    """Map every file and directory under root to a content hash."""
    result: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            result[os.path.relpath(os.path.join(dirpath, name), root)] = "<dir>"
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            result[os.path.relpath(path, root)] = digest
    return result


@unittest.skipUnless(os.name == "posix" and BASH, "start.sh tests need a POSIX shell")
class StartScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = os.path.join(tmp.name, "data")
        self.app = os.path.join(tmp.name, "app")
        self.out = os.path.join(tmp.name, "stub-ran")
        os.makedirs(self.app)
        with open(os.path.join(self.app, "server.properties"), "w", encoding="utf-8") as handle:
            handle.write("motd=Seeded default\n")
        with open(os.path.join(self.app, "manager.py"), "w", encoding="utf-8") as handle:
            handle.write(STUB_MANAGER)

    def run_start(self) -> subprocess.CompletedProcess[str]:
        if os.path.exists(self.out):
            os.remove(self.out)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MC_", "RAILPOWERED_", "RAILWAY_"))
        }
        env.update(
            MC_DIR=self.data,
            RAILPOWERED_APP_DIR=self.app,
            # A user that cannot exist, so a root CI runner skips setpriv.
            RAILPOWERED_USER="railpowered-test-no-such-user",
            STUB_OUT=self.out,
        )
        bin_dir = os.path.dirname(sys.executable)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            [BASH, START_SH],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(self.out), "start.sh did not start manager.py")
        return result

    def test_fresh_volume_is_seeded(self) -> None:
        result = self.run_start()
        with open(os.path.join(self.data, "server.properties"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "motd=Seeded default\n")
        with open(os.path.join(self.data, "eula.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "eula=true\n")
        # manager.json is created by the installer so first-deploy env vars apply.
        self.assertFalse(os.path.exists(os.path.join(self.data, "manager.json")))
        self.assertEqual(sorted(os.listdir(self.data)), ["eula.txt", "server.properties"])
        self.assertIn("https://www.minecraft.net/eula", result.stdout)

    def test_existing_volume_is_never_moved_or_overwritten(self) -> None:
        files = {
            "manager.json": '{"profiles": [{"id": "mine"}]}\n',
            "server.properties": "difficulty=hard\nlevel-seed=12345\n",
            "worlds/world/level.dat": "precious",
            "worlds/creative/level.dat": "also precious",
            "backups/world-20260101-000000.tar.gz": "backup",
            "instances/pack/mods/a.jar": "mod",
            "ops.json": '[{"name": "Steve"}]\n',
            "whitelist.json": "[]\n",
            ".admin-key": "secret\n",
            # Leftovers from old images that used to trigger an archive move.
            "world/level.dat": "imported from another image",
            "poseidon.yml": "legacy",
            ".campfire-v1": "",
            "archive/pre-railpowered/20260101T000000Z/manager.json": "{}\n",
        }
        for rel, content in files.items():
            path = os.path.join(self.data, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        before = snapshot(self.data)

        first = self.run_start()
        self.run_start()

        after = snapshot(self.data)
        self.assertIn("eula.txt", after)
        del after["eula.txt"]
        self.assertEqual(before, after)
        self.assertIn("Found a world at", first.stdout)
        self.assertIn("left untouched", first.stdout)

    def test_second_boot_keeps_edited_properties(self) -> None:
        self.run_start()
        path = os.path.join(self.data, "server.properties")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("motd=Edited by the owner\n")
        self.run_start()
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "motd=Edited by the owner\n")


if __name__ == "__main__":
    unittest.main()
