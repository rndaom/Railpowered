"""Memory defaults must fit small Railway containers and preserve overrides."""

import unittest
from unittest import mock

from mc_host.config import resolve_max_memory


class JavaMemoryTests(unittest.TestCase):
    def test_trial_limit_keeps_native_headroom(self):
        self.assertEqual(resolve_max_memory({}, 1024**3), "696M")

    def test_free_limit_reserves_native_headroom(self):
        self.assertEqual(resolve_max_memory({}, 512 * 1024**2), "256M")

    def test_paid_limit_does_not_reserve_the_whole_container(self):
        self.assertEqual(resolve_max_memory({}, 8 * 1024**3), "2048M")

    def test_explicit_heap_is_preserved(self):
        self.assertEqual(resolve_max_memory({"MC_MAX_MEMORY": "3G"}, 1024**3), "3G")

    def test_unlimited_cgroup_uses_moderate_default(self):
        with mock.patch("mc_host.config.container_memory_limit", return_value=None):
            self.assertEqual(resolve_max_memory({}), "2G")


if __name__ == "__main__":
    unittest.main()
