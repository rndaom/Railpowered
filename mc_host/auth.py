"""Dashboard authentication: signed session cookies, logout, login rate limits.

Cookie `rp_session` = "v1.<issued_unix>.<nonce>.<hmac>". The HMAC key is
sha256(volume secret + ADMIN_KEY), so changing ADMIN_KEY (or deleting
.session-secret) signs everyone out. Logout revokes the nonce in memory.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import secrets
import threading
import time
from typing import Callable

import installer

from . import config

COOKIE_NAME = "rp_session"
SESSION_MAX_AGE = 30 * 24 * 3600
CLOCK_SKEW = 300
FAILURE_WINDOW = 600
MAX_FAILURES_PER_IP = 10
MAX_FAILURES_GLOBAL = 60


def client_ip(peer: str, forwarded_for: str | None) -> str:
    """Last X-Forwarded-For hop (appended by Railway's edge), else the peer."""
    if forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1][:64]
    return peer


class RateLimiter:
    def __init__(
        self,
        per_ip: int = MAX_FAILURES_PER_IP,
        global_limit: int = MAX_FAILURES_GLOBAL,
        window: float = FAILURE_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()
        self._by_ip: dict[str, collections.deque[float]] = {}
        self._all: collections.deque[float] = collections.deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for ip in list(self._by_ip):
            entries = self._by_ip[ip]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if not entries:
                del self._by_ip[ip]

    def limited(self, ip: str) -> bool:
        with self._lock:
            now = self.clock()
            self._prune(now)
            if len(self._all) >= self.global_limit:
                return True
            return len(self._by_ip.get(ip, ())) >= self.per_ip

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._all.append(now)
            self._by_ip.setdefault(ip, collections.deque()).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._by_ip.pop(ip, None)


class SessionManager:
    def __init__(
        self,
        key_source: Callable[[], str] | None = None,
        secret_source: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key_source = key_source or config.get_admin_key
        self._secret_source = secret_source or (
            lambda: config.load_or_create_session_secret(installer.DATA_DIR)
        )
        self.clock = clock
        self._lock = threading.Lock()
        self._revoked: dict[str, float] = {}
        self._secret_cache: dict[str, str] = {}

    def _signing_key(self) -> bytes:
        admin_key = self._key_source()
        data_dir = installer.DATA_DIR
        with self._lock:
            secret = self._secret_cache.get(data_dir)
        if secret is None:
            secret = self._secret_source()
            with self._lock:
                self._secret_cache[data_dir] = secret
        return hashlib.sha256((secret + admin_key).encode("utf-8")).digest()

    def check_key(self, submitted: str) -> bool:
        expected = self._key_source()
        if not expected:
            return False
        return hmac.compare_digest(
            submitted.strip().encode("utf-8"), expected.encode("utf-8")
        )

    def _sign(self, body: str) -> str:
        return hmac.new(self._signing_key(), body.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        issued = int(self.clock())
        nonce = secrets.token_urlsafe(18)
        body = f"v1.{issued}.{nonce}"
        return f"{body}.{self._sign(body)}"

    def _parse(self, token: str) -> tuple[int, str] | None:
        if not token or len(token) > 256:
            return None
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return None
        _version, issued_raw, nonce, signature = parts
        if not issued_raw.isdigit() or not nonce:
            return None
        try:
            expected = self._sign(f"v1.{issued_raw}.{nonce}")
        except UnicodeEncodeError:
            return None
        if not hmac.compare_digest(signature.encode("ascii", "replace"), expected.encode("ascii")):
            return None
        issued = int(issued_raw)
        now = self.clock()
        if issued > now + CLOCK_SKEW or now - issued > SESSION_MAX_AGE:
            return None
        return issued, nonce

    def validate(self, token: str | None) -> bool:
        parsed = self._parse(token or "")
        if parsed is None:
            return False
        issued, nonce = parsed
        with self._lock:
            return nonce not in self._revoked

    def revoke(self, token: str | None) -> None:
        parsed = self._parse(token or "")
        if parsed is None:
            return
        issued, nonce = parsed
        now = self.clock()
        with self._lock:
            self._revoked[nonce] = issued + SESSION_MAX_AGE
            for old, expires in list(self._revoked.items()):
                if expires < now:
                    del self._revoked[old]

    def cookie_header(self, token: str, secure: bool) -> str:
        flags = f"Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
        if secure:
            flags += "; Secure"
        return f"{COOKIE_NAME}={token}; {flags}"

    @staticmethod
    def clear_cookie_header(secure: bool) -> str:
        flags = "Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        if secure:
            flags += "; Secure"
        return f"{COOKIE_NAME}=; {flags}"


def cookie_value(cookie_header: str | None, name: str = COOKIE_NAME) -> str | None:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


sessions = SessionManager()
limiter = RateLimiter()


def ensure_session_secret() -> None:
    """Create .session-secret at boot so the first login doesn't race to create it."""
    config.load_or_create_session_secret(installer.DATA_DIR)
