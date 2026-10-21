"""tools/mock_server.py stays in step with the real dashboard API.

The dashboard is built and screenshotted against the mock, so a mock that
answers with other error codes or other response shapes hides real bugs (the
settings page that could never save on Minecraft 1.6.4 was one). These tests
compare the mock with mc_host/panel.py: error codes, response key sets, and
the behaviours the dashboard relies on.
"""

from __future__ import annotations

import gzip
import http.client
import importlib.util
import io
import json
import os
import re
import tarfile
import threading
import types
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock

from runtime_support import ROOT

import installer

from mc_host import installer_api as ix
from mc_host import web
from mc_host.process import tasks

from test_panel import STATUS_KEYS, PanelTestCase


def _load_mock():
    path = os.path.join(ROOT, "tools", "mock_server.py")
    spec = importlib.util.spec_from_file_location("railpowered_mock_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock_server = _load_mock()

ZIP = b"PK\x03\x04" + b"\x00" * 64  # all the mock looks at is the magic
DOWNGRADE_DATA = {"world", "last_version", "newest_version", "version"}


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def real_error_codes() -> set[str]:
    panel_src = _read("mc_host", "panel.py")
    process_src = _read("mc_host", "process.py")
    codes = set(re.findall(r'ApiError\(\s*\d+,\s*"([a-z_]+)"', panel_src))
    codes |= set(re.findall(r'^\s+code = "([a-z_]+)"', process_src, re.M))
    codes |= set(re.findall(r'(?:Running|Busy|Failed)\(\s*"[^"]*",\s*"([a-z_]+)"\s*\)', process_src))
    codes |= set(installer.ERROR_STATUS)
    return codes


def new_mock(phase: str = "online", scenario: str = "default"):
    args = types.SimpleNamespace(phase=phase, quiet=True, no_auth=True, key="admin",
                                 latency=0, upload_kbps=4096, web_dir=os.path.join(ROOT, "web"))
    m = mock_server.Mock(args)
    if scenario != "default":
        m.reset(phase, scenario)
    return m


def call(m, method: str, path: str, body=None):
    """One API call on the mock, as the HTTP handler makes it: (status, payload)."""
    parts = [p for p in path.split("?")[0][len("/api/"):].split("/") if p]
    try:
        with m.lock:
            m.tick()
            return m.handle_api(method, parts, {}, body or {})
    except mock_server.ApiError as err:
        return err.status, err.payload()


def finish_events(m) -> None:
    m.hook("finish", {})
    m.hook("finish", {})


def start_mock_http(test, **overrides):
    """The mock behind a real socket (no sign-in unless asked): for the handler-level rules."""
    args = types.SimpleNamespace(phase="online", quiet=True, no_auth=True, key="admin",
                                 latency=0, upload_kbps=65536, web_dir=os.path.join(ROOT, "web"))
    for key, value in overrides.items():
        setattr(args, key, value)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_server.Handler)
    server.daemon_threads = True
    server.mock = mock_server.Mock(args)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return server


def mock_http(server, method, path, body=b"", headers=None, cookie=None):
    """(status, parsed JSON or None, response) for one request to the mock's socket."""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    sent = {"X-Railpowered": "1"}
    sent.update(headers or {})
    if cookie:
        sent["Cookie"] = f"rp_session={cookie}"
    conn.request(method, path, body=body, headers=sent)
    response = conn.getresponse()
    data = response.read()
    response.body = data
    conn.close()
    try:
        payload = json.loads(data) if data else None
    except ValueError:
        payload = None
    return response.status, payload, response


def world_archive(world="survival", meta=None, level=True) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        files = [("level.dat", b"L")] if level else []
        files.append(("region/r.0.0.mca", b"R"))
        if meta is not None:
            files.insert(0, (mock_server.BACKUP_META_FILE, json.dumps(meta).encode()))
        for arcname, body in files:
            name = arcname if arcname == mock_server.BACKUP_META_FILE else f"{world}/{arcname}"
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def task_of(m):
    return call(m, "GET", "/api/tasks/current")[1]["task"]



class MockErrorCodeTests(unittest.TestCase):
    def test_every_mock_code_exists_in_the_real_api(self):
        unknown = mock_server.CODES - real_error_codes() - {"mock_failure"}
        self.assertEqual(unknown, set(), "the mock answers with codes the real API never sends")

    def test_unknown_codes_are_refused(self):
        with self.assertRaises(AssertionError):
            mock_server.ApiError(400, "nope", "bad_request")

    def test_codes_match_the_real_ones_for_common_mistakes(self):
        m = new_mock()
        cases = [
            (("POST", "/api/worlds", {"name": "survival"}), 409, "world_exists"),
            (("POST", "/api/worlds", {"name": "../x"}), 400, "invalid_name"),
            (("DELETE", "/api/worlds/nowhere"), 404, "world_not_found"),
            (("DELETE", "/api/worlds/survival"), 409, "active_world"),
            (("DELETE", "/api/setups/survival"), 409, "active_setup"),
            (("PATCH", "/api/setups/missing", {"name": "x"}), 404, "not_found"),
            (("POST", "/api/setups", {"type": "modpack"}), 400, "invalid_type"),
            (("POST", "/api/setups", {"type": "forge", "minecraft_version": "latest", "level_name": "w"}), 400, "invalid_version"),
            (("POST", "/api/setups", {"type": "vanilla", "minecraft_version": "1.21.4", "level_name": "nope",
                                      "create_world": False}), 404, "world_not_found"),
            (("POST", "/api/modpacks", {"url": "http://example.com/p.mrpack", "name": "p"}), 400, "invalid_url"),
            (("POST", "/api/players/whitelist", {"name": "not a name"}), 400, "invalid_player"),
            (("POST", "/api/players/whitelist", {"name": "nobody_here"}), 404, "player_not_found"),
            (("POST", "/api/backups/missing-20250101-000000.tar.gz/restore"), 404, "backup_not_found"),
            (("POST", "/api/console", {"command": "a\nb"}), 400, "invalid"),
            (("PUT", "/api/settings", {"properties": {"white-list": True}}), 400, "invalid_setting"),
            (("PUT", "/api/settings", {"railpowered": {"colour": "red"}}), 400, "invalid_setting"),
        ]
        for request, status, code in cases:
            got_status, payload = call(m, *request)
            self.assertEqual((got_status, payload.get("code")), (status, code), request)


class MockBehaviourTests(unittest.TestCase):
    def test_activating_the_active_setup_is_not_an_error(self):
        status, payload = call(new_mock(), "POST", "/api/setups/survival/activate")
        self.assertEqual((status, payload), (200, {"ok": True, "task": None, "already_active": True}))

    def test_patching_a_setup_onto_a_new_world_creates_it(self):
        m = new_mock()
        status, payload = call(m, "PATCH", "/api/setups/creative", {"level_name": "brand-new"})
        self.assertEqual(status, 200)
        self.assertIn("brand-new", m.worlds)
        self.assertEqual(next(s for s in payload["setups"] if s["id"] == "creative")["level_name"], "brand-new")

    def test_adding_and_removing_players_is_idempotent(self):
        m = new_mock()
        for method, path, body in (
            ("POST", "/api/players/whitelist", {"name": "pixel_pete"}),
            ("POST", "/api/players/whitelist", {"name": "PIXEL_PETE"}),
            ("DELETE", "/api/players/whitelist/stranger", None),
        ):
            status, payload = call(m, method, path, body)
            self.assertEqual(status, 200, (method, path))
            self.assertEqual(sum(p["name"].lower() == "pixel_pete" for p in payload["whitelist"]), 1)

    def test_console_accepts_commands_while_starting(self):
        m = new_mock("sleeping")
        self.assertEqual(call(m, "POST", "/api/console", {"command": "list"})[1]["code"], "not_running")
        call(m, "POST", "/api/server/start")
        self.assertEqual(m.phase, "starting")
        self.assertEqual(call(m, "POST", "/api/console", {"command": "/say hi"}), (200, {"ok": True}))

    def test_save_needs_an_online_server(self):
        status, payload = call(new_mock("sleeping"), "POST", "/api/server/save")
        self.assertEqual((status, payload["code"]), (409, "not_online"))

    def test_server_actions_wait_for_any_running_task(self):
        m = new_mock()
        self.assertEqual(call(m, "POST", "/api/backups", {"world": "creative"})[0], 202)
        for action in ("start", "stop", "restart"):
            status, payload = call(m, "POST", f"/api/server/{action}")
            self.assertEqual((status, payload["code"]), (409, "busy"), action)

    def test_task_ids_stay_unique_across_resets(self):
        m = new_mock()
        first = call(m, "POST", "/api/backups", {"world": "creative"})[1]["task"]["id"]
        m.hook("reset", {})
        second = call(m, "POST", "/api/backups", {"world": "creative"})[1]["task"]["id"]
        self.assertNotEqual(first, second)

    def test_modpack_downgrade_fails_inside_the_task(self):
        m = new_mock("sleeping")
        status, payload = call(m, "POST", "/api/modpacks",
                               {"url": "https://example.com/pack.mrpack", "name": "pack", "level_name": "survival"})
        self.assertEqual(status, 202)
        finish_events(m)
        task = call(m, "GET", "/api/status")[1]["task"]
        self.assertEqual(task["state"], "failed")
        self.assertIn("has been played on Minecraft 26.3", task["error"])
        self.assertEqual(task["code"], "world_downgrade")
        self.assertEqual(task["data"], {"world": "survival", "last_version": "26.3", "newest_version": "26.3",
                                        "version": "1.21.1"})

    def test_restore_may_replace_an_existing_world(self):
        m = new_mock("sleeping")
        backup = next(b for b in m.backups if b["world"] == "survival")
        status, _ = call(m, "POST", f"/api/backups/{backup['name']}/restore", {"target": "creative"})
        self.assertEqual(status, 202)

    def test_damaged_backups_are_flagged_and_fail_to_restore(self):
        m = new_mock("sleeping")
        damaged = [b for b in call(m, "GET", "/api/backups")[1]["backups"] if b.get("damaged")]
        self.assertEqual(len(damaged), 1)
        self.assertIsNone(damaged[0]["world"])
        self.assertEqual(call(m, "POST", f"/api/backups/{damaged[0]['name']}/restore", {"target": "lost"})[0], 202)
        finish_events(m)
        self.assertEqual(call(m, "GET", "/api/status")[1]["task"]["state"], "failed")

    def test_settings_reject_keys_outside_the_schema(self):
        m = new_mock()
        status, payload = call(m, "PUT", "/api/settings", {"properties": {"server-port": 1}})
        self.assertEqual((status, payload["code"]), (400, "invalid_setting"))
        # Unchanged values the dashboard no longer sends aren't needed either.
        self.assertEqual(call(m, "PUT", "/api/settings", {"properties": {"motd": "Hi"}})[0], 200)
        self.assertEqual(call(m, "PUT", "/api/settings", {"railpowered": {"auto_start": True}})[0], 200)
        self.assertEqual(m.idle_timeout, 600, "a railpowered change must not reset the other one")

    def test_settings_keep_an_unknown_level_type_selectable(self):
        m = new_mock()
        m.properties["level-type"] = "biomesoplenty"
        payload = call(m, "GET", "/api/settings")[1]
        self.assertIn("biomesoplenty", payload["schema"]["level-type"]["options"])
        self.assertEqual(call(m, "PUT", "/api/settings", {"properties": {"level-type": "biomesoplenty"}})[0], 200)

    def test_legacy_scenario_hides_online_mode_from_the_settings(self):
        m = new_mock("online", "legacy")
        status = call(m, "GET", "/api/status")[1]
        self.assertTrue(status["legacy"])
        self.assertFalse(status["online_mode"])
        payload = call(m, "GET", "/api/settings")[1]
        self.assertNotIn("online-mode", payload["schema"])
        self.assertNotIn("simulation-distance", payload["schema"])
        # The old login servers are gone: the key isn't in the schema and isn't sent at all.
        self.assertNotIn("online-mode", payload["properties"])
        # Sending online-mode anyway fails like the real server.
        status, body = call(m, "PUT", "/api/settings", {"properties": {"online-mode": False}})
        self.assertEqual((status, body["code"]), (400, "invalid_setting"))
        self.assertEqual(call(m, "PUT", "/api/settings", {"properties": {"motd": "Old times"}})[0], 200)

    def test_versions_can_be_offline(self):
        m = new_mock()
        self.assertIs(call(m, "GET", "/api/versions")[1]["offline"], False)
        m.hook("offline", {"set": ["1"]})
        self.assertIs(call(m, "GET", "/api/versions")[1]["offline"], True)

    # ---- tasks, downgrade guard, sidecar --------------------------------

    def test_tasks_carry_code_data_note_and_a_boot_prefixed_id(self):
        m = new_mock()
        task = call(m, "POST", "/api/backups", {"world": "creative"})[1]["task"]
        self.assertRegex(task["id"], r"^t[0-9a-f]{6}-[0-9]+$")
        self.assertEqual((task["code"], task["data"], task["note"]), (None, {}, None))
        finish_events(m)
        done = task_of(m)
        self.assertEqual((done["state"], done["code"], done["data"], done["note"]), ("done", None, {}, None))

    def test_failed_tasks_say_why(self):
        m = new_mock("sleeping")
        call(m, "POST", "/api/modpacks", {"url": "https://example.com/fail.mrpack", "name": "pack", "level_name": "fresh"})
        finish_events(m)
        task = task_of(m)
        self.assertEqual((task["state"], task["code"], task["data"]), ("failed", "download_failed", {}))
        damaged = next(b for b in m.backups if b["damaged"])
        call(m, "POST", f"/api/backups/{damaged['name']}/restore", {"target": "lost"})
        finish_events(m)
        task = task_of(m)
        self.assertEqual((task["state"], task["code"], task["error"]), ("failed", "invalid_backup", "That backup file is damaged."))

    def test_worlds_report_the_newest_version_that_opened_them(self):
        m = new_mock("sleeping")
        worlds = {w["name"]: w for w in call(m, "GET", "/api/worlds")[1]["worlds"]}
        self.assertEqual(worlds["survival"]["newest_version"], "26.3")
        self.assertEqual((worlds["fabric-world"]["last_version"], worlds["fabric-world"]["newest_version"]), ("1.21.11", "26.1"))
        # A confirmed downgrade is backed up first, remembered, and doesn't lower the newest version.
        self.assertEqual(call(m, "POST", "/api/setups/creative/activate")[1]["code"], "world_downgrade")
        self.assertEqual(call(m, "POST", "/api/setups/creative/activate", {"allow_downgrade": True})[0], 202)
        finish_events(m)
        self.assertEqual(task_of(m)["state"], "done")
        self.assertEqual(call(m, "POST", "/api/server/start")[0], 200, "already confirmed: no second question")
        finish_events(m)
        worlds = {w["name"]: w for w in call(m, "GET", "/api/worlds")[1]["worlds"]}
        self.assertEqual((worlds["creative"]["last_version"], worlds["creative"]["newest_version"]), ("1.21.4", "26.2.1"))
        kinds = [b["kind"] for b in call(m, "GET", "/api/backups")[1]["backups"] if b["world"] == "creative"]
        self.assertEqual(kinds, ["pre-downgrade"])

    def test_a_newer_version_is_backed_up_and_raises_the_newest_version(self):
        m = new_mock("sleeping")
        m.worlds["survival"]["newest_version"] = m.worlds["survival"]["last_version"] = "26.2"
        call(m, "POST", "/api/server/start")
        finish_events(m)
        self.assertEqual(m.worlds["survival"]["newest_version"], "26.3")
        self.assertIn("pre-upgrade", [b["kind"] for b in m.backups if b["world"] == "survival"])

    def test_modpack_downgrade_can_be_confirmed_by_resubmitting(self):
        m = new_mock("sleeping")
        body = {"url": "https://example.com/pack.mrpack", "name": "pack", "level_name": "survival"}
        self.assertEqual(call(m, "POST", "/api/modpacks", body)[0], 202)
        finish_events(m)
        failed = task_of(m)
        self.assertEqual((failed["state"], failed["code"]), ("failed", "world_downgrade"))
        self.assertEqual(set(failed["data"]), DOWNGRADE_DATA)
        self.assertEqual(call(m, "POST", "/api/modpacks", dict(body, allow_downgrade=True))[0], 202)
        finish_events(m)
        self.assertEqual(task_of(m)["state"], "done")
        self.assertEqual(call(m, "GET", "/api/status")[1]["setup"]["instance"], "pack")
        self.assertIn("pre-downgrade", [b["kind"] for b in m.backups if b["world"] == "survival"])

    def test_restoring_a_newer_world_adds_a_note_and_the_start_asks(self):
        m = new_mock("sleeping")
        call(m, "POST", "/api/setups/fabric-friends/activate")
        finish_events(m)
        backup = next(b for b in m.backups if b["world"] == "fabric-world")
        self.assertEqual(call(m, "POST", f"/api/backups/{backup['name']}/restore")[0], 202)
        finish_events(m)
        task = task_of(m)
        self.assertEqual(task["state"], "done")
        self.assertIn("played on Minecraft 26.1, newer than this setup's 1.21.11", task["note"])
        status, payload = call(m, "POST", "/api/server/start")
        self.assertEqual((status, payload["code"]), (409, "world_downgrade"))
        self.assertEqual(set(payload), {"error", "code"} | DOWNGRADE_DATA)
        state = call(m, "GET", "/api/status")[1]
        self.assertEqual(state["phase"], "crashed")
        self.assertIn("played on Minecraft 26.1", state["last_exit"]["reason"])
        self.assertEqual(call(m, "POST", "/api/server/start", {"allow_downgrade": True}), (200, {"ok": True}))
        self.assertEqual(m.phase, "starting")
        self.assertIsNone(call(m, "GET", "/api/status")[1]["last_exit"])
        self.assertIn("pre-downgrade", [b["kind"] for b in m.backups if b["world"] == "fabric-world"])

    def test_start_answers_409_after_the_world_hook(self):
        m = new_mock("sleeping")
        m.hook("world", {"name": ["survival"], "newest": ["26.4"]})
        status, payload = call(m, "POST", "/api/server/start")
        self.assertEqual((status, payload["code"], payload["newest_version"], payload["version"]), (409, "world_downgrade", "26.4", "26.3"))
        # Activating the setup that is already active asks again with allow_downgrade.
        self.assertEqual(call(m, "POST", "/api/setups/survival/activate")[1], {"ok": True, "task": None, "already_active": True})
        self.assertEqual(call(m, "POST", "/api/setups/survival/activate", {"allow_downgrade": True})[0], 202)

    def test_world_operations_are_busy_while_the_server_starts_or_stops(self):
        m = new_mock("sleeping")
        call(m, "POST", "/api/server/start")
        backup = next(b for b in m.backups if b["world"] == "survival")
        requests = [
            ("POST", "/api/backups", {"world": "creative"}),
            ("POST", "/api/setups/creative/activate", {"allow_downgrade": True}),
            ("POST", f"/api/backups/{backup['name']}/restore", {"target": "scratch"}),
            ("POST", "/api/modpacks", {"url": "https://example.com/p.mrpack", "name": "pack", "level_name": "fresh"}),
            ("POST", "/api/setups", {"type": "vanilla", "minecraft_version": "1.21.4", "level_name": "fresh"}),
        ]
        for method, path, body in requests:
            status, payload = call(m, method, path, body)
            self.assertEqual((status, payload["code"]), (409, "busy"), path)
            self.assertIn("starting", payload["error"])
        finish_events(m)
        self.assertEqual(m.phase, "online")
        self.assertEqual(call(m, "POST", "/api/server/stop")[0], 200)
        for method, path, body in requests:
            status, payload = call(m, method, path, body)
            self.assertEqual((status, payload["code"]), (409, "busy"), path)
            self.assertIn("stopping", payload["error"])
        finish_events(m)
        self.assertEqual(call(m, "POST", "/api/backups", {"world": "creative"})[0], 202)

    # ---- downloaded files ------------------------------------------------

    def test_storage_lists_and_deletes_unused_files_only(self):
        m = new_mock("sleeping")
        listing = call(m, "GET", "/api/storage")[1]
        self.assertEqual(set(listing), {"items", "total"})
        self.assertEqual(listing["total"], sum(i["size"] for i in listing["items"]))
        items = {i["name"]: i for i in listing["items"]}
        self.assertEqual(items["minecraft_server.26.3.jar"]["used_by"], [{"id": "survival", "name": "Survival"}])
        self.assertEqual(items["forge-1.12.2"]["used_by"], [])
        status, payload = call(m, "DELETE", "/api/storage/jar/minecraft_server.26.3.jar")
        self.assertEqual((status, payload["code"]), (409, "in_use"))
        self.assertEqual(payload["used_by"], [{"id": "survival", "name": "Survival"}])
        status, payload = call(m, "DELETE", "/api/storage/instance/forge-1.12.2")
        self.assertEqual(status, 200)
        self.assertEqual((payload["freed"], set(payload)), (items["forge-1.12.2"]["size"], {"ok", "freed", "items", "total"}))
        self.assertNotIn("forge-1.12.2", {i["name"] for i in payload["items"]})
        self.assertEqual(payload["total"], listing["total"] - payload["freed"])
        for path, status, code in (
            ("/api/storage/instance/forge-1.12.2", 404, "storage_not_found"),
            ("/api/storage/world/survival", 404, "not_found"),
            ("/api/storage/jar/..", 400, "invalid_name"),
        ):
            got = call(m, "DELETE", path)
            self.assertEqual((got[0], got[1]["code"]), (status, code), path)
        call(m, "POST", "/api/backups", {"world": "creative"})
        self.assertEqual(call(m, "DELETE", "/api/storage/jar/minecraft_server.1.20.4.jar")[1]["code"], "busy")

    def test_storage_of_a_running_instance_is_in_use(self):
        m = new_mock("online")
        m.active = "cozy-farm"
        status, payload = call(m, "DELETE", "/api/storage/instance/cozy-farm")
        self.assertEqual((status, payload["code"]), (409, "in_use"))
        self.assertNotIn("used_by", payload)

    def test_deleting_a_setup_or_world_lists_the_files_nobody_uses_now(self):
        m = new_mock()
        status, payload = call(m, "DELETE", "/api/setups/creative")
        self.assertEqual((status, set(payload)), (200, {"ok", "orphaned"}))
        self.assertEqual([(i["kind"], i["name"], i["used_by"]) for i in payload["orphaned"]],
                         [("jar", "minecraft_server.1.21.4.jar", [])])
        status, payload = call(m, "DELETE", "/api/worlds/cozy-farm")
        self.assertEqual((status, set(payload)), (200, {"ok", "removed_setups", "orphaned"}))
        self.assertEqual(payload["removed_setups"], ["Cozy Farm pack"])
        self.assertEqual([i["name"] for i in payload["orphaned"]], ["cozy-farm"])
        self.assertEqual(call(m, "DELETE", "/api/worlds/old-world-2023")[1]["orphaned"], [])

    def test_installing_a_setup_leaves_its_files_on_the_volume(self):
        m = new_mock("sleeping")
        call(m, "POST", "/api/setups", {"type": "fabric", "minecraft_version": "1.20.1", "level_name": "fab"})
        finish_events(m)
        names = {i["name"]: i["used_by"] for i in call(m, "GET", "/api/storage")[1]["items"]}
        self.assertEqual([u["name"] for u in names["fabric-1.20.1"]], ["Fabric 1.20.1"])

    # ---- sessions and "latest" -------------------------------------------

    def test_revoke_all_signs_everyone_out(self):
        m = new_mock()
        m.sessions.update({"a", "b"})
        self.assertEqual(call(m, "POST", "/api/sessions/revoke-all"), (200, {"ok": True}))
        self.assertEqual(m.sessions, set())
        self.assertTrue(m.signed_out)

    def test_an_unknown_latest_release_shows_as_null_until_the_server_has_run(self):
        m = new_mock("sleeping", "fresh")
        shown = call(m, "GET", "/api/status")[1]["setup"]
        self.assertEqual(shown["resolved_version"], mock_server.LATEST, "the backend falls back to the cached release")
        m.hook("latest", {"known": ["0"]})
        self.assertIsNone(call(m, "GET", "/api/status")[1]["setup"]["resolved_version"])
        listed = call(m, "GET", "/api/setups")[1]["setups"][0]
        self.assertEqual((listed["resolved_version"], listed["summary"]), (None, "Vanilla latest · World: world"))
        call(m, "POST", "/api/server/start")
        finish_events(m)
        self.assertEqual(call(m, "GET", "/api/status")[1]["setup"]["resolved_version"], mock_server.LATEST)

    def test_setup_summaries_read_like_the_backends(self):
        summaries = {s["id"]: s["summary"] for s in call(new_mock(), "GET", "/api/setups")[1]["setups"]}
        self.assertEqual(summaries["survival"], "Vanilla latest (26.3) · World: survival")
        self.assertEqual(summaries["cozy-farm"], "Modpack cozy-farm · Minecraft 1.21.1 · World: cozy-farm")

    def test_backups_are_listed_newest_first(self):
        m = new_mock()
        call(m, "POST", "/api/backups", {"world": "creative"})
        finish_events(m)
        listed = call(m, "GET", "/api/backups")[1]["backups"]
        self.assertEqual(listed[0]["world"], "creative")
        self.assertIsNone(listed[-1]["created_at"], "unreadable archives sort last")


class MockShapeParityTests(PanelTestCase):
    """The mock's responses carry the same keys as the real server's."""

    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.mock = new_mock()

    def assertSameKeys(self, real: dict, fake: dict, what: str, ahead: set[str] = frozenset()):
        self.assertEqual(set(real) - set(fake), set(), f"{what}: keys the mock never sends")
        self.assertEqual(set(fake) - set(real) - set(ahead), set(), f"{what}: keys the real API never sends")

    def real(self, method: str, path: str, body=None):
        status, payload = self.api(method, path, body)
        self.assertLess(status, 300, (path, payload))
        return payload

    def test_status(self):
        real = self.real("GET", "/api/status")
        fake = call(self.mock, "GET", "/api/status")[1]
        self.assertEqual(set(real), STATUS_KEYS)
        self.assertSameKeys(real, fake, "status")
        self.assertSameKeys(real["setup"], fake["setup"], "status.setup")
        self.assertSameKeys(real["idle"], fake["idle"], "status.idle")

    def test_lists(self):
        for path, key in (("/api/setups", "setups"), ("/api/worlds", "worlds")):
            real = self.real("GET", path)[key]
            fake = call(self.mock, "GET", path)[1][key]
            self.assertTrue(real and fake, path)
            self.assertSameKeys(real[0], fake[0], path)

    def test_logs_and_tasks(self):
        real = self.real("GET", "/api/logs")
        fake = call(self.mock, "GET", "/api/logs")[1]
        self.assertSameKeys(real, fake, "logs")
        self.assertTrue(real["lines"] and fake["lines"])
        self.assertSameKeys(real["lines"][0], fake["lines"][0], "logs.lines")
        status, real_task = self.api("POST", "/api/backups", {"world": "world"})
        self.assertEqual(status, 202)
        tasks.wait_idle(10)
        fake_task = call(self.mock, "POST", "/api/backups", {"world": "creative"})[1]
        self.assertSameKeys(real_task, fake_task, "backup response")
        self.assertSameKeys(real_task["task"], fake_task["task"], "task")

        real_backups = self.real("GET", "/api/backups")
        fake_backups = call(self.mock, "GET", "/api/backups")[1]
        self.assertSameKeys(real_backups, fake_backups, "backups")
        self.assertSameKeys(real_backups["disk"], fake_backups["disk"], "backups.disk")
        self.assertSameKeys(real_backups["backups"][0], fake_backups["backups"][0], "backup")

    def test_players(self):
        self.write_properties(online_mode="false")  # offline names need no Mojang lookup
        self.real("POST", "/api/players/whitelist", {"name": "Steve"})
        real = self.real("GET", "/api/players")
        fake = call(self.mock, "GET", "/api/players")[1]
        self.assertSameKeys(real, fake, "players")
        self.assertSameKeys(real["whitelist"][0], fake["whitelist"][0], "players.whitelist")
        real_add = self.real("POST", "/api/players/ops", {"name": "Steve"})
        fake_add = call(self.mock, "POST", "/api/players/ops", {"name": "Steve"})[1]
        self.assertSameKeys(real_add, fake_add, "add player")
        real_del = self.real("DELETE", "/api/players/ops/Steve")
        fake_del = call(self.mock, "DELETE", "/api/players/ops/Steve")[1]
        self.assertSameKeys(real_del, fake_del, "remove player")

    def test_settings(self):
        real = self.real("GET", "/api/settings")
        fake = call(self.mock, "GET", "/api/settings")[1]
        self.assertSameKeys(real, fake, "settings")
        self.assertSameKeys(real["railpowered"], fake["railpowered"], "settings.railpowered")
        # Same version, same schema: the mock's copy of settings_schema matches.
        version = ix.active_setup().get("resolved_version") or installer.FALLBACK_RELEASE
        for probe in (version, "1.16.5", "1.6.4"):
            real_schema = installer.settings_schema(probe)
            fake_schema = mock_server.settings_schema(probe)
            self.assertEqual(set(real_schema), set(fake_schema), probe)
            for key, item in real_schema.items():
                self.assertEqual(item["type"], fake_schema[key]["type"], (probe, key))
                if item["type"] == "enum":
                    self.assertEqual(item["options"], fake_schema[key]["options"], (probe, key))
                if item["type"] == "int":
                    self.assertEqual((item["min"], item["max"]), (fake_schema[key]["min"], fake_schema[key]["max"]), (probe, key))
        real_put = self.real("PUT", "/api/settings", {"properties": {"motd": "Hello"}})
        fake_put = call(self.mock, "PUT", "/api/settings", {"properties": {"motd": "Hello"}})[1]
        self.assertSameKeys(real_put, fake_put, "settings PUT")

    def test_versions(self):
        with mock.patch.object(ix, "list_available_versions", side_effect=urllib.error.URLError("offline")), \
                mock.patch("builtins.print"):
            real = self.real("GET", "/api/versions")
        fake = call(self.mock, "GET", "/api/versions")[1]
        self.assertSameKeys(real, fake, "versions")
        # Only snapshots carry "snapshot", and the offline list has none.
        self.assertSameKeys(real["vanilla"][0], fake["vanilla"][0], "versions.vanilla", ahead={"snapshot"})

    def test_errors_and_setup_routes(self):
        status, real = self.api("GET", "/api/nothing-here")
        fake_status, fake = call(self.mock, "GET", "/api/nothing-here")
        self.assertEqual((status, fake_status), (404, 404))
        self.assertSameKeys(real, fake, "error body")
        self.assertEqual((real["code"], fake["code"]), ("not_found", "not_found"))
        active = ix.active_setup()["id"]
        real = self.real("POST", f"/api/setups/{active}/activate", {})
        fake = call(self.mock, "POST", "/api/setups/survival/activate")[1]
        self.assertEqual(real, fake)
        real = self.real("PATCH", f"/api/setups/{active}", {"name": "Renamed"})
        fake = call(self.mock, "PATCH", "/api/setups/survival", {"name": "Renamed"})[1]
        self.assertSameKeys(real, fake, "setup PATCH")
        real = self.real("POST", "/api/worlds", {"name": "fresh"})
        fake = call(self.mock, "POST", "/api/worlds", {"name": "fresh"})[1]
        self.assertSameKeys(real, fake, "world POST")

    def test_health_and_login_page(self):
        status, payload = self.api("GET", "/health", cookie=False)
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        # The real login page escapes and fills in every {{error}}; so does the mock.
        self.assertEqual(mock_server.LOGIN_ERRORS, web.LOGIN_ERRORS)

    def test_downgrade_message_and_data_match_the_installer(self):
        installer._write_world_meta("world", {"newest_version": "26.3", "last_version": "26.3"})
        with self.assertRaises(installer.InstallError) as raised:
            installer.check_downgrade("world", "1.21.1")
        message, data = mock_server.Mock.downgrade_info(self.mock, "survival", "1.21.1")
        self.assertEqual(str(raised.exception).replace("'world'", "'survival'"), message)
        self.assertEqual({k: (v if k != "world" else "survival") for k, v in raised.exception.data.items()}, data)

    def test_storage(self):
        os.makedirs(os.path.join(installer.paths()["instances"], "forge-1.12.2", "libraries"))
        os.makedirs(os.path.join(installer.paths()["instances"], "mypack"))

        def add(cfg):
            cfg["profiles"].append(
                {"id": "pack", "name": "My pack", "type": "modpack", "minecraft_version": "1.20.1",
                 "resolved_version": "1.20.1", "level_name": "packworld", "instance": "mypack"})

        installer._mutate_config(add)
        real = self.real("GET", "/api/storage")
        fake = call(self.mock, "GET", "/api/storage")[1]
        self.assertSameKeys(real, fake, "storage")
        self.assertSameKeys(real["items"][0], fake["items"][0], "storage item")
        self.assertEqual(real["items"][0]["kind"], "instance", "instances come first, like the mock's list")
        self.assertEqual(fake["items"][0]["kind"], "instance")
        # in use: 409 with used_by, the same body
        status, real_error = self.api("DELETE", "/api/storage/instance/mypack")
        fake_status, fake_error = call(self.mock, "DELETE", "/api/storage/jar/minecraft_server.26.3.jar")
        self.assertEqual((status, fake_status), (409, 409))
        self.assertEqual((real_error["code"], fake_error["code"]), ("in_use", "in_use"))
        self.assertSameKeys(real_error, fake_error, "in_use body")
        self.assertSameKeys(real_error["used_by"][0], fake_error["used_by"][0], "used_by")
        # deleted: ok, freed and the new listing
        real_ok = self.real("DELETE", "/api/storage/instance/forge-1.12.2")
        fake_ok = call(self.mock, "DELETE", "/api/storage/instance/forge-1.12.2")[1]
        self.assertSameKeys(real_ok, fake_ok, "storage DELETE")
        # unknown things
        for path, status, code in (
            ("/api/storage/instance/missing", 404, "storage_not_found"),
            ("/api/storage/world/world", 404, "not_found"),
            ("/api/storage/instance/..", 400, "invalid_name"),
        ):
            real_status, real_body = self.api("DELETE", path)
            fake_status, fake_body = call(self.mock, "DELETE", path)
            self.assertEqual((real_status, real_body["code"]), (status, code), path)
            self.assertEqual((fake_status, fake_body["code"]), (status, code), path)
        # orphaned: the setup and world delete answers
        real_del = self.real("DELETE", "/api/setups/pack")
        fake_del = call(self.mock, "DELETE", "/api/setups/creative")[1]
        self.assertSameKeys(real_del, fake_del, "setup DELETE")
        self.assertSameKeys(real_del["orphaned"][0], fake_del["orphaned"][0], "orphaned item")
        self.real("POST", "/api/worlds", {"name": "scratch"})
        real_world = self.real("DELETE", "/api/worlds/scratch")
        fake_world = call(self.mock, "DELETE", "/api/worlds/cozy-farm")[1]
        self.assertSameKeys(real_world, fake_world, "world DELETE")
        self.assertEqual(real_world["removed_setups"], [])
        self.assertEqual(fake_world["removed_setups"], ["Cozy Farm pack"])

    def test_sessions_revoke_all(self):
        fake = call(self.mock, "POST", "/api/sessions/revoke-all")
        real_status, real = self.api("POST", "/api/sessions/revoke-all", {})
        self.assertEqual((real_status, real), fake)
        self.assertEqual(self.api("GET", "/api/status")[0], 401)

    def test_task_shapes_for_failures_and_notes(self):
        # A failed task's code and data, as the real runner shapes them.
        error = installer.InstallError("older", code="world_downgrade", data={"world": "world"})
        runner = tasks.__class__()
        with mock.patch("builtins.print"):
            task = runner.submit("apply", "x", lambda _ctx: (_ for _ in ()).throw(error))
            runner.wait_idle(5)
        real = runner.snapshot()
        self.assertEqual(set(real), set(task.to_dict()))
        m = self.mock
        m.hook("task", {"kind": ["apply"], "seconds": ["1"], "fail": ["1"]})
        finish_events(m)
        fake = call(m, "GET", "/api/status")[1]["task"]
        self.assertSameKeys(real, fake, "failed task")
        self.assertEqual((fake["state"], fake["code"], fake["data"]), ("failed", "download_failed", {}))


class MockUploadParityTests(PanelTestCase):
    """Upload answers: the mock behind a socket against the real panel."""

    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.server = start_mock_http(self)

    def real(self, method, path, body=b""):
        status, payload = self.api(method, path, body=body)
        return status, payload

    def fake(self, method, path, body=b"", headers=None):
        status, payload, _ = mock_http(self.server, method, path, body, headers)
        return status, payload

    def both(self, method, path, body=b""):
        real, fake = self.real(method, path, body), self.fake(method, path, body)
        self.assertEqual(real[0], fake[0], (path, real, fake))
        self.assertEqual((real[1] or {}).get("code"), (fake[1] or {}).get("code"), (path, real, fake))
        self.assertEqual(set(real[1] or ()), set(fake[1] or ()), (path, real, fake))
        return real, fake

    def zip_with(self, minecraft):
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("modrinth.index.json", json.dumps({"dependencies": {"minecraft": minecraft}}))
        return buffer.getvalue()

    def test_pack_upload_errors(self):
        self.both("POST", "/api/modpacks/upload?name=..", ZIP)
        self.both("POST", "/api/modpacks/upload?name=pack", b"not a zip")
        self.both("POST", "/api/modpacks/upload?name=pack&token=../x", b"")
        self.both("POST", "/api/modpacks/upload?name=pack&token=" + "a" * 32, b"")
        self.both("POST", "/api/modpacks/upload?name=pack&token=" + "a" * 32, ZIP)
        from mc_host import panel
        self.assertTrue(panel._upload_lock.acquire(blocking=False))
        self.server.mock.upload_active = True
        try:
            self.both("POST", "/api/modpacks/upload?name=pack", ZIP)
            self.both("POST", "/api/backups/upload", ZIP)
        finally:
            panel._upload_lock.release()
            self.server.mock.upload_active = False

    def test_pack_upload_downgrade_answer(self):
        os.makedirs(installer.world_path("packworld"), exist_ok=True)
        installer._write_world_meta("packworld", {"newest_version": "1.21.4", "last_version": "1.21.4"})
        real_status, real = self.real("POST", "/api/modpacks/upload?name=pack&level_name=packworld", self.zip_with("1.20.1"))
        fake_status, fake = self.fake("POST", "/api/modpacks/upload?name=pack&level_name=survival", ZIP)
        self.assertEqual((real_status, fake_status), (409, 409))
        self.assertEqual((real["code"], fake["code"]), ("world_downgrade", "world_downgrade"))
        self.assertEqual(set(real), set(fake))
        self.assertEqual(set(real), {"error", "code", "upload_token"} | DOWNGRADE_DATA)
        self.assertRegex(fake["upload_token"], r"^[0-9a-f]{32}$")
        # the confirmation is accepted the same way
        stop = installer.InstallError("stop here", code="install_failed")
        with mock.patch.object(ix, "stage_modpack_archive", side_effect=stop):
            real_confirm = self.real("POST", "/api/modpacks/upload?name=pack&level_name=packworld&allow_downgrade=1&token="
                                     + real["upload_token"])
            tasks.wait_idle(5)
        fake_confirm = self.fake("POST", "/api/modpacks/upload?name=pack&level_name=survival&allow_downgrade=1&token="
                                 + fake["upload_token"])
        self.assertEqual((real_confirm[0], fake_confirm[0]), (202, 202))
        self.assertEqual(set(real_confirm[1]["task"]), set(fake_confirm[1]["task"]))

    def test_backup_upload_answers(self):
        archive = world_archive("survival", meta={"world": "survival", "kind": "manual", "last_version": "1.21.4"})
        with mock.patch("builtins.print"):  # the panel logs why it refused a file
            self.backup_upload_answers(archive)

    def backup_upload_answers(self, archive):
        real, fake = self.both("POST", "/api/backups/upload", archive)
        self.assertEqual((real[0], fake[0]), (201, 201))
        self.assertEqual(set(real[1]["backup"]), set(fake[1]["backup"]))
        self.assertEqual(set(real[1]["backups"][0]), set(fake[1]["backups"][0]))
        self.both("POST", "/api/backups/upload", b"PK a zip")
        self.both("POST", "/api/backups/upload", world_archive(level=False))
        self.both("POST", "/api/backups/upload", gzip.compress(b"hello"))

    def test_the_upload_size_limit_is_413_for_both(self):
        with mock.patch.dict(os.environ, {"MAX_UPLOAD_MB": "1"}):
            self.server.mock.max_upload = 1024 * 1024
            body = b"x" * (2 * 1024 * 1024)
            for path in ("/api/modpacks/upload?name=pack", "/api/backups/upload"):
                real = self.api("POST", path, headers={"Content-Length": str(len(body))}, body=b"")
                fake = self.fake("POST", path, headers={"Content-Length": str(len(body))})
                self.assertEqual((real[0], real[1]["code"]), (fake[0], fake[1]["code"]), path)
                self.assertEqual(real[0], 413)


class MockHttpTests(unittest.TestCase):
    """The handler-level rules: logout, the sign-in page and uploads."""

    def setUp(self) -> None:
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        self.http = http.client
        args = types.SimpleNamespace(phase="online", quiet=True, no_auth=False, key="admin",
                                     latency=0, upload_kbps=65536, web_dir=os.path.join(ROOT, "web"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mock_server.Handler)
        self.server.daemon_threads = True
        self.server.mock = mock_server.Mock(args)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = None

    def request(self, method, path, body=b"", headers=None):
        conn = self.http.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if self.cookie:
            sent["Cookie"] = f"rp_session={self.cookie}"
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def sign_in(self):
        response, _ = self.request("POST", "/login", b"key=admin", {"Content-Type": "application/x-www-form-urlencoded"})
        self.cookie = response.getheader("Set-Cookie").split(";", 1)[0].split("=", 1)[1]

    def test_login_page_renders_the_error_and_redirects_when_signed_in(self):
        response, data = self.request("GET", "/login?error=1")
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"{{error}}", data)
        self.assertIn(b"didn&#x27;t match", data)
        response, data = self.request("GET", "/login")
        self.assertIn(b'data-message=""', data)
        self.sign_in()
        response, _ = self.request("GET", "/login")
        self.assertEqual((response.status, response.getheader("Location")), (302, "/"))

    def test_logout_rules_match_the_panel(self):
        self.sign_in()
        response, _ = self.request("GET", "/api/logout")
        self.assertEqual((response.status, response.getheader("Location")), (302, "/"))
        self.assertEqual(self.request("GET", "/api/status")[0].status, 200, "a GET must not sign out")
        response, _ = self.request("POST", "/logout", b"x_railpowered=1", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status, 403, "a form field alone is not proof")
        response, _ = self.request("POST", "/logout", b"", {"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/logout", b"", {"X-Railpowered": "1"})
        self.assertEqual((response.status, response.getheader("Location")), (302, "/login"))
        self.assertEqual(self.request("GET", "/api/status")[0].status, 401)

    def test_uploads_are_checked_before_the_body(self):
        self.sign_in()
        self.server.mock.max_upload = 1024
        response, data = self.request("POST", "/api/modpacks/upload?name=pack&filename=pack.mrpack", b"x" * 2048,
                                      {"X-Railpowered": "1", "Content-Type": "application/octet-stream"})
        self.assertEqual(response.status, 413)
        self.assertIn(b"too_large", data)


class MockUploadAndHeaderTests(unittest.TestCase):
    """Uploads, security headers and static files, over a real socket."""

    def setUp(self) -> None:
        self.server = start_mock_http(self)
        self.mock = self.server.mock

    def api(self, method, path, body=b"", headers=None):
        status, payload, _ = mock_http(self.server, method, path, body, headers)
        return status, payload

    def upload_pack(self, query, body=ZIP):
        return self.api("POST", "/api/modpacks/upload?" + query, body, {"Content-Type": "application/octet-stream"})

    def finish(self):
        with self.mock.lock:
            finish_events(self.mock)

    def task(self):
        return self.api("GET", "/api/tasks/current")[1]["task"]

    def hook(self, query):
        self.assertEqual(self.api("GET", "/__mock/" + query)[0], 200)

    # ---- headers and files ----------------------------------------------

    def test_security_headers_are_the_panels(self):
        self.assertEqual(mock_server.CSP, web.CSP)
        self.assertEqual(mock_server.SECURITY_HEADERS, web.SECURITY_HEADERS)
        self.assertNotIn("unsafe-inline", mock_server.CSP)
        self.assertNotIn("google", mock_server.CSP)
        for path in ("/login", "/", "/api/status", "/static/app.js", "/static/assets/fonts/nope.woff2", "/nothing", "/health"):
            _, _, response = mock_http(self.server, "GET", path)
            for key, value in web.SECURITY_HEADERS.items():
                self.assertEqual(response.getheader(key), value, (path, key))
            self.assertEqual(response.getheader("Server"), "Railpowered", path)
            self.assertIsNone(response.getheader("Strict-Transport-Security"), path)
        _, _, response = mock_http(self.server, "GET", "/health", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.getheader("Strict-Transport-Security"), mock_server.HSTS)

    def test_pages_fail_as_plain_text_and_options_is_refused(self):
        status, payload, response = mock_http(self.server, "GET", "/nothing")
        self.assertEqual((status, response.getheader("Content-Type")), (404, "text/plain; charset=utf-8"))
        self.assertIsNone(payload)
        status, payload, response = mock_http(self.server, "OPTIONS", "/api/status")
        self.assertEqual((status, payload["code"]), (405, "method_not_allowed"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def test_static_files_follow_web_static_file(self):
        web_dir = os.path.join(ROOT, "web")
        font = "assets/fonts/IBMPlexSans-Regular-Latin1.woff2"
        self.assertTrue(os.path.isfile(os.path.join(web_dir, font)))
        with mock.patch.dict(os.environ, {"WEB_DIR": web_dir}):
            for relative in ("app.js", "app.css", "assets/logo.svg", font, "assets/fonts/OFL.txt",
                             "assets/fonts/nope.woff2", "assets/fonts/sub/x.woff2", "fonts/IBMPlexSans-Regular-Latin1.woff2",
                             "assets/fonts/", "assets/../app.js", "..", ".hidden", "a/b", "app.exe", "assets/fonts/..%2fapp.js"):
                real = web.static_file(relative)
                fake = mock_server.static_file(web_dir, relative)
                self.assertEqual(real is None, fake is None, relative)
                if real:
                    self.assertEqual((os.path.normcase(real[0]), real[1]), (os.path.normcase(fake[0]), fake[1]), relative)
        status, _, response = mock_http(self.server, "GET", "/static/" + font)
        self.assertEqual((status, response.getheader("Content-Type")), (200, "font/woff2"))
        self.assertEqual(mock_http(self.server, "GET", "/static/assets/fonts/sub/x.woff2")[0], 404)
        self.assertEqual(mock_http(self.server, "GET", "/static/fonts/IBMPlexSans-Regular-Latin1.woff2")[0], 404)

    # ---- pack uploads ----------------------------------------------------

    def test_a_pack_upload_into_a_newer_world_is_held_and_confirmed_with_its_token(self):
        status, payload = self.upload_pack("name=pack&level_name=survival&filename=p.zip")
        self.assertEqual((status, payload["code"]), (409, "world_downgrade"))
        self.assertEqual(set(payload), {"error", "code", "upload_token"} | DOWNGRADE_DATA)
        token = payload["upload_token"]
        self.assertIn(token, self.mock.held)
        self.assertIsNone(self.task(), "refused before any task")
        confirm = f"name=pack&level_name=survival&allow_downgrade=1&token={token}"
        status, payload = self.upload_pack(confirm, ZIP)
        self.assertEqual((status, payload["code"]), (400, "invalid"), "a kept upload is confirmed with an empty body")
        status, payload = self.upload_pack("name=pack&token=../x", b"")
        self.assertEqual((status, payload["code"]), (400, "invalid"))
        status, payload = self.upload_pack(confirm, b"")
        self.assertEqual((status, payload["task"]["kind"]), (202, "modpack"))
        self.finish()
        self.assertEqual(self.task()["state"], "done")
        status, payload = self.upload_pack(confirm, b"")
        self.assertEqual((status, payload["code"]), (410, "upload_expired"), "tokens are single use")

    def test_a_downgrade_found_inside_the_task_keeps_the_upload_too(self):
        status, _ = self.upload_pack("name=latepack&level_name=survival")
        self.assertEqual(status, 202)
        self.finish()
        task = self.task()
        self.assertEqual((task["state"], task["code"]), ("failed", "world_downgrade"))
        self.assertEqual(set(task["data"]), {"upload_token"} | DOWNGRADE_DATA)
        status, _ = self.upload_pack(f"name=latepack&level_name=survival&allow_downgrade=1&token={task['data']['upload_token']}", b"")
        self.assertEqual(status, 202)
        self.finish()
        self.assertEqual(self.task()["state"], "done")

    def test_upload_failures_can_be_triggered_for_the_next_upload(self):
        for code, status in (("disk_full", 507), ("too_slow", 408), ("busy", 409)):
            self.hook(f"upload-fail?code={code}")
            got = self.upload_pack("name=pack&level_name=fresh")
            self.assertEqual((got[0], got[1]["code"]), (status, code))
            self.assertFalse(self.mock.upload_active)
            got = self.upload_pack("name=pack&level_name=fresh")
            self.assertEqual(got[0], 202, "one-shot: the next upload works")
            self.finish()
        status, payload = self.upload_pack("name=pack&level_name=survival")
        self.hook("upload-fail?code=upload_expired")
        status, payload = self.upload_pack(f"name=pack&allow_downgrade=1&token={payload['upload_token']}", b"")
        self.assertEqual((status, payload["code"]), (410, "upload_expired"))

    def test_upload_rules_match_the_panels(self):
        self.assertEqual(self.upload_pack("name=pack", b"not a zip")[1]["code"], "invalid_archive")
        self.assertEqual(self.upload_pack("name=..")[1]["code"], "invalid_name")
        self.mock.upload_active = True
        self.assertEqual(self.upload_pack("name=pack")[1]["code"], "busy")
        self.assertEqual(self.api("POST", "/api/backups/upload", world_archive())[1]["code"], "busy")
        self.mock.upload_active = False
        self.mock.max_upload = 1024
        self.assertEqual(self.upload_pack("name=pack", b"x" * 2048)[0], 413)
        with self.assertRaises(mock_server.ApiError) as raised:
            mock_server.content_length(None, 10, required=True)
        self.assertEqual((raised.exception.status, raised.exception.code), (411, "length_required"))

    def test_a_running_task_refuses_an_upload_before_the_body(self):
        self.assertEqual(self.api("POST", "/api/backups", json.dumps({"world": "creative"}).encode())[0], 202)
        self.assertEqual(self.upload_pack("name=pack")[1]["code"], "busy")

    # ---- backup uploads ---------------------------------------------------

    def test_a_downloaded_backup_can_be_uploaded_again(self):
        backups = self.api("GET", "/api/backups")[1]["backups"]
        original = next(b for b in backups if b["world"] == "survival")
        status, _, response = mock_http(self.server, "GET", f"/api/backups/{original['name']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(response.body[:2], b"\x1f\x8b")
        status, payload = self.api("POST", "/api/backups/upload", response.body)
        self.assertEqual((status, set(payload)), (201, {"ok", "backup", "backups"}))
        self.assertEqual((payload["backup"]["world"], payload["backup"]["kind"], payload["backup"]["damaged"]),
                         ("survival", original["kind"], False))
        self.assertEqual(set(payload["backup"]), set(original))
        self.assertEqual(len(payload["backups"]), len(backups) + 1)
        self.assertIn(payload["backup"]["name"], [b["name"] for b in payload["backups"]])

    def test_backup_uploads_are_checked_like_the_panels(self):
        problems = {
            "not gzip": b"PK a zip",
            "gzip but not a tar": gzip.compress(b"hello"),
            "a tar without a world": world_archive(level=False),
        }
        for what, body in problems.items():
            status, payload = self.api("POST", "/api/backups/upload", body)
            self.assertEqual((status, payload["code"]), (400, "invalid_backup"), what)
        damaged = next(b for b in self.mock.backups if b["damaged"])
        body = mock_http(self.server, "GET", f"/api/backups/{damaged['name']}/download")[2].body
        status, payload = self.api("POST", "/api/backups/upload", body)
        self.assertEqual((status, payload["code"]), (400, "invalid_backup"))
        for code, expected in (("disk_full", 507), ("too_slow", 408), ("busy", 409)):
            self.hook(f"upload-fail?code={code}")
            status, payload = self.api("POST", "/api/backups/upload", world_archive())
            self.assertEqual((status, payload["code"]), (expected, code))
        self.assertEqual(self.api("POST", "/api/backups/upload", world_archive("zeta"))[0], 201)
        self.mock.max_upload = 100
        self.assertEqual(self.api("POST", "/api/backups/upload", world_archive())[1]["code"], "too_large")

    # ---- sign out everywhere ---------------------------------------------

    def test_revoke_all_clears_the_cookie_and_the_next_poll_is_a_401(self):
        server = start_mock_http(self, no_auth=False)
        response = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        response.request("POST", "/login", body=b"key=admin", headers={"Content-Type": "application/x-www-form-urlencoded"})
        cookie = response.getresponse().getheader("Set-Cookie").split(";", 1)[0].split("=", 1)[1]
        response.close()
        self.assertEqual(mock_http(server, "GET", "/api/status", cookie=cookie)[0], 200)
        status, payload, answer = mock_http(server, "POST", "/api/sessions/revoke-all", cookie=cookie)
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertIn("Max-Age=0", answer.getheader("Set-Cookie"))
        self.assertEqual(mock_http(server, "GET", "/api/status", cookie=cookie)[0], 401)

    def test_revoke_all_also_ends_a_no_auth_session_until_the_key_is_entered(self):
        self.assertEqual(self.api("GET", "/api/status")[0], 200)
        self.assertEqual(self.api("POST", "/api/sessions/revoke-all")[0], 200)
        self.assertEqual(self.api("GET", "/api/status")[0], 401)


if __name__ == "__main__":
    unittest.main()
