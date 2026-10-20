#!/usr/bin/env python3
"""Modpacks: staging installs, zip detection, mrpack safety, fresh downloads."""

import hashlib
import io
import json
import os
import threading
import time
import unittest
import zipfile
from unittest import mock

from test_installer_base import InstallerTestCase, installer, sha1


def jar_with_version(version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("version.json", json.dumps({"id": version, "name": version}))
    return buffer.getvalue()


class ModpackTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        installer.load_config()
        self.fabric_calls = []
        self._patch(mock.patch.object(installer, "install_fabric", side_effect=self.fake_fabric))
        self._patch(mock.patch.object(installer, "_relink_instance_world"))

    def fake_fabric(self, version, dest=None, loader=None):
        self.fabric_calls.append((version, loader))
        dest = dest or installer.instance_dir("fabric", version)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "fabric-server-launch.jar"), "wb") as handle:
            handle.write(b"launch")
        return dest

    def instance(self, *parts):
        return self.path("instances", *parts)

    def assert_no_leftovers(self):
        hidden = [n for n in os.listdir(self.path("instances")) if n.startswith(".")]
        self.assertEqual(hidden, [])

    def fabric_server_zip(self, version="1.20.1", nested="MyPack-Server", mods=("a.jar",), name="pack.zip"):
        prefix = f"{nested}/" if nested else ""
        entries = {
            f"{prefix}fabric-server-launch.jar": b"launch",
            f"{prefix}libraries/net/fabricmc/intermediary/{version}/intermediary-{version}.jar": b"i",
            "__MACOSX/._junk": b"",
        }
        for mod in mods:
            entries[f"{prefix}mods/{mod}"] = self.make_mod_jar()
        return self.make_zip(entries, name)


class ZipServerPackTests(ModpackTestCase):
    def test_nested_fabric_server_pack(self):
        result = installer.install_modpack_archive(self.fabric_server_zip(), "mypack")
        self.assertEqual(
            {k: result[k] for k in ("type", "minecraft_version", "level_name", "instance")},
            {"type": "modpack", "minecraft_version": "1.20.1", "level_name": "mypack", "instance": "mypack"},
        )
        self.assertEqual(result["loader"], "fabric")
        self.assertTrue(os.path.isfile(self.instance("mypack", "fabric-server-launch.jar")))
        self.assertTrue(os.path.isfile(self.instance("mypack", "mods", "a.jar")))
        self.assertFalse(os.path.exists(self.instance("mypack", "__MACOSX")))
        meta = self.read_json(f"instances/mypack/{installer.PACK_META_FILE}")
        self.assertEqual(meta["minecraft_version"], "1.20.1")
        self.assertEqual(self.fabric_calls, [])  # launch jar shipped with the pack
        self.assert_no_leftovers()

    def test_pack_becomes_a_setup_with_its_own_world(self):
        result = installer.install_modpack_archive(self.fabric_server_zip(), "mypack")
        spec = installer.create_setup(result)
        self.assertEqual(spec.cwd, self.instance("mypack"))
        self.assertEqual(spec.level_name, "mypack")
        self.assertEqual(spec.java_major, 21)
        self.assertTrue(spec.jar.endswith("fabric-server-launch.jar"))
        self.assertFalse(os.path.exists(self.path("worlds", "world", "level.dat")))

    def test_explicit_level_name(self):
        result = installer.install_modpack_archive(self.fabric_server_zip(), "mypack", level_name="shared")
        self.assertEqual(result["level_name"], "shared")
        with self.assertRaises(installer.InstallError):
            installer.install_modpack_archive(self.fabric_server_zip(), "mypack", level_name="..")

    def test_forge_server_pack_with_jar(self):
        archive = self.make_zip(
            {
                "forge-1.16.5-36.2.39.jar": b"forge",
                "minecraft_server.1.16.5.jar": b"mc",
                "mods/b.jar": self.make_mod_jar("META-INF/mods.toml"),
            }
        )
        result = installer.install_modpack_archive(archive, "forgepack")
        self.assertEqual(result["minecraft_version"], "1.16.5")
        self.assertEqual(result["loader"], "forge")
        jar, prefix = installer._modpack_launch(self.instance("forgepack"), "1.16.5")
        self.assertTrue(jar.endswith("forge-1.16.5-36.2.39.jar"))
        self.assertEqual(prefix, [])

    def test_unknown_archive_is_rejected_cleanly(self):
        archive = self.make_zip({"readme.txt": "hello", "other/readme.txt": "x"})
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_archive(archive, "mystery")
        self.assertEqual(ctx.exception.code, "unsupported_pack")
        self.assertIn("Minecraft version", str(ctx.exception))
        self.assertFalse(os.path.exists(self.instance("mystery")))
        self.assert_no_leftovers()

    def test_curseforge_client_pack_points_to_server_pack(self):
        manifest = {
            "manifestType": "minecraftModpack",
            "name": "Client Pack",
            "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.2.0", "primary": True}]},
            "files": [{"projectID": 1, "fileID": 2}],
        }
        archive = self.make_zip({"manifest.json": json.dumps(manifest), "overrides/config/x.cfg": "x"})
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_archive(archive, "client")
        self.assertEqual(ctx.exception.code, "unsupported_pack")
        self.assertIn("server pack", str(ctx.exception))

    def test_zip_slip_and_bad_zip(self):
        archive = self.make_zip({"../evil.txt": "x", "server.jar": jar_with_version("1.20.1")})
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_archive(archive, "evil")
        self.assertEqual(ctx.exception.code, "invalid_archive")
        self.assertFalse(os.path.exists(os.path.join(self.data, "instances", "..", "evil.txt")))
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as handle:
            handle.write(b"not a zip")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_archive(bad, "bad")
        self.assertEqual(ctx.exception.code, "invalid_archive")
        self.assert_no_leftovers()

    def test_extract_size_limit(self):
        archive = self.make_zip({"server.jar": jar_with_version("1.20.1"), "big.bin": b"0" * 4096})
        with self.assertRaises(installer.InstallError) as ctx:
            installer._safe_extract_zip(archive, self.tmp, max_total=1024)
        self.assertEqual(ctx.exception.code, "too_large")

    def test_reinstall_replaces_and_failure_keeps_old(self):
        installer.install_modpack_archive(self.fabric_server_zip(mods=("old.jar",)), "mypack")
        installer.install_modpack_archive(self.fabric_server_zip(mods=("new.jar",), name="v2.zip"), "mypack")
        self.assertEqual(os.listdir(self.instance("mypack", "mods")), ["new.jar"])
        broken = self.make_zip({"readme.txt": "x"}, "broken.zip")
        with self.assertRaises(installer.InstallError):
            installer.install_modpack_archive(broken, "mypack")
        self.assertEqual(os.listdir(self.instance("mypack", "mods")), ["new.jar"])
        self.assert_no_leftovers()

    def test_reinstall_updates_setups_using_the_instance(self):
        installer.create_setup(installer.install_modpack_archive(self.fabric_server_zip(), "mypack"))
        installer.install_modpack_archive(self.fabric_server_zip(version="1.20.4", name="v2.zip"), "mypack")
        setup = next(s for s in installer.list_setups() if s["instance"] == "mypack")
        self.assertEqual(setup["minecraft_version"], "1.20.4")

    def test_downgrade_guard_for_pack_world(self):
        self.make_world("mypack", last_version="1.21.1")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_archive(self.fabric_server_zip(), "mypack")
        self.assertEqual(ctx.exception.code, "world_downgrade")
        self.assertFalse(os.path.exists(self.instance("mypack")))
        result = installer.install_modpack_archive(self.fabric_server_zip(), "mypack", allow_downgrade=True)
        self.assertEqual(result["instance"], "mypack")

    def test_archive_file_is_left_for_the_caller(self):
        archive = self.fabric_server_zip()
        installer.install_modpack_archive(archive, "mypack")
        self.assertTrue(os.path.isfile(archive))


class StagedInstallTests(ModpackTestCase):
    """stage -> commit -> finalize / rollback (what the dashboard uses)."""

    def mods(self, name="mypack"):
        return sorted(os.listdir(self.instance(name, "mods")))

    def setup_version(self, name="mypack"):
        return next(s for s in installer.list_setups() if s["instance"] == name)["minecraft_version"]

    def hidden(self):
        return sorted(n for n in os.listdir(self.path("instances")) if n.startswith("."))

    def test_staging_changes_nothing_in_use_until_commit(self):
        installer.create_setup(installer.install_modpack_archive(self.fabric_server_zip(mods=("old.jar",)), "mypack"))
        staged = installer.stage_modpack_archive(
            self.fabric_server_zip(version="1.20.4", mods=("new.jar",), name="v2.zip"), "mypack")
        self.assertEqual(self.mods(), ["old.jar"])
        self.assertEqual(self.setup_version(), "1.20.1")
        self.assertEqual(staged.fields["minecraft_version"], "1.20.4")
        swap = installer.commit_modpack(staged)
        self.assertEqual(self.mods(), ["new.jar"])
        self.assertEqual(self.setup_version(), "1.20.4")
        self.assertEqual(len(self.hidden()), 1)  # the previous copy, until finalized
        installer.finalize_modpack(swap)
        self.assertEqual(self.hidden(), [])

    def test_rollback_puts_the_previous_pack_and_versions_back(self):
        installer.create_setup(installer.install_modpack_archive(self.fabric_server_zip(mods=("old.jar",)), "mypack"))
        staged = installer.stage_modpack_archive(
            self.fabric_server_zip(version="1.20.4", mods=("new.jar",), name="v2.zip"), "mypack")
        swap = installer.commit_modpack(staged)
        installer.rollback_modpack(swap)
        self.assertEqual(self.mods(), ["old.jar"])
        self.assertEqual(self.setup_version(), "1.20.1")
        self.assertEqual(self.hidden(), [])
        installer.finalize_modpack(swap)  # nothing left to delete
        self.assertEqual(self.mods(), ["old.jar"])

    def test_rollback_of_a_first_install_removes_it(self):
        swap = installer.commit_modpack(installer.stage_modpack_archive(self.fabric_server_zip(), "mypack"))
        self.assertTrue(os.path.isdir(self.instance("mypack")))
        installer.rollback_modpack(swap)
        self.assertFalse(os.path.exists(self.instance("mypack")))
        self.assertEqual(self.hidden(), [])

    def test_discard_staged(self):
        staged = installer.stage_modpack_archive(self.fabric_server_zip(), "mypack")
        self.assertEqual(len(self.hidden()), 1)
        installer.discard_staged(staged)
        self.assertEqual(self.hidden(), [])
        self.assertFalse(os.path.exists(self.instance("mypack")))
        with self.assertRaises(installer.InstallError):
            installer.commit_modpack(staged)

    def test_downgrade_guard_runs_before_the_loader_install(self):
        self.make_world("mypack", last_version="1.21.1")
        archive = self.make_zip({
            "libraries/net/fabricmc/intermediary/1.20.1/intermediary-1.20.1.jar": b"i",
            "mods/a.jar": self.make_mod_jar(),
        })
        with self.assertRaises(installer.InstallError) as ctx:
            installer.stage_modpack_archive(archive, "mypack")
        self.assertEqual(ctx.exception.code, "world_downgrade")
        self.assertEqual(ctx.exception.data["newest_version"], "1.21.1")
        self.assertEqual(self.fabric_calls, [])
        self.assert_no_leftovers()
        staged = installer.stage_modpack_archive(archive, "mypack", allow_downgrade=True)
        self.assertEqual(self.fabric_calls, [("1.20.1", None)])
        installer.discard_staged(staged)


class PeekVersionTests(ModpackTestCase):
    def peek(self, entries):
        return installer.peek_pack_version(self.make_zip(entries, "peek.zip"))

    def test_declared_versions(self):
        cases = [
            ({"modrinth.index.json": json.dumps({"dependencies": {"minecraft": "1.20.1"}})}, "1.20.1"),
            ({"Pack/fabric-server-mc.1.21.1-loader.0.16.5-launcher.1.0.1.jar": "x",
              "Pack/mods/a.jar": "x", "__MACOSX/._x": ""}, "1.21.1"),
            ({"manifest.json": json.dumps({"minecraft": {"version": "1.12.2"}})}, "1.12.2"),
            ({"S/variables.txt": "MINECRAFT_VERSION=1.20.4\n", "S/run.sh": "x"}, "1.20.4"),
            ({"libraries/net/minecraftforge/forge/1.16.5-36.2.39/x.jar": "x"}, "1.16.5"),
            ({"libraries/net/fabricmc/intermediary/1.19.2/i.jar": "x",
              "libraries/net/fabricmc/intermediary/1.20.1/i.jar": "x"}, "1.20.1"),
            ({"forge-1.16.5-36.2.39.jar": "x"}, "1.16.5"),
            ({"minecraft_server.1.7.10.jar": "x"}, "1.7.10"),
            ({installer.PACK_META_FILE: json.dumps({"minecraft_version": "1.18.2"})}, "1.18.2"),
        ]
        for entries, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.peek(entries), expected)

    def test_unknown_or_unreadable_is_none(self):
        self.assertIsNone(self.peek({"readme.txt": "x"}))
        self.assertIsNone(self.peek({"modrinth.index.json": "{not json"}))
        self.assertIsNone(self.peek({"modrinth.index.json": json.dumps({"dependencies": {"minecraft": "../x"}})}))
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as handle:
            handle.write(b"not a zip")
        self.assertIsNone(installer.peek_pack_version(bad))


class DetectionTests(ModpackTestCase):
    def detect(self, entries):
        directory = os.path.join(self.tmp, "detect")
        os.makedirs(directory)
        for rel, content in entries.items():
            full = os.path.join(directory, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(content.encode() if isinstance(content, str) else content)
        return installer._detect_pack(directory)

    def test_curseforge_manifest(self):
        manifest = {"minecraft": {"version": "1.12.2", "modLoaders": [
            {"id": "fabric-0.1", "primary": False}, {"id": "forge-14.23.5.2859", "primary": True}]}}
        info = self.detect({"manifest.json": json.dumps(manifest)})
        self.assertEqual((info.minecraft_version, info.loader, info.loader_version), ("1.12.2", "forge", "14.23.5.2859"))

    def test_variables_txt(self):
        info = self.detect({"variables.txt": 'MINECRAFT_VERSION=1.20.1\nMODLOADER=Forge\nMODLOADER_VERSION=47.2.0\n'})
        self.assertEqual((info.minecraft_version, info.loader, info.loader_version), ("1.20.1", "forge", "47.2.0"))

    def test_fabric_launcher_jar_name(self):
        info = self.detect({"fabric-server-mc.1.21.1-loader.0.16.5-launcher.1.0.1.jar": "x"})
        self.assertEqual((info.minecraft_version, info.loader, info.loader_version), ("1.21.1", "fabric", "0.16.5"))

    def test_forge_libraries(self):
        info = self.detect({"libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt": "x"})
        self.assertEqual((info.minecraft_version, info.loader, info.loader_version), ("1.20.1", "forge", "47.2.0"))

    def test_server_jar_internal_version(self):
        info = self.detect({"server.jar": jar_with_version("1.19.4")})
        self.assertEqual((info.minecraft_version, info.loader), ("1.19.4", None))

    def test_root_version_json(self):
        info = self.detect({"version.json": json.dumps({"id": "1.18.2"})})
        self.assertEqual(info.minecraft_version, "1.18.2")

    def test_minecraft_server_jar_name(self):
        info = self.detect({"minecraft_server.1.12.2.jar": "x", "mods/a.jar": self.make_mod_jar("mcmod.info")})
        self.assertEqual((info.minecraft_version, info.loader), ("1.12.2", "forge"))

    def test_hoist_only_single_non_content_folder(self):
        directory = os.path.join(self.tmp, "hoist")
        os.makedirs(os.path.join(directory, "mods"))
        installer._hoist_single_folder(directory)
        self.assertEqual(os.listdir(directory), ["mods"])
        os.makedirs(os.path.join(directory, "Other"))
        installer._hoist_single_folder(directory)
        self.assertEqual(sorted(os.listdir(directory)), ["Other", "mods"])


class MrpackTests(ModpackTestCase):
    def mrpack(self, files, deps=None, extra=None, name="demo.mrpack"):
        index = {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": "1.0",
            "name": "Demo Pack",
            "dependencies": deps or {"minecraft": "1.20.1", "fabric-loader": "0.16.14"},
            "files": files,
        }
        entries = {"modrinth.index.json": json.dumps(index)}
        entries.update(extra or {})
        return self.make_zip(entries, name)

    def mod_file(self, path, body, host="cdn.modrinth.com", server="required", hashes=True):
        url = f"https://{host}/data/{os.path.basename(path)}"
        self.net.add(url, body)
        entry = {"path": path, "downloads": [url], "fileSize": len(body),
                 "env": {"client": "required", "server": server}}
        if hashes:
            entry["hashes"] = {"sha1": sha1(body), "sha512": hashlib.sha512(body).hexdigest()}
        return entry

    def test_installs_files_overrides_and_loader(self):
        archive = self.mrpack(
            [self.mod_file("mods/a.jar", b"A"), self.mod_file("mods/client.jar", b"C", server="unsupported")],
            extra={"overrides/config/a.toml": "x=1", "server-overrides/config/b.toml": "y=2",
                   "client-overrides/options.txt": "no"},
        )
        result = installer.install_modpack_archive(archive, "demo")
        self.assertEqual(result["name"], "Demo Pack")
        self.assertEqual(result["minecraft_version"], "1.20.1")
        self.assertEqual(self.fabric_calls, [("1.20.1", "0.16.14")])
        self.assertTrue(os.path.isfile(self.instance("demo", "mods", "a.jar")))
        self.assertFalse(os.path.exists(self.instance("demo", "mods", "client.jar")))
        self.assertTrue(os.path.isfile(self.instance("demo", "config", "a.toml")))
        self.assertTrue(os.path.isfile(self.instance("demo", "config", "b.toml")))
        self.assertFalse(os.path.exists(self.instance("demo", "options.txt")))
        self.assertEqual(self.net.count("client.jar"), 0)
        self.assert_no_leftovers()

    def test_untrusted_host_missing_hash_and_bad_path(self):
        cases = [
            [self.mod_file("mods/a.jar", b"A", host="evil.example.com")],
            [self.mod_file("mods/a.jar", b"A", hashes=False)],
            [self.mod_file("../escape.jar", b"A")],
        ]
        for files in cases:
            with self.subTest(files=files[0]["path"]), self.assertRaises(installer.InstallError) as ctx:
                installer.install_modpack_archive(self.mrpack(files), "demo")
            self.assertEqual(ctx.exception.code, "invalid_archive")
        self.assertFalse(os.path.exists(self.instance("demo")))
        self.assert_no_leftovers()

    def test_hash_mismatch(self):
        entry = self.mod_file("mods/a.jar", b"A")
        self.net.add(entry["downloads"][0], b"B")  # same size, wrong content
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_archive(self.mrpack([entry]), "demo")
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertFalse(os.path.exists(self.instance("demo")))

    def test_downgrade_guard_runs_before_downloads(self):
        self.make_world("demo", last_version="1.21.1")
        archive = self.mrpack([self.mod_file("mods/a.jar", b"A")])
        with self.assertRaises(installer.InstallError) as ctx:
            installer.stage_modpack_archive(archive, "demo")
        self.assertEqual(ctx.exception.code, "world_downgrade")
        self.assertEqual(self.net.count("a.jar"), 0)
        self.assertEqual(self.fabric_calls, [])
        self.assert_no_leftovers()

    def test_first_failed_download_cancels_the_queue(self):
        started = []
        lock = threading.Lock()

        def fake_download(url, dest, sha1=None, sha512=None, max_bytes=None, label=None):
            with lock:
                started.append(url)
            if url.endswith("/m0.jar"):
                raise installer.InstallError("boom", code="download_failed")
            time.sleep(0.05)

        files = [self.mod_file(f"mods/m{i}.jar", b"x") for i in range(60)]
        with mock.patch.object(installer, "download_file", side_effect=fake_download):
            with self.assertRaises(installer.InstallError):
                installer._install_mrpack(self._unpacked(files), os.path.join(self.tmp, "stage"))
        self.assertLess(len(started), 30)

    def _unpacked(self, files):
        directory = os.path.join(self.tmp, "unpacked")
        os.makedirs(directory, exist_ok=True)
        index = {"dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.14"}, "files": files}
        with open(os.path.join(directory, "modrinth.index.json"), "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        return directory

    def test_quilt_and_neoforge_are_unsupported(self):
        for loader in ("quilt-loader", "neoforge"):
            with self.subTest(loader=loader), self.assertRaises(installer.InstallError) as ctx:
                installer.install_modpack_archive(self.mrpack([], deps={"minecraft": "1.20.1", loader: "1"}), "demo")
            self.assertEqual(ctx.exception.code, "unsupported_pack")


class UrlInstallTests(ModpackTestCase):
    URL = "https://cdn.modrinth.com/data/pack/versions/1/pack.mrpack"

    def test_downloads_fresh_every_time_and_cleans_up(self):
        with open(self.fabric_server_zip(), "rb") as handle:
            self.net.add(self.URL, handle.read())
        installer.install_modpack_from_url(self.URL, "mypack")
        installer.install_modpack_from_url(self.URL, "mypack")
        self.assertEqual(self.net.count("pack.mrpack"), 2)
        self.assertEqual(os.listdir(self.path("modpacks")), [])

    def test_rejects_non_https_and_non_zip(self):
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_from_url("http://example.com/x.mrpack", "mypack")
        self.assertEqual(ctx.exception.code, "invalid_url")
        self.net.add(self.URL, b"<html>not a pack</html>")
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_from_url(self.URL, "mypack")
        self.assertEqual(ctx.exception.code, "invalid_archive")
        self.assertEqual(os.listdir(self.path("modpacks")), [])

    def test_private_addresses_are_refused(self):
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1"):
            with self.subTest(address=address):
                self.resolved["internal.example"] = [address]
                with self.assertRaises(installer.InstallError) as ctx:
                    installer.install_modpack_from_url("https://internal.example/pack.mrpack", "mypack")
                self.assertEqual(ctx.exception.code, "invalid_url")
        self.assertEqual(self.net.count("internal.example"), 0)

    def test_redirects_must_stay_https_and_public(self):
        handler = installer._SafeRedirect()
        request = installer.urllib.request.Request("https://example.com/a")
        request.public_only = True
        with self.assertRaises(installer.InstallError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.com/b")
        self.resolved["evil.example"] = ["192.168.1.1"]
        with self.assertRaises(installer.InstallError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/b")
        new = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/b")
        self.assertTrue(new.public_only)
        self.assertEqual(new.pinned_ip, "93.184.216.34")

    def test_downloads_connect_to_the_address_that_was_checked(self):
        seen = []

        def capture(request, timeout=None):
            seen.append(getattr(request, "pinned_ip", None))
            raise installer.urllib.error.URLError("stop here")

        self.resolved["pack.example"] = ["93.184.216.34"]
        with mock.patch.object(installer, "_urlopen", side_effect=capture):
            with self.assertRaises(installer.InstallError):
                installer.download_file("https://pack.example/p.mrpack", os.path.join(self.tmp, "p"),
                                        public_only=True, retries=1)
        self.assertEqual(seen, ["93.184.216.34"])

    def test_pinned_connection_uses_the_vetted_address_and_the_real_host_name(self):
        connection = installer._PinnedHTTPSConnection("pack.example", pinned_ip="93.184.216.34", timeout=5)
        self.assertEqual(connection.host, "pack.example")  # SNI and certificate check
        with mock.patch.object(installer.socket, "create_connection", return_value="sock") as create:
            self.assertEqual(connection._create_connection(("pack.example", 443), 5, None), "sock")
        create.assert_called_once_with(("93.184.216.34", 443), 5, None)
        handler = installer._PinnedHTTPSHandler()
        request = installer.urllib.request.Request("https://pack.example/p")
        request.pinned_ip = "93.184.216.34"
        with mock.patch.object(handler, "do_open", return_value="response") as do_open:
            self.assertEqual(handler.https_open(request), "response")
        made = do_open.call_args[0][0]("pack.example", timeout=5)
        self.assertEqual((made.host, made.pinned_ip), ("pack.example", "93.184.216.34"))

    def test_upload_limit(self):
        os.environ["MAX_UPLOAD_MB"] = "1"
        self.net.add(self.URL, b"x" * (1024 * 1024 + 10))
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_modpack_from_url(self.URL, "mypack")
        self.assertEqual(ctx.exception.code, "too_large")


if __name__ == "__main__":
    unittest.main()
