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

DEFAULT_GOLD_VALUE = 99999
DEFAULT_LIVES_VALUE = 99

#: What `god` writes into `game_outcome`. Inferred from the shipped debug string `"Lives
#: checking OFF (store.game_outcome set)"` (§6.3) rather than verified against a running
#: game, so it is one named constant. Note that the field is `nil` during play on the tested
#: build, so this writes a field the game does not currently set; see the note in `god_on`.
GOD_SENTINEL = "true"

#: The live state table, **measured against the running game** (kr1-desktop-6.4.46, at the start
#: of level01): `game.simulation.store.player_gold == 195`, `lives == 20`.
#:
#: This is where the previous guess went wrong. §6.2 inferred `store.game` from the debug
#: strings in `all/debug_tools.lua`, and the first live run against the real game answered
#: `attempt to index global 'store' (a nil value)` — the release build nests the live table under
#: the `game` module instead. Recorded with its evidence so it does not have to be re-derived.
MEASURED_OWNER = "game.simulation.store"

#: Candidate owner expressions, in preference order, as Lua expressions each evaluated under
#: `pcall` (so `game.simulation.store` fails harmlessly where `game.simulation` is nil). The
#: winner is chosen by **content** — it must hold `player_gold` or `lives` as a number — so a
#: wrong entry cannot silently attach an override to an unrelated table and create a junk field.
#: Discovery happens in the `capture`, which may loop; the per-frame half obeys `assert_safe`.
OWNER_CANDIDATES = (
    ("game.simulation.store", "game.simulation.store"),
    ("store and store.game", "store.game"),
    ("game.store", "game.store"),
)

#: The fields that identify the live table.
OWNER_FIELDS = ("player_gold", "lives")

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
# Getting the live table
# ---------------------------------------------------------------------------
#
# Every snippet needs `s` — the table the game keeps its level state on — and the two ways of
# getting it are different on purpose:
#
#   * a **capture** searches the candidates (it runs once, so a loop is free);
#   * a **per-frame or one-shot** snippet takes what the capture stored, and otherwise falls back
#     to the measured path *without looping*, because it must satisfy `assert_safe`.
#
# Naming the candidate to the user is part of the job: if the chain ever changes again, the report
# should say which entry answered rather than leaving it to be inferred from a failure.


def _resolve_fragment():
    """Loop-free owner resolution, for the snippets that run every frame or only once.

    The capture searches the candidates with a loop, which is fine because it runs once. Anything
    that can be installed as a per-frame override cannot loop (`assert_safe`), so the same search
    is **unrolled** here from the same candidate list — one source of truth, two shapes, no drift.

    The content check is the important part: a candidate only wins if it actually holds
    `player_gold` or `lives` as a number, so a stale entry in the list cannot attach an override to
    an unrelated table and quietly create a junk field.
    """
    fields = " or ".join(
        "type(candidate[" + _str(field) + "]) == 'number'" for field in OWNER_FIELDS
    )
    lines = [
        "local function __krcheat_is_store(candidate)\n",
        "  return type(candidate) == 'table' and (" + fields + ")\n",
        "end\n",
        "local s = __krcheat_store\n",
    ]
    for expression, _ in OWNER_CANDIDATES:
        lines += [
            "if s == nil then\n",
            "  local ok, candidate = pcall(function() return " + expression + " end)\n",
            "  if ok and __krcheat_is_store(candidate) then s = candidate end\n",
            "end\n",
        ]
    return "".join(lines)


def _apply_head(owner=None):
    """The prologue of a snippet that writes: `s` is set, or the snippet returns an error."""
    if owner:
        return (
            JSON_PRELUDE
            + "local s = " + owner + "\n"
            + "if s == nil then return " + NOT_FOUND + " end\n"
        )
    return (
        JSON_PRELUDE
        + _resolve_fragment()
        + "if s == nil then\n"
        + "  return json.encode({ ok = false, error = 'the live table could not be found: no "
        "candidate holds player_gold or lives. Run: krcheat live probe' })\n"
        + "end\n"
    )


def _capture_head(owner=None):
    """The prologue of a capture: find the live table, remember it, and say which entry won."""
    if owner:
        return (
            JSON_PRELUDE
            + "local s = " + owner + "\n"
            + "if s == nil then return " + NOT_FOUND + " end\n"
            + "__krcheat_store = s\n"
            + "__krcheat_owner = " + _str(owner) + "\n"
        )
    probes = "".join(
        "    function() return " + expression + " end,\n" for expression, _ in OWNER_CANDIDATES
    )
    names = ", ".join(_str(name) for _, name in OWNER_CANDIDATES)
    fields = " or ".join(
        "type(candidate[" + _str(field) + "]) == 'number'" for field in OWNER_FIELDS
    )
    return (
        JSON_PRELUDE
        + "local probes = {\n"
        + probes
        + "}\n"
        + "local names = { " + names + " }\n"
        + "local s, owner = nil, nil\n"
        + "for i = 1, #probes do\n"
        + "  local ok, candidate = pcall(probes[i])\n"
        + "  if ok and type(candidate) == 'table' and (" + fields + ") then\n"
        + "    s, owner = candidate, names[i]\n"
        + "    break\n"
        + "  end\n"
        + "end\n"
        + "if s == nil then\n"
        + "  return json.encode({ ok = false, error = 'no live table found holding player_gold or "
        "lives; the owner chain has changed — run: krcheat live probe' })\n"
        + "end\n"
        + "__krcheat_store = s\n"
        + "__krcheat_owner = owner\n"
    )


def _restore_head(owner=None):
    """The prologue of a restore: the same resolution, without `lib/json`.

    Restore runs when a cheat is turned off or the heartbeat expires, so it must not be able to
    fail because the game's JSON module is unhappy (§11.7.3). It shares the resolution fragment
    with the write path, which uses no library at all, and it never raises: if the table cannot be
    found, the snippet simply reports that it had nothing to put back.
    """
    if owner:
        return "local s = " + owner + "\n"
    return _resolve_fragment()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def gold_infinity(value=DEFAULT_GOLD_VALUE, owner=None):
    """F1: gold stops decreasing.

    The snippet re-reads the field after writing it, because "the path resolved but the
    assignment was a silent no-op" is a recorded risk (§18) — a proxy table or a
    `__newindex` metamethod would otherwise look like success. With the owner chain now
    *discovered* rather than assumed, that read-back is also what would catch a resolution that
    picked the wrong table.
    """
    return (
        _apply_head(owner)
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

    `lives` is the field that exists; `lives_left` does **not** on the tested build (measured:
    `nil`). The earlier version wrote `lives_left` unconditionally, which created a field the
    game never reads and left `lives` untouched — a cheat that reported success and did nothing.
    `lives_left` is still written when it is already there, for builds that use that spelling,
    and never created from nothing.
    """
    return (
        _apply_head(owner)
        + "s.lives = " + _num(value) + "\n"
        + "if s.lives_left ~= nil then s.lives_left = " + _num(value) + " end\n"
        + "local readback = s.lives\n"
        + "if readback ~= " + _num(value) + " then\n"
        + "  return json.encode({ ok = false, wrote = " + _num(value)
        + ", readback = readback, error = 'assignment did not take' })\n"
        + "end\n"
        + "return json.encode({ ok = true, lives = readback, lives_left = s.lives_left })\n"
    )


def speed(multiplier, owner=None):
    """F9: the simulation multiplier — **not available on the tested build**.

    §6.3 inferred a time-warp field from the shipped debug strings. Measured against the running
    release build, none of the plausible names exists on any of `game.simulation.store`,
    `game.simulation` or `game`: `krcheat live eval` checked `time_scale`, `timewarp`,
    `time_warp`, `speed_multiplier`, `game_speed`, `speed`, `simulation_speed` and
    `game_speed_multiplier`, and found none of them.

    So this template still resolves a field if one ever exists (the `capture` searches and
    remembers it), and otherwise reports the measurement instead of pretending. The honest answer
    is "not on this build", and tier 3 (`krcheat patch`) is where a real implementation would go.
    """
    return (
        _apply_head(owner)
        + "local field = __krcheat_speed_field\n"
        + "if field == nil then\n"
        + "  return json.encode({ ok = false, error = 'this build has no simulation-speed field "
        "(measured): none of ' .. table.concat(__krcheat_speed_names or {}, ', ') .. ' exists. "
        "Use lives/gold, or tier 3 (krcheat patch).' })\n"
        + "end\n"
        + "s[field] = " + _num(multiplier) + "\n"
        + "return json.encode({ ok = true, field = field, value = s[field] })\n"
    )


def god_on(owner=None):
    """F10: disable life checking via `game_outcome` (§6.3).

    Marked unverified, and this is what "unverified" means concretely: `game_outcome` is `nil`
    during play on the tested build, so nothing here can confirm that writing it turns life
    checking off. The debug string that justifies the mechanism (`"Lives checking OFF
    (store.game_outcome set)"`) ships in the release bytecode, but the code path around it is
    debug-only as far as the archive shows. `lives infinity` is the mechanism that is *measured*
    to work, and it needs no sentinel.
    """
    return (
        _apply_head(owner)
        + "s.game_outcome = " + GOD_SENTINEL + "\n"
        + "return json.encode({ ok = true, game_outcome = tostring(s.game_outcome), "
        "verified = false })\n"
    )


def state_read(fields, owner=None):
    """Read a fixed list of field names: used by `live status` and by read-back checks."""
    out = [_apply_head(owner), "local values = {}\n"]
    for field in fields:
        out.append("values[" + _str(field) + "] = s." + field + "\n")
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

#: What to tell a user who asks for `speed`, with the measurement behind it rather than a shrug.
SPEED_UNAVAILABLE_NOTE = (
    "this build has no simulation-speed field: measured against the running release build, none "
    "of {0} exists on game.simulation.store, game.simulation or game, and the debug-key time "
    "warp does not exist at runtime either (DBG_TIME_MULT is a bytecode constant, not a global). "
    "A real implementation belongs in tier 3 (`krcheat patch`). One lead is `store.dt`, the "
    "per-frame delta the simulation steps with, but writing it is an unverified guess about the "
    "game's timing, so it is documented rather than done."
).format(", ".join(SPEED_FIELDS))


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
        "return json.encode({ ok = true, key = __krcheat_key, owner = __krcheat_owner,\n"
        "                     captured = summary })\n"
    )


def capture_for(key, owner=None):
    """The `capture` snippet: record what `for_override(key)` is about to overwrite.

    Runs once, before the first write, and may use loops — this is where any resolution work
    belongs, so the per-frame snippet can stay control-flow free (§11.6). The owner chain is
    resolved here (and reported), because the *first* live run against the real game showed why
    a hard-coded guess is not good enough: §6.2's `store.game` does not exist on this build.
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
        _capture_head(owner)
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
        _capture_head(owner)
        + "__krcheat_speed_names = { " + candidates + " }\n"
        + "local field = nil\n"
        + "for _, name in ipairs(__krcheat_speed_names) do\n"
        + "  if type(s[name]) == 'number' then field = name break end\n"
        + "end\n"
        + "if field == nil then\n"
        + "  return json.encode({ ok = false, owner = __krcheat_owner,\n"
        + "    error = 'this build has no simulation-speed field (measured): none of '\n"
        + "      .. table.concat(__krcheat_speed_names, ', ') .. ' exists' })\n"
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
#: Order matters for one reason only: the first entry is the one the capture requires to be a
#: number, so it is the field that identifies the live table.
FIELDS_FOR_OVERRIDE = {
    "gold": ("player_gold",),
    # `lives` first, because that is the one this build has; `lives_left` does not exist
    # (measured) and is only recorded so a restore can put it back if some other build has it.
    "lives": ("lives", "lives_left"),
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
            _restore_head(owner)
            + "local saved = __krcheat_saved and __krcheat_saved[__krcheat_key]\n"
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
        _restore_head(owner)
        + "local saved = __krcheat_saved and __krcheat_saved[__krcheat_key]\n"
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
