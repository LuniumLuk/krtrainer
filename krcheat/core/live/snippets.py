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

**Every override is a triple.** Registering an `always` sends three snippets: the per-frame
`code`, a `capture` that runs once *before* the first write, and a `restore` that runs on
`clear` (§11.7). `override_snippets(key, value)` builds all three together, because a
capture that does not match its restore is worse than no capture at all — it silently puts
back the wrong value. Splitting them across two call sites is how that drift starts, so
there is one call site.

Two properties of the restore path are deliberate:

* **It does not use `lib/json`.** The capture's *reported* summary is JSON, for `live
  status`; the *stored* originals are kept as real Lua references in `__krcheat_saved`, keyed
  by override key. That means a table-valued field can be restored exactly, and — the reason
  that matters — restoring cannot fail because a JSON module was missing or a value was not
  encodable. Restore is the one call that must always work.
* **It is keyed by `__krcheat_key`,** which the agent sets around both snippets, so the same
  template pair serves every override without the agent knowing any field names.
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

#: What `god` writes into `game_outcome`. Inferred from the shipped debug string `"Lives
#: checking OFF (store.game_outcome set)"` (§6.3) rather than verified against a running
#: game, so it is one named constant: `probe` confirms it, and changing it is a one-line edit
#: with no C and no recompilation (§11.5).
GOD_SENTINEL = "true"

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
    """F2: the level life counter stops falling.

    Both spellings are written: §6.1 lists `lives` and `lives_left` as separate identifiers
    in the same modules, so whichever one this build reads is covered.
    """
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "s.lives_left = " + _num(value) + "\n"
        + "if s.lives ~= nil then s.lives = " + _num(value) + " end\n"
        + "return json.encode({ ok = true, lives_left = s.lives_left, lives = s.lives })\n"
    )


def speed(multiplier, owner=None):
    """F9: the shipped `time warp` multiplier (§6.3, H3).

    The field name is *not* known — §6.1 does not list it, only the debug string `"z/Z: time
    warp (%sx)"`. Rather than guessing one name and being wrong, the name is resolved once by
    the `capture` half of the override, which is allowed to loop; this per-frame half then
    reads the resolved name and contains no control flow at all (§11.6).
    """
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "local field = __krcheat_speed_field\n"
        + "if field == nil then\n"
        + "  error('the time-warp field has not been resolved; re-run the speed command')\n"
        + "end\n"
        + "s[field] = " + _num(multiplier) + "\n"
        + "return json.encode({ ok = true, field = field, value = s[field] })\n"
    )


def god_on(owner=None):
    """F10: disable life checking via `game_outcome` (§6.3).

    `true` is the sentinel. That is an inference from the debug string `"Lives checking OFF
    (store.game_outcome set)"` rather than something verified, and `probe` is what confirms
    it — which is why it is one constant, in one place, and not spelled into the template.
    """
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "s.game_outcome = " + GOD_SENTINEL + "\n"
        + "return json.encode({ ok = true, game_outcome = tostring(s.game_outcome) })\n"
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
}#: Templates that may be installed as a per-frame override. These are the ones the loop
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


#: Field names the `speed` override is allowed to claim (§6.3). The one that exists is
#: remembered in `__krcheat_speed_field`; if none does, the override reports failure instead
#: of pretending to work, because a speed override that silently does nothing is exactly the
#: "assignment was a silent no-op" risk of §18.
SPEED_FIELDS = (
    "time_scale",
    "timewarp",
    "time_warp",
    "speed_multiplier",
    "game_speed",
    "speed",
)


def _capture_footer():
    """The tail of every capture: store the originals, then describe what was stored.

    `__krcheat_saved` is a plain Lua table of real references — no JSON, no copy (§11.7). The
    encoded summary is only for the human reading `live status`, so it deliberately flattens
    anything that is not a scalar: a table has no useful JSON form and might be cyclic.
    """
    return (
        "__krcheat_saved[__krcheat_key] = saved\n"
        "local summary = {}\n"
        "for i = 1, #__krcheat_fields do\n"
        "  local name = __krcheat_fields[i]\n"
        "  local value = saved.values[name]\n"
        "  if type(value) == 'table' or type(value) == 'function' then\n"
        "    summary[name] = '<' .. type(value) .. '>'\n"
        "  else\n"
        "    summary[name] = value\n"
        "  end\n"
        "end\n"
        "return json.encode({ ok = true, key = __krcheat_key, captured = summary })\n"
    )


def capture_for(key, owner=None):
    """The `capture` snippet: record what `for_override(key)` is about to overwrite.

    Runs once, before the first write, and may use loops — this is where any resolution work
    belongs, so the per-frame snippet can stay control-flow free (§11.6).
    """
    if key == "speed":
        return _capture_speed(owner)
    fields = FIELDS_FOR_OVERRIDE.get(key)
    if fields is None:
        raise UsageError("unknown override key {0!r}".format(key))
    body = "".join(
        "  __krcheat_fields[#__krcheat_fields + 1] = " + _str(name) + "\n" for name in fields
    )
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "local saved = { present = {}, values = {} }\n"
        + "__krcheat_saved = __krcheat_saved or {}\n"
        + "__krcheat_fields = {}\n"
        + body
        + "for i = 1, #__krcheat_fields do\n"
        + "  local name = __krcheat_fields[i]\n"
        + "  saved.present[name] = s[name] ~= nil\n"
        + "  saved.values[name] = s[name]\n"
        + "end\n"
        + _capture_footer()
    )


def _capture_speed(owner):
    """`speed`'s capture doubles as the field resolver — the only loop the override needs."""
    candidates = ", ".join(_str(name) for name in SPEED_FIELDS)
    return (
        JSON_PRELUDE
        + "local s = " + _owner(owner) + "\n"
        + "if s == nil then return " + NOT_FOUND + " end\n"
        + "local field = nil\n"
        + "for _, name in ipairs({ " + candidates + " }) do\n"
        + "  if type(s[name]) == 'number' then field = name break end\n"
        + "end\n"
        + "if field == nil then\n"
        + "  return json.encode({ ok = false, error = 'none of the known time-warp fields '\n"
        + "    .. 'exist on this build; run: krcheat live probe' })\n"
        + "end\n"
        + "__krcheat_speed_field = field\n"
        + "local saved = { present = {}, values = {} }\n"
        + "__krcheat_saved = __krcheat_saved or {}\n"
        + "__krcheat_fields = { field }\n"
        + "saved.present[field] = true\n"
        + "saved.values[field] = s[field]\n"
        + _capture_footer()
    )


#: The fields each override writes, so the capture and the restore cannot disagree about them.
FIELDS_FOR_OVERRIDE = {
    "gold": ("player_gold",),
    "lives": ("lives_left", "lives"),
    "god": ("game_outcome",),
    # `speed` is absent on purpose: its field name is discovered at capture time (§6.3), so
    # the capture and restore agree through `__krcheat_saved` rather than through a constant.
}


def restore_for(key, owner=None):
    """The `restore` snippet: put the captured values back and forget them (§11.7.3).

    Deliberately free of `require("lib.json")` and of any error-prone work: this is the
    snippet that runs when the user turns a cheat off or the heartbeat expires, and it has to
    work even if the game's own modules are unhappy.
    """
    if key == "speed":
        return (
            "local s = " + _owner(owner) + "\n"
            "local saved = __krcheat_saved and __krcheat_saved[__krcheat_key]\n"
            "__krcheat_speed_field = nil\n"
            "if s == nil or saved == nil then return 'nothing to restore' end\n"
            "for name, value in pairs(saved.values) do\n"
            "  if saved.present[name] then s[name] = value else s[name] = nil end\n"
            "end\n"
            "__krcheat_saved[__krcheat_key] = nil\n"
            "return 'restored'\n"
        )
    fields = FIELDS_FOR_OVERRIDE.get(key)
    if fields is None:
        raise UsageError("unknown override key {0!r}".format(key))
    body = "".join(
        "  if saved.present[" + _str(name) + "] then s[" + _str(name) + "] = saved.values["
        + _str(name) + "] else s[" + _str(name) + "] = nil end\n"
        for name in fields
    )
    return (
        "local s = " + _owner(owner) + "\n"
        "local saved = __krcheat_saved and __krcheat_saved[__krcheat_key]\n"
        "if s == nil or saved == nil then return 'nothing to restore' end\n"
        "do\n"
        + body
        + "end\n"
        "__krcheat_saved[__krcheat_key] = nil\n"
        "return 'restored'\n"
    )


def override_snippets(key, value=None):
    """(`code`, `capture`, `restore`) for one override — built together, on purpose.

    A capture whose restore does not match it would put back the wrong value and look like
    success, so the three are never assembled independently.
    """
    return (for_override(key, value), capture_for(key), restore_for(key))


def all_templates():
    """Every template, for the compile check in `--self-test`.

    Includes the capture/restore halves of every override: they run in the game too, and a
    typo in one of them would only be discovered at the worst possible moment — during a
    `clear`, when the user is trying to put the game back the way it was.
    """
    templates = {
        "gold_inf": gold_infinity(),
        "lives_inf": lives_infinity(),
        "speed": speed(2.0),
        "god_on": god_on(),
        "probe": probe(),
        "state_read": state_read(["player_gold", "lives_left", "game_outcome"]),
        "eval": eval_snippet("return 1 + 1"),
    }
    for key in ("gold", "lives", "speed", "god"):
        templates["capture_" + key] = capture_for(key)
        templates["restore_" + key] = restore_for(key)
    return templates


def per_frame_templates():
    """The templates the loop guard applies to — every `always`-capable snippet."""
    everything = all_templates()
    return {name: everything[name] for name in PER_FRAME if name in everything}
