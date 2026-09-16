"""Codec tests — the byte-identity invariant is the primary one (§16.2, D1)."""

import os
import random
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


class TestGeneratedText(unittest.TestCase):
    """Randomly shaped tables, written in the game's own style, must round-trip.

    The parser is the load-bearing component: mis-read a shape the game can emit and the
    writer re-emits it wrongly, or refuses a valid file. Sweeping the shape space finds
    that class of bug far faster than hand-written fixtures do.
    """

    def render_table(self, value, indent=0):
        pad = "\t" * (indent + 1)
        lines = ["{\n"]
        for key in sorted(value, key=lambda item: (isinstance(item, str), item)):
            item = value[key]
            rendered = (
                self.render_table(item, indent + 1)
                if isinstance(item, dict)
                else lt.render_scalar(item).text
            )
            lines.append("{0}{1} = {2};\n".format(pad, lt.render_key(key), rendered))
        lines.append("{0}}}".format("\t" * indent))
        return "".join(lines)

    def chunk_for(self, value):
        return "local obj1 = {0}\nreturn obj1\n".format(self.render_table(value))

    def random_scalar(self, rng):
        kind = rng.choice(("int", "float", "bool", "str"))
        if kind == "int":
            return rng.randint(-(2 ** 31), 2 ** 31)
        if kind == "float":
            return rng.choice(
                [0.0, -0.5, 0.1, 1 / 3, 0.021276595745681, rng.uniform(-1000, 1000)]
            )
        if kind == "bool":
            return rng.choice([True, False])
        return rng.choice(
            ["", "plain", 'quo"te', "tab\there", "nl\nhere", "\u00fcn\u00efcode", "back\\slash"]
        )

    def random_table(self, rng, depth=0):
        table = {}
        for _ in range(rng.randint(0, 4)):
            key = (
                rng.choice(["alpha", "beta", 'we"ird'])
                if rng.random() < 0.7
                else rng.randint(1, 6)
            )
            if depth < 2 and rng.random() < 0.35:
                table[key] = self.random_table(rng, depth + 1)
            else:
                table[key] = self.random_scalar(rng)
        return table

    def test_generated_text_round_trips_and_is_stable(self):
        rng = random.Random(20260916)
        for index in range(150):
            value = self.random_table(rng)
            text = self.chunk_for(value)
            with self.subTest(case=index, text=text[:60]):
                doc = lt.parse(text)
                self.assertEqual(doc.render(), text, "read/write is not byte-identical")
                self.assertEqual(doc.python(), value, "values did not survive the round trip")
                self.assertEqual(lt.parse(doc.render()).render(), text, "not stable on re-read")

    def test_an_edit_touches_exactly_one_line(self):
        """Scalar for scalar, an edit must rewrite one line and disturb no other.

        Only scalar targets are used: replacing a *table* with a scalar legitimately
        changes the line count, which would test the renderer's layout rather than the
        verbatim re-emission that D1 is about.
        """
        rng = random.Random(7)
        checked = 0
        for index in range(80):
            value = self.random_table(rng)
            scalars = [
                key
                for key in sorted(value, key=lambda item: (isinstance(item, str), item))
                if not isinstance(value[key], dict)
            ]
            if not scalars:
                continue
            target = scalars[0]
            replacement = 12345 if not isinstance(value[target], str) else "sentinel"
            if replacement == value[target]:
                replacement = 99999
            text = self.chunk_for(value)
            doc = lt.parse(text)
            self.assertTrue(doc.set(target, replacement))
            out = doc.render()
            checked += 1
            with self.subTest(case=index, target=target):
                self.assertEqual(lt.parse(out).python()[target], replacement)
                out_lines, text_lines = out.splitlines(), text.splitlines()
                self.assertEqual(len(out_lines), len(text_lines))
                differing = [
                    position
                    for position, pair in enumerate(zip(out_lines, text_lines))
                    if pair[0] != pair[1]
                ]
                self.assertEqual(
                    len(differing), 1, "an edit changed more than one line: {0}".format(differing)
                )
                self.assertIn(lt.render_key(target), out_lines[differing[0]])
        self.assertGreater(checked, 30, "the generator produced too few scalar targets")


if __name__ == "__main__":
    unittest.main()
