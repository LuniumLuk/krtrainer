"""Game-specific Lua, as parameterised templates (foundation §11.5, §11.6).

All knowledge about *the game's* field names lives here as strings, so discovering a new
field or fixing a broken path is a data change — no C, no Python logic, no
recompilation. The exact owner paths are filled in after `probe` (spike S6) and are
cached in `state.json`.

Two rules are enforced structurally rather than by convention:

* **`always` snippets must not loop** (§11.6). A `while true do end` inside a per-frame
  snippet hangs the game's main thread and is *not* recoverable — the code that would
  read the fix is the code that is hanging. So `always` snippets are built from the
  templates below, which contain no user-supplied control flow, and `assert_safe`
  rejects loop keywords outright.
* **`eval` is `once`-only.** Arbitrary Lua is never installed as a per-frame override.

Results are encoded by the *snippet*, using the game's own bundled JSON library, so
nothing needs to marshal Lua values into C or Python (§11.4).

House style note: these templates are assembled by concatenation rather than
`str.format`, because every other line contains Lua braces and a mismatched `{{` would
produce Lua that compiles but does the wrong thing. `krcheat --self-test` compiles every
template in the game's own VM, which is what actually keeps this honest.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from krcheat.core.errors import UsageError

#: The encoder the snippets use. `lib/json.lua` ships inside `game.love`.
JSON_PRELUDE = 'local json = require("lib.json")\n'

#: Words that would make an `always` snippet a hang waiting to happen.
LOOP_KEYWORDS = ("while", "repeat", "goto", "for", "::")

#: Owner expressions, in the order `probe` should confirm them (spike S6, §6.2).
#: `store.game` is the documented access shape (`all/debug_tools.lua` uses it), but the
#: exact chain is *not* hard-coded anywhere: the resolved path is cached in state.json
#: and every snippet is generated from it at call time.
OWNER_CANDIDATES = ("store.game", "_G.GAME", "_G.game", "GAME", "game")

DEFAULT_GOLD_VALUE = 99999
DEFAULT_LIVES_VALUE = 99

NOT_FOUND = "json.encode({ ok = false, error = 'owner not found' })"


def assert_safe(code):
    """Refuse a snippet that could hang the main thread (§11.6).

    Applied to every snippet that can be installed as a per-frame override. Not applied
    to `once`-only snippets: a `probe` that walks tables needs loops, and a loop that
    runs once and returns cannot hang the game.
    """
    if not code:
        return code
    stripped = _strip_strings_and_comments(code)
    for keyword in LOOP_KEYWORDS:
        pattern = re.escape(keyword) if keyword == "::" else r"\b{0}\b".format(keyword)
        if re.search(pattern, stripped):
            raise UsageError(
                "this snippet contains {0!r}. Per-frame snippets must not loop: a hang in the "
                "game's main thread cannot be undone through the channel, because the code that "
                "would read the fix is the code that is hanging.".format(keyword)
            )
    return code


def _strip_strings_and_comments(code):
    code = re.sub(r"--\[\[.*?\]\]", " ", code, flags=re.S)
    code = re.sub(r"--[^\n]*", " ", code)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    code = re.sub(r"'(?:\\.|[^'\\])*'", "''", code)
    return code


def _owner(owner):
    return owner or "store.game"


def _num(value):
    """Render a Lua number: an int stays an int, a float keeps its point."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = repr(float(value))
    if "." not in text and "e" not in text and "inf" not in text and "nan" not in text:
        text += ".0"
    return text


def _str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def gold_infinity(value=DEFAULT_GOLD_VALUE, owner=None):
    """F1: gold stops decreasing.

    The snippet re-reads the field after writing it, because "the path resolved but the
    assignment was a silent no-op" is a recorded risk (§18) — a proxy table or a
    `__newindex` metamethod would otherwise look like success.
    """
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "s.player_gold = " + _num(value) + "\n"
        + "local readback = s.player_gold\n"
        + "if readback ~= " + _num(value) + " then\n"
        + "  return json.encode({ ok = false, wrote = " + _num(value)
        + ", readback = readback, error = 'assignment did not take' })\n"
        + "end\n"
        + "return json.encode({ ok = true, player_gold = readback })\n"
    )


def lives_infinity(value=DEFAULT_LIVES_VALUE, owner=None):
    """F2: the level life counter stops falling."""
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "s.lives_left = " + _num(value) + "\n"
        + "if s.lives ~= nil then s.lives = " + _num(value) + " end\n"
        + "return json.encode({ ok = true, lives_left = s.lives_left, lives = s.lives })\n"
    )


def speed(multiplier, owner=None):
    """F9: the shipped `time warp` multiplier, if the release build keeps the field (H3)."""
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "local before = s.time_scale\n"
        + "s.time_scale = " + _num(multiplier) + "\n"
        + "return json.encode({ ok = true, before = before, after = s.time_scale })\n"
    )


def god_on(owner=None):
    """F10: disable life checking via `game_outcome` (§6.3)."""
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "local before = s.game_outcome\n"
        + "s.game_outcome = { keep = true, previous = before }\n"
        + "return json.encode({ ok = true, before = before })\n"
    )


def state_read(fields, owner=None):
    """Read a fixed list of field names: used by `live status` and by read-back checks."""
    out = [JSON_PRELUDE, "local s = ", _owner(owner), "\n", "local values = {}\n"]
    for field in fields:
        out.append("if s ~= nil then values[" + _str(field) + "] = s." + field + " end\n")
    out.append("return json.encode({ ok = true, values = values })\n")
    return "".join(out)


def probe(depth=2, limit=60):
    """F12: walk the globals and report reachable shapes, so field paths can be resolved.

    This is the single most important live command: nothing is hard-coded to an address,
    and the candidate owner chain is *discovered* rather than assumed.
    """
    depth = max(1, min(int(depth), 4))
    limit = max(1, min(int(limit), 500))
    candidates = ", ".join(_str(name) for name in OWNER_CANDIDATES)
    return (
        JSON_PRELUDE
        + "local paths = {}\n"
        + "local seen = {}\n"
        + "local function walk(node, prefix, level)\n"
        + "  if level > " + str(depth) + " or type(node) ~= 'table' then return end\n"
        + "  if seen[node] then return end\n"
        + "  seen[node] = true\n"
        + "  local count = 0\n"
        + "  for key, value in pairs(node) do\n"
        + "    count = count + 1\n"
        + "    if count > " + str(limit) + " then break end\n"
        + "    if type(value) == 'table' then\n"
        + "      paths[prefix .. tostring(key)] = 'table'\n"
        + "      walk(value, prefix .. tostring(key) .. '.', level + 1)\n"
        + "    else\n"
        + "      paths[prefix .. tostring(key)] = type(value)\n"
        + "    end\n"
        + "  end\n"
        + "end\n"
        + "walk(_G, '', 0)\n"
        + "for _, name in ipairs({ " + candidates + " }) do\n"
        + "  local value = rawget(_G, name)\n"
        + "  if type(value) == 'table' then walk(value, name .. '.', 0) end\n"
        + "end\n"
        + "return json.encode({ ok = true, paths = paths })\n"
    )


def eval_snippet(code):
    """F11: pass user Lua through. `once`-only by contract (§11.6)."""
    if not code or not code.strip():
        raise UsageError("nothing to evaluate")
    return (
        JSON_PRELUDE
        + "local ok, value = pcall(function()\n"
        + code
        + "\nend)\n"
        + "if not ok then return json.encode({ ok = false, error = tostring(value) }) end\n"
        + "if value == nil then return json.encode({ ok = true, value = nil }) end\n"
        + "return json.encode({ ok = true, value = value })\n"
    )


#: name -> builder, for `live watch`, for `--self-test` and for the tests that assert the
#: safety rules.
TEMPLATES = {
    "gold_inf": gold_infinity,
    "lives_inf": lives_infinity,
    "speed": speed,
    "god_on": god_on,
    "probe": probe,
    "state_read": state_read,
}

#: Templates that may be installed as a per-frame override. These are the ones the loop
#: guard exists for, and they contain no control flow at all (§11.6).
PER_FRAME = ("gold_inf", "lives_inf", "speed", "god_on", "state_read")

#: Templates that are `once`-only by contract. `probe` needs loops to walk the game's
#: tables, and a loop that runs once and returns cannot hang anything.
ONCE_ONLY = ("probe", "eval")


def for_override(key, value=None):
    """Build the `always` snippet for an override key (§11.2, §11.7)."""
    if key == "gold":
        return gold_infinity(DEFAULT_GOLD_VALUE if value is None else value)
    if key == "lives":
        return lives_infinity(DEFAULT_LIVES_VALUE if value is None else value)
    if key == "speed":
        return speed(2.0 if value is None else value)
    if key == "god":
        return god_on()
    raise UsageError("unknown override key {0!r} (known: gold, lives, speed, god)".format(key))


def all_templates():
    """Every template, for the compile check in `--self-test`."""
    return {
        "gold_inf": gold_infinity(),
        "lives_inf": lives_infinity(),
        "speed": speed(2.0),
        "god_on": god_on(),
        "probe": probe(),
        "state_read": state_read(["player_gold", "lives_left", "game_outcome"]),
        "eval": eval_snippet("return 1 + 1"),
    }


def per_frame_templates():
    """The templates the loop guard applies to — every `always`-capable snippet."""
    everything = all_templates()
    return {name: everything[name] for name in PER_FRAME if name in everything}
