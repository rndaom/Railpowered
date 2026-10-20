"""Runtime settings read from the environment, plus the dashboard admin key.

Importing this module has no side effects: nothing is written to disk and
nothing is bound. Bad environment values never crash the process; they fall
back to a safe default and are recorded in CONFIG_WARNINGS, which main() logs
at boot.
"""

from __future__ import annotations

import base64
import os
import re
import threading
from collections.abc import Mapping

import installer

APP_VERSION = "2.0.0"
ADMIN_KEY_FILE = ".admin-key"
SESSION_SECRET_FILE = ".session-secret"
MIN_RECOMMENDED_KEY_LENGTH = 12

CONFIG_WARNINGS: list[str] = []

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}
_MEMORY_RE = re.compile(r"^(\d+)([KkMmGg]?)$")


def _warn(message: str) -> None:
    if message not in CONFIG_WARNINGS:
        CONFIG_WARNINGS.append(message)


def _unevaluated_template(value: str) -> bool:
    return value.startswith("${{") and "}}" in value


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Read an integer variable; fall back to `default` with a warning when invalid."""
    environ = os.environ if env is None else env
    raw = str(environ.get(name, "")).strip()
    if not raw or _unevaluated_template(raw):
        return default
    try:
        value = int(raw)
    except ValueError:
        _warn(f'{name}="{raw}" is not a whole number; using {default}.')
        return default
    if minimum is not None and value < minimum:
        _warn(f"{name}={value} is below the minimum {minimum}; using {minimum}.")
        value = minimum
    if maximum is not None and value > maximum:
        _warn(f"{name}={value} is above the maximum {maximum}; using {maximum}.")
        value = maximum
    return value


def env_bool(name: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    """Read a yes/no variable (true/1/yes/on or false/0/no/off)."""
    environ = os.environ if env is None else env
    raw = str(environ.get(name, "")).strip().lower()
    if not raw or _unevaluated_template(raw):
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    _warn(f'{name}="{raw}" is not true or false; using {str(default).lower()}.')
    return default


def resolve_public_address(env: Mapping[str, str] | None = None) -> str:
    """Join address: explicit override, else Railway's TCP proxy host:port."""
    environ = os.environ if env is None else env
    override = str(environ.get("MC_PUBLIC_ADDRESS", "")).strip()
    if override and not _unevaluated_template(override):
        return override
    domain = str(environ.get("RAILWAY_TCP_PROXY_DOMAIN", "")).strip()
    port = str(environ.get("RAILWAY_TCP_PROXY_PORT", "")).strip()
    if domain and port and not _unevaluated_template(domain):
        return f"{domain}:{port}"
    return ""


def _write_secret_file(path: str, value: str) -> bool:
    """Create `path` with mode 0600. Returns False if it already exists or fails."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    return True


def _read_secret_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def load_or_create_admin_key(
    data_dir: str, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """Return (key, source); source is "env", "volume" or "generated".

    "generated" is returned only by the call that actually created the file,
    so the caller can show the key exactly once.
    """
    environ = os.environ if env is None else env
    env_key = str(environ.get("ADMIN_KEY", "")).strip()
    if env_key and not _unevaluated_template(env_key):
        return env_key, "env"
    path = os.path.join(data_dir, ADMIN_KEY_FILE)
    stored = _read_secret_file(path)
    if stored:
        return stored, "volume"
    key = base64.urlsafe_b64encode(os.urandom(18)).decode()
    if _write_secret_file(path, key):
        return key, "generated"
    # Another process won the race, or the volume is read-only.
    stored = _read_secret_file(path)
    if stored:
        return stored, "volume"
    return key, "generated"


def resolve_admin_key(
    data_dir: str, env: Mapping[str, str] | None = None
) -> tuple[str, bool]:
    """Backward-compatible wrapper: (key, came_from_env)."""
    key, source = load_or_create_admin_key(data_dir, env)
    return key, source == "env"


_admin_lock = threading.Lock()
_admin_cache: dict[str, tuple[str, str]] = {}


def admin_key_info() -> tuple[str, str]:
    """(key, source) for the current data dir, resolved lazily and cached."""
    data_dir = installer.DATA_DIR
    with _admin_lock:
        env_key = str(os.environ.get("ADMIN_KEY", "")).strip()
        cache_key = f"{data_dir}\0{env_key}"
        cached = _admin_cache.get(cache_key)
        if cached is None:
            cached = load_or_create_admin_key(data_dir)
            _admin_cache[cache_key] = cached
            # Later lookups must not report "generated" again.
            if cached[1] == "generated":
                _admin_cache[cache_key] = (cached[0], "volume")
        return cached


def get_admin_key() -> str:
    return admin_key_info()[0]


def admin_key_path() -> str:
    return os.path.join(installer.DATA_DIR, ADMIN_KEY_FILE)


def load_or_create_session_secret(data_dir: str | None = None) -> str:
    """Random per-volume secret mixed into session signatures."""
    path = os.path.join(data_dir or installer.DATA_DIR, SESSION_SECRET_FILE)
    secret = _read_secret_file(path)
    if secret:
        return secret
    secret = os.urandom(32).hex()
    if not _write_secret_file(path, secret):
        stored = _read_secret_file(path)
        if stored:
            return stored
    return secret


def resolve_web_port(
    env: Mapping[str, str] | None = None, minecraft_port: int = 25565
) -> int:
    """Dashboard port. Ignore Railway PORT when it collides with Minecraft."""
    environ = os.environ if env is None else env
    for key in ("WEB_PORT", "PORT"):
        raw = str(environ.get(key, "")).strip()
        if not raw.isdigit():
            continue
        port = int(raw)
        if 1 <= port <= 65535 and port != minecraft_port:
            return port
    return 8080


_MIB = 1024 * 1024
_MEMORY_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",  # cgroup v2
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
)


def container_memory_limit() -> int | None:
    """Return the container limit in bytes, ignoring unlimited cgroup sentinels."""
    for path in _MEMORY_LIMIT_FILES:
        try:
            with open(path, encoding="ascii") as handle:
                raw = handle.read().strip()
            limit = int(raw)
        except (OSError, ValueError):
            continue
        if 256 * _MIB <= limit < 1024 * 1024 * _MIB:
            return limit
    return None


def memory_bytes(value: str) -> int | None:
    """Parse a JVM size such as 512M, 2G or 1048576 into bytes."""
    match = _MEMORY_RE.match(str(value).strip())
    if not match:
        return None
    number = int(match.group(1))
    unit = match.group(2).upper()
    factor = {"": 1, "K": 1024, "M": _MIB, "G": 1024 * _MIB}[unit]
    return number * factor


def _auto_heap(limit_bytes: int | None) -> str:
    if limit_bytes is None:
        return "2G"
    # A 1 GiB trial container gets about 700 MiB, matching Railway's guidance.
    # On smaller containers, cap the heap to leave native/JVM headroom.
    limit_mib = limit_bytes // _MIB
    heap_mib = min(2048, max(256, min(int(limit_mib * 0.68), limit_mib - 320)))
    return f"{heap_mib}M"


def resolve_max_memory(
    env: Mapping[str, str] | None = None, limit_bytes: int | None = None
) -> str:
    """Leave native-memory headroom while avoiding an oversized idle heap."""
    environ = os.environ if env is None else env
    configured = str(environ.get("MC_MAX_MEMORY", "auto")).strip()
    if configured and configured.lower() != "auto":
        size = memory_bytes(configured)
        if size is not None and size >= 128 * _MIB:
            return configured.upper()
        _warn(
            f'MC_MAX_MEMORY="{configured}" is not a size like 1536M or 2G '
            "(at least 128M); using the automatic size."
        )
    if limit_bytes is None:
        limit_bytes = container_memory_limit()
    return _auto_heap(limit_bytes)


def resolve_min_memory(max_memory: str, env: Mapping[str, str] | None = None) -> str:
    """-Xms value; never larger than -Xmx (the JVM refuses to start otherwise)."""
    environ = os.environ if env is None else env
    configured = str(environ.get("MC_MIN_MEMORY", "256M")).strip() or "256M"
    size = memory_bytes(configured)
    if size is None:
        _warn(f'MC_MIN_MEMORY="{configured}" is not a size like 256M; using 256M.')
        configured, size = "256M", 256 * _MIB
    max_size = memory_bytes(max_memory) or size
    if size > max_size:
        _warn(
            f"MC_MIN_MEMORY={configured} is larger than the maximum heap "
            f"{max_memory}; using {max_memory}."
        )
        return max_memory
    return configured.upper()


MC_PORT = 25565
WEB_PORT = resolve_web_port()
MAX_MEMORY = resolve_max_memory()
MIN_MEMORY = resolve_min_memory(MAX_MEMORY)
# Seconds without players before an online server is stopped. 0 = never sleep.
IDLE_TIMEOUT = env_int("IDLE_TIMEOUT", 600, minimum=0, maximum=7 * 24 * 3600)
if 0 < IDLE_TIMEOUT < 60:
    _warn(f"IDLE_TIMEOUT={IDLE_TIMEOUT} is very short; using 60 seconds.")
    IDLE_TIMEOUT = 60
AUTO_START = env_bool("AUTO_START", False)
# Grace period for Minecraft to save and exit. Keep it a few seconds below
# Railway's drainingSeconds so the world is saved before SIGKILL.
STOP_TIMEOUT = env_int("STOP_TIMEOUT", 25, minimum=5, maximum=600)
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 1024, minimum=1, maximum=16 * 1024)
MC_PUBLIC_ADDRESS = resolve_public_address()


def resolve_web_dir(env: Mapping[str, str] | None = None) -> str | None:
    """Directory holding index.html/login.html/app.js. None when none exists."""
    environ = os.environ if env is None else env
    candidates = [
        str(environ.get("WEB_DIR", "")).strip() or "/server/web",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


WEB_DIR = resolve_web_dir()
