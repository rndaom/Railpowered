"""Railpowered UI development mock.

A standard-library HTTP server that serves the dashboard from ``web/`` and fakes
every API endpoint the dashboard uses, with in-memory state that behaves like
the real thing: phases move through starting/online/stopping, tasks run for a
few seconds and then finish or fail, worlds remember their last version (so the
downgrade guard can be tried), and the log has a couple of hundred realistic
lines to scroll through.

It exists so the frontend can be built, tested and screenshotted without Java,
Minecraft or a Railway volume. Nothing in ``mc_host`` imports it and it is not
part of the Docker image.

    python tools/mock_server.py                 # http://127.0.0.1:8765, key "admin"
    python tools/mock_server.py --no-auth       # skip the sign-in page
    python tools/mock_server.py --phase sleeping --port 9000

Dev hooks (plain GET requests, handy from the address bar) are listed at
``/__mock/``. For example ``/__mock/phase?set=crashed`` or
``/__mock/task?kind=backup&seconds=20&fail=1``.

Ways to reproduce the interesting errors from the UI itself:
  * world_downgrade: create a setup for an older version on an existing world
    (for example 1.21.4 on "survival"), or switch to "Creative builds" after
    playing "creative" on a newer version.
  * busy: start any task, then start another before it finishes.
  * failed task: use a modpack URL or setup name containing "fail".
  * damaged backup: restore the "lost-world" backup (it can't be read).
  * 401: open /__mock/expire, then wait for the next poll.
  * pre-1.7 server: /__mock/reset?scenario=legacy (Minecraft 1.6.4, no online mode).

The mock answers with the same error codes and response shapes as mc_host/panel.py;
tests/test_mock_server.py checks that, so keep the two in step.
"""

import argparse
import gzip
import html
import json
import os
import random
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
STATIC_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NAME_RULE = "Use letters, numbers, dots, dashes and underscores (up to 32), starting with a letter or number."
CSP = (
    "default-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
MiB = 1024 * 1024
GiB = 1024 * MiB
MAX_UPLOAD = GiB
UPLOAD_HEADROOM = 64 * MiB
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

VANILLA = [
    "26.3", "26.2.1", "26.2", "26.1.2", "26.1.1", "26.1",
    "1.21.11", "1.21.10", "1.21.8", "1.21.5", "1.21.4", "1.21.1", "1.21",
    "1.20.6", "1.20.4", "1.20.1", "1.19.4", "1.19.2", "1.18.2", "1.17.1",
    "1.16.5", "1.14.4", "1.12.2", "1.8.9", "1.7.10",
]
FORGE = ["1.21.1", "1.20.1", "1.19.2", "1.18.2", "1.16.5", "1.12.2"]
LATEST = VANILLA[0]
LEGACY_VERSION = "1.6.4"

# What web.login_page() renders into {{error}} (mc_host/web.py LOGIN_ERRORS).
LOGIN_ERRORS = {
    "1": "That key didn't match. Check it and try again.",
    "rate": "Too many attempts. Wait a few minutes and try again.",
}

# Every error code the mock answers with. Each one exists in the real API
# (mc_host/panel.py, mc_host/process.py or installer.ERROR_STATUS), except
# mock_failure from the /__mock/fail hook; tests/test_mock_server.py checks it.
CODES = {
    "invalid", "invalid_name", "invalid_type", "invalid_version", "invalid_url",
    "invalid_player", "invalid_setting", "invalid_archive", "bad_json",
    "unauthorized", "csrf", "not_found", "method_not_allowed", "length_required", "too_large",
    "disk_full", "busy", "already_running", "already_stopping", "not_running", "not_online",
    "world_exists", "world_not_found", "backup_not_found", "player_not_found", "active_setup",
    "active_world", "server_running", "world_downgrade", "mock_failure",
}


def vkey(version):
    return [int(p) for p in re.findall(r"\d+", str(version))] or [0]


def vcmp(a, b):
    ka, kb = vkey(a), vkey(b)
    n = max(len(ka), len(kb))
    ka += [0] * (n - len(ka))
    kb += [0] * (n - len(kb))
    return (ka > kb) - (ka < kb)


FABRIC = [v for v in VANILLA if vcmp(v, "1.14") >= 0]


def java_for(version):
    if vcmp(version, "26") >= 0:
        return 25
    if vcmp(version, "1.20.5") >= 0:
        return 21
    if vcmp(version, "1.17") >= 0:
        return 17
    return 8


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def offline_uuid(name):
    return str(uuid.uuid3(uuid.NAMESPACE_OID, "OfflinePlayer:" + name.lower()))


class ApiError(Exception):
    def __init__(self, status, message, code, **extra):
        assert code in CODES, f"the real API has no error code {code!r}"
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.extra = extra

    def payload(self):
        body = {"error": self.message, "code": self.code}
        body.update(self.extra)
        return body


SCHEMA = {
    "motd": {"type": "string", "max": 120, "label": "Server description (MOTD)",
             "help": "Shown under the server name in the multiplayer list."},
    "difficulty": {"type": "enum", "options": ["peaceful", "easy", "normal", "hard"], "label": "Difficulty",
                   "help": "Peaceful has no hostile mobs."},
    "gamemode": {"type": "enum", "options": ["survival", "creative", "adventure", "spectator"],
                 "label": "Game mode", "help": "The mode new players start in."},
    "hardcore": {"type": "bool", "label": "Hardcore",
                 "help": "One life: players become spectators when they die."},
    "pvp": {"type": "bool", "label": "PvP", "help": "Let players hurt each other."},
    "max-players": {"type": "int", "min": 1, "max": 500, "label": "Max players",
                    "help": "How many players can be online at once."},
    "view-distance": {"type": "int", "min": 3, "max": 32, "label": "View distance",
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
    "level-type": {"type": "enum", "options": ["normal", "flat", "large_biomes", "amplified"],
                   "label": "World type", "help": "Only affects new worlds."},
    "online-mode": {"type": "bool", "label": "Online mode",
                    "help": "Check players' Minecraft accounts with Mojang. Turn off only if you "
                            "know why: anyone could then join with any name."},
    "enable-command-block": {"type": "bool", "label": "Command blocks",
                             "help": "Allow command blocks to run commands."},
}


def is_legacy(version):
    return vcmp(version, "1.7") < 0


def settings_schema(version):
    """Like installer.settings_schema: keys a version doesn't have are left out."""
    schema = json.loads(json.dumps(SCHEMA))
    if vcmp(version, "1.14") < 0:
        schema["view-distance"]["max"] = 15
    if vcmp(version, "1.8") < 0:
        schema["gamemode"]["options"].remove("spectator")
    if vcmp(version, "1.18") < 0:
        del schema["simulation-distance"]
    if is_legacy(version):
        del schema["online-mode"]  # always off: the old login servers are gone
    if vcmp(version, "1.7.2") < 0:
        schema["level-type"]["options"] = ["normal", "flat", "large_biomes"]
    return schema


CHAT = [
    "anyone want to go to the nether", "omw", "bring torches pls", "found diamonds!!", "nice",
    "where's the base again", "-120 64 340", "brb dinner", "who took my elytra", "lol",
    "the creeper got me again", "can someone build an iron farm", "i'll do it", "thanks",
    "back", "gg", "night everyone", "the village is just past the river",
]
ADVANCEMENTS = ["[Stone Age]", "[Acquire Hardware]", "[Isn't It Iron Pick]", "[Diamonds!]",
                "[We Need to Go Deeper]", "[Hot Stuff]", "[Sweet Dreams]"]


class Mock:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.sessions = set()
        self.failed_logins = 0
        # Task ids stay unique across /__mock/reset, like a restarted backend
        # should; the dashboard must not mix up an old "t1" with a new one.
        self.task_ids = 0
        self.reset(args.phase)

    # ------------------------------------------------------------------ setup

    def reset(self, phase="online", scenario="default"):
        now = time.time()
        self.rng = random.Random(7)
        self.gen = 0
        self.seq = 0
        self.logs = deque(maxlen=1000)
        self.events = []
        self.task = None
        self.phase = "sleeping"
        self.online_since = None
        self.empty_since = None
        self.players = []
        self.max_players = 10
        self.idle_timeout = 600
        self.auto_start = False
        self.last_exit = None
        self.restart_required = False
        self.failures = {}
        self.versions_offline = False
        self.max_upload = MAX_UPLOAD
        self.join_address = "shuttle.proxy.rlwy.net:28415"
        self.friends = ["TheRealZoe", "pixel_pete", "Marrowbone"]
        self.properties = {
            "motd": "Friends only. Be nice!", "difficulty": "normal", "gamemode": "survival",
            "hardcore": False, "pvp": True, "max-players": 10, "view-distance": 10,
            "simulation-distance": 10, "spawn-protection": 16, "allow-flight": False,
            "allow-nether": True, "level-seed": "", "level-type": "normal",
            "online-mode": True, "enable-command-block": False,
        }
        if scenario == "legacy":
            # A pre-1.7 server, like the friends' 1.6.4 one: no online mode, no simulation distance.
            self.whitelist_enabled = True
            self.ops = ["TheRealZoe"]
            self.whitelist = ["TheRealZoe", "pixel_pete"]
            self.setups = [self._setup("classic", "Classic 1.6.4", "vanilla", LEGACY_VERSION, "classic")]
            self.active = "classic"
            self.worlds = {"classic": {"size_bytes": 140 * MiB, "last_version": LEGACY_VERSION, "last_played": now - 7200}}
            self.backups = []
            self.properties["online-mode"] = False
            del self.properties["simulation-distance"]
            self.friends = ["TheRealZoe", "pixel_pete"]
        elif scenario == "fresh":
            self.whitelist_enabled = False
            self.ops = []
            self.whitelist = []
            self.setups = [self._setup("default", "My server", "vanilla", "latest", "world")]
            self.active = "default"
            self.worlds = {"world": {"size_bytes": 0, "last_version": None, "last_played": None}}
            self.backups = []
            self.join_address = ""
            self.friends = []
        else:
            self.whitelist_enabled = True
            self.ops = ["TheRealZoe"]
            self.whitelist = ["TheRealZoe", "pixel_pete", "Marrowbone", "quietfox", "BlockyBen"]
            self.setups = [
                self._setup("survival", "Survival", "vanilla", "latest", "survival"),
                self._setup("fabric-friends", "Fabric with friends", "fabric", "1.21.11", "fabric-world"),
                self._setup("cozy-farm", "Cozy Farm pack", "modpack", "1.21.1", "cozy-farm", instance="cozy-farm"),
                self._setup("creative", "Creative builds", "vanilla", "1.21.4", "creative"),
            ]
            self.active = "survival"
            day = 86400
            self.worlds = {
                "survival": {"size_bytes": 412 * MiB, "last_version": "26.3", "last_played": now - 1500},
                "fabric-world": {"size_bytes": 188 * MiB, "last_version": "1.21.11", "last_played": now - 3 * day},
                "cozy-farm": {"size_bytes": 655 * MiB, "last_version": "1.21.1", "last_played": now - 12 * day},
                "creative": {"size_bytes": 96 * MiB, "last_version": "26.2.1", "last_played": now - 40 * day},
                "old-world-2023": {"size_bytes": int(1.2 * GiB), "last_version": "1.20.1", "last_played": now - 400 * day},
            }
            self.backups = []
        self.seed_logs(now, scenario)
        if scenario == "default":
            day = 86400
            for world, ts, size, version, safety in [
                ("survival", self._seed_backup_ts, 268 * MiB, "26.3", False),
                ("survival", now - 2 * day - 5000, 251 * MiB, "26.3", False),
                ("survival", now - 8 * day - 900, 240 * MiB, "26.2.1", True),
                ("cozy-farm", now - 12 * day - 3000, 410 * MiB, "1.21.1", False),
                ("old-world-2023", now - 280 * day, 820 * MiB, "1.20.1", False),
            ]:
                self._add_backup(world, ts, size, version, safety)
            # An archive that can't be read: no world, no version, flagged damaged.
            self.backups.append({"name": "lost-world-20250602-101500.tar.gz", "world": None, "size_bytes": 12 * MiB,
                                 "created_at": None, "minecraft_version": None, "kind": "manual", "damaged": True,
                                 "_ts": now - 140 * day})
        if phase != "sleeping":
            self.force_phase(phase, hold=False, seeded=True)

    def _setup(self, sid, name, stype, version, level, instance=None):
        resolved = LATEST if version == "latest" else version
        return {"id": sid, "name": name, "type": stype, "minecraft_version": version,
                "resolved_version": resolved, "level_name": level, "instance": instance}

    def _add_backup(self, world, ts, size, version, safety=False):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        name = f"{world}-pre-restore-{stamp}.tar.gz" if safety else f"{world}-{stamp}.tar.gz"
        self.backups.append({"name": name, "world": world, "size_bytes": int(size),
                             "created_at": iso(ts), "minecraft_version": version,
                             "kind": "pre-restore" if safety else "manual", "damaged": False, "_ts": ts})
        return name

    # ------------------------------------------------------------------ logs

    def _log(self, ts, source, level, text):
        self.seq += 1
        self.logs.append({"seq": self.seq, "t": time.strftime("%H:%M:%S", time.localtime(ts)),
                          "text": text, "level": level, "source": source})

    def panel(self, text, level="info", ts=None):
        self._log(ts or time.time(), "panel", level, text)

    def mc(self, msg, level="info", thread="Server thread", ts=None):
        ts = ts or time.time()
        tag = {"info": "INFO", "warn": "WARN", "error": "ERROR"}[level]
        t = time.strftime("%H:%M:%S", time.localtime(ts))
        self._log(ts, "minecraft", level, f"[{t}] [{thread}/{tag}]: {msg}")

    def startup_lines(self, version, level_name):
        return [
            ("ServerMain", "info", "Environment: Environment[sessionHost=https://sessionserver.mojang.com, servicesHost=https://api.minecraftservices.com, name=PROD]"),
            ("ServerMain", "info", "Loaded 1407 recipes"),
            ("ServerMain", "info", "Loaded 1520 advancements"),
            ("Server thread", "info", f"Starting minecraft server version {version}"),
            ("Server thread", "info", "Loading properties"),
            ("Server thread", "info", "Default game type: SURVIVAL"),
            ("Server thread", "info", "Generating keypair"),
            ("Server thread", "info", "Starting Minecraft server on *:25565"),
            ("Server thread", "info", "Using epoll channel type"),
            ("Server thread", "warn", "Ambiguity between arguments [teleport, location] and [teleport, destination] with inputs: [0.1 -0.5 .9, 0 0 0]"),
            ("Server thread", "info", f"Preparing level \"{level_name}\""),
            ("Server thread", "info", "Preparing start region for dimension minecraft:overworld"),
            ("Worker-Main-2", "info", "Preparing spawn area: 0%"),
            ("Worker-Main-2", "info", "Preparing spawn area: 63%"),
            ("Server thread", "info", "Time elapsed: 2954 ms"),
            ("Server thread", "info", "Done (4.137s)! For help, type \"help\""),
        ]

    def stop_lines(self, level_name):
        return [
            ("Server thread", "info", "Stopping the server"),
            ("Server thread", "info", "Stopping server"),
            ("Server thread", "info", "Saving players"),
            ("Server thread", "info", "Saving worlds"),
            ("Server thread", "info", f"Saving chunks for level 'ServerLevel[{level_name}]'/minecraft:overworld"),
            ("Server thread", "info", f"Saving chunks for level 'ServerLevel[{level_name}]'/minecraft:the_nether"),
            ("Server thread", "info", f"Saving chunks for level 'ServerLevel[{level_name}]'/minecraft:the_end"),
            ("Server thread", "info", "ThreadedAnvilChunkStorage: All dimensions are saved"),
        ]

    def join_lines(self, name, ts):
        ip = f"203.0.113.{10 + (sum(map(ord, name)) % 200)}"
        self.mc(f"UUID of player {name} is {offline_uuid(name)}", thread="User Authenticator #1", ts=ts)
        self.mc(f"{name}[/{ip}:{50000 + self.rng.randint(0, 9999)}] logged in with entity id {self.rng.randint(100, 900)} at ({self.rng.uniform(-200, 200):.1f}, 64.0, {self.rng.uniform(-200, 200):.1f})", ts=ts)
        self.mc(f"{name} joined the game", ts=ts)

    def leave_lines(self, name, ts):
        self.mc(f"{name} lost connection: Disconnected", ts=ts)
        self.mc(f"{name} left the game", ts=ts)

    def seed_logs(self, now, scenario):
        rng = self.rng
        setup = self.active_setup()
        version, world = setup["resolved_version"], setup["level_name"]
        t = now - 3 * 3600 - 1200
        self._seed_backup_ts = now - 2 * 3600
        self.panel("Railpowered 2.0.0 starting", ts=t)
        self.panel("Dashboard listening on port 8080", ts=t)
        self.panel("Sleep proxy listening on port 25565. Joining wakes the server.", ts=t + 1)
        if scenario == "fresh":
            return
        self.panel("Couldn't reach Mojang to refresh the version list. Using the saved list.", "warn", ts=t + 2)
        t += 600
        self.panel("TheRealZoe tried to join. Waking the server.", ts=t)
        self.panel(f"Starting Minecraft {version} (Vanilla) with Java {java_for(version)}", ts=t)
        for i, (thread, level, msg) in enumerate(self.startup_lines(version, world)):
            self.mc(msg, level, thread, ts=t + i * 0.3)
        self.mc("Failed to load function oldtweaks:tick from datapack 'file/old_tweaks'", "error", "Worker-Main-3", ts=t + 4.6)
        t += 8
        online = []
        for name in ["TheRealZoe", "pixel_pete", "Marrowbone", "quietfox"]:
            t += rng.randint(20, 240)
            self.join_lines(name, t)
            online.append(name)
        while self.seq < 120:
            t += rng.randint(15, 45)
            roll = rng.random()
            if roll < 0.62:
                self.mc(f"<{rng.choice(online)}> {rng.choice(CHAT)}", ts=t)
            elif roll < 0.74:
                self.mc(f"{rng.choice(online)} has made the advancement {rng.choice(ADVANCEMENTS)}", ts=t)
            elif roll < 0.82:
                self.mc(f"Can't keep up! Is the server overloaded? Running {rng.randint(2001, 2900)}ms or {rng.randint(40, 58)} ticks behind", "warn", ts=t)
            elif roll < 0.9:
                self.mc(f"{rng.choice(online)} was slain by Zombie" if rng.random() < 0.5 else f"{rng.choice(online)} fell from a high place", ts=t)
            else:
                self.mc("Saving the game (this may take a moment!)", ts=t)
                self.mc("Saved the game", ts=t + 1)
        for name in list(online):
            t += rng.randint(30, 120)
            self.leave_lines(name, t)
        t += 600
        self.panel("Nobody online for 10 minutes. Saving and going to sleep.", ts=t)
        for i, (thread, level, msg) in enumerate(self.stop_lines(world)):
            self.mc(msg, level, thread, ts=t + i * 0.2)
        self.panel("Minecraft stopped (exit code 0). Sleep proxy listening on port 25565.", ts=t + 3)
        self._seed_backup_ts = t + 60
        self.panel(f"Backed up {world} as {world}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t + 60))}.tar.gz", ts=t + 60)

    # ------------------------------------------------------------------ helpers

    def active_setup(self):
        for s in self.setups:
            if s["id"] == self.active:
                return s
        return self.setups[0] if self.setups else None

    def find_setup(self, sid):
        for s in self.setups:
            if s["id"] == sid:
                return s
        raise ApiError(404, "That setup doesn't exist any more. It may have been deleted in another tab.", "not_found")

    def setup_summary(self, s):
        kind = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge", "modpack": "Modpack"}[s["type"]]
        return f"{kind} {s['resolved_version']} · world {s['level_name']}"

    def running(self):
        return self.phase in ("starting", "online")

    def task_running(self):
        return self.task is not None and self.task["state"] == "running"

    def schedule(self, delay, fn):
        gen = self.gen
        self.events.append((time.time() + delay, gen, fn))

    def tick(self):
        now = time.time()
        while True:
            due = [e for e in self.events if e[0] <= now]
            if not due:
                break
            first = min(due, key=lambda e: e[0])
            self.events.remove(first)
            if first[1] == self.gen:
                first[2]()
        if (self.phase == "online" and not self.players and self.idle_timeout > 0
                and self.empty_since and now - self.empty_since >= self.idle_timeout):
            self.panel(f"Nobody online for {self.idle_timeout // 60} minutes. Saving and going to sleep.")
            self.begin_stop()

    def cancel_events(self):
        self.gen += 1
        self.events = []

    # ------------------------------------------------------------------ phases

    def begin_start(self, then=None):
        setup = self.active_setup()
        version, world = setup["resolved_version"], setup["level_name"]
        self.phase = "starting"
        self.last_exit = None
        kind = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge", "modpack": "modpack " + str(setup.get("instance"))}[setup["type"]]
        self.panel(f"Starting Minecraft {version} ({kind}) with Java {java_for(version)}")
        lines = self.startup_lines(version, world)
        for i, (thread, level, msg) in enumerate(lines):
            self.schedule(0.3 + i * 0.32, lambda m=msg, lv=level, th=thread: self.mc(m, lv, th))
        self.schedule(0.4 + len(lines) * 0.32, lambda: self.go_online(then))

    def go_online(self, then=None, join=True):
        now = time.time()
        self.phase = "online"
        self.online_since = now
        self.empty_since = now
        setup = self.active_setup()
        w = self.worlds.setdefault(setup["level_name"], {"size_bytes": 24 * MiB, "last_version": None, "last_played": None})
        if not w["last_version"] or vcmp(setup["resolved_version"], w["last_version"]) > 0 or self._downgraded:
            w["last_version"] = setup["resolved_version"]
        self._downgraded = False
        w["last_played"] = now
        if join:
            for i, name in enumerate(self.friends):
                self.schedule(2.5 + i * 3.5, lambda n=name: self.player_join(n))
        if then:
            then()

    _downgraded = False

    def player_join(self, name):
        if self.phase != "online" or name in self.players or len(self.players) >= self.max_players:
            return
        self.join_lines(name, time.time())
        self.players.append(name)
        self.empty_since = None

    def player_leave(self, name):
        if name in self.players:
            self.players.remove(name)
            self.leave_lines(name, time.time())
        if not self.players and self.phase == "online":
            self.empty_since = time.time()

    def begin_stop(self, then=None, reason_line=None):
        setup = self.active_setup()
        self.phase = "stopping"
        for name in list(self.players):
            self.player_leave(name)
        self.players = []
        if reason_line:
            self.panel(reason_line)
        lines = self.stop_lines(setup["level_name"])
        for i, (thread, level, msg) in enumerate(lines):
            self.schedule(0.2 + i * 0.3, lambda m=msg, lv=level, th=thread: self.mc(m, lv, th))

        def done():
            self.phase = "sleeping"
            self.online_since = None
            self.empty_since = None
            self.restart_required = False
            self.panel("Minecraft stopped (exit code 0). Sleep proxy listening on port 25565.")
            if then:
                then()
        self.schedule(0.5 + len(lines) * 0.3, done)

    def force_phase(self, phase, hold=False, seeded=False):
        self.cancel_events()
        now = time.time()
        setup = self.active_setup()
        if phase == "online":
            if seeded:
                start = now - 47 * 60 - 12
                self.panel("TheRealZoe tried to join. Waking the server.", ts=start)
                self.panel(f"Starting Minecraft {setup['resolved_version']} (Vanilla) with Java {java_for(setup['resolved_version'])}", ts=start)
                for i, (thread, level, msg) in enumerate(self.startup_lines(setup["resolved_version"], setup["level_name"])):
                    self.mc(msg, level, thread, ts=start + i * 0.3)
                t = start + 30
                for name in self.friends:
                    t += self.rng.randint(30, 300)
                    self.join_lines(name, t)
                while self.friends and self.seq < 205:
                    t += self.rng.randint(20, 80)
                    if t > now - 5:
                        break
                    self.mc(f"<{self.rng.choice(self.friends or ['TheRealZoe'])}> {self.rng.choice(CHAT)}", ts=t)
                self.phase = "online"
                self.online_since = start + 6
                self.players = list(self.friends)
                self.empty_since = None if self.players else now
            else:
                self.phase = "online"
                self.online_since = now - 5
                self.players = list(self.friends)
                self.empty_since = None if self.players else now
                for name in self.players:
                    self.join_lines(name, now)
            self.last_exit = None
        elif phase == "sleeping":
            if self.running():
                for name in list(self.players):
                    self.leave_lines(name, now)
                self.panel("Minecraft stopped (exit code 0). Sleep proxy listening on port 25565.")
            self.phase = "sleeping"
            self.players = []
            self.online_since = None
            self.empty_since = None
        elif phase == "crashed":
            for name in list(self.players):
                self.mc(f"{name} lost connection: Server closed", ts=now)
            self.players = []
            self.mc("Encountered an unexpected exception", "error", ts=now)
            self.mc("java.lang.OutOfMemoryError: Java heap space", "error", ts=now)
            self.mc("\tat net.minecraft.world.level.chunk.storage.RegionFile.read(RegionFile.java:118)", "error", ts=now)
            self.mc("This crash report has been saved to: ./crash-reports/crash-2026-10-20_14.02.11-server.txt", "error", ts=now)
            self.panel("Minecraft exited unexpectedly (exit code 1). Sleep proxy listening on port 25565.", "error")
            self.phase = "crashed"
            self.online_since = None
            self.empty_since = None
            self.last_exit = {"code": 1, "at": int(now), "reason": "java.lang.OutOfMemoryError: Java heap space"}
        elif phase == "starting":
            self.phase = "sleeping"
            self.players = []
            self.online_since = None
            if hold:
                self.phase = "starting"
                self.panel(f"Starting Minecraft {setup['resolved_version']} with Java {java_for(setup['resolved_version'])}")
            else:
                self.begin_start()
        elif phase == "stopping":
            if hold:
                self.phase = "stopping"
                self.players = []
            else:
                if not self.running():
                    self.phase = "online"
                self.begin_stop()
        elif phase == "preparing":
            self.start_task("prepare", f"Downloading Minecraft {setup['resolved_version']}", 6 if not hold else 3600,
                            needs_stop=True)
        else:
            raise ApiError(400, "Unknown phase.", "invalid")

    # ------------------------------------------------------------------ tasks

    def start_task(self, kind, label, seconds, effect=None, fail=None, needs_stop=False, steps=None):
        if self.task_running():
            raise ApiError(409, f"Another task is running: “{self.task['label']}”. Wait for it to finish, then try again.", "busy")
        self.task_ids += 1
        now = time.time()
        task = {"id": f"t{self.task_ids}", "kind": kind, "label": label, "state": "running", "error": None,
                "started_at": int(now), "finished_at": None}
        self.task = task
        was_running = self.running()
        self.panel(f"{label}…")

        def enter_preparing():
            if task["state"] == "running" and needs_stop:
                self.phase = "preparing"

        if needs_stop:
            if was_running:
                self.begin_stop(then=enter_preparing, reason_line="Stopping Minecraft for this change.")
            elif self.phase in ("sleeping", "crashed"):
                self.phase = "preparing"
        for i, step in enumerate(steps or []):
            self.schedule(seconds * (i + 1) / (len(steps) + 1), lambda s=step: self.panel(s))

        def finish():
            task["finished_at"] = int(time.time())
            if fail:
                task["state"] = "failed"
                task["error"] = fail
                self.panel(f"{label} failed: {fail}", "error")
            else:
                task["state"] = "done"
                if effect:
                    effect()
                self.panel(f"{label}: done")
            if needs_stop:
                if self.phase == "preparing":
                    self.phase = "sleeping"
                if was_running:
                    self.begin_start()
        self.schedule(seconds, finish)
        return task

    def visible_task(self):
        t = self.task
        if not t:
            return None
        if t["state"] != "running" and time.time() - (t["finished_at"] or 0) > 300:
            return None
        return dict(t)

    # ------------------------------------------------------------------ status

    def status(self):
        now = time.time()
        setup = self.active_setup()
        until = None
        if self.phase == "online" and not self.players and self.idle_timeout > 0 and self.empty_since:
            until = max(0, int(self.idle_timeout - (now - self.empty_since)))
        public = None
        if setup:
            public = {k: setup[k] for k in ("id", "name", "type", "minecraft_version", "resolved_version", "level_name", "instance")}
        return {
            "phase": self.phase,
            "task": self.visible_task(),
            "players": list(self.players) if self.running() else [],
            "player_count": len(self.players) if self.running() else 0,
            "max_players": self.max_players,
            "uptime": int(now - self.online_since) if self.phase == "online" and self.online_since else None,
            "idle": {"timeout_seconds": self.idle_timeout, "seconds_until_sleep": until},
            "join_address": self.join_address,
            "setup": public,
            "java": java_for(setup["resolved_version"]) if setup else None,
            "online_mode": self.online_mode(),
            "whitelist_enabled": self.whitelist_enabled,
            "legacy": bool(setup and is_legacy(setup["resolved_version"])),
            "last_exit": self.last_exit,
            "product": "Railpowered",
            "app_version": "2.0.0",
            # Ahead of the backend: the upload limit (MAX_UPLOAD_MB), so the form can check sizes first.
            "max_upload_bytes": self.max_upload,
        }

    def online_mode(self):
        setup = self.active_setup()
        if setup and is_legacy(setup["resolved_version"]):
            return False
        return bool(self.properties.get("online-mode", True))

    # ------------------------------------------------------------------ API

    def handle_api(self, method, parts, query, body):
        route = "/".join(parts)
        if route == "status" and method == "GET":
            return 200, self.status()
        if route == "tasks/current" and method == "GET":
            return 200, {"task": self.visible_task()}
        if parts[0] == "server" and len(parts) == 2 and method == "POST":
            return self.server_action(parts[1])
        if route == "console" and method == "POST":
            return self.console(body)
        if route == "logs" and method == "GET":
            after = int((query.get("after") or ["0"])[0] or 0)
            lines = [line for line in self.logs if line["seq"] > after]
            return 200, {"lines": lines, "last_seq": self.seq}
        if route == "versions" and method == "GET":
            vanilla = VANILLA[:10] if self.versions_offline else VANILLA
            fabric = FABRIC[:6] if self.versions_offline else FABRIC
            return 200, {
                "vanilla": [{"id": v, "label": f"{v} (latest)" if v == LATEST else v} for v in vanilla],
                "fabric": [{"id": v, "label": f"{v} (latest)" if v == LATEST else v} for v in fabric],
                "forge": [{"id": v, "label": v} for v in FORGE],
                "latest": LATEST, "snapshots": False, "offline": self.versions_offline,
            }
        if parts[0] == "setups":
            return self.setups_api(method, parts[1:], body)
        if parts[0] == "modpacks" and route == "modpacks" and method == "POST":
            return self.modpack_url(body)
        if parts[0] == "worlds":
            return self.worlds_api(method, parts[1:], body)
        if parts[0] == "backups":
            return self.backups_api(method, parts[1:], body)
        if parts[0] == "players":
            return self.players_api(method, parts[1:], body)
        if route == "settings":
            return self.settings_api(method, body)
        raise ApiError(404, "Not found.", "not_found")

    def server_action(self, action):
        # Like controller.start/stop and api_restart: nothing starts or stops while a task runs.
        busy_msg = f"Wait for “{self.task['label']}” to finish first." if self.task_running() else None
        if action == "start":
            if busy_msg:
                raise ApiError(409, busy_msg, "busy")
            if self.running() or self.phase == "stopping":
                raise ApiError(409, "The server is already stopping." if self.phase == "stopping"
                               else "The server is already running.", "already_running")
            self.begin_start()
            return 200, {"ok": True}
        if action == "stop":
            if busy_msg:
                raise ApiError(409, busy_msg, "busy")
            if self.phase == "stopping":
                raise ApiError(409, "The server is already stopping.", "already_stopping")
            if not self.running():
                raise ApiError(409, "The server isn't running.", "not_running")
            self.begin_stop(reason_line="Stop requested from the dashboard. Saving the world first.")
            return 200, {"ok": True}
        if action == "restart":
            if busy_msg:
                raise ApiError(409, busy_msg, "busy")
            if not self.running():
                self.begin_start()
            else:
                self.begin_stop(then=self.begin_start, reason_line="Restart requested from the dashboard.")
            return 200, {"ok": True}
        if action == "save":
            # The real endpoint only sends "save-all flush"; Minecraft logs when it's done.
            if self.phase != "online":
                raise ApiError(409, "The server isn't online.", "not_online")
            self.mc("Saving the game (this may take a moment!)")
            self.schedule(0.6, lambda: self.mc("Saved the game"))
            return 200, {"ok": True}
        raise ApiError(404, "Not found.", "not_found")

    def console(self, body):
        command = body.get("command")
        if not isinstance(command, str):
            raise ApiError(400, "Type a command first.", "invalid")
        if "\n" in command or "\r" in command:
            raise ApiError(400, "Send one command at a time (no line breaks).", "invalid")
        command = command.strip()
        if command.startswith("/"):
            command = command[1:].lstrip()
        if not command:
            raise ApiError(400, "Type a command first.", "invalid")
        # Commands go to Minecraft's stdin as soon as the process exists, so "starting" works too.
        if not self.running():
            raise ApiError(409, "The server isn't running.", "not_running")
        self.panel(f"> {command}")
        if self.phase == "starting":
            return 200, {"ok": True}
        word, _, rest = command.partition(" ")
        if word == "list":
            names = ", ".join(self.players)
            self.mc(f"There are {len(self.players)} of a max of {self.max_players} players online: {names}")
        elif word == "say":
            self.mc(f"[Server] {rest}")
        elif word == "time":
            self.mc("Set the time to 1000")
        elif word == "weather":
            self.mc("Set the weather to clear")
        elif word in ("op", "deop") and NAME_RE.match(rest.strip() or "-"):
            # Like the real server, console op/deop changes the lists the Players page shows.
            name = rest.strip()
            known = next((n for n in self.ops if n.lower() == name.lower()), None)
            if word == "op" and not known:
                self.ops.append(name)
                self.mc(f"Made {name} a server operator")
            elif word == "deop" and known:
                self.ops.remove(known)
                self.mc(f"Made {known} no longer a server operator")
            else:
                self.mc("Nothing changed. The player already has that status", "warn")
        elif word == "whitelist" and rest.split(" ")[0] in ("add", "remove") and len(rest.split(" ")) == 2:
            action, name = rest.split(" ")
            known = next((n for n in self.whitelist if n.lower() == name.lower()), None)
            if action == "add" and not known:
                self.whitelist.append(name)
                self.mc(f"Added {name} to the whitelist")
            elif action == "remove" and known:
                self.whitelist.remove(known)
                self.mc(f"Removed {known} from the whitelist")
            else:
                self.mc("Nothing changed. The player is already in that state", "warn")
        elif word in ("whitelist", "kick", "gamemode", "tp", "give"):
            self.mc(f"Made the change: {command}")
        elif word == "help":
            for line in ["/advancement (grant|revoke)", "/ban <targets> [<reason>]", "/gamemode <gamemode> [<target>]", "/list", "/say <message>", "/time (add|query|set)", "/weather (clear|rain|thunder)"]:
                self.mc(line)
        else:
            self.mc("Unknown or incomplete command, see below for error", "warn")
            self.mc(f"{command}<--[HERE]", "warn")
        return 200, {"ok": True}

    # -------------------------------------------------------------- setups

    def setup_list(self):
        out = []
        for s in self.setups:
            item = dict(s)
            item["active"] = s["id"] == self.active
            item["summary"] = self.setup_summary(s)
            out.append(item)
        return out

    def require_idle(self):
        if self.task_running():
            raise ApiError(409, f"Wait for “{self.task['label']}” to finish first.", "busy")

    def check_name(self, value, what="World"):
        if not NAME_RE.match(str(value or "")):
            raise ApiError(400, f"{what} names use 1-32 letters, numbers, dots, dashes or underscores, "
                                "and start with a letter or number.", "invalid_name")
        return value

    def setups_api(self, method, rest, body):
        if not rest:
            if method == "GET":
                return 200, {"setups": self.setup_list()}
            if method == "POST":
                return self.create_setup(body)
            raise ApiError(405, "That action isn't supported here.", "method_not_allowed")
        sid = unquote(rest[0])
        setup = self.find_setup(sid)
        if len(rest) == 1 and method == "PATCH":
            name = body.get("name")
            if name is not None:
                name = str(name).strip()
                if not name or len(name) > 48:
                    raise ApiError(400, "Setup names are 1-48 characters.", "invalid_name")
            level = body.get("level_name")
            if level is not None:
                level = self.check_name(str(level).strip())
                if sid == self.active and self.running() and level != setup["level_name"]:
                    raise ApiError(409, "Stop the server before changing the world it runs.", "server_running")
                if level in self.worlds and level != setup["level_name"]:
                    self.check_downgrade(level, setup["resolved_version"], body.get("allow_downgrade"))
            self.require_idle()
            if name is not None:
                setup["name"] = name
            if level is not None:
                # Like installer.update_setup: a world that doesn't exist yet is created empty.
                self.worlds.setdefault(level, {"size_bytes": 0, "last_version": None, "last_played": None})
                setup["level_name"] = level
            return 200, {"ok": True, "setups": self.setup_list()}
        if len(rest) == 1 and method == "DELETE":
            if sid == self.active:
                raise ApiError(409, "Switch to another setup before deleting this one.", "active_setup")
            self.require_idle()
            self.setups = [s for s in self.setups if s["id"] != sid]
            return 200, {"ok": True}
        if len(rest) == 2 and rest[1] == "activate" and method == "POST":
            if sid == self.active:
                return 200, {"ok": True, "task": None, "already_active": True}
            self.check_downgrade(setup["level_name"], setup["resolved_version"], body.get("allow_downgrade"))
            allow = bool(body.get("allow_downgrade"))

            def effect():
                self.active = sid
                if allow:
                    self._downgraded = True
            steps = [f"Checking files for Minecraft {setup['resolved_version']}", "Writing server.properties"]
            if allow:
                steps.insert(0, f"Backing up {setup['level_name']} before opening it with an older version")
            task = self.start_task("switch", f"Switching to {setup['name']}", 7, effect=effect, needs_stop=True, steps=steps)
            return 202, {"task": task}
        raise ApiError(405, "That action isn't supported here.", "method_not_allowed")

    def downgrade_message(self, world, version):
        w = self.worlds.get(world)
        if not w or not w.get("last_version") or vcmp(version, w["last_version"]) >= 0:
            return None
        return f"“{world}” was last played on Minecraft {w['last_version']}. Opening it with {version} could damage it."

    def check_downgrade(self, world, version, allow):
        message = None if allow else self.downgrade_message(world, version)
        if message:
            w = self.worlds[world]
            raise ApiError(409, message, "world_downgrade", world=world, last_version=w["last_version"], version=version)

    def create_setup(self, body):
        stype = str(body.get("type") or "vanilla").lower()
        if stype == "modpack":
            raise ApiError(400, "Install modpacks from the Modpack section.", "invalid_type")
        if stype not in ("vanilla", "fabric", "forge"):
            raise ApiError(400, "Pick Vanilla, Fabric or Forge.", "invalid_type")
        version = str(body.get("minecraft_version") or "latest")
        allowed = {"vanilla": VANILLA, "fabric": FABRIC, "forge": FORGE}[stype]
        if version == "latest" and stype == "forge":
            raise ApiError(400, "Pick a specific Minecraft version for Forge.", "invalid_version")
        if version != "latest" and version not in allowed:
            raise ApiError(400, f"{stype.title()} isn't available for Minecraft {version}.", "invalid_version")
        name = str(body.get("name") or "").strip()
        if len(name) > 48:
            raise ApiError(400, "Setup names are at most 48 characters.", "invalid_name")
        level = self.check_name(str(body.get("level_name") or ""))
        create = body.get("create_world", True) is not False
        if create and level in self.worlds:
            raise ApiError(409, f"A world named {level} already exists.", "world_exists")
        if not create and level not in self.worlds:
            raise ApiError(404, f"There is no world named {level}.", "world_not_found")
        resolved = LATEST if version == "latest" else version
        if not create:
            self.check_downgrade(level, resolved, body.get("allow_downgrade"))
        kind = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge"}[stype]
        name = name or f"{kind} {resolved}"
        fail = None
        if "fail" in name.lower():
            fail = f"Couldn't download {kind} for Minecraft {resolved}: the download server answered 503. Try again in a few minutes."
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "setup"
        sid = base
        n = 2
        while any(s["id"] == sid for s in self.setups):
            sid = f"{base}-{n}"
            n += 1
        activate = body.get("activate", True) is not False

        def effect():
            if create:
                self.worlds[level] = {"size_bytes": 0, "last_version": None, "last_played": None}
            self.setups.append(self._setup(sid, name, stype, version, level))
            if activate:
                self.active = sid
        steps = [f"Downloading Minecraft {resolved} server", "Checking the download"]
        if stype == "fabric":
            steps.append("Installing the Fabric loader")
        if stype == "forge":
            steps += ["Running the Forge installer", "Installing libraries"]
        shown = "the latest version" if version == "latest" else version
        task = self.start_task("apply", f"Installing {kind} {shown}", 9 if stype != "forge" else 14,
                               effect=effect, fail=fail, needs_stop=activate, steps=steps)
        return 202, {"task": task}

    def modpack_url(self, body):
        url = str(body.get("url") or "").strip()
        if not url:
            raise ApiError(400, "Paste a link to a .mrpack or .zip file.", "invalid_url")
        if not url.lower().startswith("https://") or len(url) > 2048 or re.search(r"\s", url):
            raise ApiError(400, "Use an https:// link to a .mrpack or .zip file.", "invalid_url")
        name = self.check_name(str(body.get("name") or "").strip(), "Modpack")
        level = str(body.get("level_name") or "").strip() or name
        fail = None
        if "fail" in url.lower():
            fail = "Couldn't download the modpack: the link answered 404 Not Found. Check that it points to the .mrpack or .zip file itself."
        return self.start_modpack(name, level, fail, body.get("allow_downgrade"))

    def start_modpack(self, name, level, fail, allow_downgrade):
        self.check_name(level)
        self.require_idle()
        version = "1.21.1"
        # The pack's Minecraft version is only known once it has been read, so,
        # like the real installer, a downgrade fails the task instead of the request.
        if not fail and level in self.worlds and not allow_downgrade:
            fail = self.downgrade_message(level, version)

        def effect():
            self.worlds.setdefault(level, {"size_bytes": 0, "last_version": None, "last_played": None})
            existing = [s for s in self.setups if s.get("instance") == name]
            if existing:
                existing[0].update({"level_name": level})
                self.active = existing[0]["id"]
            else:
                sid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "modpack"
                self.setups.append(self._setup(sid, name, "modpack", version, level, instance=name))
                self.active = sid
        steps = ["Reading the pack manifest", "Downloading 142 mods", "Installing Fabric loader 0.17.2", "Copying config files"]
        task = self.start_task("modpack", f"Installing modpack {name}", 12, effect=effect, fail=fail, needs_stop=True, steps=steps)
        return 202, {"task": task}

    # -------------------------------------------------------------- worlds

    def world_list(self):
        active = self.active_setup()
        out = []
        for name, w in self.worlds.items():
            out.append({
                "name": name, "size_bytes": w["size_bytes"],
                "active": bool(active and active["level_name"] == name),
                "last_version": w["last_version"],
                "last_played": iso(w["last_played"]) if w["last_played"] else None,
                "used_by": [s["name"] for s in self.setups if s["level_name"] == name],
            })
        return out

    def worlds_api(self, method, rest, body):
        if not rest and method == "GET":
            return 200, {"worlds": self.world_list()}
        if not rest and method == "POST":
            name = self.check_name(str(body.get("name") or "").strip())
            if name in self.worlds:
                raise ApiError(409, f"A world named {name} already exists.", "world_exists")
            self.worlds[name] = {"size_bytes": 0, "last_version": None, "last_played": None}
            return 200, {"ok": True, "worlds": self.world_list()}
        if len(rest) == 1 and method == "DELETE":
            name = self.check_name(unquote(rest[0]))
            if name not in self.worlds:
                raise ApiError(404, f"There is no world named {name}.", "world_not_found")
            active = self.active_setup()
            if active and active["level_name"] == name:
                raise ApiError(409, "This is the active setup's world. Switch setups before deleting it.", "active_world")
            self.require_idle()
            removed = [s["name"] for s in self.setups if s["level_name"] == name]
            self.setups = [s for s in self.setups if s["level_name"] != name]
            del self.worlds[name]
            return 200, {"ok": True, "removed_setups": removed}
        raise ApiError(405, "That action isn't supported here.", "method_not_allowed")

    # -------------------------------------------------------------- backups

    def disk(self):
        total = 10 * GiB
        used = int(1.4 * GiB) + sum(w["size_bytes"] for w in self.worlds.values()) + sum(b["size_bytes"] for b in self.backups)
        return {"used_bytes": used, "total_bytes": total, "free_bytes": max(0, total - used)}

    def backups_api(self, method, rest, body):
        if not rest and method == "GET":
            out = [{k: v for k, v in b.items() if not k.startswith("_")} for b in self.backups]
            return 200, {"backups": out, "disk": self.disk()}
        if not rest and method == "POST":
            world = self.check_name(str(body.get("world") or (self.active_setup() or {}).get("level_name") or "world"))
            if world not in self.worlds:
                raise ApiError(404, f"There is no world named {world}.", "world_not_found")
            self.require_idle()
            running = self.running() and self.active_setup()["level_name"] == world
            steps = (["Pausing autosave (save-off)", "Flushing the world to disk (save-all flush)"] if running else []) + ["Compressing the world"]
            if running:
                steps.append("Resuming autosave (save-on)")
            w = self.worlds[world]

            def effect():
                self._add_backup(world, time.time(), max(MiB, int(w["size_bytes"] * 0.62)), w["last_version"] or LATEST)
            task = self.start_task("backup", f"Backing up {world}", 6, effect=effect, steps=steps)
            return 202, {"task": task}
        name = unquote(rest[0]) if rest else ""
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz$", name):
            raise ApiError(400, "That isn't a backup name.", "invalid_name")
        backup = next((b for b in self.backups if b["name"] == name), None)
        if not backup:
            raise ApiError(404, "That backup doesn't exist any more.", "backup_not_found")
        if len(rest) == 1 and method == "DELETE":
            self.require_idle()
            self.backups.remove(backup)
            return 200, {"ok": True}
        if len(rest) == 2 and rest[1] == "restore" and method == "POST":
            target = str(body.get("target") or "").strip() or None
            if target:
                self.check_name(target)
            effective = target or backup["world"]
            if effective is None:
                # Like the panel: an unreadable archive's file name still says which world it is.
                named = re.match(r"^(.+?)-(?:pre-[a-z]+-)?\d{8}-\d{6}(?:-\d+)?\.tar\.gz$", name)
                effective = named.group(1) if named else None
            active = self.active_setup()
            if effective and self.phase in ("starting", "online", "stopping") and active and active["level_name"] == effective:
                raise ApiError(409, f"Stop the server before restoring {effective}.", "server_running")
            self.require_idle()
            fail = None
            if backup.get("damaged"):
                fail = "That backup can't be read: the archive is damaged or incomplete. Download it to look inside, or pick another backup."
            # An existing target is replaced, after a safety copy (installer.restore_backup).
            replacing = bool(effective) and effective in self.worlds

            def effect():
                if replacing:
                    cur = self.worlds[effective]
                    self._add_backup(effective, time.time(), max(MiB, int(cur["size_bytes"] * 0.62)), cur["last_version"] or LATEST, safety=True)
                self.worlds[effective] = {"size_bytes": int(backup["size_bytes"] / 0.62), "last_version": backup["minecraft_version"],
                                          "last_played": backup["_ts"]}
            steps = ([f"Saving the current {effective} as a safety backup"] if replacing else []) + ["Unpacking the backup"]
            task = self.start_task("restore", f"Restoring {effective or 'world'} from backup", 7, effect=effect, fail=fail, steps=steps)
            return 202, {"task": task}
        raise ApiError(405, "That action isn't supported here.", "method_not_allowed")

    # -------------------------------------------------------------- players

    def players_payload(self):
        return {
            "online": list(self.players) if self.running() else [],
            "ops": [{"name": n, "uuid": offline_uuid(n)} for n in self.ops],
            "whitelist": [{"name": n, "uuid": offline_uuid(n)} for n in self.whitelist],
            "whitelist_enabled": self.whitelist_enabled,
            "online_mode": self.online_mode(),
        }

    def players_api(self, method, rest, body):
        if not rest and method == "GET":
            return 200, self.players_payload()
        if rest == ["whitelist", "enabled"] and method == "PUT":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise ApiError(400, "enabled must be true or false.", "invalid")
            self.whitelist_enabled = enabled
            if self.running():
                self.panel(f"> whitelist {'on' if enabled else 'off'}")
                self.mc("Whitelist is now turned " + ("on" if enabled else "off"))
            return 200, {"ok": True, **self.players_payload()}
        if rest and rest[0] in ("ops", "whitelist"):
            kind = rest[0]
            target = self.ops if kind == "ops" else self.whitelist
            if len(rest) == 1 and method == "POST":
                name = str(body.get("name") or "").strip()
                if not PLAYER_RE.match(name):
                    raise ApiError(400, "Player names are 1-16 letters, numbers or underscores.", "invalid_player")
                if "nobody" in name.lower():
                    raise ApiError(404, f"Mojang has no player called {name}. Check the spelling.", "player_not_found")
                # Adding someone who is already there is a no-op, like installer.add_player.
                if not any(n.lower() == name.lower() for n in target):
                    target.append(name)
                    if self.running():
                        self.panel(f"> {'op' if kind == 'ops' else 'whitelist add'} {name}")
                return 200, {"ok": True, **self.players_payload()}
            if len(rest) == 2 and method == "DELETE":
                name = unquote(rest[1])
                if not PLAYER_RE.match(name):
                    raise ApiError(400, "Player names are 1-16 letters, numbers or underscores.", "invalid_player")
                match = next((n for n in target if n.lower() == name.lower()), None)
                if match:
                    target.remove(match)
                    if self.running():
                        self.panel(f"> {'deop' if kind == 'ops' else 'whitelist remove'} {match}")
                return 200, {"ok": True, **self.players_payload()}
        raise ApiError(405, "That action isn't supported here.", "method_not_allowed")

    # -------------------------------------------------------------- settings

    def settings_view(self):
        """Like installer.get_settings: an unknown on-disk enum value joins the
        options so the select can show it. Pre-1.7 versions report online-mode
        off even though their schema leaves it out (the dashboard must not send it back)."""
        setup = self.active_setup()
        version = setup["resolved_version"] if setup else LATEST
        schema = settings_schema(version)
        values = {}
        for key, item in schema.items():
            values[key] = self.properties.get(key)
            if item["type"] == "enum" and values[key] not in item["options"]:
                item["options"] = item["options"] + [values[key]]
        if is_legacy(version):
            values["online-mode"] = False
        return version, schema, values

    def settings_api(self, method, body):
        if method == "GET":
            _, schema, values = self.settings_view()
            return 200, {
                "properties": values,
                "railpowered": {"idle_timeout_minutes": self.idle_timeout // 60, "auto_start": self.auto_start},
                "restart_required": self.restart_required and self.running(),
                "schema": schema,
            }
        if method == "PUT":
            props = body.get("properties")
            rp = body.get("railpowered")
            props = {} if props is None else props
            rp = {} if rp is None else rp
            if not isinstance(props, dict) or not isinstance(rp, dict):
                raise ApiError(400, "properties and railpowered must be objects.", "invalid")
            setup = self.active_setup()
            version = setup["resolved_version"] if setup else LATEST
            schema = settings_schema(version)
            clean = {}
            for key, value in props.items():
                sch = schema.get(key)
                if not sch:
                    raise ApiError(400, f"'{str(key)[:40]}' can't be changed here on Minecraft {version}.", "invalid_setting")
                label = sch["label"]
                if sch["type"] == "int":
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ApiError(400, f"{label} must be a whole number.", "invalid_setting")
                    if value < sch["min"] or value > sch["max"]:
                        raise ApiError(400, f"{label} must be between {sch['min']} and {sch['max']}.", "invalid_setting")
                elif sch["type"] == "bool":
                    if not isinstance(value, bool):
                        raise ApiError(400, f"{label} must be on or off.", "invalid_setting")
                elif sch["type"] == "enum":
                    # The value already on disk stays allowed (a modpack's own level-type, say).
                    if value not in sch["options"] and value != self.properties.get(key):
                        raise ApiError(400, f"{label} must be one of: {', '.join(sch['options'])}.", "invalid_setting")
                else:
                    value = str(value)
                    if "\n" in value or "\r" in value:
                        raise ApiError(400, f"{label} must be a single line.", "invalid_setting")
                    if len(value) > sch.get("max", 256):
                        raise ApiError(400, f"{label} can be at most {sch['max']} characters.", "invalid_setting")
                clean[key] = value
            rp_clean = {}
            for key, value in rp.items():
                if key == "idle_timeout_minutes":
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
                        raise ApiError(400, "Sleep timer must be between 0 (never sleep) and 1440 minutes.", "invalid_setting")
                elif key == "auto_start":
                    if not isinstance(value, bool):
                        raise ApiError(400, "Start on deploy must be on or off.", "invalid_setting")
                else:
                    raise ApiError(400, f"Unknown Railpowered setting '{str(key)[:40]}'.", "invalid_setting")
                rp_clean[key] = value
            changed = any(self.properties.get(k) != v for k, v in clean.items())
            self.properties.update(clean)
            self.max_players = self.properties["max-players"]
            if clean:
                self.panel("Server settings saved.")
            if "idle_timeout_minutes" in rp_clean:
                self.idle_timeout = rp_clean["idle_timeout_minutes"] * 60
                if self.phase == "online" and not self.players:
                    self.empty_since = time.time()
            if "auto_start" in rp_clean:
                self.auto_start = rp_clean["auto_start"]
            if rp_clean:
                self.panel("Railpowered settings saved.")
            if changed and self.running():
                self.restart_required = True
            return 200, {"ok": True, "restart_required": self.restart_required and self.running()}
        raise ApiError(405, "That action isn't supported here.", "method_not_allowed")

    # -------------------------------------------------------------- dev hooks

    def hook(self, name, q):
        def arg(key, default=None):
            return (q.get(key) or [default])[0]
        if name == "phase":
            self.force_phase(arg("set", "online"), hold=arg("hold") == "1")
        elif name == "players":
            names = [n for n in (arg("names", "") or "").split(",") if n]
            if self.phase != "online":
                raise ApiError(409, "Players can only join while the server is online.", "not_running")
            for n in list(self.players):
                if n not in names:
                    self.player_leave(n)
            for n in names:
                self.player_join(n)
        elif name == "idle":
            left = int(arg("left", "90"))
            for n in list(self.players):
                self.player_leave(n)
            self.empty_since = time.time() - max(0, self.idle_timeout - left)
        elif name == "task":
            kind = arg("kind", "backup")
            seconds = float(arg("seconds", "8"))
            label = arg("label") or {"backup": "Backing up survival", "prepare": "Downloading Minecraft 26.3",
                                     "restore": "Restoring survival", "apply": "Installing Fabric 26.3"}.get(kind, "Working")
            fail = arg("error", "The download stopped halfway: connection reset by peer. Try again.") if arg("fail") == "1" else None
            self.start_task(kind, label, seconds, fail=fail, needs_stop=kind in ("prepare", "apply", "switch", "modpack"))
        elif name == "fail":
            self.failures[arg("path", "/api/status")] = [int(arg("status", "502")), int(arg("times", "3"))]
        elif name == "expire":
            self.sessions.clear()
        elif name == "address":
            self.join_address = arg("value", "")
        elif name == "offline":
            self.versions_offline = arg("set", "1") == "1"
        elif name == "upload-limit":
            self.max_upload = int(float(arg("mb", "1024")) * MiB)
        elif name == "reset":
            self.reset(arg("phase", "online"), arg("scenario", "default"))
        elif name == "finish":
            for e in list(self.events):
                self.events.remove(e)
                if e[1] == self.gen:
                    e[2]()
        else:
            return None
        return {"ok": True, "phase": self.phase, "task": self.visible_task()}


HOOKS_HELP = {
    "/__mock/phase?set=online|sleeping|starting|stopping|preparing|crashed[&hold=1]": "jump to a phase; hold=1 freezes transitional phases",
    "/__mock/players?names=a,b": "set who is online (server must be online)",
    "/__mock/idle?left=90": "everyone leaves; the sleep countdown shows this many seconds",
    "/__mock/task?kind=backup&seconds=8[&fail=1&error=...][&label=...]": "run a fake task",
    "/__mock/fail?path=/api/status&status=502&times=3": "make the next N requests to a path fail",
    "/__mock/expire": "end every session (the next API call gets 401)",
    "/__mock/address?value=host:port": "change the join address (empty for none)",
    "/__mock/offline?set=1|0": "answer /api/versions as if Mojang can't be reached",
    "/__mock/upload-limit?mb=256": "change the modpack upload limit (MAX_UPLOAD_MB)",
    "/__mock/reset?phase=online&scenario=default|fresh|legacy": "start over; fresh = a brand new install, legacy = Minecraft 1.6.4",
    "/__mock/finish": "run every scheduled event now",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "RailpoweredMock/2.0"
    protocol_version = "HTTP/1.1"

    @property
    def mock(self):
        return self.server.mock

    def log_message(self, fmt, *args):
        if self.mock.args.quiet:
            return
        line = fmt % args
        if "/api/status" in line or "/api/logs" in line:
            return
        print(f"{self.address_string()} {line}", flush=True)

    # ---------------------------------------------------------------- responses

    def _security_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")

    def send_bytes(self, status, data, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status, payload, extra=None):
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self.send_bytes(status, json.dumps(payload).encode(), "application/json", headers)

    def redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self._security_headers()
        self.end_headers()

    def send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext not in CONTENT_TYPES or not os.path.isfile(path):
            self.send_json(404, {"error": "Not found.", "code": "not_found"})
            return
        with open(path, "rb") as fh:
            data = fh.read()
        # no-store: edits to web/ show up on a plain reload while developing.
        self.send_bytes(200, data, CONTENT_TYPES[ext], {"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- auth

    def authed(self):
        if self.mock.args.no_auth:
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "rp_session" and v in self.mock.sessions:
                return True
        return False

    def read_body(self, limit=1 * MiB):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        n = int(length)
        if n > limit:
            raise ApiError(413, "That request is too large.", "too_large")
        return self.rfile.read(n)

    # ---------------------------------------------------------------- dispatch

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        url = urlsplit(self.path)
        path = url.path
        query = parse_qs(url.query, keep_blank_values=True)
        web = self.mock.args.web_dir
        try:
            if path in ("/__mock", "/__mock/"):
                return self.send_json(200, {"hooks": HOOKS_HELP})
            if path.startswith("/__mock/"):
                with self.mock.lock:
                    self.mock.tick()
                    result = self.mock.hook(path[len("/__mock/"):], query)
                if result is None:
                    return self.send_json(404, {"error": "Unknown hook.", "hooks": HOOKS_HELP})
                return self.send_json(200, result)
            if path == "/health":
                return self.send_json(200, {"status": "ok"})
            if path.startswith("/static/"):
                rest = path[len("/static/"):]
                parts = rest.split("/")
                if len(parts) == 1 and STATIC_RE.match(parts[0]):
                    return self.send_file(os.path.join(web, parts[0]))
                if len(parts) == 2 and parts[0] == "assets" and STATIC_RE.match(parts[1]):
                    return self.send_file(os.path.join(web, "assets", parts[1]))
                return self.send_json(404, {"error": "Not found.", "code": "not_found"})
            if path == "/favicon.ico":
                return self.send_file(os.path.join(web, "assets", "favicon.svg"))
            if path == "/" and method == "GET":
                if not self.authed():
                    return self.redirect("/login")
                return self.send_file(os.path.join(web, "index.html"))
            if path == "/login" and method == "GET":
                if self.authed():
                    return self.redirect("/")
                return self.login_page((query.get("error") or [""])[0])
            if path == "/login" and method == "POST":
                return self.login()
            if path == "/logout" and method == "POST":
                return self.logout()
            if path == "/api/logout" and method == "GET":
                # Kept for old bookmarks; a GET never signs anyone out (CSRF-safe).
                return self.redirect("/")
            if path.startswith("/api/"):
                return self.api(method, path, query)
            self.send_json(404, {"error": "Not found.", "code": "not_found"})
        except ApiError as err:
            if err.status == 413 or path == "/api/modpacks/upload":
                self.close_connection = True
            self.send_json(err.status, err.payload())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def login_page(self, error):
        with open(os.path.join(self.mock.args.web_dir, "login.html"), "rb") as fh:
            page = fh.read().decode("utf-8")
        # Same substitution as web.login_page(): every {{error}}, escaped.
        page = page.replace("{{error}}", html.escape(LOGIN_ERRORS.get(error, "")))
        self.send_bytes(200, page.encode("utf-8"), CONTENT_TYPES[".html"], {"Cache-Control": "no-store"})

    def login(self):
        form = parse_qs(self.read_body(64 * 1024).decode("utf-8", "replace"))
        key = (form.get("key") or [""])[0]
        if self.mock.failed_logins >= 5:
            return self.redirect("/login?error=rate")
        if key != self.mock.args.key:
            self.mock.failed_logins += 1
            return self.redirect("/login?error=rate" if self.mock.failed_logins >= 5 else "/login?error=1")
        token = uuid.uuid4().hex
        self.mock.sessions.add(token)
        self.mock.failed_logins = 0
        return self.redirect("/", cookie=f"rp_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000")

    def logout(self):
        self.read_body(64 * 1024)
        # Like panel._logout: the X-Railpowered header, or a browser-sent Origin
        # that matches this host. A form field alone proves nothing.
        origin = self.headers.get("Origin")
        same = None if not origin else urlsplit(origin).netloc == (self.headers.get("Host") or "")
        if same is False or (same is None and self.headers.get("X-Railpowered") != "1"):
            raise ApiError(403, "Log out from the dashboard.", "csrf")
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "rp_session":
                self.mock.sessions.discard(v)
        return self.redirect("/login", cookie="rp_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")

    def api(self, method, path, query):
        is_upload = path == "/api/modpacks/upload"
        # Read the body up front so an early error never leaves it in the socket.
        raw = b"" if is_upload else self.read_body()
        if is_upload:
            self.close_connection = True
        if not self.authed():
            raise ApiError(401, "Log in to continue.", "unauthorized")
        if method != "GET" and self.headers.get("X-Railpowered") != "1":
            raise ApiError(403, "This request is missing the dashboard header.", "csrf")
        with self.mock.lock:
            failure = self.mock.failures.get(path)
            if failure and failure[1] > 0:
                failure[1] -= 1
                raise ApiError(failure[0], "The mock was told to fail this request.", "mock_failure")
        if method != "GET" and self.mock.args.latency:
            time.sleep(self.mock.args.latency / 1000)
        parts = [p for p in path[len("/api/"):].split("/") if p]
        if not parts:
            raise ApiError(404, "Not found.", "not_found")

        if parts[0] == "backups" and len(parts) == 3 and parts[2] == "download" and method == "GET":
            name = unquote(parts[1])
            with self.mock.lock:
                exists = any(b["name"] == name for b in self.mock.backups)
            if not exists:
                raise ApiError(404, "That backup doesn't exist any more.", "backup_not_found")
            data = gzip.compress(f"Railpowered mock backup {name}\n".encode())
            return self.send_bytes(200, data, "application/gzip", {
                "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

        if parts == ["modpacks", "upload"] and method == "POST":
            return self.upload(query)

        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                raise ApiError(400, "The request body isn't valid JSON.", "bad_json") from None
            if not isinstance(body, dict):
                raise ApiError(400, "The request body must be a JSON object.", "bad_json")
        with self.mock.lock:
            self.mock.tick()
            status, payload = self.mock.handle_api(method, parts, query, body)
        self.send_json(status, payload)

    def upload(self, query):
        name = (query.get("name") or [""])[0].strip()
        level = (query.get("level_name") or [""])[0].strip() or name
        allow = (query.get("allow_downgrade") or [""])[0].strip().lower() in ("1", "true", "yes")
        # Checked before the body is read, like the panel; the connection is then closed.
        with self.mock.lock:
            self.mock.check_name(name, "Modpack")
            self.mock.check_name(level)
            self.mock.require_idle()
            limit = self.mock.max_upload
            free = self.mock.disk()["free_bytes"]
        length = self.headers.get("Content-Length")
        if not length:
            raise ApiError(411, "Send the body with a Content-Length.", "length_required")
        n = int(length)
        if n > limit:
            raise ApiError(413, f"That request is too large (limit {limit // MiB} MB).", "too_large")
        if free < n * 3 + UPLOAD_HEADROOM:
            raise ApiError(507, "There isn't enough free space on the volume for this pack.", "disk_full")
        # Read slowly so the progress bar has something to show.
        rate = max(1, self.mock.args.upload_kbps) * 1024
        left = n
        while left > 0:
            chunk = self.rfile.read(min(64 * 1024, left))
            if not chunk:
                break
            left -= len(chunk)
            time.sleep(len(chunk) / rate)
        with self.mock.lock:
            self.mock.tick()
            status, payload = self.mock.start_modpack(name, level, None, allow)
        self.send_json(status, payload)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_web = os.path.normpath(os.path.join(here, "..", "web"))
    parser = argparse.ArgumentParser(description="Railpowered UI development mock (not for production).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--web-dir", default=default_web, help="folder holding index.html, app.js and friends")
    parser.add_argument("--key", default="admin", help="dashboard key accepted by the sign-in page")
    parser.add_argument("--no-auth", action="store_true", help="skip the sign-in page")
    parser.add_argument("--phase", default="online", choices=["online", "sleeping", "crashed", "starting"])
    parser.add_argument("--latency", type=int, default=250, help="extra milliseconds on every non-GET API call")
    parser.add_argument("--upload-kbps", type=int, default=4096, help="how fast uploads are read, in KiB/s")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    args.web_dir = os.path.abspath(args.web_dir)
    if not os.path.isfile(os.path.join(args.web_dir, "index.html")):
        parser.error(f"no index.html in {args.web_dir}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.mock = Mock(args)
    print(f"Railpowered mock on http://{args.host}:{args.port}/  (web: {args.web_dir})", flush=True)
    print("Sign-in is off." if args.no_auth else f"Sign in with the key: {args.key}", flush=True)
    print(f"Dev hooks: http://{args.host}:{args.port}/__mock/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
