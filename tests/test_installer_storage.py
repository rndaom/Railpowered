#!/usr/bin/env python3
"""Downloaded files: list instances and jars with their users, delete unused ones."""

import os
import unittest

from test_installer_base import InstallerTestCase, installer


class StorageTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.ensure_layout()
        self.write("instances/fabric-1.20.1/libraries/x.jar", b"x" * 100)
        self.write("instances/forge-1.12.2/forge.jar", b"f" * 10)
        self.write(f"instances/mypack/{installer.PACK_META_FILE}", "{}")
        self.write("instances/.staging-mypack-abc/x", b"hidden")
        self.write("jars/minecraft_server.1.16.5.jar", b"v" * 50)
        self.write("jars/fabric-installer-1.0.1.jar", b"i" * 5)
        self.write("jars/notes.txt", "not a jar")
        os.makedirs(self.path("worlds", "world"), exist_ok=True)

        def add(cfg):
            cfg["profiles"][0]["resolved_version"] = "1.21.4"  # the default "latest" setup
            cfg["profiles"] += [
                {"id": "fab", "name": "Fabric", "type": "fabric", "minecraft_version": "1.20.1",
                 "resolved_version": "1.20.1", "level_name": "world", "instance": None},
                {"id": "pack", "name": "My pack", "type": "modpack", "minecraft_version": "1.20.1",
                 "resolved_version": "1.20.1", "level_name": "packworld", "instance": "mypack"},
                {"id": "van", "name": "Vanilla", "type": "vanilla", "minecraft_version": "1.16.5",
                 "resolved_version": "1.16.5", "level_name": "world", "instance": None},
            ]

        installer._mutate_config(add)

    def items(self):
        return {(i["kind"], i["name"]): i for i in installer.list_storage()["items"]}

    def test_lists_instances_and_jars_with_their_setups(self):
        items = self.items()
        self.assertEqual(set(items), {
            ("instance", "fabric-1.20.1"), ("instance", "forge-1.12.2"), ("instance", "mypack"),
            ("jar", "minecraft_server.1.16.5.jar"), ("jar", "fabric-installer-1.0.1.jar"),
        })
        self.assertEqual(items[("instance", "fabric-1.20.1")]["used_by"], [{"id": "fab", "name": "Fabric"}])
        self.assertEqual(items[("instance", "fabric-1.20.1")]["size"], 100)
        self.assertEqual(items[("instance", "mypack")]["type"], "modpack")
        self.assertEqual(items[("jar", "minecraft_server.1.16.5.jar")]["used_by"][0]["id"], "van")
        self.assertEqual(items[("instance", "forge-1.12.2")]["used_by"], [])
        self.assertEqual(installer.list_storage()["total"], sum(i["size"] for i in items.values()))

    def test_deletes_only_unused_items(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.delete_storage("instance", "mypack")
        self.assertEqual(ctx.exception.code, "in_use")
        self.assertEqual(ctx.exception.data["used_by"][0]["id"], "pack")
        result = installer.delete_storage("instance", "forge-1.12.2")
        self.assertEqual(result["freed"], 10)
        self.assertFalse(os.path.exists(self.path("instances", "forge-1.12.2")))
        installer.delete_storage("jar", "fabric-installer-1.0.1.jar")
        self.assertNotIn(("jar", "fabric-installer-1.0.1.jar"), self.items())
        self.assertEqual(
            sorted(n for n in os.listdir(self.path("instances")) if n.startswith(".trash")), []
        )

    def test_refuses_bad_names_and_kinds(self):
        for kind, name, code in (
            ("instance", "..", "invalid_name"),
            ("instance", ".staging-mypack-abc", "storage_not_found"),
            ("jar", "notes.txt", "storage_not_found"),
            ("instance", "missing", "storage_not_found"),
            ("world", "world", "invalid_request"),
        ):
            with self.subTest(name=name), self.assertRaises(installer.InstallError) as ctx:
                installer.delete_storage(kind, name)
            self.assertEqual(ctx.exception.code, code)

    def test_instance_worlds_link_is_not_followed(self):
        self.write("worlds/keep/level.dat", b"L")
        try:
            os.symlink(self.path("worlds"), self.path("instances", "forge-1.12.2", "worlds"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        installer.delete_storage("instance", "forge-1.12.2")
        self.assertTrue(os.path.isfile(self.path("worlds", "keep", "level.dat")))

    def test_delete_setup_returns_what_nothing_uses_now(self):
        orphaned = installer.delete_setup("pack")
        self.assertEqual([(i["kind"], i["name"]) for i in orphaned], [("instance", "mypack")])
        self.assertEqual(orphaned[0]["used_by"], [])
        orphaned = installer.delete_setup("van")
        self.assertEqual([(i["kind"], i["name"]) for i in orphaned], [("jar", "minecraft_server.1.16.5.jar")])

        def add(cfg):
            cfg["profiles"].append(
                {"id": "fab2", "name": "Fabric again", "type": "fabric", "minecraft_version": "1.20.1",
                 "resolved_version": "1.20.1", "level_name": "other", "instance": None})
        installer._mutate_config(add)
        self.assertEqual(installer.delete_setup("fab"), [])  # fab2 still uses fabric-1.20.1


if __name__ == "__main__":
    unittest.main()
