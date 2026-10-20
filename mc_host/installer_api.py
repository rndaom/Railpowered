"""The one place where mc_host talks to installer.py (contract §2).

installer.py owns every rule about setups, worlds, backups, players and
settings: names, versions, the settings schema and its validation, the
downgrade guard, the child environment. This module only

- looks installer functions up at call time (so tests may patch either
  ``installer.<name>`` or ``installer_api.<name>``),
- trims installer results to the shapes the HTTP API promises (contract §1),
- maps InstallError to an HTTP status.

Nothing here may touch the network on the status / proxy-ping path.
"""

from __future__ import annotations

import os
from typing import Any

import installer

InstallError = installer.InstallError
NAME_RE = installer.NAME_RE
PLAYER_RE = installer.PLAYER_NAME_RE
SERVER_TYPES = installer.SERVER_TYPES
VERSION_LATEST = installer.VERSION_LATEST
SETUP_NAME_MAX = installer.SETUP_NAME_MAX


# -- errors ------------------------------------------------------------------
def install_error(message: str, code: str = "install_error") -> InstallError:
    return installer.InstallError(message, code=code)


def error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or "install_error")


def error_status(exc: BaseException) -> int:
    """HTTP status for an InstallError (installer.ERROR_STATUS is the table)."""
    explicit = getattr(exc, "http_status", None)
    if isinstance(explicit, int):
        return explicit
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    return installer.ERROR_STATUS.get(error_code(exc), 500)


def error_data(exc: BaseException) -> dict[str, Any]:
    """Extra JSON fields an error carries (e.g. world/versions for world_downgrade)."""
    data = getattr(exc, "data", None)
    return dict(data) if isinstance(data, dict) else {}


# -- names and versions --------------------------------------------------------
def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def valid_version(version: str) -> bool:
    if version == VERSION_LATEST:
        return True
    if not installer.VERSION_RE.match(version or ""):
        return False
    try:
        installer.parse_version(version)
    except InstallError:
        return False
    return True


def is_legacy(version: str | None) -> bool:
    """Pre-1.7 servers (old login protocol, plain `save-all`)."""
    if not version or version == VERSION_LATEST:
        return False
    try:
        return installer.is_legacy_protocol(version)
    except InstallError:
        return False


def java_major(version: str | None, server_type: str = "vanilla") -> int | None:
    if not version:
        return None
    try:
        return int(installer.java_major_for_version(version, server_type))
    except Exception:
        return None


def cached_latest() -> str | None:
    """The newest release we know about without touching the network."""
    try:
        return installer.cached_latest_release()
    except Exception:
        return None


# -- straight delegation -------------------------------------------------------
def load_config() -> dict[str, Any]:
    return installer.load_config()


def list_setups() -> list[dict[str, Any]]:
    return installer.list_setups()


def active_setup() -> dict[str, Any]:
    return installer.active_setup()


def create_setup(fields: dict[str, Any], allow_downgrade: bool = False, activate: bool = True) -> Any:
    return installer.create_setup(fields, allow_downgrade=allow_downgrade, activate=activate)


def activate_setup(setup_id: str, allow_downgrade: bool = False) -> Any:
    return installer.activate_setup(setup_id, allow_downgrade=allow_downgrade)


def update_setup(
    setup_id: str,
    name: str | None = None,
    level_name: str | None = None,
    allow_downgrade: bool = False,
) -> dict[str, Any]:
    return installer.update_setup(
        setup_id, name=name, level_name=level_name, allow_downgrade=allow_downgrade
    )


def delete_setup(setup_id: str) -> None:
    installer.delete_setup(setup_id)


def prepare_runtime() -> Any:
    return installer.prepare_runtime()


def check_downgrade(level_name: str, version: str) -> None:
    installer.check_downgrade(level_name, version)


def install_modpack_from_url(
    url: str, name: str, level_name: str | None = None, allow_downgrade: bool = False
) -> dict[str, Any]:
    return installer.install_modpack_from_url(
        url, name, level_name=level_name, allow_downgrade=allow_downgrade
    )


def install_modpack_archive(
    path: str, name: str, level_name: str | None = None, allow_downgrade: bool = False
) -> dict[str, Any]:
    """Install an uploaded pack. The caller still owns (and deletes) ``path``."""
    return installer.install_modpack_archive(
        path, name, level_name=level_name, allow_downgrade=allow_downgrade
    )


def create_world(name: str) -> str:
    return installer.create_world(name)


def delete_world(name: str) -> list[str]:
    return list(installer.delete_world(name))


def world_exists(name: str) -> bool:
    try:
        return os.path.isdir(installer.world_path(name))
    except InstallError:
        return False


def record_world_version(level_name: str, version: str, server_type: str) -> None:
    installer.record_world_version(level_name, version, server_type=server_type)


def restore_backup(name: str, target: str | None = None) -> dict[str, Any]:
    return installer.restore_backup(name, target=target)


def delete_backup(name: str) -> None:
    installer.delete_backup(name)


def backup_file(name: str) -> str:
    return installer.backup_file(name)


def valid_backup_name(name: str) -> bool:
    return bool(installer.BACKUP_NAME_RE.match(name or ""))


def disk_usage() -> dict[str, int]:
    return installer.disk_usage()


def read_properties() -> dict[str, str]:
    try:
        return dict(installer.read_properties())
    except OSError:
        return {}


def add_player(kind: str, name: str) -> dict[str, Any]:
    return installer.add_player(kind, name)


def remove_player(kind: str, name: str) -> bool:
    return installer.remove_player(kind, name)


def set_whitelist_enabled(enabled: bool) -> bool:
    return installer.set_whitelist_enabled(enabled)


def sync_access_lists_from_runtime() -> Any:
    return installer.sync_access_lists_from_runtime()


def get_settings() -> dict[str, Any]:
    return installer.get_settings()


def update_settings(properties: dict[str, Any], railpowered: dict[str, Any]) -> dict[str, Any]:
    return installer.update_settings(properties, railpowered)


def railpowered_settings() -> dict[str, Any]:
    return installer.railpowered_settings()


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return installer.child_env(extra)


# -- shapes for the HTTP API -----------------------------------------------------
_WORLD_KEYS = ("name", "size_bytes", "active", "last_version", "last_played", "used_by")
_BACKUP_KEYS = ("name", "world", "size_bytes", "created_at", "minecraft_version", "kind")


def list_worlds() -> list[dict[str, Any]]:
    return [{key: item.get(key) for key in _WORLD_KEYS} for item in installer.list_worlds()]


def list_backups() -> list[dict[str, Any]]:
    return [{key: item.get(key) for key in _BACKUP_KEYS} for item in installer.list_backups()]


def create_backup(world: str, before_tar: Any = None, after_tar: Any = None) -> dict[str, Any]:
    result = installer.create_backup(world, before_tar=before_tar, after_tar=after_tar)
    return {key: result.get(key) for key in _BACKUP_KEYS}


def players() -> dict[str, Any]:
    """ops / whitelist / flags from the installer; the panel adds "online"."""
    data = installer.players()
    return {
        "ops": [{"name": e["name"], "uuid": e.get("uuid")} for e in data.get("ops", [])],
        "whitelist": [{"name": e["name"], "uuid": e.get("uuid")} for e in data.get("whitelist", [])],
        "whitelist_enabled": bool(data.get("whitelist_enabled")),
        "online_mode": bool(data.get("online_mode", True)),
    }


def _version_items(values: Any) -> list[dict[str, str]]:
    items = []
    for value in values or []:
        if isinstance(value, dict) and value.get("id"):
            items.append({"id": str(value["id"]), "label": str(value.get("label") or value["id"])})
    return items


def list_available_versions() -> dict[str, Any]:
    raw = installer.list_available_versions() or {}
    return {
        "vanilla": _version_items(raw.get("vanilla")),
        "fabric": _version_items(raw.get("fabric")),
        "forge": _version_items(raw.get("forge")),
        "latest": raw.get("latest") or None,
        "snapshots": bool(raw.get("snapshots", False)),
        "offline": bool(raw.get("offline", False)),
    }


def offline_versions() -> dict[str, Any]:
    """Curated list for when even the installer's offline path failed."""
    latest = cached_latest() or installer.FALLBACK_RELEASE

    def items(ids: list[str]) -> list[dict[str, str]]:
        return [{"id": v, "label": f"{v} (latest)" if v == latest else v} for v in ids]

    vanilla = list(dict.fromkeys([latest, *installer.CURATED_VANILLA]))
    fabric = list(dict.fromkeys([latest, *installer.CURATED_FABRIC]))
    return {
        "vanilla": items(vanilla),
        "fabric": items(fabric),
        "forge": [
            {"id": mc, "label": f"{mc} (Forge {build.split('-')[0]})"}
            for mc, build in installer.FORGE_BUILDS.items()
        ],
        "latest": latest,
        "snapshots": False,
        "offline": True,
    }
