"""The Tk preflight and the GUI opt-in (D10, §7.4).

These tests must not start Tk in-process *or* in a subprocess on a machine where Tk is
broken: `abort()` produces a macOS crash-report dialog, and a test suite that pops dialogs
is worse than one with no Tk coverage. The static verdict is what makes that possible, and
`TestNoTkProcessIsEverStarted` asserts it directly by failing if anything spawns a child.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import config as config_mod, context as context_mod, log as log_mod
from krcheat.core import state as state_mod
from krcheat.core.errors import UsageError
from krcheat.gui import tkprobe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_home(enable_gui=False):
    """A throwaway KRCHEAT_HOME, with the GUI switched on or left at its default."""
    home = tempfile.mkdtemp(prefix="krcheat-test-home-")
    if enable_gui:
        with open(os.path.join(home, "config.ini"), "w", encoding="utf-8") as handle:
            handle.write("[ui]\nenabled = true\n")
    return home


class HomeCase(unittest.TestCase):
    """A context with its own home, save directory and (optionally) the GUI enabled."""

    GUI_ENABLED = False

    def setUp(self):
        self.previous_home = os.environ.get("KRCHEAT_HOME")
        self.previous_allow = os.environ.pop(tkprobe.ALLOW_ENV, None)
        self.home = make_home(enable_gui=self.GUI_ENABLED)
        os.environ["KRCHEAT_HOME"] = self.home
        self.savedir = tempfile.mkdtemp(prefix="krcheat-test-save-")
        self.ctx = context_mod.Ctx(
            command="gui",
            argv=["gui"],
            config=config_mod.Config.load(),
            state=state_mod.State.load(),
            log=log_mod.NullLogger(),
        )
        self.ctx.save_dir_override = self.savedir
        self.ctx.game_override = os.path.join(self.home, "not-installed", "Kingdom Rush.app")

    def tearDown(self):
        if self.previous_home is None:
            os.environ.pop("KRCHEAT_HOME", None)
        else:
            os.environ["KRCHEAT_HOME"] = self.previous_home
        if self.previous_allow is not None:
            os.environ[tkprobe.ALLOW_ENV] = self.previous_allow
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.savedir, ignore_errors=True)


class TestGuiIsOptIn(HomeCase):
    """Decision D10: macOS use is CLI-only, so the GUI is off unless asked for."""

    GUI_ENABLED = False

    def test_the_default_config_says_the_gui_is_off(self):
        self.assertIn("enabled = false", config_mod.DEFAULT_TEXT)
        self.assertIs(config_mod.DEFAULTS["ui.enabled"], False)
        self.assertFalse(config_mod.gui_enabled(self.ctx.config))

    def test_gui_refuses_and_names_the_config_key(self):
        from krcheat.cli import cmd_gui

        with self.assertRaises(UsageError) as caught:
            cmd_gui(self.ctx, None)
        message = caught.exception.message
        self.assertIn("ui.enabled", message)
        self.assertIn(config_mod.GUI_ENABLE_HINT, message)

    def test_gui_command_exits_1_without_touching_tk(self):
        from krcheat import cli

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["gui", "--save-dir", self.savedir])
        self.assertEqual(code, 1)
        self.assertIn("ui.enabled", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_enabling_it_flips_the_verdict(self):
        self.ctx.config.set("ui.enabled", True)
        self.assertTrue(config_mod.gui_enabled(self.ctx.config))

    def test_a_config_that_cannot_answer_counts_as_off(self):
        class Broken(object):
            def get_typed(self, key):
                raise ValueError("no")

        self.assertFalse(config_mod.gui_enabled(Broken()))

    def test_doctor_does_not_warn_about_a_component_you_do_not_use(self):
        from krcheat.core import doctor as doctor_mod

        result = doctor_mod.run(self.ctx)
        rows = [row for row in result.payload["checks"] if row["check"] == "tkinter"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pass")
        self.assertIn("not requested", rows[0]["detail"])
        self.assertFalse([item for item in result.warnings if "tkinter" in item.lower()])

    def test_doctor_reports_tk_again_once_the_gui_is_enabled(self):
        from krcheat.core import doctor as doctor_mod

        self.ctx.config.set("ui.enabled", True)
        result = doctor_mod.run(self.ctx)
        rows = [row for row in result.payload["checks"] if row["check"] == "tkinter"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("not requested", rows[0]["detail"])
        self.assertIn(rows[0]["status"], ("pass", "warn"))


class TestStaticVerdict(unittest.TestCase):
    def test_import_status_shape(self):
        imported, version, error = tkprobe.import_status()
        self.assertIsInstance(imported, bool)
        if imported:
            self.assertIsNone(error)
        else:
            self.assertIsNotNone(error)

    def test_static_verdict_is_a_decision(self):
        result = tkprobe.static_verdict()
        self.assertIn("usable", result)
        self.assertIn("certain", result)
        self.assertIn(result["usable"], (True, False))
        if not result["usable"]:
            self.assertTrue(result["reason"])

    def test_old_tk_on_macos_is_certainly_unusable(self):
        """The observed failure: Tk 8.5 on macOS 15 aborts, so we must not try it."""
        static = tkprobe.static_verdict()
        version = static.get("tk_version")
        macos = static.get("macos")
        if not (macos and version and float(version) < tkprobe.MIN_SAFE_TK):
            self.skipTest("Tk is new enough (or not macOS): nothing to refuse")
        self.assertFalse(static["usable"])
        self.assertTrue(static["certain"])
        self.assertIn("aborts", static["reason"])

    def test_verdict_never_probes_when_the_static_check_is_certain(self):
        static = tkprobe.static_verdict()
        if not static["certain"]:
            self.skipTest("the static check is not conclusive here")
        calls = []
        original = tkprobe.probe

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        os.environ.pop(tkprobe.ALLOW_ENV, None)
        tkprobe.probe = spy
        try:
            self.assertEqual(tkprobe.verdict(), static)
            self.assertEqual(tkprobe.summary().get("usable"), False)
            with self.assertRaises(UsageError):
                tkprobe.require()
        finally:
            tkprobe.probe = original
        self.assertEqual(calls, [], "a Tk process was started despite a certain verdict")

    def test_describe_is_a_sentence(self):
        text = tkprobe.describe()
        self.assertIsInstance(text, str)
        self.assertTrue(text)

    def test_the_escape_hatch_bypasses_the_check(self):
        os.environ[tkprobe.ALLOW_ENV] = "1"
        try:
            self.assertIsNone(tkprobe.require())
        finally:
            os.environ.pop(tkprobe.ALLOW_ENV, None)


class TestNoTkProcessIsEverStarted(HomeCase):
    """The regression guard for the crash-dialog bug."""

    GUI_ENABLED = True

    def test_doctor_does_not_spawn_a_tk_child(self):
        if not tkprobe.static_verdict()["certain"]:
            self.skipTest("Tk is plausible here; doctor would be allowed to probe")
        started = []
        saved = subprocess.run

        def spy(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            if isinstance(argv, (list, tuple)) and any("tkinter" in str(item) for item in argv):
                started.append(argv)
            return saved(*args, **kwargs)

        from krcheat.core import doctor as doctor_mod

        subprocess.run = spy
        try:
            doctor_mod.run(self.ctx)
        finally:
            subprocess.run = saved
        self.assertEqual(started, [], "doctor spawned a Tk process on an aborting Tk")

    def test_gui_run_refuses_instead_of_aborting(self):
        if tkprobe.verdict().get("usable"):
            self.skipTest("Tk works in this interpreter; running it would open a window")
        # The public entry point, which is what the CLI calls. It must refuse before
        # importing anything that needs tkinter, so a missing _tkinter is a message
        # rather than a ModuleNotFoundError.
        from krcheat import gui

        with self.assertRaises(UsageError) as caught:
            gui.run(self.ctx)
        self.assertIn("GUI is optional", caught.exception.message)

    def test_app_module_is_only_imported_after_the_preflight(self):
        """Regression guard: importing tkinter first turns a refusal into a traceback."""
        if tkprobe.verdict().get("usable"):
            self.skipTest("Tk works here, so the import order does not matter")
        source = (
            "import sys, krcheat.gui as g\n"
            "from krcheat.core import config, state, log, context\n"
            "ctx = context.Ctx(config=config.Config.load(), state=state.State.load(),"
            " log=log.NullLogger())\n"
            "try:\n"
            "    g.run(ctx)\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, 'krcheat.gui.app' in sys.modules)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", source],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=dict(os.environ, KRCHEAT_HOME=self.home),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stdout.decode().strip(), "UsageError False")

    def test_gui_module_is_not_imported_eagerly(self):
        # `import krcheat.gui` must not drag tkinter in: tiers 1-3 have no Tk dependency.
        completed = subprocess.run(
            [sys.executable, "-c", "import sys, krcheat.gui; print('tkinter' in sys.modules)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stdout.decode().strip(), "False")

    def test_gui_command_exits_1_without_a_traceback(self):
        if tkprobe.verdict().get("usable"):
            self.skipTest("Tk works here; running the command would open a window")
        from krcheat import cli

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["gui", "--save-dir", self.savedir])
        self.assertEqual(code, 1)
        self.assertIn("GUI is optional", err.getvalue())
        # A traceback here would mean the import order regressed (exit 6).
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
