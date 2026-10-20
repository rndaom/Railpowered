"""Dashboard HTTP server: pages, static files and the JSON API (contract §1).

Routing lives in ROUTES; each handler gets the regex match and query. Errors
are raised as ApiError / InstallError / ServerError / TaskBusy and turned
into {"error", "code"} JSON with the right status. Anything unexpected is a
generic 500 whose traceback goes to stdout (Railway logs) only.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
import socket
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
# A request gets this long for its request line, headers and (non-upload)
# body; past it the connection is shut. Uploads instead must keep moving at
# least UPLOAD_MIN_BYTES per UPLOAD_WINDOW seconds.
REQUEST_DEADLINE = 15.0
HEALTH_DEADLINE = 5.0  # connections on the slots kept for /health and signed-in requests
UPLOAD_WINDOW = 20.0
UPLOAD_MIN_BYTES = 64 * 1024
HSTS = "max-age=31536000"  # no includeSubDomains: custom domains may have siblings
# An uploaded pack that hit the world-downgrade guard is kept this long as
# modpacks/.held-<token><ext>, so confirming doesn't mean uploading it again.
HELD_UPLOAD_TTL = installer.HELD_UPLOAD_TTL
_HELD_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_HELD_EXTENSIONS = (".mrpack", ".zip")
# One upload streams at a time: same-name uploads can't trip over each other
# and the free-space check isn't shared by several streams at once.
_upload_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# -- routing table -----------------------------------------------------------
Route = tuple[str, "re.Pattern[str]", str, bool]
ROUTES: list[Route] = []


def route(method: str, pattern: str, auth_required: bool = True) -> Callable[[Callable], Callable]:
    def register(fn: Callable) -> Callable:
        ROUTES.append((method, re.compile(f"^{pattern}\\Z"), fn.__name__, auth_required))
        return fn

    return register


_SEG = r"([^/]+)"
# <world>[-pre-restore|-pre-downgrade]-YYYYmmdd-HHMMSS[-n].tar.gz
_BACKUP_WORLD = re.compile(r"^(.+?)-(?:pre-[a-z]+-)?\d{8}-\d{6}(?:-\d+)?\.tar\.gz\Z")


class PanelServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent connections.

    When the general slots are full, the next ``session_slots`` connections
    only serve requests with a valid session cookie (and /health), and the
    last ``health_slots`` only serve /health; both get a short deadline for
    their headers. So a flood of slow or anonymous requests can't lock a
    signed-in owner out or fail Railway's health check. One client (last
    X-Forwarded-For hop) can have at most ``per_client`` requests in
    progress."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64
    max_connections = 64
    health_slots = 8
    session_slots = 8
    per_client = 16

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        general = self.max_connections - self.health_slots - self.session_slots
        if general < 1:
            raise ValueError("max_connections must leave at least one general slot")
        self._pools = {
            "general": threading.BoundedSemaphore(general),
            "session": threading.BoundedSemaphore(self.session_slots) if self.session_slots else None,
            "health": threading.BoundedSemaphore(self.health_slots) if self.health_slots else None,
        }
        self._reserved: dict[int, str] = {}
        self._clients: dict[str, int] = {}
        self._clients_lock = threading.Lock()

    def process_request(self, request: Any, client_address: Any) -> None:
        for pool, slots in self._pools.items():
            if slots is not None and slots.acquire(blocking=False):
                break
        else:
            self.shutdown_request(request)
            return
        if pool != "general":
            with self._clients_lock:
                self._reserved[id(request)] = pool
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request: Any) -> None:
        with self._clients_lock:
            pool = self._reserved.pop(id(request), "general")
        self._pools[pool].release()

    def slot_kind(self, request: Any) -> str:
        """"general", "session" (signed-in requests and /health) or "health"."""
        with self._clients_lock:
            return self._reserved.get(id(request), "general")

    def client_enter(self, ip: str) -> bool:
        with self._clients_lock:
            count = self._clients.get(ip, 0)
            if count >= self.per_client:
                return False
            self._clients[ip] = count + 1
            return True

    def client_exit(self, ip: str) -> None:
        with self._clients_lock:
            count = self._clients.get(ip, 0) - 1
            if count > 0:
                self._clients[ip] = count
            else:
                self._clients.pop(ip, None)


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


def _remove_quietly(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _hold_upload(path: str) -> str:
    """Keep an uploaded archive for a confirmation; returns its token."""
    token = secrets.token_hex(16)
    extension = os.path.splitext(path)[1].lower()
    if extension not in _HELD_EXTENSIONS:
        extension = ".zip"
    held = os.path.join(os.path.dirname(path), f".held-{token}{extension}")
    os.replace(path, held)
    try:
        os.utime(held)
    except OSError:
        pass
    return token


def _claim_held(modpacks: str, token: str, name: str) -> str:
    """Move a held upload back to a fresh upload name for a new install."""
    if not _HELD_TOKEN_RE.fullmatch(token):
        raise ApiError(400, "invalid", "That upload token isn't valid.")
    for extension in _HELD_EXTENSIONS:
        held = os.path.join(modpacks, f".held-{token}{extension}")
        if os.path.isfile(held):
            final = os.path.join(modpacks, f".{name}-{secrets.token_hex(4)}.upload{extension}")
            os.replace(held, final)
            return final
    raise ApiError(
        410, "upload_expired", "That upload is no longer kept. Upload the modpack again."
    )


def purge_held_uploads(now: float | None = None) -> int:
    """Delete held uploads older than HELD_UPLOAD_TTL; returns how many."""
    modpacks = installer.paths()["modpacks"]
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(modpacks)
    except OSError:
        return 0
    for entry in names:
        if not entry.startswith(".held-"):
            continue
        path = os.path.join(modpacks, entry)
        try:
            if now - os.path.getmtime(path) > HELD_UPLOAD_TTL:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def _restart_note(exc: ServerError) -> Exception:
    return ix.install_error(
        f"The change was saved, but the server could not start: {exc}", "start_failed"
    )


def _run_with_restart(change: Callable[[], Any], restart: bool, reason: str) -> Any:
    """Stop if online, apply `change`, then start again (old setup on failure).

    `restart` means the change replaces what the server runs (a switch).
    A start that is still preparing, or a stop in progress, is waited out
    first; a restart that was under way counts as running."""
    resume = controller.settle() if restart else False
    running = restart and controller.is_running()
    was_running = running or (restart and resume)
    if running:
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
    elif restart:
        # A crash of the previous setup says nothing about this one.
        controller.clear_crash("Switched setups; the previous crash no longer applies.")
    return result


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "Railpowered"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 30
    _head = False
    _headers_sent = False
    _deadline: threading.Timer | None = None
    _expired = False

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def version_string(self) -> str:
        return self.server_version

    # -- deadlines -------------------------------------------------------------
    def handle_one_request(self) -> None:
        slot_kind = getattr(self.server, "slot_kind", None)
        self._slot = slot_kind(self.request) if slot_kind else "general"
        self._arm(REQUEST_DEADLINE if self._slot == "general" else HEALTH_DEADLINE)
        try:
            super().handle_one_request()
        except OSError:
            if not self._expired:
                raise
            self.close_connection = True  # cut off by the deadline (some systems raise)
        finally:
            self._disarm()

    def _arm(self, seconds: float) -> None:
        """Shut the connection if the client hasn't sent everything in time
        (a per-recv timeout alone lets a byte every 29 s hold a slot forever)."""
        self._disarm()
        timer = threading.Timer(seconds, self._expire)
        timer.daemon = True
        self._deadline = timer
        timer.start()

    def _disarm(self) -> None:
        timer, self._deadline = self._deadline, None
        if timer is not None:
            timer.cancel()

    def _expire(self) -> None:
        self._expired = True
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- framework errors go through _send (headers, no HTML template) --------
    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        self._disarm()
        if self._headers_sent:
            return
        text = message or self.responses.get(code, ("Error",))[0]
        self._head = getattr(self, "command", "") == "HEAD"
        try:
            if str(getattr(self, "path", "") or "").startswith("/api/"):
                self._json({"error": text, "code": f"http_{code}"}, code)
            else:
                self._send(code, text.encode("utf-8", "replace"), "text/plain; charset=utf-8")
        except OSError:
            pass

    def do_OPTIONS(self) -> None:
        self._disarm()
        self._head = False
        self._headers_sent = False
        path = urlsplit(self.path).path or "/"
        self._fail(path.startswith("/api/"), 405, "method_not_allowed", "That method isn't supported here.")

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
        # Headers are in; bodies are read under their own deadline (_body) or
        # throughput floor (_receive_file).
        self._disarm()
        self._head = method == "HEAD"
        self._headers_sent = False
        effective = "GET" if self._head else method
        url = urlsplit(self.path)
        path = url.path or "/"
        self.query = parse_qs(url.query, keep_blank_values=True)
        is_api = path.startswith("/api/")
        slot = getattr(self, "_slot", "general")
        if path != "/health" and (slot == "health" or (slot == "session" and not self._authed())):
            self.close_connection = True
            self._fail(is_api, 503, "busy", "The dashboard is busy. Try again in a moment.")
            return
        ip = self._client_ip()
        enter = getattr(self.server, "client_enter", None)
        if enter is not None and not enter(ip):
            self.close_connection = True
            self._fail(is_api, 503, "busy", "Too many requests from this address at once. Try again in a moment.")
            return
        try:
            self._route(method, effective, path, is_api)
        finally:
            if enter is not None:
                self.server.client_exit(ip)

    def _route(self, method: str, effective: str, path: str, is_api: bool) -> None:
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
            self._fail(is_api, exc.status, exc.code, str(exc), getattr(exc, "data", None))
        except installer.InstallError as exc:
            if getattr(exc, "detail", None):
                print(f"{ix.error_code(exc)}: {exc.detail}", flush=True)
            self._fail(
                is_api, ix.error_status(exc), ix.error_code(exc),
                str(exc) or "That didn't work.", ix.error_data(exc),
            )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except OSError:
            if not self._expired:
                raise
            self.close_connection = True  # the deadline shut the connection
        except Exception as exc:
            print(traceback.format_exc(), flush=True)
            state.add_log(
                f"Dashboard request {method} {path} failed: {type(exc).__name__}", "error"
            )
            self._fail(is_api, 500, "internal", GENERIC_500)

    def _fail(
        self, is_api: bool, status: int, code: str, message: str, extra: dict[str, Any] | None = None
    ) -> None:
        if self._headers_sent:
            self.close_connection = True
            return
        if is_api:
            self._json({**(extra or {}), "error": message, "code": code}, status)
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
        return auth.session_token(self.headers.get("Cookie"))

    def _authed(self) -> bool:
        return auth.sessions.validate(self._token())

    def _secure(self) -> bool:
        """The request reached Railway's edge over https."""
        headers = getattr(self, "headers", None)
        if headers is None:
            return False
        return headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"

    def _cookie_secure(self) -> bool:
        """Secure (and __Host-) cookies unless this is plain http on a local
        or private address, where the browser couldn't keep them."""
        return self._secure() or not auth.is_local_host(self.headers.get("Host"))

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
        self._arm(REQUEST_DEADLINE)
        try:
            data = self.rfile.read(length)
        except OSError:
            data = b""
        finally:
            self._disarm()
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
        cookies: list[str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in web.SECURITY_HEADERS.items():
            self.send_header(key, value)
        if self._secure():
            self.send_header("Strict-Transport-Security", HSTS)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        for cookie in cookies or ():
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self._headers_sent = True
        if not self._head and body:
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _html(self, text: str, status: int = 200) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

    def _redirect(self, location: str, status: int = 302, cookies: list[str] | None = None) -> None:
        self._send(status, b"", "text/plain; charset=utf-8", headers={"Location": location}, cookies=cookies)

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
            # Pages link assets as ?v=<content hash>, so a versioned URL can be
            # cached for good; a bare URL must be revalidated after a redeploy.
            versioned = bool((self.query.get("v") or [""])[0])
            if versioned:
                cache = web.STATIC_CACHE_VERSIONED
            elif found[1].startswith("font/"):
                cache = web.STATIC_CACHE_FONTS
            else:
                cache = web.STATIC_CACHE
            self._serve_file(*found, cache=cache)
            return
        if path == "/favicon.ico" and method == "GET":
            found = web.favicon()
            if found is None:
                self._send(204, b"", "image/x-icon", cache=web.STATIC_CACHE)
                return
            self._serve_file(*found)
            return
        raise ApiError(404, "not_found", "Not found.")

    def _serve_file(self, path: str, content_type: str, cache: str = web.STATIC_CACHE) -> None:
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, content_type, cache=cache)

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
        # Reserve the attempt (after the body: a slow body holds no reservation).
        verdict, ticket = auth.limiter.begin(ip)
        if verdict == "limited":
            self._redirect("/login?error=rate", 303)
            return

        def check() -> bool:
            return bool(key) and auth.sessions.check_key(key)

        if verdict == "slow":
            ok = auth.limiter.slow_lane(ip, ticket, check)
            if ok is None:
                self._redirect("/login?error=rate", 303)
                return
        else:
            ok = check()
        if ok:
            auth.limiter.succeed(ip, ticket)
            token = auth.sessions.issue()
            state.add_log(f"Dashboard login from {auth.loggable_ip(ip)}")
            secure = self._cookie_secure()
            cookies = [auth.sessions.cookie_header(token, secure)]
            if secure:  # the pre-__Host- cookie, if any, goes
                cookies.append(f"{auth.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Secure")
            self._redirect("/", 303, cookies)
            return
        state.add_log(f"Failed dashboard login from {auth.loggable_ip(ip)}", "warn")
        target = "/login?error=rate" if auth.limiter.limited(ip) else "/login?error=1"
        self._redirect(target, 303)

    def _logout(self) -> None:
        # The dashboard's fetch sends the X-Railpowered header (plus a hidden
        # x_railpowered=1 form field); a plain form post is accepted when the
        # browser shows it came from this origin. A form field alone proves
        # nothing (any site can set one), so it never stands in for either.
        try:
            self._body(LOGIN_LIMIT)
        except ApiError:
            pass
        origin = self._same_origin()
        if origin is False or (origin is None and self.headers.get("X-Railpowered") != "1"):
            raise ApiError(403, "csrf", "Log out from the dashboard.")
        auth.sessions.revoke(self._token())
        self._redirect("/login", 303, auth.sessions.clear_cookie_headers(self._cookie_secure()))

    @route("POST", "/api/sessions/revoke-all")
    def api_revoke_all_sessions(self) -> None:
        """Sign out every browser, this one included (kept across restarts)."""
        self._json_body()
        auth.sessions.revoke_all()
        state.add_log(f"All dashboard sessions were signed out from {auth.loggable_ip(self._client_ip())}.", "warn")
        body = json.dumps({"ok": True}).encode("utf-8")
        self._send(
            200, body, "application/json; charset=utf-8",
            cookies=auth.sessions.clear_cookie_headers(self._cookie_secure()),
        )

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
        data = self._json_body()
        # A 409 world_downgrade answer can be confirmed by sending
        # {"allow_downgrade": true}: the world is backed up, then opened.
        allow_downgrade = _bool(data, "allow_downgrade", False)
        controller.start(reason="dashboard", allow_downgrade=allow_downgrade)
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
        if not ix.valid_version(version):
            raise ApiError(400, "invalid_version", "That Minecraft version doesn't look right.")
        if version == "latest" and server_type == "forge":
            raise ApiError(400, "invalid_version", "Pick a specific Minecraft version for Forge.")
        if server_type == "forge" and version not in installer.FORGE_BUILDS:
            raise ApiError(
                400, "invalid_version",
                f"Forge is available for {', '.join(installer.FORGE_BUILDS)}.",
            )
        name = _str(data, "name")
        if len(name) > ix.SETUP_NAME_MAX:
            raise ApiError(
                400, "invalid_name", f"Setup names are at most {ix.SETUP_NAME_MAX} characters."
            )
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
        if not create_world and not allow_downgrade:
            _precheck_downgrade(level, version)
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

        task = tasks.submit("apply", label, run, guard=controller.ensure_quiet)
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
            if not name or len(name) > ix.SETUP_NAME_MAX:
                raise ApiError(
                    400, "invalid_name", f"Setup names are 1-{ix.SETUP_NAME_MAX} characters."
                )
        level = data.get("level_name")
        if level is not None:
            level = _check_name(_str(data, "level_name"))
            if setup["active"] and controller.lifecycle_active() and level != setup["level_name"]:
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
        if setup["active"] and not allow_downgrade:
            self._json({"ok": True, "task": None, "already_active": True})
            return
        # With allow_downgrade the active setup is prepared again too: that is
        # how a world restored from a newer backup gets confirmed.
        if not allow_downgrade:
            ix.check_setup_downgrade(setup_id)
        _require_idle_tasks()

        def run(_ctx: Any) -> None:
            _run_with_restart(
                lambda: ix.activate_setup(setup_id, allow_downgrade=allow_downgrade),
                restart=True,
                reason=f"switching to {setup['name']}",
            )

        task = tasks.submit("switch", f"Switching to {setup['name']}", run, guard=controller.ensure_quiet)
        self._json({"task": task.to_dict()}, 202)

    @route("DELETE", f"/api/setups/{_SEG}")
    def api_delete_setup(self, setup_id: str) -> None:
        setup = self._find_setup(setup_id)
        if setup["active"]:
            raise ApiError(409, "active_setup", "Switch to another setup before deleting this one.")
        _require_idle_tasks()
        orphaned = ix.delete_setup(setup_id)
        # orphaned: installed files no setup uses now (offer to delete them).
        self._json({"ok": True, "orphaned": orphaned})

    # -- modpacks -----------------------------------------------------------------
    def _modpack_task(
        self,
        label: str,
        stage: Callable[[], Any],
        name: str,
        allow_downgrade: bool,
    ) -> None:
        """Install a pack as a task: stage it while the server keeps running
        (downloads, loader, the downgrade guard), then stop, swap it in and
        activate its setup. If activation fails the previous pack and setup
        versions are put back before the previous setup starts again."""

        def run(_ctx: Any) -> None:
            staged = stage()
            try:
                def change() -> Any:
                    swap = ix.commit_modpack(staged)
                    try:
                        fields = dict(staged.fields)
                        fields.setdefault("name", name)
                        spec = ix.create_setup(fields, allow_downgrade=allow_downgrade, activate=True)
                    except BaseException:
                        ix.rollback_modpack(swap)
                        raise
                    ix.finalize_modpack(swap)
                    return spec

                updating = controller.running_instance() == name
                reason = f"updating {name}" if updating else f"switching to {name}"
                _run_with_restart(change, restart=True, reason=reason)
            finally:
                ix.discard_staged(staged)

        task = tasks.submit("modpack", label, run, guard=controller.ensure_quiet)
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
        allow_downgrade = _bool(data, "allow_downgrade", False)
        _require_idle_tasks()
        # The version is known only after the download; the guard runs then,
        # before the pack's own files are fetched. A world_downgrade failure
        # carries code and data on the task; resubmit with allow_downgrade.
        self._modpack_task(
            f"Installing modpack {name}",
            lambda: ix.stage_modpack_from_url(
                url, name, level_name=level, allow_downgrade=allow_downgrade
            ),
            name,
            allow_downgrade,
        )

    @route("POST", "/api/modpacks/upload")
    def api_modpack_upload(self) -> None:
        name = _check_name(((self.query.get("name") or [""])[0]).strip(), "Modpack")
        level = ((self.query.get("level_name") or [""])[0]).strip() or name
        _check_name(level)
        filename = ((self.query.get("filename") or [""])[0] or self.headers.get("X-Filename", ""))
        extension = ".mrpack" if filename.lower().endswith(".mrpack") else ".zip"
        flag = ((self.query.get("allow_downgrade") or [""])[0]).strip().lower()
        allow_downgrade = flag in ("1", "true", "yes")
        # token: confirm a held upload (409 world_downgrade) without resending it.
        token = ((self.query.get("token") or [""])[0]).strip().lower()
        _require_idle_tasks()
        # Refuse before reading a possibly huge body: the install task would
        # be refused anyway while the server is starting or stopping.
        controller.ensure_quiet()
        if not _upload_lock.acquire(blocking=False):
            self.close_connection = True
            raise ApiError(409, "busy", "Another modpack upload is in progress. Wait for it to finish.")
        try:
            modpacks = installer.paths()["modpacks"]
            os.makedirs(modpacks, exist_ok=True)
            purge_held_uploads()
            if token:
                if self._content_length(JSON_LIMIT) != 0:
                    self.close_connection = True
                    raise ApiError(400, "invalid", "Send no file when confirming a kept upload.")
                final = _claim_held(modpacks, token, name)
            else:
                final = self._receive_upload(modpacks, name, extension)
            try:
                if not allow_downgrade:
                    self._precheck_pack(final, level)
                self._modpack_task(
                    f"Installing modpack {name}",
                    self._stage_upload(final, name, level, allow_downgrade),
                    name,
                    allow_downgrade,
                )
            except BaseException:
                _remove_quietly(final)
                raise
        finally:
            _upload_lock.release()

    def _receive_upload(self, modpacks: str, name: str, extension: str) -> str:
        """Stream the body to a unique hidden file; returns its path."""
        limit = installer.max_upload_bytes()
        length = self._content_length(limit, required=True)
        free = shutil.disk_usage(modpacks).free
        if free < length * 3 + 64 * 1024 * 1024:
            self.close_connection = True
            raise ApiError(507, "disk_full", "There isn't enough free space on the volume for this pack.")
        partial = os.path.join(modpacks, f".upload-{secrets.token_hex(6)}.part")
        final = os.path.join(modpacks, f".{name}-{secrets.token_hex(4)}.upload{extension}")
        try:
            self._receive_file(partial, length)
            with open(partial, "rb") as handle:
                magic = handle.read(4)
            if magic != b"PK\x03\x04":
                raise ApiError(400, "invalid_archive", "That file isn't a .mrpack or .zip modpack.")
            os.replace(partial, final)
        except BaseException:
            _remove_quietly(partial)
            raise
        return final

    @staticmethod
    def _precheck_pack(path: str, level: str) -> None:
        """409 world_downgrade before any task when the archive says which
        version it is. The archive is kept and the error carries
        upload_token, so the confirmation doesn't upload it again."""
        version = ix.peek_pack_version(path)
        if not version or not ix.world_exists(level):
            return
        try:
            ix.check_downgrade(level, version)
        except installer.InstallError as exc:
            if ix.error_code(exc) == "world_downgrade":
                exc.data["upload_token"] = _hold_upload(path)
            raise

    @staticmethod
    def _stage_upload(path: str, name: str, level: str, allow_downgrade: bool) -> Callable[[], Any]:
        def stage() -> Any:
            # The installer leaves the archive alone; the upload is ours to
            # delete, or to keep when the owner has to confirm a downgrade.
            try:
                return ix.stage_modpack_archive(
                    path, name, level_name=level, allow_downgrade=allow_downgrade
                )
            except installer.InstallError as exc:
                if ix.error_code(exc) == "world_downgrade" and os.path.isfile(path):
                    exc.data["upload_token"] = _hold_upload(path)
                raise
            finally:
                _remove_quietly(path)

        return stage

    def _receive_file(self, path: str, length: int) -> None:
        """Stream the body to ``path``. No fixed deadline (big files take
        a while), but at least UPLOAD_MIN_BYTES must arrive in every
        UPLOAD_WINDOW seconds."""
        remaining = length
        window_start, window_bytes = time.monotonic(), 0
        try:
            self.connection.settimeout(UPLOAD_WINDOW)
        except OSError:
            pass
        try:
            with open(path, "wb") as handle:
                while remaining > 0:
                    try:
                        # read1: one recv at most, so the floor is checked
                        # even when bytes trickle in.
                        chunk = self.rfile.read1(min(CHUNK, remaining))
                    except TimeoutError:
                        chunk = None
                    if chunk is None:
                        self.close_connection = True
                        raise ApiError(408, "too_slow", "The upload stalled and was stopped. Try again.")
                    if not chunk:
                        self.close_connection = True
                        raise ApiError(400, "incomplete_body", "The upload was cut off. Try again.")
                    handle.write(chunk)
                    remaining -= len(chunk)
                    window_bytes += len(chunk)
                    now = time.monotonic()
                    if now - window_start >= UPLOAD_WINDOW:
                        if window_bytes < UPLOAD_MIN_BYTES and remaining > 0:
                            self.close_connection = True
                            raise ApiError(408, "too_slow", "The upload was too slow and was stopped. Try again.")
                        window_start, window_bytes = now, 0
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                self.close_connection = True
                raise ApiError(
                    507, "disk_full", "The volume ran out of space during the upload."
                ) from exc
            raise

    # -- worlds ---------------------------------------------------------------------
    @route("GET", "/api/worlds")
    def api_worlds(self) -> None:
        self._json({"worlds": ix.list_worlds()})

    @route("POST", "/api/worlds")
    def api_create_world(self) -> None:
        data = self._json_body()
        name = _check_name(_str(data, "name"))
        ix.create_world(name)
        self._json({"ok": True, "worlds": ix.list_worlds()})

    @route("DELETE", f"/api/worlds/{_SEG}")
    def api_delete_world(self, name: str) -> None:
        _check_name(name)
        if not ix.world_exists(name):
            raise ApiError(404, "world_not_found", f"There is no world named {name}.")
        active = ix.active_setup()
        if active.get("level_name") == name or controller.current_world() == name:
            raise ApiError(
                409, "active_world", "This is the active setup's world. Switch setups before deleting it."
            )
        _require_idle_tasks()
        before = ix.storage_keys()
        removed = ix.delete_world(name)
        self._json({"ok": True, "removed_setups": removed, "orphaned": ix.orphaned_storage(before)})

    # -- downloaded files (instances and server jars) -----------------------------
    @route("GET", "/api/storage")
    def api_storage(self) -> None:
        self._json(ix.list_storage())

    @route("DELETE", f"/api/storage/{_SEG}/{_SEG}")
    def api_delete_storage(self, kind: str, name: str) -> None:
        if kind not in ("instance", "jar"):
            raise ApiError(404, "not_found", "Not found.")
        _require_idle_tasks()
        if kind == "instance" and controller.running_instance() == name:
            raise ApiError(409, "in_use", "The server is running from these files. Stop it first.")
        result = ix.delete_storage(kind, name)
        self._json({"ok": True, "freed": result["freed"], **ix.list_storage()})

    # -- backups --------------------------------------------------------------------
    @route("GET", "/api/backups")
    def api_backups(self) -> None:
        self._json({"backups": ix.list_backups(), "disk": ix.disk_usage()})

    @route("POST", "/api/backups")
    def api_create_backup(self) -> None:
        data = self._json_body()
        world = _str(data, "world") or str(ix.active_setup().get("level_name") or "world")
        _check_name(world)
        if not ix.world_exists(world):
            raise ApiError(404, "world_not_found", f"There is no world named {world}.")
        _require_idle_tasks()
        task = tasks.submit(
            "backup", f"Backing up {world}", lambda _ctx: controller.backup_world(world),
            guard=controller.ensure_quiet,
        )
        self._json({"task": task.to_dict()}, 202)

    @route("POST", "/api/backups/upload")
    def api_upload_backup(self) -> None:
        """Bring a downloaded backup (.tar.gz) back, e.g. onto a new volume.
        The body is the file; it's checked and listed like any backup."""
        # Not during a task: a restore or prune is reading and pruning backups/.
        _require_idle_tasks()
        if not _upload_lock.acquire(blocking=False):
            self.close_connection = True
            raise ApiError(409, "busy", "Another upload is in progress. Wait for it to finish.")
        try:
            limit = installer.max_upload_bytes()
            length = self._content_length(limit, required=True)
            backups = installer.paths()["backups"]
            os.makedirs(backups, exist_ok=True)
            if shutil.disk_usage(backups).free < length + 64 * 1024 * 1024:
                self.close_connection = True
                raise ApiError(507, "disk_full", "There isn't enough free space on the volume for this backup.")
            partial = os.path.join(backups, f".upload-{secrets.token_hex(6)}.part")
            try:
                self._receive_file(partial, length)
                with open(partial, "rb") as handle:
                    magic = handle.read(2)
                if magic != b"\x1f\x8b":
                    raise ApiError(400, "invalid_backup", "That file isn't a .tar.gz backup.")
                backup = ix.import_backup(partial)
            finally:
                _remove_quietly(partial)  # gone already when it was imported
        finally:
            _upload_lock.release()
        self._json({"ok": True, "backup": backup, "backups": ix.list_backups()}, 201)

    def _backup_path(self, name: str) -> str:
        if not ix.valid_backup_name(name):
            raise ApiError(400, "invalid_name", "That isn't a backup name.")
        return ix.backup_file(name)  # 404 backup_not_found when it's gone

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
        if effective is None:
            # Unreadable metadata: the file name still says which world it is.
            named = _BACKUP_WORLD.match(name)
            effective = named.group(1) if named else None
        if effective and controller.current_world() == effective:
            raise ApiError(409, "server_running", f"Stop the server before restoring {effective}.")
        _require_idle_tasks()
        label = f"Restoring {effective or 'world'} from backup"

        def run(ctx: Any) -> dict[str, Any]:
            result = ix.restore_backup(name, target=target)
            warning = (result or {}).get("downgrade_warning")
            if isinstance(warning, dict) and warning.get("message"):
                ctx.set_note(str(warning["message"]))
            if (result or {}).get("world") == ix.active_setup().get("level_name"):
                controller.clear_crash("The active world was restored; the previous crash no longer applies.")
            return result

        task = tasks.submit("restore", label, run, guard=controller.ensure_quiet)
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
        added = ix.add_player(kind, name)
        name = str((added or {}).get("name") or name)  # Mojang's spelling of the name
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
        controller.set_whitelist(enabled)
        controller.invalidate_settings()
        self._json({"ok": True, **self._players_payload()})

    # -- settings ---------------------------------------------------------------------
    @route("GET", "/api/settings")
    def api_settings(self) -> None:
        data = ix.get_settings()
        self._json(
            {
                "properties": data["properties"],
                "railpowered": data["railpowered"],
                "restart_required": restart_required(data),
                "schema": data["schema"],
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
        result = ix.update_settings(properties, railpowered)
        controller.invalidate_settings()
        if properties:
            state.add_log("Server settings saved.")
        if railpowered:
            state.add_log("Railpowered settings saved.")
        self._json({"ok": True, "restart_required": restart_required(result)})


# -- helpers used by handlers and tests -------------------------------------------
def restart_required(settings: dict[str, Any]) -> bool:
    """The installer knows whether settings changed since the server was
    prepared; that only matters while it runs (a stopped server picks the
    new values up on its next start)."""
    return controller.is_running() and bool(settings.get("restart_required"))


def _precheck_downgrade(level: str, version: str) -> None:
    """Fail fast (409 world_downgrade, before any task starts) when ``version``
    is older than the world's last run. "latest" is checked against the
    newest release known without the network; the installer re-checks."""
    if version == "latest":
        version = ix.cached_latest() or ""
    if version and ix.world_exists(level):
        ix.check_downgrade(level, version)


def build_status() -> dict[str, Any]:
    snap = controller.snapshot()
    try:
        setup = ix.active_setup()
    except Exception:
        print(traceback.format_exc(), flush=True)
        setup = {"id": "", "name": "", "type": "vanilla", "minecraft_version": "latest",
                 "resolved_version": None, "level_name": "world", "instance": None}
    last_exit = dict(snap["last_exit"]) if snap["last_exit"] else None
    if last_exit is not None:
        # An exit of another setup (before a switch) isn't this one's story.
        exited = last_exit.pop("setup_id", None)
        if exited and setup.get("id") and exited != setup.get("id"):
            last_exit = None
    runtime = snap["runtime"]
    if runtime is not None and getattr(runtime, "version", None):
        setup["resolved_version"] = runtime.version
    version = setup.get("resolved_version")
    if not version:
        # Never started on "latest": show the release it will most likely get
        # (cached manifest or newest jar on the volume; no network here).
        try:
            version = installer._display_version(setup)
        except Exception:
            version = None
    props = ix.read_properties()
    try:
        max_players = int(props.get("max-players", "20"))
    except ValueError:
        max_players = 20
    if runtime is not None:
        java = getattr(runtime, "java_major", None)
    else:
        java = ix.java_major(version, str(setup.get("type") or "vanilla"))
    legacy = ix.is_legacy(version)
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
        "online_mode": not legacy and props.get("online-mode", "true").strip().lower() != "false",
        "whitelist_enabled": props.get("white-list", "false").strip().lower() == "true",
        "legacy": legacy,
        "last_exit": last_exit,
        "product": installer.APP_NAME,
        "app_version": config.APP_VERSION,
        "max_upload_bytes": installer.max_upload_bytes(),
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
