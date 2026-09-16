"""Regression tests for the bugs found in the review of 2026-09-16.

One test per finding, named after the behaviour that was wrong. Each of these failed
before the fix, which is the only reason they exist.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat import cli
from krcheat.core import config as config_mod, context as context_mod, data as data_mod
from krcheat.core import log as log_mod, paths, safety, state as state_mod
from krcheat.core.errors import ChannelUnavailable, NotFoundError, UsageError
from krcheat.core.result import Result

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "slot_synthetic.lua")


def fixture_text():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


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

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args) + ["--save-dir", self.savedir])
        return code, out.getvalue(), err.getvalue()

    def make_log(self, name):
        path = os.path.join(self.home, "logs", name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"event": "test"}\n')
        return path


class TestLogPruneProtectsTheOpenFile(Base):
    """Pruning used to be able to delete the log file the run was writing to.

    On macOS the open handle survives the unlink, so the run would keep "logging"
    into a file nobody can find — losing exactly the diagnostics it was collected for.
    """

    def test_prune_never_deletes_the_protected_file(self):
        keep = self.make_log("krcheat-20260101.jsonl")
        doomed = self.make_log("krcheat-20260102.jsonl")
        removed = log_mod.prune_logs(keep_days=0, max_bytes=0, protect=keep)
        self.assertTrue(os.path.exists(keep), "the protected log was removed")
        self.assertIn(doomed, removed)
        self.assertNotIn(keep, removed)

    def test_prune_without_protection_still_removes_everything_it_should(self):
        first = self.make_log("krcheat-20260101.jsonl")
        second = self.make_log("krcheat-20260102.jsonl")
        removed = log_mod.prune_logs(keep_days=0, max_bytes=0)
        self.assertEqual(sorted(removed), sorted([first, second]))

    def test_configure_protects_the_file_it_opens(self):
        target = os.path.join(self.home, "logs", "krcheat-20260303.jsonl")
        logger = log_mod.configure(
            level="info", path=target, keep_days=0, max_bytes=0, command="test"
        )
        try:
            logger.info("still.here")
            self.assertTrue(os.path.exists(target), "configure pruned its own log file")
            self.assertGreater(os.path.getsize(target), 0)
        finally:
            logger.close()

    def test_tail_reports_where_the_records_came_from(self):
        path = self.make_log("krcheat-20260404.jsonl")
        records, source = log_mod.tail_lines(count=5)
        self.assertEqual(source, path)
        self.assertEqual(len(records), 1)

    def test_tail_with_no_logs_at_all_is_empty_not_an_error(self):
        records, source = log_mod.tail_lines(count=5)
        self.assertEqual(records, [])
        self.assertIsNone(source)


class TestSessionSlotFallsBackToAsking(Base):
    """`Ctx.slot` passed the session value as *explicit*, so a session slot whose file had
    been deleted raised exit 2 for the rest of the session instead of asking again."""

    def test_a_deleted_session_slot_asks_again(self):
        ctx = self.ctx(interactive=True)
        ctx.session["slot"] = 9  # never existed / deleted since
        asked = []

        def ask(available):
            asked.append(available)
            return 1

        self.assertEqual(ctx.slot(ask=ask), 1)
        self.assertEqual(asked, [[1]])
        self.assertEqual(ctx.session["slot"], 1)

    def test_an_explicit_slot_is_still_a_hard_error(self):
        ctx = self.ctx(interactive=True)
        with self.assertRaises(NotFoundError):
            ctx.slot(explicit=9, ask=lambda available: 1)
        self.assertFalse(os.path.exists(os.path.join(self.savedir, "slot_9.lua")))

    def test_the_session_slot_is_used_when_the_file_is_still_there(self):
        ctx = self.ctx(interactive=False)
        ctx.session["slot"] = 1
        self.assertEqual(ctx.slot(), 1)

    def test_no_session_and_no_prompt_is_a_usage_error(self):
        ctx = self.ctx(interactive=False)
        with self.assertRaises(UsageError):
            ctx.slot()


class TestReferenceCommandsDoNotPrompt(Base):
    """`profile list` loaded a profile (and therefore prompted for a slot) even when the
    archive could answer the question. Asking "which slot?" before printing the game's own
    id list is noise, and in a pipe it was a usage error."""

    def test_list_levels_needs_no_slot_in_a_pipe(self):
        code, out, err = self.run_cli("profile", "list", "levels")
        self.assertEqual(code, 0, err)
        self.assertIn("1", out)

    def test_list_upgrades_needs_no_slot(self):
        code, out, err = self.run_cli("profile", "list", "upgrades")
        self.assertEqual(code, 0, err)
        self.assertIn("archers", out)

    def test_list_merges_a_save_when_a_slot_is_given(self):
        code, out, err = self.run_cli("--slot", "1", "profile", "list", "heroes")
        self.assertEqual(code, 0, err)
        self.assertIn("hero_magnus", out)

    def test_counters_still_need_a_slot_because_they_live_in_a_save(self):
        code, _out, err = self.run_cli("profile", "list", "counters")
        self.assertEqual(code, 1)
        self.assertIn("--slot", err)


class TestF15CommandsHonourTheGates(Base):
    """§10.6: `data *` writes into the game's *read* path, so it is subject to the same
    gates as a save edit. It was doing its own snapshot-and-write with no gates at all."""

    def shadow_path(self):
        return os.path.join(self.savedir, "kr1", "data", "levels", "level01_data.lua")

    def test_set_level_data_checks_the_gates_before_anything_else(self):
        calls = []
        original = safety.check_gates

        def refuse(*args, **kwargs):
            calls.append(kwargs)
            raise ChannelUnavailable("the game is running")

        ctx = self.ctx()
        safety.check_gates = refuse
        try:
            with self.assertRaises(ChannelUnavailable):
                data_mod.set_level_data(
                    ctx, Result(command="data.set"), 1, "starting_gold", 5,
                    save_dir=ctx.save_dir(),
                )
        finally:
            safety.check_gates = original
        self.assertEqual(len(calls), 1)
        self.assertFalse(os.path.exists(self.shadow_path()), "a file was written despite the gate")

    def test_revert_checks_the_gates_before_removing(self):
        os.makedirs(os.path.dirname(self.shadow_path()), exist_ok=True)
        with open(self.shadow_path(), "w", encoding="utf-8") as handle:
            handle.write("-- krcheat-f15 level=1 generated=now version=none\nreturn {}\n")
        original = safety.check_gates

        def refuse(*args, **kwargs):
            raise ChannelUnavailable("the game is running")

        safety.check_gates = refuse
        try:
            with self.assertRaises(ChannelUnavailable):
                data_mod.revert(self.ctx(), Result(command="data.revert"))
        finally:
            safety.check_gates = original
        self.assertTrue(os.path.exists(self.shadow_path()), "the override was removed anyway")

    def test_revert_with_nothing_installed_does_not_need_the_gates(self):
        result = data_mod.revert(self.ctx(), Result(command="data.revert"))
        self.assertTrue(result.notes)


class TestOracleRunsFromAnyDirectory(Base):
    """The oracle child was started with `-m krcheat.core.oracle`, which depends on the
    working directory or on the package being pip-installed."""

    def setUp(self):
        Base.setUp(self)
        from krcheat.core import oracle

        self.oracle = oracle
        if not oracle.available():
            self.skipTest("the game is not installed: oracle tests are skipped, not failed")

    def test_check_works_from_an_unrelated_working_directory(self):
        previous = os.getcwd()
        os.chdir(tempfile.gettempdir())
        try:
            result = self.oracle.check('return { ok = true, n = 41 + 1 }', mode="run")
        finally:
            os.chdir(previous)
        self.assertTrue(result.get("ok"), result.get("error"))
        self.assertEqual(result["value"], {"ok": True, "n": 42})

    def test_the_bootstrap_names_an_existing_package_parent(self):
        parent = self.oracle.PACKAGE_PARENT
        self.assertTrue(os.path.isdir(parent))
        self.assertTrue(os.path.isdir(os.path.join(parent, "krcheat")))
        self.assertIn(parent, self.oracle._bootstrap())


class TestProcessSnapshotExpires(Base):
    """A cached `ps` answer with no expiry meant a long-lived front-end (`krcheat gui`)
    reported whichever state the game was in when the window opened."""

    def test_the_snapshot_is_refreshed_after_the_ttl(self):
        paths._ps_output()
        paths._PS_CACHE["at"] = 0.0
        stale = paths._PS_CACHE["at"]
        paths._ps_output()
        self.assertGreater(paths._PS_CACHE["at"], stale)

    def test_a_zero_max_age_always_refreshes(self):
        paths._ps_output()
        paths._PS_CACHE["out"] = "poisoned"
        self.assertNotEqual(paths._ps_output(max_age=0), "poisoned")

    def test_the_ttl_is_short_enough_to_be_useful(self):
        self.assertLessEqual(paths._PS_TTL_SECONDS, 5.0)


if __name__ == "__main__":
    unittest.main()
