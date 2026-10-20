"""Dashboard HTTP server: pages, static files and the JSON API (contract §1).

Routing lives in ROUTES; each handler gets the regex match and query. Errors
are raised as ApiError / InstallError / ServerError / TaskBusy and turned
into {"error", "code"} JSON with the right status. Anything unexpected is a
generic 500 whose traceback goes to stdout (Railway logs) only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

import installer

from . import auth, config, web
from . import installer_api as ix
from .process import ServerError, controller, tasks
from .state import ONLINE, state
from .tasks import TaskBusy

JSON_LIMIT = 64 * 1024
LOGIN_LIMIT = 4 * 1024
CHUNK = 64 * 1024
CONSOLE_MAX = 1000
GENERIC_500 = "Something went wrong on the server. The log has the details."
CONFLICT_CODES = {
    "world_downgrade", "busy", "conflict", "exists", "already_exists", "world_exists",
    "setup_exists", "instance_exists", "active_setup", "active_world", "in_use",
    "running", "server_running", "already_running", "not_running", "not_online",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def install_error_status(code: str) -> int:
    if code == "not_found" or code.endswith("_not_found"):
        return 404
    if code in CONFLICT_CODES:
        return 409
    if code in ("too_large", "upload_too_large"):
        return 413
    return 400


# -- routing table -----------------------------------------------------------
Route = tuple[str, "re.Pattern[str]", str, bool]
ROUTES: list[Route] = []


def route(method: str, pattern: str, auth_required: bool = True) -> Callable[[Callable], Callable]:
    def register(fn: Callable) -> Callable:
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn.__name__, auth_required))
        return fn

    return register


_SEG = r"([^/]+)"


class PanelServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent connections."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64
    max_connections = 64

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


_versions_cache: dict[str, Any] = {"at": 0.0, "data": None, "ttl": 0.0}
_versions_lock = threading.Lock()


def _type_label(server_type: str) -> str:
    return {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge"}.get(server_type, server_type)


def _bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(400, "invalid", f"{key} must be true or false.")
    return value


def _str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ApiError(400, "invalid", f"{key} must be text.")
    return value.strip()


def _check_name(value: str, what: str = "World") -> str:
    if not ix.valid_name(value):
        raise ApiError(
            400,
            "invalid_name",
            f"{what} names use 1-32 letters, numbers, dots, dashes or underscores, "
            "and start with a letter or number.",
        )
    return value


def _check_player(value: str) -> str:
    if not ix.PLAYER_RE.match(value or ""):
        raise ApiError(400, "invalid_player", "Player names are 1-16 letters, numbers or underscores.")
    return value


def _require_idle_tasks() -> None:
    task = tasks.running()
    if task is not None:
        raise ApiError(409, "busy", f"Wait for “{task.label}” to finish first.")


def _restart_note(exc: ServerError) -> Exception:
    return ix.install_error(
        f"The change was saved, but the server could not start: {exc}", "start_failed"
    )


def _run_with_restart(change: Callable[[], Any], restart: bool, reason: str) -> Any:
    """Stop if online, apply `change`, then start again (old setup on failure)."""
    was_running = restart and controller.is_running()
    if was_running:
        controller.stop(ignore_task=True, reason=reason)
    try:
        result = change()
    except Exception:
        if was_running:
            try:
                controller.start(ignore_task=True, reason="back on the previous setup")
            except ServerError:
                pass
        raise
    if was_running:
        try:
            controller.start(ignore_task=True, reason=reason)
        except ServerError as exc:
            raise _restart_note(exc) from exc
    return result


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "Railpowered"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 30

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # -- dispatch ------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        self._head = method == "HEAD"
        self._headers_sent = False
        effective = "GET" if self._head else method
        url = urlsplit(self.path)
        path = url.path or "/"
        self.query = parse_qs(url.query, keep_blank_values=True)
        is_api = path.startswith("/api/")
        try:
            if is_api and path != "/api/login" and path != "/api/logout":
                self._api(effective, path)
            else:
                self._pages(effective, path)
        except ApiError as exc:
            self._fail(is_api, exc.status, exc.code, exc.message)
        except TaskBusy as exc:
            self._fail(is_api, 409, "busy", str(exc))
        except ServerError as exc:
            self._fail(is_api, exc.status, exc.code, str(exc))
        except installer.InstallError as exc:
            code = ix.error_code(exc)
            self._fail(is_api, install_error_status(code), code, str(exc) or "That didn't work.")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            print(traceback.format_exc(), flush=True)
            state.add_log(
                f"Dashboard request {method} {path} failed: {type(exc).__name__}", "error"
            )
            self._fail(is_api, 500, "internal", GENERIC_500)

    def _fail(self, is_api: bool, status: int, code: str, message: str) -> None:
        if self._headers_sent:
            self.close_connection = True
            return
        if is_api:
            self._json({"error": message, "code": code}, status)
        else:
            self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _api(self, method: str, path: str) -> None:
        allowed = False
        for route_method, pattern, handler_name, auth_required in ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            if route_method != method:
                allowed = True
                continue
            if auth_required and not self._authed():
                raise ApiError(401, "unauthorized", "Log in to continue.")
            if method != "GET":
                self._check_csrf()
            getattr(self, handler_name)(*[unquote(group) for group in match.groups()])
            return
        if not self._authed():
            raise ApiError(401, "unauthorized", "Log in to continue.")
        if allowed:
            raise ApiError(405, "method_not_allowed", "That action isn't supported here.")
        raise ApiError(404, "not_found", "Not found.")

    # -- auth helpers ----------------------------------------------------------
    def _token(self) -> str | None:
        return auth.cookie_value(self.headers.get("Cookie"))

    def _authed(self) -> bool:
        return auth.sessions.validate(self._token())

    def _secure(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"

    def _client_ip(self) -> str:
        return auth.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))

    def _same_origin(self) -> bool | None:
        """True/False when an Origin header is present, None when absent."""
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return None if not origin else False
        origin_host = urlsplit(origin).netloc.lower()
        hosts = {
            (self.headers.get("Host") or "").lower(),
            (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower(),
        }
        return origin_host in {host for host in hosts if host}

    def _check_csrf(self) -> None:
        if self.headers.get("X-Railpowered") != "1":
            raise ApiError(403, "csrf", "This request is missing the dashboard header.")
        if self._same_origin() is False:
            raise ApiError(403, "csrf", "This request came from another site.")

    # -- bodies ----------------------------------------------------------------
    def _content_length(self, limit: int, required: bool = False) -> int:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApiError(411, "length_required", "Send the body with a Content-Length.")
        raw = self.headers.get("Content-Length")
        if raw is None or raw.strip() == "":
            if required:
                raise ApiError(411, "length_required", "Send the body with a Content-Length.")
            return 0
        raw = raw.strip()
        if not raw.isdigit():
            self.close_connection = True
            raise ApiError(400, "bad_length", "Content-Length must be a number.")
        length = int(raw)
        if length > limit:
            self.close_connection = True
            limit_text = f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"
            raise ApiError(413, "too_large", f"That request is too large (limit {limit_text}).")
        return length

    def _body(self, limit: int) -> bytes:
        length = self._content_length(limit)
        if length == 0:
            return b""
        data = self.rfile.read(length)
        if len(data) != length:
            self.close_connection = True
            raise ApiError(400, "incomplete_body", "The request body was cut off.")
        return data

    def _json_body(self) -> dict[str, Any]:
        raw = self._body(JSON_LIMIT)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad_json", "The request body isn't valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "The request body must be a JSON object.")
        return data

    # -- responses -------------------------------------------------------------
    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in web.SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self._headers_sent = True
        if not self._head and body:
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _html(self, text: str, status: int = 200) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

    def _redirect(self, location: str, status: int = 302, cookie: str | None = None) -> None:
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(status, b"", "text/plain; charset=utf-8", headers=headers)

    # -- pages -----------------------------------------------------------------
    def _pages(self, method: str, path: str) -> None:
        if path == "/health" and method == "GET":
            self._json({"status": "ok"})
            return
        if path in ("/", "/index.html") and method == "GET":
            if self._authed():
                self._html(web.index_page())
            else:
                self._redirect("/login")
            return
        if path == "/login" and method == "GET":
            if self._authed():
                self._redirect("/")
                return
            error = (self.query.get("error") or [""])[0]
            self._html(web.login_page(error))
            return
        if path in ("/login", "/api/login") and method == "POST":
            self._login()
            return
        if path == "/logout" and method == "POST":
            self._logout()
            return
        if path == "/api/logout" and method == "GET":
            # Kept for old bookmarks; a GET never changes state (CSRF-safe).
            self._redirect("/")
            return
        if path.startswith("/static/") and method == "GET":
            found = web.static_file(path[len("/static/"):])
            if found is None:
                raise ApiError(404, "not_found", "Not found.")
            self._serve_file(*found)
            return
        if path == "/favicon.ico" and method == "GET":
            found = web.favicon()
            if found is None:
                self._send(204, b"", "image/x-icon", cache=web.STATIC_CACHE)
                return
            self._serve_file(*found)
            return
        raise ApiError(404, "not_found", "Not found.")

    def _serve_file(self, path: str, content_type: str) -> None:
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, content_type, cache=web.STATIC_CACHE)

    def _login(self) -> None:
        ip = self._client_ip()
        if auth.limiter.limited(ip):
            self._redirect("/login?error=rate", 303)
            return
        try:
            raw = self._body(LOGIN_LIMIT)
        except ApiError:
            self._redirect("/login?error=1", 303)
            return
        form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        key = (form.get("key") or [""])[0]
        if key and auth.sessions.check_key(key):
            auth.limiter.reset(ip)
            token = auth.sessions.issue()
            state.add_log(f"Dashboard login from {ip}")
            self._redirect("/", 303, auth.sessions.cookie_header(token, self._secure()))
            return
        auth.limiter.record_failure(ip)
        state.add_log(f"Failed dashboard login from {ip}", "warn")
        target = "/login?error=rate" if auth.limiter.limited(ip) else "/login?error=1"
        self._redirect(target, 303)

    def _logout(self) -> None:
        header_ok = self.headers.get("X-Railpowered") == "1"
        origin = self._same_origin()
        if origin is False or (not header_ok and origin is None):
            raise ApiError(403, "csrf", "Log out from the dashboard.")
        try:
            self._body(LOGIN_LIMIT)
        except ApiError:
            pass
        auth.sessions.revoke(self._token())
        self._redirect("/login", 303, auth.sessions.clear_cookie_header(self._secure()))

    # -- status, logs, tasks ---------------------------------------------------
    @route("GET", "/api/status")
    def api_status(self) -> None:
        self._json(build_status())

    @route("GET", "/api/logs")
    def api_logs(self) -> None:
        raw = (self.query.get("after") or [""])[0].strip()
        after = None
        if raw:
            if not raw.lstrip("-").isdigit():
                raise ApiError(400, "invalid", "after must be a number.")
            after = int(raw)
        self._json(state.logs_after(after))

    @route("GET", "/api/tasks/current")
    def api_task(self) -> None:
        self._json({"task": tasks.snapshot()})

    # -- server control ----------------------------------------------------------
    @route("POST", "/api/server/start")
    def api_start(self) -> None:
        self._json_body()
        controller.start(reason="dashboard")
        self._json({"ok": True})

    @route("POST", "/api/server/stop")
    def api_stop(self) -> None:
        self._json_body()
        controller.stop(reason="dashboard")
        self._json({"ok": True})

    @route("POST", "/api/server/restart")
    def api_restart(self) -> None:
        self._json_body()
        _require_idle_tasks()
        controller.restart()
        self._json({"ok": True})

    @route("POST", "/api/server/save")
    def api_save(self) -> None:
        self._json_body()
        with state.lock:
            phase = state.phase
            spec = state.runtime
        if phase != ONLINE:
            raise ApiError(409, "not_online", "The server isn't online.")
        legacy = ix.is_legacy(getattr(spec, "version", None))
        if not controller.send_command("save-all" if legacy else "save-all flush"):
            raise ApiError(409, "not_online", "The server isn't accepting commands.")
        self._json({"ok": True})

    @route("POST", "/api/console")
    def api_console(self) -> None:
        data = self._json_body()
        command = data.get("command")
        if not isinstance(command, str):
            raise ApiError(400, "invalid", "Type a command first.")
        if "\n" in command or "\r" in command:
            raise ApiError(400, "invalid", "Send one command at a time (no line breaks).")
        command = command.strip()
        if command.startswith("/"):
            command = command[1:].lstrip()
        if not command:
            raise ApiError(400, "invalid", "Type a command first.")
        if len(command) > CONSOLE_MAX:
            raise ApiError(400, "invalid", f"Commands are limited to {CONSOLE_MAX} characters.")
        if not controller.send_command(command):
            raise ApiError(409, "not_running", "The server isn't running.")
        state.add_log(f"> {command}")
        self._json({"ok": True})

    # -- versions and setups -----------------------------------------------------
    @route("GET", "/api/versions")
    def api_versions(self) -> None:
        self._json(cached_versions())

    @route("GET", "/api/setups")
    def api_setups(self) -> None:
        self._json({"setups": ix.list_setups()})

    @route("POST", "/api/setups")
    def api_create_setup(self) -> None:
        data = self._json_body()
        server_type = _str(data, "type", "vanilla").lower()
        if server_type == "modpack":
            raise ApiError(400, "invalid_type", "Install modpacks from the Modpack section.")
        if server_type not in ix.SERVER_TYPES:
            raise ApiError(400, "invalid_type", "Pick Vanilla, Fabric or Forge.")
        version = _str(data, "minecraft_version", "latest") or "latest"
        if not ix.VERSION_RE.match(version):
            raise ApiError(400, "invalid_version", "That Minecraft version doesn't look right.")
        if version == "latest" and server_type == "forge":
            raise ApiError(400, "invalid_version", "Pick a specific Minecraft version for Forge.")
        forge_builds = getattr(installer, "FORGE_BUILDS", None)
        if server_type == "forge" and forge_builds and version not in forge_builds:
            raise ApiError(
                400, "invalid_version",
                f"Forge is available for {', '.join(sorted(forge_builds))}.",
            )
        name = _str(data, "name")
        if len(name) > 48:
            raise ApiError(400, "invalid_name", "Setup names are at most 48 characters.")
        allow_downgrade = _bool(data, "allow_downgrade", False)
        activate = _bool(data, "activate", True)
        create_world = _bool(data, "create_world", True)
        level = _str(data, "level_name")
        worlds_dir = installer.paths()["worlds"]
        if level:
            _check_name(level)
            exists = os.path.isdir(os.path.join(worlds_dir, level))
            if create_world and exists:
                raise ApiError(409, "world_exists", f"A world named {level} already exists.")
            if not create_world and not exists:
                raise ApiError(404, "world_not_found", f"There is no world named {level}.")
        else:
            base = f"{server_type}-{version.replace('.', '_')}"[:28]
            level, counter = base, 2
            while os.path.exists(os.path.join(worlds_dir, level)):
                level, counter = f"{base}-{counter}", counter + 1
            create_world = True
        if not create_world and not allow_downgrade and version != "latest":
            ix.check_downgrade(level, version)
        _require_idle_tasks()
        fields = {
            "name": name or None,
            "type": server_type,
            "minecraft_version": version,
            "level_name": level,
            "create_world": create_world,
        }
        shown = "the latest version" if version == "latest" else version
        label = f"Installing {_type_label(server_type)} {shown}"

        def run(_ctx: Any) -> None:
            _run_with_restart(
                lambda: ix.create_setup(fields, allow_downgrade=allow_downgrade, activate=activate),
                restart=activate,
                reason="switching to the new setup",
            )

        task = tasks.submit("apply", label, run)
        self._json({"task": task.to_dict()}, 202)

    def _find_setup(self, setup_id: str) -> dict[str, Any]:
        for item in ix.list_setups():
            if item["id"] == setup_id:
                return item
        raise ApiError(404, "not_found", "That setup no longer exists.")

    @route("PATCH", f"/api/setups/{_SEG}")
    def api_update_setup(self, setup_id: str) -> None:
        data = self._json_body()
        setup = self._find_setup(setup_id)
        name = data.get("name")
        if name is not None:
            name = _str(data, "name")
            if not name or len(name) > 48:
                raise ApiError(400, "invalid_name", "Setup names are 1-48 characters.")
        level = data.get("level_name")
        if level is not None:
            level = _check_name(_str(data, "level_name"))
            if setup["active"] and controller.is_running() and level != setup["level_name"]:
                raise ApiError(
                    409, "server_running", "Stop the server before changing the world it runs."
                )
        allow_downgrade = _bool(data, "allow_downgrade", False)
        _require_idle_tasks()
        ix.update_setup(setup_id, name=name, level_name=level, allow_downgrade=allow_downgrade)
        self._json({"ok": True, "setups": ix.list_setups()})

    @route("POST", f"/api/setups/{_SEG}/activate")
    def api_activate_setup(self, setup_id: str) -> None:
        data = self._json_body()
        allow_downgrade = _bool(data, "allow_downgrade", False)
        setup = self._find_setup(setup_id)
        if setup["active"]:
            self._json({"ok": True, "task": None, "already_active": True})
            return
        version = setup.get("resolved_version") or setup["minecraft_version"]
        if not allow_downgrade and version and version != "latest":
            ix.check_downgrade(setup["level_name"], version)
        _require_idle_tasks()

        def run(_ctx: Any) -> None:
            _run_with_restart(
                lambda: ix.activate_setup(setup_id, allow_downgrade=allow_downgrade),
                restart=True,
                reason=f"switching to {setup['name']}",
            )

        task = tasks.submit("switch", f"Switching to {setup['name']}", run)
        self._json({"task": task.to_dict()}, 202)

    @route("DELETE", f"/api/setups/{_SEG}")
    def api_delete_setup(self, setup_id: str) -> None:
        setup = self._find_setup(setup_id)
        if setup["active"]:
            raise ApiError(409, "active_setup", "Switch to another setup before deleting this one.")
        _require_idle_tasks()
        ix.delete_setup(setup_id)
        self._json({"ok": True})

    # -- modpacks -----------------------------------------------------------------
    def _modpack_task(self, label: str, install: Callable[[], dict[str, Any]], name: str) -> None:
        def run(_ctx: Any) -> None:
            fields = dict(install())
            fields.setdefault("name", name)
            _run_with_restart(
                lambda: ix.create_setup(fields, activate=True),
                restart=True,
                reason=f"switching to {name}",
            )

        task = tasks.submit("modpack", label, run)
        self._json({"task": task.to_dict()}, 202)

    @route("POST", "/api/modpacks")
    def api_modpack_url(self) -> None:
        data = self._json_body()
        url = _str(data, "url")
        if not url:
            raise ApiError(400, "invalid_url", "Paste a link to a .mrpack or .zip file.")
        if not url.lower().startswith("https://") or len(url) > 2048 or re.search(r"\s", url):
            raise ApiError(400, "invalid_url", "Use an https:// link to a .mrpack or .zip file.")
        name = _check_name(_str(data, "name"), "Modpack")
        level = _str(data, "level_name") or name
        _check_name(level)
        _require_idle_tasks()
        self._modpack_task(
            f"Installing modpack {name}",
            lambda: ix.install_modpack_from_url(url, name, level_name=level),
            name,
        )

    @route("POST", "/api/modpacks/upload")
    def api_modpack_upload(self) -> None:
        name = _check_name(((self.query.get("name") or [""])[0]).strip(), "Modpack")
        level = ((self.query.get("level_name") or [""])[0]).strip() or name
        _check_name(level)
        filename = ((self.query.get("filename") or [""])[0] or self.headers.get("X-Filename", ""))
        extension = ".mrpack" if filename.lower().endswith(".mrpack") else ".zip"
        _require_idle_tasks()
        limit = config.MAX_UPLOAD_MB * 1024 * 1024
        length = self._content_length(limit, required=True)
        modpacks = installer.paths()["modpacks"]
        os.makedirs(modpacks, exist_ok=True)
        free = shutil.disk_usage(modpacks).free
        if free < length * 3 + 64 * 1024 * 1024:
            self.close_connection = True
            raise ApiError(507, "disk_full", "There isn't enough free space on the volume for this pack.")
        partial = os.path.join(modpacks, f".upload-{secrets.token_hex(6)}.part")
        final = os.path.join(modpacks, f"{name}.upload{extension}")
        try:
            self._receive_file(partial, length)
            with open(partial, "rb") as handle:
                magic = handle.read(4)
            if magic != b"PK\x03\x04":
                raise ApiError(400, "invalid_archive", "That file isn't a .mrpack or .zip modpack.")
            os.replace(partial, final)
        except BaseException:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise

        def install() -> dict[str, Any]:
            try:
                return ix.install_modpack_archive(final, name, level_name=level)
            finally:
                try:
                    os.remove(final)
                except OSError:
                    pass

        try:
            self._modpack_task(f"Installing modpack {name}", install, name)
        except TaskBusy:
            try:
                os.remove(final)
            except OSError:
                pass
            raise

    def _receive_file(self, path: str, length: int) -> None:
        remaining = length
        with open(path, "wb") as handle:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    self.close_connection = True
                    raise ApiError(400, "incomplete_body", "The upload was cut off. Try again.")
                handle.write(chunk)
                remaining -= len(chunk)

    # -- worlds ---------------------------------------------------------------------
    @route("GET", "/api/worlds")
    def api_worlds(self) -> None:
        self._json({"worlds": ix.list_worlds()})

    @route("POST", "/api/worlds")
    def api_create_world(self) -> None:
        data = self._json_body()
        name = _check_name(_str(data, "name"))
        if os.path.exists(os.path.join(installer.paths()["worlds"], name)):
            raise ApiError(409, "world_exists", f"A world named {name} already exists.")
        ix.create_world(name)
        self._json({"ok": True, "worlds": ix.list_worlds()})

    @route("DELETE", f"/api/worlds/{_SEG}")
    def api_delete_world(self, name: str) -> None:
        _check_name(name)
        if not os.path.isdir(os.path.join(installer.paths()["worlds"], name)):
            raise ApiError(404, "not_found", f"There is no world named {name}.")
        active = ix.active_setup()
        if active.get("level_name") == name or controller.current_world() == name:
            raise ApiError(
                409, "active_world", "This is the active setup's world. Switch setups before deleting it."
            )
        _require_idle_tasks()
        removed = ix.delete_world(name)
        self._json({"ok": True, "removed_setups": removed})

    # -- backups --------------------------------------------------------------------
    @route("GET", "/api/backups")
    def api_backups(self) -> None:
        self._json({"backups": ix.list_backups(), "disk": ix.disk_usage()})

    @route("POST", "/api/backups")
    def api_create_backup(self) -> None:
        data = self._json_body()
        world = _str(data, "world") or str(ix.active_setup().get("level_name") or "world")
        _check_name(world)
        if not os.path.isdir(os.path.join(installer.paths()["worlds"], world)):
            raise ApiError(404, "not_found", f"There is no world named {world}.")
        _require_idle_tasks()
        task = tasks.submit("backup", f"Backing up {world}", lambda _ctx: controller.backup_world(world))
        self._json({"task": task.to_dict()}, 202)

    def _backup_path(self, name: str) -> str:
        path = ix.backup_file(name)
        if not os.path.isfile(path):
            raise ApiError(404, "not_found", "That backup no longer exists.")
        return path

    @route("POST", f"/api/backups/{_SEG}/restore")
    def api_restore_backup(self, name: str) -> None:
        data = self._json_body()
        self._backup_path(name)
        target = _str(data, "target") or None
        if target:
            _check_name(target)
        effective = target
        if effective is None:
            match = next((b for b in ix.list_backups() if b["name"] == name), None)
            effective = (match or {}).get("world")
        if effective and controller.current_world() == effective:
            raise ApiError(409, "server_running", f"Stop the server before restoring {effective}.")
        _require_idle_tasks()
        label = f"Restoring {effective or 'world'} from backup"
        task = tasks.submit("restore", label, lambda _ctx: ix.restore_backup(name, target=target))
        self._json({"task": task.to_dict()}, 202)

    @route("DELETE", f"/api/backups/{_SEG}")
    def api_delete_backup(self, name: str) -> None:
        self._backup_path(name)
        _require_idle_tasks()
        ix.delete_backup(name)
        self._json({"ok": True})

    @route("GET", f"/api/backups/{_SEG}/download")
    def api_download_backup(self, name: str) -> None:
        path = self._backup_path(name)
        size = os.path.getsize(path)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.send_header("Cache-Control", "no-store")
        for key, value in web.SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self._headers_sent = True
        if self._head:
            return
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- players ----------------------------------------------------------------------
    def _players_payload(self) -> dict[str, Any]:
        return {"online": state.player_list(), **ix.players()}

    @route("GET", "/api/players")
    def api_players(self) -> None:
        self._json(self._players_payload())

    @route("POST", "/api/players/(ops|whitelist)")
    def api_add_player(self, kind: str) -> None:
        data = self._json_body()
        name = _check_player(_str(data, "name"))
        ix.add_player(kind, name)
        controller.send_command(f"op {name}" if kind == "ops" else f"whitelist add {name}")
        self._json({"ok": True, **self._players_payload()})

    @route("DELETE", f"/api/players/(ops|whitelist)/{_SEG}")
    def api_remove_player(self, kind: str, name: str) -> None:
        _check_player(name)
        ix.remove_player(kind, name)
        controller.send_command(f"deop {name}" if kind == "ops" else f"whitelist remove {name}")
        self._json({"ok": True, **self._players_payload()})

    @route("PUT", "/api/players/whitelist/enabled")
    def api_whitelist_enabled(self) -> None:
        data = self._json_body()
        if "enabled" not in data:
            raise ApiError(400, "invalid", "enabled must be true or false.")
        enabled = _bool(data, "enabled")
        ix.set_whitelist_enabled(enabled)
        controller.send_command("whitelist on" if enabled else "whitelist off")
        controller.invalidate_settings()
        self._json({"ok": True, **self._players_payload()})

    # -- settings ---------------------------------------------------------------------
    @route("GET", "/api/settings")
    def api_settings(self) -> None:
        data = ix.get_settings()
        self._json(
            {
                "properties": data.get("properties", {}),
                "railpowered": data.get("railpowered", ix.railpowered_settings()),
                "restart_required": restart_required(data),
                "schema": data.get("schema", {}),
            }
        )

    @route("PUT", "/api/settings")
    def api_update_settings(self) -> None:
        data = self._json_body()
        properties = data.get("properties")
        railpowered = data.get("railpowered")
        properties = {} if properties is None else properties
        railpowered = {} if railpowered is None else railpowered
        if not isinstance(properties, dict) or not isinstance(railpowered, dict):
            raise ApiError(400, "invalid", "properties and railpowered must be objects.")
        ix.update_settings(properties, railpowered)
        controller.invalidate_settings()
        if properties:
            state.add_log("Server settings saved.")
        self._json({"ok": True, "restart_required": restart_required(ix.get_settings())})


# -- helpers used by handlers and tests -------------------------------------------
def restart_required(settings: dict[str, Any]) -> bool:
    if settings.get("restart_required"):
        return True
    with state.lock:
        running = state.process is not None
        at_start = dict(state.properties_at_start or {})
    if not running:
        return False
    current = ix.read_properties()
    keys = set(settings.get("schema") or {}) or set(settings.get("properties") or {})
    return any(current.get(key) != at_start.get(key) for key in keys)


def build_status() -> dict[str, Any]:
    snap = controller.snapshot()
    try:
        setup = ix.active_setup()
    except Exception:
        print(traceback.format_exc(), flush=True)
        setup = {"id": "", "name": "", "type": "vanilla", "minecraft_version": "latest",
                 "resolved_version": None, "level_name": "world", "instance": None}
    runtime = snap["runtime"]
    if runtime is not None and getattr(runtime, "version", None):
        setup["resolved_version"] = runtime.version
    version = setup.get("resolved_version")
    props = ix.read_properties()
    try:
        max_players = int(props.get("max-players", "20"))
    except ValueError:
        max_players = 20
    java = getattr(runtime, "java_major", None) if runtime is not None else ix.java_major(version)
    players = snap["players"]
    return {
        "phase": snap["phase"],
        "task": tasks.snapshot(),
        "players": players,
        "player_count": len(players),
        "max_players": max_players,
        "uptime": snap["uptime"],
        "idle": snap["idle"],
        "join_address": config.MC_PUBLIC_ADDRESS,
        "setup": {
            "id": setup.get("id"),
            "name": setup.get("name"),
            "type": setup.get("type"),
            "minecraft_version": setup.get("minecraft_version"),
            "resolved_version": version,
            "level_name": setup.get("level_name"),
            "instance": setup.get("instance"),
        },
        "java": java,
        "online_mode": props.get("online-mode", "true").strip() != "false",
        "whitelist_enabled": props.get("white-list", "false").strip() == "true",
        "legacy": ix.is_legacy(version),
        "last_exit": snap["last_exit"],
        "product": getattr(installer, "APP_NAME", "Railpowered"),
        "app_version": config.APP_VERSION,
    }


def cached_versions() -> dict[str, Any]:
    now = time.time()
    with _versions_lock:
        data = _versions_cache["data"]
        if data is not None and now - _versions_cache["at"] < _versions_cache["ttl"]:
            return data
    try:
        data = ix.list_available_versions()
        ttl = 60.0 if data.get("offline") else 900.0
        if not data.get("vanilla"):
            data, ttl = ix.offline_versions(), 60.0
    except Exception:
        print(traceback.format_exc(), flush=True)
        data, ttl = ix.offline_versions(), 60.0
    with _versions_lock:
        _versions_cache.update({"at": now, "data": data, "ttl": ttl})
    return data
