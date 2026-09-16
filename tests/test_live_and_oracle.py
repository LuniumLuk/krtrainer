"""Snippets, the channel protocol, and the S8 oracle (§11, §16.2).

Oracle tests are **skipped, not failed**, when the game is not installed: the oracle
enhances the tool, it is not required by it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import lua_table as lt, oracle, paths
from krcheat.core.errors import UsageError
from krcheat.core.live import protocol, snippets


def oracle_available():
    return oracle.available()


class TestSnippets(unittest.TestCase):
    def test_every_template_encodes_its_result(self):
        for name, code in snippets.all_templates().items():
            if name.startswith("restore_"):
                continue  # asserted below to be the exception, on purpose
            with self.subTest(name=name):
                self.assertIn("json.encode", code)

    def test_restore_templates_do_not_depend_on_the_games_json_module(self):
        # A deliberate asymmetry (§11.7.3). Capture *reports* what it stored as JSON, but the
        # stored originals are Lua references, and restore uses only those. Restoring is what
        # runs when a user turns a cheat off or the heartbeat expires, so it must not be able
        # to fail because `lib/json` was unavailable or a value was not encodable.
        for key in ("gold", "lives", "speed", "god"):
            with self.subTest(key=key):
                restore = snippets.restore_for(key)
                self.assertNotIn("json", restore)
                self.assertIn("__krcheat_saved", restore)
                self.assertIn("__krcheat_key", restore)
                self.assertIn("json.encode", snippets.capture_for(key))

    def test_override_snippets_are_built_as_a_matching_triple(self):
        for key in ("gold", "lives", "speed", "god"):
            with self.subTest(key=key):
                code, capture, restore = snippets.override_snippets(key)
                self.assertEqual(code, snippets.for_override(key))
                self.assertEqual(capture, snippets.capture_for(key))
                self.assertEqual(restore, snippets.restore_for(key))
                # Both halves must agree about which fields are involved, or `off` puts back
                # the wrong value and looks like it worked.
                for field in snippets.FIELDS_FOR_OVERRIDE.get(key, ()):
                    self.assertIn(field, capture)
                    self.assertIn(field, restore)

    def test_no_per_frame_template_contains_control_flow(self):
        for name, code in snippets.per_frame_templates().items():
            with self.subTest(name=name):
                self.assertEqual(snippets.assert_safe(code), code)

    def test_once_only_templates_are_not_required_to_be_loop_free(self):
        # `probe` walks the game's tables, so it loops. It is `once`-only, and a loop
        # that runs once and returns cannot hang the main thread.
        self.assertIn("for", snippets.probe())
        self.assertIn("probe", snippets.ONCE_ONLY)
        self.assertNotIn("probe", snippets.PER_FRAME)

    def test_loop_guard_rejects_loops(self):
        for bad in (
            "while true do end",
            "for i = 1, 10 do end",
            "repeat x = 1 until true",
            "goto done",
            "::done::",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(UsageError):
                    snippets.assert_safe(bad)

    def test_loop_guard_ignores_words_inside_strings_and_comments(self):
        snippets.assert_safe('local s = "for the win"\n-- while nothing\nreturn json.encode({})')

    def test_gold_forces_a_readback(self):
        code = snippets.gold_infinity()
        self.assertIn("readback", code)
        self.assertIn("assignment did not take", code)

    def test_owner_can_be_overridden(self):
        code = snippets.gold_infinity(owner="GAME")
        self.assertIn("local s = GAME", code)
        self.assertIn("s.player_gold", code)

    def test_for_override_covers_the_four_keys(self):
        for key in ("gold", "lives", "speed", "god"):
            code = snippets.for_override(key)
            snippets.assert_safe(code)
            self.assertIn("json.encode", code)

    def test_for_override_rejects_unknown_keys(self):
        with self.assertRaises(UsageError):
            snippets.for_override("teleport")

    def test_eval_snippet_passes_code_through(self):
        code = snippets.eval_snippet("return 1 + 1")
        self.assertIn("return 1 + 1", code)
        with self.assertRaises(UsageError):
            snippets.eval_snippet("   ")


class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-tmp-")
        self.previous_tmpdir = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = self.home
        # `tempfile` caches its answer for the life of the process, so a test that changes TMPDIR
        # would otherwise decide where *later* tests look for a channel. The channel path itself
        # no longer goes through `tempfile` (`protocol.channel_root` matches the agent), and this
        # keeps the cache from leaking into anything else that does.
        self.cached_tempdir = tempfile.tempdir
        tempfile.tempdir = None

    def tearDown(self):
        if self.previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = self.previous_tmpdir
        tempfile.tempdir = self.cached_tempdir
        shutil.rmtree(self.home, ignore_errors=True)

    def test_always_and_clear_require_a_key(self):
        with self.assertRaises(ValueError):
            protocol.Request("code", mode="always")
        with self.assertRaises(ValueError):
            protocol.Request(None, mode="clear")
        self.assertEqual(protocol.Request.always("gold", "code").key, "gold")
        self.assertIsNone(protocol.Request.clear("gold").code)

    def test_request_handoff_is_a_rename(self):
        channel = protocol.Channel(4242)
        channel.reset()
        payload = channel.write_request(protocol.Request.once("return 1", request_id=7))
        self.assertEqual(payload["id"], 7)
        self.assertTrue(os.path.exists(channel.cmd_path))
        self.assertFalse(os.path.exists(channel.cmd_path + ".tmp"))

    def test_oversized_request_is_refused(self):
        channel = protocol.Channel(4243)
        with self.assertRaises(ValueError):
            channel.write_request(protocol.Request.once("x" * (protocol.MAX_REQUEST_BYTES + 1)))

    def test_response_is_ignored_when_it_is_for_another_request(self):
        channel = protocol.Channel(4244)
        with open(channel.out_path, "w", encoding="utf-8") as handle:
            json.dump({"id": 1, "ok": True, "result": "{}"}, handle)
        self.assertIsNone(channel.read_response(request_id=2))
        self.assertTrue(channel.read_response(request_id=1).ok)

    def test_response_decodes_the_snippets_json(self):
        response = protocol.Response.from_json(
            {"id": 1, "ok": True, "result": '{"gold": 265}', "ms": 0.4}
        )
        self.assertEqual(response.decoded(), {"gold": 265})
        self.assertEqual(response.to_dict()["result"], {"gold": 265})

    def test_unreadable_response_is_reported_not_raised(self):
        channel = protocol.Channel(4245)
        with open(channel.out_path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        response = channel.read_response(request_id=1)
        self.assertFalse(response.ok)
        self.assertIn("unreadable", response.error)

    def test_heartbeat_age(self):
        channel = protocol.Channel(4246)
        channel.touch_heartbeat()
        self.assertIsNotNone(channel.heartbeat_age())
        self.assertLess(channel.heartbeat_age(), 5)

    def test_a_write_removes_the_previous_response(self):
        """§11.3: a response is consumed exactly once.

        Without this, a caller that asks twice in a row can read the *first* answer as the
        answer to its second question — which is not hypothetical: it is what made every
        `live` command report the result of the command before it.
        """
        channel = protocol.Channel(4247)
        channel.write_request(protocol.Request.once("return 1", request_id=1))
        with open(channel.out_path, "w", encoding="utf-8") as handle:
            json.dump({"id": 1, "ok": True, "result": '{"first": true}', "_test": True}, handle)
        self.assertIsNotNone(channel.read_response(request_id=1))
        channel.write_request(protocol.Request.once("return 2", request_id=2))
        self.assertIsNone(
            channel.read_response(request_id=2), "a stale response survived a new request"
        )

    def test_channels_only_list_processes_that_still_exist(self):
        live = os.getpid()
        dead = _a_pid_that_is_gone()
        protocol.Channel(live)
        protocol.Channel(dead)
        pids = [item["pid"] for item in protocol.list_channels()]
        self.assertIn(live, pids)
        self.assertNotIn(dead, pids)
        self.assertIn(dead, [item["pid"] for item in protocol.stale_channels()])

    def test_pruning_is_age_gated_and_removes_only_dead_channels(self):
        live = os.getpid()
        dead = _a_pid_that_is_gone()
        fresh = _a_pid_that_is_gone()
        protocol.Channel(live)
        protocol.Channel(dead)
        protocol.Channel(fresh)
        # A channel that appeared seconds ago usually means a process still starting up, so a
        # young one must survive even though its pid is gone.
        self.assertNotIn(dead, protocol.prune_channels(min_age=3600))
        removed = protocol.prune_channels(min_age=0.0)
        self.assertIn(dead, removed)
        self.assertIn(fresh, removed)
        self.assertNotIn(live, removed)
        self.assertTrue(os.path.isdir(protocol.channel_dir(live)))


def _a_pid_that_is_gone():
    """A pid that no longer exists. `os.fork` is not used: this must work anywhere."""
    probe = subprocess.Popen([sys.executable, "-c", "pass"])
    probe.wait()
    return probe.pid


class TestOracle(unittest.TestCase):
    def setUp(self):
        if not oracle_available():
            self.skipTest("the game is not installed: oracle tests are skipped, not failed")

    def test_it_loads_and_runs_a_data_chunk(self):
        chunk = 'local obj1 = {\n\t["gems"] = 4154;\n\t["nested"] = {\n\t\t[1] = true;\n\t};\n}\nreturn obj1\n'
        result = oracle.check(chunk, name="<test>", mode="run")
        self.assertTrue(result.get("ok"), result.get("error"))
        self.assertEqual(
            oracle.normalize(result["value"]), {"gems": 4154, "nested": {"1": True}}
        )

    def test_number_types_survive(self):
        result = oracle.check("return { i = 3, f = 0.5 }", mode="run")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["value"], {"i": 3, "f": 0.5})

    def test_it_agrees_with_the_python_parser(self):
        text = 'local obj1 = {\n\t["a"] = 1;\n\t["b"] = "two";\n\t["c"] = -0.25;\n}\nreturn obj1\n'
        expected = oracle.normalize(lt.parse(text).python())
        result = oracle.check(text, mode="run")
        self.assertTrue(result.get("ok"))
        self.assertEqual(oracle.normalize(result["value"]), expected)

    def test_our_parser_is_stricter_than_lua(self):
        # Lua does not need the `;` between entries; the game always writes it, so our
        # grammar requires it — being stricter is what keeps the writer byte-exact.
        text = 'local obj1 = {\n\t["a"] = 1\n}\nreturn obj1\n'
        self.assertTrue(oracle.check(text, mode="run").get("ok"))
        with self.assertRaises(lt.LuaTableError):
            lt.parse(text)

    def test_load_mode_compiles_without_running(self):
        # A module that calls into LÖVE cannot run out of process, but it must compile.
        module = 'local love = nil\nlocal x = 1\nreturn { ["a"] = x }\n'
        self.assertTrue(oracle.check(module, mode="load").get("ok"))

    def test_no_libraries_are_opened(self):
        # The sandbox has no `os`, so this cannot execute anything dangerous.
        result = oracle.check('return os and os.execute and "reachable" or "sandboxed"', mode="run")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["value"], "sandboxed")

    def test_snippet_templates_compile_in_the_real_vm(self):
        for name, code in snippets.all_templates().items():
            with self.subTest(name=name):
                result = oracle.check(code, name="<{0}>".format(name), mode="load")
                self.assertTrue(result.get("ok"), "{0}: {1}".format(name, result.get("error")))


if __name__ == "__main__":
    unittest.main()
