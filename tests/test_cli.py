"""End-to-end CLI tests: dispatch, rendering, exit codes (§10)."""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat import cli
from krcheat.core import paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "slot_synthetic.lua")

NO_ORACLE = ["--no-oracle"]


def fixture_text():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        self.savedir = tempfile.mkdtemp(prefix="krcheat-test-save-")
        self.home_override = os.environ.get("KRCHEAT_HOME")
        os.environ["KRCHEAT_HOME"] = self.home
        shutil.copyfile(FIXTURE, os.path.join(self.savedir, "slot_1.lua"))

    def tearDown(self):
        if self.home_override is None:
            os.environ.pop("KRCHEAT_HOME", None)
        else:
            os.environ["KRCHEAT_HOME"] = self.home_override
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.savedir, ignore_errors=True)

    def run_cli(self, *args):
        """Run the CLI, returning (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        argv = list(args) + ["--save-dir", self.savedir]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def slot_path(self):
        return os.path.join(self.savedir, "slot_1.lua")

    def read_slot(self):
        with open(self.slot_path(), "r", encoding="utf-8") as handle:
            return handle.read()


class TestBasics(CliCase):
    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("krcheat", out.getvalue())

    def test_unknown_command_is_a_usage_error(self):
        code, _out, err = self.run_cli("frobnicate")
        self.assertEqual(code, 1)
        self.assertIn("frobnicate", err)

    def test_missing_argument_is_a_usage_error(self):
        code, _out, err = self.run_cli("profile", "get")
        self.assertEqual(code, 1)

    def test_no_command_prints_help(self):
        code, out, _err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("usage", out.lower())

    def test_unknown_global_flag_is_a_usage_error(self):
        code, _out, err = self.run_cli("--nonsense", "doctor")
        self.assertEqual(code, 1)


class TestProfile(CliCase):
    def test_show(self):
        code, out, _err = self.run_cli("--slot", "1", "profile", "show")
        self.assertEqual(code, 0)
        self.assertIn("version_string", out)
        self.assertIn("kr1-desktop-6.4.46", out)

    def test_get_and_json(self):
        code, out, _err = self.run_cli("--slot", "1", "--json", "profile", "get", "levels.1.stars")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["result"]["value"], 3)
        self.assertTrue(payload["ok"])

    def test_set_writes_and_snapshots(self):
        code, out, _err = self.run_cli(
            "--slot", "1", "profile", "set", "gems", "9999", *NO_ORACLE
        )
        self.assertEqual(code, 0)
        self.assertIn("snapshot:", out)
        self.assertEqual(self.read_slot(), fixture_text().replace('["gems"] = 100;', '["gems"] = 9999;'))
        self.assertTrue(os.listdir(os.path.join(self.home, "backups")))

    def test_dry_run_changes_nothing(self):
        before = self.read_slot()
        code, out, _err = self.run_cli(
            "--dry-run", "--slot", "1", "profile", "set", "gems", "9999", *NO_ORACLE
        )
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(self.read_slot(), before)
        backups = os.path.join(self.home, "backups")
        self.assertEqual(os.listdir(backups) if os.path.isdir(backups) else [], [])

    def test_no_op_is_reported_and_writes_nothing(self):
        code, out, _err = self.run_cli(
            "--slot", "1", "profile", "set", "gems", "100", *NO_ORACLE
        )
        self.assertEqual(code, 0)
        self.assertIn("no changes", out)

    def test_missing_slot_is_not_found(self):
        code, _out, err = self.run_cli("--slot", "7", "profile", "show")
        self.assertEqual(code, 2)
        self.assertIn("slot 7", err)

    def test_no_slot_non_interactive_is_a_usage_error(self):
        code, _out, err = self.run_cli("profile", "show")
        self.assertEqual(code, 1)
        self.assertIn("--slot", err)

    def test_unknown_path_is_a_usage_error(self):
        code, _out, err = self.run_cli("--slot", "1", "profile", "get", "nope.nope")
        self.assertEqual(code, 1)

    def test_out_of_range_difficulty_is_a_validation_error(self):
        code, _out, err = self.run_cli("--slot", "1", "profile", "set", "difficulty", "9", *NO_ORACLE)
        self.assertEqual(code, 4)
        self.assertIn("difficulty", err)

    def test_hero_skills_refused(self):
        code, _out, err = self.run_cli(
            "--slot", "1", "profile", "set", "hero", "hero_malik", "skills", "all=3", *NO_ORACLE
        )
        self.assertEqual(code, 3)
        self.assertIn("H6", err)

    def test_list_levels_needs_no_slot(self):
        code, out, _err = self.run_cli("profile", "list", "levels")
        self.assertEqual(code, 0)
        self.assertIn("1", out)

    def test_set_achievements_all(self):
        code, out, _err = self.run_cli(
            "--slot", "1", "profile", "set", "achievements", "all", *NO_ORACLE
        )
        self.assertEqual(code, 0)
        self.assertIn('["SLAYER"] = true;', self.read_slot())


class TestBackupAndDiagnostics(CliCase):
    def test_backup_list_is_empty_then_populated(self):
        code, out, _err = self.run_cli("backup", "list")
        self.assertEqual(code, 0)
        self.assertIn("no snapshots", out)
        self.run_cli("--slot", "1", "profile", "set", "gems", "5", *NO_ORACLE)
        code, out, _err = self.run_cli("backup", "list")
        self.assertIn("snapshot(s)", out)

    def test_restore_returns_the_original(self):
        self.run_cli("--slot", "1", "profile", "set", "gems", "5", *NO_ORACLE)
        code, out, _err = self.run_cli("backup", "restore", "latest")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_slot(), fixture_text())
        self.assertIn("restored from snapshot", out)

    def test_restore_unknown_id(self):
        code, _out, err = self.run_cli("backup", "restore", "nope")
        self.assertEqual(code, 2)

    def test_log_path_tail_and_prune(self):
        self.run_cli("--slot", "1", "profile", "show")
        code, out, _err = self.run_cli("log", "path")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith(".jsonl"))
        code, out, _err = self.run_cli("log", "tail", "--lines", "5")
        self.assertEqual(code, 0)
        self.assertIn("command.start", out)
        code, out, _err = self.run_cli("log", "prune")
        self.assertEqual(code, 0)

    def test_log_tail_is_json_when_asked(self):
        self.run_cli("--slot", "1", "profile", "show")
        code, out, _err = self.run_cli("--json", "log", "tail", "--lines", "3")
        payload = json.loads(out)
        self.assertEqual(payload["result"]["count"], 3)

    def test_debug_log_level_records_the_write_path(self):
        self.run_cli("--log-level", "debug", "--slot", "1", "profile", "set", "gems", "7", *NO_ORACLE)
        code, out, _err = self.run_cli("log", "tail", "--lines", "60", "--event", "write.")
        self.assertIn("write.snapshot", out)
        self.assertIn("write.verified", out)

    def test_no_log_writes_nothing(self):
        before = set(os.listdir(os.path.join(self.home, "logs"))) if os.path.isdir(
            os.path.join(self.home, "logs")
        ) else set()
        self.run_cli("--no-log", "--slot", "1", "profile", "show")
        after = set(os.listdir(os.path.join(self.home, "logs"))) if os.path.isdir(
            os.path.join(self.home, "logs")
        ) else set()
        self.assertEqual(before, after)

    def test_config_round_trip(self):
        code, _out, _err = self.run_cli("config", "set", "logging.level", "debug")
        self.assertEqual(code, 0)
        code, out, _err = self.run_cli("config", "get", "logging.level")
        self.assertIn("debug", out)
        code, out, _err = self.run_cli("config", "list")
        self.assertIn("safety.require_yes_when_steam_running", out)

    def test_config_rejects_an_unknown_key(self):
        code, _out, err = self.run_cli("config", "set", "nonsense.key", "1")
        self.assertEqual(code, 1)

    def test_live_status_reports_the_real_channel_state(self):
        code, out, _err = self.run_cli("live", "status")
        self.assertEqual(code, 0)
        # No game is running in the test environment, so the honest answer is "no channel"
        # plus the reason. What must not happen is a note about an unbuilt milestone: the
        # agent is implemented now, and status is how a user finds out what is missing.
        self.assertIn("channel: no", out)
        self.assertIn("overrides: none", out)
        self.assertIn("agent:", out)
        self.assertNotIn("not built in this build", out)

    def test_live_gold_refuses_with_exit_3(self):
        # Exit 3 is the channel-unavailable contract (§10.5). The reason is now that no game
        # is running, not that the feature does not exist, and the message has to say what to
        # do about it.
        code, _out, err = self.run_cli("live", "gold", "infinity")
        self.assertEqual(code, 3)
        self.assertIn("krcheat play", err)

    def test_patch_scan_is_backlog(self):
        code, _out, err = self.run_cli("patch", "scan", "265")
        self.assertEqual(code, 3)
        self.assertIn("M8", err)

    def test_self_test_passes(self):
        code, out, _err = self.run_cli("--self-test")
        self.assertEqual(code, 0)
        self.assertIn("0 failed", out)

    def test_doctor_reports_and_records_state(self):
        code, out, _err = self.run_cli("doctor")
        self.assertIn(code, (0, 2))
        self.assertIn("passed", out)
        self.assertTrue(os.path.exists(os.path.join(self.home, "state.json")))


class TestDataCommands(CliCase):
    def test_data_list_is_empty(self):
        code, out, _err = self.run_cli("data", "list")
        self.assertEqual(code, 0)
        self.assertIn("none", out)

    def test_set_and_revert_a_level_override(self):
        code, out, _err = self.run_cli(
            "data", "set", "level", "1", "starting_gold", "9999", *NO_ORACLE
        )
        self.assertEqual(code, 0)
        shadow = os.path.join(self.savedir, "kr1", "data", "levels", "level01_data.lua")
        self.assertTrue(os.path.exists(shadow))
        with open(shadow, "r", encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("-- krcheat-f15"))
        code, out, _err = self.run_cli("data", "list")
        self.assertIn("level 1", out)
        code, out, _err = self.run_cli("data", "revert", "--all", *NO_ORACLE)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(shadow))
        self.assertFalse(os.path.exists(os.path.join(self.savedir, "krcheat")))

    def test_wave_overrides_are_refused_with_a_reason(self):
        code, _out, err = self.run_cli("data", "set", "wave", "1", "gold", "5")
        self.assertEqual(code, 3)
        self.assertIn("S2", err)

    def test_unknown_level_field_is_a_usage_error(self):
        code, _out, err = self.run_cli("data", "set", "level", "1", "starting_ponies", "2")
        self.assertEqual(code, 1)


class TestHelpSurface(unittest.TestCase):
    """Every action must be visible in `--help`.

    argparse only lists a sub-action that was given `help=` text — without it the action is
    silently invisible, so `krcheat data --help` used to print nothing at all. The cheatsheet
    is checked against this surface, so a hidden action is a documentation bug waiting to
    happen.
    """

    def _subparser_actions(self, parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                yield action
                for child in action.choices.values():
                    for nested in self._subparser_actions(child):
                        yield nested

    def test_every_sub_action_has_help_text(self):
        parser = cli.build_parser()
        missing = []
        for action in self._subparser_actions(parser):
            documented = {item.dest for item in action._choices_actions}
            for name in sorted(set(action.choices)):
                if name not in documented:
                    missing.append("{0} {1}".format(action.dest or "<root>", name))
        self.assertEqual(missing, [], "hidden from --help: {0}".format(", ".join(missing)))

    def test_top_level_help_lists_every_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--help"])
        text = out.getvalue()
        for command in (
            "doctor", "profile", "backup", "log", "config", "data", "live", "agent", "play",
            "install", "uninstall", "repair", "patch", "self-test", "gui",
        ):
            self.assertIn(command, text)

    def test_the_cheatsheet_only_names_commands_that_exist(self):
        """The cheatsheet is a promise; this keeps it from drifting away from the parser.

        Two directions, both cheap: every command must be mentioned somewhere, and every
        `krcheat <word>` in the file must be a real command. Deliberately not a shell parser —
        a token immediately after `krcheat` is only checked when it is a bare word, so
        `krcheat --slot 1 profile show` is skipped rather than misread.
        """
        import re

        with open(os.path.join(REPO_ROOT, "CHEATSHEET.md"), "r", encoding="utf-8") as handle:
            text = handle.read()
        parser = cli.build_parser()
        commands = set()
        for action in self._subparser_actions(parser):
            commands.update(action.choices)

        mentioned = set(re.findall(r"\bkrcheat\s+([a-z][a-z-]*)", text))
        unknown = sorted(name for name in mentioned if name not in commands)
        self.assertEqual(unknown, [], "the cheatsheet names unknown commands: {0}".format(unknown))

        undocumented = sorted(
            name
            for name in ("live", "profile", "backup", "data", "config", "log", "doctor", "play")
            if name not in text
        )
        self.assertEqual(undocumented, [], "missing from the cheatsheet: {0}".format(undocumented))

    def test_data_help_lists_its_actions(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["data", "--help"])
        text = out.getvalue()
        for action in ("list", "set", "revert"):
            self.assertIn(action, text)


if __name__ == "__main__":
    unittest.main()
