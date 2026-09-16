"""Codec tests — the byte-identity invariant is the primary one (§16.2, D1)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import lua_table as lt

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "slot_synthetic.lua")


def fixture_text():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


class TestRoundTrip(unittest.TestCase):
    def test_parse_render_is_byte_identical(self):
        text = fixture_text()
        self.assertEqual(lt.parse(text).render(), text)

    def test_parse_render_is_byte_identical_for_edge_shapes(self):
        cases = [
            "local obj1 = {\n}\nreturn obj1\n",
            "local obj1 = {\n\t[\"a\"] = 1;\n}\nreturn obj1\n",
            "local obj1 = {\n\t[1] = -0.5;\n\t[2] = 1e10;\n\t[3] = 0x1f;\n}\nreturn obj1\n",
            "local obj1 = {\n\t[\"s\"] = \"line\\nbreak\\ttab \\\"quoted\\\" \\\\ back\";\n}\nreturn obj1\n",
            "local obj1 = {\n\t[\"b\"] = true;\n\t[\"c\"] = false;\n}\nreturn obj1\n",
            "local multiRefObjects = {\n} -- multiRefObjects\nlocal obj1 = {\n\t[\"a\"] = 1;\n}\nreturn obj1\n",
            # unusual but legal: extra whitespace, commas as terminators, a comment
            "local  obj1 = {\n  [\"a\"] = 1, [\"b\"] = 2;\n  -- a comment\n  [\"c\"] = 3;\n}\nreturn obj1\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(lt.parse(text).render(), text)

    def test_values_survive_reparse(self):
        doc = lt.parse(fixture_text())
        self.assertEqual(doc.get("gems"), 100)
        self.assertEqual(doc.get("ratio"), 0.021276595745681)
        self.assertEqual(doc.get("levels.1"), {1: 1, 2: 1, 3: 1, "stars": 3})
        self.assertEqual(doc.get("heroes.selected"), "hero_magnus")
        self.assertEqual(doc.get("levels.14"), {})
        self.assertIs(doc.get("achievements.FIRST_BLOOD"), True)
        self.assertEqual(doc.keys_at("levels"), [1, 2, 14])


class TestEdits(unittest.TestCase):
    def test_change_one_scalar(self):
        text = fixture_text()
        doc = lt.parse(text)
        self.assertTrue(doc.set("gems", 9999))
        out = doc.render()
        self.assertIn('["gems"] = 9999;', out)
        self.assertEqual(len(lt.structural_diff(
            dict(lt.parse(out).python(), gems=9999), lt.parse(out).python()
        )), 0)
        # everything except the gems value is untouched
        self.assertEqual(out.replace('["gems"] = 9999;', '["gems"] = 100;'), text)

    def test_no_op_keeps_bytes(self):
        doc = lt.parse(fixture_text())
        self.assertFalse(doc.set("gems", 100))
        self.assertFalse(doc.dirty)
        self.assertEqual(doc.render(), fixture_text())

    def test_create_nested_path(self):
        doc = lt.parse(fixture_text())
        doc.set("levels.3.stars", 2, create=True)
        out = lt.parse(doc.render()).python()
        self.assertEqual(out["levels"][3], {"stars": 2})
        self.assertEqual(out["levels"][1], {1: 1, 2: 1, 3: 1, "stars": 3})

    def test_new_numeric_key_is_inserted_in_order(self):
        doc = lt.parse(fixture_text())
        doc.set("levels.2.stars", 2)  # exists: replace, not insert
        doc.set("levels.15.stars", 1, create=True)
        out = doc.render()
        self.assertLess(out.index("[14] = {"), out.index("[15] = {"))

    def test_renders_added_scalars(self):
        doc = lt.parse(fixture_text())
        doc.set("ratio2", 0.1, create=True)
        doc.set("flag", True, create=True)
        doc.set("label", 'he said "hi"', create=True)
        out = lt.parse(doc.render()).python()
        self.assertEqual(out["ratio2"], 0.1)
        self.assertIs(out["flag"], True)
        self.assertEqual(out["label"], 'he said "hi"')

    def test_new_key_is_appended_to_a_string_keyed_table(self):
        doc = lt.parse(fixture_text())
        doc.set("seen.enemy_sheep", True, create=True)
        self.assertEqual(doc.render().count("enemy_sheep"), 1)

    def test_deep_path_creation_only_touches_its_branch(self):
        doc = lt.parse(fixture_text())
        doc.set("heroes.status.hero_thor.xp", 42, create=True)
        out = lt.parse(doc.render()).python()
        self.assertEqual(out["heroes"]["status"]["hero_thor"], {"xp": 42})
        self.assertEqual(out["heroes"]["status"]["hero_magnus"], {"skills": {}, "xp": 0})


class TestRejections(unittest.TestCase):
    def test_missing_return_is_rejected(self):
        with self.assertRaises(lt.LuaTableError):
            lt.parse("local obj1 = {\n}\n")

    def test_returning_something_undeclared_is_rejected(self):
        with self.assertRaises(lt.LuaTableError):
            lt.parse("local obj1 = {\n}\nreturn obj2\n")

    def test_non_table_root_is_rejected(self):
        with self.assertRaises(lt.LuaTableError):
            lt.parse("local obj1 = 5\nreturn obj1\n")

    def test_nil_entry_is_rejected(self):
        with self.assertRaises(lt.LuaTableError):
            lt.parse('local obj1 = {\n\t["a"] = nil;\n}\nreturn obj1\n')

    def test_missing_semicolon_is_rejected(self):
        with self.assertRaises(lt.LuaTableError):
            lt.parse('local obj1 = {\n\t["a"] = 1\n}\nreturn obj1\n')

    def test_unknown_path_read_raises(self):
        doc = lt.parse(fixture_text())
        with self.assertRaises(lt.LuaTableError):
            doc.get("nope.nope")

    def test_no_deletion_check_finds_lost_keys(self):
        before = lt.parse(fixture_text()).python()
        after = lt.parse('local obj1 = {\n\t["gems"] = 1;\n}\nreturn obj1\n').python()
        missing = lt.flatten_paths(before) - lt.flatten_paths(after)
        self.assertIn("upgrades.archers", missing)
        self.assertIn("levels.1.stars", missing)


if __name__ == "__main__":
    unittest.main()
