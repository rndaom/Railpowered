"""Dashboard HTTP API over a real server on an ephemeral port."""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import zipfile
from unittest import mock

from runtime_support import TEST_KEY, FakeProxy, RuntimeTestCase

import installer

from mc_host import auth, config, panel
from mc_host import installer_api as ix
from mc_host.panel import PanelHandler, PanelServer
from mc_host.process import AlreadyRunning, StartFailed, controller, tasks
from mc_host.state import state

STATUS_KEYS = {
    "phase", "task", "players", "player_count", "max_players", "uptime", "idle",
    "join_address", "setup", "java", "online_mode", "whitelist_enabled", "legacy",
    "last_exit", "product", "app_version",
}


class PanelTestCase(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        for target, attr, value in (
            (auth, "limiter", auth.RateLimiter()),
            (auth, "sessions", auth.SessionManager()),
            (controller, "proxy", FakeProxy()),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        panel._versions_cache.update({"at": 0.0, "data": None, "ttl": 0.0})
        self.web_dir = os.path.join(self.data_dir, "web")
        os.makedirs(os.path.join(self.web_dir, "assets"))
        with open(os.path.join(self.web_dir, "index.html"), "w", encoding="utf-8") as handle:
            handle.write('<!doctype html><title>Dashboard</title><script src="/static/app.js"></script>')
        with open(os.path.join(self.web_dir, "login.html"), "w", encoding="utf-8") as handle:
            handle.write('<form method="post" action="/login"><p class="error">{{error}}</p></form>')
        with open(os.path.join(self.web_dir, "app.js"), "w", encoding="utf-8") as handle:
            handle.write("console.log('hi');")
        with open(os.path.join(self.web_dir, "assets", "logo.svg"), "w", encoding="utf-8") as handle:
            handle.write("<svg xmlns='http://www.w3.org/2000/svg'/>")
        with open(os.path.join(self.data_dir, "secret.txt"), "w", encoding="utf-8") as handle:
            handle.write("top secret")
        os.environ["WEB_DIR"] = self.web_dir
        # A volume that has been prepared once: the active setup's world exists.
        os.makedirs(installer.world_path("world"), exist_ok=True)

        self.server = PanelServer(("127.0.0.1", 0), PanelHandler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server)
        self.addCleanup(tasks.wait_idle, 10)
        self.cookie: str | None = None

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- client helpers ------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        json_body=None,
        headers: dict | None = None,
        cookie: bool = True,
        csrf: bool = True,
    ):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        sent = {}
        if cookie and self.cookie:
            sent["Cookie"] = f"rp_session={self.cookie}"
        if csrf and method not in ("GET", "HEAD"):
            sent["X-Railpowered"] = "1"
        if json_body is not None:
            body = json.dumps(json_body).encode()
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def api(self, method: str, path: str, json_body=None, **kwargs):
        response, data = self.request(method, path, json_body=json_body, **kwargs)
        payload = json.loads(data) if data else None
        return response.status, payload

    def login(self, key: str = TEST_KEY, headers: dict | None = None):
        body = f"key={key}".encode()
        response, _ = self.request(
            "POST", "/login", body=body, cookie=False, csrf=False,
            headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        )
        set_cookie = response.getheader("Set-Cookie") or ""
        if set_cookie.startswith("rp_session=") and "Max-Age=0" not in set_cookie:
            self.cookie = set_cookie.split(";", 1)[0].split("=", 1)[1]
        return response


class PagesAndAuthTests(PanelTestCase):
    def test_health_needs_no_auth_and_supports_head(self):
        status, payload = self.api("GET", "/health", cookie=False)
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        response, data = self.request("HEAD", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"")
        self.assertEqual(response.getheader("Content-Length"), str(len(b'{"status":"ok"}')))

    def test_root_redirects_to_login_until_signed_in(self):
        response, _ = self.request("GET", "/")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.getheader("Location"), "/login")
        response, body = self.request("GET", "/login?error=1")
        self.assertEqual(response.status, 200)
        self.assertIn(b"didn&#x27;t match", body)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.login()
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"<title>Dashboard</title>", body)
        # Assets are linked by content hash so a redeploy can't serve a stale app.js.
        self.assertRegex(body.decode(), r'src="/static/app\.js\?v=[0-9a-f]{12}"')
        response, _ = self.request("GET", "/login")
        self.assertEqual((response.status, response.getheader("Location")), (302, "/"))

    def test_unknown_error_param_renders_nothing(self):
        _, body = self.request("GET", "/login?error=<script>")
        self.assertNotIn(b"<script>", body)

    def test_fallback_pages_when_web_dir_missing(self):
        os.environ["WEB_DIR"] = os.path.join(self.data_dir, "nope")
        with mock.patch.object(config, "resolve_web_dir", return_value=None):
            response, body = self.request("GET", "/login")
            self.assertEqual(response.status, 200)
            self.assertIn(b'action="/login"', body)

    def test_login_success_sets_signed_lax_cookie(self):
        response = self.login()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/")
        cookie = response.getheader("Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertTrue(self.cookie.startswith("v1."))
        self.assertEqual(len(self.cookie.split(".")), 4)
        self.assertEqual(self.api("GET", "/api/status")[0], 200)

    def test_secure_flag_behind_https_proxy(self):
        response = self.login(headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", response.getheader("Set-Cookie"))

    def test_pasted_key_with_whitespace_is_accepted(self):
        response = self.login(key=f"%20{TEST_KEY}%0A")
        self.assertEqual(response.getheader("Location"), "/")

    def test_wrong_key_redirects_with_error_and_no_cookie(self):
        response = self.login(key="nope")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/login?error=1")
        self.assertIsNone(response.getheader("Set-Cookie"))
        self.assertTrue(any("Failed dashboard login" in e["text"] for e in state.logs_after()["lines"]))

    def test_legacy_login_path_still_works(self):
        response, _ = self.request(
            "POST", "/api/login", body=f"key={TEST_KEY}".encode(), csrf=False,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.getheader("Location"), "/")

    def test_rate_limit_per_client_ip_uses_last_forwarded_hop(self):
        attacker = {"X-Forwarded-For": "6.6.6.6, 10.0.0.9"}
        for _ in range(10):
            self.assertEqual(self.login("bad", headers=attacker).getheader("Location"), "/login?error=1"
                             if _ < 9 else "/login?error=rate")
        response = self.login(TEST_KEY, headers=attacker)
        self.assertEqual(response.getheader("Location"), "/login?error=rate")
        self.assertIsNone(response.getheader("Set-Cookie"))
        # Spoofing the first hop does not help; a different real client is fine.
        response = self.login(TEST_KEY, headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.9"})
        self.assertEqual(response.getheader("Location"), "/login?error=rate")
        response = self.login(TEST_KEY, headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.10"})
        self.assertEqual(response.getheader("Location"), "/")

    def test_global_failure_ceiling(self):
        limiter = auth.RateLimiter(per_ip=100, global_limit=3)
        with mock.patch.object(auth, "limiter", limiter):
            for index in range(3):
                self.login("bad", headers={"X-Forwarded-For": f"9.9.9.{index}"})
            response = self.login(TEST_KEY, headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(response.getheader("Location"), "/login?error=rate")

    def test_tampered_or_foreign_cookies_are_rejected(self):
        self.login()
        issued, nonce, signature = self.cookie.split(".")[1:]
        for bad in (
            f"v1.{issued}.{nonce}.{'0' * 64}",
            f"v1.{int(issued) + 1}.{nonce}.{signature}",
            "admin",
            "",
        ):
            self.cookie = bad
            self.assertEqual(self.api("GET", "/api/status")[0], 401)
        foreign = auth.SessionManager(key_source=lambda: "another-key")
        self.cookie = foreign.issue()
        self.assertEqual(self.api("GET", "/api/status")[0], 401)

    def test_changing_admin_key_signs_everyone_out(self):
        self.login()
        os.environ["ADMIN_KEY"] = "a-brand-new-key-123"
        self.assertEqual(self.api("GET", "/api/status")[0], 401)

    def test_expired_session_is_rejected(self):
        old = auth.SessionManager(clock=lambda: time.time() - 31 * 24 * 3600)
        self.cookie = old.issue()
        self.assertEqual(self.api("GET", "/api/status")[0], 401)

    def test_logout_is_a_post_that_revokes_the_session(self):
        self.login()
        token = self.cookie
        response, _ = self.request("POST", "/logout", csrf=False)
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/logout")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/login")
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        self.cookie = token
        self.assertEqual(self.api("GET", "/api/status")[0], 401)

    def test_logout_form_post_from_same_origin(self):
        self.login()
        response, _ = self.request(
            "POST", "/logout", csrf=False,
            headers={"Origin": f"http://127.0.0.1:{self.port}", "Host": f"127.0.0.1:{self.port}"},
        )
        self.assertEqual(response.status, 303)

    def test_get_api_logout_changes_nothing(self):
        self.login()
        response, _ = self.request("GET", "/api/logout")
        self.assertEqual(response.status, 302)
        self.assertIsNone(response.getheader("Set-Cookie"))
        self.assertEqual(self.api("GET", "/api/status")[0], 200)


class ApiBasicsTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    def test_unauthenticated_api_is_json_401(self):
        self.cookie = None
        for method, path in (("GET", "/api/status"), ("POST", "/api/server/start"),
                             ("DELETE", "/api/worlds/world"), ("GET", "/api/nope")):
            status, payload = self.api(method, path)
            self.assertEqual(status, 401, path)
            self.assertEqual(payload["code"], "unauthorized")

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.api("GET", "/api/nope"), (404, {"error": "Not found.", "code": "not_found"}))
        status, payload = self.api("DELETE", "/api/status")
        self.assertEqual((status, payload["code"]), (405, "method_not_allowed"))

    def test_csrf_header_required_for_state_changes(self):
        status, payload = self.api("POST", "/api/server/stop", csrf=False)
        self.assertEqual((status, payload["code"]), (403, "csrf"))
        status, payload = self.api(
            "POST", "/api/server/stop", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual((status, payload["code"]), (403, "csrf"))

    def test_status_shape(self):
        response, data = self.request("GET", "/api/status")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        payload = json.loads(data)
        self.assertEqual(set(payload), STATUS_KEYS)
        self.assertEqual(payload["phase"], "sleeping")
        self.assertEqual(set(payload["setup"]), {
            "id", "name", "type", "minecraft_version", "resolved_version", "level_name", "instance"
        })
        self.assertEqual(set(payload["idle"]), {"timeout_seconds", "seconds_until_sleep"})
        self.assertIsNone(payload["idle"]["seconds_until_sleep"])
        self.assertEqual(payload["app_version"], config.APP_VERSION)
        self.assertEqual(payload["product"], "Railpowered")
        self.assertEqual(payload["players"], [])

    def test_head_on_api_route(self):
        response, data = self.request("HEAD", "/api/status")
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"")
        self.assertGreater(int(response.getheader("Content-Length")), 10)

    def test_body_limits_and_bad_bodies(self):
        status, payload = self.api(
            "POST", "/api/console", headers={"Content-Length": "200000"}, body=b""
        )
        self.assertEqual((status, payload["code"]), (413, "too_large"))
        for length in ("-1", "abc"):
            status, payload = self.api(
                "POST", "/api/console", headers={"Content-Length": length}, body=b""
            )
            self.assertEqual((status, payload["code"]), (400, "bad_length"))
        response, data = self.request("POST", "/api/console", body=b"{not json")
        self.assertEqual((response.status, json.loads(data)["code"]), (400, "bad_json"))
        response, data = self.request("POST", "/api/console", body=b"command=op x")
        self.assertEqual(response.status, 400)

    def test_login_body_limit(self):
        response, _ = self.request(
            "POST", "/login", body=b"key=" + b"a" * 10000, cookie=False, csrf=False
        )
        self.assertEqual(response.getheader("Location"), "/login?error=1")

    def test_unexpected_errors_are_generic(self):
        with mock.patch.object(ix, "list_worlds", side_effect=RuntimeError("secret detail")):
            with mock.patch("builtins.print"):
                status, payload = self.api("GET", "/api/worlds")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "internal")
        self.assertNotIn("secret", payload["error"])

    def test_logs_after(self):
        first = state.add_log("alpha")
        state.add_log("beta", "warn")
        status, payload = self.api("GET", f"/api/logs?after={first}")
        self.assertEqual(status, 200)
        texts = [line["text"] for line in payload["lines"]]
        self.assertEqual(texts[0], "beta")
        self.assertEqual(payload["lines"][0]["level"], "warn")
        self.assertEqual(payload["lines"][0]["source"], "panel")
        self.assertEqual(set(payload["lines"][0]), {"seq", "t", "text", "level", "source"})
        self.assertGreaterEqual(payload["last_seq"], first + 1)
        self.assertEqual(self.api("GET", "/api/logs?after=x")[0], 400)

    def test_admin_key_never_reaches_the_dashboard_log(self):
        texts = " ".join(line["text"] for line in state.logs_after()["lines"])
        self.assertNotIn(TEST_KEY, texts)


class StaticTests(PanelTestCase):
    def test_serves_allowed_static_files(self):
        response, data = self.request("GET", "/static/app.js", cookie=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/javascript; charset=utf-8")
        self.assertEqual(response.getheader("Cache-Control"), "no-cache")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(data, b"console.log('hi');")
        response, _ = self.request("GET", "/static/app.js?v=0123456789ab", cookie=False)
        self.assertEqual(response.getheader("Cache-Control"), "public, max-age=31536000, immutable")
        response, _ = self.request("GET", "/static/assets/logo.svg")
        self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")

    def test_traversal_and_odd_names_are_404(self):
        for path in (
            "/static/../secret.txt",
            "/static/%2e%2e/secret.txt",
            "/static/..%2fsecret.txt",
            "/static/assets/../../secret.txt",
            "/static/.hidden.js",
            "/static/a/b/c.js",
            "/static/index.py",
            "/static/",
            "/static/assets/",
            "/static//etc/passwd",
            "/static/C:%5cwindows%5cwin.ini",
        ):
            response, data = self.request("GET", path)
            self.assertEqual(response.status, 404, path)
            self.assertNotIn(b"top secret", data)

    def test_favicon(self):
        response, _ = self.request("GET", "/favicon.ico")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")


class ServerControlTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    def test_start_ok_conflict_and_failure(self):
        with mock.patch.object(controller, "start") as start:
            self.assertEqual(self.api("POST", "/api/server/start"), (200, {"ok": True}))
            start.side_effect = AlreadyRunning("The server is already running.")
            status, payload = self.api("POST", "/api/server/start")
            self.assertEqual((status, payload["code"]), (409, "already_running"))
            start.side_effect = StartFailed("Mojang is unreachable")
            status, payload = self.api("POST", "/api/server/start")
            self.assertEqual(status, 500)
            self.assertEqual(payload, {"error": "Mojang is unreachable", "code": "start_failed"})

    def test_stop_when_not_running(self):
        status, payload = self.api("POST", "/api/server/stop")
        self.assertEqual((status, payload["code"]), (409, "not_running"))

    def test_save_requires_online(self):
        status, payload = self.api("POST", "/api/server/save")
        self.assertEqual((status, payload["code"]), (409, "not_online"))

    def test_console_validation(self):
        status, payload = self.api("POST", "/api/console", {"command": "say hi\nop Mallory"})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/console", {"command": "   "})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/console", {"command": "x" * 1001})
        self.assertEqual(status, 400)
        status, payload = self.api("POST", "/api/console", {"command": "say hi"})
        self.assertEqual((status, payload["code"]), (409, "not_running"))
        with mock.patch.object(controller, "send_command", return_value=True) as send:
            self.assertEqual(self.api("POST", "/api/console", {"command": "/op Steve"})[0], 200)
        send.assert_called_once_with("op Steve")

    def test_task_flow_202_then_409_then_done(self):
        release = threading.Event()

        def slow_backup(world):
            release.wait(5)
            return {"name": f"{world}.tar.gz"}

        with mock.patch.object(controller, "backup_world", side_effect=slow_backup):
            status, payload = self.api("POST", "/api/backups", {"world": "world"})
            self.assertEqual(status, 202)
            task = payload["task"]
            self.assertEqual((task["kind"], task["state"]), ("backup", "running"))
            self.assertEqual(
                set(task), {"id", "kind", "label", "state", "error", "started_at", "finished_at"}
            )
            status, payload = self.api("POST", "/api/backups", {"world": "world"})
            self.assertEqual((status, payload["code"]), (409, "busy"))
            status, payload = self.api("POST", "/api/server/start")
            self.assertEqual((status, payload["code"]), (409, "busy"))
            self.assertEqual(self.api("GET", "/api/status")[1]["phase"], "preparing")
            self.assertEqual(self.api("GET", "/api/tasks/current")[1]["task"]["id"], task["id"])
            release.set()
            self.assertTrue(tasks.wait_idle(5))
        status, payload = self.api("GET", "/api/status")
        self.assertEqual(payload["task"]["state"], "done")
        self.assertEqual(payload["task"]["id"], task["id"])

    def test_failed_task_reports_plain_error(self):
        with mock.patch.object(
            controller, "backup_world", side_effect=installer.InstallError("Not enough disk space.")
        ):
            self.assertEqual(self.api("POST", "/api/backups", {})[0], 202)
            tasks.wait_idle(5)
        task = self.api("GET", "/api/tasks/current")[1]["task"]
        self.assertEqual((task["state"], task["error"]), ("failed", "Not enough disk space."))


class SetupRouteTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    def test_create_setup_validation(self):
        cases = [
            ({"type": "bukkit", "minecraft_version": "1.20.1"}, 400),
            ({"type": "modpack", "minecraft_version": "1.20.1"}, 400),
            ({"type": "vanilla", "minecraft_version": "../../x"}, 400),
            ({"type": "forge", "minecraft_version": "latest"}, 400),
            ({"type": "forge", "minecraft_version": "1.19.2"}, 400),
            ({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": ".."}, 400),
            ({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "world"}, 409),
            ({"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "nowhere",
              "create_world": False}, 404),
            ({"type": "vanilla", "minecraft_version": "1.20.1", "activate": "yes"}, 400),
        ]
        for body, expected in cases:
            self.assertEqual(self.api("POST", "/api/setups", body)[0], expected, body)

    def test_create_setup_runs_as_task_with_fresh_world_name(self):
        with mock.patch.object(ix, "create_setup") as create:
            status, payload = self.api(
                "POST", "/api/setups", {"type": "fabric", "minecraft_version": "1.20.1"}
            )
            self.assertEqual(status, 202)
            self.assertEqual(payload["task"]["kind"], "apply")
            self.assertEqual(payload["task"]["label"], "Installing Fabric 1.20.1")
            tasks.wait_idle(5)
        fields = create.call_args[0][0]
        self.assertEqual(fields["level_name"], "fabric-1_20_1")
        self.assertTrue(fields["create_world"])
        self.assertEqual(create.call_args[1], {"allow_downgrade": False, "activate": True})

    def test_downgrade_guard_is_checked_before_the_task(self):
        installer.record_world_version("world", "1.21.1", "vanilla")
        status, payload = self.api(
            "POST", "/api/setups",
            {"type": "vanilla", "minecraft_version": "1.20.1", "level_name": "world",
             "create_world": False},
        )
        self.assertEqual((status, payload["code"]), (409, "world_downgrade"))
        # The dialog names the world and both versions.
        self.assertEqual(
            (payload["world"], payload["last_version"], payload["version"]),
            ("world", "1.21.1", "1.20.1"),
        )
        self.assertIsNone(tasks.running())
        active = self.api("GET", "/api/setups")[1]["setups"][0]
        with mock.patch.object(ix, "activate_setup"):
            other = installer.load_config()
            other["profiles"].append({**other["profiles"][0], "id": "old", "name": "Old",
                                      "minecraft_version": "1.20.1", "resolved_version": "1.20.1"})
            installer.save_config(other)
            status, payload = self.api("POST", "/api/setups/old/activate", {})
            self.assertEqual((status, payload["code"]), (409, "world_downgrade"))
            status, payload = self.api("POST", "/api/setups/old/activate", {"allow_downgrade": True})
            self.assertEqual(status, 202)
            tasks.wait_idle(5)
        self.assertTrue(active["active"])

    def test_setup_list_activate_and_delete(self):
        status, payload = self.api("GET", "/api/setups")
        self.assertEqual(status, 200)
        setups = payload["setups"]
        self.assertTrue(setups)
        active = next(item for item in setups if item["active"])
        self.assertEqual(
            set(active),
            {"id", "name", "type", "minecraft_version", "resolved_version", "level_name",
             "instance", "active", "summary"},
        )
        status, payload = self.api("DELETE", f"/api/setups/{active['id']}")
        self.assertEqual((status, payload["code"]), (409, "active_setup"))
        self.assertEqual(self.api("DELETE", "/api/setups/missing")[0], 404)
        status, payload = self.api("POST", f"/api/setups/{active['id']}/activate", {})
        self.assertEqual((status, payload["already_active"]), (200, True))

    def test_rename_setup(self):
        active = self.api("GET", "/api/setups")[1]["setups"][0]
        status, payload = self.api("PATCH", f"/api/setups/{active['id']}", {"name": "Main world"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["setups"][0]["name"], "Main world")
        self.assertEqual(self.api("PATCH", f"/api/setups/{active['id']}", {"name": ""})[0], 400)

    def test_versions_fall_back_when_offline(self):
        with mock.patch.object(ix, "list_available_versions",
                               side_effect=urllib.error.URLError("offline")):
            with mock.patch("builtins.print"):
                status, payload = self.api("GET", "/api/versions")
        self.assertEqual(status, 200)
        self.assertTrue(payload["offline"])
        self.assertTrue(payload["vanilla"])
        self.assertEqual(set(payload["vanilla"][0]), {"id", "label"})


class ModpackRouteTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    def _zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("modrinth.index.json", "{}")
        return buffer.getvalue()

    def test_url_validation(self):
        for body in (
            {"url": "", "name": "pack"},
            {"url": "file:///server/data/.admin-key", "name": "pack"},
            {"url": "http://example.com/pack.mrpack", "name": "pack"},
            {"url": "https://example.com/pack.mrpack", "name": ".."},
            {"url": "https://example.com/pack.mrpack", "name": "my pack"},
        ):
            self.assertEqual(self.api("POST", "/api/modpacks", body)[0], 400, body)

    def test_url_install_task(self):
        fields = {"type": "modpack", "minecraft_version": "1.20.1", "level_name": "pack",
                  "instance": "pack"}
        with mock.patch.object(ix, "install_modpack_from_url", return_value=fields) as install, \
                mock.patch.object(ix, "create_setup") as create:
            status, payload = self.api(
                "POST", "/api/modpacks", {"url": "https://cdn.modrinth.com/x.mrpack", "name": "pack"}
            )
            self.assertEqual((status, payload["task"]["kind"]), (202, "modpack"))
            tasks.wait_idle(5)
        install.assert_called_once_with(
            "https://cdn.modrinth.com/x.mrpack", "pack", level_name="pack", allow_downgrade=False
        )
        self.assertEqual(create.call_args[0][0]["name"], "pack")
        self.assertEqual(create.call_args[1], {"allow_downgrade": False, "activate": True})

    def test_url_install_passes_allow_downgrade(self):
        fields = {"type": "modpack", "minecraft_version": "1.20.1", "level_name": "world",
                  "instance": "pack"}
        with mock.patch.object(ix, "install_modpack_from_url", return_value=fields) as install, \
                mock.patch.object(ix, "create_setup") as create:
            status, _ = self.api(
                "POST", "/api/modpacks",
                {"url": "https://cdn.modrinth.com/x.mrpack", "name": "pack", "level_name": "world",
                 "allow_downgrade": True},
            )
            self.assertEqual(status, 202)
            tasks.wait_idle(5)
        self.assertTrue(install.call_args[1]["allow_downgrade"])
        self.assertTrue(create.call_args[1]["allow_downgrade"])

    def test_upload_requires_length_and_limits_size(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /api/modpacks/upload?name=pack HTTP/1.0\r\n"
            + f"Cookie: rp_session={self.cookie}\r\nX-Railpowered: 1\r\n\r\n".encode()
        )
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
        self.assertIn(b" 411 ", reply.split(b"\r\n", 1)[0])
        with mock.patch.dict(os.environ, {"MAX_UPLOAD_MB": "1"}):
            status, payload = self.api(
                "POST", "/api/modpacks/upload?name=pack",
                headers={"Content-Length": str(2 * 1024 * 1024)}, body=b"",
            )
        self.assertEqual((status, payload["code"]), (413, "too_large"))

    def test_upload_rejects_bad_names_and_non_zip(self):
        status, payload = self.api("POST", "/api/modpacks/upload?name=..", body=b"PK\x03\x04")
        self.assertEqual((status, payload["code"]), (400, "invalid_name"))
        status, payload = self.api("POST", "/api/modpacks/upload?name=pack", body=b"not a zip")
        self.assertEqual((status, payload["code"]), (400, "invalid_archive"))
        leftovers = os.listdir(installer.paths()["modpacks"])
        self.assertEqual(leftovers, [])

    def test_upload_streams_to_disk_and_installs(self):
        seen = {}

        def install(path, name, level_name=None, allow_downgrade=False):
            with open(path, "rb") as handle:
                seen["data"] = handle.read()
            seen["args"] = (name, level_name, allow_downgrade)
            return {"type": "modpack", "minecraft_version": "1.20.1", "level_name": level_name,
                    "instance": name}

        data = self._zip_bytes()
        with mock.patch.object(ix, "install_modpack_archive", side_effect=install), \
                mock.patch.object(ix, "create_setup"):
            status, payload = self.api(
                "POST",
                "/api/modpacks/upload?name=pack&level_name=packworld&filename=My%20Pack.mrpack"
                "&allow_downgrade=1",
                body=data,
            )
            self.assertEqual(status, 202)
            tasks.wait_idle(5)
        self.assertEqual(seen["data"], data)
        self.assertEqual(seen["args"], ("pack", "packworld", True))
        self.assertEqual(os.listdir(installer.paths()["modpacks"]), [])


class WorldBackupRouteTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    def test_world_routes(self):
        status, payload = self.api("GET", "/api/worlds")
        self.assertEqual(status, 200)
        world = payload["worlds"][0]
        self.assertEqual(
            set(world), {"name", "size_bytes", "active", "last_version", "last_played", "used_by"}
        )
        self.assertEqual((world["name"], world["active"]), ("world", True))
        self.assertEqual(self.api("POST", "/api/worlds", {"name": "creative"})[0], 200)
        self.assertEqual(self.api("POST", "/api/worlds", {"name": "creative"})[0], 409)
        self.assertEqual(self.api("POST", "/api/worlds", {"name": "."})[0], 400)
        status, payload = self.api("DELETE", "/api/worlds/world")
        self.assertEqual((status, payload["code"]), (409, "active_world"))
        for bad in ("%2E%2E", "..", "a%2Fb"):
            self.assertIn(self.api("DELETE", f"/api/worlds/{bad}")[0], (400, 404), bad)
        self.assertTrue(os.path.isdir(installer.paths()["worlds"]))
        self.assertEqual(self.api("DELETE", "/api/worlds/missing")[0], 404)
        status, payload = self.api("DELETE", "/api/worlds/creative")
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed_setups"], [])

    def test_backup_list_download_and_delete(self):
        level = os.path.join(installer.world_path("world"), "level.dat")
        with open(level, "wb") as handle:
            handle.write(os.urandom(300_000))
        status, payload = self.api("POST", "/api/backups", {"world": "world"})
        self.assertEqual(status, 202)
        tasks.wait_idle(10)
        status, payload = self.api("GET", "/api/backups")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload["disk"]), {"used_bytes", "total_bytes", "free_bytes"})
        backup = payload["backups"][0]
        self.assertEqual(
            set(backup), {"name", "world", "size_bytes", "created_at", "minecraft_version", "kind"}
        )
        self.assertEqual(backup["kind"], "manual")
        self.assertEqual(backup["world"], "world")
        response, data = self.request("GET", f"/api/backups/{backup['name']}/download")
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.getheader("Content-Length")), len(data))
        self.assertEqual(len(data), backup["size_bytes"])
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        response, data = self.request("HEAD", f"/api/backups/{backup['name']}/download")
        self.assertEqual((response.status, data), (200, b""))
        self.assertEqual(self.api("GET", "/api/backups/..%2Fmanager.json/download")[0], 400)
        self.assertEqual(self.api("GET", "/api/backups/missing.tar.gz/download")[0], 404)
        self.assertEqual(self.api("DELETE", f"/api/backups/{backup['name']}")[0], 200)
        self.assertEqual(self.api("GET", "/api/backups")[1]["backups"], [])

    def test_restore_refuses_the_running_world(self):
        backup_dir = installer.paths()["backups"]
        with open(os.path.join(backup_dir, "world-20260101-000000.tar.gz"), "wb") as handle:
            handle.write(b"x")
        with mock.patch.object(controller, "current_world", return_value="world"):
            status, payload = self.api("POST", "/api/backups/world-20260101-000000.tar.gz/restore", {})
        self.assertEqual((status, payload["code"]), (409, "server_running"))
        with mock.patch.object(ix, "restore_backup") as restore:
            status, payload = self.api(
                "POST", "/api/backups/world-20260101-000000.tar.gz/restore", {"target": "world"}
            )
            self.assertEqual((status, payload["task"]["kind"]), (202, "restore"))
            tasks.wait_idle(5)
        restore.assert_called_once_with("world-20260101-000000.tar.gz", target="world")


class PlayersAndSettingsTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    @staticmethod
    def _console_reply(command: str) -> bool:
        # A running Minecraft answers `whitelist on|off`; the toggle waits for it.
        if command.startswith("whitelist o"):
            line = f"[12:00:00] [Server thread/INFO]: Whitelist is now turned {command[10:]}"
            with controller._waiters_lock:
                waiters = [w for w in controller._waiters if w.regex.search(line)]
            for waiter in waiters:
                waiter.event.set()
        return True

    def test_players_routes_validate_and_send_console_commands(self):
        status, payload = self.api("GET", "/api/players")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload), {"online", "ops", "whitelist", "whitelist_enabled", "online_mode"}
        )
        self.assertEqual(self.api("POST", "/api/players/ops", {"name": "x');alert(1)//"})[0], 400)
        self.assertEqual(self.api("POST", "/api/players/admins", {"name": "Steve"})[0], 404)
        with mock.patch.object(ix, "add_player", return_value={"name": "Steve", "uuid": "x"}) as add, \
                mock.patch.object(ix, "remove_player") as remove, \
                mock.patch.object(ix, "set_whitelist_enabled") as toggle, \
                mock.patch.object(controller, "send_command", side_effect=self._console_reply) as send:
            self.assertEqual(self.api("POST", "/api/players/ops", {"name": "Steve"})[0], 200)
            self.assertEqual(self.api("DELETE", "/api/players/whitelist/Alex")[0], 200)
            self.assertEqual(
                self.api("PUT", "/api/players/whitelist/enabled", {"enabled": True})[0], 200
            )
            self.assertEqual(
                self.api("PUT", "/api/players/whitelist/enabled", {"enabled": "yes"})[0], 400
            )
        add.assert_called_once_with("ops", "Steve")
        remove.assert_called_once_with("whitelist", "Alex")
        toggle.assert_called_once_with(True)
        self.assertEqual(
            [call.args[0] for call in send.call_args_list],
            ["op Steve", "whitelist remove Alex", "whitelist on"],
        )

    def test_whitelist_toggle_writes_both_properties(self):
        self.write_properties(motd="Keep me", **{"white_list": "false"})
        self.assertEqual(
            self.api("PUT", "/api/players/whitelist/enabled", {"enabled": True})[0], 200
        )
        props = installer.read_properties()
        self.assertEqual(props["white-list"], "true")
        self.assertEqual(props["enforce-whitelist"], "true")
        self.assertEqual(props["motd"], "Keep me")
        self.assertTrue(self.api("GET", "/api/status")[1]["whitelist_enabled"])

    def test_settings_roundtrip_preserves_other_keys(self):
        self.write_properties(motd="Old", **{"max_players": "20", "custom_key": "keep"})
        status, payload = self.api("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"properties", "railpowered", "restart_required", "schema"})
        self.assertIn("motd", payload["schema"])
        self.assertEqual(payload["properties"]["max-players"], 20)
        self.assertFalse(payload["restart_required"])
        status, payload = self.api(
            "PUT", "/api/settings",
            {"properties": {"motd": "Fresh", "max-players": 10, "pvp": False},
             "railpowered": {"idle_timeout_minutes": 15}},
        )
        self.assertEqual((status, payload), (200, {"ok": True, "restart_required": False}))
        props = installer.read_properties()
        self.assertEqual((props["motd"], props["max-players"], props["pvp"]), ("Fresh", "10", "false"))
        self.assertEqual(props["custom-key"], "keep")
        self.assertEqual(
            self.api("GET", "/api/settings")[1]["railpowered"]["idle_timeout_minutes"], 15
        )
        self.assertEqual(controller.idle_timeout_seconds(), 900)

    def test_settings_validation(self):
        for body in (
            {"properties": {"max-players": "ten"}},
            {"properties": {"max-players": 0}},
            {"properties": {"difficulty": "nightmare"}},
            {"properties": {"motd": "two\nlines"}},
            {"properties": {"rcon.password": "x"}},
            {"railpowered": {"idle_timeout_minutes": -1}},
            {"properties": []},
        ):
            self.assertEqual(self.api("PUT", "/api/settings", body)[0], 400, body)


if __name__ == "__main__":
    unittest.main()
