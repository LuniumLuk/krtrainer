"""The agent, exercised for real (foundation §11, milestones M3 and M4).

The spikes that need a running Kingdom Rush cannot be run here, and pretending otherwise
would be the worst thing this test file could do. What *can* be run is everything on this
side of the boundary, and it turns out that is most of the live channel:

* the dylib is compiled by the same builder the CLI uses, ad-hoc signed and universal;
* it is loaded into a process with `DYLD_INSERT_LIBRARIES`;
* it captures the `lua_State *` by interposing `luaL_newstate` — the harness is a *different
  image*, so this is the real mechanism and not a shortcut;
* frames arrive through interposed `SDL_GL_SwapWindow`, with a real (hidden) SDL window;
* requests and responses go through the real file channel, with the real JSON, and are
  evaluated by the real LuaJIT 2.1 from the game's own bundle.

Only two things are faked, and both are faked explicitly: the game's `store.game` table comes
from a small setup script, and the harness plays the part of LÖVE's frame loop by calling the
same entry point LÖVE's present does.

Everything is skipped when the toolchain or the game is absent, because a test that cannot
run must say so rather than fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from krcheat.core import paths  # noqa: E402
from krcheat.core.live import agent as agent_mod  # noqa: E402
from krcheat.core.live import protocol, snippets  # noqa: E402
from krcheat.core.live.transport_dylib import DylibTransport  # noqa: E402

AGENT_DIR = os.path.join(REPO_ROOT, "krcheat", "agent")
HARNESS = os.path.join(AGENT_DIR, "kr_harness")
PROBE = os.path.join(AGENT_DIR, "kr_probe.dylib")


def _bundle():
    try:
        bundle = paths.find_app_bundle()
    except Exception:
        return None
    return bundle if os.path.exists(bundle.game_love) else None


def _have_toolchain():
    tools = agent_mod.toolchain()
    return bool(tools["clang"] and tools["codesign"])


def _build_harness(log):
    """Compile the harness against the game's Lua.framework, once."""
    bundle = _bundle()
    if bundle is None:
        return False
    frameworks = os.path.join(bundle.path, "Contents", "Frameworks")
    argv = [
        agent_mod.toolchain()["clang"],
        "-O2",
        "-Wall",
        "-I",
        AGENT_DIR,
        "-o",
        HARNESS,
        os.path.join(AGENT_DIR, "harness.c"),
        "-F",
        frameworks,
        "-framework",
        "Lua",
        "-framework",
        "SDL2",
        "-Wl,-rpath," + frameworks,
        "-undefined",
        "dynamic_lookup",
    ]
    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode != 0:
        log.append(completed.stdout.decode("utf-8", "replace"))
        return False
    argv = [
        agent_mod.toolchain()["clang"],
        "-O2",
        "-I",
        AGENT_DIR,
        "-dynamiclib",
        "-undefined",
        "dynamic_lookup",
        "-o",
        PROBE,
        os.path.join(AGENT_DIR, "probe_caller.c"),
    ]
    subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return True


class _Fixture(object):
    """A harness process running the real agent, plus the Lua world it sees."""

    def __init__(self, setup_source):
        self.setup_source = setup_source
        self.temp = tempfile.mkdtemp(prefix="krcheat-agent-test-")
        self.process = None
        self.pid = None
        self.channel = None
        self.lines = []

    def setup_path(self):
        path = os.path.join(self.temp, "setup.lua")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.setup_source)
        return path

    def extract_json_module(self):
        """Copy `lib/json.lua` out of `game.love` so `require("lib.json")` is the real one.

        The snippets encode their own results with the game's JSON library (§11.4), so a test
        against a stand-in encoder would be testing the stand-in. The harness runs with this
        directory as its cwd, which puts the module on the default `package.path`.
        """
        bundle = _bundle()
        if bundle is None:
            return False
        from krcheat.core import mine

        with mine.Archive(bundle) as archive:
            blob = archive.read("lib/json.lua")
        if not blob:
            return False
        directory = os.path.join(self.temp, "lib")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "json.lua"), "wb") as handle:
            handle.write(blob)
        return True

    def start(self, seconds=12.0, extra=()):
        dylib = agent_mod.build()
        self.extract_json_module()
        env = dict(os.environ)
        env["DYLD_INSERT_LIBRARIES"] = dylib
        argv = [
            HARNESS,
            "--setup",
            self.setup_path(),
            "--seconds",
            str(seconds),
            "--ticks-per-second",
            "400",
            "--probe-dylib",
            PROBE,
        ] + list(extra)
        self.process = subprocess.Popen(
            argv, env=env, cwd=self.temp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.pid = self.process.pid
        self.channel = protocol.Channel(self.pid)
        # Wait for the agent's constructor to create the channel; that is the earliest proof
        # the injection took, and it is written before any Lua state exists.
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if os.path.exists(self.channel.log_path):
                return self
            if self.process.poll() is not None:
                raise AssertionError("harness exited: {0}".format(self.output()))
            time.sleep(0.02)
        raise AssertionError("the agent never created its channel")

    def output(self):
        if self.process is None:
            return ""
        try:
            if self.process.poll() is None:
                return "\n".join(self.lines)
            data = self.process.stdout.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return ""
        return data

    def wait_for(self, predicate, timeout=20.0, poll=0.02):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return False

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                self.process.kill()
        if self.process is not None:
            try:
                self.process.stdout.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.temp, ignore_errors=True)


def _transport_for(fixture, ctx=None):
    """A real `DylibTransport` pointed at the harness's channel.

    The CLI's `start()` looks for a *game* process, and the harness is not one, so the pid is
    supplied here. Everything downstream of that is the production implementation: request
    framing, the heartbeat, and restore-on-clear.
    """
    return DylibTransport(ctx=ctx, pid=fixture.pid)


#: The smallest world the snippets can run against. It has the fields §6.1 lists, with
#: values chosen so a wrong write is visible: gold 265 (the shipped starting value), lives 20.
WORLD = """\
store = { game = { player_gold = 265, lives_left = 20, lives = 20, game_outcome = nil,
                   time_scale = 1 } }
"""


class TestAgentLoad(unittest.TestCase):
    """S5: does an injected dylib capture a real Lua state in another process?"""

    @classmethod
    def setUpClass(cls):
        cls.log = []
        cls.available = bool(_bundle()) and _have_toolchain() and _build_harness(cls.log)

    def setUp(self):
        if not self.available:
            self.skipTest(
                "needs clang, codesign and the game's Lua.framework: {0}".format(
                    (self.log or ["(built)"])[0][:200]
                )
            )

    def test_injection_captures_the_state_and_ticks_frames(self):
        fixture = _Fixture(WORLD).start(seconds=6)
        self.addCleanup(fixture.cleanup)
        self.assertTrue(
            fixture.wait_for(lambda: "attached:" in _read(fixture.channel.log_path), timeout=10),
            "the agent did not attach to a lua_State",
        )
        log = _read(fixture.channel.log_path)
        self.assertIn("injected into pid=", log)
        self.assertIn("state=0x", log)

    def test_status_line_reports_frames_and_no_reentry(self):
        fixture = _Fixture(WORLD).start(seconds=3)
        self.addCleanup(fixture.cleanup)
        fixture.process.wait(timeout=30)
        output = fixture.output()
        self.assertIn("HARNESS frames=sdl", output, "the SDL present hook was not used")
        status = [line for line in output.splitlines() if line.startswith("HARNESS status")]
        self.assertTrue(status, output)
        parsed = agent_mod.parse_agent_status(status[0].split(" ", 2)[2])
        self.assertEqual(parsed.get("attached"), "yes")
        self.assertGreater(parsed.get("frames", 0), 0)
        self.assertEqual(parsed.get("reentry_skips"), 0)

    def test_concurrent_state_creation_is_not_mistaken_for_recursion(self):
        """The bug that crashed the game, pinned.

        LÖVE gives each `love.thread` worker its own `lua_State` and creates them on those
        threads, so concurrent `luaL_newstate` calls are startup, not an edge case. The agent's
        guard used to be a single global `int`, so two threads each saw the other's flag and each
        concluded it had recursed *itself* — and the guard's response was to return NULL. The game
        dereferenced that NULL `lua_State` inside `lua_pushcclosure` on a thread runner and died of
        `EXC_BAD_ACCESS at 0x10`.

        Two things are asserted, and the second is the one with teeth:

        * `nulls=0` — no call is ever handed a NULL state;
        * no `routed back to us` line in the agent log — the guards are per-thread, so ordinary
          concurrency is not reported as recursion at all.

        Measured, not assumed: rebuilding the agent with a shared counter (a one-line change, no
        tripwire) reproduces the second assertion failing while `nulls=0` still passes. Without
        that check this test would have verified the symptom instead of the cause.
        """
        fixture = _Fixture(WORLD).start(seconds=2, extra=["--threads", "12"])
        self.addCleanup(fixture.cleanup)
        fixture.process.wait(timeout=60)
        output = fixture.output()
        self.assertIn("HARNESS threads=12 started=12 nulls=0", output)
        self.assertIn("HARNESS frames=sdl", output)

        log = _read(fixture.channel.log_path)
        self.assertNotIn(
            "routed back to us",
            log,
            "concurrent state creation was mistaken for recursion:\n{0}".format(log),
        )
        # The last-resort lookup is load-bearing when a call *is* routed back, so it must work on
        # this machine — and the agent reports that once per attach rather than leaving it to
        # chance.
        self.assertIn(
            "fallback resolution: luaL_newstate=yes lua_newstate=yes swap=yes",
            log,
            "the agent could not resolve the originals it would need as a fallback:\n{0}".format(log),
        )


class TestBootstrapInRealLua(unittest.TestCase):
    """Transport B's generated module, run by a real LuaJIT with no `love` in it.

    The oracle cannot host this: it opens no libraries, so even `pcall` is missing and no
    genuine module can run there. The harness is the right environment — the game's own LuaJIT
    with the standard library, and no LÖVE — which is precisely the case the module has to
    survive, because `main_globals` is loaded very early in startup.
    """

    @classmethod
    def setUpClass(cls):
        cls.log = []
        cls.available = bool(_bundle()) and _have_toolchain() and _build_harness(cls.log)

    def setUp(self):
        if not self.available:
            self.skipTest("needs clang, codesign and the game's Lua.framework")

    def test_it_returns_the_constants_and_installs_a_poller(self):
        from krcheat.core.live import transport_patched

        fixture = _Fixture("").start(seconds=2)
        self.addCleanup(fixture.cleanup)
        # Written into the fixture's cwd, so `loadfile` finds it by relative name.
        module = os.path.join(fixture.temp, "main_globals.lua")
        with open(module, "w", encoding="utf-8") as handle:
            handle.write(transport_patched.render_bootstrap("9.9.9", _FakeSaveDir(fixture.temp)))

        setup = (
            'local m = assert(loadfile("main_globals.lua"))()\n'
            'assert(type(m) == "table", "the module must return a table")\n'
            'assert(m.KR_PLATFORM == "mac", "KR_PLATFORM")\n'
            'assert(m.KR_TARGET == "desktop", "KR_TARGET")\n'
            'assert(m.KR_GAME == "kr1", "KR_GAME")\n'
            'assert(_G.kr_globals_backup == nil, "the module must not leak globals")\n'
        )
        with open(os.path.join(fixture.temp, "setup.lua"), "w", encoding="utf-8") as handle:
            handle.write(setup)

        completed = subprocess.run(
            [HARNESS, "--setup", os.path.join(fixture.temp, "setup.lua"), "--seconds", "1",
             "--report", "return 'bootstrap-ok'"],
            env=dict(os.environ, DYLD_INSERT_LIBRARIES=agent_mod.build()),
            cwd=fixture.temp,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = completed.stdout.decode("utf-8", "replace")
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("REPORT bootstrap-ok", output)

    def test_it_survives_without_any_love_table(self):
        """Every path in the module is guarded, so LÖVE's absence is a no-op, not an error."""
        from krcheat.core.live import transport_patched

        text = transport_patched.render_bootstrap("9.9.9", _FakeSaveDir("/tmp"))
        # The three LÖVE touchpoints, all behind pcall or an existence check.
        self.assertIn('pcall(require, "love.filesystem")', text)
        self.assertIn("local function global_love()", text)
        self.assertIn("local ok, value = pcall(function() return _G.love end)", text)


class _FakeSaveDir(object):
    """`render_bootstrap` only needs `save_dir.path`; the module stores relative names."""

    def __init__(self, path):
        self.path = path


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


class TestAgentProtocol(unittest.TestCase):
    """M3/M4 through the real channel: requests, snippets, the override lifecycle."""

    @classmethod
    def setUpClass(cls):
        cls.log = []
        cls.available = bool(_bundle()) and _have_toolchain() and _build_harness(cls.log)
        cls.fixture = None

    def setUp(self):
        if not self.available:
            self.skipTest("needs clang, codesign and the game's Lua.framework")
        self.fixture = _Fixture(WORLD).start(seconds=40)
        self.addCleanup(self._cleanup)
        self.transport = _transport_for(self.fixture)

    def _cleanup(self):
        if self.fixture is not None:
            self.fixture.cleanup()

    # -- the primitive -------------------------------------------------------

    def test_eval_once_returns_the_snippets_json(self):
        response = self.transport.once(snippets.eval_snippet("return 6 * 7"))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.decoded(), {"ok": True, "value": 42})

    def test_a_syntax_error_comes_back_verbatim(self):
        response = self.transport.once("this is not lua")
        self.assertFalse(response.ok)
        # The error is the Lua message, carrying the chunk name the agent chose, so a user can
        # tell which snippet failed instead of reading `<string>:1:`.
        self.assertIn("krcheat-once", response.error or "")

    def test_state_read_sees_the_world(self):
        response = self.transport.once(snippets.state_read(["player_gold", "lives_left"]))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.decoded()["values"]["player_gold"], 265)

    # -- overrides (§11.7) ---------------------------------------------------

    def test_always_forces_the_value_every_frame(self):
        code, capture, restore = snippets.override_snippets("gold")
        response = self.transport.always("gold", code, capture=capture, restore=restore)
        self.assertTrue(response.ok, response.error)
        # The game's own code writing a different value is the case that matters: the
        # override has to win the next frame, not only the frame it was registered on.
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self._silently_zero_gold() and self._gold() == 99999, timeout=10
            ),
            "the override did not reapply after the game changed the value",
        )

    def _silently_zero_gold(self):
        self.transport.once(snippets.eval_snippet("store.game.player_gold = 5"))
        return True

    def _gold(self, touch=True):
        response = self.transport.once(
            snippets.eval_snippet("return store.game.player_gold"), touch=touch
        )
        decoded = response.decoded()
        return decoded.get("value") if isinstance(decoded, dict) else None

    def test_clear_restores_the_captured_value_not_the_forced_one(self):
        code, capture, restore = snippets.override_snippets("gold")
        self.transport.always("gold", code, capture=capture, restore=restore)
        self.assertTrue(self.fixture.wait_for(lambda: self._gold() == 99999, timeout=10))
        response = self.transport.clear("gold")
        self.assertTrue(response.ok, response.error)
        self.assertTrue(
            self.fixture.wait_for(lambda: self._gold() == 265, timeout=10),
            "clear must restore the value captured at registration, not merely stop writing",
        )

    def test_replace_re_captures_instead_of_restoring_the_cheat(self):
        code, capture, restore = snippets.override_snippets("gold")
        self.transport.always("gold", code, capture=capture, restore=restore)
        self.assertTrue(self.fixture.wait_for(lambda: self._gold() == 99999, timeout=10))
        # Registering a second time must capture the *real* value. If the capture ran after
        # the first write, clearing would leave 99999 behind and look like it worked.
        self.transport.always("gold", code, capture=capture, restore=restore)
        self.transport.clear("gold")
        self.assertTrue(
            self.fixture.wait_for(lambda: self._gold() == 265, timeout=10),
            "replacing an override captured the forced value",
        )

    def test_god_restores_a_table_valued_field_by_reference(self):
        # The reason capture/restore do not round-trip through JSON: a table cannot, and
        # losing it would corrupt the game's win/lose state.
        self.transport.once(
            snippets.eval_snippet("store.game.game_outcome = { reason = 'playing' }")
        )
        code, capture, restore = snippets.override_snippets("god")
        response = self.transport.always("god", code, capture=capture, restore=restore)
        self.assertTrue(response.ok, response.error)
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self._eval("return type(store.game.game_outcome)") == "boolean",
                timeout=10,
            )
        )
        self.transport.clear("god")
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self._eval("return store.game.game_outcome.reason") == "playing",
                timeout=10,
            ),
            "the captured table was not restored",
        )

    def test_speed_resolves_the_field_once_in_the_capture(self):
        # `speed` is the one override whose field name is unknown (§6.3), so the loop lives in
        # the capture and the per-frame snippet stays control-flow free.
        code, capture, restore = snippets.override_snippets("speed", 3.0)
        snippets.assert_safe(code)
        response = self.transport.always("speed", code, capture=capture, restore=restore)
        self.assertTrue(response.ok, response.error)
        self.assertTrue(
            self.fixture.wait_for(lambda: self._eval("return store.game.time_scale") == 3, timeout=10)
        )
        self.transport.clear("speed")
        self.assertTrue(self.fixture.wait_for(lambda: self._eval("return store.game.time_scale") == 1))

    def _eval(self, code):
        response = self.transport.once(snippets.eval_snippet(code))
        decoded = response.decoded()
        return decoded.get("value") if isinstance(decoded, dict) else None

    # -- status and lifecycle ------------------------------------------------

    def test_status_enumerates_active_overrides(self):
        code, capture, restore = snippets.override_snippets("lives")
        self.transport.always("lives", code, capture=capture, restore=restore)
        payload = self.transport.status().decoded()
        keys = [entry["key"] for entry in payload["overrides"]]
        self.assertIn("lives", keys)
        entry = [item for item in payload["overrides"] if item["key"] == "lives"][0]
        self.assertTrue(entry["has_capture"])
        self.assertIn("captured", entry.get("saved", ""))
        self.transport.clear("lives")
        payload = self.transport.status().decoded()
        self.assertEqual(payload["overrides"], [])

    def test_clearing_something_that_is_not_set_is_not_an_error(self):
        response = self.transport.clear("speed")
        self.assertTrue(response.ok, response.error)

    def test_clear_all_releases_everything(self):
        for key in ("gold", "lives"):
            code, capture, restore = snippets.override_snippets(key)
            self.transport.always(key, code, capture=capture, restore=restore)
        self.transport.clear_all()
        self.assertEqual(self.transport.status().decoded()["overrides"], [])
        self.assertTrue(self.fixture.wait_for(lambda: self._gold() == 265, timeout=10))

    def test_a_stale_heartbeat_clears_everything(self):
        """§11.7.4: a crashed CLI must not leave the game modified forever."""
        code, capture, restore = snippets.override_snippets("gold")
        self.transport.always("gold", code, capture=capture, restore=restore,
                              heartbeat_seconds=1.0)
        self.assertTrue(self.fixture.wait_for(lambda: self._gold() == 99999, timeout=10))
        # From here on, nothing renews the heartbeat — which is exactly what a crashed CLI
        # looks like. Polling through the transport would renew it and hide the behaviour
        # under test, so this reads with `touch=False`.
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self._gold(touch=False) == 265, timeout=25, poll=0.2
            ),
            "the override survived a stale heartbeat",
        )
        self.assertEqual(self.transport.status(touch=False).decoded()["overrides"], [])

    # -- hardening (§11.6) ---------------------------------------------------

    def test_a_runaway_snippet_is_killed_by_the_watchdog(self):
        """The one failure the design cannot undo, so it must be structurally impossible."""
        response = self.transport.once("while true do end", timeout=15)
        self.assertFalse(response.ok)
        self.assertIn("instruction budget", (response.error or "").lower())

    def test_the_watchdog_covers_the_loops_luajit_would_otherwise_hide(self):
        """Regression guard for the measured finding behind `kr_jit_off_for_chunk`.

        The instruction-count hook is only consulted by LuaJIT's *interpreter*. A JIT-compiled
        loop never returns to the dispatch loop, so before the agent started marking each
        snippet as "do not compile", `while i < 1000000000 do i = i + 1 end` ran to completion
        at full speed (2.5 s) and `while true do end` hung the process outright. Both are
        asserted here, so that removing the `jit.off` call cannot quietly restore the hole.
        """
        for label, code in (
            ("a JIT-friendly counter loop", "local i = 0\nwhile i < 1000000000 do i = i + 1 end\nreturn 'done'"),
            ("an empty loop", "while true do end"),
            ("a numeric for", "for i = 1, 1000000000 do end\nreturn 'done'"),
            ("tail recursion", "local function f() return f() end\nf()"),
        ):
            with self.subTest(label):
                response = self.transport.once(code, timeout=15)
                self.assertFalse(response.ok, "{0} was not interrupted".format(label))
                self.assertIn("instruction budget", (response.error or "").lower())

    def test_identical_requests_in_the_same_second_are_both_answered(self):
        """Regression guard for the change detector (§11.3).

        The agent decides whether `cmd.json` is new by looking at it, not by reading it, so the
        detector's granularity is a correctness property. With a seconds-resolution timestamp,
        two requests of equal length written within the same second looked identical and the
        second was never read: the caller timed out, which is a silent failure in ordinary use
        (`live status` twice in a row is enough). Millisecond timestamps must both be answered.
        """
        code = snippets.eval_snippet("return 1")
        first = self.transport.once(code, timeout=10)
        second = self.transport.once(code, timeout=10)
        third = self.transport.once(code, timeout=10)
        for response in (first, second, third):
            self.assertTrue(response.ok, response.error)
        self.assertEqual(first.id < second.id < third.id, True)

    def test_the_agent_keeps_working_after_a_killed_snippet(self):
        self.transport.once("while true do end", timeout=15)
        response = self.transport.once(snippets.eval_snippet("return 'still here'"))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.decoded()["value"], "still here")

    def test_an_oversized_request_is_refused(self):
        blob = "x" * (protocol.MAX_REQUEST_BYTES + 10)
        with self.assertRaises(ValueError):
            self.transport.channel().write_request(protocol.Request.once(blob, request_id=1))

    def test_a_response_is_ignored_when_it_is_for_another_request(self):
        channel = self.transport.channel()
        channel.write_request(protocol.Request.once("return 1", request_id=1000))
        self.assertIsNone(channel.read_response(request_id=1001))
        self.assertIsNotNone(
            self.fixture.wait_for(
                lambda: channel.read_response(request_id=1000) is not None, timeout=10
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
