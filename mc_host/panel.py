"""Admin HTTP panel: login, status, version/world/backup/console APIs."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

import installer

from .config import (
    ADMIN_KEY,
    ADMIN_TOKEN,
    IDLE_TIMEOUT,
    LOGIN_HTML,
    MC_PUBLIC_ADDRESS,
    TEMPLATE_DIR,
)
from .process import (
    clear_installing,
    require_stopped,
    send_command,
    sleep_proxy,
    start_server,
    stop_server,
)
from .state import state


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _path(self) -> str:
        return urlparse(self.path).path

    def do_GET(self):
        path = self._path()
        if path == "/health":
            self._send_json({"status": "ok"})
            return
        if path == "/api/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", "admin_session=; Path=/; Max-Age=0")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path in ("/", ""):
            if self._is_admin():
                self._serve_panel()
            else:
                self._send_login()
            return
        if path.startswith("/api/"):
            if not self._is_admin():
                self._send_json({"error": "unauthorized"}, 401)
                return
            try:
                self._api_get(path)
            except installer.InstallError as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        self.send_error(404)

    def do_POST(self):
        path = self._path()
        if path == "/api/login":
            self._do_login()
            return
        if not self._is_admin():
            self._send_json({"error": "unauthorized"}, 401)
            return
        try:
            self._api_post(path)
        except installer.InstallError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            state.add_log(f"API error: {exc}")
            self._send_json({"error": str(exc)}, 500)

    def do_DELETE(self):
        if not self._is_admin():
            self._send_json({"error": "unauthorized"}, 401)
            return
        path = self._path()
        try:
            if path.startswith("/api/backups/"):
                installer.delete_backup(unquote(path.split("/", 3)[-1]))
                self._send_json({"success": True, "backups": installer.list_backups()})
                return
            if path.startswith("/api/worlds/"):
                require_stopped()
                installer.delete_world(unquote(path.split("/", 3)[-1]))
                self._send_json(
                    {
                        "success": True,
                        "worlds": installer.list_worlds(),
                        "profiles": installer.list_profiles(),
                    }
                )
                return
            if path.startswith("/api/profiles/"):
                installer.delete_profile(unquote(path.split("/", 3)[-1]))
                self._send_json({"success": True, "profiles": installer.list_profiles()})
                return
        except installer.InstallError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self.send_error(404)

    def _api_get(self, path: str) -> None:
        if path == "/api/status":
            self._serve_status()
        elif path == "/api/logs":
            self._send_json({"logs": state.get_logs(100)})
        elif path == "/api/versions":
            cfg = installer.load_config()
            payload = installer.list_available_versions()
            payload["current"] = {
                "type": cfg.get("type"),
                "minecraft_version": cfg.get("minecraft_version"),
                "level_name": cfg.get("level_name"),
                "modpack": cfg.get("modpack"),
                "active_profile": cfg.get("active_profile"),
            }
            payload["profiles"] = installer.list_profiles(cfg)
            self._send_json(payload)
        elif path == "/api/profiles":
            self._send_json({"profiles": installer.list_profiles()})
        elif path == "/api/worlds":
            self._send_json({"worlds": installer.list_worlds()})
        elif path == "/api/backups":
            self._send_json({"backups": installer.list_backups()})
        elif path.startswith("/api/backups/"):
            name = unquote(path.split("/", 3)[-1])
            file_path = installer.backup_path(name)
            if not os.path.isfile(file_path):
                self._send_json({"error": "Backup not found"}, 404)
                return
            with open(file_path, "rb") as handle:
                data = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{os.path.basename(file_path)}"'
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/players":
            _ops_path, ops = installer.list_file_names("ops")
            _wl_path, whitelist = installer.list_file_names("whitelist")
            self._send_json({"ops": ops, "whitelist": whitelist})
        else:
            self.send_error(404)

    def _api_post(self, path: str) -> None:
        if path == "/api/start":
            if sleep_proxy.active:
                sleep_proxy.stop()
                time.sleep(0.5)
            ok = start_server()
            self._send_json({"success": ok})
            return
        if path == "/api/stop":
            self._send_json({"success": stop_server()})
            return
        if path == "/api/command":
            data = self._read_json()
            cmd = str(data.get("command") or "").strip()
            ok = send_command(cmd) if cmd else False
            self._send_json({"success": ok})
            return
        if path == "/api/save":
            ok = send_command("save-all")
            self._send_json({"success": ok})
            return
        if path == "/api/version":
            require_stopped(lock_install=True)
            try:
                data = self._read_json()
                cfg = installer.load_config()
                server_type = str(data.get("type") or cfg.get("type") or "vanilla")
                version = installer.resolve_minecraft_version(
                    str(data.get("minecraft_version") or cfg.get("minecraft_version") or "")
                )
                level = str(data.get("level_name") or "").strip()
                if level:
                    if not installer.WORLD_NAME_RE.match(level):
                        raise installer.InstallError("Invalid world name")
                else:
                    level = f"{server_type}-{version.replace('.', '_')}"
                    if not os.path.isdir(installer.world_path(level)):
                        os.makedirs(installer.world_path(level), exist_ok=True)
                if server_type != "modpack":
                    cfg["modpack"] = None
                    cfg["instance"] = None
                else:
                    cfg["modpack"] = data.get("modpack", cfg.get("modpack"))
                    cfg["instance"] = data.get("instance", cfg.get("instance"))
                cfg.update(
                    {
                        "type": server_type,
                        "minecraft_version": version,
                        "level_name": level,
                    }
                )
                spec = installer.apply_server(cfg)
                state.add_log(
                    f"Configured {spec.type} {spec.version} on world {spec.level_name}"
                )
                self._send_json(
                    {
                        "success": True,
                        "current": installer.load_config(),
                        "profiles": installer.list_profiles(),
                        "java": spec.java_major,
                    }
                )
            finally:
                clear_installing()
            return
        if path == "/api/worlds":
            require_stopped()
            data = self._read_json()
            name = str(data.get("name") or "").strip()
            installer.create_world(name)
            self._send_json({"success": True, "worlds": installer.list_worlds()})
            return
        if path == "/api/worlds/select":
            require_stopped()
            data = self._read_json()
            cfg = installer.select_world(str(data.get("name") or "").strip())
            self._send_json(
                {
                    "success": True,
                    "current": cfg,
                    "worlds": installer.list_worlds(),
                    "profiles": installer.list_profiles(),
                }
            )
            return
        if path.startswith("/api/profiles/"):
            self._api_profile_post(path)
            return
        if path == "/api/backups":
            cfg = installer.load_config()
            data = self._read_json()
            if state.running:
                send_command("save-all")
                time.sleep(2)
            backup = installer.create_backup(
                str(data.get("level_name") or cfg.get("level_name") or "world")
            )
            self._send_json({"success": True, "backup": backup, "backups": installer.list_backups()})
            return
        if path == "/api/backups/restore":
            require_stopped()
            data = self._read_json()
            restored = installer.restore_backup(str(data.get("name") or ""))
            self._send_json({"success": True, "restored": restored, "worlds": installer.list_worlds()})
            return
        if path == "/api/modpack":
            require_stopped(lock_install=True)
            try:
                data = self._read_json()
                name = str(data.get("name") or "").strip()
                url = str(data.get("url") or "").strip()
                if not url:
                    raise installer.InstallError("Modpack URL is required")
                cfg = installer.install_modpack_from_url(url, name or "modpack")
                spec = installer.apply_server(cfg)
                self._send_json(
                    {
                        "success": True,
                        "current": installer.load_config(),
                        "profiles": installer.list_profiles(),
                        "java": spec.java_major,
                    }
                )
            finally:
                clear_installing()
            return
        if path == "/api/modpack/upload":
            require_stopped(lock_install=True)
            try:
                filename = self.headers.get("X-Filename", "pack.mrpack")
                name = self.headers.get("X-Instance-Name") or os.path.splitext(
                    os.path.basename(filename)
                )[0]
                if not installer.WORLD_NAME_RE.match(name):
                    name = "modpack"
                raw = self._read_raw()
                dest = os.path.join(
                    installer.paths()["modpacks"], os.path.basename(filename)
                )
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as handle:
                    handle.write(raw)
                cfg = installer.install_modpack_archive(dest, name)
                spec = installer.apply_server(cfg)
                self._send_json(
                    {
                        "success": True,
                        "current": installer.load_config(),
                        "profiles": installer.list_profiles(),
                        "java": spec.java_major,
                    }
                )
            finally:
                clear_installing()
            return
        if path in ("/api/ops", "/api/whitelist"):
            data = self._read_json()
            kind = "ops" if path.endswith("ops") else "whitelist"
            action = str(data.get("action") or "add")
            player = str(data.get("name") or "").strip()
            names = installer.update_name_list(kind, player, add=(action != "remove"))
            if state.running:
                command = {
                    ("ops", True): f"op {player}",
                    ("ops", False): f"deop {player}",
                    ("whitelist", True): f"whitelist add {player}",
                    ("whitelist", False): f"whitelist remove {player}",
                }.get((kind, action != "remove"))
                if command:
                    send_command(command)
            self._send_json({"success": True, kind: names})
            return
        self.send_error(404)

    def _is_admin(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                return part[len("admin_session=") :] == ADMIN_TOKEN
        return False

    def _cookie_flags(self) -> str:
        flags = "HttpOnly; SameSite=Strict; Max-Age=604800"
        proto = self.headers.get("X-Forwarded-Proto", "")
        if proto == "https":
            flags += "; Secure"
        return flags

    def _send_login(self, error=""):
        replacement = f'<p class="error">{error}</p>' if error else ""
        self._send_html(LOGIN_HTML.replace("{error}", replacement))

    def _do_login(self):
        body = self._read_raw().decode("utf-8", errors="replace")
        params = parse_qs(body)
        key = params.get("key", [""])[0]
        if key == ADMIN_KEY:
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"admin_session={ADMIN_TOKEN}; Path=/; {self._cookie_flags()}",
            )
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._send_login("Incorrect admin key.")

    def _serve_panel(self):
        self._serve_template("index.html")

    def _serve_template(self, name):
        try:
            with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as handle:
                html = handle.read()
            if name == "index.html":
                html = html.replace(
                    "__MC_PUBLIC_ADDRESS_JSON__",
                    json.dumps(MC_PUBLIC_ADDRESS),
                )
            self._send_html(html)
        except FileNotFoundError:
            self.send_error(404, "Template not found")

    def _serve_status(self):
        uptime = int(time.time() - state.start_time) if state.start_time else None
        idle_secs = 0
        if state.running and state.player_count == 0:
            idle_secs = int(time.time() - state.last_activity)
        cfg = installer.load_config()
        spec = state.runtime
        version = installer.configured_version(cfg)
        self._send_json(
            {
                "running": state.running,
                "starting": state.starting,
                "stopping": state.stopping,
                "installing": state.installing,
                "players": sorted(state.players),
                "player_count": state.player_count,
                "uptime": uptime,
                "idle_timeout": IDLE_TIMEOUT,
                "idle_seconds": idle_secs,
                "proxy_active": sleep_proxy.active,
                "type": cfg.get("type"),
                "minecraft_version": version,
                "level_name": cfg.get("level_name"),
                "modpack": cfg.get("modpack"),
                "java": spec.java_major if spec else installer.java_major_for_version(version),
                "legacy": installer.uses_legacy_files(version),
                "online_mode": installer.properties_for(cfg).get("online-mode") == "true",
                "product": installer.APP_NAME,
                "active_profile": cfg.get("active_profile"),
                "profile_name": next(
                    (
                        item["name"]
                        for item in cfg.get("profiles") or []
                        if item.get("id") == cfg.get("active_profile")
                    ),
                    "",
                ),
            }
        )

    def _api_profile_post(self, path: str) -> None:
        rest = unquote(path[len("/api/profiles/") :]).strip("/")
        if rest.endswith("/use"):
            profile_id = rest[: -len("/use")].strip("/")
            self._use_profile(profile_id)
            return
        data = self._read_json()
        cfg = installer.rename_profile(rest, str(data.get("name") or ""))
        self._send_json({"success": True, "current": cfg, "profiles": installer.list_profiles()})

    def _use_profile(self, profile_id: str) -> None:
        cfg = installer.load_config()
        if cfg.get("active_profile") == profile_id and state.running:
            self._send_json(
                {
                    "success": True,
                    "already_active": True,
                    "started": True,
                    "current": cfg,
                    "profiles": installer.list_profiles(),
                }
            )
            return
        if state.running or state.starting or state.stopping:
            stop_server()
        require_stopped(lock_install=True)
        try:
            cfg = installer.activate_profile(profile_id)
            spec = installer.apply_server(cfg)
            if sleep_proxy.active:
                sleep_proxy.stop()
                time.sleep(0.5)
            started = start_server()
            state.add_log(
                f"Switched to {spec.type} {spec.version} on world {spec.level_name}"
            )
            self._send_json(
                {
                    "success": True,
                    "started": started,
                    "current": installer.load_config(),
                    "profiles": installer.list_profiles(),
                    "java": spec.java_major,
                }
            )
        finally:
            clear_installing()

    def _read_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"command": raw.decode("utf-8", errors="replace").strip()}
        return data if isinstance(data, dict) else {}

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
