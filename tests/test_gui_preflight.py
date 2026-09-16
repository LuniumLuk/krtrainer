"""The Tk preflight (§7.4).

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

    def test_require_refuses_or_returns_a_verdict(self):
        os.environ.pop(tkprobe.ALLOW_ENV, None)
        result = tkprobe.verdict()
        if result.get("usable"):
            self.assertIsNotNone(tkprobe.require())
        else:
            with self.assertRaises(UsageError) as caught:
                tkprobe.require()
            self.assertIn("CLI", caught.exception.message)

    def test_the_escape_hatch_bypasses_the_check(self):
        os.environ[tkprobe.ALLOW_ENV] = "1"
        try:
            self.assertIsNone(tkprobe.require())
        finally:
            os.environ.pop(tkprobe.ALLOW_ENV, None)


class TestNoTkProcessIsEverStarted(unittest.TestCase):
    """The regression guard for the crash-dialog bug."""

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

        subprocess.run = spy
        home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        previous = os.environ.get("KRCHEAT_HOME")
        os.environ["KRCHEAT_HOME"] = home
        try:
            from krcheat.core import doctor as doctor_mod

            ctx = context_mod.Ctx(
                command="doctor",
                argv=["doctor"],
                config=config_mod.Config.load(),
                state=state_mod.State.load(),
                log=log_mod.NullLogger(),
            )
            ctx.game_override = os.path.join(home, "not-installed", "Kingdom Rush.app")
            ctx.save_dir_override = home
            doctor_mod.run(ctx)
        finally:
            subprocess.run = saved
            if previous is None:
                os.environ.pop("KRCHEAT_HOME", None)
            else:
                os.environ["KRCHEAT_HOME"] = previous
            shutil.rmtree(home, ignore_errors=True)
        self.assertEqual(started, [], "doctor spawned a Tk process on an aborting Tk")

    def test_gui_run_refuses_instead_of_aborting(self):
        if tkprobe.verdict().get("usable"):
            self.skipTest("Tk works in this interpreter; running it would open a window")
        home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        previous = os.environ.get("KRCHEAT_HOME")
        os.environ["KRCHEAT_HOME"] = home
        os.environ.pop(tkprobe.ALLOW_ENV, None)
        try:
            ctx = context_mod.Ctx(
                command="gui",
                argv=["gui"],
                config=config_mod.Config.load(),
                state=state_mod.State.load(),
                log=log_mod.NullLogger(),
            )
            from krcheat.gui import app as gui_app

            with self.assertRaises(UsageError):
                gui_app.run(ctx)
        finally:
            if previous is None:
                os.environ.pop("KRCHEAT_HOME", None)
            else:
                os.environ["KRCHEAT_HOME"] = previous
            shutil.rmtree(home, ignore_errors=True)

    def test_gui_module_is_not_imported_eagerly(self):
        # `import krcheat.gui` must not drag tkinter in: tiers 1-3 have no Tk dependency.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        completed = subprocess.run(
            [sys.executable, "-c", "import sys, krcheat.gui; print('tkinter' in sys.modules)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stdout.decode().strip(), "False")


class TestCliGuiPath(unittest.TestCase):
    def test_gui_command_exits_1_without_aborting(self):
        if tkprobe.verdict().get("usable"):
            self.skipTest("Tk works here; running the command would open a window")
        from krcheat import cli

        home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        savedir = tempfile.mkdtemp(prefix="krcheat-test-save-")
        previous = os.environ.get("KRCHEAT_HOME")
        os.environ["KRCHEAT_HOME"] = home
        os.environ.pop(tkprobe.ALLOW_ENV, None)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["gui", "--save-dir", savedir])
            self.assertEqual(code, 1)
            self.assertIn("CLI", err.getvalue())
        finally:
            if previous is None:
                os.environ.pop("KRCHEAT_HOME", None)
            else:
                os.environ["KRCHEAT_HOME"] = previous
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(savedir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
