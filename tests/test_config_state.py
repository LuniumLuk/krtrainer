"""`config.ini` and `state.json` (§9.7, D8)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import config as config_mod, paths, state as state_mod
from krcheat.core.errors import UsageError

HANDWRITTEN = """\
# top comment that must survive
[paths]
# an inline note
save_dir = /tmp/example

[extra]
custom_key = keep me

[logging]
level = info
"""


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        os.environ["KRCHEAT_HOME"] = self.home
        self.path = os.path.join(self.home, "config.ini")

    def tearDown(self):
        os.environ.pop("KRCHEAT_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return config_mod.Config.load(self.path)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return handle.read()


class TestDefaults(ConfigCase):
    def test_missing_file_yields_defaults(self):
        cfg = config_mod.Config.load(self.path)
        self.assertEqual(cfg.get_typed("logging.level"), "info")
        self.assertIs(cfg.get_typed("ui.confirm_before_write"), True)
        self.assertIsNone(cfg.get("paths.game"))

    def test_ensure_file_writes_a_commented_default(self):
        cfg = config_mod.Config.load(self.path)
        self.assertTrue(cfg.ensure_file())
        text = self.read()
        self.assertIn("[logging]", text)
        self.assertIn("#", text)
        self.assertFalse(cfg.ensure_file())  # idempotent

    def test_empty_path_means_unset(self):
        cfg = self.write("[paths]\ngame =\n")
        self.assertIsNone(cfg.get("paths.game"))


class TestPreservation(ConfigCase):
    def test_comments_and_unknown_keys_survive(self):
        cfg = self.write(HANDWRITTEN)
        cfg.set("logging.level", "debug")
        text = self.read()
        self.assertIn("# top comment that must survive", text)
        self.assertIn("# an inline note", text)
        self.assertIn("custom_key = keep me", text)
        self.assertIn("level = debug", text)
        self.assertEqual(config_mod.Config.load(self.path).get_typed("logging.level"), "debug")

    def test_unknown_keys_are_reported_not_dropped(self):
        cfg = self.write(HANDWRITTEN)
        self.assertEqual(cfg.unknown_keys(), ["extra.custom_key"])

    def test_new_key_is_inserted_into_its_section(self):
        cfg = self.write(HANDWRITTEN)
        cfg.set("logging.retention_days", "3")
        text = self.read()
        self.assertIn("retention_days = 3", text)
        self.assertLess(text.index("[logging]"), text.index("retention_days"))
        self.assertEqual(config_mod.Config.load(self.path).get_typed("logging.retention_days"), 3)

    def test_new_section_is_appended(self):
        cfg = self.write(HANDWRITTEN)
        cfg.set("safety.require_yes_when_steam_running", "false")
        text = self.read()
        self.assertIn("[safety]", text)
        self.assertIs(config_mod.Config.load(self.path).get_typed("safety.require_yes_when_steam_running"), False)


class TestValidation(ConfigCase):
    def test_bad_boolean_is_refused(self):
        cfg = self.write(HANDWRITTEN)
        with self.assertRaises(UsageError):
            cfg.set("safety.require_yes_when_steam_running", "maybe")

    def test_bad_integer_is_refused(self):
        cfg = self.write(HANDWRITTEN)
        with self.assertRaises(UsageError):
            cfg.set("logging.retention_days", "soon")

    def test_unknown_section_is_refused(self):
        cfg = self.write(HANDWRITTEN)
        with self.assertRaises(UsageError):
            cfg.set("nonsense.key", "1")

    def test_bare_key_is_refused(self):
        cfg = self.write(HANDWRITTEN)
        with self.assertRaises(UsageError):
            cfg.set("level", "debug")

    def test_unparseable_file_is_reported(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("this is not an ini file\n")
        with self.assertRaises(UsageError):
            config_mod.Config.load(self.path)


class TestState(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        os.environ["KRCHEAT_HOME"] = self.home
        self.path = os.path.join(self.home, "state.json")

    def tearDown(self):
        os.environ.pop("KRCHEAT_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_round_trip(self):
        state = state_mod.State.load(self.path)
        state.put("version_string", "kr1-desktop-6.4.46")
        state.save()
        self.assertTrue(os.path.exists(self.path))
        reloaded = state_mod.State.load(self.path)
        self.assertEqual(reloaded.get("version_string"), "kr1-desktop-6.4.46")

    def test_cache_is_keyed_on_version_and_hash(self):
        state = state_mod.State.load(self.path)
        state.stamp_install("v1", "hash1")
        self.assertTrue(state.cache_matches("v1", "hash1"))
        self.assertFalse(state.cache_matches("v2", "hash1"))
        self.assertFalse(state.cache_matches("v1", "hash2"))

    def test_version_change_invalidates_derived_cache(self):
        state = state_mod.State.load(self.path)
        state.stamp_install("v1", "hash1")
        state.put("mined_ids", {"achievements": ["A"]})
        state.stamp_install("v2", "hash2")
        self.assertNotIn("mined_ids", state.data)
        self.assertEqual(state.get("version_string"), "v2")

    def test_corrupt_file_is_replaced_not_trusted(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        state = state_mod.State.load(self.path)
        self.assertFalse(state.exists)
        self.assertTrue(os.path.exists(self.path + ".corrupt"))

    def test_save_is_not_written_when_nothing_changed(self):
        state = state_mod.State.load(self.path)
        state.put("a", 1)
        self.assertTrue(state.save())
        stamp = os.path.getmtime(self.path)
        self.assertFalse(state.save())
        self.assertEqual(stamp, os.path.getmtime(self.path))


class TestSlotPolicy(unittest.TestCase):
    """D9: nothing about slot selection may be persisted (§10.7)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        os.environ["KRCHEAT_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("KRCHEAT_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_config_has_no_slot_key(self):
        self.assertFalse([key for key in config_mod.DEFAULTS if "slot" in key])

    def test_default_config_text_has_no_slot_key(self):
        self.assertNotIn("slot =", config_mod.DEFAULT_TEXT)
        self.assertIn("--slot", config_mod.DEFAULT_TEXT)  # mentioned as deliberately absent

    def test_state_never_records_a_selected_slot(self):
        state = state_mod.State.load()
        state.record_slots([{"slot": 1, "path": "/x/slot_1.lua", "size": 10, "mtime": 0}])
        blob = json.dumps(state.data)
        self.assertIn("slot_1.lua", blob)  # inventory: a fact about the filesystem
        self.assertNotIn("selected", blob)
        self.assertNotIn("active_slot", blob)


if __name__ == "__main__":
    unittest.main()
