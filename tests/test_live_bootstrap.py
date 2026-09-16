"""Transport B's bootstrap, the keeper, and `play`'s launch environment.

These three are the parts of tier 2 that can be checked without the game running, and each
one has a failure mode that is invisible in normal use:

* a bootstrap that does not return the game's constants breaks the game at startup;
* a bootstrap that cannot be told apart from a foreign file gets deleted by `uninstall`;
* an override whose keeper dies quietly leaves the game modified, or worse, the keeper keeps
  renewing a heartbeat for a game that is gone;
* a launch environment missing `DYLD_INSERT_LIBRARIES` produces a game with no agent, which
  looks exactly like a broken agent.

The Lua assertions run in the game's own LuaJIT through the S8 oracle, and are skipped when
the game is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import config as config_mod, context as context_mod
from krcheat.core import log as log_mod, oracle, paths, state as state_mod
from krcheat.core.errors import ChannelUnavailable, UsageError
from krcheat.core.live import agent as agent_mod
from krcheat.core.live import keeper, protocol, transport_patched
from krcheat.core.live.transport_patched import PatchedLoveTransport

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "slot_synthetic.lua")

#: Stand-in for the shipped 119-byte `main_globals.lua`, so the install mechanics can be
#: tested where the game is not installed. Nothing in transport B interprets these bytes; it
#: copies them and hands them back to the game.
FAKE_ORIGINAL = b"\x1bLJ\x02\x00fake-bytecode-KR_PLATFORM-mac\x00"


class _OfflineTransport(PatchedLoveTransport):
    """Transport B with the archive replaced by a constant.

    Only `archive_original` is overridden: everything about where files go, what they contain
    and how they are removed is the production code path.
    """

    def archive_original(self):
        return FAKE_ORIGINAL


class Base(unittest.TestCase):
    def setUp(self):
        self.previous_home = os.environ.get("KRCHEAT_HOME")
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        self.savedir = tempfile.mkdtemp(prefix="krcheat-test-save-")
        os.environ["KRCHEAT_HOME"] = self.home
        os.makedirs(os.path.join(self.home, "logs"), exist_ok=True)
        shutil.copyfile(FIXTURE, os.path.join(self.savedir, "slot_1.lua"))

    def tearDown(self):
        if self.previous_home is None:
            os.environ.pop("KRCHEAT_HOME", None)
        else:
            os.environ["KRCHEAT_HOME"] = self.previous_home
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.savedir, ignore_errors=True)

    def ctx(self, command="test", **flags):
        ctx = context_mod.Ctx(
            command=command,
            argv=[command],
            config=config_mod.Config.load(),
            state=state_mod.State.load(),
            log=log_mod.NullLogger(),
            **flags
        )
        ctx.save_dir_override = self.savedir
        ctx.game_override = os.path.join(self.home, "not-installed", "Kingdom Rush.app")
        return ctx

    def transport(self, ctx=None):
        return _OfflineTransport(ctx=ctx or self.ctx())


class TestArchiveDependency(Base):
    """The one place transport B needs the game archive, kept separate on purpose."""

    def test_write_files_needs_an_original_and_says_so(self):
        with self.assertRaises(Exception) as caught:
            self.transport().write_files(None)
        self.assertIn("nothing to stand in for", str(caught.exception))

    def test_write_files_copies_the_bytes_verbatim(self):
        transport = self.transport()
        written = transport.write_files(FAKE_ORIGINAL)
        with open(written["original"], "rb") as handle:
            self.assertEqual(handle.read(), FAKE_ORIGINAL)


class TestBootstrapModule(Base):
    """The generated Lua, and its contract with the game."""

    def setUp(self):
        super().setUp()
        self.ctx = self.ctx()
        self.text = transport_patched.render_bootstrap("9.9.9", self.transport(self.ctx).save_dir())

    def test_it_is_marked_and_self_describing(self):
        self.assertTrue(self.text.startswith(transport_patched.MARKER))
        self.assertIn("transport B", self.text)
        self.assertIn("krcheat uninstall", self.text)

    def test_it_never_shadows_a_module_other_than_main_globals(self):
        # The whole safety argument of transport B is that exactly one file is replaced, and
        # that file does nothing but define three constants.
        self.assertEqual(transport_patched.SHADOW_MODULE, "main_globals.lua")
        self.assertEqual(
            transport_patched.EXPECTED_CONSTANTS,
            {"KR_PLATFORM": "mac", "KR_TARGET": "desktop", "KR_GAME": "kr1"},
        )

    def test_it_returns_the_games_own_constants(self):
        for name, value in transport_patched.EXPECTED_CONSTANTS.items():
            self.assertIn('{0} = "{1}"'.format(name, value), self.text)

    def test_it_compiles_in_the_games_luajit(self):
        if not oracle.available():
            self.skipTest("the game is not installed: the oracle is skipped, not failed")
        result = oracle.check(self.text, name="<bootstrap>", mode="load")
        self.assertTrue(result.get("ok"), result.get("error"))

    # Its *behaviour* is checked in tests/test_agent_integration.py::TestBootstrapInRealLua,
    # not here: the S8 oracle opens no libraries at all, so even `pcall` is missing and no
    # genuine module can run inside it.

    def test_the_archived_copy_wins_over_our_own_constants(self):
        """If the game ever adds a constant, the archived module must still supply it.

        The replacement loads the real bytecode copy when it can, so a game update that adds a
        key is inherited rather than dropped. In the oracle there is no `love.filesystem`, so
        this asserts the fallback *path* is present and correct instead of running it.
        """
        self.assertIn("love.filesystem", self.text)
        self.assertIn("krcheat/orig/main_globals.luac", self.text)
        self.assertIn("for key, value in pairs(result)", self.text)


class TestInstallation(Base):
    def test_install_then_uninstall_leaves_the_save_directory_as_it_was(self):
        transport = self.transport()
        before = sorted(os.listdir(self.savedir))
        written = transport.install()
        self.assertTrue(os.path.exists(written["shadow"]))
        self.assertTrue(os.path.exists(written["original"]))
        self.assertTrue(transport.installed())
        removed = transport.uninstall()
        self.assertIn(written["shadow"], removed)
        self.assertEqual(sorted(os.listdir(self.savedir)), before)
        self.assertFalse(transport.installed())
        # The directory we created for the archived copy must not be left behind empty.
        self.assertFalse(os.path.exists(os.path.join(self.savedir, "krcheat")))

    def test_install_is_idempotent(self):
        transport = self.transport()
        first = transport.install()
        second = transport.install()
        self.assertEqual(first["shadow"], second["shadow"])
        self.assertEqual(first["bytes"], second["bytes"])

    def test_uninstall_refuses_to_delete_a_file_it_did_not_write(self):
        # Someone else's `main_globals.lua` is not ours to remove, and silently deleting it
        # would be the worst possible way to find that out.
        foreign = os.path.join(self.savedir, transport_patched.SHADOW_MODULE)
        with open(foreign, "w", encoding="utf-8") as handle:
            handle.write("-- somebody else's file\nreturn {}\n")
        with self.assertRaises(UsageError):
            self.transport().uninstall()
        self.assertTrue(os.path.exists(foreign))

    def test_available_is_false_until_the_game_confirms_the_bootstrap_ran(self):
        transport = self.transport()
        self.assertFalse(transport.available()[0])
        transport.install()
        usable, why = transport.available()
        self.assertFalse(usable)
        self.assertIn("unverified", why)

    def test_a_negative_verdict_keeps_transport_b_unavailable(self):
        transport = self.transport()
        transport.install()
        transport.record_verdict(False, why="the game ignored it")
        usable, why = transport.available()
        self.assertFalse(usable)
        self.assertIn("did not load", why)

    def test_a_positive_verdict_makes_transport_b_available(self):
        transport = self.transport()
        transport.install()
        transport.record_verdict(True, how="update")
        usable, why = transport.available()
        self.assertTrue(usable)
        self.assertIn("update", why)

    def test_check_distinguishes_not_started_yet_from_failed(self):
        """The distinction that stops `--check` from declaring S2 dead on the first run."""
        transport = self.transport()
        transport.install()
        early = transport.check()
        self.assertFalse(early["loaded"])
        self.assertFalse(early["conclusive"], "a game that has not run is not a failure")
        self.assertIn("has not been started", early["reason"])

        # Now pretend the game ran: it writes its own files when it does.
        os.utime(
            os.path.join(self.savedir, "slot_1.lua"),
            (time.time() + 30, time.time() + 30),
        )
        late = transport.check()
        self.assertFalse(late["loaded"])
        self.assertTrue(late["conclusive"])
        self.assertEqual(transport.verdict()["loaded"], False)

    def test_check_records_what_the_evidence_file_says(self):
        transport = self.transport()
        transport.install()
        evidence = transport_patched.evidence_path(transport.save_dir())
        os.makedirs(os.path.dirname(evidence), exist_ok=True)
        with open(evidence, "w", encoding="utf-8") as handle:
            json.dump({"loaded": True, "hook": "draw", "version": "9.9.9"}, handle)
        payload = transport.check()
        self.assertTrue(payload["loaded"])
        self.assertEqual(payload["hook"], "draw")
        self.assertEqual(transport.verdict()["hook"], "draw")

    def test_start_explains_itself_instead_of_pretending(self):
        # Transport B's request/response files are read by the bootstrap, and only transport
        # A's reader is wired up in this build. Refusing is the honest answer.
        transport = self.transport()
        transport.install()
        transport.record_verdict(True, how="update")
        with self.assertRaises(ChannelUnavailable):
            transport.start(launch=True)


class TestKeeper(Base):
    """The process that makes an override outlive the command that set it (§11.7.4)."""

    def test_it_exits_when_the_game_is_gone(self):
        dead = _a_pid_that_is_gone()
        channel = protocol.Channel(dead)
        with open(channel.log_path, "w", encoding="utf-8") as handle:
            handle.write("stub\n")
        started = time.time()
        code = keeper.run(dead, heartbeat=0.3, keys=["gold"], quiet=True, sleep=lambda _s: None)
        self.assertEqual(code, 0)
        self.assertLess(time.time() - started, 10)

    def test_it_refuses_to_keep_a_channel_that_does_not_exist(self):
        missing = _a_pid_that_is_gone()
        shutil.rmtree(protocol.channel_dir(missing), ignore_errors=True)
        self.assertEqual(keeper.run(missing, quiet=True), 3)

    def test_it_records_its_own_pid_so_it_can_be_stopped(self):
        dead = _a_pid_that_is_gone()
        channel = protocol.Channel(dead)
        with open(channel.log_path, "w", encoding="utf-8") as handle:
            handle.write("stub\n")
        keeper.run(dead, heartbeat=0.3, keys=["gold"], quiet=True, sleep=lambda _s: None)
        # It cleans up after itself on the way out.
        self.assertIsNone(keeper.keeper_pid(channel.path))

    def test_describe_reports_a_running_keeper_and_a_dead_one_differently(self):
        channel = protocol.Channel(4242)
        pid_file = os.path.join(channel.path, "keeper.pid")
        try:
            os.remove(pid_file)
        except OSError:
            pass
        self.assertEqual(keeper.describe(channel.path), {"running": False})
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write("{0} gold\n".format(os.getpid()))
        self.assertTrue(keeper.describe(channel.path)["running"])
        stale = _a_pid_that_is_gone()
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write("{0} gold\n".format(stale))
        described = keeper.describe(channel.path)
        # A pidfile for a dead process is worth reporting rather than hiding: it is what a
        # user finds when they wonder whether a keeper is still holding their overrides.
        self.assertFalse(described["running"])
        self.assertEqual(described["pid"], stale)

    def test_stop_signals_the_recorded_pid(self):
        dead = _a_pid_that_is_gone()
        channel = protocol.Channel(4243)
        self.assertFalse(keeper.stop(channel_path=channel.path), "no keeper to stop")
        with open(os.path.join(channel.path, "keeper.pid"), "w", encoding="utf-8") as handle:
            handle.write("{0}\n".format(os.getpid()))
        self.assertTrue(keeper.stop(channel_path=channel.path))
        self.assertFalse(keeper.stop(pid=dead))


class TestPlayEnvironment(Base):
    """`krcheat play` sets the environment the agent needs, or it silently launches a game
    with no agent at all."""

    def test_launch_env_points_at_a_built_agent_and_steam_ids(self):
        from krcheat.core import live as live_pkg  # noqa: F401 - keeps the import path honest
        from krcheat.core.live import agent as agent_mod
        from krcheat.core.live.transport_dylib import LAUNCH_ENV, DylibTransport

        if not agent_mod.available()[0]:
            self.skipTest("no compiler, so there is no dylib to point at")
        ctx = self.ctx()
        transport = DylibTransport(ctx=ctx)
        try:
            env = transport.launch_env()
        except ChannelUnavailable as exc:
            self.skipTest("cannot build the agent here: {0}".format(exc))
        self.assertEqual(env["SteamAppId"], LAUNCH_ENV["SteamAppId"])
        self.assertEqual(env["DYLD_INSERT_LIBRARIES"], agent_mod.build())
        self.assertTrue(os.path.exists(env["DYLD_INSERT_LIBRARIES"]))
        self.assertTrue(env["DYLD_INSERT_LIBRARIES"].endswith(".dylib"))

    def test_the_agent_dylib_is_named_after_its_sources(self):
        """A stale dylib answering new Python is impossible by construction, not by luck."""
        if not agent_mod.available()[0]:
            self.skipTest("no compiler")
        first = agent_mod.fingerprint()
        self.assertEqual(first, agent_mod.fingerprint())
        self.assertNotEqual(first, agent_mod.fingerprint(sources_dir=self.tmp_sources()))

    def tmp_sources(self):
        directory = tempfile.mkdtemp(prefix="krcheat-fake-sources-")
        self.addCleanup(shutil.rmtree, directory, True)
        for name in agent_mod.SOURCES:
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write("/* different */\n")
        return directory

    def test_a_build_failure_explains_itself(self):
        from krcheat.core.live import agent as agent_mod

        broken = tempfile.mkdtemp(prefix="krcheat-broken-sources-")
        self.addCleanup(shutil.rmtree, broken, True)
        for name in agent_mod.SOURCES:
            with open(os.path.join(broken, name), "w", encoding="utf-8") as handle:
                handle.write("this is not C\n")
        if not agent_mod.available(broken)[0]:
            self.skipTest("no compiler")
        with self.assertRaises(ChannelUnavailable) as caught:
            agent_mod.build(force=True, sources_dir=broken)
        self.assertIn("compiler said", str(caught.exception))


def _a_pid_that_is_gone():
    probe = subprocess.Popen([sys.executable, "-c", "pass"])
    probe.wait()
    return probe.pid
