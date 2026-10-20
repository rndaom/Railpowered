"""tools/mock_server.py stays in step with the real dashboard API.

The dashboard is built and screenshotted against the mock, so a mock that
answers with other error codes or other response shapes hides real bugs (the
settings page that could never save on Minecraft 1.6.4 was one). These tests
compare the mock with mc_host/panel.py: error codes, response key sets, and
the behaviours the dashboard relies on.
"""

from __future__ import annotations

import importlib.util
import os
import re
import time
import types
import unittest
import urllib.error
from unittest import mock

from runtime_support import ROOT

import installer

from mc_host import installer_api as ix
from mc_host.process import tasks

from test_panel import STATUS_KEYS, PanelTestCase


def _load_mock():
    path = os.path.join(ROOT, "tools", "mock_server.py")
    spec = importlib.util.spec_from_file_location("railpowered_mock_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock_server = _load_mock()

# Fields the mock already sends that the backend lane is adding; the dashboard
# reads them when present and copes when they are missing.
AHEAD_OF_BACKEND = {
    "status": {"max_upload_bytes"},
    "backup": {"damaged"},
}


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
        self.assertIn("last played on Minecraft 26.3", task["error"])

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

    def test_legacy_scenario_reports_online_mode_outside_the_schema(self):
        m = new_mock("online", "legacy")
        status = call(m, "GET", "/api/status")[1]
        self.assertTrue(status["legacy"])
        self.assertFalse(status["online_mode"])
        payload = call(m, "GET", "/api/settings")[1]
        self.assertNotIn("online-mode", payload["schema"])
        self.assertNotIn("simulation-distance", payload["schema"])
        self.assertIs(payload["properties"]["online-mode"], False)
        # Sending every property back, as the old dashboard did, fails like the real server.
        status, body = call(m, "PUT", "/api/settings", {"properties": payload["properties"]})
        self.assertEqual((status, body["code"]), (400, "invalid_setting"))
        self.assertEqual(call(m, "PUT", "/api/settings", {"properties": {"motd": "Old times"}})[0], 200)

    def test_versions_can_be_offline(self):
        m = new_mock()
        self.assertIs(call(m, "GET", "/api/versions")[1]["offline"], False)
        m.hook("offline", {"set": ["1"]})
        self.assertIs(call(m, "GET", "/api/versions")[1]["offline"], True)


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
        self.assertSameKeys(real, fake, "status", AHEAD_OF_BACKEND["status"])
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
        self.assertSameKeys(real_backups["backups"][0], fake_backups["backups"][0], "backup", AHEAD_OF_BACKEND["backup"])

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
        self.assertSameKeys(real["vanilla"][0], fake["vanilla"][0], "versions.vanilla")

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
        self.assertEqual(set(mock_server.LOGIN_ERRORS), {"1", "rate"})
        from mc_host import web
        self.assertEqual(mock_server.LOGIN_ERRORS, {k: web.LOGIN_ERRORS[k] for k in mock_server.LOGIN_ERRORS})


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


if __name__ == "__main__":
    unittest.main()
