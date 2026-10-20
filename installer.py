#!/usr/bin/env python3
"""Everything Railpowered does to the data volume.

This module owns setups (saved server configurations), worlds, Minecraft and
mod-loader installs, modpacks, backups, player lists and server settings. It
knows nothing about HTTP or the running Java process: ``mc_host`` calls the
public functions below and turns :class:`InstallError` into API responses.

Layout of the data volume (``MC_DIR``, default ``/server/data``)::

    manager.json            setups and Railpowered settings (atomic, with .bak)
    server.properties       canonical Minecraft settings
    ops.json, whitelist.json canonical player lists
    .uuid-cache.json        Mojang name -> UUID lookups
    worlds/<name>/          one folder per world (+ .railpowered.json sidecar)
    instances/<name>/       Fabric, Forge and modpack server folders
    jars/                   vanilla server jars and loader installers
    cache/                  Mojang/Fabric metadata, Log4Shell configs
    backups/                <world>-<stamp>.tar.gz archives
"""

from __future__ import annotations

import copy
import hashlib
import http.client
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("MC_DIR", "/server/data")

APP_NAME = "Railpowered"
APP_VERSION = "2.0.0"
APP_USER_AGENT = f"Railpowered/{APP_VERSION} (+https://github.com/rndaom/railpowered)"
EULA_URL = "https://www.minecraft.net/eula"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META_URL = "https://meta.fabricmc.net/v2"
FABRIC_MAVEN_URL = "https://maven.fabricmc.net/net/fabricmc/fabric-installer"
FORGE_MAVEN_URL = "https://maven.minecraftforge.net/net/minecraftforge/forge"
MOJANG_PROFILE_URLS = (
    "https://api.mojang.com/users/profiles/minecraft/{name}",
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}",
)
FABRIC_INSTALLER_PIN = os.environ.get("FABRIC_INSTALLER_VERSION", "").strip()
FABRIC_LOADER_PIN = os.environ.get("FABRIC_LOADER_VERSION", "").strip()

VERSION_LATEST = "latest"
# Only ever shown when nothing better is known; never written to a setup.
FALLBACK_RELEASE = "26.3"
CONFIG_SCHEMA = 2

SERVER_TYPES = ("vanilla", "fabric", "forge", "modpack")
TYPE_LABELS = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge", "modpack": "Modpack"}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
WORLD_NAME_RE = NAME_RE  # v1 name, kept for callers
NAME_RULE = (
    "1 to 32 characters: letters, numbers, dot, dash and underscore, "
    "starting with a letter or number"
)
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._+-]{0,39}$")
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.tar\.gz$")
SETUP_NAME_MAX = 48
RESERVED_INSTANCE_PREFIXES = ("fabric-", "forge-")

MANIFEST_TTL = 600  # seconds a version list counts as fresh
FABRIC_TTL = 3600
NETWORK_RETRY_AFTER = 60  # seconds before retrying a host that just failed
UUID_CACHE_TTL = 30 * 24 * 3600
INSTALL_TIMEOUT = 900
MIB = 1024 * 1024

# Mojang's documented Log4Shell mitigations (December 2021).
LOG4J_CONFIGS = {
    "17-111": (
        "https://launcher.mojang.com/v1/objects/"
        "4bb89a97a66f350bc9f73b3ca8509632682aea2e/log4j2_17-111.xml",
        "4bb89a97a66f350bc9f73b3ca8509632682aea2e",
    ),
    "112-116": (
        "https://launcher.mojang.com/v1/objects/"
        "02937d122c86ce73319ef9975b58896fc1b491d1/log4j2_112-116.xml",
        "02937d122c86ce73319ef9975b58896fc1b491d1",
    ),
}

# Curated Forge builds (newest recommended build for each version).
FORGE_BUILDS = {
    "1.16.5": "36.2.39",
    "1.12.2": "14.23.5.2859",
    "1.7.10": "10.13.4.1614-1.7.10",
}
CURATED_VANILLA = ["1.21.11", "1.21.1", "1.20.1", "1.16.5", "1.12.2", "1.8.9", "1.7.10", "1.2.5"]
CURATED_FABRIC = ["1.21.11", "1.21.1", "1.20.1"]
OLDEST_SERVER_JAR = (1, 2, 5)  # 1.0-1.2.4 have no official server download

JAVA_HOMES = {
    8: os.environ.get("JAVA_8_HOME", "/opt/java/8"),
    21: os.environ.get("JAVA_21_HOME", "/opt/java/21"),
    25: os.environ.get("JAVA_25_HOME", "/opt/java/25"),
}

MRPACK_HOSTS = {
    "cdn.modrinth.com",
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
}
MAX_ZIP_ENTRIES = 100_000
MAX_JSON_BYTES = 32 * MIB

WORLD_META_FILE = ".railpowered.json"
BACKUP_META_FILE = "railpowered-backup.json"
PACK_META_FILE = "modpack-meta.json"
INSTALL_MARKER = ".railpowered-install.json"

SECRET_ENV_NAMES = {"ADMIN_KEY", "RAILWAY_TOKEN", "RAILWAY_API_TOKEN"}
SECRET_ENV_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|COOKIE|SESSION|AUTH)",
    re.IGNORECASE,
)

_log = logging.getLogger("railpowered.installer")
_log_handler: Callable[[str], Any] | None = None


def set_log_handler(handler: Callable[[str], Any] | None) -> None:
    """Send progress messages ("Downloading Minecraft 1.20.1...") to ``handler``."""
    global _log_handler
    _log_handler = handler


def _say(message: str) -> None:
    if _log_handler is not None:
        try:
            _log_handler(message)
            return
        except Exception:  # never let logging break an install
            pass
    _log.info(message)


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def max_upload_bytes() -> int:
    return _env_int("MAX_UPLOAD_MB", 1024, minimum=1) * MIB


# ---------------------------------------------------------------------------
# Errors and the runtime description handed to the process layer
# ---------------------------------------------------------------------------

ERROR_STATUS = {
    "invalid_name": 400,
    "invalid_version": 400,
    "invalid_type": 400,
    "invalid_setting": 400,
    "invalid_player_name": 400,
    "invalid_url": 400,
    "invalid_archive": 400,
    "invalid_backup": 400,
    "invalid_request": 400,
    "unsupported_pack": 400,
    "setup_not_found": 404,
    "world_not_found": 404,
    "backup_not_found": 404,
    "instance_not_found": 404,
    "player_not_found": 404,
    "world_exists": 409,
    "active_setup": 409,
    "active_world": 409,
    "world_downgrade": 409,
    "too_large": 413,
    "disk_full": 507,
    "network": 502,
    "download_failed": 502,
    "mojang_unreachable": 502,
    "offline": 503,
}


class InstallError(Exception):
    """A user-facing failure. ``str(exc)`` is a plain-English message."""

    def __init__(
        self,
        message: str,
        code: str = "install_error",
        detail: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail
        self.http_status = http_status
        self.status = ERROR_STATUS.get(code, 500)


@dataclass
class RuntimeSpec:
    """How to launch the prepared server. Memory flags are the caller's job."""

    java_bin: str
    cwd: str
    jar: str = ""
    extra_args: list[str] = field(default_factory=lambda: ["nogui"])
    jvm_args: list[str] = field(default_factory=list)
    prefix_args: list[str] = field(default_factory=list)
    version: str = ""
    type: str = "vanilla"
    level_name: str = "world"
    java_major: int = 0
    env: dict[str, str] = field(default_factory=dict)
    setup_id: str = ""
    instance: str | None = None
    warnings: list[str] = field(default_factory=list)

    def build_command(self, extra_jvm_args: list[str] | tuple[str, ...] = ()) -> list[str]:
        """java [extra_jvm_args] [jvm_args] [@argfiles] [-jar jar] nogui"""
        command = [self.java_bin, *extra_jvm_args, *self.jvm_args, *self.prefix_args]
        if self.jar:
            command.extend(["-jar", self.jar])
        command.extend(self.extra_args)
        return command

    @property
    def command(self) -> list[str]:
        return self.build_command()


# ---------------------------------------------------------------------------
# Paths, names and file helpers
# ---------------------------------------------------------------------------


def paths() -> dict[str, str]:
    return {
        "data": DATA_DIR,
        "jars": os.path.join(DATA_DIR, "jars"),
        "worlds": os.path.join(DATA_DIR, "worlds"),
        "backups": os.path.join(DATA_DIR, "backups"),
        "instances": os.path.join(DATA_DIR, "instances"),
        "modpacks": os.path.join(DATA_DIR, "modpacks"),
        "cache": os.path.join(DATA_DIR, "cache"),
        "config": os.path.join(DATA_DIR, "manager.json"),
        "config_backup": os.path.join(DATA_DIR, "manager.json.bak"),
        "properties": os.path.join(DATA_DIR, "server.properties"),
        "uuid_cache": os.path.join(DATA_DIR, ".uuid-cache.json"),
    }


def ensure_layout() -> None:
    for key in ("jars", "worlds", "backups", "instances", "modpacks", "cache"):
        os.makedirs(paths()[key], exist_ok=True)


def validate_name(name: Any, what: str = "world") -> str:
    text = str(name if name is not None else "").strip()
    if not NAME_RE.match(text):
        shown = text if len(text) <= 40 else text[:40] + "..."
        raise InstallError(
            f"'{shown}' isn't a valid {what} name. Use {NAME_RULE}.",
            code="invalid_name",
        )
    return text


def _safe_child(parent: str, name: str) -> str:
    """Join ``parent`` and a single path component, refusing anything that
    resolves to the parent itself or outside it (``..``, separators, symlinks
    that point elsewhere)."""
    text = str(name or "")
    if (
        not text
        or text in (".", "..")
        or "/" in text
        or "\\" in text
        or "\0" in text
        or ":" in text
        or os.path.isabs(text)
    ):
        raise InstallError(f"Refusing unsafe path name '{text}'.", code="invalid_name")
    candidate = os.path.join(parent, text)
    real_parent = os.path.realpath(parent)
    real_child = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([real_parent, real_child])
    except ValueError:  # different drives on Windows
        common = ""
    if common != real_parent or real_child == real_parent:
        raise InstallError(f"Refusing unsafe path name '{text}'.", code="invalid_name")
    return candidate


def _rmtree_child(parent: str, name: str) -> None:
    target = _safe_child(parent, name)
    if os.path.islink(target):
        os.unlink(target)
    elif os.path.isdir(target):
        shutil.rmtree(target)
    elif os.path.exists(target):
        os.unlink(target)


def _fsync_dir(directory: str) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes | str) -> None:
    """Write via a temp file in the same folder, fsync, then os.replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _atomic_write_json(path: str, data: Any) -> None:
    _atomic_write(path, json.dumps(data, indent=2) + "\n")


def _read_json(path: str) -> Any:
    """Parsed JSON, or None when the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts if ts is not None else _now(), tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def sha1_file(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def parse_version(version: str) -> tuple[int, int, int]:
    raw = str(version or "").strip()
    base = re.split(r"[-+ _]", raw, maxsplit=1)[0]
    parts: list[int] = []
    for token in base.split("."):
        if token.isdigit():
            parts.append(int(token))
        else:
            break
    if not parts:
        raise InstallError(f"'{version}' isn't a Minecraft version.", code="invalid_version")
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def version_key(version: str) -> tuple[int, ...] | None:
    """Sortable key; pre-releases sort before their release. None if unknown."""
    try:
        base = parse_version(version)
    except InstallError:
        return None
    lowered = str(version).lower()
    pre = bool(re.search(r"(-pre|-rc|pre-release|-snapshot|snapshot)", lowered))
    return (*base, 0 if pre else 1)


def is_year_version(version: str) -> bool:
    return parse_version(version)[0] >= 26


def is_legacy_protocol(version: str) -> bool:
    """Before 1.7 the login protocol (and Mojang auth for it) is gone."""
    return (not is_year_version(version)) and parse_version(version) < (1, 7, 0)


def uses_json_lists(version: str) -> bool:
    """1.7.6 moved ops/whitelist from .txt name lists to UUID .json lists."""
    return is_year_version(version) or parse_version(version) >= (1, 7, 6)


def _validate_version(version: Any, server_type: str) -> str:
    text = str(version if version is not None else "").strip()
    if not text or text.lower() == VERSION_LATEST:
        if server_type in ("vanilla", "fabric"):
            return VERSION_LATEST
        raise InstallError(
            f"Pick a specific Minecraft version for {TYPE_LABELS.get(server_type, server_type)}.",
            code="invalid_version",
        )
    if not VERSION_RE.match(text):
        raise InstallError(f"'{text[:40]}' isn't a Minecraft version.", code="invalid_version")
    parse_version(text)
    return text


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

_NET_LOCK = threading.Lock()
_JSON_CACHE: dict[str, tuple[float, Any]] = {}
_FAILED_AT: dict[str, float] = {}


def _urlopen(request: urllib.request.Request, timeout: float):
    """The only place that opens network connections (tests replace it)."""
    return urllib.request.urlopen(request, timeout=timeout)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _require_https(url: str) -> str:
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise InstallError("Only https:// links are supported.", code="invalid_url")
    return text


def _http_get(url: str, timeout: float = 20, max_bytes: int = MAX_JSON_BYTES) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": APP_USER_AGENT, "Accept": "application/json"}
    )
    try:
        with _urlopen(request, timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode() or 200)
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096) or b""
        except Exception:
            body = b""
        return int(exc.code), body
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        host = urlparse(url).netloc or url
        raise InstallError(
            f"Couldn't reach {host}. Check the connection and try again in a minute.",
            code="network",
            detail=f"{url}: {exc}",
        ) from exc
    if len(body) > max_bytes:
        raise InstallError("The server sent an unexpectedly large reply.", code="network")
    return status, body


def _http_json(url: str, timeout: float = 20) -> Any:
    status, body = _http_get(url, timeout=timeout)
    host = urlparse(url).netloc or url
    if status >= 400:
        raise InstallError(
            f"{host} answered with an error (HTTP {status}). Try again in a minute.",
            code="network",
            detail=f"{url}: HTTP {status}",
            http_status=status,
        )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError(
            f"{host} sent a reply Railpowered couldn't read. Try again in a minute.",
            code="network",
            detail=f"{url}: {exc}",
        ) from exc


def _cached_json(
    name: str, url: str, ttl: float, network: bool = True, force: bool = False, timeout: float = 10
) -> tuple[Any, bool]:
    """(data, fresh). Data comes from memory/disk cache, refreshed when stale.
    A failed host is not retried for NETWORK_RETRY_AFTER seconds."""
    path = os.path.join(paths()["cache"], name + ".json")
    with _NET_LOCK:
        cached = _JSON_CACHE.get(path)
        if cached is None:
            disk = _read_json(path)
            if isinstance(disk, dict) and "data" in disk:
                try:
                    fetched = float(disk.get("fetched_at") or 0)
                except (TypeError, ValueError):
                    fetched = 0.0
                if fetched > _now() + 60:
                    fetched = 0.0
                cached = (fetched, disk["data"])
                _JSON_CACHE[path] = cached
        now = _now()
        if cached is not None and not force and now - cached[0] < ttl:
            return cached[1], True
        recently_failed = now - _FAILED_AT.get(url, -1e18) < NETWORK_RETRY_AFTER
    if network and not recently_failed:
        try:
            data = _http_json(url, timeout=timeout)
        except InstallError as exc:
            with _NET_LOCK:
                _FAILED_AT[url] = _now()
            _say(f"Network problem: {exc.detail or exc}")
        else:
            entry = (_now(), data)
            with _NET_LOCK:
                _JSON_CACHE[path] = entry
                _FAILED_AT.pop(url, None)
            try:
                _atomic_write_json(path, {"fetched_at": entry[0], "url": url, "data": data})
            except OSError:
                pass
            return data, True
    if cached is not None:
        return cached[1], False
    return None, False


def download_file(
    url: str,
    dest: str,
    expected_sha1: str | None = None,
    *,
    sha512: str | None = None,
    max_bytes: int | None = None,
    label: str | None = None,
    reuse: bool | None = None,
    retries: int = 3,
    timeout: float = 60,
) -> str:
    """Download ``url`` to ``dest`` (https only) via a temp file, with retries
    and hash checks. An existing ``dest`` is reused only when its hash matches,
    or when ``reuse=True`` is passed for files keyed by version."""
    url = _require_https(url)
    label = label or os.path.basename(urlparse(url).path) or "a file"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isfile(dest):
        if expected_sha1 and sha1_file(dest) == expected_sha1.lower():
            return dest
        if not expected_sha1 and not sha512 and reuse:
            return dest
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        fd, tmp = tempfile.mkstemp(
            prefix="." + os.path.basename(dest) + ".", suffix=".part", dir=os.path.dirname(dest)
        )
        os.close(fd)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": APP_USER_AGENT})
            digest1 = hashlib.sha1()
            digest512 = hashlib.sha512()
            written = 0
            with _urlopen(request, timeout) as response, open(tmp, "wb") as handle:
                length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
                if max_bytes and length and str(length).isdigit() and int(length) > max_bytes:
                    raise InstallError(
                        f"{label} is larger than the {max_bytes // MIB} MB limit.", code="too_large"
                    )
                while True:
                    chunk = response.read(MIB)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise InstallError(
                            f"{label} is larger than the {max(1, max_bytes // MIB)} MB limit.",
                            code="too_large",
                        )
                    digest1.update(chunk)
                    digest512.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_sha1 and digest1.hexdigest() != expected_sha1.lower():
                raise InstallError(
                    f"{label} was corrupted while downloading (checksum mismatch). Try again.",
                    code="download_failed",
                    detail=f"sha1 {digest1.hexdigest()} != {expected_sha1}",
                )
            if sha512 and digest512.hexdigest() != sha512.lower():
                raise InstallError(
                    f"{label} was corrupted while downloading (checksum mismatch). Try again.",
                    code="download_failed",
                    detail="sha512 mismatch",
                )
            os.replace(tmp, dest)
            return dest
        except InstallError:
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500 and exc.code not in (408, 425, 429):
                raise InstallError(
                    f"Couldn't download {label}: the server answered HTTP {exc.code}.",
                    code="download_failed",
                    detail=f"{url}: HTTP {exc.code}",
                    http_status=exc.code,
                ) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            last_error = exc
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if attempt + 1 < retries:
            _sleep(1.5 * (attempt + 1) ** 2)
    raise InstallError(
        f"Couldn't download {label} (network error). Check the connection and try again in a minute.",
        code="download_failed",
        detail=f"{url}: {last_error}",
    )


def _maven_sha1(url: str) -> str | None:
    """Maven publishes <file>.sha1 next to each artifact; best effort."""
    try:
        status, body = _http_get(url + ".sha1", timeout=15, max_bytes=1024)
    except InstallError:
        return None
    if status != 200:
        return None
    text = body.decode("ascii", errors="ignore").strip().split()
    if text and re.fullmatch(r"[0-9a-fA-F]{40}", text[0]):
        return text[0].lower()
    return None


# ---------------------------------------------------------------------------
# Mojang version metadata, "latest", Java selection
# ---------------------------------------------------------------------------


def _manifest(network: bool = True, force: bool = False) -> tuple[dict[str, Any] | None, bool]:
    data, fresh = _cached_json("version_manifest", MANIFEST_URL, MANIFEST_TTL, network, force)
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        return None, False
    return data, fresh


def fetch_version_manifest() -> dict[str, Any]:
    manifest, _fresh = _manifest(network=True)
    if manifest is None:
        raise InstallError(
            "Couldn't reach Mojang's version list. Check the connection and try again in a minute.",
            code="network",
        )
    return manifest


def _manifest_latest(manifest: dict[str, Any] | None) -> str | None:
    if not manifest:
        return None
    latest = (manifest.get("latest") or {}).get("release")
    if isinstance(latest, str) and VERSION_RE.match(latest):
        return latest
    return None


def vanilla_jar_path(version: str) -> str:
    return _safe_child(paths()["jars"], f"minecraft_server.{version}.jar")


def _installed_vanilla_versions() -> list[str]:
    found = []
    try:
        names = os.listdir(paths()["jars"])
    except OSError:
        return []
    for name in names:
        match = re.fullmatch(r"minecraft_server\.(.+)\.jar", name)
        if match and VERSION_RE.match(match.group(1)) and version_key(match.group(1)):
            found.append(match.group(1))
    return found


def _installed_fabric_versions() -> list[str]:
    found = []
    try:
        names = os.listdir(paths()["instances"])
    except OSError:
        return []
    for name in names:
        match = re.fullmatch(r"fabric-(.+)", name)
        if not match or not VERSION_RE.match(match.group(1)):
            continue
        directory = os.path.join(paths()["instances"], name)
        if _install_complete(directory, "fabric", match.group(1)) and version_key(match.group(1)):
            found.append(match.group(1))
    return found


def _newest(versions: list[str]) -> str | None:
    keyed = [(version_key(v), v) for v in versions if version_key(v)]
    if not keyed:
        return None
    return max(keyed)[1]


def _newest_installed(server_type: str) -> str | None:
    if server_type == "fabric":
        return _newest(_installed_fabric_versions()) or _newest(_installed_vanilla_versions())
    return _newest(_installed_vanilla_versions())


def latest_release(network: bool = True) -> str | None:
    """Mojang's current release, from the (refreshed) manifest cache."""
    manifest, _fresh = _manifest(network=network)
    return _manifest_latest(manifest)


def cached_latest_release() -> str | None:
    """No network: cached manifest, else the newest jar on the volume."""
    return latest_release(network=False) or _newest(_installed_vanilla_versions())


def resolve_minecraft_version(
    version: str | None, offline_ok: bool = True, server_type: str = "vanilla"
) -> str:
    """Turn 'latest' into a concrete version. Tries Mojang first (cached for
    a few minutes); when that fails and ``offline_ok`` is true, falls back to
    the newest server already on the volume so a wake needs no network."""
    raw = str(version or "").strip()
    if raw and raw.lower() != VERSION_LATEST:
        return _validate_version(raw, server_type if server_type in SERVER_TYPES else "vanilla")
    manifest, fresh = _manifest(network=True)
    latest = _manifest_latest(manifest)
    if fresh and latest:
        return latest
    if not offline_ok:
        raise InstallError(
            "Couldn't reach Mojang to find the latest Minecraft version. Try again in a minute.",
            code="network",
        )
    installed = _newest_installed(server_type)
    if installed:
        return installed
    if latest:
        return latest
    raise InstallError(
        "Couldn't reach Mojang to find the latest Minecraft version, and no server is "
        "downloaded yet. Try again in a minute.",
        code="offline",
    )


def _version_cache_path(version: str) -> str:
    return _safe_child(os.path.join(paths()["cache"], "versions"), f"{version}.json")


def _version_meta(version: str, network: bool = True) -> dict[str, Any] | None:
    """Server download and Java requirement for one version (cached on disk)."""
    os.makedirs(os.path.join(paths()["cache"], "versions"), exist_ok=True)
    path = _version_cache_path(version)
    cached = _read_json(path)
    if isinstance(cached, dict) and cached.get("id") == version:
        return cached
    if not network:
        return None
    manifest, fresh = _manifest(network=True)
    entry = None
    if manifest:
        entry = next((v for v in manifest["versions"] if v.get("id") == version), None)
    if entry is None and manifest is not None and not fresh:
        manifest, _ = _manifest(network=True, force=True)
        if manifest:
            entry = next((v for v in manifest["versions"] if v.get("id") == version), None)
    if manifest is None:
        raise InstallError(
            f"Couldn't reach Mojang to download Minecraft {version}. Try again in a minute.",
            code="network",
        )
    if entry is None or not entry.get("url"):
        raise InstallError(
            f"Minecraft {version} isn't in Mojang's version list.", code="invalid_version"
        )
    data = _http_json(_require_https(str(entry["url"])), timeout=20)
    server = ((data.get("downloads") or {}).get("server") or {}) if isinstance(data, dict) else {}
    java = (data.get("javaVersion") or {}).get("majorVersion") if isinstance(data, dict) else None
    meta = {
        "id": version,
        "java_major": int(java) if isinstance(java, int) else None,
        "server_url": server.get("url"),
        "server_sha1": server.get("sha1"),
        "server_size": server.get("size"),
        "release_time": entry.get("releaseTime"),
        "type": entry.get("type"),
    }
    _atomic_write_json(path, meta)
    return meta


def _fallback_java(version: str) -> int:
    try:
        if is_year_version(version):
            return 25
        parsed = parse_version(version)
    except InstallError:
        return 25
    if parsed < (1, 17, 0):
        return 8
    if parsed < (1, 20, 5):
        return 17
    return 21


def required_java(version: str, server_type: str = "vanilla", loader: str | None = None) -> int:
    """Minimum Java major for this server, from Mojang's javaVersion (cached)
    or the fallback table."""
    try:
        old = not is_year_version(version) and parse_version(version) < (1, 17, 0)
    except InstallError:
        old = False
    if old and (server_type == "forge" or loader == "forge"):
        return 8
    meta = None
    try:
        meta = _version_meta(version, network=False)
    except (InstallError, OSError):
        meta = None
    if meta and isinstance(meta.get("java_major"), int):
        return int(meta["java_major"])
    return _fallback_java(version)


def _java_executable(home: str) -> str | None:
    for name in ("java", "java.exe"):
        candidate = os.path.join(home, "bin", name)
        if os.path.isfile(candidate):
            return candidate
    return None


def installed_java() -> dict[int, tuple[str, str]]:
    found = {}
    for major, home in JAVA_HOMES.items():
        exe = _java_executable(home) if home else None
        if exe:
            found[major] = (exe, home)
    return found


_PATH_JAVA: dict[str, int | None] = {}


def _path_java_major(exe: str) -> int | None:
    if exe in _PATH_JAVA:
        return _PATH_JAVA[exe]
    major = None
    try:
        out = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=20
        )
        text = (out.stderr or "") + (out.stdout or "")
        match = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if match:
            first = int(match.group(1))
            major = int(match.group(2) or 0) if first == 1 else first
    except (OSError, subprocess.SubprocessError):
        major = None
    _PATH_JAVA[exe] = major
    return major


def select_java(required: int, maximum: int | None = None) -> tuple[int, str, str | None]:
    """Smallest installed Java >= required (and <= maximum). Returns
    (major, java executable, JAVA_HOME or None)."""
    installed = installed_java()
    for major in sorted(installed):
        if major >= required and (maximum is None or major <= maximum):
            exe, home = installed[major]
            return major, exe, home
    exe = shutil.which("java")
    if exe:
        major = _path_java_major(exe)
        if major is not None and major >= required and (maximum is None or major <= maximum):
            return major, exe, None
    wanted = f"Java {required}" if maximum == required else f"Java {required} or newer"
    raise InstallError(
        f"This Minecraft version needs {wanted}, which isn't installed in this container.",
        code="java_missing",
    )


def java_major_for_version(version: str, server_type: str = "vanilla") -> int:
    """Java major that would run ``version`` (no network)."""
    try:
        resolved = version if str(version).lower() != VERSION_LATEST else (cached_latest_release() or FALLBACK_RELEASE)
        need = required_java(resolved, server_type)
        maximum = 8 if need == 8 and server_type == "forge" else None
        return select_java(need, maximum)[0]
    except InstallError:
        return _fallback_java(str(version))


def _log4j_args(version: str) -> list[str]:
    """Mojang's Log4Shell mitigation for 1.7-1.18.0 (1.18.1+ is fixed)."""
    try:
        if is_year_version(version):
            return []
        parsed = parse_version(version)
    except InstallError:
        return []
    if (1, 7, 0) <= parsed <= (1, 11, 2):
        key = "17-111"
    elif (1, 12, 0) <= parsed <= (1, 16, 5):
        key = "112-116"
    elif (1, 17, 0) <= parsed <= (1, 18, 0):
        return ["-Dlog4j2.formatMsgNoLookups=true"]
    else:
        return []
    url, sha1 = LOG4J_CONFIGS[key]
    dest = os.path.join(paths()["cache"], "log4j", os.path.basename(urlparse(url).path))
    download_file(url, dest, sha1, label="Mojang's Log4Shell safety config")
    return [f"-Dlog4j.configurationFile={os.path.abspath(dest)}"]


def _is_secret_env(name: str, value: str) -> bool:
    if name in SECRET_ENV_NAMES or SECRET_ENV_RE.search(name):
        return True
    if "://" in value:
        try:
            parsed = urlparse(value)
            if parsed.password:
                return True
        except ValueError:
            return False
    return False


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ without ADMIN_KEY and other secrets, for the JVM and installers."""
    env = {k: v for k, v in os.environ.items() if not _is_secret_env(k, v)}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# Config (manager.json)
# ---------------------------------------------------------------------------

_CONFIG_LOCK = threading.RLock()
_RUNTIME_LOCK = threading.RLock()
_config_cache: tuple[tuple[Any, ...], dict[str, Any]] | None = None
_config_warnings: list[str] = []


def config_warnings(clear: bool = False) -> list[str]:
    """Problems noticed while loading manager.json (for the dashboard log)."""
    with _CONFIG_LOCK:
        items = list(_config_warnings)
        if clear:
            _config_warnings.clear()
    return items


def _warn(message: str) -> None:
    with _CONFIG_LOCK:
        if message not in _config_warnings:
            _config_warnings.append(message)
    _say(message)


def setup_display_name(setup: dict[str, Any]) -> str:
    stype = str(setup.get("type") or "vanilla")
    version = str(setup.get("minecraft_version") or VERSION_LATEST)
    if stype == "modpack":
        return f"{setup.get('instance') or 'Modpack'} ({version})"
    if version == VERSION_LATEST:
        return f"Latest {stype}" if stype != "vanilla" else "Latest vanilla"
    return f"{TYPE_LABELS.get(stype, stype.title())} {version}"


def _default_setup() -> dict[str, Any]:
    return {
        "id": "default",
        "name": "Latest vanilla",
        "type": "vanilla",
        "minecraft_version": VERSION_LATEST,
        "level_name": "world",
        "instance": None,
    }


def _clean_setup_name(name: Any) -> str | None:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    return text[:SETUP_NAME_MAX] or None


def _sanitize_setup(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    setup_id = str(raw.get("id") or "").strip()
    if not NAME_RE.match(setup_id):
        return None
    stype = str(raw.get("type") or "vanilla").strip().lower()
    if stype not in SERVER_TYPES:
        return None
    try:
        version = _validate_version(raw.get("minecraft_version"), stype)
    except InstallError:
        return None
    level = str(raw.get("level_name") or "world").strip()
    if not NAME_RE.match(level):
        level = "world"
    instance = None
    if stype == "modpack":
        instance = str(raw.get("instance") or raw.get("modpack") or "").strip()
        if not NAME_RE.match(instance):
            return None
    setup: dict[str, Any] = {
        "id": setup_id,
        "name": "",
        "type": stype,
        "minecraft_version": version,
        "level_name": level,
        "instance": instance,
    }
    setup["name"] = _clean_setup_name(raw.get("name")) or setup_display_name(setup)
    resolved = str(raw.get("resolved_version") or "").strip()
    if resolved and VERSION_RE.match(resolved):
        setup["resolved_version"] = resolved
    if isinstance(raw.get("created_at"), str):
        setup["created_at"] = raw["created_at"][:40]
    return setup


def _sanitize_settings(raw: Any) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return clean
    idle = raw.get("idle_timeout_minutes")
    if isinstance(idle, int) and not isinstance(idle, bool) and 0 <= idle <= 1440:
        clean["idle_timeout_minutes"] = idle
    if isinstance(raw.get("auto_start"), bool):
        clean["auto_start"] = raw["auto_start"]
    saved = raw.get("legacy_saved_online_mode")
    if saved in ("true", "false"):
        clean["legacy_saved_online_mode"] = saved
    return clean


def _fingerprint(setup: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(setup.get("type") or ""),
        str(setup.get("minecraft_version") or "").lower(),
        str(setup.get("level_name") or ""),
        str(setup.get("instance") or ""),
    )


def _current_env() -> dict[str, str]:
    return {
        "SERVER_TYPE": os.environ.get("SERVER_TYPE", "").strip().lower(),
        "MINECRAFT_VERSION": os.environ.get("MINECRAFT_VERSION", "").strip(),
    }


def _setup_from_env(env: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """A setup described by SERVER_TYPE/MINECRAFT_VERSION, or (None, warning)."""
    stype = env.get("SERVER_TYPE") or "vanilla"
    if stype not in ("vanilla", "fabric", "forge"):
        return None, (
            f"SERVER_TYPE={stype!r} isn't supported (use vanilla, fabric or forge; "
            "install modpacks from the dashboard)."
        )
    try:
        version = _validate_version(env.get("MINECRAFT_VERSION") or VERSION_LATEST, stype)
    except InstallError as exc:
        return None, f"MINECRAFT_VERSION is not usable: {exc}"
    setup = {
        "id": "default",
        "type": stype,
        "minecraft_version": version,
        "level_name": "world",
        "instance": None,
    }
    setup["name"] = setup_display_name(setup)
    return setup, None


def _mirror(cfg: dict[str, Any]) -> None:
    """Top-level copies of the active setup, for readers of the v1 shape."""
    setup = _active_setup(cfg)
    cfg["type"] = setup["type"]
    cfg["minecraft_version"] = setup["minecraft_version"]
    cfg["level_name"] = setup["level_name"]
    cfg["instance"] = setup.get("instance")
    cfg["modpack"] = setup.get("instance") if setup["type"] == "modpack" else None


def _active_setup(cfg: dict[str, Any]) -> dict[str, Any]:
    active = cfg.get("active_profile")
    for setup in cfg.get("profiles") or []:
        if setup.get("id") == active:
            return setup
    return (cfg.get("profiles") or [_default_setup()])[0]


def _normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("profiles") or []:
        setup = _sanitize_setup(item)
        if setup and setup["id"] not in seen:
            seen.add(setup["id"])
            profiles.append(setup)
    if not profiles:
        seed = {
            "id": "default",
            "name": None,
            "type": raw.get("type") or "vanilla",
            "minecraft_version": raw.get("minecraft_version") or VERSION_LATEST,
            "level_name": raw.get("level_name") or "world",
            "instance": raw.get("instance") or raw.get("modpack"),
        }
        setup = _sanitize_setup(seed) or _sanitize_setup(
            {**_default_setup(), "level_name": seed["level_name"]}
        ) or _default_setup()
        profiles.append(setup)
    cfg: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "active_profile": raw.get("active_profile"),
        "profiles": profiles,
        "settings": _sanitize_settings(raw.get("settings")),
    }
    if isinstance(raw.get("env_applied"), dict):
        cfg["env_applied"] = {
            k: str(v) for k, v in raw["env_applied"].items() if k in ("SERVER_TYPE", "MINECRAFT_VERSION")
        }
    ids = {s["id"] for s in profiles}
    if cfg["active_profile"] not in ids:
        top = _sanitize_setup({**_default_setup(), **{k: raw.get(k) for k in ("type", "minecraft_version", "level_name", "instance", "modpack") if raw.get(k)}, "id": "x"})
        match = None
        if top:
            match = next((s for s in profiles if _fingerprint(s) == _fingerprint(top)), None)
        cfg["active_profile"] = (match or profiles[0])["id"]
    _mirror(cfg)
    return cfg


def _migrate_v1(raw: dict[str, Any], cfg: dict[str, Any]) -> None:
    """One-time upgrade of a v1 manager.json (no "schema" key)."""
    active = _active_setup(cfg)
    top_version = str(raw.get("minecraft_version") or "").strip()
    concrete = bool(top_version) and top_version.lower() != VERSION_LATEST and VERSION_RE.match(top_version)
    if concrete and active.get("minecraft_version") in (VERSION_LATEST, top_version):
        active.setdefault("resolved_version", top_version)
        # v1 pinned the version it actually ran into the top level; remember it
        # for the active world so the downgrade guard protects it from day one.
        level = active["level_name"]
        try:
            world_dir = world_path(level)
            if os.path.isfile(os.path.join(world_dir, "level.dat")) and not os.path.isfile(
                os.path.join(world_dir, WORLD_META_FILE)
            ):
                _write_world_meta(level, {"last_version": top_version, "last_type": active["type"], "last_played": None})
        except (InstallError, OSError):
            pass
    if "env_applied" not in cfg:
        cfg["env_applied"] = _current_env()


def _seed_config() -> dict[str, Any]:
    env = _current_env()
    setup, warning = _setup_from_env(env)
    if warning:
        _warn(warning + " Starting with the latest vanilla release.")
    cfg = {
        "schema": CONFIG_SCHEMA,
        "active_profile": "default",
        "profiles": [setup or _default_setup()],
        "settings": {},
        "env_applied": env,
    }
    _mirror(cfg)
    return cfg


def _config_key(path: str) -> tuple[Any, ...] | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (path, st.st_mtime_ns, st.st_size, st.st_ino)


def _write_config(cfg: dict[str, Any]) -> None:
    global _config_cache
    text = json.dumps(cfg, indent=2) + "\n"
    _atomic_write(paths()["config"], text)
    try:
        _atomic_write(paths()["config_backup"], text)
    except OSError:
        pass
    key = _config_key(paths()["config"])
    _config_cache = (key, copy.deepcopy(cfg)) if key else None


def _load_config_file(path: str) -> dict[str, Any] | None:
    """Parsed config, or None when the file is corrupt. Raises on I/O errors."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(
            "manager.json can't be read right now; nothing was changed.",
            code="config_unreadable",
            detail=str(exc),
        ) from exc
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _recover_config(path: str) -> dict[str, Any]:
    kept = f"{path}.corrupt-{_stamp()}"
    try:
        os.replace(path, kept)
    except OSError:
        kept = path
    backup = _load_config_file(paths()["config_backup"])
    if backup is not None:
        cfg = _normalize_config(backup)
        _warn(
            "manager.json was damaged; restored the last good copy "
            f"(the damaged file is kept as {os.path.basename(kept)})."
        )
    else:
        cfg = _seed_config()
        _warn(
            "manager.json was damaged and no backup copy exists; started with the default "
            f"setup (the damaged file is kept as {os.path.basename(kept)})."
        )
    return cfg


def load_config() -> dict[str, Any]:
    """The current config (a private copy). Never overwrites an unreadable
    manager.json: a damaged file is kept aside and the .bak copy is used."""
    global _config_cache
    with _CONFIG_LOCK:
        ensure_layout()
        path = paths()["config"]
        key = _config_key(path)
        if key is not None and _config_cache is not None and _config_cache[0] == key:
            return copy.deepcopy(_config_cache[1])
        if key is None:
            cfg = _seed_config()
            _write_config(cfg)
            return copy.deepcopy(cfg)
        raw = _load_config_file(path)
        if raw is None:
            cfg = _recover_config(path)
            _write_config(cfg)
            return copy.deepcopy(cfg)
        cfg = _normalize_config(raw)
        if raw.get("schema") != CONFIG_SCHEMA:
            _migrate_v1(raw, cfg)
        if cfg != raw:
            _write_config(cfg)
        else:
            _config_cache = (key, copy.deepcopy(cfg))
        return copy.deepcopy(cfg)


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize and atomically write ``cfg`` (tmp + fsync + os.replace)."""
    with _CONFIG_LOCK:
        ensure_layout()
        clean = _normalize_config(copy.deepcopy(cfg))
        _write_config(clean)
        return copy.deepcopy(clean)


def _mutate_config(fn: Callable[[dict[str, Any]], Any]) -> Any:
    with _CONFIG_LOCK:
        cfg = load_config()
        result = fn(cfg)
        save_config(cfg)
        return result


def railpowered_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective idle timeout / auto-start (manager.json overrides env)."""
    cfg = cfg if cfg is not None else load_config()
    settings = cfg.get("settings") or {}
    idle_seconds = _env_int("IDLE_TIMEOUT", 600, minimum=0)
    default_minutes = 0 if idle_seconds == 0 else max(1, math.ceil(idle_seconds / 60))
    return {
        "idle_timeout_minutes": settings.get("idle_timeout_minutes", default_minutes),
        "auto_start": settings.get("auto_start", _env_bool("AUTO_START", False)),
    }


def pending_env_setup() -> dict[str, Any] | None:
    """If SERVER_TYPE/MINECRAFT_VERSION changed since they were last applied,
    the setup fields they describe (apply with create_setup, then call
    acknowledge_env_setup). Returns {"fields", "message"} or None."""
    cfg = load_config()
    env = _current_env()
    applied = cfg.get("env_applied") or {}
    if env == applied:
        return None
    setup, warning = _setup_from_env(env)
    if setup is None:
        _warn(f"{warning} The Railway variable change was ignored.")
        acknowledge_env_setup()
        return None
    fields = {k: setup[k] for k in ("type", "minecraft_version", "level_name")}
    if any(_fingerprint({**fields, "instance": None}) == _fingerprint(s) for s in cfg["profiles"]) and \
            _fingerprint(_active_setup(cfg)) == _fingerprint({**fields, "instance": None}):
        acknowledge_env_setup()
        return None
    fields["level_name"] = _env_world_name(setup)
    return {
        "fields": fields,
        "message": (
            f"Railway variables now ask for {setup_display_name(setup)}; "
            "switching to it with its own world."
        ),
    }


def _env_world_name(setup: dict[str, Any]) -> str:
    base = f"{setup['type']}-{setup['minecraft_version']}".replace(".", "_")[:32]
    return base if NAME_RE.match(base) else "world"


def acknowledge_env_setup() -> None:
    env = _current_env()
    _mutate_config(lambda cfg: cfg.__setitem__("env_applied", env))


# ---------------------------------------------------------------------------
# Setups
# ---------------------------------------------------------------------------


def _find_setup(cfg: dict[str, Any], setup_id: str) -> dict[str, Any]:
    for setup in cfg.get("profiles") or []:
        if setup.get("id") == setup_id:
            return setup
    raise InstallError("That setup doesn't exist (it may have been deleted).", code="setup_not_found")


def _display_version(setup: dict[str, Any]) -> str | None:
    version = str(setup.get("minecraft_version") or "")
    if version and version != VERSION_LATEST:
        return version
    return setup.get("resolved_version") or cached_latest_release()


def setup_summary(setup: dict[str, Any]) -> str:
    stype = setup["type"]
    version = setup["minecraft_version"]
    resolved = _display_version(setup)
    if version == VERSION_LATEST:
        shown = f"latest ({resolved})" if resolved else "latest"
    else:
        shown = version
    if stype == "modpack":
        head = f"Modpack {setup.get('instance')} · Minecraft {shown}"
    else:
        head = f"{TYPE_LABELS[stype]} {shown}"
    return f"{head} · World: {setup['level_name']}"


def _public_setup(setup: dict[str, Any], active_id: str | None) -> dict[str, Any]:
    return {
        "id": setup["id"],
        "name": setup["name"],
        "type": setup["type"],
        "minecraft_version": setup["minecraft_version"],
        "resolved_version": _display_version(setup),
        "level_name": setup["level_name"],
        "instance": setup.get("instance"),
        "active": setup["id"] == active_id,
        "summary": setup_summary(setup),
    }


def list_setups(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg if cfg is not None else load_config()
    active = cfg.get("active_profile")
    return [_public_setup(s, active) for s in cfg["profiles"]]


def active_setup(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config()
    return _public_setup(_active_setup(cfg), cfg.get("active_profile"))


def _new_setup_id(cfg: dict[str, Any]) -> str:
    taken = {s["id"] for s in cfg.get("profiles") or []}
    while True:
        candidate = f"s{secrets.token_hex(4)}"
        if candidate not in taken:
            return candidate


def _setup_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fields, dict):
        raise InstallError("Setup details are missing.", code="invalid_request")
    stype = str(fields.get("type") or "vanilla").strip().lower()
    if stype not in SERVER_TYPES:
        raise InstallError(
            f"'{stype[:20]}' isn't a server type Railpowered supports (vanilla, fabric, forge, modpack).",
            code="invalid_type",
        )
    version = _validate_version(fields.get("minecraft_version"), stype)
    if stype == "forge" and version not in FORGE_BUILDS:
        raise InstallError(
            f"Forge setups support Minecraft {', '.join(FORGE_BUILDS)}. "
            "For other versions, install a Forge modpack.",
            code="invalid_version",
        )
    level = validate_name(fields.get("level_name") or "world", "world")
    instance = None
    if stype == "modpack":
        instance = validate_name(fields.get("instance") or fields.get("modpack") or "", "modpack")
    setup = {
        "id": "",
        "name": "",
        "type": stype,
        "minecraft_version": version,
        "level_name": level,
        "instance": instance,
    }
    if fields.get("name") is not None and not _clean_setup_name(fields.get("name")):
        raise InstallError(f"Setup names must be 1 to {SETUP_NAME_MAX} characters.", code="invalid_request")
    setup["name"] = _clean_setup_name(fields.get("name")) or setup_display_name(setup)
    return setup


def _downgrade_from(level: str, version: str) -> str | None:
    """The newer version that last ran ``level``, if ``version`` is older."""
    meta = world_meta(level)
    last = meta.get("last_version") if meta else None
    if not last:
        return None
    new_key, old_key = version_key(version), version_key(str(last))
    if new_key is None or old_key is None:
        return None
    return str(last) if new_key < old_key else None


def check_downgrade(level_name: str, version: str) -> None:
    """Raise world_downgrade when ``version`` is older than the world's last run."""
    last = _downgrade_from(level_name, version)
    if last:
        raise InstallError(
            f"World '{level_name}' was last played on Minecraft {last}. Opening it with "
            f"{version} can damage or delete parts of it. Pick {last} or newer, use a new "
            "world, or confirm to take a backup and open it anyway.",
            code="world_downgrade",
        )


def _accept_downgrade(level: str, version: str, server_type: str, previous: str) -> None:
    meta = world_meta(level) or {}
    meta.update(
        {
            "last_version": version,
            "last_type": server_type,
            "downgraded_from": previous,
            "downgraded_at": _iso(),
        }
    )
    _write_world_meta(level, meta)


def _remember_setup(setup: dict[str, Any], activate: bool, resolved: str) -> None:
    def apply(cfg: dict[str, Any]) -> None:
        stored = dict(setup)
        stored["resolved_version"] = resolved
        for index, item in enumerate(cfg["profiles"]):
            if item["id"] == stored["id"]:
                stored.setdefault("created_at", item.get("created_at"))
                cfg["profiles"][index] = stored
                break
        else:
            stored.setdefault("created_at", _iso())
            cfg["profiles"].append(stored)
        if activate:
            cfg["active_profile"] = stored["id"]

    _mutate_config(apply)


def create_setup(
    fields: dict[str, Any], allow_downgrade: bool = False, activate: bool = True
) -> RuntimeSpec:
    """Create (and by default activate) a setup. Everything is installed and
    checked first; manager.json changes only when that succeeds.

    fields: type, minecraft_version, level_name, name?, instance (modpack),
    create_world (True: must be new, False: must exist, None: either)."""
    with _RUNTIME_LOCK:
        cfg = load_config()
        setup = _setup_from_fields(fields)
        world_dir = world_path(setup["level_name"])
        exists = os.path.isdir(world_dir)
        create_world = fields.get("create_world")
        if create_world is True and exists:
            raise InstallError(
                f"A world named '{setup['level_name']}' already exists. Pick another name, "
                "or choose it as an existing world.",
                code="world_exists",
            )
        if create_world is False and not exists:
            raise InstallError(f"World '{setup['level_name']}' doesn't exist.", code="world_not_found")
        if setup["type"] == "modpack" and not os.path.isdir(instance_path(setup["instance"])):
            raise InstallError(
                f"Modpack '{setup['instance']}' isn't installed. Install it first.",
                code="instance_not_found",
            )
        same = next((s for s in cfg["profiles"] if _fingerprint(s) == _fingerprint(setup)), None)
        if same:
            setup["id"] = same["id"]
            if fields.get("name") is None:
                setup["name"] = same["name"]
            setup["created_at"] = same.get("created_at")
        else:
            setup["id"] = _new_setup_id(cfg)
        version = resolve_minecraft_version(setup["minecraft_version"], True, setup["type"])
        previous = _downgrade_from(setup["level_name"], version) if exists else None
        if previous and not allow_downgrade:
            check_downgrade(setup["level_name"], version)
        created = False
        try:
            if not exists:
                os.makedirs(world_dir, exist_ok=True)
                created = True
            if previous:
                _auto_backup(setup["level_name"], "pre-downgrade")
            if activate:
                _sync_back_quietly(cfg)
                candidate = copy.deepcopy(cfg)
                candidate["profiles"] = [s for s in candidate["profiles"] if s["id"] != setup["id"]]
                candidate["profiles"].append(dict(setup))
                candidate["active_profile"] = setup["id"]
                spec = prepare_runtime(candidate, persist=False, allow_downgrade=bool(previous))
            else:
                spec = _install_runtime(setup, version)
        except BaseException:
            if created:
                _remove_if_empty(world_dir)
            raise
        _remember_setup(setup, activate, spec.version or version)
        if previous and activate:
            _accept_downgrade(setup["level_name"], spec.version or version, setup["type"], previous)
        _say(f"Setup ready: {setup['name']} ({setup_summary(setup)})")
        return spec


def activate_setup(setup_id: str, allow_downgrade: bool = False) -> RuntimeSpec:
    """Make a saved setup active after preparing it; config changes only on success."""
    with _RUNTIME_LOCK:
        cfg = load_config()
        setup = _find_setup(cfg, setup_id)
        version = resolve_minecraft_version(setup["minecraft_version"], True, setup["type"])
        previous = _downgrade_from(setup["level_name"], version)
        if previous and not allow_downgrade:
            check_downgrade(setup["level_name"], version)
        if previous:
            _auto_backup(setup["level_name"], "pre-downgrade")
        _sync_back_quietly(cfg)
        candidate = copy.deepcopy(cfg)
        candidate["active_profile"] = setup_id
        spec = prepare_runtime(candidate, persist=False, allow_downgrade=bool(previous))
        resolved = spec.version or version

        def apply(fresh: dict[str, Any]) -> None:
            item = _find_setup(fresh, setup_id)
            item["resolved_version"] = resolved
            fresh["active_profile"] = setup_id

        _mutate_config(apply)
        if previous:
            _accept_downgrade(setup["level_name"], resolved, setup["type"], previous)
        return spec


def update_setup(
    setup_id: str,
    name: str | None = None,
    level_name: str | None = None,
    allow_downgrade: bool = False,
) -> dict[str, Any]:
    """Rename a setup or point it at another world (created if missing).
    The caller must make sure an active setup's server is stopped first."""
    with _RUNTIME_LOCK:
        cfg = load_config()
        setup = _find_setup(cfg, setup_id)
        new_name = None
        if name is not None:
            new_name = _clean_setup_name(name)
            if not new_name:
                raise InstallError(
                    f"Setup names must be 1 to {SETUP_NAME_MAX} characters.", code="invalid_request"
                )
        new_level = None
        if level_name is not None:
            new_level = validate_name(level_name, "world")
            if new_level != setup["level_name"]:
                version = _display_version(setup)
                previous = _downgrade_from(new_level, version) if version else None
                if previous and not allow_downgrade:
                    check_downgrade(new_level, version)
                if previous:
                    _auto_backup(new_level, "pre-downgrade")
                    _accept_downgrade(new_level, version, setup["type"], previous)
                os.makedirs(world_path(new_level), exist_ok=True)

        def apply(fresh: dict[str, Any]) -> dict[str, Any]:
            item = _find_setup(fresh, setup_id)
            if new_name:
                item["name"] = new_name
            if new_level:
                item["level_name"] = new_level
            return _public_setup(item, fresh.get("active_profile"))

        return _mutate_config(apply)


def delete_setup(setup_id: str) -> None:
    def apply(cfg: dict[str, Any]) -> None:
        _find_setup(cfg, setup_id)
        if cfg.get("active_profile") == setup_id:
            raise InstallError(
                "This setup is active. Switch to another setup before deleting it.",
                code="active_setup",
            )
        cfg["profiles"] = [s for s in cfg["profiles"] if s["id"] != setup_id]

    _mutate_config(apply)


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------

_SIZE_CACHE: dict[str, tuple[float, int]] = {}
SIZE_CACHE_TTL = 60


def world_path(name: str) -> str:
    validate_name(name, "world")
    return _safe_child(paths()["worlds"], name)


def _dir_size(path: str, fresh: bool = False) -> int:
    now = _now()
    cached = _SIZE_CACHE.get(path)
    if cached and not fresh and now - cached[0] < SIZE_CACHE_TTL:
        return cached[1]
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            stack.append(entry.path)
                        elif entry.is_file():
                            total += entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    _SIZE_CACHE[path] = (now, total)
    return total


def _remove_if_empty(directory: str) -> None:
    try:
        if os.path.isdir(directory) and not os.listdir(directory):
            os.rmdir(directory)
    except OSError:
        pass


def world_meta(level_name: str) -> dict[str, Any]:
    data = _read_json(os.path.join(world_path(level_name), WORLD_META_FILE))
    return data if isinstance(data, dict) else {}


def _write_world_meta(level_name: str, meta: dict[str, Any]) -> None:
    directory = world_path(level_name)
    os.makedirs(directory, exist_ok=True)
    _atomic_write_json(os.path.join(directory, WORLD_META_FILE), meta)


def record_world_version(level_name: str, version: str, server_type: str = "vanilla") -> None:
    """Called when the server reaches 'online': remember the newest version
    that has run this world (the downgrade guard reads it)."""
    try:
        validate_name(level_name, "world")
        _validate_version(version, "forge")
    except InstallError:
        return
    meta = world_meta(level_name)
    last = meta.get("last_version")
    old_key, new_key = version_key(str(last)) if last else None, version_key(version)
    if not last or (new_key is not None and (old_key is None or new_key >= old_key)):
        meta["last_version"] = version
    meta["last_type"] = server_type
    meta["last_played"] = _iso()
    _write_world_meta(level_name, meta)


def list_worlds() -> list[dict[str, Any]]:
    ensure_layout()
    cfg = load_config()
    active = _active_setup(cfg)["level_name"]
    used: dict[str, list[str]] = {}
    for setup in cfg["profiles"]:
        used.setdefault(setup["level_name"], []).append(setup["name"])
    result = []
    worlds_dir = paths()["worlds"]
    for name in sorted(os.listdir(worlds_dir), key=str.lower):
        if not NAME_RE.match(name):
            continue
        full = os.path.join(worlds_dir, name)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        meta = world_meta(name)
        size = _dir_size(full)
        try:
            modified = int(os.path.getmtime(full))
        except OSError:
            modified = 0
        result.append(
            {
                "name": name,
                "size_bytes": size,
                "active": name == active,
                "last_version": meta.get("last_version"),
                "last_played": meta.get("last_played"),
                "used_by": used.get(name, []),
                "has_level": os.path.isfile(os.path.join(full, "level.dat")),
                "size": size,
                "modified": modified,
            }
        )
    return result


def create_world(name: str) -> str:
    dest = world_path(name)
    if os.path.exists(dest):
        raise InstallError(f"A world named '{name}' already exists.", code="world_exists")
    os.makedirs(dest)
    return name


def delete_world(name: str) -> list[str]:
    """Delete a world folder and the (inactive) setups that use it.
    Returns the names of the removed setups."""
    with _CONFIG_LOCK:
        cfg = load_config()
        validate_name(name, "world")
        if _active_setup(cfg)["level_name"] == name:
            raise InstallError(
                "This is the active setup's world. Switch to another setup first.",
                code="active_world",
            )
        dest = world_path(name)
        if not os.path.isdir(dest):
            raise InstallError(f"World '{name}' doesn't exist.", code="world_not_found")
        _rmtree_child(paths()["worlds"], name)
        _SIZE_CACHE.pop(dest, None)
        removed = [s["name"] for s in cfg["profiles"] if s["level_name"] == name]
        if removed:
            cfg["profiles"] = [s for s in cfg["profiles"] if s["level_name"] != name]
            save_config(cfg)
        return removed


# ---------------------------------------------------------------------------
# Instances, Fabric and Forge
# ---------------------------------------------------------------------------


def instance_path(name: str) -> str:
    return _safe_child(paths()["instances"], name)


def instance_dir(server_type: str, version: str, name: str | None = None) -> str:
    """instances/<name> for modpacks, instances/<type>-<version> otherwise."""
    if name:
        return instance_path(validate_name(name, "modpack"))
    _validate_version(version, server_type)
    return instance_path(f"{server_type}-{version}")


def _read_install_marker(directory: str) -> dict[str, Any]:
    data = _read_json(os.path.join(directory, INSTALL_MARKER))
    return data if isinstance(data, dict) else {}


def _write_install_marker(directory: str, **fields: Any) -> None:
    _atomic_write_json(
        os.path.join(directory, INSTALL_MARKER), {**fields, "complete": True, "installed_at": _iso()}
    )


def _install_complete(directory: str, loader: str, version: str, loader_version: str | None = None) -> bool:
    marker = _read_install_marker(directory)
    if marker.get("complete") and marker.get("loader") == loader and marker.get("minecraft_version") == version:
        if loader_version and marker.get("loader_version") not in (None, loader_version):
            return False
        return True
    legacy = os.path.join(directory, f".{loader}-ready")  # v1 marker
    try:
        with open(legacy, encoding="utf-8") as handle:
            lines = handle.read().split()
    except OSError:
        return False
    if not lines or lines[0] != version:
        return False
    if loader_version and len(lines) > 1 and lines[1] != loader_version:
        return False
    return True


def fabric_installer_version() -> str:
    if FABRIC_INSTALLER_PIN:
        return FABRIC_INSTALLER_PIN
    data, _ = _cached_json("fabric_installers", f"{FABRIC_META_URL}/versions/installer", FABRIC_TTL)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("stable") and item.get("version"):
                return str(item["version"])
        if data and isinstance(data[0], dict) and data[0].get("version"):
            return str(data[0]["version"])
    return "1.1.0"


def fabric_loader_for(mc_version: str) -> str:
    if FABRIC_LOADER_PIN:
        return FABRIC_LOADER_PIN
    try:
        data = _http_json(f"{FABRIC_META_URL}/versions/loader/{quote(mc_version)}", timeout=15)
    except InstallError:
        data = None
    if isinstance(data, list):
        for item in data:
            loader = (item or {}).get("loader") or {}
            if loader.get("stable") and loader.get("version"):
                return str(loader["version"])
        if data:
            loader = (data[0] or {}).get("loader") or {}
            if loader.get("version"):
                return str(loader["version"])
    try:
        modern = is_year_version(mc_version) or parse_version(mc_version) >= (1, 21, 0)
    except InstallError:
        modern = True
    return "0.19.3" if modern else "0.16.14"


def _run_installer(command: list[str], cwd: str, java_home: str | None, what: str) -> None:
    extra = {"JAVA_HOME": java_home} if java_home else None
    try:
        subprocess.run(
            command,
            check=True,
            cwd=cwd,
            timeout=INSTALL_TIMEOUT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=child_env(extra),
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            f"{what} took too long and was stopped. Try again.",
            code="install_failed",
            detail=str(exc.stdout or "")[-3000:] if exc.stdout else str(exc),
        ) from exc
    except subprocess.CalledProcessError as exc:
        output = str(exc.stdout or "")
        lines = [line for line in output.splitlines() if line.strip()]
        hint = lines[-1][:300] if lines else f"exit code {exc.returncode}"
        raise InstallError(
            f"{what} failed: {hint}", code="install_failed", detail=output[-3000:]
        ) from exc
    except OSError as exc:
        raise InstallError(f"{what} couldn't start Java.", code="install_failed", detail=str(exc)) from exc


def install_fabric(version: str, dest: str | None = None, loader: str | None = None) -> str:
    """Install Fabric into ``dest`` (default instances/fabric-<version>).
    Re-runs the installer unless a completed install marker matches."""
    _validate_version(version, "fabric")
    dest = dest or instance_dir("fabric", version)
    launch = os.path.join(dest, "fabric-server-launch.jar")
    if os.path.isfile(launch) and _install_complete(dest, "fabric", version, loader):
        return dest
    os.makedirs(dest, exist_ok=True)
    loader = loader or fabric_loader_for(version)
    installer_version = fabric_installer_version()
    url = f"{FABRIC_MAVEN_URL}/{installer_version}/fabric-installer-{installer_version}.jar"
    installer = download_file(
        url,
        os.path.join(paths()["jars"], f"fabric-installer-{installer_version}.jar"),
        _maven_sha1(url),
        label="the Fabric installer",
        reuse=True,
    )
    try:
        _version_meta(version, network=True)
    except InstallError:
        pass
    major, java, home = select_java(required_java(version, "fabric"))
    _say(f"Installing Fabric {loader} for Minecraft {version} (Java {major})...")
    _run_installer(
        [java, "-jar", installer, "server", "-mcversion", version, "-loader", loader,
         "-downloadMinecraft", "-dir", dest, "-noprofile"],
        dest,
        home,
        f"Installing Fabric for Minecraft {version}",
    )
    if not os.path.isfile(launch):
        raise InstallError(
            "The Fabric installer finished without creating fabric-server-launch.jar.",
            code="install_failed",
        )
    _write_install_marker(dest, loader="fabric", minecraft_version=version, loader_version=loader)
    return dest


def forge_build_for(mc_version: str, forge_version: str | None = None) -> str:
    build = str(forge_version or "").strip() or FORGE_BUILDS.get(mc_version, "")
    if not build:
        raise InstallError(
            f"Railpowered has no Forge build for Minecraft {mc_version}. "
            f"Supported: {', '.join(FORGE_BUILDS)}, or a modpack that names its Forge version.",
            code="invalid_version",
        )
    if not re.fullmatch(r"[0-9][0-9A-Za-z._-]{0,40}", build):
        raise InstallError(f"'{build[:40]}' isn't a Forge version.", code="invalid_version")
    if build.startswith(mc_version + "-"):
        build = build[len(mc_version) + 1:]
    try:
        old = parse_version(mc_version) < (1, 10, 0)
    except InstallError:
        old = False
    if old and not build.endswith(f"-{mc_version}"):
        build = f"{build}-{mc_version}"  # maven paths for <=1.9 Forge carry the suffix
    return build


def _forge_installer_url(mc_version: str, forge_version: str | None = None) -> str:
    build = forge_build_for(mc_version, forge_version)
    rev = f"{mc_version}-{build}"
    return f"{FORGE_MAVEN_URL}/{rev}/forge-{rev}-installer.jar"


def _find_forge_launch(directory: str, mc_version: str | None = None) -> str | None:
    """unix_args.txt (1.17+) or the Forge server jar (older)."""
    forge_libs = os.path.join(directory, "libraries", "net", "minecraftforge", "forge")
    if os.path.isdir(forge_libs):
        candidates = []
        for entry in sorted(os.listdir(forge_libs)):
            args = os.path.join(forge_libs, entry, "unix_args.txt")
            if os.path.isfile(args):
                candidates.append((not (mc_version and entry.startswith(mc_version + "-")), args))
        if candidates:
            return sorted(candidates)[0][1]
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    jars = [
        n for n in names
        if n.lower().startswith("forge-") and n.lower().endswith(".jar") and "installer" not in n.lower()
    ]
    if mc_version:
        jars.sort(key=lambda n: not n.startswith(f"forge-{mc_version}-"))
    return os.path.join(directory, jars[0]) if jars else None


def install_forge(
    version: str,
    dest: str | None = None,
    forge_version: str | None = None,
    installer_path: str | None = None,
) -> str:
    """Install Forge into ``dest`` (default instances/forge-<version>)."""
    _validate_version(version, "forge")
    build = forge_build_for(version, forge_version) if not installer_path else (forge_version or "")
    dest = dest or instance_dir("forge", version)
    if _find_forge_launch(dest, version) and _install_complete(dest, "forge", version, build or None):
        return dest
    os.makedirs(dest, exist_ok=True)
    if not installer_path:
        url = _forge_installer_url(version, build)
        installer_path = download_file(
            url,
            os.path.join(paths()["jars"], os.path.basename(urlparse(url).path)),
            _maven_sha1(url),
            label=f"the Forge installer for {version}",
            reuse=True,
        )
    if version == "1.7.10":
        # The 1.7.10 installer fetches the vanilla jar from a host that no
        # longer exists; it skips the download when the jar is already there.
        seeded = os.path.join(dest, "minecraft_server.1.7.10.jar")
        if not os.path.isfile(seeded):
            shutil.copy2(install_vanilla("1.7.10"), seeded)
    major, java, home = select_java(8 if parse_version(version) < (1, 17, 0) else required_java(version),
                                    8 if parse_version(version) < (1, 17, 0) else None)
    _say(f"Installing Forge {build or ''} for Minecraft {version} (Java {major})...".replace("  ", " "))
    _run_installer([java, "-jar", installer_path, "--installServer"], dest, home,
                   f"Installing Forge for Minecraft {version}")
    if not _find_forge_launch(dest, version):
        raise InstallError(
            "The Forge installer finished without creating a server jar or unix_args.txt.",
            code="install_failed",
        )
    _write_install_marker(dest, loader="forge", minecraft_version=version, loader_version=build or None)
    return dest


def install_vanilla(version: str) -> str:
    """Return the path of the vanilla server jar, downloading it if needed.
    A jar already on the volume is used without any network access."""
    _validate_version(version, "forge")
    dest = vanilla_jar_path(version)
    meta = _version_meta(version, network=False)
    if os.path.isfile(dest):
        if meta is None:
            try:
                meta = _version_meta(version, network=True)
            except InstallError:
                return dest  # offline: trust the jar we already have
        if not meta.get("server_sha1") or _jar_verified(dest, meta, version):
            return dest
        _say(f"The Minecraft {version} server jar failed its checksum; downloading it again.")
    elif meta is None:
        meta = _version_meta(version, network=True)
    if not meta or not meta.get("server_url"):
        raise InstallError(
            f"Minecraft {version} has no official server download.", code="invalid_version"
        )
    _say(f"Downloading the Minecraft {version} server...")
    download_file(meta["server_url"], dest, meta.get("server_sha1"), label=f"Minecraft {version}")
    _mark_jar_verified(dest, meta, version)
    return dest


def _jar_verified(path: str, meta: dict[str, Any], version: str) -> bool:
    st = os.stat(path)
    stamp = meta.get("verified") or {}
    if stamp.get("size") == st.st_size and stamp.get("mtime_ns") == st.st_mtime_ns:
        return True
    if sha1_file(path) != str(meta.get("server_sha1")).lower():
        return False
    _mark_jar_verified(path, meta, version)
    return True


def _mark_jar_verified(path: str, meta: dict[str, Any], version: str) -> None:
    st = os.stat(path)
    meta = dict(meta)
    meta["verified"] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    try:
        _atomic_write_json(_version_cache_path(version), meta)
    except OSError:
        pass


def _rescue_instance_worlds(local_worlds: str, instance_name: str) -> None:
    """Move world folders out of an instance-local worlds/ dir into the shared
    worlds/ dir without overwriting or deleting anything."""
    shared = paths()["worlds"]
    for entry in sorted(os.listdir(local_worlds)):
        source = os.path.join(local_worlds, entry)
        base = entry if NAME_RE.match(entry) else re.sub(r"[^A-Za-z0-9._-]", "-", entry).strip(".-_")[:24] or "world"
        target_name = base
        counter = 1
        while os.path.exists(os.path.join(shared, target_name)):
            counter += 1
            target_name = f"{base[:20]}-{instance_name[:6]}-{counter}"[:32]
        shutil.move(source, _safe_child(shared, target_name))
        if target_name != entry:
            _say(f"Moved world '{entry}' from {instance_name} to the shared worlds as '{target_name}'.")


def _relink_instance_world(run_dir: str, level: str) -> None:
    """Point <instance>/worlds at the shared worlds folder."""
    shared = paths()["worlds"]
    os.makedirs(world_path(level), exist_ok=True)
    link = os.path.join(run_dir, "worlds")
    if os.path.islink(link):
        if os.path.realpath(link) == os.path.realpath(shared):
            return
        os.unlink(link)
    elif os.path.isdir(link):
        _rescue_instance_worlds(link, os.path.basename(run_dir))
        os.rmdir(link)
    elif os.path.exists(link):
        os.unlink(link)
    try:
        os.symlink(os.path.relpath(shared, run_dir), link, target_is_directory=True)
    except OSError as exc:
        raise InstallError(
            "Couldn't link the shared worlds folder into the server folder "
            "(this system doesn't allow symlinks).",
            code="install_failed",
            detail=str(exc),
        ) from exc


def runtime_dir(setup: dict[str, Any]) -> str:
    """Working directory of a setup's server (no install)."""
    stype = setup.get("type") or "vanilla"
    if stype == "vanilla":
        return DATA_DIR
    if stype == "modpack":
        return instance_path(validate_name(setup.get("instance") or "", "modpack"))
    version = str(setup.get("resolved_version") or setup.get("minecraft_version") or "")
    if version == VERSION_LATEST or not version:
        version = _display_version(setup) or FALLBACK_RELEASE
    return instance_dir(str(stype), version)


def _write_eula(directory: str) -> None:
    # One-click deploys accept the EULA on the operator's behalf; the README,
    # the deploy form and the dashboard footer disclose it.
    _atomic_write(
        os.path.join(directory, "eula.txt"),
        f"# Accepted by Railpowered on the server owner's behalf: {EULA_URL}\neula=true\n",
    )


def _install_runtime(setup: dict[str, Any], version: str) -> RuntimeSpec:
    """Download/install whatever the setup needs; writes no settings files."""
    ensure_layout()
    stype = setup["type"]
    loader = None
    prefix: list[str] = []
    if stype == "vanilla":
        jar = install_vanilla(version)
        cwd = DATA_DIR
    elif stype == "fabric":
        cwd = install_fabric(version)
        jar = os.path.join(cwd, "fabric-server-launch.jar")
        loader = "fabric"
    elif stype == "forge":
        cwd = install_forge(version)
        loader = "forge"
        launch = _find_forge_launch(cwd, version)
        if not launch:
            raise InstallError("Forge's launch files are missing.", code="install_failed")
        jar, prefix = ("", [f"@{launch}"]) if launch.endswith("unix_args.txt") else (launch, [])
    elif stype == "modpack":
        cwd = instance_path(validate_name(setup.get("instance") or "", "modpack"))
        if not os.path.isdir(cwd):
            raise InstallError(
                f"Modpack '{setup.get('instance')}' is missing from the volume. Install it again.",
                code="instance_not_found",
            )
        loader = (_read_pack_meta(cwd) or {}).get("loader")
        jar, prefix = _modpack_launch(cwd, version)
    else:
        raise InstallError(f"Unknown server type '{stype}'.", code="invalid_type")
    need = required_java(version, stype, loader)
    maximum = 8 if need == 8 and (stype == "forge" or loader == "forge") else None
    major, java, home = select_java(need, maximum)
    return RuntimeSpec(
        java_bin=java,
        cwd=cwd,
        jar=jar,
        extra_args=["nogui"],
        jvm_args=["-Djava.awt.headless=true", "-Djava.net.preferIPv4Stack=true"],
        prefix_args=prefix,
        version=version,
        type=stype,
        level_name=setup["level_name"],
        java_major=major,
        env={"JAVA_HOME": home} if home else {},
        setup_id=setup.get("id", ""),
        instance=setup.get("instance"),
    )


def prepare_runtime(
    cfg: dict[str, Any] | None = None, persist: bool = True, allow_downgrade: bool = False
) -> RuntimeSpec:
    """Install and configure the active setup of ``cfg`` (default: saved
    config) and return how to launch it. With persist=False nothing in
    manager.json changes (create/activate use that for transactions)."""
    with _RUNTIME_LOCK:
        cfg = _normalize_config(copy.deepcopy(cfg)) if cfg else load_config()
        setup = _active_setup(cfg)
        ensure_layout()
        version = resolve_minecraft_version(setup["minecraft_version"], True, setup["type"])
        level = setup["level_name"]
        os.makedirs(world_path(level), exist_ok=True)
        if not allow_downgrade:
            check_downgrade(level, version)
        spec = _install_runtime(setup, version)
        spec.jvm_args.extend(_log4j_args(version))
        _sync_back_quietly(cfg, setup)
        _write_eula(spec.cwd)
        _apply_managed_properties(version, level, spec.cwd)
        if spec.cwd != DATA_DIR:
            _relink_instance_world(spec.cwd, level)
        _render_access_lists(version, spec.cwd, network=True)
        _remember_prepared_properties()
        if persist and setup.get("resolved_version") != version:
            def apply(fresh: dict[str, Any]) -> None:
                for item in fresh["profiles"]:
                    if item["id"] == setup["id"]:
                        item["resolved_version"] = version
            _mutate_config(apply)
        return spec


# ---------------------------------------------------------------------------
# Available versions
# ---------------------------------------------------------------------------


def list_available_versions() -> dict[str, Any]:
    """Versions for the setup form. Never fails offline (cached or curated)."""
    manifest, fresh = _manifest(network=True)
    latest = _manifest_latest(manifest) or cached_latest_release()
    releases: list[str] = []
    if manifest:
        for item in manifest["versions"]:
            vid = item.get("id")
            key = version_key(str(vid)) if vid else None
            if item.get("type") == "release" and key and VERSION_RE.match(str(vid)):
                if key[:3] >= OLDEST_SERVER_JAR:
                    releases.append(str(vid))
    else:
        releases = list(CURATED_VANILLA)
        for extra in _installed_vanilla_versions():
            if extra not in releases:
                releases.append(extra)
        if latest and latest not in releases:
            releases.insert(0, latest)
    releases.sort(key=lambda v: version_key(v) or (0,), reverse=True)

    def item(vid: str, popular: set[str]) -> dict[str, Any]:
        label = f"{vid} (latest)" if vid == latest else vid
        return {"id": vid, "label": label, "popular": vid in popular or vid == latest}

    vanilla_popular = set(CURATED_VANILLA)
    vanilla = [item(v, vanilla_popular) for v in releases]

    games, fabric_fresh = _cached_json("fabric_games", f"{FABRIC_META_URL}/versions/game", FABRIC_TTL)
    fabric_ids: list[str] = []
    if isinstance(games, list):
        release_set = set(releases) if manifest else None
        for entry in games:
            vid = str((entry or {}).get("version") or "")
            if (entry or {}).get("stable") and VERSION_RE.match(vid) and (release_set is None or vid in release_set):
                fabric_ids.append(vid)
    if not fabric_ids:
        fabric_ids = list(dict.fromkeys(([latest] if latest else []) + CURATED_FABRIC))
    fabric_ids.sort(key=lambda v: version_key(v) or (0,), reverse=True)
    fabric = [item(v, set(CURATED_FABRIC)) for v in fabric_ids]

    forge = [
        {"id": mc, "label": f"{mc} (Forge {build.split('-')[0]})", "popular": True, "forge_version": build}
        for mc, build in FORGE_BUILDS.items()
    ]
    return {
        "vanilla": vanilla,
        "fabric": fabric,
        "forge": forge,
        "latest": latest,
        "snapshots": False,
        "offline": not (fresh and (fabric_fresh or games is not None)),
    }


# ---------------------------------------------------------------------------
# Modpacks
# ---------------------------------------------------------------------------


@dataclass
class PackInfo:
    minecraft_version: str | None = None
    loader: str | None = None  # fabric | forge | neoforge | quilt | None
    loader_version: str | None = None
    name: str | None = None
    source: str = "zip"
    curseforge_client: bool = False


def _validate_pack_name(name: Any) -> str:
    text = validate_name(name, "modpack")
    if text.lower().startswith(RESERVED_INSTANCE_PREFIXES):
        raise InstallError(
            "Modpack names can't start with 'fabric-' or 'forge-' (those folders hold plain "
            "Fabric and Forge setups).",
            code="invalid_name",
        )
    return text


def _extract_limit(dest: str) -> int:
    configured = _env_int("MAX_EXTRACT_MB", 8192, minimum=1) * MIB
    try:
        free = shutil.disk_usage(dest).free - 256 * MIB
    except OSError:
        free = configured
    return max(0, min(configured, free))


def _safe_extract_zip(archive_path: str, dest: str, max_total: int | None = None) -> None:
    """Extract a zip, refusing absolute/parent paths, huge or bomb-like content."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InstallError(
            "That file isn't a valid .zip or .mrpack modpack.", code="invalid_archive", detail=str(exc)
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise InstallError("That modpack has too many files.", code="too_large")
        limit = max_total if max_total is not None else _extract_limit(dest)
        if sum(info.file_size for info in infos) > limit:
            raise InstallError(
                "That modpack is too big to unpack on this volume.", code="too_large"
            )
        written = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if name.startswith("/") or any(p == ".." for p in parts) or (parts and ":" in parts[0]):
                raise InstallError(f"Refusing unsafe path in the archive: {name[:120]}", code="invalid_archive")
            if not parts:
                continue
            target = os.path.join(dest, *parts)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                with archive.open(info) as source, open(target, "wb") as out:
                    while True:
                        chunk = source.read(MIB)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            raise InstallError(
                                "That modpack is too big to unpack on this volume.", code="too_large"
                            )
                        out.write(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
                raise InstallError(
                    "The modpack archive is damaged.", code="invalid_archive", detail=str(exc)
                ) from exc


def _zip_root_has(archive_path: str, inner: str) -> bool:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            return any(item.filename.replace("\\", "/").lstrip("/") == inner for item in archive.infolist())
    except (zipfile.BadZipFile, OSError):
        return False


_CONTENT_DIRS = {
    "mods", "config", "libraries", "defaultconfigs", "kubejs", "scripts", "world", "worlds",
    "overrides", "server-overrides", "resourcepacks", "datapacks", ".fabric", "versions", "plugins",
}
_JUNK = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}


def _hoist_single_folder(directory: str) -> None:
    """Server packs often unpack into one top-level folder; move its contents up."""
    for junk in _JUNK:
        path = os.path.join(directory, junk)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.unlink(path)
    entries = os.listdir(directory)
    if len(entries) != 1:
        return
    only = os.path.join(directory, entries[0])
    if not os.path.isdir(only) or os.path.islink(only) or entries[0].lower() in _CONTENT_DIRS:
        return
    temp = os.path.join(directory, f".hoist-{secrets.token_hex(4)}")
    os.replace(only, temp)
    for name in os.listdir(temp):
        os.replace(os.path.join(temp, name), os.path.join(directory, name))
    os.rmdir(temp)


def _read_pack_meta(directory: str) -> dict[str, Any] | None:
    data = _read_json(os.path.join(directory, PACK_META_FILE))
    if not isinstance(data, dict):
        return None
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    meta = dict(data)
    meta.setdefault("minecraft_version", deps.get("minecraft"))
    if not meta.get("loader"):
        for key, loader in (("fabric-loader", "fabric"), ("forge", "forge"), ("neoforge", "neoforge"), ("quilt-loader", "quilt")):
            if deps.get(key):
                meta["loader"] = loader
                meta.setdefault("loader_version", deps[key])
    return meta


def _parse_loader_id(text: str) -> tuple[str | None, str | None]:
    lowered = str(text or "").strip().lower()
    for loader in ("neoforge", "forge", "fabric", "quilt"):
        if lowered.startswith(loader + "-"):
            return loader, str(text).strip()[len(loader) + 1:] or None
        if lowered == loader:
            return loader, None
    return None, None


def _jar_inner_version(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                data = json.loads(archive.read("version.json").decode("utf-8"))
            except KeyError:
                return None
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError):
        return None
    for key in ("id", "name"):
        value = str((data or {}).get(key) or "")
        if VERSION_RE.match(value):
            return value
    return None


def _mod_loader_from_mods(directory: str) -> str | None:
    mods = os.path.join(directory, "mods")
    if not os.path.isdir(mods):
        return None
    for name in sorted(os.listdir(mods))[:40]:
        if not name.endswith(".jar"):
            continue
        try:
            with zipfile.ZipFile(os.path.join(mods, name)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            continue
        if "fabric.mod.json" in names:
            return "fabric"
        if "META-INF/neoforge.mods.toml" in names:
            return "neoforge"
        if "META-INF/mods.toml" in names or "mcmod.info" in names:
            return "forge"
        if "quilt.mod.json" in names:
            return "quilt"
    return None


def _has_mod_jars(directory: str) -> bool:
    mods = os.path.join(directory, "mods")
    return os.path.isdir(mods) and any(n.endswith(".jar") for n in os.listdir(mods))


def _detect_pack(directory: str) -> PackInfo:
    """Work out Minecraft version and loader of an unpacked server zip."""
    info = PackInfo()
    names = os.listdir(directory)
    lower = {n.lower(): n for n in names}

    meta = _read_pack_meta(directory)
    if meta:
        info.minecraft_version = meta.get("minecraft_version") or None
        info.loader = meta.get("loader")
        info.loader_version = meta.get("loader_version")
        info.name = meta.get("name")

    manifest = _read_json(os.path.join(directory, "manifest.json"))
    if isinstance(manifest, dict) and isinstance(manifest.get("minecraft"), dict):
        mc = manifest["minecraft"]
        info.minecraft_version = info.minecraft_version or str(mc.get("version") or "") or None
        loaders = [l for l in mc.get("modLoaders") or [] if isinstance(l, dict)]
        loaders.sort(key=lambda l: not l.get("primary"))
        if loaders and not info.loader:
            info.loader, info.loader_version = _parse_loader_id(str(loaders[0].get("id") or ""))
        info.name = info.name or manifest.get("name")
        info.curseforge_client = bool(manifest.get("files")) and manifest.get("manifestType") == "minecraftModpack"

    variables = os.path.join(directory, "variables.txt")
    if os.path.isfile(variables):
        values = {}
        with open(variables, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip().upper()] = value.strip().strip('"')
        info.minecraft_version = info.minecraft_version or values.get("MINECRAFT_VERSION") or None
        if not info.loader and values.get("MODLOADER"):
            info.loader = values["MODLOADER"].strip().lower() or None
            info.loader_version = values.get("MODLOADER_VERSION") or None

    for name in names:
        match = re.fullmatch(r"fabric-server-mc\.(.+?)-loader\.(.+?)-launcher\..+\.jar", name)
        if match:
            info.minecraft_version = info.minecraft_version or match.group(1)
            info.loader = info.loader or "fabric"
            info.loader_version = info.loader_version or match.group(2)
    intermediary = os.path.join(directory, "libraries", "net", "fabricmc", "intermediary")
    if os.path.isdir(intermediary):
        versions = [v for v in os.listdir(intermediary) if VERSION_RE.match(v)]
        if versions:
            info.minecraft_version = info.minecraft_version or _newest(versions)
            info.loader = info.loader or "fabric"
    fabric_server = os.path.join(directory, ".fabric", "server")
    if os.path.isdir(fabric_server):
        for name in os.listdir(fabric_server):
            match = re.fullmatch(r"(.+)-server\.jar", name)
            if match and VERSION_RE.match(match.group(1)):
                info.minecraft_version = info.minecraft_version or match.group(1)
                info.loader = info.loader or "fabric"
    if "fabric-server-launch.jar" in lower:
        info.loader = info.loader or "fabric"

    forge_libs = os.path.join(directory, "libraries", "net", "minecraftforge", "forge")
    if os.path.isdir(forge_libs):
        for entry in sorted(os.listdir(forge_libs)):
            if "-" in entry:
                mc, build = entry.split("-", 1)
                if VERSION_RE.match(mc):
                    info.minecraft_version = info.minecraft_version or mc
                    info.loader = info.loader or "forge"
                    info.loader_version = info.loader_version or build
    for name in sorted(names):
        match = re.fullmatch(r"forge-(\d[\w.]*?)-(\d[\w.\-]*?)(-universal|-installer|-server)?\.jar", name)
        if match:
            info.minecraft_version = info.minecraft_version or match.group(1)
            info.loader = info.loader or "forge"
            info.loader_version = info.loader_version or match.group(2)
    for name in names:
        match = re.fullmatch(r"minecraft_server\.(.+)\.jar", name)
        if match and VERSION_RE.match(match.group(1)):
            info.minecraft_version = info.minecraft_version or match.group(1)
    if not info.minecraft_version:
        for candidate in ("server.jar", "minecraft_server.jar"):
            if candidate in lower:
                info.minecraft_version = _jar_inner_version(os.path.join(directory, lower[candidate]))
                if info.minecraft_version:
                    break
    if not info.minecraft_version:
        data = _read_json(os.path.join(directory, "version.json"))
        if isinstance(data, dict):
            for key in ("id", "name"):
                value = str(data.get(key) or "")
                if VERSION_RE.match(value):
                    info.minecraft_version = value
                    break
    if not info.loader:
        info.loader = _mod_loader_from_mods(directory)
    if info.minecraft_version and not VERSION_RE.match(str(info.minecraft_version)):
        info.minecraft_version = None
    if info.loader not in (None, "fabric", "forge", "neoforge", "quilt"):
        info.loader = None
    return info


def _modpack_launch(directory: str, version: str | None = None) -> tuple[str, list[str]]:
    """(jar, prefix_args) that start an installed modpack."""
    forge = _find_forge_launch(directory, version)
    if forge and forge.endswith("unix_args.txt"):
        return "", [f"@{forge}"]
    preferred = ["fabric-server-launch.jar"]
    names = sorted(os.listdir(directory))
    preferred += [n for n in names if re.fullmatch(r"fabric-server-mc\..+-launcher\..+\.jar", n)]
    for name in preferred:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path, []
    if forge:
        return forge, []
    for name in ("server.jar", "minecraft_server.jar"):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path, []
    jars = [n for n in names if n.endswith(".jar") and "installer" not in n.lower()]
    if jars:
        return os.path.join(directory, jars[0]), []
    raise InstallError(
        "Couldn't find a server jar in the modpack. Use a Modrinth .mrpack or the pack's "
        "server-pack zip.",
        code="unsupported_pack",
    )


def _install_loader(staging: str, info: PackInfo) -> None:
    mc = str(info.minecraft_version)
    if info.loader in ("neoforge", "quilt"):
        name = "NeoForge" if info.loader == "neoforge" else "Quilt"
        raise InstallError(
            f"{name} modpacks aren't supported yet (Railpowered runs Fabric and Forge).",
            code="unsupported_pack",
        )
    if info.loader == "forge":
        if _find_forge_launch(staging, mc):
            return
        local = next(
            (os.path.join(staging, n) for n in sorted(os.listdir(staging))
             if n.lower().startswith("forge-") and n.lower().endswith("-installer.jar")),
            None,
        )
        install_forge(mc, dest=staging, forge_version=info.loader_version, installer_path=local)
        if local and os.path.isfile(local):
            os.unlink(local)
        return
    if info.loader == "fabric":
        if os.path.isfile(os.path.join(staging, "fabric-server-launch.jar")) or any(
            re.fullmatch(r"fabric-server-mc\..+-launcher\..+\.jar", n) for n in os.listdir(staging)
        ):
            return
        install_fabric(mc, dest=staging, loader=info.loader_version)
        return
    try:
        _modpack_launch(staging, mc)
        return
    except InstallError:
        pass
    if _has_mod_jars(staging):
        raise InstallError(
            "This pack has mods but doesn't say which loader they need. Use a Modrinth .mrpack "
            "or the pack's server-pack zip.",
            code="unsupported_pack",
        )
    # A pack of datapacks/resource packs only: run it on the vanilla server.
    shutil.copy2(install_vanilla(mc), os.path.join(staging, "server.jar"))


def _pick_mrpack_url(urls: list[Any]) -> str | None:
    for url in urls:
        parsed = urlparse(str(url))
        if parsed.scheme == "https" and (parsed.hostname or "").lower() in MRPACK_HOSTS:
            return str(url)
    return None


def _install_mrpack(unpacked: str, staging: str) -> PackInfo:
    index = _read_json(os.path.join(unpacked, "modrinth.index.json"))
    if not isinstance(index, dict):
        raise InstallError("This .mrpack is missing modrinth.index.json.", code="invalid_archive")
    deps = index.get("dependencies") if isinstance(index.get("dependencies"), dict) else {}
    info = PackInfo(minecraft_version=str(deps.get("minecraft") or "") or None, name=index.get("name"), source="mrpack")
    for key, loader in (("fabric-loader", "fabric"), ("forge", "forge"), ("neoforge", "neoforge"), ("quilt-loader", "quilt")):
        if deps.get(key):
            info.loader, info.loader_version = loader, str(deps[key])
            break
    if not info.minecraft_version or not VERSION_RE.match(info.minecraft_version):
        raise InstallError("This .mrpack doesn't say which Minecraft version it's for.", code="invalid_archive")
    if info.loader in ("neoforge", "quilt"):
        _install_loader(staging, info)  # raises the friendly unsupported error
    jobs = []
    for entry in index.get("files") or []:
        if not isinstance(entry, dict):
            continue
        if ((entry.get("env") or {}).get("server")) == "unsupported":
            continue
        rel = str(entry.get("path") or "").replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or rel.startswith("/") or ".." in parts or ":" in parts[0]:
            raise InstallError(f"Unsafe file path in the .mrpack: {rel[:120]}", code="invalid_archive")
        url = _pick_mrpack_url(entry.get("downloads") or [])
        if not url:
            raise InstallError(
                f"The .mrpack downloads {parts[-1]} from a host Railpowered doesn't trust.",
                code="invalid_archive",
            )
        hashes = entry.get("hashes") or {}
        sha1, sha512 = hashes.get("sha1"), hashes.get("sha512")
        if not sha1 and not sha512:
            raise InstallError(f"The .mrpack has no checksum for {parts[-1]}.", code="invalid_archive")
        size = entry.get("fileSize")
        jobs.append((url, os.path.join(staging, *parts), sha1, sha512, size if isinstance(size, int) and size > 0 else None, parts[-1]))
    if jobs:
        _say(f"Downloading {len(jobs)} modpack files...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(download_file, url, dest, sha1, sha512=sha512, max_bytes=size, label=label)
                for url, dest, sha1, sha512, size, label in jobs
            ]
            for future in futures:
                future.result()
    for folder in ("overrides", "server-overrides"):
        source = os.path.join(unpacked, folder)
        if os.path.isdir(source):
            shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=False)
    return info


def install_modpack_archive(
    archive_path: str, name: str, level_name: str | None = None, allow_downgrade: bool = False
) -> dict[str, Any]:
    """Install a .mrpack or server zip as instances/<name>. Everything happens
    in a staging folder; the old instance is replaced only on success.
    Returns setup fields for create_setup (the archive file is left alone)."""
    name = _validate_pack_name(name)
    level = validate_name(level_name or name, "world")
    with _RUNTIME_LOCK:
        ensure_layout()
        instances = paths()["instances"]
        staging = tempfile.mkdtemp(prefix=f".staging-{name}-", dir=instances)
        unpacked = tempfile.mkdtemp(prefix=f".unpack-{name}-", dir=instances)
        try:
            _safe_extract_zip(archive_path, unpacked)
            if os.path.isfile(os.path.join(unpacked, "modrinth.index.json")):
                info = _install_mrpack(unpacked, staging)
            else:
                os.rmdir(staging)
                os.replace(unpacked, staging)
                os.makedirs(unpacked)
                _hoist_single_folder(staging)
                info = _detect_pack(staging)
                if info.curseforge_client and not _has_mod_jars(staging) and not _has_mod_jars(os.path.join(staging, "overrides")):
                    raise InstallError(
                        "This is a CurseForge client pack: it lists mods by ID instead of including "
                        "them. Download the pack's server pack from CurseForge and upload that.",
                        code="unsupported_pack",
                    )
                if not info.minecraft_version:
                    raise InstallError(
                        "Couldn't tell which Minecraft version this pack is for. Use a Modrinth "
                        ".mrpack or a server-pack zip that includes its server or loader files.",
                        code="unsupported_pack",
                    )
            _install_loader(staging, info)
            mc = str(info.minecraft_version)
            _modpack_launch(staging, mc)  # must be startable before we swap it in
            if os.path.isdir(world_path(level)):
                previous = _downgrade_from(level, mc)
                if previous and not allow_downgrade:
                    check_downgrade(level, mc)
            deps = {"minecraft": mc}
            if info.loader == "fabric":
                deps["fabric-loader"] = info.loader_version or ""
            elif info.loader == "forge":
                deps["forge"] = info.loader_version or ""
            _atomic_write_json(
                os.path.join(staging, PACK_META_FILE),
                {
                    "name": info.name or name,
                    "minecraft_version": mc,
                    "loader": info.loader,
                    "loader_version": info.loader_version,
                    "source": info.source,
                    "installed_at": _iso(),
                    "dependencies": deps,
                },
            )
            final = instance_path(name)
            old = None
            if os.path.lexists(final):
                old = os.path.join(instances, f".old-{name}-{secrets.token_hex(4)}")
                os.replace(final, old)
            try:
                os.replace(staging, final)
            except OSError:
                if old:
                    os.replace(old, final)
                raise
            staging = ""
            if old:
                local_worlds = os.path.join(old, "worlds")
                if os.path.isdir(local_worlds) and not os.path.islink(local_worlds):
                    _rescue_instance_worlds(local_worlds, name)
                _rmtree_child(instances, os.path.basename(old))
        finally:
            for leftover in (staging, unpacked):
                if leftover and os.path.isdir(leftover):
                    shutil.rmtree(leftover, ignore_errors=True)

    def retarget(cfg: dict[str, Any]) -> None:
        for setup in cfg["profiles"]:
            if setup["type"] == "modpack" and setup.get("instance") == name:
                setup["minecraft_version"] = mc
                setup["resolved_version"] = mc

    _mutate_config(retarget)
    _say(f"Installed modpack {info.name or name} (Minecraft {mc}, {info.loader or 'vanilla'}).")
    return {
        "type": "modpack",
        "minecraft_version": mc,
        "level_name": level,
        "instance": name,
        "name": _clean_setup_name(info.name) or name,
        "loader": info.loader,
    }


def install_modpack_from_url(
    url: str, name: str, level_name: str | None = None, allow_downgrade: bool = False
) -> dict[str, Any]:
    """Download a .mrpack/.zip (fresh every time) and install it."""
    name = _validate_pack_name(name)
    if level_name is not None:
        validate_name(level_name, "world")
    url = _require_https(url)
    ensure_layout()
    fd, tmp = tempfile.mkstemp(prefix=".download-", suffix=".pack", dir=paths()["modpacks"])
    os.close(fd)
    try:
        _say("Downloading the modpack...")
        download_file(url, tmp, max_bytes=max_upload_bytes(), label="the modpack", reuse=False)
        if not zipfile.is_zipfile(tmp):
            raise InstallError(
                "That link didn't return a .mrpack or .zip file. On Modrinth, copy the download "
                "link of a version's .mrpack file.",
                code="invalid_archive",
            )
        return install_modpack_archive(tmp, name, level_name, allow_downgrade)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# server.properties and settings
# ---------------------------------------------------------------------------

_PROP_LINE_RE = re.compile(r"^\s*([^=:\s#!][^=:]*?)\s*[=:]\s?(.*)$")


def _unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("u"):
            try:
                return chr(int(token[1:], 16))
            except ValueError:
                return token
        return {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(token, token)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", repl, value)


def _escape(value: str) -> str:
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ord(ch) > 126 or ord(ch) < 32:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    text = "".join(out)
    return "\\" + text if text.startswith((" ", "#", "!")) else text


class _Properties:
    """Ordered server.properties that keeps comments and unknown keys."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = list(lines or [])

    @classmethod
    def load(cls, path: str) -> "_Properties":
        try:
            with open(path, encoding="latin-1") as handle:
                return cls(handle.read().splitlines())
        except FileNotFoundError:
            return cls()

    def _index(self, key: str) -> int | None:
        for index, line in enumerate(self.lines):
            match = _PROP_LINE_RE.match(line)
            if match and match.group(1).strip() == key:
                return index
        return None

    def get(self, key: str) -> str | None:
        index = self._index(key)
        if index is None:
            return None
        return _unescape(_PROP_LINE_RE.match(self.lines[index]).group(2))

    def set(self, key: str, value: str) -> None:
        line = f"{key}={_escape(str(value))}"
        index = self._index(key)
        if index is None:
            self.lines.append(line)
        else:
            self.lines[index] = line

    def as_dict(self) -> dict[str, str]:
        values = {}
        for line in self.lines:
            match = _PROP_LINE_RE.match(line)
            if match:
                values[match.group(1).strip()] = _unescape(match.group(2))
        return values

    def dump(self) -> str:
        return "\n".join(self.lines) + "\n"


def read_properties(path: str | None = None) -> dict[str, str]:
    return _Properties.load(path or paths()["properties"]).as_dict()


def write_properties(values: dict[str, Any], path: str | None = None) -> None:
    """Update keys in server.properties, keeping every other line."""
    target = path or paths()["properties"]
    with _CONFIG_LOCK:
        props = _Properties.load(target)
        for key, value in values.items():
            props.set(str(key), str(value))
        _atomic_write(target, props.dump().encode("latin-1"))


DIFFICULTIES = ["peaceful", "easy", "normal", "hard"]
GAMEMODES = ["survival", "creative", "adventure", "spectator"]
LEVEL_TYPES = ["normal", "flat", "large_biomes", "amplified"]


def _names_era(version: str) -> bool:
    return is_year_version(version) or parse_version(version) >= (1, 14, 0)


def _supports(version: str, minimum: tuple[int, int, int]) -> bool:
    return is_year_version(version) or parse_version(version) >= minimum


def _normalize_enum(key: str, value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if key == "difficulty":
        return DIFFICULTIES[int(text)] if text.isdigit() and int(text) < 4 else text
    if key == "gamemode":
        return GAMEMODES[int(text)] if text.isdigit() and int(text) < 4 else text
    if key == "level-type":
        text = text.removeprefix("minecraft:")
        return {"default": "normal", "largebiomes": "large_biomes", "default_1_1": "normal"}.get(text, text)
    return text


def _format_enum(key: str, value: str, version: str) -> str:
    if key in ("difficulty", "gamemode") and not _names_era(version):
        options = DIFFICULTIES if key == "difficulty" else GAMEMODES
        return str(options.index(value)) if value in options else value
    if key == "level-type" and value in LEVEL_TYPES:
        if _supports(version, (1, 19, 0)):
            return f"minecraft:{value}"
        if _supports(version, (1, 13, 0)):
            return {"normal": "default", "large_biomes": "largeBiomes"}.get(value, value)
        return {"normal": "DEFAULT", "large_biomes": "LARGEBIOMES"}.get(value, value.upper())
    return value


def settings_schema(version: str) -> dict[str, dict[str, Any]]:
    """Editable server.properties keys for ``version`` (unsupported keys hidden)."""
    view_max = 32 if _supports(version, (1, 14, 0)) else 15
    gamemodes = [g for g in GAMEMODES if g != "spectator" or _supports(version, (1, 8, 0))]
    schema: dict[str, dict[str, Any]] = {
        "motd": {"type": "string", "max": 120, "label": "Server description (MOTD)",
                 "help": "Shown under the server name in the multiplayer list."},
        "difficulty": {"type": "enum", "options": DIFFICULTIES, "label": "Difficulty",
                       "help": "Peaceful has no hostile mobs."},
        "gamemode": {"type": "enum", "options": gamemodes, "label": "Game mode",
                     "help": "The mode new players start in."},
        "hardcore": {"type": "bool", "label": "Hardcore",
                     "help": "One life: players become spectators when they die."},
        "pvp": {"type": "bool", "label": "PvP", "help": "Let players hurt each other."},
        "max-players": {"type": "int", "min": 1, "max": 500, "label": "Max players",
                        "help": "How many players can be online at once."},
        "view-distance": {"type": "int", "min": 3, "max": view_max, "label": "View distance",
                          "help": "Chunks sent to players. Lower values use less memory and CPU."},
        "simulation-distance": {"type": "int", "min": 3, "max": 32, "label": "Simulation distance",
                                "help": "Chunks around players where mobs and crops are updated."},
        "spawn-protection": {"type": "int", "min": 0, "max": 256, "label": "Spawn protection",
                             "help": "Radius around spawn that only operators can build in (0 = off)."},
        "allow-flight": {"type": "bool", "label": "Allow flight",
                         "help": "Don't kick players for flying (needed by some mods)."},
        "allow-nether": {"type": "bool", "label": "Allow the Nether", "help": "Let players enter the Nether."},
        "level-seed": {"type": "string", "max": 64, "label": "World seed",
                       "help": "Only affects new worlds; existing worlds keep their seed."},
        "level-type": {"type": "enum", "options": list(LEVEL_TYPES), "label": "World type",
                       "help": "Only affects new worlds."},
        "online-mode": {"type": "bool", "label": "Online mode",
                        "help": "Check players' Minecraft accounts with Mojang. Turn off only if you "
                                "know why: anyone could then join with any name."},
        "enable-command-block": {"type": "bool", "label": "Command blocks",
                                 "help": "Allow command blocks to run commands."},
    }
    if not _supports(version, (1, 18, 0)):
        del schema["simulation-distance"]
    if is_legacy_protocol(version):
        del schema["online-mode"]  # always off: the old login servers are gone
    if not _supports(version, (1, 7, 2)):
        schema["level-type"]["options"] = ["normal", "flat", "large_biomes"]
    if not _supports(version, (1, 4, 2)):
        del schema["enable-command-block"]
    return schema


PROPERTY_DEFAULTS = {
    "motd": "A Railpowered Minecraft server",
    "difficulty": "easy",
    "gamemode": "survival",
    "hardcore": "false",
    "pvp": "true",
    "max-players": "20",
    "view-distance": "8",
    "simulation-distance": "6",
    "spawn-protection": "0",
    "allow-flight": "false",
    "allow-nether": "true",
    "level-seed": "",
    "level-type": "normal",
    "online-mode": "true",
    "enable-command-block": "false",
    "white-list": "false",
    "enforce-whitelist": "false",
}
# Written when missing so new servers start with modest load.
_SEED_KEYS = ("motd", "view-distance", "simulation-distance", "spawn-protection", "online-mode", "white-list")


def _apply_managed_properties(version: str, level: str, run_dir: str) -> None:
    """Rewrite only the keys Railpowered owns; copy the file into instance dirs."""
    with _CONFIG_LOCK:
        path = paths()["properties"]
        props = _Properties.load(path)
        before = props.dump()
        if not props.lines:
            props.lines.append(f"# Minecraft server settings, managed by {APP_NAME} (edit them in the dashboard)")
        for key in _SEED_KEYS:
            if key == "simulation-distance" and not _supports(version, (1, 18, 0)):
                continue
            if props.get(key) is None:
                props.set(key, PROPERTY_DEFAULTS[key])
        props.set("server-port", "25565")
        props.set("server-ip", "")
        props.set("level-name", f"worlds/{level}")
        cfg = load_config()
        saved = (cfg.get("settings") or {}).get("legacy_saved_online_mode")
        if is_legacy_protocol(version):
            current = (props.get("online-mode") or "true").strip().lower()
            if current != "false":
                _mutate_config(lambda c: c["settings"].__setitem__("legacy_saved_online_mode",
                                                                   "true" if current == "true" else "false"))
                props.set("online-mode", "false")
        elif saved:
            props.set("online-mode", saved)
            _mutate_config(lambda c: c["settings"].pop("legacy_saved_online_mode", None))
        for key in ("difficulty", "gamemode", "level-type"):
            value = _normalize_enum(key, props.get(key))
            if value:
                props.set(key, _format_enum(key, value, version))
        text = props.dump()
        if text != before:
            _atomic_write(path, text.encode("latin-1"))
        if os.path.realpath(run_dir) != os.path.realpath(DATA_DIR):
            _atomic_write(os.path.join(run_dir, "server.properties"), text.encode("latin-1"))


_prepared_digest: str | None = None


def _user_properties_digest() -> str:
    values = read_properties()
    relevant = {k: values.get(k) for k in PROPERTY_DEFAULTS}
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def _remember_prepared_properties() -> None:
    global _prepared_digest
    _prepared_digest = _user_properties_digest()


def restart_required() -> bool:
    """True when settings changed since the running server was prepared.
    (Only meaningful while the server runs; the caller checks that.)"""
    return _prepared_digest is not None and _prepared_digest != _user_properties_digest()


def _active_version(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    return _display_version(_active_setup(cfg)) or FALLBACK_RELEASE


def _typed(schema_item: dict[str, Any], key: str, raw: str | None) -> Any:
    value = raw if raw is not None else PROPERTY_DEFAULTS.get(key, "")
    kind = schema_item["type"]
    if kind == "bool":
        return str(value).strip().lower() == "true"
    if kind == "int":
        try:
            return int(str(value).strip())
        except ValueError:
            return int(PROPERTY_DEFAULTS.get(key, "0") or 0)
    if kind == "enum":
        return _normalize_enum(key, value) or PROPERTY_DEFAULTS.get(key)
    return value


def get_settings() -> dict[str, Any]:
    cfg = load_config()
    version = _active_version(cfg)
    schema = copy.deepcopy(settings_schema(version))
    raw = read_properties()
    values = {}
    for key, item in schema.items():
        values[key] = _typed(item, key, raw.get(key))
        if item["type"] == "enum" and values[key] not in item["options"]:
            item["options"] = item["options"] + [values[key]]
    if is_legacy_protocol(version):
        values["online-mode"] = False
    return {
        "properties": values,
        "railpowered": railpowered_settings(cfg),
        "restart_required": restart_required(),
        "schema": schema,
        "minecraft_version": version,
    }


def _validate_property(key: str, item: dict[str, Any], value: Any) -> str:
    label = item["label"]
    kind = item["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        if str(value).lower() in ("true", "false"):
            return str(value).lower()
        raise InstallError(f"{label} must be on or off.", code="invalid_setting")
    if kind == "int":
        try:
            number = int(value)
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
        except (TypeError, ValueError):
            raise InstallError(f"{label} must be a whole number.", code="invalid_setting") from None
        if number < item["min"] or number > item["max"]:
            raise InstallError(
                f"{label} must be between {item['min']} and {item['max']}.", code="invalid_setting"
            )
        return str(number)
    if kind == "enum":
        text = _normalize_enum(key, str(value)) or ""
        if text not in item["options"]:
            raise InstallError(
                f"{label} must be one of: {', '.join(item['options'])}.", code="invalid_setting"
            )
        return text
    text = str(value if value is not None else "")
    if "\n" in text or "\r" in text:
        raise InstallError(f"{label} must be a single line.", code="invalid_setting")
    if len(text) > item.get("max", 256):
        raise InstallError(f"{label} can be at most {item['max']} characters.", code="invalid_setting")
    return text


def update_settings(
    props: dict[str, Any] | None = None, railpowered: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and save dashboard settings. server.properties keeps every
    key it doesn't touch. Returns {"ok": True, "restart_required": bool}."""
    if props is not None and not isinstance(props, dict):
        raise InstallError("Settings must be an object.", code="invalid_setting")
    if railpowered is not None and not isinstance(railpowered, dict):
        raise InstallError("Railpowered settings must be an object.", code="invalid_setting")
    with _CONFIG_LOCK:
        cfg = load_config()
        version = _active_version(cfg)
        schema = settings_schema(version)
        updates: dict[str, str] = {}
        for key, value in (props or {}).items():
            if key not in schema:
                raise InstallError(
                    f"'{str(key)[:40]}' can't be changed here on Minecraft {version}.",
                    code="invalid_setting",
                )
            text = _validate_property(key, schema[key], value)
            if schema[key]["type"] == "enum":
                text = _format_enum(key, text, version)
            updates[key] = text
        rp_updates: dict[str, Any] = {}
        for key, value in (railpowered or {}).items():
            if key == "idle_timeout_minutes":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
                    raise InstallError(
                        "Sleep timer must be between 0 (never sleep) and 1440 minutes.",
                        code="invalid_setting",
                    )
                rp_updates[key] = value
            elif key == "auto_start":
                if not isinstance(value, bool):
                    raise InstallError("Start on deploy must be on or off.", code="invalid_setting")
                rp_updates[key] = value
            else:
                raise InstallError(f"Unknown Railpowered setting '{str(key)[:40]}'.", code="invalid_setting")
        if updates:
            write_properties(updates)
            run = _active_run_dir(cfg)
            if run and os.path.isfile(os.path.join(run, "server.properties")):
                write_properties(updates, os.path.join(run, "server.properties"))
        if rp_updates:
            _mutate_config(lambda c: c["settings"].update(rp_updates))
    return {"ok": True, "restart_required": restart_required()}


def _active_run_dir(cfg: dict[str, Any]) -> str | None:
    try:
        run = runtime_dir(_active_setup(cfg))
    except InstallError:
        return None
    if os.path.realpath(run) == os.path.realpath(DATA_DIR) or not os.path.isdir(run):
        return None
    return run


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

_BACKUP_LOCK = threading.Lock()
_BACKUP_META_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def disk_usage() -> dict[str, int]:
    ensure_layout()
    usage = shutil.disk_usage(DATA_DIR)
    return {"used_bytes": usage.total - usage.free, "total_bytes": usage.total, "free_bytes": usage.free}


def backup_file(name: str) -> str:
    """Absolute path of an existing backup archive (validated)."""
    text = str(name or "")
    if not BACKUP_NAME_RE.match(text):
        raise InstallError("That isn't a backup name.", code="backup_not_found")
    path = _safe_child(paths()["backups"], text)
    if not os.path.isfile(path):
        raise InstallError("That backup doesn't exist.", code="backup_not_found")
    return path


def _backup_meta(path: str) -> dict[str, Any]:
    st = os.stat(path)
    key = (st.st_size, st.st_mtime_ns)
    cached = _BACKUP_META_CACHE.get(path)
    if cached and cached[0] == key:
        return cached[1]
    meta: dict[str, Any] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            first = archive.next()
            if first is not None and first.name == BACKUP_META_FILE and first.isfile():
                handle = archive.extractfile(first)
                if handle is not None:
                    loaded = json.loads(handle.read(65536).decode("utf-8"))
                    if isinstance(loaded, dict):
                        meta = loaded
    except (tarfile.TarError, OSError, ValueError, EOFError):
        meta = {"damaged": True}
    if not meta.get("world"):
        match = re.fullmatch(r"(\d{8}T\d{6}Z)_(.+)\.tar\.gz", os.path.basename(path))  # v1 name
        if match and NAME_RE.match(match.group(2)):
            meta.setdefault("world", match.group(2))
            try:
                stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                meta.setdefault("created_at", stamp.isoformat().replace("+00:00", "Z"))
            except ValueError:
                pass
    meta.setdefault("created_at", _iso(st.st_mtime))
    _BACKUP_META_CACHE[path] = (key, meta)
    return meta


def list_backups() -> list[dict[str, Any]]:
    ensure_layout()
    items = []
    directory = paths()["backups"]
    for name in os.listdir(directory):
        if not BACKUP_NAME_RE.match(name):
            continue
        full = os.path.join(directory, name)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        meta = _backup_meta(full)
        st = os.stat(full)
        items.append(
            {
                "name": name,
                "world": meta.get("world"),
                "size_bytes": st.st_size,
                "created_at": meta.get("created_at"),
                "minecraft_version": meta.get("last_version"),
                "kind": meta.get("kind", "manual"),
                "damaged": bool(meta.get("damaged")),
                "size": st.st_size,
                "modified": int(st.st_mtime),
            }
        )
    items.sort(key=lambda item: (str(item["created_at"] or ""), item["name"]), reverse=True)
    return items


def _backup_name(world: str, kind: str) -> str:
    middle = "" if kind == "manual" else f"{kind}-"
    base = f"{world}-{middle}{_stamp()}"
    directory = paths()["backups"]
    name = f"{base}.tar.gz"
    counter = 1
    while os.path.exists(os.path.join(directory, name)) or os.path.exists(os.path.join(directory, f".{name}.tmp")):
        counter += 1
        name = f"{base}-{counter}.tar.gz"
    return name


def _backup_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if info.issym() or info.islnk() or not (info.isfile() or info.isdir()):
        return None
    if info.name.endswith("/session.lock"):
        return None
    return info


def create_backup(
    world: str | None = None,
    before_tar: Callable[[], Any] | None = None,
    after_tar: Callable[[], Any] | None = None,
    kind: str = "manual",
) -> dict[str, Any]:
    """Archive one world (plus railpowered-backup.json) as
    backups/<world>-<YYYYmmdd-HHMMSS>.tar.gz. Written to a hidden .tmp and
    renamed when complete; a failure leaves nothing behind. ``before_tar``
    (e.g. save-off + save-all flush) runs first and ``after_tar`` (save-on)
    always runs afterwards."""
    ensure_layout()
    cfg = load_config()
    world = validate_name(world or _active_setup(cfg)["level_name"], "world")
    source = world_path(world)
    if not os.path.isdir(source):
        raise InstallError(f"World '{world}' doesn't exist.", code="world_not_found")
    size = _dir_size(source, fresh=True)
    free = shutil.disk_usage(paths()["backups"]).free
    if free < int(size * 0.6) + 32 * MIB:
        raise InstallError(
            f"Not enough free space for a backup of '{world}' ({free // MIB} MB free, the world "
            f"is {size // MIB} MB). Delete old backups or worlds first.",
            code="disk_full",
        )
    meta = world_meta(world)
    with _BACKUP_LOCK:
        name = _backup_name(world, kind)
        tmp = os.path.join(paths()["backups"], f".{name}.tmp")
        open(tmp, "wb").close()  # reserve the name
    final = os.path.join(paths()["backups"], name)
    info = {
        "format": 1,
        "app": APP_NAME,
        "world": world,
        "last_version": meta.get("last_version"),
        "type": meta.get("last_type"),
        "kind": kind,
        "created_at": _iso(),
    }
    try:
        try:
            if before_tar:
                before_tar()
            with tarfile.open(tmp, "w:gz", compresslevel=6) as archive:
                payload = json.dumps(info, indent=2).encode("utf-8")
                header = tarfile.TarInfo(BACKUP_META_FILE)
                header.size = len(payload)
                header.mtime = int(_now())
                header.mode = 0o644
                archive.addfile(header, io.BytesIO(payload))
                archive.add(source, arcname=world, filter=_backup_filter)
            with open(tmp, "r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, final)
        finally:
            if after_tar:
                after_tar()
    except InstallError:
        _discard(tmp)
        raise
    except (OSError, tarfile.TarError) as exc:
        _discard(tmp)
        raise InstallError(
            f"The backup of '{world}' failed and was discarded.", code="backup_failed", detail=str(exc)
        ) from exc
    except BaseException:
        _discard(tmp)
        raise
    if kind != "manual":
        _prune_automatic_backups(world)
    st = os.stat(final)
    _say(f"Backed up world '{world}' as {name}.")
    return {
        "name": name,
        "world": world,
        "size_bytes": st.st_size,
        "created_at": info["created_at"],
        "minecraft_version": info["last_version"],
        "kind": kind,
        "size": st.st_size,
        "modified": int(st.st_mtime),
    }


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _auto_backup(world: str, kind: str) -> str | None:
    directory = world_path(world)
    if not os.path.isdir(directory) or not os.listdir(directory):
        return None
    return create_backup(world, kind=kind)["name"]


def _prune_automatic_backups(world: str) -> None:
    keep = _env_int("BACKUP_KEEP", 0, minimum=0)
    if keep <= 0:
        return
    automatic = [b for b in list_backups() if b["world"] == world and b["kind"] != "manual"]
    for item in automatic[keep:]:
        try:
            delete_backup(item["name"])
        except (InstallError, OSError):
            pass


def delete_backup(name: str) -> None:
    path = backup_file(name)
    os.unlink(path)
    _BACKUP_META_CACHE.pop(path, None)


def _backup_world_prefix(members: list[tarfile.TarInfo], meta: dict[str, Any]) -> tuple[str, str]:
    """(world name, member prefix) for v2 and v1 archives."""
    world = meta.get("world")
    if isinstance(world, str) and NAME_RE.match(world):
        if any(m.name == world or m.name.startswith(world + "/") for m in members):
            return world, world + "/"
    for member in members:
        parts = member.name.split("/")
        if len(parts) >= 2 and parts[0] == "worlds" and NAME_RE.match(parts[1]):
            return parts[1], f"worlds/{parts[1]}/"  # v1 layout
    tops = {m.name.split("/")[0] for m in members if m.name != BACKUP_META_FILE}
    if len(tops) == 1:
        top = tops.pop()
        if NAME_RE.match(top):
            return top, top + "/"
    raise InstallError("This backup doesn't contain a world.", code="invalid_backup")


def restore_backup(name: str, target: str | None = None) -> dict[str, Any]:
    """Replace a world with the one in the backup (or restore into ``target``).
    The current world is backed up first as <world>-pre-restore-<stamp>.
    Setups, server.properties and player lists are never touched.
    The caller must make sure the target world isn't running."""
    with _RUNTIME_LOCK:
        source = backup_file(name)
        worlds = paths()["worlds"]
        staging = tempfile.mkdtemp(prefix=".restore-", dir=worlds)
        try:
            try:
                archive = tarfile.open(source, "r:gz")
            except (tarfile.TarError, OSError, EOFError) as exc:
                raise InstallError("That backup file is damaged.", code="invalid_backup", detail=str(exc)) from exc
            with archive:
                try:
                    members = archive.getmembers()
                except (tarfile.TarError, OSError, EOFError) as exc:
                    raise InstallError("That backup file is damaged.", code="invalid_backup", detail=str(exc)) from exc
                meta = _backup_meta(source)
                world, prefix = _backup_world_prefix(members, meta)
                dest_name = validate_name(target or world, "world")
                limit = _extract_limit(worlds)
                total = 0
                restored = os.path.join(staging, "world")
                os.makedirs(restored)
                for member in members:
                    if not member.name.startswith(prefix):
                        continue
                    rel = member.name[len(prefix):]
                    parts = [p for p in rel.split("/") if p not in ("", ".")]
                    if not parts:
                        continue
                    if member.name.startswith("/") or ".." in parts or ":" in parts[0]:
                        raise InstallError("This backup contains unsafe paths.", code="invalid_backup")
                    if not (member.isfile() or member.isdir()):
                        continue
                    total += member.size
                    if total > limit:
                        raise InstallError("Not enough free space to restore this backup.", code="disk_full")
                    clone = copy.copy(member)
                    clone.name = "world/" + "/".join(parts)
                    try:
                        if hasattr(tarfile, "data_filter"):
                            archive.extract(clone, staging, filter="data")
                        else:
                            archive.extract(clone, staging)
                    except (tarfile.TarError, OSError, EOFError) as exc:
                        raise InstallError("That backup file is damaged.", code="invalid_backup", detail=str(exc)) from exc
            dest = world_path(dest_name)
            pre = None
            if os.path.isdir(dest) and os.listdir(dest):
                pre = create_backup(dest_name, kind="pre-restore")["name"]
            old = None
            if os.path.lexists(dest):
                old = os.path.join(staging, "old")
                os.replace(dest, old)
            try:
                os.replace(restored, dest)
            except OSError:
                if old:
                    os.replace(old, dest)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        _SIZE_CACHE.pop(dest, None)
        _say(f"Restored world '{dest_name}' from {name}.")
        return {
            "world": dest_name,
            "name": name,
            "pre_restore_backup": pre,
            "last_version": world_meta(dest_name).get("last_version"),
        }


# ---------------------------------------------------------------------------
# Players: ops, whitelist, UUIDs
# ---------------------------------------------------------------------------

_LIST_FILES = {"ops": ("ops.json", "ops.txt"), "whitelist": ("whitelist.json", "white-list.txt")}


def offline_uuid(name: str) -> str:
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    hexed = bytes(digest).hex()
    return f"{hexed[0:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:32]}"


def dashed_uuid(raw: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", str(raw or ""))
    if len(compact) != 32:
        raise InstallError("Invalid UUID", code="invalid_request")
    compact = compact.lower()
    return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"


def _uuid_cache() -> dict[str, Any]:
    data = _read_json(paths()["uuid_cache"])
    return data if isinstance(data, dict) else {}


def _cached_uuid(name: str) -> tuple[str, str] | None:
    entry = _uuid_cache().get(name.casefold())
    if not isinstance(entry, dict):
        return None
    try:
        if _now() - float(entry.get("at") or 0) > UUID_CACHE_TTL:
            return None
        return str(entry["name"]), dashed_uuid(entry["uuid"])
    except (KeyError, TypeError, ValueError, InstallError):
        return None


def _store_uuid(name: str, uuid: str) -> None:
    with _CONFIG_LOCK:
        cache = _uuid_cache()
        cache[name.casefold()] = {"name": name, "uuid": uuid, "at": int(_now())}
        try:
            _atomic_write_json(paths()["uuid_cache"], cache)
        except OSError:
            pass


def lookup_player(name: str, network: bool = True) -> tuple[str, str]:
    """(exact name, online UUID) from the cache or Mojang. Raises
    player_not_found for unknown names and mojang_unreachable otherwise."""
    cached = _cached_uuid(name)
    if cached:
        return cached
    if not network:
        raise InstallError(f"{name}'s account isn't cached yet.", code="mojang_unreachable")
    last: InstallError | None = None
    for template in MOJANG_PROFILE_URLS:
        url = template.format(name=quote(name))
        try:
            status, body = _http_get(url, timeout=10, max_bytes=65536)
        except InstallError as exc:
            last = exc
            continue
        if status in (204, 404) or (status == 200 and not body.strip()):
            raise InstallError(f"No Minecraft account is named {name}.", code="player_not_found")
        if status == 200:
            try:
                data = json.loads(body.decode("utf-8"))
                exact = str(data.get("name") or name)
                uuid = dashed_uuid(str(data.get("id") or ""))
            except (ValueError, AttributeError, InstallError, UnicodeDecodeError) as exc:
                last = InstallError("bad reply", code="network", detail=str(exc))
                continue
            _store_uuid(exact, uuid)
            return exact, uuid
        last = InstallError(f"HTTP {status}", code="network", http_status=status)
    detail = last.detail if last else None
    raise InstallError(
        f"Couldn't check {name} with Mojang right now. Try again in a minute.",
        code="mojang_unreachable",
        detail=detail or (str(last) if last else None),
    )


def player_uuid(name: str, online: bool = False) -> str:
    """Online UUID (cache/Mojang) when ``online``; never silently offline."""
    if online:
        return lookup_player(name)[1]
    return offline_uuid(name)


def _kind(kind: str) -> str:
    if kind not in _LIST_FILES:
        raise InstallError("Unknown player list (use ops or whitelist).", code="invalid_request")
    return kind


def _normalize_entry(kind: str, raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not PLAYER_NAME_RE.match(name):
        return None
    entry = dict(raw)
    entry["name"] = name
    try:
        entry["uuid"] = dashed_uuid(str(raw.get("uuid") or ""))
    except InstallError:
        cached = _cached_uuid(name)
        entry["uuid"] = cached[1] if cached else offline_uuid(name)
    if kind == "ops":
        level = raw.get("level")
        entry["level"] = level if isinstance(level, int) and 0 <= level <= 4 else 4
        entry["bypassesPlayerLimit"] = bool(raw.get("bypassesPlayerLimit", False))
    return entry


def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for entry in entries:
        key = entry["name"].casefold()
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def _read_entries(kind: str, path: str) -> list[dict[str, Any]] | None:
    """Entries from a json list file; None if missing or unparsable."""
    data = _read_json(path)
    if not isinstance(data, list):
        return None
    return _dedupe([e for e in (_normalize_entry(kind, item) for item in data) if e])


def _read_txt_names(path: str) -> list[str] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    names = []
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#") and PLAYER_NAME_RE.match(text):
            names.append(text)
    return names


def _canonical_path(kind: str) -> str:
    return os.path.join(DATA_DIR, _LIST_FILES[kind][0])


def _load_list(kind: str) -> list[dict[str, Any]]:
    entries = _read_entries(kind, _canonical_path(kind))
    if entries:
        return entries
    names = _read_txt_names(os.path.join(DATA_DIR, _LIST_FILES[kind][1])) or []
    return _dedupe([e for e in (_normalize_entry(kind, n) for n in names) if e])


def _list_key(entries: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (e["name"].casefold(), e["uuid"], e.get("level"), e.get("bypassesPlayerLimit"))
        for e in entries
    ]


def _save_list(kind: str, entries: list[dict[str, Any]]) -> None:
    _atomic_write_json(_canonical_path(kind), entries)


def _effective_online_mode(version: str) -> bool:
    if is_legacy_protocol(version):
        return False
    return (read_properties().get("online-mode") or "true").strip().lower() != "false"


def _render_access_lists(version: str, run_dir: str, network: bool = False) -> list[str]:
    """Write the canonical lists in the format ``version`` reads, into
    ``run_dir``, fixing UUIDs for the current online mode. Returns warnings."""
    warnings = []
    online = _effective_online_mode(version)
    json_lists = uses_json_lists(version)
    same_dir = os.path.realpath(run_dir) == os.path.realpath(DATA_DIR)
    with _CONFIG_LOCK:
        for kind in _LIST_FILES:
            entries = _load_list(kind)
            converted = []
            for entry in entries:
                entry = dict(entry)
                if online and entry["uuid"] == offline_uuid(entry["name"]):
                    try:
                        exact, uuid = lookup_player(entry["name"], network=network)
                        entry["uuid"] = uuid
                        entry["name"] = exact
                    except InstallError as exc:
                        warnings.append(f"{entry['name']}: {exc}")
                elif not online:
                    entry["uuid"] = offline_uuid(entry["name"])
                converted.append(entry)
            if _list_key(converted) != _list_key(_read_entries(kind, _canonical_path(kind)) or []):
                _save_list(kind, converted)
            json_name, txt_name = _LIST_FILES[kind]
            if json_lists:
                if not same_dir:
                    _atomic_write_json(os.path.join(run_dir, json_name), converted)
            else:
                _atomic_write(os.path.join(run_dir, txt_name), "".join(e["name"] + "\n" for e in converted))
    for warning in warnings:
        _say(f"Player list: {warning}")
    return warnings


def _newer(path: str, than: str) -> bool:
    try:
        mine = os.stat(path).st_mtime_ns
    except OSError:
        return False
    try:
        other = os.stat(than).st_mtime_ns
    except OSError:
        return True
    return mine > other


def _sync_back_quietly(cfg: dict[str, Any], setup: dict[str, Any] | None = None) -> None:
    try:
        sync_access_lists_from_runtime(cfg, setup)
    except (InstallError, OSError) as exc:
        _say(f"Couldn't read in-game player list changes: {exc}")


def sync_access_lists_from_runtime(
    cfg: dict[str, Any] | None = None, setup: dict[str, Any] | None = None
) -> dict[str, bool]:
    """Copy in-game /op, /whitelist and `whitelist on|off` changes from the
    server's folder back into the canonical files (newer file wins). Cheap;
    call it when the server stops and before reading player lists."""
    cfg = cfg if cfg is not None else load_config()
    setup = setup or _active_setup(cfg)
    changed = {"ops": False, "whitelist": False, "properties": False}
    try:
        run_dir = runtime_dir(setup)
    except InstallError:
        return changed
    if not os.path.isdir(run_dir):
        return changed
    version = _display_version(setup) or FALLBACK_RELEASE
    same_dir = os.path.realpath(run_dir) == os.path.realpath(DATA_DIR)
    with _CONFIG_LOCK:
        for kind in _LIST_FILES:
            json_name, txt_name = _LIST_FILES[kind]
            canonical = _canonical_path(kind)
            current = _load_list(kind)
            if uses_json_lists(version):
                if same_dir:
                    continue
                live_path = os.path.join(run_dir, json_name)
                if not _newer(live_path, canonical):
                    continue
                live = _read_entries(kind, live_path)
                if live is None or _list_key(live) == _list_key(current):
                    continue
                _save_list(kind, live)
                changed[kind] = True
            else:
                live_path = os.path.join(run_dir, txt_name)
                if not _newer(live_path, canonical):
                    continue
                names = _read_txt_names(live_path)
                if names is None:
                    continue
                if [n.casefold() for n in names] == [e["name"].casefold() for e in current]:
                    continue
                by_name = {e["name"].casefold(): e for e in current}
                rebuilt = [by_name.get(n.casefold()) or _normalize_entry(kind, n) for n in names]
                _save_list(kind, _dedupe([e for e in rebuilt if e]))
                changed[kind] = True
        if not same_dir:
            live_props = os.path.join(run_dir, "server.properties")
            if _newer(live_props, paths()["properties"]):
                live = read_properties(live_props)
                mine = read_properties()
                updates = {
                    key: live[key]
                    for key in ("white-list", "enforce-whitelist")
                    if key in live and live.get(key) != mine.get(key)
                }
                if updates:
                    write_properties(updates)
                    changed["properties"] = True
    return changed


def players() -> dict[str, Any]:
    """{"ops", "whitelist", "whitelist_enabled", "online_mode"} (canonical,
    after pulling in in-game changes). The panel adds "online"."""
    cfg = load_config()
    _sync_back_quietly(cfg)
    version = _active_version(cfg)
    props = read_properties()
    return {
        "ops": [{"name": e["name"], "uuid": e["uuid"], "level": e.get("level", 4)} for e in _load_list("ops")],
        "whitelist": [{"name": e["name"], "uuid": e["uuid"]} for e in _load_list("whitelist")],
        "whitelist_enabled": (props.get("white-list") or "false").strip().lower() == "true",
        "online_mode": _effective_online_mode(version),
    }


def add_player(kind: str, name: str) -> dict[str, Any]:
    """Add a player to ops or the whitelist. In online mode the account must
    exist at Mojang; nothing is written when it can't be checked."""
    kind = _kind(kind)
    name = str(name or "").strip()
    if not PLAYER_NAME_RE.match(name):
        raise InstallError(
            "Player names are 1 to 16 letters, numbers or underscores.", code="invalid_player_name"
        )
    cfg = load_config()
    _sync_back_quietly(cfg)
    version = _active_version(cfg)
    online = _effective_online_mode(version)
    with _CONFIG_LOCK:
        entries = _load_list(kind)
        existing = next((e for e in entries if e["name"].casefold() == name.casefold()), None)
        if existing:
            return {"name": existing["name"], "uuid": existing["uuid"]}
    if online:
        exact, uuid = lookup_player(name)
    else:
        exact, uuid = name, offline_uuid(name)
    with _CONFIG_LOCK:
        entries = _load_list(kind)
        if not any(e["name"].casefold() == exact.casefold() for e in entries):
            entry = _normalize_entry(kind, {"uuid": uuid, "name": exact})
            entries.append(entry)
            _save_list(kind, entries)
        _render_to_active(cfg, version)
    return {"name": exact, "uuid": uuid}


def remove_player(kind: str, name: str) -> bool:
    kind = _kind(kind)
    name = str(name or "").strip()
    cfg = load_config()
    _sync_back_quietly(cfg)
    version = _active_version(cfg)
    with _CONFIG_LOCK:
        entries = _load_list(kind)
        kept = [e for e in entries if e["name"].casefold() != name.casefold()]
        if len(kept) == len(entries):
            return False
        _save_list(kind, kept)
        _render_to_active(cfg, version)
    return True


def _render_to_active(cfg: dict[str, Any], version: str) -> None:
    try:
        run_dir = runtime_dir(_active_setup(cfg))
    except InstallError:
        return
    if os.path.isdir(run_dir):
        _render_access_lists(version, run_dir, network=False)


def set_whitelist_enabled(enabled: bool) -> bool:
    """Turn whitelist enforcement on/off (white-list + enforce-whitelist)."""
    if not isinstance(enabled, bool):
        raise InstallError("Whitelist must be on or off.", code="invalid_setting")
    value = "true" if enabled else "false"
    updates = {"white-list": value, "enforce-whitelist": value}
    with _CONFIG_LOCK:
        cfg = load_config()
        write_properties(updates)
        run = _active_run_dir(cfg)
        if run and os.path.isfile(os.path.join(run, "server.properties")):
            write_properties(updates, os.path.join(run, "server.properties"))
    return enabled


# ---------------------------------------------------------------------------
# Cache reset (tests and long-running processes)
# ---------------------------------------------------------------------------


def _reset_caches() -> None:
    global _config_cache, _prepared_digest
    with _NET_LOCK:
        _JSON_CACHE.clear()
        _FAILED_AT.clear()
    with _CONFIG_LOCK:
        _config_cache = None
        _config_warnings.clear()
    _SIZE_CACHE.clear()
    _BACKUP_META_CACHE.clear()
    _PATH_JAVA.clear()
    _prepared_digest = None


def clear_latest_cache() -> None:
    """Forget cached version lists and network failures."""
    _reset_caches()


# ---------------------------------------------------------------------------
# TODO(integration): v1 panel compatibility. mc_host still calls these names;
# delete this block once it uses the setup API above.
# ---------------------------------------------------------------------------


def list_profiles(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return list_setups(cfg)


def get_profile(cfg: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    return next((s for s in cfg.get("profiles") or [] if s.get("id") == profile_id), None)


def upsert_profile(cfg: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Make the top-level fields of a v1-style cfg its active setup (in place)."""
    fields = {k: cfg.get(k) for k in ("type", "minecraft_version", "level_name", "instance", "modpack")}
    candidate = _sanitize_setup({**fields, "id": "pending", "name": name})
    if candidate is None:
        raise InstallError("That setup isn't valid.", code="invalid_request")
    cfg.setdefault("profiles", [])
    match = next((s for s in cfg["profiles"] if _fingerprint(s) == _fingerprint(candidate)), None)
    if match is None:
        candidate["id"] = _new_setup_id(cfg)
        cfg["profiles"].append(candidate)
        match = candidate
    cfg["active_profile"] = match["id"]
    return cfg


def configured_version(cfg: dict[str, Any] | None = None) -> str:
    return _active_version(_normalize_config(copy.deepcopy(cfg)) if cfg else None)


def uses_legacy_files(version: str) -> bool:
    return not uses_json_lists(version)


def properties_for(cfg: dict[str, Any]) -> dict[str, str]:
    values = read_properties()
    version = configured_version(cfg)
    values["online-mode"] = "true" if _effective_online_mode(version) else "false"
    return values


def apply_server(cfg: dict[str, Any]) -> RuntimeSpec:
    fields = {k: cfg.get(k) for k in ("type", "minecraft_version", "level_name", "instance", "modpack")}
    current = _active_setup(load_config())
    candidate = _sanitize_setup({**fields, "id": "pending"})
    if candidate and _fingerprint(candidate) == _fingerprint(current):
        return prepare_runtime()
    return create_setup(fields)


def activate_profile(profile_id: str) -> dict[str, Any]:
    activate_setup(profile_id)
    return load_config()


def rename_profile(profile_id: str, name: str) -> dict[str, Any]:
    update_setup(profile_id, name=name)
    return load_config()


def delete_profile(profile_id: str) -> dict[str, Any]:
    delete_setup(profile_id)
    return load_config()


def select_world(name: str) -> dict[str, Any]:
    cfg = load_config()
    update_setup(cfg["active_profile"], level_name=name)
    return load_config()


def backup_path(name: str) -> str:
    return backup_file(name)


def list_file_names(kind: str) -> tuple[str, list[str]]:
    data = players()
    return _canonical_path(_kind(kind)), [e["name"] for e in data[kind]]


def update_name_list(kind: str, name: str, add: bool) -> list[str]:
    if add:
        add_player(kind, name)
    else:
        remove_player(kind, name)
    return [e["name"] for e in _load_list(_kind(kind))]


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(list_setups(), indent=2))
    sys.exit(0)
