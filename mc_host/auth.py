"""Dashboard authentication: signed session cookies, logout, login rate limits,
and optional time-based one-time codes (TOTP_SECRET, RFC 6238).

Cookie `__Host-rp_session` (over https; `rp_session` on a local http
address) = "v1.<issued_unix>.<nonce>.<hmac>". The HMAC key is
sha256(volume secret + ADMIN_KEY), so changing ADMIN_KEY (or deleting
.session-secret) signs everyone out. Logout revokes the nonce, and "sign out
everywhere" rejects every token issued before it; both are kept on the
volume (.session-revoked.json) so a restart doesn't bring sessions back.
"""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import struct
import threading
import time
from typing import Any, Callable

import installer

from . import config

COOKIE_NAME = "rp_session"
SECURE_COOKIE_NAME = "__Host-rp_session"  # Secure, Path=/, no Domain: no cookie tossing
SESSION_MAX_AGE = 30 * 24 * 3600
CLOCK_SKEW = 300
FAILURE_WINDOW = 600
MAX_FAILURES_PER_IP = 10
MAX_FAILURES_GLOBAL = 60
# Past the global ceiling, key checks go through a slow lane: one check every
# SLOW_LANE_DELAY seconds across all clients, with at most SLOW_LANE_QUEUE
# callers waiting (each for at most SLOW_LANE_WAIT). Anyone past that gets an
# immediate "too many attempts", so a flood can't tie up the dashboard's
# connections, and a wait that times out counts as a failed attempt.
SLOW_LANE_DELAY = 3.0
SLOW_LANE_WAIT = 10.0
SLOW_LANE_QUEUE = 2
REVOKED_FILE = ".session-revoked.json"
MAX_REVOKED = 500
TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_DRIFT = 1  # steps either side of now, for clocks that are a little off
TOTP_MIN_SECRET_BYTES = 10  # 16 base32 characters, what authenticator apps make


def client_ip(peer: str, forwarded_for: str | None) -> str:
    """Last X-Forwarded-For hop (appended by Railway's edge), else the peer."""
    if forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1][:64]
    return peer


def is_local_host(host_header: str | None) -> bool:
    """True for a dashboard reached on this machine or a private network
    address over plain http (where a Secure cookie couldn't be stored)."""
    host = (host_header or "").strip().lower()
    if not host:
        return True
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def loggable_ip(value: str) -> str:
    """``value`` when it parses as an IP address, else a placeholder, so a
    forged X-Forwarded-For can't write arbitrary text into the log."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return "an unrecognised address"


class RateLimiter:
    """Login attempts: a hard per-IP limit, and a global ceiling that turns
    into a short, bounded slow lane.

    begin() reserves an attempt atomically (counted as a failure unless
    succeed() or cancel() follows), so concurrent guesses can't all slip
    past the check before any of them is recorded."""

    def __init__(
        self,
        per_ip: int = MAX_FAILURES_PER_IP,
        global_limit: int = MAX_FAILURES_GLOBAL,
        window: float = FAILURE_WINDOW,
        clock: Callable[[], float] = time.monotonic,
        slow_delay: float = SLOW_LANE_DELAY,
        slow_wait: float = SLOW_LANE_WAIT,
        slow_queue: int = SLOW_LANE_QUEUE,
    ) -> None:
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window = window
        self.clock = clock
        self.slow_delay = slow_delay
        self.slow_wait = slow_wait
        self.slow_queue = slow_queue
        self._waiting = 0
        self._lock = threading.Lock()
        self._slow = threading.Lock()
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
        """True when ``ip`` used up its attempts (the global ceiling only slows)."""
        with self._lock:
            self._prune(self.clock())
            return len(self._by_ip.get(ip, ())) >= self.per_ip

    def begin(self, ip: str) -> tuple[str, float | None]:
        """Reserve one attempt: ("limited", None), or ("ok" | "slow", ticket)."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            if len(self._by_ip.get(ip, ())) >= self.per_ip:
                return "limited", None
            verdict = "slow" if len(self._all) >= self.global_limit else "ok"
            self._all.append(now)
            self._by_ip.setdefault(ip, collections.deque()).append(now)
            return verdict, now

    def _drop(self, ip: str, ticket: float | None) -> None:
        if ticket is None:
            return
        for entries in (self._all, self._by_ip.get(ip)):
            if entries is None:
                continue
            try:
                entries.remove(ticket)
            except ValueError:
                pass
        if ip in self._by_ip and not self._by_ip[ip]:
            del self._by_ip[ip]

    def succeed(self, ip: str, ticket: float | None = None) -> None:
        """The key was right: the reservation and the IP's failures go."""
        with self._lock:
            self._drop(ip, ticket)
            self._by_ip.pop(ip, None)

    def cancel(self, ip: str, ticket: float | None) -> None:
        """The attempt never checked a key (e.g. the slow lane timed out)."""
        with self._lock:
            self._drop(ip, ticket)

    def slow_lane(self, ip: str, ticket: float | None, check: Callable[[], bool]) -> bool | None:
        """Run ``check`` in the global slow lane; None when it got no turn.

        A full queue answers at once and the attempt is dropped (no key was
        tried). A wait that runs out keeps the reservation, so it counts as a
        failure: one address can't hold the queue forever for free."""
        with self._lock:
            if self._waiting >= self.slow_queue:
                self._drop(ip, ticket)
                return None
            self._waiting += 1
        try:
            got_turn = self._slow.acquire(timeout=self.slow_wait)
        finally:
            with self._lock:
                self._waiting -= 1
        if not got_turn:
            return None
        try:
            time.sleep(self.slow_delay)
            return check()
        finally:
            self._slow.release()

    # Kept for callers that record outside begin() (and for tests).
    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._all.append(now)
            self._by_ip.setdefault(ip, collections.deque()).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._by_ip.pop(ip, None)


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS, digest: str = "sha1") -> str:
    """RFC 4226 one-time code for ``counter``."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def totp(
    key: bytes, at: float, digits: int = TOTP_DIGITS, step: int = TOTP_STEP, digest: str = "sha1"
) -> str:
    """RFC 6238 code for unix time ``at``."""
    return hotp(key, int(at // step), digits, digest)


def parse_totp_secret(raw: str) -> bytes | None:
    """The key behind a base32 TOTP secret (spaces, dashes, lower case and
    missing padding are fine), or None when it isn't one or is too short."""
    text = re.sub(r"[\s-]", "", raw or "").upper().rstrip("=")
    if not text or not re.fullmatch(r"[A-Z2-7]+", text):
        return None
    try:
        key = base64.b32decode(text + "=" * (-len(text) % 8))
    except (binascii.Error, ValueError):
        return None
    return key if len(key) >= TOTP_MIN_SECRET_BYTES else None


class TotpChecker:
    """Second sign-in factor, on when TOTP_SECRET is set.

    A secret that doesn't parse fails closed (every code is wrong) so a typo
    can't silently turn the second factor off; main() says so at boot. A code
    is accepted once: a step already used can't sign in again."""

    def __init__(
        self,
        secret_source: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret_source = secret_source or (lambda: os.environ.get("TOTP_SECRET", ""))
        self.clock = clock
        self._lock = threading.Lock()
        self._last_step: dict[bytes, int] = {}

    def _raw(self) -> str:
        raw = str(self._secret_source() or "").strip()
        return "" if raw.startswith("${{") else raw

    def enabled(self) -> bool:
        return bool(self._raw())

    def valid_secret(self) -> bool:
        return parse_totp_secret(self._raw()) is not None

    def check(self, submitted: str) -> bool:
        key = parse_totp_secret(self._raw())
        code = re.sub(r"\s", "", submitted or "")
        if key is None or not re.fullmatch(rf"\d{{{TOTP_DIGITS}}}", code):
            return False
        now = int(self.clock() // TOTP_STEP)
        with self._lock:
            last = self._last_step.get(key, -1)
            for step in range(now - TOTP_DRIFT, now + TOTP_DRIFT + 1):
                if step > last and hmac.compare_digest(hotp(key, step), code):
                    self._last_step = {key: step}
                    return True
        return False


class SessionManager:
    def __init__(
        self,
        key_source: Callable[[], str] | None = None,
        secret_source: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
        store_dir: Callable[[], str] | None = None,
    ) -> None:
        self._key_source = key_source or config.get_admin_key
        self._secret_source = secret_source or (
            lambda: config.load_or_create_session_secret(installer.DATA_DIR)
        )
        self._store_dir = store_dir or (lambda: installer.DATA_DIR)
        self.clock = clock
        self._lock = threading.Lock()
        self._stores: dict[str, dict[str, Any]] = {}
        self._secret_cache: dict[str, str] = {}

    # -- revocations kept on the volume ----------------------------------------
    def _store(self) -> tuple[str, dict[str, Any]]:
        """(data dir, {"revoked": {nonce: expiry}, "not_before": ts}); call under _lock."""
        directory = self._store_dir()
        store = self._stores.get(directory)
        if store is None:
            store = {"revoked": {}, "not_before": 0}
            data = installer._read_json(os.path.join(directory, REVOKED_FILE))
            if isinstance(data, dict):
                revoked = data.get("revoked")
                if isinstance(revoked, dict):
                    store["revoked"] = {
                        str(k): float(v) for k, v in revoked.items()
                        if isinstance(v, (int, float)) and len(str(k)) <= 64
                    }
                if isinstance(data.get("not_before"), (int, float)):
                    store["not_before"] = int(data["not_before"])
            self._stores[directory] = store
        return directory, store

    def _save(self, directory: str, store: dict[str, Any]) -> None:
        now = self.clock()
        revoked = {k: v for k, v in store["revoked"].items() if v >= now}
        if len(revoked) > MAX_REVOKED:
            # Keep the ones that stay valid longest; older tokens expire sooner.
            keep = sorted(revoked.items(), key=lambda item: item[1], reverse=True)[:MAX_REVOKED]
            revoked = dict(keep)
        store["revoked"] = revoked
        try:
            installer._atomic_write_json(
                os.path.join(directory, REVOKED_FILE),
                {"revoked": revoked, "not_before": store["not_before"]},
            )
        except OSError as exc:  # still revoked in memory
            print(f"Couldn't save the signed-out sessions: {exc}", flush=True)

    # -- signing ---------------------------------------------------------------
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
        with self._lock:
            _directory, store = self._store()
            floor = store["not_before"] + 1
        # A login in the same second as "sign out everywhere" still counts.
        issued = max(int(self.clock()), floor)
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
            _directory, store = self._store()
            if issued <= store["not_before"]:
                return False
            return nonce not in store["revoked"]

    def revoke(self, token: str | None) -> None:
        parsed = self._parse(token or "")
        if parsed is None:
            return
        issued, nonce = parsed
        with self._lock:
            directory, store = self._store()
            store["revoked"][nonce] = issued + SESSION_MAX_AGE
            self._save(directory, store)

    def revoke_all(self) -> None:
        """Sign out every browser: tokens issued up to now stop working."""
        with self._lock:
            directory, store = self._store()
            store["not_before"] = int(self.clock())
            store["revoked"] = {}
            self._save(directory, store)
            # Re-read the volume secret too (a deleted .session-secret now counts).
            self._secret_cache.clear()

    # -- cookies ---------------------------------------------------------------
    def cookie_header(self, token: str, secure: bool) -> str:
        flags = f"Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
        if secure:
            return f"{SECURE_COOKIE_NAME}={token}; {flags}; Secure"
        return f"{COOKIE_NAME}={token}; {flags}"

    @staticmethod
    def clear_cookie_headers(secure: bool) -> list[str]:
        flags = "Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        headers = []
        if secure:
            headers.append(f"{SECURE_COOKIE_NAME}=; {flags}; Secure")
            headers.append(f"{COOKIE_NAME}=; {flags}; Secure")
        else:
            headers.append(f"{COOKIE_NAME}=; {flags}")
        return headers

    @staticmethod
    def clear_cookie_header(secure: bool) -> str:
        return SessionManager.clear_cookie_headers(secure)[-1]


def cookie_value(cookie_header: str | None, name: str = COOKIE_NAME) -> str | None:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def session_token(cookie_header: str | None) -> str | None:
    """The session cookie: the __Host- one when present (it can't be set by
    another subdomain), else the plain name (local http, and sessions from
    before the rename)."""
    return cookie_value(cookie_header, SECURE_COOKIE_NAME) or cookie_value(cookie_header, COOKIE_NAME)


sessions = SessionManager()
limiter = RateLimiter()
second_factor = TotpChecker()


def ensure_session_secret() -> None:
    """Create .session-secret at boot so the first login doesn't race to create it."""
    config.load_or_create_session_secret(installer.DATA_DIR)
