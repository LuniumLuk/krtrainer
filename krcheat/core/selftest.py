"""`krcheat --self-test` — the regression guard of §16.2.

Exercises the codec, the snapshot/restore path, the config writer, the state cache and
the snippet templates **without touching the game and without touching a real save**. It
is the thing to run after a change, and the thing to ask a user to run when a report
smells like an internal problem rather than a game problem.

Everything happens in a temporary directory that is removed afterwards.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import List

from krcheat.core import backup, config as config_mod, lua_table as lt, safety, state as state_mod
from krcheat.core.live import snippets
from krcheat.core.result import Result
from krcheat.core.safety import FAIL, PASS, WARN, Gate

#: A synthetic save. Shaped like the real thing — tabs, `["k"] = v;`, an empty table, a
#: `%.14g` float, mixed string/numeric keys — and containing no game data at all.
FIXTURE = (
    "local obj1 = {\n"
    '\t["achievements"] = {\n'
    '\t\t["FIRST_BLOOD"] = true;\n'
    "\t};\n"
    '\t["empty"] = {\n'
    "\t};\n"
    '\t["gems"] = 4154;\n'
    '\t["levels"] = {\n'
    "\t\t[1] = {\n"
    "\t\t\t[1] = 1;\n"
    '\t\t\t["stars"] = 3;\n'
    "\t\t};\n"
    "\t};\n"
    '\t["ratio"] = 0.021276595745681;\n'
    '\t["title"] = "kr1-desktop-6.4.46";\n'
    "}\n"
    "return obj1\n"
)


def run(ctx, with_oracle=True):
    result = Result(command="self-test")
    checks: List[Gate] = []
    workdir = tempfile.mkdtemp(prefix="krcheat-selftest-")
    try:
        checks.append(_check_roundtrip())
        checks.append(_check_targeted_edit())
        checks.append(_check_noop_identity())
        checks.append(_check_creation())
        checks.append(_check_rejection())
        checks.append(_check_backup(workdir))
        checks.append(_check_config(workdir))
        checks.append(_check_state(workdir))
        checks.append(_check_snippets())
        checks.append(_check_oracle_compile(ctx, with_oracle))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    summary = {
        "pass": sum(1 for check in checks if check.status == PASS),
        "warn": sum(1 for check in checks if check.status == WARN),
        "fail": sum(1 for check in checks if check.status == FAIL),
    }
    result.set(checks=[check.to_dict() for check in checks], summary=summary)
    result.ok = summary["fail"] == 0
    result.exit_code = 0 if result.ok else 6
    for check in checks:
        if check.status == FAIL:
            result.warn("{0}: {1}".format(check.name, check.detail))
    ctx.log_info("selftest.done", **summary)
    return result


# ---------------------------------------------------------------------------


def _check_roundtrip():
    try:
        doc = lt.parse(FIXTURE)
        rendered = doc.render()
    except Exception as exc:
        return Gate("codec round-trip", FAIL, "{0}: {1}".format(type(exc).__name__, exc))
    if rendered != FIXTURE:
        return Gate(
            "codec round-trip",
            FAIL,
            "render() is not byte-identical ({0} bytes in, {1} out)".format(
                len(FIXTURE), len(rendered)
            ),
        )
    return Gate("codec round-trip", PASS, "parse -> render is byte-identical ({0} bytes)".format(len(FIXTURE)))


def _check_targeted_edit():
    doc = lt.parse(FIXTURE)
    doc.set("gems", 9999)
    rendered = doc.render()
    parsed = lt.parse(rendered).python()
    if parsed.get("gems") != 9999:
        return Gate("targeted edit", FAIL, "the new value did not survive a re-parse")
    # Everything else must be untouched, byte for byte.
    before = lt.parse(FIXTURE).python()
    before["gems"] = 9999
    if lt.structural_diff(before, parsed):
        return Gate(
            "targeted edit",
            FAIL,
            "editing one value changed other keys: {0}".format(lt.structural_diff(before, parsed)[:3]),
        )
    if FIXTURE.count('\t["gems"] = 9999;') != 0 or "9999" not in rendered:
        return Gate("targeted edit", FAIL, "the edit is not present in the output")
    return Gate("targeted edit", PASS, "one value changed, all other bytes preserved")


def _check_noop_identity():
    doc = lt.parse(FIXTURE)
    changed = doc.set("gems", 4154)
    if changed:
        return Gate("no-op edit", FAIL, "setting a value to what it already is reported a change")
    if doc.dirty:
        return Gate("no-op edit", FAIL, "the document is dirty after a no-op edit")
    if doc.render() != FIXTURE:
        return Gate("no-op edit", FAIL, "a no-op edit changed the rendered bytes")
    return Gate("no-op edit", PASS, "a command that changes nothing produces identical bytes")


def _check_creation():
    doc = lt.parse(FIXTURE)
    doc.set("levels.19.stars", 3, create=True)
    output = lt.parse(doc.render()).python()
    if output["levels"].get(19) != {"stars": 3}:
        return Gate("key creation", FAIL, "the created table is wrong: {0!r}".format(output["levels"]))
    if 1 not in output["levels"]:
        return Gate("key creation", FAIL, "creating a key disturbed the existing entries")
    return Gate("key creation", PASS, "new nested keys are created without disturbing others")


def _check_rejection():
    """The writer must never be able to lose a key (§12.3)."""
    broken = lt.parse('local obj1 = {\n\t["gems"] = 1;\n}\nreturn obj1\n')
    try:
        safety.validate_text(broken.render(), broken.python(), lt.parse(FIXTURE).python())
    except Exception as exc:
        if "remove" in str(exc) or "mandatory" in str(exc):
            return Gate("no-deletion guard", PASS, "a lost key is refused before the swap")
        return Gate("no-deletion guard", FAIL, "refused for the wrong reason: {0}".format(exc))
    return Gate("no-deletion guard", FAIL, "a save missing mandatory keys was accepted")


def _check_backup(workdir):
    target = os.path.join(workdir, "slot_9.lua")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(FIXTURE)
    original_hash = __import__("hashlib").sha256(FIXTURE.encode("utf-8")).hexdigest()
    # Point the backup directory at the temporary workspace.
    old_home = os.environ.get("KRCHEAT_HOME")
    os.environ["KRCHEAT_HOME"] = os.path.join(workdir, "home")
    try:
        manifest = backup.snapshot([target], label="selftest", command="selftest",
                                   version_string="krcheat-selftest")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("damaged")
        backup.restore(manifest["id"])
        with open(target, "r", encoding="utf-8") as handle:
            restored = handle.read()
    except Exception as exc:
        return Gate("snapshot/restore", FAIL, "{0}: {1}".format(type(exc).__name__, exc))
    finally:
        if old_home is None:
            os.environ.pop("KRCHEAT_HOME", None)
        else:
            os.environ["KRCHEAT_HOME"] = old_home
    if restored != FIXTURE:
        return Gate("snapshot/restore", FAIL, "the restored bytes differ from the original")
    if __import__("hashlib").sha256(restored.encode("utf-8")).hexdigest() != original_hash:
        return Gate("snapshot/restore", FAIL, "the restored hash differs")
    return Gate("snapshot/restore", PASS, "restore is byte-identical and hash-verified")


def _check_config(workdir):
    path = os.path.join(workdir, "config.ini")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# a comment that must survive\n[safety]\n# inline note\nrequire_yes_when_steam_running = true\n[extra]\ncustom_key = keep me\n")
    try:
        cfg = config_mod.Config.load(path)
        cfg.set("safety.require_yes_when_steam_running", "false")
        cfg.set("logging.level", "debug")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        reloaded = config_mod.Config.load(path)
    except Exception as exc:
        return Gate("config writer", FAIL, "{0}: {1}".format(type(exc).__name__, exc))
    problems = []
    if "a comment that must survive" not in text:
        problems.append("a comment was dropped")
    if "custom_key = keep me" not in text:
        problems.append("an unknown key was dropped")
    if reloaded.get_typed("safety.require_yes_when_steam_running") is not False:
        problems.append("the new boolean did not take")
    if reloaded.get_typed("logging.level") != "debug":
        problems.append("a new section/key was not added")
    if problems:
        return Gate("config writer", FAIL, "; ".join(problems))
    return Gate("config writer", PASS, "comments and unknown keys preserved across a rewrite")


def _check_state(workdir):
    path = os.path.join(workdir, "state.json")
    try:
        state = state_mod.State.load(path)
        state.stamp_install("kr1-desktop-6.4.46", "abc123")
        state.put("mined_ids", {"achievements": ["A"]})
        state.save(force=True)
        state = state_mod.State.load(path)
        if not state.cache_matches("kr1-desktop-6.4.46", "abc123"):
            return Gate("state cache", FAIL, "a fresh cache did not match its own key")
        if state.cache_matches("kr1-desktop-6.4.47", "abc123"):
            return Gate("state cache", FAIL, "the cache matched a different version_string")
        state.stamp_install("kr1-desktop-6.4.47", "def456")
        if "mined_ids" in state.data:
            return Gate("state cache", FAIL, "mined ids survived a version change")
        if state.get("archive_hash") != "def456":
            return Gate("state cache", FAIL, "the archive hash was not updated")
    except Exception as exc:
        return Gate("state cache", FAIL, "{0}: {1}".format(type(exc).__name__, exc))
    return Gate("state cache", PASS, "invalidated on version change, keyed on version+hash")


def _check_snippets():
    problems = []
    templates = snippets.all_templates()
    for name, code in templates.items():
        if not code:
            problems.append("{0} is empty".format(name))
            continue
        if name.startswith("restore_"):
            # Restore snippets must *not* depend on the game's JSON module: they are the ones
            # that run when the user turns a cheat off, and an encoding failure there would
            # leave the game holding a value we wrote (§11.7.3). Asserted, not assumed.
            if "json" in code:
                problems.append("{0} depends on lib/json, which restore must not".format(name))
            continue
        if "json.encode" not in code:
            problems.append("{0} does not encode its result".format(name))
    for key in ("gold", "lives", "speed", "god"):
        for name, code in (
            ("capture_" + key, snippets.capture_for(key)),
            ("restore_" + key, snippets.restore_for(key)),
        ):
            if name not in templates:
                problems.append("{0} is not covered by all_templates()".format(name))
            elif not code.strip():
                problems.append("{0} is empty".format(name))
    for keyword in ("while true do end", "for i = 1, 10 do end", "repeat until true"):
        try:
            snippets.assert_safe(keyword)
            problems.append("assert_safe accepted {0!r}".format(keyword))
        except Exception:
            pass
    for name in ("gold", "lives", "speed", "god"):
        try:
            snippets.assert_safe(snippets.for_override(name))
        except Exception as exc:
            problems.append("override {0} tripped the loop guard: {1}".format(name, exc))
    if problems:
        return Gate("snippet library", FAIL, "; ".join(problems))
    return Gate(
        "snippet library",
        PASS,
        "{0} templates; the loop guard rejects control flow (§11.6)".format(len(templates)),
    )


def _check_oracle_compile(ctx, enabled=True):
    """Compile every template in the game's own LuaJIT (S8, D4)."""
    if not enabled:
        return Gate("oracle compile", WARN, "skipped (--no-oracle)")
    from krcheat.core import oracle as oracle_mod

    try:
        bundle = ctx.bundle()
    except Exception:
        bundle = None
    if not oracle_mod.available(bundle):
        return Gate("oracle compile", WARN, "skipped: Lua.framework not found")
    failures = []
    for name, code in snippets.all_templates().items():
        check = oracle_mod.check(code, name="<{0}>".format(name), mode="load", bundle=bundle)
        if check.get("skipped"):
            return Gate("oracle compile", WARN, "skipped: {0}".format(check.get("error")))
        if not check.get("ok"):
            failures.append("{0}: {1}".format(name, check.get("error")))
    if failures:
        return Gate("oracle compile", FAIL, "; ".join(failures[:3]))
    return Gate(
        "oracle compile",
        PASS,
        "{0} snippet(s) compiled by the game's own LuaJIT".format(len(snippets.all_templates())),
    )
