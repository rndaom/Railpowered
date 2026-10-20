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
  * 401: open /__mock/expire, then wait for the next poll.
"""

import argparse
import gzip
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
    "motd": {"type": "string", "label": "Server list message",
             "help": "Shown under the server name in your friends' server list."},
    "difficulty": {"type": "enum", "label": "Difficulty",
                   "options": ["peaceful", "easy", "normal", "hard"]},
    "gamemode": {"type": "enum", "label": "Game mode for new players",
                 "options": ["survival", "creative", "adventure", "spectator"],
                 "help": "Players who have joined before keep the mode they had."},
    "hardcore": {"type": "bool", "label": "Hardcore",
                 "help": "One life each: players become spectators when they die."},
    "pvp": {"type": "bool", "label": "Players can hurt each other"},
    "allow-flight": {"type": "bool", "label": "Allow flying",
                     "help": "Stops players being kicked for flying. Some mods need this."},
    "allow-nether": {"type": "bool", "label": "Allow the Nether"},
    "enable-command-block": {"type": "bool", "label": "Command blocks"},
    "spawn-protection": {"type": "int", "min": 0, "max": 64, "label": "Spawn protection (blocks)",
                         "help": "Only operators can build this close to spawn. 0 turns it off."},
    "max-players": {"type": "int", "min": 1, "max": 100, "label": "Maximum players"},
    "online-mode": {"type": "bool", "label": "Check Minecraft accounts",
                    "help": "Stops people joining with someone else's name. Leave this on unless you know you need it off."},
    "view-distance": {"type": "int", "min": 3, "max": 32, "label": "View distance (chunks)",
                      "help": "How far players can see. Lower numbers run faster on small servers."},
    "simulation-distance": {"type": "int", "min": 3, "max": 32, "label": "Simulation distance (chunks)",
                            "help": "How far from players crops grow and mobs move."},
    "level-seed": {"type": "string", "label": "World seed",
                   "help": "Only used when a new world is created. Leave it empty for a random world."},
    "level-type": {"type": "enum", "label": "World type",
                   "options": [
                       {"value": "minecraft:normal", "label": "Normal"},
                       {"value": "minecraft:flat", "label": "Superflat"},
                       {"value": "minecraft:large_biomes", "label": "Large biomes"},
                       {"value": "minecraft:amplified", "label": "Amplified"},
                   ],
                   "help": "Only used when a new world is created."},
}

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
        self.task_ids = 0
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
        self.join_address = "shuttle.proxy.rlwy.net:28415"
        self.friends = ["TheRealZoe", "pixel_pete", "Marrowbone"]
        self.properties = {
            "motd": "Friends only. Be nice!", "difficulty": "normal", "gamemode": "survival",
            "hardcore": False, "pvp": True, "max-players": 10, "view-distance": 10,
            "simulation-distance": 10, "spawn-protection": 16, "allow-flight": False,
            "allow-nether": True, "level-seed": "", "level-type": "minecraft:normal",
            "online-mode": True, "enable-command-block": False,
        }
        if scenario == "fresh":
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
        if scenario != "fresh":
            day = 86400
            for world, ts, size, version, safety in [
                ("survival", self._seed_backup_ts, 268 * MiB, "26.3", False),
                ("survival", now - 2 * day - 5000, 251 * MiB, "26.3", False),
                ("survival", now - 8 * day - 900, 240 * MiB, "26.2.1", True),
                ("cozy-farm", now - 12 * day - 3000, 410 * MiB, "1.21.1", False),
                ("old-world-2023", now - 280 * day, 820 * MiB, "1.20.1", False),
            ]:
                self._add_backup(world, ts, size, version, safety)
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
                             "created_at": iso(ts), "minecraft_version": version, "_ts": ts})
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
            raise ApiError(400, "Unknown phase.", "bad_request")

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
            "online_mode": self.properties["online-mode"],
            "whitelist_enabled": self.whitelist_enabled,
            "legacy": False,
            "last_exit": self.last_exit,
            "product": "Railpowered",
            "app_version": "2.0.0",
        }

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
            return 200, {
                "vanilla": [{"id": v, "label": f"{v} (latest)" if v == LATEST else v} for v in VANILLA],
                "fabric": [{"id": v, "label": f"{v} (latest)" if v == LATEST else v} for v in FABRIC],
                "forge": [{"id": v, "label": v} for v in FORGE],
                "latest": LATEST, "snapshots": False,
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
        raise ApiError(404, "There's nothing at that address.", "not_found")

    def server_action(self, action):
        busy_msg = None
        if self.task_running() and self.phase == "preparing":
            busy_msg = f"Wait for “{self.task['label']}” to finish first."
        if action == "start":
            if busy_msg:
                raise ApiError(409, busy_msg, "busy")
            if self.running():
                raise ApiError(409, "The server is already running.", "already_running")
            if self.phase == "stopping":
                raise ApiError(409, "The server is still stopping. Try again in a few seconds.", "busy")
            self.begin_start()
            return 200, {"ok": True}
        if action == "stop":
            if busy_msg:
                raise ApiError(409, busy_msg, "busy")
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
            if self.phase != "online":
                raise ApiError(409, "The world can only be saved while the server is online.", "not_running")
            self.mc("Saving the game (this may take a moment!)")
            self.schedule(0.6, lambda: self.mc("Saved the game"))
            return 200, {"ok": True}
        raise ApiError(404, "Unknown server action.", "not_found")

    def console(self, body):
        command = body.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ApiError(400, "Type a command first.", "bad_request")
        if "\n" in command or "\r" in command:
            raise ApiError(400, "Send one command at a time. Line breaks aren't allowed.", "bad_request")
        if not self.running():
            raise ApiError(409, "Minecraft isn't running. Start the server to send commands.", "not_running")
        if self.phase == "starting":
            raise ApiError(409, "Minecraft is still starting. Try again when it's online.", "not_running")
        command = command.strip()
        self.panel(f"> {command}")
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

    def setups_api(self, method, rest, body):
        if not rest:
            if method == "GET":
                out = []
                for s in self.setups:
                    item = dict(s)
                    item["active"] = s["id"] == self.active
                    item["summary"] = self.setup_summary(s)
                    out.append(item)
                return 200, {"setups": out}
            if method == "POST":
                return self.create_setup(body)
        sid = unquote(rest[0])
        setup = self.find_setup(sid)
        if len(rest) == 1 and method == "PATCH":
            if "name" in body:
                name = str(body.get("name") or "").strip()
                if not name or len(name) > 48:
                    raise ApiError(400, "Setup names are 1-48 characters.", "invalid_name")
                setup["name"] = name
            if "level_name" in body:
                level = str(body.get("level_name") or "")
                if not NAME_RE.match(level):
                    raise ApiError(400, "World names: " + NAME_RULE, "bad_request")
                if sid == self.active and self.running():
                    raise ApiError(409, "Stop the server before changing the world of the active setup.", "conflict")
                if level not in self.worlds:
                    raise ApiError(400, f"There's no world called “{level}”.", "bad_request")
                setup["level_name"] = level
            return 200, {"ok": True}
        if len(rest) == 1 and method == "DELETE":
            if sid == self.active:
                raise ApiError(409, "That's the active setup. Switch to another one before deleting it.", "active_setup")
            self.setups = [s for s in self.setups if s["id"] != sid]
            return 200, {"ok": True}
        if len(rest) == 2 and rest[1] == "activate" and method == "POST":
            if sid == self.active:
                raise ApiError(409, "That setup is already active.", "conflict")
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
        raise ApiError(405, "That isn't supported here.", "method_not_allowed")

    def check_downgrade(self, world, version, allow):
        w = self.worlds.get(world)
        if allow or not w or not w.get("last_version"):
            return
        if vcmp(version, w["last_version"]) < 0:
            raise ApiError(409, f"“{world}” was last played on Minecraft {w['last_version']}. Opening it with {version} could damage it.",
                           "world_downgrade", world=world, last_version=w["last_version"], version=version)

    def create_setup(self, body):
        stype = body.get("type")
        if stype not in ("vanilla", "fabric", "forge"):
            raise ApiError(400, "Choose Vanilla, Fabric or Forge.", "bad_request")
        version = str(body.get("minecraft_version") or "")
        allowed = {"vanilla": VANILLA, "fabric": FABRIC, "forge": FORGE}[stype]
        if version != "latest" and version not in allowed:
            raise ApiError(400, f"{stype.title()} isn't available for Minecraft {version or '(none)'}.", "bad_request")
        if version == "latest" and stype == "forge":
            raise ApiError(400, "Pick a specific Minecraft version for Forge.", "bad_request")
        level = str(body.get("level_name") or "")
        if not NAME_RE.match(level):
            raise ApiError(400, "World names: " + NAME_RULE, "bad_request")
        create = bool(body.get("create_world"))
        if create and level in self.worlds:
            raise ApiError(409, f"There's already a world called “{level}”. Pick another name.", "world_exists")
        if not create and level not in self.worlds:
            raise ApiError(400, f"There's no world called “{level}”.", "bad_request")
        resolved = LATEST if version == "latest" else version
        if not create:
            self.check_downgrade(level, resolved, body.get("allow_downgrade"))
        kind = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge"}[stype]
        name = str(body.get("name") or "").strip() or f"{kind} {resolved}"
        if len(name) > 48:
            raise ApiError(400, "Setup names are at most 48 characters.", "invalid_name")
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
        task = self.start_task("apply", f"Installing {kind} {resolved}", 9 if stype != "forge" else 14,
                               effect=effect, fail=fail, needs_stop=activate, steps=steps)
        return 202, {"task": task}

    def modpack_url(self, body):
        url = str(body.get("url") or "").strip()
        name = str(body.get("name") or "").strip()
        if not re.match(r"^https://\S+$", url):
            raise ApiError(400, "The modpack link should start with https://", "bad_request")
        if not NAME_RE.match(name):
            raise ApiError(400, "Pack names: " + NAME_RULE, "bad_request")
        level = str(body.get("level_name") or name)
        fail = None
        if "fail" in url.lower():
            fail = "Couldn't download the modpack: the link answered 404 Not Found. Check that it points to the .mrpack or .zip file itself."
        return self.start_modpack(name, level, fail, body.get("allow_downgrade"))

    def start_modpack(self, name, level, fail, allow_downgrade):
        if not NAME_RE.match(level):
            raise ApiError(400, "World names: " + NAME_RULE, "bad_request")
        version = "1.21.1"
        if level in self.worlds:
            self.check_downgrade(level, version, allow_downgrade)

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
            name = str(body.get("name") or "")
            if not NAME_RE.match(name):
                raise ApiError(400, "World names: " + NAME_RULE, "bad_request")
            if name in self.worlds:
                raise ApiError(409, f"There's already a world called “{name}”.", "conflict")
            self.worlds[name] = {"size_bytes": 0, "last_version": None, "last_played": None}
            return 200, {"ok": True}
        if len(rest) == 1 and method == "DELETE":
            name = unquote(rest[0])
            if name not in self.worlds:
                raise ApiError(404, f"There's no world called “{name}”.", "not_found")
            active = self.active_setup()
            if active and active["level_name"] == name:
                raise ApiError(409, f"“{name}” belongs to the active setup. Switch to another setup before deleting it.", "conflict")
            removed = [s["name"] for s in self.setups if s["level_name"] == name]
            self.setups = [s for s in self.setups if s["level_name"] != name]
            del self.worlds[name]
            return 200, {"ok": True, "removed_setups": removed}
        raise ApiError(405, "That isn't supported here.", "method_not_allowed")

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
            world = body.get("world") or (self.active_setup() or {}).get("level_name")
            if world not in self.worlds:
                raise ApiError(404, f"There's no world called “{world}”.", "not_found")
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
        backup = next((b for b in self.backups if b["name"] == name), None)
        if not backup:
            raise ApiError(404, "That backup doesn't exist any more.", "not_found")
        if len(rest) == 1 and method == "DELETE":
            self.backups.remove(backup)
            return 200, {"ok": True}
        if len(rest) == 2 and rest[1] == "restore" and method == "POST":
            target = body.get("target") or backup["world"]
            if not NAME_RE.match(str(target)):
                raise ApiError(400, "World names: " + NAME_RULE, "bad_request")
            if body.get("target") and target in self.worlds and target != backup["world"]:
                raise ApiError(409, f"There's already a world called “{target}”. Pick another name.", "conflict")
            active = self.active_setup()
            if self.phase in ("starting", "online", "stopping") and active and active["level_name"] == target:
                raise ApiError(409, f"The server is running “{target}”. Stop it first, or restore into a new world.", "conflict")
            replacing = target in self.worlds

            def effect():
                if replacing:
                    cur = self.worlds[target]
                    self._add_backup(target, time.time(), max(MiB, int(cur["size_bytes"] * 0.62)), cur["last_version"] or LATEST, safety=True)
                self.worlds[target] = {"size_bytes": int(backup["size_bytes"] / 0.62), "last_version": backup["minecraft_version"],
                                       "last_played": backup["_ts"]}
            steps = ([f"Saving the current {target} as a safety backup"] if replacing else []) + ["Unpacking the backup"]
            task = self.start_task("restore", f"Restoring {target}", 7, effect=effect, steps=steps)
            return 202, {"task": task}
        raise ApiError(405, "That isn't supported here.", "method_not_allowed")

    # -------------------------------------------------------------- players

    def players_api(self, method, rest, body):
        if not rest and method == "GET":
            return 200, {
                "online": list(self.players) if self.running() else [],
                "ops": [{"name": n, "uuid": offline_uuid(n)} for n in self.ops],
                "whitelist": [{"name": n, "uuid": offline_uuid(n)} for n in self.whitelist],
                "whitelist_enabled": self.whitelist_enabled,
                "online_mode": self.properties["online-mode"],
            }
        if rest == ["whitelist", "enabled"] and method == "PUT":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise ApiError(400, "Send enabled: true or false.", "bad_request")
            self.whitelist_enabled = enabled
            if self.running():
                self.panel(f"> whitelist {'on' if enabled else 'off'}")
                self.mc("Whitelist is now turned " + ("on" if enabled else "off"))
            return 200, {"ok": True}
        if rest and rest[0] in ("ops", "whitelist"):
            kind = rest[0]
            target = self.ops if kind == "ops" else self.whitelist
            label = "an operator" if kind == "ops" else "on the whitelist"
            if len(rest) == 1 and method == "POST":
                name = str(body.get("name") or "").strip()
                if not PLAYER_RE.match(name):
                    raise ApiError(400, "Minecraft usernames use 1 to 16 letters, numbers or underscores.", "bad_request")
                if "nobody" in name.lower():
                    raise ApiError(404, f"Mojang has no player called “{name}”. Check the spelling.", "not_found")
                if any(n.lower() == name.lower() for n in target):
                    raise ApiError(409, f"{name} is already {label}.", "conflict")
                target.append(name)
                if self.running():
                    self.panel(f"> {'op' if kind == 'ops' else 'whitelist add'} {name}")
                return 200, {"ok": True}
            if len(rest) == 2 and method == "DELETE":
                name = unquote(rest[1])
                match = next((n for n in target if n.lower() == name.lower()), None)
                if not match:
                    raise ApiError(404, f"{name} isn't {label}.", "not_found")
                target.remove(match)
                if self.running():
                    self.panel(f"> {'deop' if kind == 'ops' else 'whitelist remove'} {match}")
                return 200, {"ok": True}
        raise ApiError(405, "That isn't supported here.", "method_not_allowed")

    # -------------------------------------------------------------- settings

    def settings_api(self, method, body):
        if method == "GET":
            return 200, {
                "properties": dict(self.properties),
                "railpowered": {"idle_timeout_minutes": self.idle_timeout // 60, "auto_start": self.auto_start},
                "restart_required": self.restart_required,
                "schema": SCHEMA,
            }
        if method == "PUT":
            props = body.get("properties") or {}
            rp = body.get("railpowered") or {}
            if not isinstance(props, dict) or not isinstance(rp, dict):
                raise ApiError(400, "Send properties and railpowered as objects.", "bad_request")
            clean = {}
            for key, value in props.items():
                sch = SCHEMA.get(key)
                if not sch:
                    continue
                label = sch["label"]
                if sch["type"] == "int":
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ApiError(400, f"{label} must be a whole number.", "bad_request")
                    if value < sch["min"] or value > sch["max"]:
                        raise ApiError(400, f"{label} must be between {sch['min']} and {sch['max']}.", "bad_request")
                elif sch["type"] == "bool":
                    if not isinstance(value, bool):
                        raise ApiError(400, f"{label} must be on or off.", "bad_request")
                elif sch["type"] == "enum":
                    options = [o["value"] if isinstance(o, dict) else o for o in sch["options"]]
                    if value not in options:
                        raise ApiError(400, f"{label} can't be “{value}”.", "bad_request")
                else:
                    value = str(value)
                    if "\n" in value or len(value) > 200:
                        raise ApiError(400, f"{label} must be one line of up to 200 characters.", "bad_request")
                clean[key] = value
            minutes = rp.get("idle_timeout_minutes", self.idle_timeout // 60)
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
                raise ApiError(400, "Sleep after must be between 0 and 1440 minutes.", "bad_request")
            changed = any(self.properties.get(k) != v for k, v in clean.items())
            self.properties.update(clean)
            self.max_players = self.properties["max-players"]
            self.idle_timeout = minutes * 60
            if self.phase == "online" and not self.players:
                self.empty_since = time.time()
            self.auto_start = bool(rp.get("auto_start", self.auto_start))
            if changed and self.running():
                self.restart_required = True
            return 200, {"ok": True, "restart_required": self.restart_required}
        raise ApiError(405, "That isn't supported here.", "method_not_allowed")

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
    "/__mock/reset?phase=online&scenario=default|fresh": "start over; fresh = a brand new install",
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
                return self.send_json(200, {"ok": True})
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
                return self.send_file(os.path.join(web, "login.html"))
            if path == "/login" and method == "POST":
                return self.login()
            if path == "/logout" and method == "POST":
                return self.logout()
            if path == "/api/logout" and method == "GET":
                return self.logout()
            if path.startswith("/api/"):
                return self.api(method, path, query)
            self.send_json(404, {"error": "Not found.", "code": "not_found"})
        except ApiError as err:
            self.send_json(err.status, err.payload())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

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
        if self.command == "POST":
            body = self.read_body(64 * 1024).decode("utf-8", "replace")
            if self.headers.get("X-Railpowered") != "1" and "x_railpowered=1" not in body:
                return self.send_json(403, {"error": "This request was blocked. Reload the page and try again.", "code": "csrf"})
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
            raise ApiError(401, "Sign in again.", "unauthorized")
        if method != "GET" and self.headers.get("X-Railpowered") != "1":
            raise ApiError(403, "This request was blocked. Reload the page and try again.", "csrf")
        with self.mock.lock:
            failure = self.mock.failures.get(path)
            if failure and failure[1] > 0:
                failure[1] -= 1
                raise ApiError(failure[0], "The mock was told to fail this request.", "mock_failure")
        if method != "GET" and self.mock.args.latency:
            time.sleep(self.mock.args.latency / 1000)
        parts = [p for p in path[len("/api/"):].split("/") if p]
        if not parts:
            raise ApiError(404, "There's nothing at that address.", "not_found")

        if parts[0] == "backups" and len(parts) == 3 and parts[2] == "download" and method == "GET":
            name = unquote(parts[1])
            with self.mock.lock:
                exists = any(b["name"] == name for b in self.mock.backups)
            if not exists:
                raise ApiError(404, "That backup doesn't exist any more.", "not_found")
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
                raise ApiError(400, "The request body wasn't valid JSON.", "bad_request")
            if not isinstance(body, dict):
                raise ApiError(400, "The request body should be a JSON object.", "bad_request")
        with self.mock.lock:
            self.mock.tick()
            status, payload = self.mock.handle_api(method, parts, query, body)
        self.send_json(status, payload)

    def upload(self, query):
        name = (query.get("name") or [""])[0]
        level = (query.get("level_name") or [name])[0] or name
        allow = (query.get("allow_downgrade") or [""])[0] in ("1", "true")
        length = self.headers.get("Content-Length")
        if not length:
            raise ApiError(411, "The upload needs a Content-Length header.", "length_required")
        n = int(length)
        if n > MAX_UPLOAD:
            raise ApiError(413, "That file is bigger than 1 GB, the most this server accepts.", "too_large")
        if not NAME_RE.match(name):
            self.rfile.read(n)
            raise ApiError(400, "Pack names: " + NAME_RULE, "bad_request")
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
