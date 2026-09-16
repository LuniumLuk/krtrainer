"""Tier-1 operations and the write path (§15.2, §10.2)."""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import backup, config as config_mod, context as context_mod, log as log_mod
from krcheat.core import lua_table as lt, paths, profile as profile_mod, safety
from krcheat.core.errors import ChannelUnavailable, NotFoundError, UsageError, ValidationError
from krcheat.core import state as state_mod
from krcheat.core.result import Result

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "slot_synthetic.lua")


def fixture_text():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        self.savedir = tempfile.mkdtemp(prefix="krcheat-test-save-")
        self.home_override = os.environ.get("KRCHEAT_HOME")
        os.environ["KRCHEAT_HOME"] = self.home
        self.slot_path = os.path.join(self.savedir, "slot_1.lua")
        with open(self.slot_path, "w", encoding="utf-8") as handle:
            handle.write(fixture_text())
        self.ctx = context_mod.Ctx(
            command="test",
            argv=["krcheat", "profile", "set"],
            config=config_mod.Config.load(os.path.join(self.home, "config.ini")),
            state=state_mod.State.load(os.path.join(self.home, "state.json")),
            log=log_mod.NullLogger(),
            oracle=False,
        )
        self.ctx.save_dir_override = self.savedir
        # A nonexistent --game keeps the version check out of the picture: the gate is
        # tested on its own in test_cli.py, and here it would only couple the tests to
        # whichever game build happens to be installed.
        self.ctx.game_override = os.path.join(self.savedir, "not-installed", "Kingdom Rush.app")

    def tearDown(self):
        if self.home_override is None:
            os.environ.pop("KRCHEAT_HOME", None)
        else:
            os.environ["KRCHEAT_HOME"] = self.home_override
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.savedir, ignore_errors=True)

    def load(self):
        return profile_mod.Profile.load(self.ctx.save_dir(), 1, logger=self.ctx.log)

    def digest(self):
        with open(self.slot_path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def text(self):
        with open(self.slot_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def apply(self, mutate, dry_run=False):
        self.ctx.dry_run = dry_run
        profile = self.load()
        result = Result(command="profile.set")
        mutate(profile, result)
        profile.save(self.ctx, result, label="test")
        self.ctx.dry_run = False
        return result


class TestLoading(ProfileCase):
    def test_reads_the_synthetic_save(self):
        profile = self.load()
        self.assertEqual(profile.get("gems"), 100)
        self.assertEqual(profile.version_string, "kr1-desktop-6.4.46")

    def test_summary_counts(self):
        summary = self.load().summary()
        self.assertEqual(summary["gems"], 100)
        self.assertEqual(summary["levels_total"], 3)
        self.assertEqual(summary["stars_total"], 4)
        self.assertIn(1, summary["levels_completed"])
        self.assertEqual(summary["achievements_total"], 2)
        self.assertEqual(summary["seen_total"], 2)
        self.assertEqual(summary["seen_unlocked"], 1)

    def test_missing_slot_is_not_found(self):
        with self.assertRaises(NotFoundError):
            profile_mod.Profile.load(self.ctx.save_dir(), 9)

    def test_unparseable_save_is_a_validation_error(self):
        with open(os.path.join(self.savedir, "slot_2.lua"), "w", encoding="utf-8") as handle:
            handle.write("not a save")
        with self.assertRaises(ValidationError):
            profile_mod.Profile.load(self.ctx.save_dir(), 2)

    def test_partial_save_warns(self):
        with open(os.path.join(self.savedir, "slot_3.lua"), "w", encoding="utf-8") as handle:
            handle.write('local obj1 = {\n\t["gems"] = 1;\n}\nreturn obj1\n')
        profile = profile_mod.Profile.load(self.ctx.save_dir(), 3)
        self.assertTrue(profile.warnings)


class TestOperations(ProfileCase):
    def test_gems(self):
        result = self.apply(lambda profile, result: profile.set_gems(result, 9999))
        self.assertEqual(result.changes[0].path, "gems")
        self.assertEqual(result.changes[0].before, 100)
        self.assertEqual(result.changes[0].after, 9999)
        self.assertEqual(lt.parse(self.text()).get("gems"), 9999)

    def test_gems_rejects_negative_and_huge(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_gems(Result(command="t"), -1)
        with self.assertRaises(ValidationError):
            profile.set_gems(Result(command="t"), 2 ** 32)
        with self.assertRaises(UsageError):
            profile.set_gems(Result(command="t"), "lots")

    def test_difficulty_range(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_difficulty(Result(command="t"), 9)

    def test_difficulty_ok(self):
        result = self.apply(lambda profile, result: profile.set_difficulty(result, 3))
        self.assertEqual(lt.parse(self.text()).get("difficulty"), 3)

    def test_upgrades_all_and_single(self):
        result = self.apply(lambda profile, result: profile.set_upgrades(result, "archers=1,mages=2"))
        values = lt.parse(self.text()).get("upgrades")
        self.assertEqual(values["archers"], 1)
        self.assertEqual(values["mages"], 2)
        self.assertEqual(values["rain"], 5)

    def test_upgrades_unknown_category_is_refused(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_upgrades(Result(command="t"), "trebuchets=1")

    def test_upgrades_above_observed_warns_but_writes(self):
        result = self.apply(lambda profile, result: profile.set_upgrades(result, "archers=7"))
        self.assertTrue(result.warnings)

    def test_stars_all(self):
        result = self.apply(lambda profile, result: profile.set_stars_all(result, stars=3))
        values = lt.parse(self.text()).get("levels")
        self.assertEqual(values[1]["stars"], 3)
        self.assertEqual(values[14], {1: 1, 2: 1, 3: 1, "stars": 3})
        self.assertEqual(values[2], {1: 1, 2: 1, 3: 1, "stars": 3})

    def test_stars_range(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_stars_all(Result(command="t"), stars=9)

    def test_level_stars_and_clear(self):
        result = self.apply(lambda profile, result: profile.set_level_stars(result, 2, 3, mode="heroic"))
        self.assertEqual(lt.parse(self.text()).get("levels.2.2"), 1)
        result = self.apply(lambda profile, result: profile.set_level_clear(result, 2, mode="heroic"))
        self.assertEqual(lt.parse(self.text()).get("levels.2.2"), 0)

    def test_level_clear_needs_a_mode(self):
        profile = self.load()
        with self.assertRaises(UsageError):
            profile.set_level_clear(Result(command="t"), 2)

    def test_hero_xp(self):
        result = self.apply(lambda profile, result: profile.set_hero_xp(result, "hero_malik", 500))
        self.assertEqual(lt.parse(self.text()).get("heroes.status.hero_malik.xp"), 500)

    def test_hero_xp_for_an_absent_hero_is_refused(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_hero_xp(Result(command="t"), "hero_nobody", 1)

    def test_hero_skills_are_refused_by_design(self):
        profile = self.load()
        with self.assertRaises(ChannelUnavailable):
            profile.set_hero_skills(Result(command="t"), "hero_malik", "all=3")

    def test_achievements_specific_ids(self):
        result = self.apply(lambda profile, result: profile.set_achievements(result, "SLAYER"))
        self.assertIs(lt.parse(self.text()).get("achievements.SLAYER"), True)

    def test_achievements_unknown_id_is_refused(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_achievements(Result(command="t"), "NOT_AN_ACHIEVEMENT")

    def test_achievements_none(self):
        result = self.apply(lambda profile, result: profile.set_achievements(result, "none"))
        self.assertIs(lt.parse(self.text()).get("achievements.FIRST_BLOOD"), False)

    def test_counters(self):
        result = self.apply(lambda profile, result: profile.set_counters(result, "DIE_HARD", 123))
        self.assertEqual(lt.parse(self.text()).get("achievement_counters.DIE_HARD"), 123)

    def test_counters_unknown_id_is_refused(self):
        profile = self.load()
        with self.assertRaises(ValidationError):
            profile.set_counters(Result(command="t"), "NOPE", 1)

    def test_seen_all(self):
        result = self.apply(lambda profile, result: profile.set_seen_all(result))
        self.assertIs(lt.parse(self.text()).get("seen.tower_arcane_wizard"), True)


class TestWritePath(ProfileCase):
    def test_dry_run_writes_nothing_and_takes_no_snapshot(self):
        before = self.digest()
        result = Result(command="t")
        self.ctx.dry_run = True
        profile = self.load()
        profile.set_gems(result, 1)
        profile.save(self.ctx, result, label="test")
        self.ctx.dry_run = False
        self.assertEqual(self.digest(), before)
        self.assertEqual(backup.list_snapshots(), [])
        self.assertTrue(result.dry_run)

    def test_real_write_changes_only_the_intended_bytes(self):
        result = self.apply(lambda profile, result: profile.set_gems(result, 9999))
        expected = fixture_text().replace('["gems"] = 100;', '["gems"] = 9999;')
        self.assertEqual(self.text(), expected)
        self.assertTrue(result.snapshot)

    def test_snapshot_precedes_the_write(self):
        result = self.apply(lambda profile, result: profile.set_gems(result, 9999))
        manifest = backup.resolve_snapshot(result.snapshot)
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(fixture_text().encode()).hexdigest())

    def test_no_op_leaves_the_file_untouched(self):
        before = self.digest()
        result = self.apply(lambda profile, result: profile.set_gems(result, 100))
        self.assertEqual(self.digest(), before)
        self.assertEqual(backup.list_snapshots(), [])
        self.assertTrue(any("no changes" in note for note in result.notes))

    def test_restore_returns_the_original(self):
        result = self.apply(lambda profile, result: profile.set_gems(result, 9999))
        backup.restore(result.snapshot)
        self.assertEqual(self.text(), fixture_text())

    def test_post_write_verification_keeps_the_file_consistent(self):
        result = self.apply(lambda profile, result: profile.set_stars_all(result, stars=2))
        # The result on disk parses, contains the change, and lost no key.
        parsed = lt.parse(self.text()).python()
        self.assertEqual(parsed["levels"][14]["stars"], 2)
        self.assertEqual(sorted(parsed.keys()), sorted(lt.parse(fixture_text()).python().keys()))


class TestValidation(unittest.TestCase):
    def test_no_deletion_is_refused(self):
        before = lt.parse(fixture_text()).python()
        text = 'local obj1 = {\n\t["gems"] = 1;\n}\nreturn obj1\n'
        with self.assertRaises(ValidationError):
            safety.validate_text(text, lt.parse(text).python(), before)

    def test_structural_mismatch_is_refused(self):
        text = fixture_text().replace('["gems"] = 100;', '["gems"] = 101;')
        with self.assertRaises(ValidationError):
            safety.validate_text(text, lt.parse(fixture_text()).python(), None)

    def test_unparseable_text_is_refused(self):
        with self.assertRaises(ValidationError):
            safety.validate_text("garbage", {}, None)

    def test_mandatory_key_loss_is_refused(self):
        before = lt.parse(fixture_text()).python()
        text = fixture_text().replace('\t["version_string"] = "kr1-desktop-6.4.46";\n', "")
        with self.assertRaises(ValidationError):
            safety.validate_text(text, lt.parse(text).python(), before)


class TestSlotResolution(ProfileCase):
    def test_explicit_slot_wins(self):
        with open(os.path.join(self.savedir, "slot_4.lua"), "w", encoding="utf-8") as handle:
            handle.write(fixture_text())
        self.assertEqual(paths.resolve_slot(self.ctx.save_dir(), explicit=4), 4)

    def test_nonexistent_slot_is_not_found_and_never_created(self):
        with self.assertRaises(NotFoundError):
            paths.resolve_slot(self.ctx.save_dir(), explicit=11)
        self.assertFalse(os.path.exists(os.path.join(self.savedir, "slot_11.lua")))

    def test_non_interactive_without_a_slot_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            paths.resolve_slot(self.ctx.save_dir(), interactive=False)

    def test_ask_is_consulted_never_guessed(self):
        asked = []

        def ask(available):
            asked.append(available)
            return 1

        self.assertEqual(paths.resolve_slot(self.ctx.save_dir(), ask=ask), 1)
        self.assertEqual(asked, [[1]])

    def test_find_slots_ignores_other_files(self):
        for name in ("settings.lua", "global.lua", "slot_x.lua", "steam_autocloud.vdf"):
            open(os.path.join(self.savedir, name), "w").close()
        self.assertEqual(self.ctx.save_dir().find_slots(), [1])


if __name__ == "__main__":
    unittest.main()
