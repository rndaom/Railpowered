"""The one place where mc_host talks to installer.py.

Every function here calls the contract-§2 installer function when it exists
and otherwise falls back to a thin shim over the current installer. Shims are
marked `# TODO(integration)`; once installer.py implements the §2 API they
become dead branches and can be deleted. Lookups happen at call time, so
tests that patch `installer.<name>` or `installer.DATA_DIR` are honoured.

Nothing in this module may touch the network on the status/ping path:
`resolved_version()` only reads caches.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import time
from typing import Any

import installer

from . import config

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
VERSION_RE = re.compile(r"^(latest|[0-9][0-9A-Za-z._+-]{0,31})$")
SERVER_TYPES = ("vanilla", "fabric", "forge", "modpack")
SIDECAR = ".railpowered.json"

InstallError = installer.InstallError

_latest_hint: dict[str, str] = {}


# -- helpers ---------------------------------------------------------------
def _fn(name: str) -> Any:
    candidate = getattr(installer, name, None)
    return candidate if callable(candidate) else None


def _accepts(fn: Any, param: str) -> bool:
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def install_error(message: str, code: str = "install_error") -> Exception:
    try:
        exc = installer.InstallError(message, code=code)
    except TypeError:
        exc = installer.InstallError(message)
    if getattr(exc, "code", None) != code:
        try:
            exc.code = code  # type: ignore[attr-defined]
        except AttributeError:
            pass
    return exc


def error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or "install_error")


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _parse_version(version: str) -> tuple[int, ...]:
    try:
        return tuple(installer.parse_version(version))
    except Exception:
        parts = re.findall(r"\d+", version or "")
        return tuple(int(p) for p in parts[:3])


def unescape_property(value: str) -> str:
    """Decode Java .properties escapes (\\uXXXX, \\n, \\:, ...)."""
    if "\\" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("u"):
            try:
                return chr(int(token[1:], 16))
            except ValueError:
                return token
        return {"n": "\n", "t": "\t", "r": "\r", "f": "\f"}.get(token, token)

    return re.sub(r"\\(u[0-9A-Fa-f]{4}|.)", repl, value)


# -- config and setups -----------------------------------------------------
def load_config() -> dict[str, Any]:
    return installer.load_config()


def remember_resolved(minecraft_version: str | None, resolved: str | None) -> None:
    if resolved and str(minecraft_version or "latest").lower() == "latest":
        _latest_hint["latest"] = str(resolved)


def resolved_version(minecraft_version: str | None) -> str | None:
    """Concrete version for a setup, from caches only (never the network)."""
    version = str(minecraft_version or "latest").strip()
    if version.lower() != "latest":
        return version
    cached = getattr(installer, "_latest_cache", None)
    if cached:
        return str(cached)
    return _latest_hint.get("latest")


def _type_label(server_type: str) -> str:
    return {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge", "modpack": "Modpack"}.get(
        server_type, server_type.title()
    )


def _normalize_setup(item: dict[str, Any], active_id: str | None = None) -> dict[str, Any]:
    server_type = str(item.get("type") or "vanilla")
    version = str(item.get("minecraft_version") or "latest")
    resolved = item.get("resolved_version") or resolved_version(version)
    level = str(item.get("level_name") or "world")
    instance = item.get("instance") or (item.get("modpack") if server_type == "modpack" else None)
    shown = version if version != "latest" else f"latest ({resolved})" if resolved else "latest"
    label = str(instance) if server_type == "modpack" and instance else _type_label(server_type)
    summary = item.get("summary") or f"{label} {shown} · world {level}"
    active = item.get("active")
    if active is None:
        active = item.get("id") == active_id
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or f"{label} {shown}"),
        "type": server_type,
        "minecraft_version": version,
        "resolved_version": resolved,
        "level_name": level,
        "instance": instance,
        "active": bool(active),
        "summary": summary,
    }


def list_setups(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    fn = _fn("list_setups")
    if fn is not None and cfg is None:
        return [_normalize_setup(item) for item in fn()]
    # TODO(integration): shim over the legacy "profiles" list. Built straight
    # from the config so it never resolves "latest" over the network.
    cfg = cfg or load_config()
    active = cfg.get("active_profile")
    return [
        _normalize_setup(item, active)
        for item in (cfg.get("profiles") or [])
        if isinstance(item, dict)
    ]


def active_setup(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    setups = list_setups(cfg if _fn("list_setups") is None else None)
    for item in setups:
        if item["active"]:
            return item
    return _normalize_setup(
        {
            "id": cfg.get("active_profile") or "",
            "type": cfg.get("type"),
            "minecraft_version": cfg.get("minecraft_version"),
            "level_name": cfg.get("level_name"),
            "instance": cfg.get("instance"),
            "active": True,
        }
    )


def create_setup(
    fields: dict[str, Any], allow_downgrade: bool = False, activate: bool = True
) -> Any:
    fn = _fn("create_setup")
    if fn is not None:
        return fn(fields, allow_downgrade=allow_downgrade, activate=activate)
    # TODO(integration): legacy apply_server saves before preparing and always
    # activates; the installer worker makes create_setup transactional.
    cfg = load_config()
    server_type = str(fields.get("type") or "vanilla")
    cfg.update(
        {
            "type": server_type,
            "minecraft_version": fields.get("minecraft_version") or "latest",
            "level_name": fields.get("level_name") or "world",
            "modpack": fields.get("modpack") or fields.get("instance")
            if server_type == "modpack"
            else None,
            "instance": fields.get("instance") if server_type == "modpack" else None,
        }
    )
    os.makedirs(installer.world_path(str(cfg["level_name"])), exist_ok=True)
    spec = installer.apply_server(cfg)
    name = str(fields.get("name") or "").strip()
    if name:
        saved = load_config()
        installer.rename_profile(str(saved.get("active_profile")), name)
    return spec


def activate_setup(setup_id: str, allow_downgrade: bool = False) -> Any:
    fn = _fn("activate_setup")
    if fn is not None:
        return fn(setup_id, allow_downgrade=allow_downgrade)
    # TODO(integration): legacy activate_profile + prepare_runtime.
    cfg = installer.activate_profile(setup_id)
    return installer.prepare_runtime(cfg)


def update_setup(
    setup_id: str,
    name: str | None = None,
    level_name: str | None = None,
    allow_downgrade: bool = False,
) -> None:
    fn = _fn("update_setup")
    if fn is not None:
        fn(setup_id, name=name, level_name=level_name, allow_downgrade=allow_downgrade)
        return
    # TODO(integration): legacy rename_profile plus a direct config edit.
    if name:
        installer.rename_profile(setup_id, name)
    if level_name:
        cfg = load_config()
        profile = next(
            (item for item in cfg.get("profiles") or [] if item.get("id") == setup_id), None
        )
        if profile is None:
            raise install_error("That setup no longer exists.", "not_found")
        profile["level_name"] = level_name
        if cfg.get("active_profile") == setup_id:
            cfg["level_name"] = level_name
        os.makedirs(installer.world_path(level_name), exist_ok=True)
        installer.save_config(cfg)


def delete_setup(setup_id: str) -> None:
    fn = _fn("delete_setup")
    if fn is not None:
        fn(setup_id)
        return
    installer.delete_profile(setup_id)  # TODO(integration)


def prepare_runtime(cfg: dict[str, Any] | None = None) -> Any:
    return installer.prepare_runtime(cfg)


def check_downgrade(level_name: str, version: str) -> None:
    fn = _fn("check_downgrade")
    if fn is not None:
        fn(level_name, version)
    # TODO(integration): without the installer guard there is nothing to check.


# -- versions --------------------------------------------------------------
def _version_items(values: Any, latest: str | None) -> list[dict[str, str]]:
    items = []
    for value in values or []:
        if isinstance(value, dict):
            vid = str(value.get("id") or "")
            label = str(value.get("label") or vid)
        else:
            vid = str(value)
            label = f"{vid} (latest)" if latest and vid == latest else vid
        if vid:
            items.append({"id": vid, "label": label})
    return items


def offline_versions() -> dict[str, Any]:
    latest = resolved_version("latest") or getattr(installer, "FALLBACK_RELEASE", "")
    vanilla = list(getattr(installer, "CURATED_VANILLA", []))
    if latest and latest not in vanilla:
        vanilla.insert(0, latest)
    return {
        "vanilla": _version_items(vanilla, latest),
        "fabric": _version_items(getattr(installer, "CURATED_FABRIC", []), latest),
        "forge": _version_items(list(getattr(installer, "FORGE_BUILDS", {}).keys()), None),
        "latest": latest,
        "snapshots": False,
        "offline": True,
    }


def list_available_versions() -> dict[str, Any]:
    raw = installer.list_available_versions() or {}
    latest = str(raw.get("latest") or "") or None
    return {
        "vanilla": _version_items(raw.get("vanilla"), latest),
        "fabric": _version_items(raw.get("fabric"), latest),
        "forge": _version_items(raw.get("forge"), None),
        "latest": latest,
        "snapshots": bool(raw.get("snapshots", False)),
        "offline": bool(raw.get("offline", False)),
    }


# -- modpacks --------------------------------------------------------------
def install_modpack_from_url(url: str, name: str, level_name: str | None = None) -> dict[str, Any]:
    fn = installer.install_modpack_from_url
    if _accepts(fn, "level_name"):
        return fn(url, name, level_name=level_name)
    fields = dict(fn(url, name))  # TODO(integration): old signature
    fields["level_name"] = level_name or name
    return fields


def install_modpack_archive(path: str, name: str, level_name: str | None = None) -> dict[str, Any]:
    fn = installer.install_modpack_archive
    if _accepts(fn, "level_name"):
        return fn(path, name, level_name=level_name)
    fields = dict(fn(path, name))  # TODO(integration): old signature
    fields["level_name"] = level_name or name
    return fields


# -- worlds ----------------------------------------------------------------
def _worlds_dir() -> str:
    return installer.paths()["worlds"]


def read_world_meta(name: str) -> dict[str, Any]:
    if not valid_name(name):
        return {}
    try:
        with open(os.path.join(_worlds_dir(), name, SIDECAR), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_worlds() -> list[dict[str, Any]]:
    raw = installer.list_worlds()
    setups = list_setups()
    result = []
    for item in raw:
        name = str(item.get("name") or "")
        meta = read_world_meta(name) if "last_version" not in item else {}
        used_by = item.get("used_by")
        if used_by is None:
            used_by = [s["name"] for s in setups if s["level_name"] == name]
        result.append(
            {
                "name": name,
                "size_bytes": int(item.get("size_bytes", item.get("size", 0)) or 0),
                "active": bool(item.get("active")),
                "last_version": item.get("last_version", meta.get("last_version")),
                "last_played": item.get("last_played", meta.get("last_played")),
                "used_by": list(used_by),
            }
        )
    return result


def create_world(name: str) -> None:
    installer.create_world(name)


def delete_world(name: str) -> list[str]:
    before = [s["name"] for s in list_setups() if s["level_name"] == name]
    result = installer.delete_world(name)
    if isinstance(result, list):
        return [str(item.get("name") if isinstance(item, dict) else item) for item in result]
    return before  # TODO(integration): legacy delete_world returns None


def record_world_version(level_name: str, version: str, server_type: str) -> None:
    fn = _fn("record_world_version")
    if fn is not None:
        fn(level_name, version, server_type)
        return
    # TODO(integration): minimal sidecar writer matching contract §0.
    if not valid_name(level_name) or not version:
        return
    folder = os.path.join(_worlds_dir(), level_name)
    if not os.path.isdir(folder):
        return
    meta = read_world_meta(level_name)
    previous = str(meta.get("last_version") or "")
    newest = version
    if previous and _parse_version(previous) > _parse_version(version):
        newest = previous
    meta.update(
        {
            "last_version": newest,
            "last_type": server_type,
            "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    tmp = os.path.join(folder, SIDECAR + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
    os.replace(tmp, os.path.join(folder, SIDECAR))


# -- backups ---------------------------------------------------------------
_BACKUP_NEW = re.compile(r"^(?P<world>.+)-(?P<stamp>\d{8}-\d{6})\.tar\.gz$")
_BACKUP_OLD = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)_(?P<world>.+)\.tar\.gz$")


def _normalize_backup(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "")
    world = item.get("world")
    if not world:
        match = _BACKUP_NEW.match(name) or _BACKUP_OLD.match(name)
        world = match.group("world") if match else None
    created = item.get("created_at", item.get("modified"))
    return {
        "name": name,
        "world": world,
        "size_bytes": int(item.get("size_bytes", item.get("size", 0)) or 0),
        "created_at": created,
        "minecraft_version": item.get("minecraft_version"),
    }


def list_backups() -> list[dict[str, Any]]:
    return [_normalize_backup(item) for item in installer.list_backups()]


def create_backup(world: str) -> dict[str, Any]:
    return _normalize_backup(installer.create_backup(world))


def restore_backup(name: str, target: str | None = None) -> Any:
    fn = installer.restore_backup
    if _accepts(fn, "target"):
        return fn(name, target=target)
    # TODO(integration): the legacy restore can only restore in place.
    if target:
        match = _BACKUP_NEW.match(name) or _BACKUP_OLD.match(name)
        if not match or match.group("world") != target:
            raise install_error(
                "Restoring into a different world is not supported yet.", "unsupported"
            )
    return fn(name, select=False)


def delete_backup(name: str) -> None:
    installer.delete_backup(name)


def backup_file(name: str) -> str:
    fn = _fn("backup_file")
    if fn is not None:
        return fn(name)
    return installer.backup_path(name)  # TODO(integration)


def disk_usage() -> dict[str, int]:
    fn = _fn("disk_usage")
    if fn is not None:
        return fn()
    usage = shutil.disk_usage(installer.DATA_DIR)  # TODO(integration)
    return {"used_bytes": usage.used, "total_bytes": usage.total, "free_bytes": usage.free}


# -- players ---------------------------------------------------------------
def read_properties() -> dict[str, str]:
    try:
        return dict(installer.read_properties())
    except OSError:
        return {}


def _player_entries(values: Any) -> list[dict[str, Any]]:
    entries = []
    for value in values or []:
        if isinstance(value, dict):
            name, uuid = str(value.get("name") or ""), value.get("uuid")
        else:
            name, uuid = str(value), None
        # Only real player names reach the browser (no quotes, tags or spaces).
        if PLAYER_RE.match(name):
            entries.append({"name": name, "uuid": uuid})
    return entries


def players() -> dict[str, Any]:
    fn = _fn("players")
    if fn is not None:
        data = dict(fn())
    else:
        # TODO(integration): legacy name lists without UUIDs.
        props = read_properties()
        data = {
            "ops": installer.list_file_names("ops")[1],
            "whitelist": installer.list_file_names("whitelist")[1],
            "whitelist_enabled": props.get("white-list", "false").strip() == "true",
            "online_mode": props.get("online-mode", "true").strip() != "false",
        }
    return {
        "ops": _player_entries(data.get("ops")),
        "whitelist": _player_entries(data.get("whitelist")),
        "whitelist_enabled": bool(data.get("whitelist_enabled")),
        "online_mode": bool(data.get("online_mode", True)),
    }


def add_player(kind: str, name: str) -> None:
    fn = _fn("add_player")
    if fn is not None:
        fn(kind, name)
        return
    installer.update_name_list(kind, name, add=True)  # TODO(integration)


def remove_player(kind: str, name: str) -> None:
    fn = _fn("remove_player")
    if fn is not None:
        fn(kind, name)
        return
    installer.update_name_list(kind, name, add=False)  # TODO(integration)


def set_whitelist_enabled(enabled: bool) -> None:
    fn = _fn("set_whitelist_enabled")
    if fn is not None:
        fn(enabled)
        return
    # TODO(integration): write both keys, keeping every other property.
    props = read_properties()
    value = "true" if enabled else "false"
    props["white-list"] = value
    props["enforce-whitelist"] = value
    installer.write_properties(props)


def sync_access_lists_from_runtime() -> None:
    fn = _fn("sync_access_lists_from_runtime")
    if fn is not None:
        fn()
    # TODO(integration): nothing to sync back with the legacy installer.


# -- settings --------------------------------------------------------------
_BOOL = {"type": "bool"}
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    "motd": {"type": "string", "max": 120, "label": "Server message",
             "help": "Shown under the server name in the multiplayer list."},
    "difficulty": {"type": "enum", "options": ["peaceful", "easy", "normal", "hard"],
                   "label": "Difficulty", "help": ""},
    "gamemode": {"type": "enum", "options": ["survival", "creative", "adventure", "spectator"],
                 "label": "Game mode", "help": "The mode new players start in."},
    "hardcore": {**_BOOL, "label": "Hardcore", "help": "Players are banned when they die."},
    "pvp": {**_BOOL, "label": "PvP", "help": "Players can hurt each other."},
    "max-players": {"type": "int", "min": 1, "max": 200, "label": "Player slots", "help": ""},
    "view-distance": {"type": "int", "min": 3, "max": 32, "label": "View distance",
                      "help": "Chunks sent to players. Lower values use less memory."},
    "simulation-distance": {"type": "int", "min": 3, "max": 32, "label": "Simulation distance",
                            "help": "Chunks that keep ticking around players."},
    "spawn-protection": {"type": "int", "min": 0, "max": 64, "label": "Spawn protection",
                         "help": "Radius around spawn that only operators can change."},
    "allow-flight": {**_BOOL, "label": "Allow flight",
                     "help": "Stops kicks for flying, which some mods need."},
    "allow-nether": {**_BOOL, "label": "Allow the Nether", "help": ""},
    "level-seed": {"type": "string", "max": 64, "label": "World seed",
                   "help": "Only affects new worlds."},
    "level-type": {"type": "enum",
                   "options": ["minecraft:normal", "minecraft:flat", "minecraft:large_biomes",
                               "minecraft:amplified"],
                   "label": "World type", "help": "Only affects new worlds."},
    "online-mode": {**_BOOL, "label": "Online mode",
                    "help": "Check accounts with Mojang. Turn off only for offline LAN play."},
    "enable-command-block": {**_BOOL, "label": "Command blocks", "help": ""},
}
_DEFAULTS = {
    "motd": "A Minecraft Server", "difficulty": "easy", "gamemode": "survival",
    "hardcore": "false", "pvp": "true", "max-players": "20", "view-distance": "8",
    "simulation-distance": "6", "spawn-protection": "0", "allow-flight": "false",
    "allow-nether": "true", "level-seed": "", "level-type": "minecraft:normal",
    "online-mode": "true", "enable-command-block": "false",
}
_NUMERIC_ENUM_BEFORE = (1, 14)  # difficulty/gamemode were numbers before 1.14
_LEGACY_LEVEL_TYPES = {"default": "minecraft:normal", "flat": "minecraft:flat",
                       "largebiomes": "minecraft:large_biomes", "amplified": "minecraft:amplified"}


def _settings_version() -> str:
    setup = active_setup()
    return str(setup.get("resolved_version") or setup.get("minecraft_version") or "")


def _schema_for(version: str) -> dict[str, dict[str, Any]]:
    """Settings this version understands (legacy versions hide newer keys)."""
    schema = {key: dict(value) for key, value in SETTINGS_SCHEMA.items()}
    parsed = _parse_version(version) if version and version != "latest" else ()
    if not parsed:
        return schema
    if parsed < (1, 18):
        schema.pop("simulation-distance", None)
    if parsed < (1, 19):
        schema["level-type"]["options"] = ["default", "flat", "largebiomes", "amplified"]
    if parsed < (1, 7):
        schema["level-type"]["options"] = ["default", "flat"]
    if parsed < (1, 4):
        schema.pop("enable-command-block", None)
        schema["gamemode"]["options"] = ["survival", "creative", "adventure"]
    if parsed < (1, 3):
        schema.pop("hardcore", None)
        schema["gamemode"]["options"] = ["survival", "creative"]
    if parsed < (1, 8):
        schema["gamemode"]["options"] = [
            option for option in schema["gamemode"]["options"] if option != "spectator"
        ]
    return schema


def _uses_numeric_enums(version: str) -> bool:
    return bool(version) and version != "latest" and _parse_version(version) < _NUMERIC_ENUM_BEFORE


def railpowered_defaults() -> dict[str, Any]:
    seconds = config.IDLE_TIMEOUT
    minutes: float = seconds / 60
    if seconds % 60 == 0:
        minutes = seconds // 60
    return {"idle_timeout_minutes": minutes, "auto_start": config.AUTO_START}


def railpowered_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    result = railpowered_defaults()
    try:
        stored = (cfg or load_config()).get("settings") or {}
    except Exception:
        stored = {}
    if isinstance(stored, dict):
        minutes = stored.get("idle_timeout_minutes")
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes >= 0:
            result["idle_timeout_minutes"] = minutes
        if isinstance(stored.get("auto_start"), bool):
            result["auto_start"] = stored["auto_start"]
    return result


def get_settings() -> dict[str, Any]:
    fn = _fn("get_settings")
    if fn is not None:
        data = dict(fn())
        data.setdefault("railpowered", railpowered_settings())
        return data
    # TODO(integration): shim reading server.properties directly.
    version = _settings_version()
    schema = _schema_for(version)
    props = read_properties()
    values: dict[str, Any] = {}
    for key, spec in schema.items():
        raw = unescape_property(props.get(key, _DEFAULTS.get(key, "")))
        if spec["type"] == "bool":
            values[key] = raw.strip().lower() == "true"
        elif spec["type"] == "int":
            try:
                values[key] = int(raw.strip())
            except ValueError:
                values[key] = int(_DEFAULTS.get(key, "0"))
        elif spec["type"] == "enum":
            options = spec["options"]
            text = raw.strip().lower()
            if key == "level-type" and "minecraft:normal" in options:
                text = _LEGACY_LEVEL_TYPES.get(text, text)
            if key in ("difficulty", "gamemode") and text.isdigit():
                index = int(text)
                text = SETTINGS_SCHEMA[key]["options"][index] if index < 4 else ""
            fallback = "easy" if key == "difficulty" else options[0]
            values[key] = text if text in options else fallback
        else:
            values[key] = raw
    return {"properties": values, "railpowered": railpowered_settings(), "schema": schema}


def validate_settings(
    properties: dict[str, Any], schema: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (properties or {}).items():
        spec = schema.get(key)
        if spec is None:
            raise install_error(f"{key} can't be changed here.", "invalid_setting")
        kind = spec["type"]
        label = spec.get("label") or key
        if kind == "bool":
            if not isinstance(value, bool):
                raise install_error(f"{label} must be on or off.", "invalid_setting")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise install_error(f"{label} must be a whole number.", "invalid_setting")
            low, high = spec.get("min"), spec.get("max")
            if (low is not None and value < low) or (high is not None and value > high):
                raise install_error(f"{label} must be between {low} and {high}.", "invalid_setting")
            value = int(value)
        elif kind == "enum":
            if value not in spec.get("options", []):
                raise install_error(f"{label} must be one of: {', '.join(spec['options'])}.",
                                    "invalid_setting")
        else:
            if not isinstance(value, str) or len(value) > int(spec.get("max", 256)):
                raise install_error(f"{label} is too long.", "invalid_setting")
            if any(ch in value for ch in "\r\n\x00"):
                raise install_error(f"{label} must be a single line.", "invalid_setting")
        clean[key] = value
    return clean


def validate_railpowered(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key == "idle_timeout_minutes":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1440:
                raise install_error(
                    "Sleep timer must be between 0 (never) and 1440 minutes.", "invalid_setting"
                )
            clean[key] = int(value) if float(value).is_integer() else float(value)
        elif key == "auto_start":
            if not isinstance(value, bool):
                raise install_error("Start on deploy must be on or off.", "invalid_setting")
            clean[key] = value
        else:
            raise install_error(f"{key} is not a Railpowered setting.", "invalid_setting")
    return clean


def _escape_property(value: str) -> str:
    out = []
    for ch in value:
        if ord(ch) > 0x7E:
            out.append("\\u%04x" % ord(ch) if ord(ch) <= 0xFFFF else ch)
        else:
            out.append(ch)
    return "".join(out)


def update_settings(properties: dict[str, Any], railpowered: dict[str, Any]) -> None:
    fn = _fn("update_settings")
    if fn is not None:
        fn(properties or {}, railpowered or {})
        return
    # TODO(integration): shim. Validate, then merge into server.properties
    # without dropping keys the dashboard doesn't manage.
    version = _settings_version()
    schema = _schema_for(version)
    clean_props = validate_settings(properties or {}, schema)
    clean_rp = validate_railpowered(railpowered or {})
    if clean_props:
        numeric = _uses_numeric_enums(version)
        props = read_properties()
        for key, value in clean_props.items():
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif numeric and key in ("difficulty", "gamemode"):
                text = str(SETTINGS_SCHEMA[key]["options"].index(value))
            else:
                text = _escape_property(str(value))
            props[key] = text
        installer.write_properties(props)
    if clean_rp:
        cfg = load_config()
        stored = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
        stored = dict(stored)
        stored.update(clean_rp)
        cfg["settings"] = stored
        installer.save_config(cfg)


# -- runtime helpers -------------------------------------------------------
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


def child_env() -> dict[str, str]:
    fn = _fn("child_env")
    if fn is not None:
        return dict(fn())
    # TODO(integration): strip the dashboard key and anything secret-looking.
    return {
        key: value
        for key, value in os.environ.items()
        if key != "ADMIN_KEY" and not _SECRET_ENV.search(key)
    }


def java_major(version: str | None) -> int | None:
    if not version:
        return None
    try:
        return int(installer.java_major_for_version(version))
    except Exception:
        return None


def is_legacy(version: str | None) -> bool:
    if not version:
        return False
    try:
        return bool(installer.uses_legacy_files(version))
    except Exception:
        return False
