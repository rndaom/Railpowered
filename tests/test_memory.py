"""Memory defaults must fit small Railway containers and preserve overrides."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mc_host import config
from mc_host.config import (
    env_bool,
    env_int,
    load_or_create_admin_key,
    memory_bytes,
    resolve_max_memory,
    resolve_min_memory,
    resolve_web_dir,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class JavaMemoryTests(unittest.TestCase):
    def setUp(self):
        self.warnings = list(config.CONFIG_WARNINGS)
        config.CONFIG_WARNINGS.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        config.CONFIG_WARNINGS[:] = self.warnings

    def test_trial_limit_keeps_native_headroom(self):
        self.assertEqual(resolve_max_memory({}, 1024**3), "696M")

    def test_free_limit_reserves_native_headroom(self):
        self.assertEqual(resolve_max_memory({}, 512 * 1024**2), "256M")

    def test_paid_limit_does_not_reserve_the_whole_container(self):
        self.assertEqual(resolve_max_memory({}, 8 * 1024**3), "2048M")

    def test_explicit_heap_is_preserved(self):
        self.assertEqual(resolve_max_memory({"MC_MAX_MEMORY": "3G"}, 1024**3), "3G")
        self.assertEqual(resolve_max_memory({"MC_MAX_MEMORY": "1536m"}, 1024**3), "1536M")

    def test_unlimited_cgroup_uses_moderate_default(self):
        with mock.patch("mc_host.config.container_memory_limit", return_value=None):
            self.assertEqual(resolve_max_memory({}), "2G")

    def test_invalid_heap_falls_back_to_auto_with_warning(self):
        for bad in ("lots", "2GB", "-1G", "64M"):
            self.assertEqual(resolve_max_memory({"MC_MAX_MEMORY": bad}, 1024**3), "696M", bad)
        self.assertTrue(any("MC_MAX_MEMORY" in item for item in config.CONFIG_WARNINGS))

    def test_min_memory_is_clamped_to_max(self):
        self.assertEqual(resolve_min_memory("696M", {}), "256M")
        self.assertEqual(resolve_min_memory("696M", {"MC_MIN_MEMORY": "2G"}), "696M")
        self.assertEqual(resolve_min_memory("2G", {"MC_MIN_MEMORY": "1g"}), "1G")
        self.assertEqual(resolve_min_memory("2G", {"MC_MIN_MEMORY": "huge"}), "256M")
        self.assertTrue(any("MC_MIN_MEMORY" in item for item in config.CONFIG_WARNINGS))

    def test_memory_bytes(self):
        self.assertEqual(memory_bytes("512M"), 512 * 1024**2)
        self.assertEqual(memory_bytes("2g"), 2 * 1024**3)
        self.assertEqual(memory_bytes("1048576"), 1048576)
        self.assertIsNone(memory_bytes("1.5G"))
        self.assertIsNone(memory_bytes(""))


class EnvHelperTests(unittest.TestCase):
    def setUp(self):
        self.warnings = list(config.CONFIG_WARNINGS)
        config.CONFIG_WARNINGS.clear()
        self.addCleanup(lambda: config.CONFIG_WARNINGS.__setitem__(slice(None), self.warnings))

    def test_env_int_falls_back_on_bad_values(self):
        self.assertEqual(env_int("IDLE_TIMEOUT", 600, env={}), 600)
        self.assertEqual(env_int("IDLE_TIMEOUT", 600, env={"IDLE_TIMEOUT": "ten"}), 600)
        self.assertTrue(any("IDLE_TIMEOUT" in item for item in config.CONFIG_WARNINGS))
        self.assertEqual(env_int("IDLE_TIMEOUT", 600, env={"IDLE_TIMEOUT": " 120 "}), 120)
        self.assertEqual(env_int("X", 600, env={"X": "${{shared.X}}"}), 600)

    def test_env_int_clamps(self):
        self.assertEqual(env_int("S", 25, minimum=5, maximum=600, env={"S": "1"}), 5)
        self.assertEqual(env_int("S", 25, minimum=5, maximum=600, env={"S": "9999"}), 600)

    def test_env_bool(self):
        for raw in ("1", "true", "YES", "on"):
            self.assertTrue(env_bool("B", False, env={"B": raw}), raw)
        for raw in ("0", "false", "No", "off"):
            self.assertFalse(env_bool("B", True, env={"B": raw}), raw)
        self.assertTrue(env_bool("B", True, env={"B": "maybe"}))
        self.assertTrue(any('B="maybe"' in item for item in config.CONFIG_WARNINGS))

    def test_web_dir_prefers_env_then_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(resolve_web_dir({"WEB_DIR": tmp}), tmp)
        repo_web = os.path.join(ROOT, "web")
        expected = repo_web if os.path.isdir(repo_web) else None
        self.assertEqual(resolve_web_dir({"WEB_DIR": os.path.join(ROOT, "missing")}), expected)


class AdminKeyTests(unittest.TestCase):
    def test_generated_once_then_from_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            key, source = load_or_create_admin_key(tmp, {})
            self.assertEqual(source, "generated")
            self.assertGreaterEqual(len(key), 20)
            self.assertEqual(load_or_create_admin_key(tmp, {}), (key, "volume"))
            self.assertEqual(load_or_create_admin_key(tmp, {"ADMIN_KEY": " mine "}), ("mine", "env"))
            if os.name == "posix":
                mode = os.stat(os.path.join(tmp, config.ADMIN_KEY_FILE)).st_mode & 0o777
                self.assertEqual(mode, 0o600)

    def test_unevaluated_template_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, source = load_or_create_admin_key(tmp, {"ADMIN_KEY": "${{secret(32)}}"})
            self.assertEqual(source, "generated")

    def test_import_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MC_DIR=tmp, PYTHONPATH=ROOT)
            env.pop("ADMIN_KEY", None)
            result = subprocess.run(
                [sys.executable, "-c", "import mc_host.config, mc_host.panel, mc_host.process"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
