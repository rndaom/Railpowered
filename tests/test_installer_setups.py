#!/usr/bin/env python3
"""Setups: transactional create/activate, downgrade guard, runtime spec."""

import os
import unittest
from unittest import mock

from test_installer_base import InstallerTestCase, can_symlink, installer


class SetupTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        self.vanilla_jar("26.3")
        self.vanilla_jar("1.21.1")
        self.vanilla_jar("1.20.1")
        installer.load_config()

    def active(self):
        cfg = installer.load_config()
        return next(s for s in cfg["profiles"] if s["id"] == cfg["active_profile"])


class CreateSetupTests(SetupTestCase):
    def test_create_new_world_setup_prepares_and_activates(self):
        spec = installer.create_setup(
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "creative",
             "create_world": True}
        )
        self.assertEqual((spec.version, spec.type, spec.level_name), ("1.20.1", "vanilla", "creative"))
        active = self.active()
        self.assertEqual(active["minecraft_version"], "1.20.1")
        self.assertEqual(active["resolved_version"], "1.20.1")
        self.assertEqual(active["name"], "Vanilla 1.20.1")
        self.assertTrue(os.path.isdir(self.path("worlds", "creative")))
        self.assertIn("level-name=worlds/creative", self.read("server.properties"))
        self.assertIn("eula=true", self.read("eula.txt"))
        self.assertIn("https://www.minecraft.net/eula", self.read("eula.txt"))
        self.assertEqual(len(installer.list_setups()), 2)

    def test_failed_prepare_keeps_old_setup_and_removes_new_world(self):
        before = self.read_json("manager.json")
        with mock.patch.object(
            installer, "_install_runtime",
            side_effect=installer.InstallError("Download failed", code="download_failed"),
        ):
            with self.assertRaises(installer.InstallError):
                installer.create_setup(
                    {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "fresh"}
                )
        self.assertEqual(self.read_json("manager.json"), before)
        self.assertFalse(os.path.exists(self.path("worlds", "fresh")))
        self.assertEqual(self.active()["id"], "default")

    def test_failed_prepare_keeps_existing_world(self):
        self.make_world("kept")
        with mock.patch.object(installer, "_install_runtime", side_effect=installer.InstallError("x")):
            with self.assertRaises(installer.InstallError):
                installer.create_setup(
                    {"type": "vanilla", "minecraft_version": "1.21.1", "level_name": "kept"}
                )
        self.assertTrue(os.path.isfile(self.path("worlds", "kept", "level.dat")))

    def test_create_world_flag_is_enforced(self):
        self.make_world("taken")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.create_setup(
                {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "taken", "create_world": True}
            )
        self.assertEqual(ctx.exception.code, "world_exists")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.create_setup(
                {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "nope", "create_world": False}
            )
        self.assertEqual(ctx.exception.code, "world_not_found")

    def test_identical_setup_is_reused(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "a"})
        first = self.active()["id"]
        installer.activate_setup("default")
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "a"})
        self.assertEqual(self.active()["id"], first)
        self.assertEqual(len(installer.list_setups()), 2)

    def test_create_without_activation(self):
        spec = installer.create_setup(
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "later", "name": "Later"},
            activate=False,
        )
        self.assertEqual(spec.version, "1.20.1")
        self.assertEqual(self.active()["id"], "default")
        names = [s["name"] for s in installer.list_setups()]
        self.assertIn("Later", names)

    def test_invalid_fields(self):
        cases = [
            ({"type": "paper", "minecraft_version": "1.20.1"}, "invalid_type"),
            ({"type": "vanilla", "minecraft_version": "1.20.1; rm"}, "invalid_version"),
            ({"type": "forge", "minecraft_version": "1.20.1"}, "invalid_version"),
            ({"type": "forge", "minecraft_version": "latest"}, "invalid_version"),
            ({"type": "modpack", "minecraft_version": "1.20.1", "instance": "missing"}, "instance_not_found"),
            ({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "a b"}, "invalid_name"),
        ]
        for fields, code in cases:
            with self.subTest(fields=fields), self.assertRaises(installer.InstallError) as ctx:
                installer.create_setup(fields)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(len(installer.list_setups()), 1)


class ActivateUpdateDeleteTests(SetupTestCase):
    def setUp(self):
        super().setUp()
        installer.create_setup(
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "other"}, activate=False
        )
        self.other = next(s["id"] for s in installer.list_setups() if s["level_name"] == "other")

    def test_activate_switches(self):
        spec = installer.activate_setup(self.other)
        self.assertEqual(spec.level_name, "other")
        self.assertEqual(self.active()["id"], self.other)
        self.assertEqual(installer.load_config()["level_name"], "other")

    def test_failed_activation_keeps_previous_setup(self):
        with mock.patch.object(installer, "_install_runtime", side_effect=installer.InstallError("x")):
            with self.assertRaises(installer.InstallError):
                installer.activate_setup(self.other)
        self.assertEqual(self.active()["id"], "default")

    def test_activate_unknown(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.activate_setup("nope")
        self.assertEqual(ctx.exception.code, "setup_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_update_renames_and_moves_world(self):
        result = installer.update_setup(self.other, name="  Creative  ", level_name="moved")
        self.assertEqual(result["name"], "Creative")
        self.assertEqual(result["level_name"], "moved")
        self.assertTrue(os.path.isdir(self.path("worlds", "moved")))
        with self.assertRaises(installer.InstallError):
            installer.update_setup(self.other, name="   ")

    def test_delete_refuses_active_setup(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.delete_setup("default")
        self.assertEqual(ctx.exception.code, "active_setup")
        installer.delete_setup(self.other)
        self.assertEqual([s["id"] for s in installer.list_setups()], ["default"])
        self.assertTrue(os.path.isdir(self.path("worlds", "other")))  # worlds are kept


class ListingTests(SetupTestCase):
    def test_list_setups_needs_no_network(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "creative"})
        self.net.calls.clear()
        items = installer.list_setups()
        self.assertEqual(self.net.calls, [])
        by_level = {item["level_name"]: item for item in items}
        self.assertEqual(by_level["creative"]["summary"], "Vanilla 1.20.1 · World: creative")
        self.assertTrue(by_level["creative"]["active"])
        default = by_level["world"]
        self.assertEqual(default["name"], "Latest vanilla")
        self.assertEqual(default["minecraft_version"], "latest")
        for key in ("id", "name", "type", "minecraft_version", "resolved_version", "level_name",
                    "instance", "active", "summary"):
            self.assertIn(key, default)

    def test_latest_is_resolved_but_never_pinned(self):
        spec = installer.prepare_runtime()
        self.assertEqual(spec.version, "26.3")  # newest jar on the volume while offline
        setup = self.active()
        self.assertEqual(setup["minecraft_version"], "latest")
        self.assertEqual(setup["resolved_version"], "26.3")
        self.assertEqual(installer.list_setups()[0]["resolved_version"], "26.3")


class DowngradeTests(SetupTestCase):
    def setUp(self):
        super().setUp()
        self.make_world("old", last_version="1.21.1")

    def test_downgrade_is_refused_by_default(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "old"})
        self.assertEqual(ctx.exception.code, "world_downgrade")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("1.21.1", str(ctx.exception))
        self.assertEqual(os.listdir(self.path("backups")), [])
        self.assertEqual(len(installer.list_setups()), 1)

    def test_allowed_downgrade_takes_backup_and_never_lowers_the_guard(self):
        installer.create_setup(
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "old"}, allow_downgrade=True
        )
        backups = installer.list_backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["world"], "old")
        self.assertEqual(backups[0]["kind"], "pre-downgrade")
        meta = installer.world_meta("old")
        self.assertEqual(meta["last_version"], "1.21.1")  # 1.20.1 hasn't run yet
        self.assertEqual(meta["newest_version"], "1.21.1")
        self.assertEqual(meta["downgrade_ack"]["version"], "1.20.1")
        self.assertEqual(meta["downgrade_ack"]["from"], "1.21.1")
        self.assertEqual(meta["downgrade_ack"]["backup"], backups[0]["name"])
        # The confirmed version may retry without asking again (e.g. after a
        # crash), but any other older version is still checked against 1.21.1.
        installer.check_downgrade("old", "1.20.1")
        installer.prepare_runtime()
        self.assertEqual(len(installer.list_backups()), 1)
        with self.assertRaises(installer.InstallError):
            installer.check_downgrade("old", "1.20.4")

    def test_confirmed_version_that_ran_stays_accepted(self):
        installer.create_setup(
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "old"}, allow_downgrade=True
        )
        installer.record_world_version("old", "1.20.1", "vanilla")  # it reached "online"
        meta = installer.world_meta("old")
        self.assertEqual((meta["last_version"], meta["newest_version"]), ("1.20.1", "1.21.1"))
        self.assertNotIn("downgrade_ack", meta)
        self.assertEqual(meta["accepted_downgrades"], ["1.20.1"])
        installer.check_downgrade("old", "1.20.1")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.check_downgrade("old", "1.19.2")
        self.assertEqual(ctx.exception.data["last_version"], "1.21.1")
        # A newer version opening it starts the history over.
        installer.record_world_version("old", "26.3", "vanilla")
        meta = installer.world_meta("old")
        self.assertEqual(meta["newest_version"], "26.3")
        self.assertNotIn("accepted_downgrades", meta)
        with self.assertRaises(installer.InstallError):
            installer.check_downgrade("old", "1.20.1")

    def test_failed_downgrade_install_takes_no_backup_and_confirms_nothing(self):
        with mock.patch.object(installer, "_install_runtime",
                               side_effect=installer.InstallError("Java 8 is missing", code="java_missing")):
            for _ in range(3):
                with self.assertRaises(installer.InstallError):
                    installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1",
                                            "level_name": "old"}, allow_downgrade=True)
        self.assertEqual(installer.list_backups(), [])
        self.assertNotIn("downgrade_ack", installer.world_meta("old"))

    def test_retry_reuses_the_backup_while_the_world_is_unchanged(self):
        first = installer._safety_backup("old", "pre-downgrade")
        self.assertEqual(installer._safety_backup("old", "pre-downgrade"), first)
        later = os.path.getmtime(self.path("backups", first)) + 5
        os.utime(self.path("worlds", "old", "level.dat"), (later, later))
        self.assertNotEqual(installer._safety_backup("old", "pre-downgrade"), first)

    def test_inactive_setup_only_validates(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "old"},
                               allow_downgrade=True, activate=False)
        cfg = installer.load_config()
        setup = next(s for s in cfg["profiles"] if s["level_name"] == "old")
        installer.update_setup(setup["id"], level_name="old")  # no change
        self.make_world("other", last_version="1.21.1")
        with self.assertRaises(installer.InstallError):
            installer.update_setup(setup["id"], level_name="other")
        installer.update_setup(setup["id"], level_name="other", allow_downgrade=True)
        self.assertEqual(installer.list_backups(), [])
        for world in ("old", "other"):
            meta = installer.world_meta(world)
            self.assertEqual(meta["last_version"], "1.21.1")
            self.assertNotIn("downgrade_ack", meta)
        with self.assertRaises(installer.InstallError):
            installer.check_setup_downgrade(setup["id"])
        installer.activate_setup(setup["id"], allow_downgrade=True)
        self.assertEqual(installer.world_meta("other")["downgrade_ack"]["version"], "1.20.1")
        self.assertEqual([b["world"] for b in installer.list_backups()], ["other"])

    def test_sidecars_from_before_newest_version_still_guard(self):
        directory = self.make_world("legacy")
        with open(os.path.join(directory, installer.WORLD_META_FILE), "w", encoding="utf-8") as handle:
            handle.write('{"last_version": "1.20.1", "downgraded_from": "1.21.1"}')
        with self.assertRaises(installer.InstallError):
            installer.check_downgrade("legacy", "1.20.4")

    def test_upgrade_takes_a_backup_once(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.21.1", "level_name": "old"})
        self.assertEqual(installer.list_backups(), [])  # same version
        installer.create_setup({"type": "vanilla", "minecraft_version": "26.3", "level_name": "old"})
        backups = installer.list_backups()
        self.assertEqual([(b["world"], b["kind"]) for b in backups], [("old", "pre-upgrade")])
        installer.prepare_runtime()  # e.g. the first start crashed; the world is unchanged
        self.assertEqual(len(installer.list_backups()), 1)
        installer.record_world_version("old", "26.3", "vanilla")
        installer.prepare_runtime()
        self.assertEqual(len(installer.list_backups()), 1)

    def test_failed_upgrade_backup_only_warns(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.21.1", "level_name": "old"})
        cfg = installer.load_config()
        cfg["profiles"][-1]["minecraft_version"] = "26.3"
        installer.save_config(cfg)
        with mock.patch.object(installer, "create_backup",
                               side_effect=installer.InstallError("No space", code="disk_full")):
            spec = installer.prepare_runtime()
        self.assertEqual(spec.version, "26.3")
        self.assertTrue(any("without a backup" in w for w in spec.warnings))

    def test_start_with_confirmation_backs_up_and_records_it(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.21.1", "level_name": "old"})
        installer.record_world_version("old", "26.3", "vanilla")  # e.g. a newer backup was restored
        with self.assertRaises(installer.InstallError):
            installer.prepare_runtime()
        spec = installer.prepare_runtime(allow_downgrade=True)
        self.assertEqual(spec.downgrade["from"], "26.3")
        self.assertEqual(installer.world_meta("old")["downgrade_ack"]["version"], "1.21.1")
        self.assertEqual([b["kind"] for b in installer.list_backups()], ["pre-downgrade"])
        installer.prepare_runtime()  # confirmed: the next start doesn't ask again

    def test_upgrade_and_same_version_are_fine(self):
        installer.check_downgrade("old", "1.21.1")
        installer.check_downgrade("old", "26.3")
        installer.check_downgrade("brand-new", "1.2.5")
        with self.assertRaises(installer.InstallError):
            installer.check_downgrade("old", "1.21")

    def test_prepare_runtime_guards_active_world(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.21.1", "level_name": "old"})
        installer.record_world_version("old", "26.3", "vanilla")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.prepare_runtime()
        self.assertEqual(ctx.exception.code, "world_downgrade")

    def test_record_world_version_keeps_newest(self):
        installer.record_world_version("old", "1.20.1", "vanilla")
        meta = installer.world_meta("old")
        self.assertEqual(meta["newest_version"], "1.21.1")
        self.assertEqual(meta["last_version"], "1.20.1")
        self.assertTrue(meta["last_played"])
        installer.record_world_version("old", "26.3", "fabric")
        self.assertEqual(installer.world_meta("old")["newest_version"], "26.3")
        self.assertEqual(installer.world_meta("old")["last_version"], "26.3")
        self.assertEqual(installer.world_meta("old")["last_type"], "fabric")
        installer.record_world_version("..", "26.3")  # ignored, no exception
        installer.record_world_version("old", "not a version")
        self.assertEqual(installer.world_meta("old")["last_version"], "26.3")
        listed = {w["name"]: w for w in installer.list_worlds()}
        self.assertEqual(listed["old"]["newest_version"], "26.3")


class RuntimeSpecTests(SetupTestCase):
    def test_spec_fields_and_command(self):
        installer.create_setup({"type": "vanilla", "minecraft_version": "1.21.1", "level_name": "w"})
        spec = installer.prepare_runtime()
        java21 = os.path.join(self.java_homes[21], "bin", "java")
        self.assertEqual(spec.java_bin, java21)
        self.assertEqual(spec.java_major, 21)
        self.assertEqual(spec.env, {"JAVA_HOME": self.java_homes[21]})
        self.assertEqual(spec.cwd, self.data)
        command = spec.build_command(["-Xmx1G"])
        self.assertEqual(command[:2], [java21, "-Xmx1G"])
        self.assertEqual(command[-3:], ["-jar", self.path("jars", "minecraft_server.1.21.1.jar"), "nogui"])
        self.assertEqual(spec.command[0], java21)
        for attr in ("command", "cwd", "java_major", "version", "type", "level_name", "env"):
            self.assertTrue(hasattr(spec, attr))

    def test_missing_java_is_a_clear_error(self):
        installer.JAVA_HOMES.clear()
        with self.assertRaises(installer.InstallError) as ctx:
            installer.create_setup({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "w"})
        self.assertEqual(ctx.exception.code, "java_missing")
        self.assertIn("Java 17", str(ctx.exception))


class FabricSetupTests(SetupTestCase):
    def fake_install(self, version, dest=None, loader=None):
        dest = dest or installer.instance_dir("fabric", version)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "fabric-server-launch.jar"), "wb") as handle:
            handle.write(b"launch")
        return dest

    def test_fabric_setup_runs_in_instance_with_shared_world_and_properties(self):
        self.write("server.properties", "motd=Mine\ncustom=1\n")
        with mock.patch.object(installer, "install_fabric", side_effect=self.fake_install), \
                mock.patch.object(installer, "_relink_instance_world") as relink:
            spec = installer.create_setup(
                {"type": "fabric", "minecraft_version": "1.20.1", "level_name": "modded"}
            )
        run_dir = self.path("instances", "fabric-1.20.1")
        self.assertEqual(spec.cwd, run_dir)
        self.assertEqual(spec.jar, os.path.join(run_dir, "fabric-server-launch.jar"))
        relink.assert_called_once_with(run_dir, "modded")
        with open(os.path.join(run_dir, "server.properties"), encoding="latin-1") as handle:
            copied = handle.read()
        self.assertIn("motd=Mine", copied)
        self.assertIn("custom=1", copied)
        self.assertIn("level-name=worlds/modded", copied)
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "eula.txt")))


class WorldLinkTests(SetupTestCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.path("instances", "fabric-1.20.1")
        os.makedirs(self.run_dir)

    @unittest.skipUnless(can_symlink(), "needs symlink privilege")
    def test_instance_worlds_link_points_at_shared_worlds(self):
        installer._relink_instance_world(self.run_dir, "modern")
        link = os.path.join(self.run_dir, "worlds")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.path("worlds")))
        self.assertTrue(os.path.isdir(self.path("worlds", "modern")))
        installer._relink_instance_world(self.run_dir, "modern")  # idempotent
        self.assertTrue(os.path.islink(link))

    @unittest.skipUnless(can_symlink(), "needs symlink privilege")
    def test_instance_local_worlds_are_rescued_not_deleted(self):
        self.make_world("stuck")
        local = os.path.join(self.run_dir, "worlds")
        os.makedirs(os.path.join(local, "stuck"))
        os.makedirs(os.path.join(local, "solo"))
        with open(os.path.join(local, "stuck", "level.dat"), "wb") as handle:
            handle.write(b"instance copy")
        with open(os.path.join(local, "solo", "level.dat"), "wb") as handle:
            handle.write(b"solo")
        installer._relink_instance_world(self.run_dir, "stuck")
        self.assertTrue(os.path.islink(local))
        with open(self.path("worlds", "stuck", "level.dat"), "rb") as handle:
            self.assertEqual(handle.read(), b"level-stuck")  # shared copy untouched
        self.assertTrue(os.path.isfile(self.path("worlds", "solo", "level.dat")))
        renamed = [n for n in os.listdir(self.path("worlds")) if n.startswith("stuck-")]
        self.assertEqual(len(renamed), 1)

    def test_no_symlink_support_is_a_clear_error(self):
        with mock.patch.object(installer.os, "symlink", side_effect=OSError(1314, "privilege not held")):
            with self.assertRaises(installer.InstallError) as ctx:
                installer._relink_instance_world(self.run_dir, "modern")
        self.assertEqual(ctx.exception.code, "install_failed")
        self.assertIn("symlink", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
