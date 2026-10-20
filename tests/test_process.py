"""Server lifecycle: phases, ready detection, idle sleep, crashes, the proxy invariant."""

from __future__ import annotations

import itertools
import threading
import time
import unittest
from unittest import mock

from runtime_support import FakeProcess, FakeProxy, RuntimeTestCase, fake_spec, wait_for

import installer

from mc_host import installer_api as ix
from mc_host import main as main_module
from mc_host.process import (
    AlreadyRunning,
    NotRunning,
    ServerBusy,
    ServerController,
    StartFailed,
    WHITELIST_RE,
    build_command,
    idle_should_stop,
)
from mc_host.state import ServerState
from mc_host.tasks import TaskRunner

MODERN = "[12:00:00] [Server thread/INFO]: "
# Unique per call: time.monotonic() ticks in ~16 ms steps on Windows, so two
# quick markers could collide and a settle would match the previous one.
_MARKERS = itertools.count()
DONE = MODERN + 'Done (4.213s)! For help, type "help"'


class ControllerTestCase(RuntimeTestCase):
    idle_minutes = 10

    def setUp(self) -> None:
        super().setUp()
        self.state = ServerState()
        self.state.echo_logs = False
        self.logs: list[tuple[str, str]] = []
        self.tasks = TaskRunner(log=lambda text, level="info": self.logs.append((level, text)))
        self.proxy = FakeProxy()
        self.processes: list[FakeProcess] = []
        self.popen_calls: list[dict] = []
        self.next_process_kwargs: dict = {}
        self.spec = fake_spec()
        self.controller = ServerController(
            state=self.state,
            tasks=self.tasks,
            proxy=self.proxy,
            popen=self._popen,
            stop_timeout=2,
        )
        patches = {
            "prepare_runtime": mock.Mock(side_effect=lambda cfg=None, **_kw: self.spec),
            "child_env": mock.Mock(return_value={"PATH": "/usr/bin"}),
            "read_properties": mock.Mock(return_value={"motd": "Hi", "max-players": "8"}),
            "active_setup": mock.Mock(
                return_value={"minecraft_version": "1.20.1", "resolved_version": "1.20.1",
                              "level_name": "world", "type": "vanilla"}
            ),
            "record_world_version": mock.Mock(),
            "sync_access_lists_from_runtime": mock.Mock(),
            "apply_pending_properties": mock.Mock(return_value=False),
            "railpowered_settings": mock.Mock(
                side_effect=lambda cfg=None: {"idle_timeout_minutes": self.idle_minutes,
                                              "auto_start": False}
            ),
            "create_backup": mock.Mock(side_effect=self._fake_create_backup),
        }
        self.backup_error = None
        self.ix = {}
        for name, value in patches.items():
            patcher = mock.patch.object(ix, name, value)
            self.ix[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup_processes)

    def _fake_create_backup(self, world, before_tar=None, after_tar=None):
        # Mirrors installer.create_backup: before_tar, archive, after_tar in a finally.
        try:
            if before_tar:
                before_tar()
            if self.backup_error:
                raise self.backup_error
            return {"name": f"{world}-1.tar.gz"}
        finally:
            if after_tar:
                after_tar()

    def _cleanup_processes(self) -> None:
        for proc in self.processes:
            proc.exit(0)

    def _popen(self, command, **kwargs):
        self.popen_calls.append({"command": command, **kwargs})
        proc = FakeProcess(**self.next_process_kwargs)
        self.processes.append(proc)
        return proc

    @property
    def proc(self) -> FakeProcess:
        return self.processes[-1]

    def start_online(self) -> FakeProcess:
        self.controller.start()
        self.proc.emit(DONE)
        self.assertTrue(wait_for(lambda: self.state.phase == "online"))
        return self.proc

    def emit_and_settle(self, line: str) -> None:
        marker = f"{MODERN}marker {next(_MARKERS)}"
        self.proc.emit(line)
        self.proc.emit(marker)
        self.assertTrue(wait_for(lambda: any(e["text"].endswith(marker.split(": ", 1)[1])
                                             for e in self.state.logs_after()["lines"])))


class StartAndReadyTests(ControllerTestCase):
    def test_start_hands_port_to_java_and_waits_for_done(self):
        self.controller.ensure_listener()
        self.assertTrue(self.proxy.active)
        self.controller.start()
        self.assertEqual(self.state.phase, "starting")
        self.assertFalse(self.proxy.active, "proxy must release 25565 before Java binds it")
        self.assertEqual(self.controller.public_phase(), "starting")
        call = self.popen_calls[0]
        self.assertIn("-Dlog4j2.formatMsgNoLookups=true", call["command"])
        self.assertTrue(any(arg.startswith("-Xmx") for arg in call["command"]))
        self.assertEqual(call["env"], {"PATH": "/usr/bin"})
        self.assertEqual(call["errors"], "replace")
        self.assertIsNone(self.controller.seconds_until_sleep())
        self.proc.emit(DONE)
        self.assertTrue(wait_for(lambda: self.state.phase == "online"))
        self.ix["record_world_version"].assert_called_once_with("world", "1.20.1", "vanilla")
        self.assertIsNotNone(self.controller.seconds_until_sleep())

    def test_chat_cannot_fake_ready(self):
        self.controller.start()
        self.emit_and_settle(MODERN + "<Bob> Done (1.0s)! fooled you")
        self.emit_and_settle(MODERN + "[Bob] Done (2.0s)!")
        self.assertEqual(self.state.phase, "starting")

    def test_legacy_done_line(self):
        self.controller.start()
        self.proc.emit('2012-03-01 12:00:00 [INFO] Done (1234567ns)! For help, type "help" or "?"')
        self.assertTrue(wait_for(lambda: self.state.phase == "online"))

    def test_second_start_is_refused(self):
        self.controller.start()
        with self.assertRaises(AlreadyRunning):
            self.controller.start()
        self.assertEqual(len(self.popen_calls), 1)

    def test_start_refused_while_task_runs(self):
        release = threading.Event()
        self.tasks.submit("backup", "Backing up world", lambda ctx: release.wait(5))
        try:
            with self.assertRaises(ServerBusy):
                self.controller.start()
            self.assertEqual(self.controller.public_phase(), "preparing")
        finally:
            release.set()
            self.tasks.wait_idle(5)

    def test_failed_prepare_crashes_with_reason_and_rearms_proxy(self):
        self.ix["prepare_runtime"].side_effect = installer.InstallError("Mojang is unreachable")
        with self.assertRaises(StartFailed) as caught:
            self.controller.start()
        self.assertEqual(str(caught.exception), "Mojang is unreachable")
        self.assertEqual(self.state.phase, "crashed")
        self.assertIsNone(self.state.last_exit["code"])
        self.assertEqual(self.state.last_exit["reason"], "Mojang is unreachable")
        self.assertTrue(self.proxy.active)

    def test_downgrade_refusal_keeps_its_code_and_can_be_confirmed(self):
        self.ix["prepare_runtime"].side_effect = installer.InstallError(
            "World 'world' has been played on Minecraft 1.21.1.", code="world_downgrade",
            data={"world": "world", "last_version": "1.21.1", "version": "1.20.1"},
        )
        with self.assertRaises(StartFailed) as caught:
            self.controller.start()
        error = caught.exception
        self.assertEqual((error.code, error.status, error.data["last_version"]), ("world_downgrade", 409, "1.21.1"))
        self.ix["prepare_runtime"].side_effect = lambda cfg=None, **_kw: self.spec
        self.controller.start(allow_downgrade=True)
        self.ix["prepare_runtime"].assert_called_with(allow_downgrade=True)
        self.assertEqual(self.state.phase, "starting")

    def test_missing_java_is_reported_plainly(self):
        def boom(*args, **kwargs):
            raise FileNotFoundError(2, "No such file", "/opt/java/8/bin/java")

        self.controller.popen = boom
        with mock.patch("builtins.print"):
            with self.assertRaises(StartFailed) as caught:
                self.controller.start()
        self.assertIn("Java could not be found", str(caught.exception))
        self.assertTrue(self.proxy.active)
        self.assertFalse(self.state.port_handed_off)

    def test_ensure_listener_never_steals_the_port_from_java(self):
        self.controller.start()
        starts = self.proxy.starts
        self.assertFalse(self.controller.ensure_listener())
        self.assertEqual(self.proxy.starts, starts)


class PlayerTrackingTests(ControllerTestCase):
    def test_join_and_leave_update_players_and_idle_timer(self):
        self.start_online()
        self.emit_and_settle(MODERN + "Alice joined the game")
        self.assertEqual(self.state.players, {"Alice"})
        self.assertIsNone(self.state.idle_since)
        self.emit_and_settle(MODERN + "Alice left the game")
        self.assertEqual(self.state.players, set())
        self.assertIsNotNone(self.state.idle_since)

    def test_chat_lines_cannot_spoof_joins_or_leaves(self):
        self.start_online()
        self.emit_and_settle(MODERN + "Alice joined the game")
        for line in (
            MODERN + "<Bob> Ghost joined the game",
            MODERN + "<Alice> Alice left the game",
            MODERN + "[Server] Ghost joined the game",
            MODERN + "* Alice left the game",
            MODERN + "[Not Secure] <Bob> Ghost joined the game",
            "2012-03-01 12:00:00 [INFO] <Bob> Ghost [/1.2.3.4:5] logged in with entity id 7",
            "2012-03-01 12:00:00 [INFO] <Alice> Alice lost connection: bye",
        ):
            self.emit_and_settle(line)
        self.assertEqual(self.state.players, {"Alice"})

    def test_renamed_player_and_forge_prefix(self):
        self.start_online()
        self.emit_and_settle(MODERN + "Steve (formerly known as Notch) joined the game")
        self.emit_and_settle(
            "[12:00:00] [Server thread/INFO] [minecraft/DedicatedServer]: Alex joined the game"
        )
        self.assertEqual(self.state.players, {"Steve", "Alex"})

    def test_legacy_join_and_leave(self):
        self.start_online()
        self.emit_and_settle(
            "2012-03-01 12:00:00 [INFO] Steve [/127.0.0.1:50211] logged in with entity id 182 at (1.5, 64.0, 2.5)"
        )
        self.emit_and_settle(
            "2013-09-19 12:00:00 [INFO] Alex[/127.0.0.1:50212] logged in with entity id 183 at (1.5, 64.0, 2.5)"
        )
        self.assertEqual(self.state.players, {"Steve", "Alex"})
        self.emit_and_settle("2012-03-01 12:00:00 [INFO] Steve lost connection: disconnect.quitting")
        self.assertEqual(self.state.players, {"Alex"})

    def test_26x_system_chat_lines(self):
        # 26.x logs joins, leaves and command replies as "System chat: ...".
        self.start_online()
        self.emit_and_settle(MODERN + "System chat: Alice joined the game")
        self.emit_and_settle(MODERN + "System chat: Bob joined the game")
        self.emit_and_settle(MODERN + "<Bob> System chat: Ghost joined the game")
        self.assertEqual(self.state.players, {"Alice", "Bob"})
        self.emit_and_settle(MODERN + "System chat: Bob left the game")
        self.assertEqual(self.state.players, {"Alice"})

    def test_ansi_codes_are_stripped(self):
        self.start_online()
        self.emit_and_settle("\x1b[32m" + MODERN + "Alice joined the game\x1b[0m")
        self.assertEqual(self.state.players, {"Alice"})


class IdleTests(ControllerTestCase):
    idle_minutes = 1

    def test_pure_decision(self):
        self.assertFalse(idle_should_stop("starting", 0, 0.0, 60, 1000.0))
        self.assertFalse(idle_should_stop("online", 1, 0.0, 60, 1000.0))
        self.assertFalse(idle_should_stop("online", 0, None, 60, 1000.0))
        self.assertFalse(idle_should_stop("online", 0, 0.0, 0, 1000.0), "0 means never sleep")
        self.assertFalse(idle_should_stop("online", 0, 0.0, 60, 1000.0, busy=True))
        self.assertFalse(idle_should_stop("online", 0, 990.0, 60, 1000.0))
        self.assertTrue(idle_should_stop("online", 0, 900.0, 60, 1000.0))

    def test_idle_timer_does_not_run_while_booting(self):
        self.controller.start()
        self.assertFalse(self.controller.idle_tick(now=time.time() + 600))
        self.assertEqual(self.state.phase, "starting")

    def test_start_watchdog_stops_a_server_that_never_gets_ready(self):
        self.controller.start()
        proc = self.proc
        proc.emit(MODERN + "Loading mods: 312")
        self.assertFalse(self.controller.idle_tick(now=time.time() + 60))
        with mock.patch.object(main_module.config, "START_TIMEOUT", 900):
            self.assertTrue(self.controller.idle_tick(now=time.time() + 901))
        self.assertEqual(proc.commands[-1], "stop")
        self.assertEqual(self.state.phase, "crashed")
        self.assertIn("didn't finish starting within 15 minutes", self.state.last_exit["reason"])
        self.assertTrue(self.proxy.active)
        # Joins don't wake it straight back into the same hang.
        self.assertTrue(self.controller.wake_blocked())

    def test_idle_stop_saves_stops_and_rearms_proxy(self):
        self.next_process_kwargs = {
            "list_reply": MODERN + "There are 0 of a max of 20 players online: "
        }
        proc = self.start_online()
        since = self.state.idle_since
        self.assertFalse(self.controller.idle_tick(now=since + 30))
        self.assertEqual(self.controller.seconds_until_sleep(now=since + 30), 30)
        self.assertTrue(self.controller.idle_tick(now=since + 61))
        self.assertEqual(self.state.phase, "sleeping")
        self.assertEqual(proc.commands[:3], ["list", "save-all flush", "stop"])
        self.assertTrue(self.proxy.active)
        self.ix["sync_access_lists_from_runtime"].assert_called_once()

    def test_list_reconcile_keeps_server_up_when_players_are_online(self):
        self.next_process_kwargs = {
            "list_reply": MODERN + "There are 1 of a max of 20 players online: Steve"
        }
        self.start_online()
        since = self.state.idle_since
        self.assertFalse(self.controller.idle_tick(now=since + 120))
        self.assertEqual(self.state.phase, "online")
        self.assertEqual(self.state.players, {"Steve"})

    def test_list_reconcile_26x_system_chat_reply(self):
        self.next_process_kwargs = {
            "list_reply": MODERN + "System chat: There are 1 of a max of 20 players online: Steve"
        }
        self.start_online()
        since = self.state.idle_since
        self.assertFalse(self.controller.idle_tick(now=since + 120))
        self.assertEqual(self.state.players, {"Steve"})

    def test_list_reconcile_old_two_line_format(self):
        self.next_process_kwargs = {"list_reply": MODERN + "There are 1/20 players online:"}
        self.start_online()
        waiter = threading.Thread(target=lambda: setattr(self, "names", self.controller.reconcile_players(3)))
        waiter.start()
        self.assertTrue(wait_for(lambda: "list" in self.proc.commands))
        self.proc.emit(MODERN + "Steve, Alex")
        waiter.join(5)
        self.assertEqual(self.names, ["Steve", "Alex"])

    def test_zero_timeout_never_sleeps(self):
        self.idle_minutes = 0
        self.start_online()
        self.assertIsNone(self.controller.seconds_until_sleep())
        self.assertFalse(self.controller.idle_tick(now=time.time() + 100_000))

    def test_idle_skipped_while_task_runs(self):
        self.start_online()
        release = threading.Event()
        self.tasks.submit("backup", "Backing up", lambda ctx: release.wait(5))
        try:
            self.assertFalse(self.controller.idle_tick(now=time.time() + 100_000))
        finally:
            release.set()
            self.tasks.wait_idle(5)


class StopAndCrashTests(ControllerTestCase):
    def test_stop_sends_save_then_stop(self):
        proc = self.start_online()
        self.assertTrue(self.controller.stop())
        self.assertEqual(proc.commands, ["save-all flush", "stop"])
        self.assertEqual(self.state.phase, "sleeping")
        self.assertIsNone(self.state.process)
        self.assertTrue(self.proxy.active)
        self.assertIsNone(self.state.last_exit)
        with self.assertRaises(NotRunning):
            self.controller.stop()

    def test_legacy_versions_use_plain_save_all(self):
        self.spec = fake_spec(version="1.2.5", java_major=8)
        proc = self.start_online()
        self.controller.stop()
        self.assertEqual(proc.commands, ["save-all", "stop"])

    def test_stop_kills_after_timeout(self):
        self.next_process_kwargs = {"stops_on_command": False}
        self.controller.stop_timeout = 0.3
        proc = self.start_online()
        self.assertTrue(self.controller.stop())
        self.assertEqual(proc.returncode, -9)
        self.assertEqual(self.state.phase, "sleeping")
        self.assertTrue(self.proxy.active)

    def test_crash_records_exit_and_rearms_proxy(self):
        proc = self.start_online()
        self.emit_and_settle(MODERN + "Alice joined the game")
        proc.emit("[12:00:09] [Server thread/ERROR]: Encountered an unexpected exception")
        proc.emit("java.lang.OutOfMemoryError: Java heap space")
        proc.emit("\tat net.minecraft.server.Main.run(Main.java:1)")
        proc.exit(1)
        self.assertTrue(wait_for(lambda: self.state.phase == "crashed"))
        self.assertEqual(self.controller.public_phase(), "crashed")
        self.assertEqual(self.state.last_exit["code"], 1)
        self.assertIn("OutOfMemoryError", self.state.last_exit["reason"])
        self.assertEqual(self.state.players, set())
        self.assertTrue(wait_for(lambda: self.proxy.active))

    def test_crash_while_starting_is_labelled(self):
        self.controller.start()
        self.proc.emit("Error: Could not create the Java Virtual Machine.")
        self.proc.exit(1)
        self.assertTrue(wait_for(lambda: self.state.phase == "crashed"))
        self.assertTrue(self.state.last_exit["reason"].startswith("Crashed while starting"))
        self.assertTrue(self.proxy.active)

    def test_normal_stop_is_never_reported_as_a_crash(self):
        for _ in range(5):
            self.start_online()
            self.controller.stop()
            self.assertEqual(self.state.phase, "sleeping")
            self.assertIsNone(self.state.last_exit)
        self.assertFalse(any("unexpectedly" in e["text"] for e in self.state.logs_after()["lines"]))

    def test_start_after_crash_clears_last_exit_when_online(self):
        proc = self.start_online()
        proc.exit(3)
        self.assertTrue(wait_for(lambda: self.state.phase == "crashed"))
        self.start_online()
        self.assertIsNone(self.state.last_exit)

    def test_restart(self):
        first = self.start_online()
        self.controller.restart()
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(self.processes), 2)
        self.assertEqual(self.state.phase, "starting")

    def test_shutdown_waits_for_a_stop_already_in_progress(self):
        self.next_process_kwargs = {"stops_on_command": False}
        self.controller.stop_timeout = 0.5
        proc = self.start_online()
        stopper = threading.Thread(target=self.controller.stop, kwargs={"reason": "idle"})
        stopper.start()
        self.assertTrue(wait_for(lambda: self.state.phase == "stopping"))
        self.controller.shutdown()
        # Returning means the container may end: Java must be gone by then.
        self.assertIsNotNone(proc.poll())
        stopper.join(5)
        self.assertFalse(self.proxy.active)

    def test_shutdown_during_prepare_never_launches_java(self):
        gate = threading.Event()

        def slow_prepare(cfg=None):
            gate.wait(5)
            return self.spec

        self.ix["prepare_runtime"].side_effect = slow_prepare
        errors = []

        def start():
            try:
                self.controller.start(reason="a player joined")
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(exc)

        thread = threading.Thread(target=start)
        thread.start()
        self.assertTrue(wait_for(lambda: self.state.phase == "starting"))
        self.controller.shutdown()
        gate.set()
        thread.join(5)
        self.assertEqual(self.popen_calls, [])
        self.assertIsInstance(errors[0], ServerBusy)
        self.assertEqual(self.state.phase, "sleeping")
        self.assertFalse(self.proxy.active)

    def test_shutdown_stops_server_even_during_a_task(self):
        proc = self.start_online()
        release = threading.Event()
        self.tasks.submit("modpack", "Installing", lambda ctx: release.wait(5))
        try:
            self.controller.shutdown()
        finally:
            release.set()
            self.tasks.wait_idle(5)
        self.assertEqual(proc.commands, ["save-all flush", "stop"])
        self.assertFalse(self.proxy.active)


class LifecycleGuardTests(ControllerTestCase):
    def _block_prepare(self):
        gate = threading.Event()

        def slow_prepare(cfg=None):
            gate.wait(5)
            return self.spec

        self.ix["prepare_runtime"].side_effect = slow_prepare
        thread = threading.Thread(target=self.controller.start)
        thread.start()
        self.assertTrue(wait_for(lambda: self.state.phase == "starting"))
        self.addCleanup(thread.join, 5)
        self.addCleanup(gate.set)
        return gate

    def test_tasks_are_refused_while_a_start_is_preparing(self):
        self._block_prepare()
        self.assertFalse(self.controller.is_running())
        self.assertTrue(self.controller.lifecycle_active())
        self.assertEqual(self.controller.current_world(), "world")  # what Java is about to open
        with self.assertRaises(ServerBusy):
            self.tasks.submit("switch", "Switching", lambda ctx: None, guard=self.controller.ensure_quiet)
        self.assertIsNone(self.tasks.running())

    def test_tasks_are_refused_while_stopping(self):
        self.next_process_kwargs = {"stops_on_command": False}
        self.controller.stop_timeout = 0.5
        self.start_online()
        stopper = threading.Thread(target=self.controller.stop)
        stopper.start()
        self.assertTrue(wait_for(lambda: self.state.phase == "stopping"))
        with self.assertRaises(ServerBusy):
            self.tasks.submit("restore", "Restoring", lambda ctx: None, guard=self.controller.ensure_quiet)
        stopper.join(5)
        self.tasks.submit("restore", "Restoring", lambda ctx: None, guard=self.controller.ensure_quiet)
        self.tasks.wait_idle(5)

    def test_settle_waits_out_a_prepare_and_reports_a_restart(self):
        gate = self._block_prepare()
        threading.Timer(0.2, gate.set).start()
        self.assertFalse(self.controller.settle(5))
        self.assertTrue(self.controller.is_running())

    def test_clear_crash_after_a_switch(self):
        self.ix["prepare_runtime"].side_effect = installer.InstallError(
            "Java 8 is missing", code="java_missing")
        with self.assertRaises(StartFailed):
            self.controller.start()
        self.assertEqual(self.state.last_exit["setup_id"], None)
        self.assertTrue(self.controller.wake_blocked())
        exit_info = self.controller.snapshot()["last_exit"]
        self.assertEqual((exit_info["wake"], exit_info["wake_at"]), ("manual", None))
        self.assertEqual(self.controller.proxy_info()["mode"], "crashed")
        self.assertFalse(self.controller.request_wake("Steve"))
        self.assertTrue(self.controller.clear_crash("Switched setups."))
        self.assertEqual(self.state.phase, "sleeping")
        self.assertIsNone(self.state.last_exit)
        self.assertFalse(self.controller.wake_blocked())
        self.assertFalse(self.controller.clear_crash())  # nothing to clear

    def test_transient_failure_backs_off_for_a_while(self):
        self.ix["prepare_runtime"].side_effect = installer.InstallError("Mojang is unreachable")
        with self.assertRaises(StartFailed):
            self.controller.start()
        self.assertTrue(self.controller.wake_blocked())
        self.assertFalse(self.controller.wake_blocked(now=time.time() + 301))
        exit_info = self.controller.snapshot()["last_exit"]
        self.assertEqual(exit_info["wake"], "later")
        self.assertAlmostEqual(exit_info["wake_at"], time.time() + 300, delta=5)

    def test_crash_after_being_online_still_wakes(self):
        proc = self.start_online()
        proc.exit(1)
        self.assertTrue(wait_for(lambda: self.state.phase == "crashed"))
        self.assertFalse(self.controller.wake_blocked())
        self.assertEqual(self.controller.proxy_info()["mode"], "sleeping")
        self.assertEqual(self.controller.snapshot()["last_exit"]["wake"], "join")

    def test_whitelist_gate_fails_closed(self):
        self.ix["read_properties"].return_value = {"white-list": "True"}
        with mock.patch.object(ix, "players", side_effect=installer.InstallError("unreadable")):
            info = self.controller.proxy_info()
        self.assertEqual(info["allowed"], set())


class WakeAndProxyTests(ControllerTestCase):
    EMPTY_LIST = MODERN + "There are 0 of a max of 20 players online: "

    def wake_online(self, name: str = "Steve") -> None:
        count = len(self.processes)
        self.assertTrue(self.controller.request_wake(name))
        self.assertTrue(wait_for(lambda: len(self.processes) == count + 1))
        self.proc.emit(DONE)
        self.assertTrue(wait_for(lambda: self.state.phase == "online"))

    def sleep_idle(self) -> None:
        self.assertTrue(self.controller.idle_tick(now=self.state.idle_since + 601))
        self.assertTrue(wait_for(lambda: self.state.phase == "sleeping"))

    def test_empty_wakes_back_off_15_minutes_then_an_hour(self):
        self.next_process_kwargs = {"list_reply": self.EMPTY_LIST}
        self.controller.ensure_listener()
        self.wake_online()
        self.sleep_idle()
        paused = self.controller.wake_paused_until()
        self.assertAlmostEqual(paused, time.time() + 900, delta=10)
        self.assertFalse(self.controller.request_wake("Scanner"))
        self.assertEqual(self.controller.proxy_info()["mode"], "resting")
        self.assertEqual(self.controller.snapshot()["idle"]["wake_paused_until"], int(paused))
        self.assertTrue(any("nobody joined" in text for text in (e["text"] for e in self.state.logs_after()["lines"])))
        # After the pause, the next empty wake pauses for an hour.
        self.controller._wake_paused_until = time.time() - 1
        self.assertIsNone(self.controller.wake_paused_until())
        self.assertEqual(self.controller.proxy_info()["mode"], "sleeping")
        self.wake_online()
        self.sleep_idle()
        self.assertAlmostEqual(self.controller.wake_paused_until(), time.time() + 3600, delta=10)
        self.controller._wake_paused_until = time.time() - 1
        self.wake_online()
        self.sleep_idle()
        self.assertAlmostEqual(self.controller.wake_paused_until(), time.time() + 3600, delta=10)

    def test_a_player_joining_resets_the_backoff(self):
        self.next_process_kwargs = {"list_reply": self.EMPTY_LIST}
        self.wake_online()
        self.sleep_idle()
        self.controller._wake_paused_until = time.time() - 1
        self.wake_online()
        self.emit_and_settle(MODERN + "Alice joined the game")
        self.emit_and_settle(MODERN + "Alice left the game")
        self.sleep_idle()
        self.assertIsNone(self.controller.wake_paused_until())
        self.assertEqual(self.controller._empty_wakes, 0)
        self.assertIsNone(self.controller.snapshot()["idle"]["wake_paused_until"])

    def test_players_found_by_the_idle_check_count_as_a_join(self):
        self.next_process_kwargs = {"list_reply": MODERN + "There are 1 of a max of 20 players online: Alice"}
        self.wake_online()
        self.assertFalse(self.controller.idle_tick(now=self.state.idle_since + 601))
        self.assertEqual(self.state.players, {"Alice"})
        self.emit_and_settle(MODERN + "Alice left the game")
        self.proc.list_reply = self.EMPTY_LIST
        self.sleep_idle()
        self.assertIsNone(self.controller.wake_paused_until())

    def test_dashboard_starts_and_manual_stops_never_pause_wakes(self):
        self.next_process_kwargs = {"list_reply": self.EMPTY_LIST}
        self.start_online()  # a start from the dashboard, nobody joins
        self.sleep_idle()
        self.assertIsNone(self.controller.wake_paused_until())
        self.wake_online()
        self.controller.stop()  # stopped by the owner, not by idle sleep
        self.assertTrue(wait_for(lambda: self.state.phase == "sleeping"))
        self.assertIsNone(self.controller.wake_paused_until())
        # A pause is lifted by a start from the dashboard.
        self.wake_online()
        self.sleep_idle()
        self.assertIsNotNone(self.controller.wake_paused_until())
        self.start_online()
        self.assertIsNone(self.controller.wake_paused_until())

    def test_wake_starts_once(self):
        self.assertTrue(self.controller.request_wake("Steve"))
        self.controller.request_wake("Alex")
        self.assertTrue(wait_for(lambda: len(self.popen_calls) == 1))
        time.sleep(0.1)
        self.assertEqual(len(self.popen_calls), 1)
        self.assertFalse(self.controller.request_wake("Steve"))

    def test_wake_ignored_during_task_and_proxy_says_updating(self):
        self.controller.ensure_listener()
        release = threading.Event()
        self.tasks.submit("apply", "Installing Fabric 1.20.1", lambda ctx: release.wait(5))
        try:
            self.assertFalse(self.controller.request_wake("Steve"))
            self.assertEqual(self.controller.proxy_info()["mode"], "updating")
            self.assertEqual(self.controller.public_phase(), "preparing")
            self.assertTrue(self.proxy.active, "the proxy keeps 25565 during tasks")
        finally:
            release.set()
            self.tasks.wait_idle(5)
        self.assertEqual(self.popen_calls, [])
        self.assertEqual(self.controller.proxy_info()["mode"], "sleeping")

    def test_task_end_rearms_proxy(self):
        self.proxy.active = False
        self.tasks.submit("restore", "Restoring", lambda ctx: None)
        self.tasks.wait_idle(5)
        self.assertTrue(self.proxy.active)

    def test_failed_task_still_rearms_proxy(self):
        self.proxy.active = False

        def fail(ctx):
            raise installer.InstallError("download failed")

        self.tasks.submit("apply", "Installing", fail)
        self.tasks.wait_idle(5)
        self.assertTrue(self.proxy.active)
        self.assertEqual(self.tasks.snapshot()["state"], "failed")
        self.assertEqual(self.tasks.snapshot()["error"], "download failed")

    def test_proxy_info_reads_motd_slots_version_and_whitelist(self):
        installer.write_properties({"motd": "Café SMP", "max-players": "8", "white-list": "true"})
        with open(installer.paths()["properties"], encoding="latin-1") as handle:
            self.assertIn("motd=Caf\\u00e9 SMP", handle.read())  # stored escaped
        self.ix["read_properties"].side_effect = lambda: dict(installer.read_properties())
        with mock.patch.object(
            ix, "players",
            return_value={"ops": [{"name": "Owner"}], "whitelist": [{"name": "Steve"}]},
        ):
            info = self.controller.proxy_info()
        self.assertEqual(info["motd"], "Café SMP")
        self.assertEqual(info["max_players"], 8)
        self.assertEqual(info["version_name"], "1.20.1")
        self.assertEqual(info["allowed"], {"owner", "steve"})
        self.assertEqual(info["mode"], "sleeping")

    def test_proxy_info_while_starting(self):
        gate = threading.Event()

        def slow_prepare(cfg=None):
            gate.wait(5)
            return self.spec

        self.ix["prepare_runtime"].side_effect = slow_prepare
        thread = threading.Thread(target=self.controller.start)
        thread.start()
        try:
            self.assertTrue(wait_for(lambda: self.state.phase == "starting"))
            self.assertEqual(self.controller.proxy_info()["mode"], "starting")
        finally:
            gate.set()
            thread.join(5)


class BackupTests(ControllerTestCase):
    def test_live_backup_uses_save_off_flush_and_save_on(self):
        proc = self.start_online()
        self.controller.backup_world("world")
        self.assertEqual(proc.commands, ["save-off", "save-all flush", "save-on"])
        self.assertEqual(self.ix["create_backup"].call_args[0], ("world",))

    def test_save_on_runs_even_when_the_backup_fails(self):
        proc = self.start_online()
        self.backup_error = installer.InstallError("disk full")
        with self.assertRaises(installer.InstallError):
            self.controller.backup_world("world")
        self.assertEqual(proc.commands[-1], "save-on")

    def test_backup_of_other_world_does_not_touch_the_server(self):
        proc = self.start_online()
        self.controller.backup_world("creative")
        self.assertEqual(proc.commands, [])

    def test_backup_while_booting_is_refused(self):
        self.controller.start()
        with self.assertRaises(installer.InstallError):
            self.controller.backup_world("world")


class WhitelistTests(ControllerTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(ix, "set_whitelist_enabled", side_effect=self._record_write)
        self.write = patcher.start()
        self.addCleanup(patcher.stop)
        self.seen_at_write: list[list[str]] = []

    def _record_write(self, enabled, keep=None):
        self.seen_at_write.append([e["text"] for e in self.state.logs_after()["lines"]])

    def test_properties_are_written_after_minecraft_confirms(self):
        # Minecraft saves server.properties while handling `whitelist on`;
        # writing first would lose enforce-whitelist.
        proc = self.start_online()
        self.controller.set_whitelist(True)
        self.assertEqual(proc.commands, ["whitelist on"])
        # The properties as they were before Minecraft rewrote the file.
        self.write.assert_called_once_with(True, keep={"motd": "Hi", "max-players": "8"})
        self.assertTrue(any(t.endswith("Whitelist is now turned on") for t in self.seen_at_write[0]))

    def test_stopped_server_only_writes_the_file(self):
        started = time.monotonic()
        self.controller.set_whitelist(False)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.write.call_args.args, (False,))
        self.assertEqual(self.processes, [])

    def test_missing_confirmation_still_writes_with_a_warning(self):
        proc = self.start_online()
        with mock.patch.object(proc, "on_command", side_effect=proc.commands.append):
            self.controller.set_whitelist(True, timeout=0.1)
        self.assertEqual(self.write.call_args.args, (True,))
        self.assertTrue(any("didn't confirm the whitelist" in e["text"]
                            for e in self.state.logs_after()["lines"]))

    def test_reply_wording_across_versions(self):
        for line in (
            "[12:00:00] [Server thread/INFO]: System chat: Whitelist is now turned on",
            "[12:00:00] [Server thread/INFO]: Whitelist is already turned off",
            "[12:00:00] [Server thread/INFO]: Turned on the whitelist",
            "2026-10-20 12:00:00 [INFO] Turned off white-listing",
        ):
            with self.subTest(line=line):
                self.assertTrue(WHITELIST_RE.search(line))
        self.assertIsNone(WHITELIST_RE.search("[12:00:00] [Server thread/INFO]: <Steve> Whitelist is now turned on"))


class CommandLineTests(unittest.TestCase):
    def test_build_from_parts(self):
        spec = fake_spec(prefix_args=["@libraries/args.txt"], jar="")
        command = build_command(spec)
        self.assertEqual(command[0], "/opt/java/21/bin/java")
        self.assertIn("-XX:+UseG1GC", command)
        self.assertEqual(command[-2:], ["@libraries/args.txt", "nogui"])
        self.assertNotIn("-jar", command)

    def test_build_from_command_keeps_installer_flags(self):
        spec = fake_spec(jvm_args=["-Xmx3G", "-XX:+UseZGC"])
        command = build_command(spec)
        self.assertEqual(command[-3:], ["-jar", "server.jar", "nogui"])
        self.assertEqual(command.count("-Xmx3G"), 1)
        self.assertFalse(any(arg.startswith("-Xmx") and arg != "-Xmx3G" for arg in command))
        self.assertNotIn("-XX:+UseG1GC", command)
        self.assertIn("-Dlog4j2.formatMsgNoLookups=true", command)


class SignalTests(unittest.TestCase):
    def test_signal_handler_only_sets_a_flag(self):
        flags = dict(main_module._flags)
        self.addCleanup(main_module._flags.update, flags)
        main_module._flags["shutdown"] = False
        with mock.patch.object(main_module.state, "add_log") as add_log:
            main_module._on_signal(15, None)
        self.assertTrue(main_module._flags["shutdown"])
        add_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
