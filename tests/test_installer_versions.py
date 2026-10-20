#!/usr/bin/env python3
"""Versions, 'latest' with offline fallback, Java selection, downloads, Log4Shell."""

import json
import os
import time
import unittest
import urllib.error
from unittest import mock

from test_installer_base import InstallerTestCase, installer, sha1


class VersionParsingTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(installer.parse_version("1.2.5"), (1, 2, 5))
        self.assertEqual(installer.parse_version("1.21"), (1, 21, 0))
        self.assertEqual(installer.parse_version("26.3"), (26, 3, 0))
        self.assertEqual(installer.parse_version("1.20.5-pre1"), (1, 20, 5))
        with self.assertRaises(installer.InstallError):
            installer.parse_version("latest")

    def test_eras(self):
        self.assertTrue(installer.is_year_version("26.2"))
        self.assertTrue(installer.is_legacy_protocol("1.6.4"))
        self.assertFalse(installer.is_legacy_protocol("1.7.2"))
        self.assertFalse(installer.uses_json_lists("1.7.5"))
        self.assertTrue(installer.uses_json_lists("1.7.6"))
        self.assertTrue(installer.uses_json_lists("1.7.10"))
        self.assertTrue(installer.uses_json_lists("26.1"))

    def test_version_key_orders_prereleases_first(self):
        keys = sorted(["1.20.5", "1.20.5-pre1", "1.9", "1.20.4", "26.1"], key=installer.version_key)
        self.assertEqual(keys, ["1.9", "1.20.4", "1.20.5-pre1", "1.20.5", "26.1"])


class JavaSelectionTests(InstallerTestCase):
    def test_fallback_table(self):
        cases = {
            "1.2.5": 8, "1.12.2": 8, "1.16.5": 8, "1.17.1": 17, "1.20.4": 17,
            "1.20.5": 21, "1.21.1": 21, "26.3": 25,
        }
        for version, major in cases.items():
            with self.subTest(version=version):
                self.assertEqual(installer.required_java(version), major)

    def test_old_forge_needs_exactly_java_8(self):
        self.assertEqual(installer.required_java("1.16.5", "forge"), 8)
        self.assertEqual(installer.required_java("1.12.2", "modpack", loader="forge"), 8)
        self.assertEqual(installer.java_major_for_version("1.7.10", "forge"), 8)

    def test_mojang_java_version_wins_when_cached(self):
        os.makedirs(self.path("cache", "versions"))
        self.write("cache/versions/1.17.json", json.dumps({"id": "1.17", "java_major": 16}))
        self.assertEqual(installer.required_java("1.17"), 16)
        self.assertEqual(installer.java_major_for_version("1.17"), 21)  # smallest installed >= 16

    def test_java_major_for_common_versions(self):
        self.assertEqual(installer.java_major_for_version("1.21.1"), 21)
        self.assertEqual(installer.java_major_for_version("1.2.5"), 8)
        self.assertEqual(installer.java_major_for_version("26.3"), 25)

    def test_select_smallest_sufficient(self):
        major, exe, home = installer.select_java(17)
        self.assertEqual((major, home), (21, self.java_homes[21]))
        self.assertTrue(exe.startswith(self.java_homes[21]))

    def test_missing_java(self):
        del installer.JAVA_HOMES[8]
        with self.assertRaises(installer.InstallError) as ctx:
            installer.select_java(8, 8)
        self.assertEqual(ctx.exception.code, "java_missing")
        self.assertIn("Java 8", str(ctx.exception))

    def test_path_java_is_checked_not_assumed(self):
        installer.JAVA_HOMES.clear()
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/java"), \
                mock.patch.object(installer, "_path_java_major", return_value=17):
            self.assertEqual(installer.select_java(17)[0], 17)
            with self.assertRaises(installer.InstallError):
                installer.select_java(21)


class LatestTests(InstallerTestCase):
    def test_latest_from_mojang_and_cached_on_disk(self):
        self.manifest(latest="26.3")
        self.assertEqual(installer.resolve_minecraft_version("latest"), "26.3")
        self.assertEqual(installer.resolve_minecraft_version(""), "26.3")
        self.assertEqual(installer.resolve_minecraft_version("1.20.1"), "1.20.1")
        self.assertTrue(os.path.isfile(self.path("cache", "version_manifest.json")))
        calls = len(self.net.calls)
        installer._reset_caches()  # new process: reads the disk cache, no network
        self.assertEqual(installer.resolve_minecraft_version("latest"), "26.3")
        self.assertEqual(len(self.net.calls), calls)

    def test_offline_falls_back_to_newest_jar_on_volume(self):
        self.vanilla_jar("1.20.1")
        self.vanilla_jar("1.21.4")
        self.vanilla_jar("1.9")
        self.assertEqual(installer.resolve_minecraft_version("latest"), "1.21.4")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.resolve_minecraft_version("latest", offline_ok=False)
        self.assertEqual(ctx.exception.code, "network")

    def test_offline_with_nothing_is_a_clear_error(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.resolve_minecraft_version("latest")
        self.assertEqual(ctx.exception.code, "offline")
        self.assertEqual(ctx.exception.status, 503)

    def test_stale_manifest_used_when_offline_and_no_jar(self):
        self.manifest(latest="26.2")
        installer.resolve_minecraft_version("latest")
        self.net.routes.clear()
        with mock.patch.object(installer, "_now", return_value=time.time() + 7200):
            self.assertEqual(installer.resolve_minecraft_version("latest"), "26.2")

    def test_failures_are_not_retried_immediately(self):
        self.vanilla_jar("1.20.1")
        installer.resolve_minecraft_version("latest")
        installer.resolve_minecraft_version("latest")
        self.assertEqual(self.net.count("version_manifest"), 1)

    def test_new_release_is_picked_up_after_ttl(self):
        self.manifest(latest="26.2")
        self.assertEqual(installer.resolve_minecraft_version("latest"), "26.2")
        self.manifest(latest="26.3", releases=("26.3", "26.2"))
        with mock.patch.object(installer, "_now", return_value=time.time() + installer.MANIFEST_TTL + 5):
            self.assertEqual(installer.resolve_minecraft_version("latest"), "26.3")


class AvailableVersionsTests(InstallerTestCase):
    def test_online_list_is_sorted_and_filtered(self):
        self.manifest(latest="26.3")
        self.net.add(
            f"{installer.FABRIC_META_URL}/versions/game",
            [{"version": "1.20.1", "stable": True}, {"version": "26.3", "stable": True},
             {"version": "26.4-snapshot-1", "stable": False}],
        )
        result = installer.list_available_versions()
        ids = [v["id"] for v in result["vanilla"]]
        self.assertEqual(ids, ["26.3", "1.21.1", "1.20.1", "1.12.2", "1.7.10", "1.2.5"])
        self.assertNotIn("1.0", ids)  # no official server jar
        self.assertEqual(result["vanilla"][0]["label"], "26.3 (latest)")
        self.assertEqual([v["id"] for v in result["fabric"]], ["26.3", "1.20.1"])
        self.assertEqual(result["latest"], "26.3")
        self.assertFalse(result["snapshots"])
        self.assertFalse(result["offline"])
        self.assertEqual({f["id"] for f in result["forge"]}, set(installer.FORGE_BUILDS))

    def test_offline_list_still_works(self):
        self.vanilla_jar("1.19.2")
        result = installer.list_available_versions()
        self.assertTrue(result["offline"])
        ids = [v["id"] for v in result["vanilla"]]
        self.assertIn("1.19.2", ids)
        self.assertIn("1.21.1", ids)
        self.assertEqual(ids, sorted(ids, key=installer.version_key, reverse=True))
        self.assertTrue(result["fabric"])


class VanillaInstallTests(InstallerTestCase):
    def test_existing_jar_needs_no_network(self):
        jar = self.vanilla_jar("1.20.1")
        self.assertEqual(installer.install_vanilla("1.20.1"), jar)

    def test_download_verifies_checksum_and_caches_java(self):
        self.manifest()
        self.version_json("1.21.1", java=21)
        path = installer.install_vanilla("1.21.1")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"server-1.21.1")
        self.assertEqual(installer.required_java("1.21.1"), 21)
        self.net.routes.clear()
        installer._reset_caches()
        self.assertEqual(installer.install_vanilla("1.21.1"), path)  # verified stamp, no network

    def test_corrupt_download_leaves_nothing(self):
        self.manifest()
        self.version_json("1.20.1", java=17)
        meta_url = "https://piston-meta.mojang.com/v1/packages/1.20.1.json"
        data = json.loads(self.net.routes[meta_url][1])
        self.net.add(data["downloads"]["server"]["url"], b"tampered")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_vanilla("1.20.1")
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertEqual([n for n in os.listdir(self.path("jars"))], [])

    def test_unknown_version(self):
        self.manifest()
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_vanilla("1.99.9")
        self.assertEqual(ctx.exception.code, "invalid_version")


class DownloadTests(InstallerTestCase):
    def test_requires_https(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.download_file("http://example.com/x.jar", self.path("x.jar"))
        self.assertEqual(ctx.exception.code, "invalid_url")
        with self.assertRaises(installer.InstallError):
            installer.download_file("file:///etc/passwd", self.path("x"))

    def test_retries_transient_failures(self):
        url = "https://example.com/file.bin"
        attempts = []

        def flaky(u):
            attempts.append(u)
            if len(attempts) < 3:
                raise urllib.error.URLError("reset")
            from test_installer_base import FakeResponse
            return FakeResponse(b"payload")

        self.net.routes[url] = flaky
        dest = installer.download_file(url, self.path("file.bin"), sha1(b"payload"))
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_404_is_not_retried_and_cleans_up(self):
        url = "https://example.com/missing.bin"
        self.net.add(url, b"nope", status=404)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.download_file(url, self.path("missing.bin"))
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertEqual(self.net.count("missing.bin"), 1)
        self.assertFalse([n for n in os.listdir(self.data) if n.endswith(".part")])

    def test_size_limit(self):
        url = "https://example.com/big.bin"
        self.net.add(url, b"x" * 2048)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.download_file(url, self.path("big.bin"), max_bytes=1024)
        self.assertEqual(ctx.exception.code, "too_large")
        self.assertFalse(os.path.exists(self.path("big.bin")))


class LoaderInstallTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        self.runs = []
        self._patch(mock.patch.object(installer.subprocess, "run", side_effect=self.fake_run))

    def fake_run(self, command, **kwargs):
        self.runs.append((command, kwargs))
        cwd = kwargs["cwd"]
        if "--installServer" in command:
            with open(os.path.join(cwd, "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar"), "wb") as handle:
                handle.write(b"forge")
        else:
            with open(os.path.join(cwd, "fabric-server-launch.jar"), "wb") as handle:
                handle.write(b"launch")
        return mock.Mock(returncode=0, stdout="")

    def test_fabric_install_checks_hashes_strips_secrets_and_marks_complete(self):
        os.environ["ADMIN_KEY"] = "secret"
        self.net.add(f"{installer.FABRIC_META_URL}/versions/installer", [{"version": "1.1.0", "stable": True}])
        jar_url = f"{installer.FABRIC_MAVEN_URL}/1.1.0/fabric-installer-1.1.0.jar"
        self.net.add(jar_url, b"installer")
        self.net.add(jar_url + ".sha1", sha1(b"installer"))
        dest = installer.install_fabric("1.20.1", loader="0.16.14")
        command, kwargs = self.runs[0]
        self.assertIn("-mcversion", command)
        self.assertEqual(command[command.index("-loader") + 1], "0.16.14")
        self.assertTrue(command[0].startswith(self.java_homes[21]))
        self.assertNotIn("ADMIN_KEY", kwargs["env"])
        self.assertEqual(kwargs["env"]["JAVA_HOME"], self.java_homes[21])
        self.assertTrue(installer._install_complete(dest, "fabric", "1.20.1", "0.16.14"))
        self.net.routes.clear()
        installer.install_fabric("1.20.1")  # complete: no network, no installer run
        self.assertEqual(len(self.runs), 1)

    def test_bad_installer_checksum(self):
        jar_url = f"{installer.FABRIC_MAVEN_URL}/1.1.0/fabric-installer-1.1.0.jar"
        self.net.add(jar_url, b"installer")
        self.net.add(jar_url + ".sha1", "0" * 40)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_fabric("1.20.1", loader="0.16.14")
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertEqual(self.runs, [])

    def test_installer_failure_is_reported(self):
        def failing(command, **kwargs):
            raise installer.subprocess.CalledProcessError(1, command, output="line1\nCould not resolve host\n")

        installer.subprocess.run.side_effect = failing
        jar_url = f"{installer.FABRIC_MAVEN_URL}/1.1.0/fabric-installer-1.1.0.jar"
        self.net.add(jar_url, b"installer")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_fabric("1.20.1", loader="0.16.14")
        self.assertEqual(ctx.exception.code, "install_failed")
        self.assertIn("Could not resolve host", str(ctx.exception))
        self.assertFalse(installer._install_complete(installer.instance_dir("fabric", "1.20.1"), "fabric", "1.20.1"))

    def test_forge_1_7_10_preseeds_vanilla_and_uses_java_8(self):
        self.vanilla_jar("1.7.10")
        url = installer._forge_installer_url("1.7.10")
        self.net.add(url, b"forge-installer")
        dest = installer.install_forge("1.7.10")
        self.assertTrue(os.path.isfile(os.path.join(dest, "minecraft_server.1.7.10.jar")))
        command, kwargs = self.runs[0]
        self.assertTrue(command[0].startswith(self.java_homes[8]))
        self.assertIn("--installServer", command)
        self.assertTrue(installer._install_complete(dest, "forge", "1.7.10", "10.13.4.1614-1.7.10"))
        jar, prefix = installer._modpack_launch(dest, "1.7.10")
        self.assertTrue(jar.endswith("-universal.jar"))


class Log4ShellTests(InstallerTestCase):
    def fake_download(self, url, dest, expected_sha1=None, **kwargs):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write("<Configuration/>")
        self.downloads.append((url, expected_sha1))
        return dest

    def setUp(self):
        super().setUp()
        self.downloads = []
        self._patch(mock.patch.object(installer, "download_file", side_effect=self.fake_download))

    def test_mitigations_per_era(self):
        args = installer._log4j_args("1.12.2")
        self.assertEqual(len(args), 1)
        self.assertTrue(args[0].startswith("-Dlog4j.configurationFile="))
        self.assertTrue(args[0].endswith("log4j2_112-116.xml"))
        self.assertEqual(self.downloads[0][1], "02937d122c86ce73319ef9975b58896fc1b491d1")
        self.assertTrue(installer._log4j_args("1.7.10")[0].endswith("log4j2_17-111.xml"))
        self.assertEqual(installer._log4j_args("1.17.1"), ["-Dlog4j2.formatMsgNoLookups=true"])
        self.assertEqual(installer._log4j_args("1.18"), ["-Dlog4j2.formatMsgNoLookups=true"])
        self.assertEqual(installer._log4j_args("1.18.2"), [])
        self.assertEqual(installer._log4j_args("1.6.4"), [])
        self.assertEqual(installer._log4j_args("26.3"), [])

    def test_runtime_spec_carries_mitigation(self):
        self.vanilla_jar("1.12.2")
        spec = installer.create_setup({"type": "vanilla", "minecraft_version": "1.12.2", "level_name": "old"})
        self.assertTrue(any(a.startswith("-Dlog4j.configurationFile=") for a in spec.jvm_args))
        self.assertEqual(spec.java_major, 8)


class ChildEnvTests(InstallerTestCase):
    def test_strips_secrets(self):
        os.environ.update(
            {
                "ADMIN_KEY": "secret",
                "RAILWAY_TOKEN": "t",
                "MY_API_KEY": "k",
                "DB_PASSWORD": "p",
                "DATABASE_URL": "postgres://user:pw@host/db",
                "PUBLIC_URL": "https://example.com",
                "KEEP_ME": "yes",
            }
        )
        env = installer.child_env({"JAVA_HOME": "/opt/java/21"})
        for name in ("ADMIN_KEY", "RAILWAY_TOKEN", "MY_API_KEY", "DB_PASSWORD", "DATABASE_URL"):
            self.assertNotIn(name, env)
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertEqual(env["PUBLIC_URL"], "https://example.com")
        self.assertEqual(env["JAVA_HOME"], "/opt/java/21")


class LoaderTests(InstallerTestCase):
    def test_env_pin_wins(self):
        with mock.patch.object(installer, "FABRIC_LOADER_PIN", "0.16.14"):
            self.assertEqual(installer.fabric_loader_for("26.2"), "0.16.14")
        self.assertEqual(self.net.calls, [])

    def test_meta_stable_loader(self):
        self.net.add(
            f"{installer.FABRIC_META_URL}/versions/loader/26.2",
            [{"loader": {"version": "0.19.6-beta", "stable": False}},
             {"loader": {"version": "0.19.5", "stable": True}}],
        )
        self.assertEqual(installer.fabric_loader_for("26.2"), "0.19.5")

    def test_offline_fallback(self):
        self.assertEqual(installer.fabric_loader_for("26.2"), "0.19.3")
        self.assertEqual(installer.fabric_loader_for("1.20.1"), "0.16.14")

    def test_forge_builds_and_urls(self):
        self.assertEqual(installer.forge_build_for("1.7.10"), "10.13.4.1614-1.7.10")
        self.assertEqual(installer.forge_build_for("1.7.10", "10.13.4.1614"), "10.13.4.1614-1.7.10")
        self.assertEqual(installer.forge_build_for("1.12.2", "14.23.5.2860"), "14.23.5.2860")
        self.assertEqual(installer.forge_build_for("1.16.5", "1.16.5-36.2.34"), "36.2.34")
        self.assertTrue(
            installer._forge_installer_url("1.7.10").endswith(
                "/1.7.10-10.13.4.1614-1.7.10/forge-1.7.10-10.13.4.1614-1.7.10-installer.jar"
            )
        )
        with self.assertRaises(installer.InstallError):
            installer.forge_build_for("1.20.1")
        with self.assertRaises(installer.InstallError):
            installer.forge_build_for("1.12.2", "../../x")

    def test_install_markers_v1_and_v2(self):
        directory = self.path("instances", "fabric-1.20.1")
        os.makedirs(directory)
        self.assertFalse(installer._install_complete(directory, "fabric", "1.20.1"))
        self.write("instances/fabric-1.20.1/.fabric-ready", "1.20.1 0.16.14\n")
        self.assertTrue(installer._install_complete(directory, "fabric", "1.20.1", "0.16.14"))
        self.assertFalse(installer._install_complete(directory, "fabric", "1.20.1", "0.17.0"))
        installer._write_install_marker(directory, loader="fabric", minecraft_version="1.20.1", loader_version="0.17.0")
        self.assertTrue(installer._install_complete(directory, "fabric", "1.20.1", "0.17.0"))


if __name__ == "__main__":
    unittest.main()
