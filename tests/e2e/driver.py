"""End-to-end check of a running Railpowered server.

It drives the dashboard API exactly as the dashboard does (the rp_session
cookie from signing in, plus the X-Railpowered header on every change) and
talks to the Minecraft port the way a player's launcher does (mcproto.py).
Nothing here knows about Docker: tests/e2e/run_docker.sh starts the
container and points this script at it. Standard library only.

Scenario "full" (the default) expects a fresh volume with AUTO_START off:

  health       /health answers
  auth         sign-in, 401 without a session, 403 without the dashboard
               header or from another origin
  prepare      the boot "prepare" task finishes and the server sleeps
  proxy        the sleep proxy answers modern and 1.6 pings with the real
               version, and a ping does not wake the server
  start        Start from the dashboard until online; the real server
               answers pings and console commands reach it
  backup       a live backup while online (the server never leaves online);
               the archive downloads and holds the world
  stop         Stop from the dashboard; the proxy owns the port again
  wake         a join attempt is told "waking up" and starts the server
  fabric       create and activate Fabric on a new world while online (the
               stop, install, start path); meanwhile the proxy says
               "updating" and a join does not wake anything
  switch-back  activate the first setup again
  idle         a 1-minute sleep timer puts the empty server to sleep
  online       start once more and leave it running, so the caller can
               check that SIGTERM saves and exits cleanly

Scenario "redeploy" runs against a new container on the same volume after
"full" (using its --state file): the session cookie still works, setups,
worlds and backups are still there, and the server prepares and sleeps.

    python3 tests/e2e/driver.py --url http://127.0.0.1:8080 --key "$ADMIN_KEY"
    python3 tests/e2e/driver.py --list
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import sys
import tarfile
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

try:  # imported as tests/e2e package (unit tests) or run as a script
    from . import mcproto
except ImportError:  # pragma: no cover - script mode
    import mcproto  # type: ignore[no-redef]

PLAYER = "RailpoweredCI"
FABRIC_WORLD = "e2e-fabric"
SLEEPING_MOTD = "Sleeping"  # proxy.MOTD_SUFFIX for the sleeping mode
UPDATING_MOTD = "Updating"
POLL = 2.0


class CheckFailed(Exception):
    """A check did not hold. The message says what was expected and what happened."""


# -- output ------------------------------------------------------------------
def say(text: str = "") -> None:
    print(text, flush=True)


def detail(text: str) -> None:
    say(f"      {text}")


# -- dashboard API -------------------------------------------------------------
@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CheckFailed(f"expected JSON, got HTTP {self.status}: {self.body[:200]!r}") from None


class Api:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise SystemExit(f"--url must be an http:// URL, not {base_url!r}")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.timeout = timeout
        self.cookie: str | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        session: bool = True,
        dashboard: bool = True,
        timeout: float | None = None,
    ) -> Reply:
        sent: dict[str, str] = {}
        data: bytes | None = None
        if form is not None:
            data = urlencode(form).encode("utf-8")
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        if session and self.cookie:
            sent["Cookie"] = f"rp_session={self.cookie}"
        if dashboard and method != "GET":
            sent["X-Railpowered"] = "1"
        sent.update(headers or {})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            payload = response.read()
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, payload)
        finally:
            conn.close()

    def call(self, method: str, path: str, expect: int = 200, **kwargs: Any) -> Any:
        """JSON request that must answer `expect`; returns the decoded body."""
        reply = self.request(method, path, **kwargs)
        if reply.status != expect:
            try:
                info = reply.json()
                why = f"{info.get('code')}: {info.get('error')}" if isinstance(info, dict) else info
            except CheckFailed:
                why = reply.body[:200]
            raise CheckFailed(f"{method} {path} answered {reply.status} ({why}), expected {expect}")
        return reply.json()

    def status(self) -> dict[str, Any]:
        return self.call("GET", "/api/status")

    def log_mark(self) -> int:
        return int(self.call("GET", "/api/logs")["last_seq"])

    def logs_since(self, mark: int) -> list[str]:
        return [line["text"] for line in self.call("GET", f"/api/logs?after={mark}")["lines"]]


# -- shared state ----------------------------------------------------------------
@dataclass
class Context:
    api: Api
    key: str
    mc_host: str
    mc_port: int
    deadline: float
    fabric_version: str
    state: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def wait_until(ctx: Context, what: str, check: Callable[[], tuple[Any, str]], timeout: float) -> Any:
    """Poll `check` (returns (result or None, note)) until it yields a result.

    Each new note (for example "phase starting") is printed once, so the log
    shows how the server moved without repeating itself."""
    started = time.monotonic()
    deadline = min(started + timeout, ctx.deadline)
    last = None
    while True:
        result, note = check()
        if note and note != last:
            detail(f"[{time.monotonic() - started:5.0f}s] {note}")
            last = note
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            limit = "the overall deadline" if deadline == ctx.deadline else f"{timeout:.0f}s"
            raise CheckFailed(f"{what}: still '{note}' after {limit}")
        time.sleep(POLL)


def _describe(status: dict[str, Any]) -> str:
    task = status.get("task") or {}
    note = f"phase {status.get('phase')}"
    if task.get("state") in ("running", "failed"):  # finished tasks linger in the status for 5 minutes
        note += f', task "{task.get("label")}" {task.get("state")}'
    return note


def wait_phase(ctx: Context, target: str, timeout: float, *, idle_tasks: bool = True) -> dict[str, Any]:
    """Wait for `target` phase (and, by default, no running task). A crash fails at once."""

    def check() -> tuple[Any, str]:
        status = ctx.api.status()
        phase = status.get("phase")
        if phase == "crashed" and target != "crashed":
            reason = (status.get("last_exit") or {}).get("reason") or "no reason recorded"
            raise CheckFailed(f"Minecraft crashed while waiting for {target}: {reason}")
        task = status.get("task") or {}
        busy = task.get("state") == "running"
        return (status if phase == target and not (idle_tasks and busy) else None), _describe(status)

    return wait_until(ctx, f"waiting for phase {target}", check, timeout)


def wait_task(
    ctx: Context,
    task: dict[str, Any],
    timeout: float,
    during: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Wait for a task the API just returned to finish; fail if it failed."""
    task_id = task["id"]

    def check() -> tuple[Any, str]:
        status = ctx.api.status()
        if during is not None:
            during(status)
        current = status.get("task")
        if current is None:
            raise CheckFailed(f'task "{task["label"]}" ({task_id}) disappeared from /api/status')
        if current.get("id") != task_id:
            raise CheckFailed(f'expected task {task_id}, but /api/status shows {current.get("id")} "{current.get("label")}"')
        if current.get("state") == "failed":
            raise CheckFailed(f'task "{current.get("label")}" failed: {current.get("error")}')
        return (current if current.get("state") == "done" else None), _describe(status)

    done = wait_until(ctx, f'task "{task["label"]}"', check, timeout)
    shown = ctx.api.call("GET", "/api/tasks/current")["task"]
    require(shown and shown.get("id") == task_id and shown.get("state") == "done",
            f"/api/tasks/current should report {task_id} as done, got {shown}")
    return done


def submit_task(ctx: Context, method: str, path: str, body: dict[str, Any], kind: str) -> dict[str, Any]:
    task = ctx.api.call(method, path, expect=202, body=body)["task"]
    require(task and task.get("kind") == kind and task.get("state") in ("running", "done"),
            f"{method} {path} should start a {kind} task, got {task}")
    detail(f'started task {task["id"]} "{task["label"]}"')
    return task


# -- Minecraft port -------------------------------------------------------------------
def ping(ctx: Context, wait: float = 20.0) -> mcproto.Status:
    try:
        return mcproto.retry(lambda: mcproto.status(ctx.mc_host, ctx.mc_port), wait)
    except OSError as exc:
        raise CheckFailed(f"nothing answered a status ping on {ctx.mc_host}:{ctx.mc_port} ({exc})") from None


def expect_proxy(ctx: Context, version: str, max_players: int, motd: str = SLEEPING_MOTD) -> mcproto.Status:
    reply = ping(ctx)
    require(motd in reply.description,
            f"the sleep proxy should show '{motd}' in the MOTD, got {reply.description!r}")
    require(reply.version_name == version,
            f"the sleep proxy should report version {version}, got {reply.version_name!r}")
    require(reply.max_players == max_players,
            f"the sleep proxy should report {max_players} slots, got {reply.max_players}")
    detail(f"proxy ping: {reply.version_name}, {reply.description!r}, {reply.latency_ms} ms")
    return reply


def expect_minecraft(ctx: Context, version: str) -> mcproto.Status:
    reply = ping(ctx)
    require(SLEEPING_MOTD not in reply.description and UPDATING_MOTD not in reply.description,
            f"port {ctx.mc_port} should be the real server now, but the MOTD is {reply.description!r}")
    require(reply.version_name == version,
            f"the server should report version {version}, got {reply.version_name!r}")
    detail(f"server ping: {reply.version_name} (protocol {reply.protocol}), {reply.description!r}, {reply.latency_ms} ms")
    return reply


def find_line(lines: list[str], pattern: str) -> str | None:
    regex = re.compile(pattern)
    return next((line for line in lines if regex.search(line)), None)


def expect_line(ctx: Context, mark: int, pattern: str, what: str, timeout: float = 20.0) -> str:
    def check() -> tuple[Any, str]:
        return find_line(ctx.api.logs_since(mark), pattern), ""

    try:
        return wait_until(ctx, what, check, timeout)
    except CheckFailed:
        raise CheckFailed(f"the dashboard log never showed {what} (/{pattern}/)") from None


# -- steps: full scenario -----------------------------------------------------------------
def step_health(ctx: Context) -> None:
    def check() -> tuple[Any, str]:
        try:
            reply = ctx.api.request("GET", "/health", session=False, timeout=5)
        except OSError as exc:
            return None, f"not answering yet ({type(exc).__name__})"
        if reply.status != 200:
            return None, f"HTTP {reply.status}"
        return (reply.json() if reply.json().get("status") == "ok" else None), "answering"

    wait_until(ctx, "/health", check, 180)


def step_auth(ctx: Context) -> None:
    api = ctx.api
    anonymous = api.request("GET", "/api/status", session=False)
    require(anonymous.status == 401 and anonymous.json().get("code") == "unauthorized",
            f"/api/status without a session should be 401 unauthorized, got {anonymous.status}")
    detail("no session: 401")

    wrong = api.request("POST", "/login", form={"key": "not-the-key"}, session=False, dashboard=False)
    require(wrong.status == 303 and wrong.headers.get("location") == "/login?error=1",
            f"a wrong key should redirect to /login?error=1, got {wrong.status} {wrong.headers.get('location')}")
    require("set-cookie" not in wrong.headers, "a wrong key must not set a cookie")
    detail("wrong key: back to /login?error=1, no cookie")

    good = api.request("POST", "/login", form={"key": ctx.key}, session=False, dashboard=False)
    cookie = good.headers.get("set-cookie", "")
    match = re.match(r"rp_session=([^;]+);", cookie)
    require(good.status == 303 and good.headers.get("location") == "/" and match,
            f"signing in should redirect to / with a session cookie, got {good.status} "
            f"{good.headers.get('location')} {cookie!r}")
    flags = cookie.lower()
    require("httponly" in flags and "samesite=lax" in flags, f"the session cookie lacks HttpOnly/SameSite: {cookie!r}")
    api.cookie = match.group(1)
    ctx.state["cookie"] = api.cookie
    detail("signed in: rp_session cookie (HttpOnly, SameSite=Lax)")

    page = api.request("GET", "/")
    require(page.status == 200 and page.headers.get("content-type", "").startswith("text/html"),
            f"the dashboard page should load once signed in, got {page.status}")

    # /api/server/save is harmless either way: it only ever saves the world.
    bare = api.request("POST", "/api/server/save", body={}, dashboard=False)
    require(bare.status == 403 and bare.json().get("code") == "csrf",
            f"a change without the X-Railpowered header should be 403 csrf, got {bare.status}")
    foreign = api.request("POST", "/api/server/save", body={}, headers={"Origin": "https://evil.example"})
    require(foreign.status == 403 and foreign.json().get("code") == "csrf",
            f"a change from another origin should be 403 csrf, got {foreign.status}")
    detail("missing header and foreign Origin: 403 csrf")


def step_prepare(ctx: Context) -> None:
    def check() -> tuple[Any, str]:
        status = ctx.api.status()
        task = status.get("task") or {}
        if task.get("state") == "failed":
            raise CheckFailed(f'task "{task.get("label")}" failed: {task.get("error")}')
        if status.get("phase") == "crashed":
            raise CheckFailed(f"the server is crashed: {(status.get('last_exit') or {}).get('reason')}")
        ready = status.get("phase") == "sleeping" and task.get("state") != "running"
        return (status if ready else None), _describe(status)

    status = wait_until(ctx, "preparing the server files", check, 600)
    setup = status["setup"]
    require(setup.get("resolved_version"), f"the active setup has no resolved version after preparing: {setup}")
    ctx.state.update(
        setup_id=setup["id"],
        setup_type=setup["type"],
        version=setup["resolved_version"],
        level=setup["level_name"],
        max_players=status["max_players"],
    )
    detail(f'active setup "{setup["name"]}": {setup["type"]} {setup["resolved_version"]}, '
           f'world {setup["level_name"]}, Java {status.get("java")}')
    require(status.get("last_exit") is None, f"a fresh server should have no last exit, got {status.get('last_exit')}")


def step_prepare_full(ctx: Context) -> None:
    step_prepare(ctx)
    require(ctx.state["setup_type"] == "vanilla",
            f"a fresh deploy should start on vanilla, got {ctx.state['setup_type']}")
    versions = ctx.api.call("GET", "/api/versions")
    require(versions.get("vanilla"), "/api/versions lists no vanilla versions")
    detail(f"/api/versions: {len(versions['vanilla'])} vanilla, {len(versions.get('fabric') or [])} fabric"
           + (" (offline list)" if versions.get("offline") else ""))


def step_proxy(ctx: Context) -> None:
    mark = ctx.api.log_mark()
    version, slots = ctx.state["version"], ctx.state["max_players"]
    expect_proxy(ctx, version, slots)
    try:
        legacy = mcproto.legacy_ping(ctx.mc_host, ctx.mc_port)
    except (OSError, mcproto.ProtocolError) as exc:
        raise CheckFailed(f"the sleep proxy did not answer a 1.6 ping: {exc}") from None
    require(legacy.version == version and "(sleeping)" in legacy.motd and legacy.max_players == slots,
            f"unexpected 1.6 ping reply: {legacy}")
    detail(f"1.6 ping: {legacy.version}, {legacy.motd!r}")
    time.sleep(1.5)
    status = ctx.api.status()
    require(status["phase"] == "sleeping", f"a ping must not wake the server, but the phase is {status['phase']}")
    require(not find_line(ctx.api.logs_since(mark), r"Join attempt|Starting Minecraft"),
            "a ping was logged as a join attempt")


def step_start(ctx: Context) -> None:
    mark = ctx.api.log_mark()
    ctx.api.call("POST", "/api/server/start", body={}, timeout=180)
    status = wait_phase(ctx, "online", 420)
    require(status.get("last_exit") is None and status.get("uptime") is not None,
            f"an online server should have an uptime and no last exit: {status}")
    expect_line(ctx, mark, r"Minecraft is ready", "Minecraft is ready")
    expect_minecraft(ctx, ctx.state["version"])

    mark = ctx.api.log_mark()
    ctx.api.call("POST", "/api/console", body={"command": "/list"})
    line = expect_line(ctx, mark, r"There are 0(?: of a max(?: of)? |/)\d+ players online", "the reply to 'list'")
    detail(f"console: list -> {line.strip()}")


def step_backup(ctx: Context) -> None:
    api, world = ctx.api, ctx.state["level"]
    before = {item["name"] for item in api.call("GET", "/api/backups")["backups"]}
    mark = api.log_mark()
    phases: set[str] = set()
    task = submit_task(ctx, "POST", "/api/backups", {}, "backup")
    wait_task(ctx, task, 300, during=lambda status: phases.add(status["phase"]))
    require(phases == {"online"}, f"the server must stay online during a live backup; saw phases {sorted(phases)}")

    lines = api.logs_since(mark)
    require(not find_line(lines, r"didn't confirm"), "Minecraft never confirmed the save before the backup")
    listing = api.call("GET", "/api/backups")
    new = [item for item in listing["backups"] if item["name"] not in before]
    require(len(new) == 1, f"expected exactly one new backup, got {new}")
    backup = new[0]
    require(backup["world"] == world and backup["kind"] == "manual" and backup["size_bytes"] > 0,
            f"unexpected backup entry: {backup}")
    require(backup["minecraft_version"] == ctx.state["version"],
            f"the backup should record version {ctx.state['version']}, got {backup['minecraft_version']}")
    require(find_line(lines, re.escape(f"as {backup['name']}")), "the log does not mention the new backup")
    detail(f"{backup['name']}: {backup['size_bytes'] / 1048576:.1f} MiB, {backup['minecraft_version']}")

    reply = api.request("GET", f"/api/backups/{quote(backup['name'])}/download", timeout=120)
    require(reply.status == 200 and reply.headers.get("content-type") == "application/gzip",
            f"downloading the backup answered {reply.status} {reply.headers.get('content-type')}")
    require(int(reply.headers.get("content-length", -1)) == len(reply.body) == backup["size_bytes"],
            "the download's size does not match the backup")
    with tarfile.open(fileobj=io.BytesIO(reply.body), mode="r:gz") as archive:
        names = archive.getnames()
        require("railpowered-backup.json" in names, "the archive has no railpowered-backup.json")
        member = archive.extractfile("railpowered-backup.json")
        meta = json.loads(member.read().decode("utf-8")) if member else {}
    require(f"{world}/level.dat" in names, f"the archive has no {world}/level.dat ({len(names)} entries)")
    require(not any(name.endswith("session.lock") for name in names), "the archive includes session.lock")
    require(meta.get("world") == world and meta.get("kind") == "manual", f"unexpected backup metadata: {meta}")
    detail(f"download: {len(names)} entries, level.dat and railpowered-backup.json present")
    ctx.state["backup"] = backup["name"]


def step_stop(ctx: Context) -> None:
    ctx.api.call("POST", "/api/server/stop", body={}, timeout=120)
    wait_phase(ctx, "sleeping", 60)
    expect_proxy(ctx, ctx.state["version"], ctx.state["max_players"])


def step_wake(ctx: Context) -> None:
    mark = ctx.api.log_mark()
    try:
        reply = mcproto.retry(lambda: mcproto.login(ctx.mc_host, ctx.mc_port, PLAYER), 20)
    except (OSError, mcproto.ProtocolError) as exc:
        raise CheckFailed(f"the join attempt got no answer: {exc}") from None
    require(reply.kind == "disconnect" and "waking up" in reply.reason,
            f"a join while asleep should be told the server is waking up, got {reply.kind} {reply.reason!r}")
    detail(f"join attempt as {PLAYER}: kicked with {reply.reason!r}")
    expect_line(ctx, mark, rf"Join attempt by {PLAYER} while sleeping", "the join attempt")
    expect_line(ctx, mark, rf"Starting Minecraft \({PLAYER} joined\)", "the wake-up start")
    wait_phase(ctx, "online", 300)
    expect_minecraft(ctx, ctx.state["version"])


def step_fabric(ctx: Context) -> None:
    api, version = ctx.api, ctx.fabric_version
    worlds = {item["name"] for item in api.call("GET", "/api/worlds")["worlds"]}
    require(FABRIC_WORLD not in worlds, f"world {FABRIC_WORLD} already exists; run the full scenario on a fresh volume")
    mark = api.log_mark()
    probe: dict[str, Any] = {}

    def check_proxy_while_busy(status: dict[str, Any]) -> None:
        # While the task has Minecraft stopped ("preparing"), the proxy must
        # own the port, say "updating", and refuse to wake the server.
        if probe or status.get("phase") != "preparing":
            return
        try:
            pinged = mcproto.status(ctx.mc_host, ctx.mc_port, timeout=5)
            kicked = mcproto.login(ctx.mc_host, ctx.mc_port, PLAYER, timeout=5)
        except (OSError, mcproto.ProtocolError) as exc:
            if api.status().get("phase") == "preparing":
                raise CheckFailed(f"the sleep proxy did not answer while the server was preparing: {exc}") from None
            return  # the install finished between the poll and the ping; try the next poll
        require(UPDATING_MOTD in pinged.description,
                f"while preparing, the proxy MOTD should say '{UPDATING_MOTD}', got {pinged.description!r}")
        require(kicked.kind == "disconnect" and "updating" in kicked.reason,
                f"a join while preparing should be told the server is updating, got {kicked.kind} {kicked.reason!r}")
        probe.update(motd=pinged.description, kick=kicked.reason)

    task = submit_task(
        ctx,
        "POST",
        "/api/setups",
        {
            "type": "fabric",
            "minecraft_version": version,
            "name": f"E2E Fabric {version}",
            "level_name": FABRIC_WORLD,
            "create_world": True,
            "activate": True,
        },
        "apply",
    )
    wait_task(ctx, task, 900, during=check_proxy_while_busy)
    if probe:
        detail(f"while preparing: proxy MOTD {probe['motd']!r}, join told {probe['kick']!r}")
    else:
        ctx.notes.append("fabric: the install finished before the proxy could be probed in the 'preparing' phase")
        detail("note: never caught the 'preparing' phase, so the proxy was not probed during the install")

    status = wait_phase(ctx, "online", 420)
    setup = status["setup"]
    require(setup["type"] == "fabric" and setup["resolved_version"] == version and setup["level_name"] == FABRIC_WORLD,
            f"the active setup should be Fabric {version} on {FABRIC_WORLD}, got {setup}")
    require(status.get("java") == 21, f"Fabric {version} should run on Java 21, got {status.get('java')}")
    lines = api.logs_since(mark)
    require(find_line(lines, r"with Fabric Loader"), "Minecraft's log never showed Fabric Loader starting")
    require(not find_line(lines, r"Join attempt by"), "a join during the install was treated as a wake-up")
    detail(f'active setup "{setup["name"]}" on Java {status["java"]}, Fabric Loader running')
    expect_minecraft(ctx, version)

    setups = api.call("GET", "/api/setups")["setups"]
    fabric = [item for item in setups if item["type"] == "fabric" and item["active"]]
    original = [item for item in setups if item["id"] == ctx.state["setup_id"]]
    require(len(fabric) == 1 and original and not original[0]["active"],
            f"expected the new Fabric setup active and the first one kept: {setups}")
    ctx.state["fabric_setup_id"] = fabric[0]["id"]


def step_switch_back(ctx: Context) -> None:
    setup_id = ctx.state["setup_id"]
    task = submit_task(ctx, "POST", f"/api/setups/{quote(setup_id)}/activate", {}, "switch")
    wait_task(ctx, task, 600)
    status = wait_phase(ctx, "online", 300)
    setup = status["setup"]
    require(setup["id"] == setup_id and setup["level_name"] == ctx.state["level"]
            and setup["resolved_version"] == ctx.state["version"],
            f"expected setup {setup_id} on world {ctx.state['level']} again, got {setup}")
    detail(f'back on "{setup["name"]}" ({setup["resolved_version"]}, world {setup["level_name"]})')
    expect_minecraft(ctx, ctx.state["version"])


def step_idle(ctx: Context) -> None:
    api = ctx.api
    previous = api.call("GET", "/api/settings")["railpowered"]["idle_timeout_minutes"]
    mark = api.log_mark()
    api.call("PUT", "/api/settings", body={"railpowered": {"idle_timeout_minutes": 1}})
    try:
        idle = api.status()["idle"]
        require(idle["timeout_seconds"] == 60, f"the sleep timer should now be 60 s, got {idle}")
        detail(f"sleep timer 1 min; status says {idle['seconds_until_sleep']} s to go")
        wait_phase(ctx, "sleeping", 180)
        expect_line(ctx, mark, r"No players for 1 minute; going to sleep", "the idle stop")
    finally:
        api.call("PUT", "/api/settings", body={"railpowered": {"idle_timeout_minutes": previous}})
    detail(f"sleep timer restored to {previous} min")
    expect_proxy(ctx, ctx.state["version"], ctx.state["max_players"])


def step_online(ctx: Context) -> None:
    ctx.api.call("POST", "/api/server/start", body={}, timeout=180)
    wait_phase(ctx, "online", 300)
    expect_minecraft(ctx, ctx.state["version"])
    detail("left online for the shutdown check")


# -- steps: redeploy scenario ----------------------------------------------------------------
def step_session(ctx: Context) -> None:
    cookie = ctx.state.get("cookie")
    require(cookie, "the state file has no session cookie; run the full scenario first")
    ctx.api.cookie = cookie
    reply = ctx.api.request("GET", "/api/status")
    require(reply.status == 200, f"the session from before the redeploy should still work, got {reply.status}")
    detail("the session cookie from before the redeploy still works")


def step_persisted(ctx: Context) -> None:
    api = ctx.api
    setups = {item["id"]: item for item in api.call("GET", "/api/setups")["setups"]}
    original, fabric = ctx.state.get("setup_id"), ctx.state.get("fabric_setup_id")
    require(original in setups and setups[original]["active"], f"setup {original} should still be active: {setups}")
    if fabric:
        require(fabric in setups and setups[fabric]["type"] == "fabric", f"the Fabric setup {fabric} is gone: {setups}")
    worlds = {item["name"] for item in api.call("GET", "/api/worlds")["worlds"]}
    expected = {ctx.state["level"]} | ({FABRIC_WORLD} if fabric else set())
    require(expected <= worlds, f"worlds {sorted(expected - worlds)} are missing (have {sorted(worlds)})")
    backups = {item["name"] for item in api.call("GET", "/api/backups")["backups"]}
    if ctx.state.get("backup"):
        require(ctx.state["backup"] in backups, f"backup {ctx.state['backup']} is missing (have {sorted(backups)})")
    detail(f"{len(setups)} setups, worlds {sorted(worlds)}, {len(backups)} backup(s) kept")


def step_prepare_redeploy(ctx: Context) -> None:
    expected = dict(ctx.state)
    step_prepare(ctx)
    require(ctx.state["setup_id"] == expected.get("setup_id") and ctx.state["version"] == expected.get("version"),
            f"after the redeploy the active setup should be {expected.get('setup_id')} on "
            f"{expected.get('version')}, got {ctx.state['setup_id']} on {ctx.state['version']}")


@dataclass
class Step:
    name: str
    title: str
    run: Callable[[Context], None]


SCENARIOS: dict[str, list[Step]] = {
    "full": [
        Step("health", "Dashboard answers /health", step_health),
        Step("auth", "Sign-in, sessions and CSRF protection", step_auth),
        Step("prepare", "Boot prepares the server files, then sleeps", step_prepare_full),
        Step("proxy", "Sleep proxy answers server-list pings", step_proxy),
        Step("start", "Start from the dashboard until online", step_start),
        Step("backup", "Live backup while online", step_backup),
        Step("stop", "Stop from the dashboard; the proxy takes the port back", step_stop),
        Step("wake", "A join attempt wakes the server", step_wake),
        Step("fabric", "Create and activate a Fabric setup on a new world", step_fabric),
        Step("switch-back", "Switch back to the first setup", step_switch_back),
        Step("idle", "The sleep timer stops an empty server", step_idle),
        Step("online", "Start again for the shutdown check", step_online),
    ],
    "redeploy": [
        Step("health", "Dashboard answers /health", step_health),
        Step("session", "The session survives the redeploy", step_session),
        Step("persisted", "Setups, worlds and backups survive the redeploy", step_persisted),
        Step("prepare", "Boot prepares the active setup, then sleeps", step_prepare_redeploy),
        Step("proxy", "Sleep proxy answers server-list pings", step_proxy),
    ],
}


# -- runner ---------------------------------------------------------------------------------------
def _print_dashboard_log(api: Api, count: int = 40) -> None:
    try:
        lines = api.call("GET", "/api/logs", timeout=10)["lines"][-count:]
    except Exception as exc:  # the server may be the thing that broke
        say(f"(could not read the dashboard log: {exc})")
        return
    say(f"--- last {len(lines)} dashboard log lines ---")
    for line in lines:
        say(f"[{line.get('t')}] {line.get('level', 'info'):5} {line.get('text')}")


def _write_summary(path: str, scenario: str, results: list[tuple[Step, str, float]], notes: list[str]) -> None:
    rows = [f"### Railpowered end-to-end: {scenario}", "", "| Step | Result | Time |", "|---|---|---|"]
    rows += [
        f"| {step.title} | {outcome} | {'-' if outcome == 'not run' else f'{seconds:.0f}s'} |"
        for step, outcome, seconds in results
    ]
    rows += [""] + [f"- Note: {note}" for note in notes]
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n\n")
    except OSError as exc:
        say(f"(could not write the job summary: {exc})")


def run(ctx: Context, steps: list[Step], scenario: str, summary: str | None) -> int:
    results: list[tuple[Step, str, float]] = []
    total = time.monotonic()
    failed = False
    for index, step in enumerate(steps, 1):
        say(f"[{index:2}/{len(steps)}] {step.title}  ({step.name})")
        started = time.monotonic()
        try:
            step.run(ctx)
        except Exception as exc:  # report every failure the same way, then stop
            seconds = time.monotonic() - started
            if isinstance(exc, CheckFailed):
                say(f"      FAILED after {seconds:.0f}s: {exc}")
            elif isinstance(exc, (OSError, mcproto.ProtocolError, http.client.HTTPException)):
                say(f"      FAILED after {seconds:.0f}s: {type(exc).__name__}: {exc}")
            else:  # a bug in this script, or a step run without the steps it needs
                say(f"      ERROR after {seconds:.0f}s:")
                say(traceback.format_exc().rstrip())
            results.append((step, "FAILED", seconds))
            _print_dashboard_log(ctx.api)
            failed = True
            break
        seconds = time.monotonic() - started
        say(f"      ok ({seconds:.0f}s)")
        results.append((step, "passed", seconds))
    for step in steps[len(results):]:
        results.append((step, "not run", 0.0))
    if summary:
        _write_summary(summary, scenario, results, ctx.notes)
    say()
    for note in ctx.notes:
        say(f"Note: {note}")
    verdict = "FAILED" if failed else "passed"
    say(f"End-to-end scenario '{scenario}' {verdict} in {time.monotonic() - total:.0f}s.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end check of a running Railpowered server.")
    parser.add_argument("--url", help="dashboard URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--key", default=os.environ.get("ADMIN_KEY", ""), help="dashboard key (default: $ADMIN_KEY)")
    parser.add_argument("--mc-host", help="Minecraft host (default: the dashboard host)")
    parser.add_argument("--mc-port", type=int, default=25565, help="Minecraft port (default 25565)")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="full")
    parser.add_argument("--state", help="JSON file: written by 'full', read by 'redeploy'")
    parser.add_argument("--skip", action="append", default=[], metavar="STEP", help="skip a step (repeatable)")
    parser.add_argument("--fabric-version", default="1.21.1")
    parser.add_argument("--deadline-minutes", type=float, default=18.0, help="give up after this long overall")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                        help="append a Markdown summary here (default: $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--list", action="store_true", help="list the steps of each scenario and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, steps in SCENARIOS.items():
            say(f"{name}:")
            for step in steps:
                say(f"  {step.name:12} {step.title}")
        return 0
    if not args.url:
        parser.error("--url is required")
    if not args.key and args.scenario == "full":
        parser.error("--key (or $ADMIN_KEY) is required")
    known = {step.name for step in SCENARIOS[args.scenario]}
    unknown = sorted(set(args.skip) - known)
    if unknown:
        parser.error(f"unknown step(s) for '{args.scenario}': {', '.join(unknown)}")

    api = Api(args.url)
    ctx = Context(
        api=api,
        key=args.key,
        mc_host=args.mc_host or api.host,
        mc_port=args.mc_port,
        deadline=time.monotonic() + args.deadline_minutes * 60,
        fabric_version=args.fabric_version,
    )
    if args.scenario == "redeploy":
        if not args.state or not os.path.isfile(args.state):
            parser.error("the redeploy scenario needs --state from a full run")
        with open(args.state, encoding="utf-8") as handle:
            ctx.state.update(json.load(handle))
    steps = [step for step in SCENARIOS[args.scenario] if step.name not in args.skip]
    say(f"Railpowered end-to-end '{args.scenario}': dashboard {args.url}, "
        f"Minecraft {ctx.mc_host}:{ctx.mc_port}, {len(steps)} steps")
    if args.skip:
        say(f"Skipping: {', '.join(args.skip)}")
    say()
    code = run(ctx, steps, args.scenario, args.summary)
    if args.state and args.scenario == "full":
        with open(args.state, "w", encoding="utf-8") as handle:
            json.dump(ctx.state, handle, indent=2)
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.exit(main())
