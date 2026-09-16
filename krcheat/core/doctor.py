"""`krcheat doctor` — environment and diagnostics (foundation §10.1).

Doctor answers one question: *can this tool do its job here, and if not, why not.* Every
check is a `Gate` with a pass/warn/fail status and a sentence of detail, so the output is
usable both by a person and by a script.

Two deliberate properties:

* It **never raises for a missing game**. "The app bundle is not installed" is a
  diagnosis, not a crash, so it comes back as a FAIL row with exit code 2 rather than a
  traceback.
* It **records its summary** in `state.json` (`last_doctor`), because tier-2 commands
  refuse to start until a doctor run has passed (§15.3).
"""

from __future__ import annotations

import os
import shutil
from typing import List

from krcheat.core import config as config_mod
from krcheat.core import log as log_mod
from krcheat.core import mine, paths
from krcheat.core.errors import EXIT_NOT_FOUND, EXIT_OK, EXIT_VALIDATION, KrcheatError
from krcheat.core.result import Result
from krcheat.core.safety import FAIL, PASS, WARN, Gate

#: Exit code per failing check, so a script can tell "not installed" from "broken".
EXIT_FOR_CHECK = {
    "app bundle": EXIT_NOT_FOUND,
    "game archive": EXIT_NOT_FOUND,
    "save directory": EXIT_NOT_FOUND,
    "slots": EXIT_VALIDATION,
    "oracle": EXIT_OK,  # informational: the oracle enhances, it is not required
    "tkinter": EXIT_OK,  # informational: the GUI is optional
    "agent toolchain": EXIT_OK,
}


def run(ctx, deep=False, oracle_test=False):
    """Every check, in order. Never raises for a missing component."""
    result = Result(command="doctor")
    checks: List[Gate] = []

    checks.extend(_check_bundle(ctx))
    checks.extend(_check_archive(ctx))
    checks.extend(_check_save_dir(ctx))
    checks.extend(_check_slots(ctx))
    checks.extend(_check_steam(ctx))
    checks.extend(_check_processes(ctx))
    checks.extend(_check_tools(ctx))
    checks.extend(_check_tkinter(ctx))
    checks.extend(_check_oracle(ctx, run_test=oracle_test))
    checks.extend(_check_home(ctx))
    checks.extend(_check_config(ctx))
    checks.extend(_check_state(ctx))
    checks.extend(_check_backups(ctx))

    summary = {
        "pass": sum(1 for check in checks if check.status == PASS),
        "warn": sum(1 for check in checks if check.status == WARN),
        "fail": sum(1 for check in checks if check.status == FAIL),
    }
    result.set(checks=[check.to_dict() for check in checks], summary=summary)
    result.exit_code = _exit_code(checks)
    result.ok = summary["fail"] == 0

    for check in checks:
        if check.status == FAIL:
            result.warn("{0}: {1}".format(check.name, check.detail))

    try:
        ctx.state.record_doctor(
            {
                "pass": summary["pass"],
                "warn": summary["warn"],
                "fail": summary["fail"],
                "version_string": _version_of(ctx),
                "slots": [entry["slot"] for entry in _slot_inventory(ctx)],
            }
        )
    except Exception:
        pass
    ctx.log_info("doctor.done", deep=deep, **summary)
    return result


def _exit_code(checks):
    worst = EXIT_OK
    for check in checks:
        if check.status != FAIL:
            continue
        code = EXIT_FOR_CHECK.get(check.name, EXIT_VALIDATION)
        if code and worst in (EXIT_OK, EXIT_VALIDATION):
            worst = code
    return worst


def _version_of(ctx):
    try:
        return ctx.bundle().expected_version_string()
    except KrcheatError:
        return None


def _slot_inventory(ctx):
    try:
        return ctx.save_dir().slot_inventory()
    except KrcheatError:
        return []


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_bundle(ctx):
    try:
        bundle = ctx.bundle()
    except KrcheatError as exc:
        return [Gate("app bundle", FAIL, exc.message)]
    out = [
        Gate(
            "app bundle",
            PASS,
            bundle.path,
            version=bundle.short_version(),
            bundle_id=bundle.bundle_id(),
            expected_version_string=bundle.expected_version_string(),
        )
    ]
    expected = bundle.expected_version_string()
    if not expected:
        out.append(
            Gate("app bundle version", WARN, "Info.plist has no CFBundleShortVersionString; the "
                 "version_string check will be skipped")
        )
    return out


def _check_archive(ctx):
    try:
        bundle = ctx.bundle()
    except KrcheatError as exc:
        return [Gate("game archive", FAIL, "not checked: {0}".format(exc.message))]
    path = bundle.game_love
    if not os.path.exists(path):
        return [Gate("game archive", FAIL, "missing: {0}".format(path))]
    size = os.path.getsize(path)
    try:
        digest = paths.archive_hash(bundle, state=ctx.state)
    except KrcheatError as exc:
        return [Gate("game archive", WARN, "present but unreadable: {0}".format(exc.message))]
    # Record the install's identity, which is what makes every cached derivation
    # (mined ids, probe results) invalidate itself on a game update (§9.7).
    try:
        ctx.state.stamp_install(bundle.expected_version_string(), digest)
    except Exception:
        pass
    gate = Gate(
        "game archive",
        PASS,
        "{0} ({1:.1f} MB)".format(os.path.basename(path), size / 1048576.0),
        sha256=digest,
        bytes=size,
    )
    try:
        ids = mine.mine_all(bundle, state=ctx.state, logger=ctx.log)
        gate.extra["achievement_ids"] = len(ids.get("achievements", []))
        gate.extra["hero_ids"] = len(ids.get("heroes", []))
        gate.extra["levels"] = len(ids.get("levels", []))
    except Exception as exc:
        return [gate, Gate("archive ids", WARN, "could not mine ids: {0}".format(exc))]
    return [gate]


def _check_save_dir(ctx):
    try:
        save_dir = ctx.save_dir()
    except KrcheatError as exc:
        return [Gate("save directory", FAIL, exc.message)]
    writable = save_dir.writable()
    return [
        Gate(
            "save directory",
            PASS if writable else FAIL,
            "{0}{1}".format(save_dir.path, "" if writable else " (not writable)"),
            writable=writable,
        )
    ]


def _check_slots(ctx):
    try:
        save_dir = ctx.save_dir()
    except KrcheatError as exc:
        return [Gate("slots", FAIL, "not checked: {0}".format(exc.message))]
    found = save_dir.find_slots()
    if not found:
        return [Gate("slots", WARN, "no slot_*.lua files; create a profile in the game first")]
    out = []
    for number in found:
        path = save_dir.slot_path(number)
        try:
            text = open(path, "r", encoding="utf-8").read()
        except OSError as exc:
            out.append(Gate("slots", FAIL, "slot {0}: {1}".format(number, exc)))
            continue
        try:
            from krcheat.core import lua_table as lt

            doc = lt.parse(text)
        except Exception as exc:
            out.append(Gate("slots", FAIL, "slot {0} does not parse: {1}".format(number, exc)))
            continue
        missing = [key for key in lt.EXPECTED_TOP_LEVEL if not doc.has(key)]
        status = WARN if missing else PASS
        out.append(
            Gate(
                "slots",
                status,
                "slot {0}: {1} bytes, {2} top-level keys{3}".format(
                    number,
                    len(text),
                    len(doc.keys()),
                    "" if not missing else ", missing " + ", ".join(missing),
                ),
                slot=number,
                version_string=_safe_get(doc, "version_string"),
                gems=_safe_get(doc, "gems"),
                levels=len(doc.keys_at("levels")),
            )
        )
    try:
        save_dir_name = save_dir
        ctx.state.record_slots(save_dir_name.slot_inventory())
    except Exception:
        pass
    return out


def _safe_get(doc, key):
    try:
        return doc.get(key)
    except Exception:
        return None


def _check_steam(ctx):
    try:
        save_dir = ctx.save_dir()
    except KrcheatError:
        save_dir = None
    cloud = paths.steam_cloud_state(save_dir) if save_dir else {"entries": []}
    if cloud.get("syncs_save_dir"):
        return [
            Gate(
                "steam cloud",
                WARN,
                "Steam Cloud mirrors this save directory ({0} entr{1}). A cloud restore can "
                "overwrite local edits: closing Steam before writing is recommended.".format(
                    len(cloud.get("entries", [])),
                    "y" if len(cloud.get("entries", [])) == 1 else "ies",
                ),
                remotecache=cloud.get("remotecache"),
                entries=cloud.get("entries", [])[:10],
            )
        ]
    return [Gate("steam cloud", PASS, "no cloud mirroring detected for this save directory")]


def _check_processes(ctx):
    try:
        bundle = ctx.bundle()
    except KrcheatError:
        bundle = None
    game = paths.find_game_process(bundle)
    steam = paths.find_steam_process()
    out = [
        Gate(
            "game process",
            PASS if game is None else WARN,
            "not running" if game is None else "running (pid {0}) — tier-1 writes would be "
            "refused (§15.3)".format(game["pid"]),
            running=game is not None,
            pid=(game or {}).get("pid"),
        ),
        Gate(
            "steam process",
            PASS if steam is None else WARN,
            "not running" if steam is None else "running (pid {0})".format(steam["pid"]),
            running=steam is not None,
        ),
    ]
    return out


def _check_tools(ctx):
    """The compiler the agent needs once, and whether it has been built.

    Reported as three facts rather than one verdict, because they fail differently: no clang
    means transport A is unavailable on this machine, while "not built yet" means the next
    live command will spend a few seconds compiling and then work. `live status` carries the
    same report, so this stays a summary.
    """
    from krcheat.core.live import agent as agent_mod

    info = agent_mod.describe(getattr(ctx, "log", None))
    usable, reason = info["usable"], info["reason"]
    # Transport B is compiled by nothing, so it stays available with no toolchain at all.
    from krcheat.core.live.transport_patched import PatchedLoveTransport

    transport_b = PatchedLoveTransport(ctx=None).installed() or None
    detail = "clang={0}, codesign={1}, agent={2}".format(
        info["toolchain"]["clang"] or "missing",
        info["toolchain"]["codesign"] or "missing",
        os.path.basename(info["built"]) if info["built"] else "not built yet",
    )
    if transport_b is not None:
        detail += ", transport B installed=yes"
    return [
        Gate(
            "agent toolchain",
            PASS if usable else WARN,
            detail,
            usable=usable,
            reason=reason,
            source_dir=info["source_dir"],
            built=info["built"],
            fingerprint=info["fingerprint"],
            toolchain=info["toolchain"],
        )
    ]


def _check_tkinter(ctx):
    """§7.4: `_tkinter` is an optional build-time module, and a non-framework Python
    produces Tk windows that open behind the terminal and refuse focus.

    The verdict is static first: on this macOS the interpreter's Tk 8.5 `abort()`s when it
    opens a window, which cannot be caught in-process — and probing it in a subprocess
    would show a macOS crash-report dialog, once per `doctor` run. `tkprobe` therefore
    only starts a Tk process when the version numbers say it is plausible.

    When the GUI is switched off (the default, decision D10) there is nothing to warn
    about: a component the user has decided not to use is not a finding.
    """
    if not config_mod.gui_enabled(ctx.config):
        return [
            Gate(
                "tkinter",
                PASS,
                "not requested (ui.enabled = false): the CLI is the whole tool. Enable the GUI "
                "with '{0}'.".format(config_mod.GUI_ENABLE_HINT),
                available=True,
                enabled=False,
            )
        ]
    from krcheat.gui import tkprobe

    info = tkprobe.summary()
    if not info.get("available"):
        return [
            Gate(
                "tkinter",
                WARN,
                "not importable ({0}). The GUI is unavailable; the CLI is unaffected.".format(
                    info.get("import_error")
                ),
                available=False,
            )
        ]
    detail = "Tk {0}".format(info.get("tk_version"))
    if info.get("usable"):
        detail += (
            " — opened a window successfully"
            if info.get("source") == "probe"
            else " — passes the static check (not opened)"
        )
        status = PASS if info.get("framework_build") else WARN
    else:
        detail += " — {0} {1}".format(
            info.get("reason") or "not usable",
            "The GUI is unavailable; the CLI is unaffected.",
        )
        status = WARN
    if not info.get("framework_build"):
        detail += " (not a framework build: windows may open behind the terminal)"
    return [
        Gate(
            "tkinter",
            status,
            detail,
            available=True,
            usable=info.get("usable"),
            tk_version=info.get("tk_version"),
            framework_build=info.get("framework_build"),
            source=info.get("source"),
            reason=info.get("reason"),
        )
    ]


def _check_oracle(ctx, run_test=False):
    from krcheat.core import oracle as oracle_mod

    try:
        bundle = ctx.bundle()
    except KrcheatError:
        bundle = None
    path = oracle_mod.framework_path(bundle)
    if path is None:
        return [
            Gate(
                "oracle",
                WARN,
                "Lua.framework not found; generated saves will not be verified against the real "
                "VM (write-path validation still runs)",
                available=False,
            )
        ]
    gate = Gate("oracle", PASS, "available: {0}".format(path), available=True)
    if not run_test:
        return [gate]
    probe = 'local t = { ["a"] = 1; ["b"] = "two"; }\nreturn t\n'
    check = oracle_mod.check(probe, name="<doctor>", mode="run")
    if check.get("ok") and check.get("value") == {"a": 1, "b": "two"}:
        gate.extra["selftest"] = "pass"
        gate.detail += " — self-test passed (loaded and ran a chunk in the game's own VM)"
    else:
        gate.status = WARN
        gate.extra["selftest"] = "fail"
        gate.extra["oracle_error"] = check.get("error")
        gate.detail += " — self-test failed: {0}".format(check.get("error"))
    ctx.log_info("doctor.oracle_selftest", ok=bool(gate.extra["selftest"] == "pass"))
    return [gate]


def _check_home(ctx):
    out = []
    created = []
    try:
        created = paths.ensure_home()
    except OSError as exc:
        return [Gate("krcheat home", FAIL, "cannot create {0}: {1}".format(paths.home(), exc))]
    out.append(
        Gate(
            "krcheat home",
            PASS,
            "{0}{1}".format(paths.home(), " (created)" if created else ""),
            created=created,
        )
    )
    logs = paths.logs_dir()
    writable = os.path.isdir(logs) and os.access(logs, os.W_OK)
    out.append(
        Gate(
            "log directory",
            PASS if writable else WARN,
            "{0}{1}".format(logs, "" if writable else " (not writable; logging degrades to silent)"),
            writable=writable,
            active_log=log_mod.active_log_path(),
        )
    )
    return out


def _check_config(ctx):
    path = ctx.config.path
    try:
        reloaded = config_mod.Config.load(path)
    except KrcheatError as exc:
        return [Gate("config.ini", FAIL, exc.message, path=path)]
    unknown = reloaded.unknown_keys()
    detail = "{0} ({1} known key(s)".format(path, len(config_mod.DEFAULTS))
    detail += ", {0}".format(
        "unknown keys preserved: " + ", ".join(unknown[:5]) if unknown else "no unknown keys"
    ) + ")"
    return [
        Gate(
            "config.ini",
            PASS,
            detail,
            path=path,
            exists=os.path.exists(path),
            unknown_keys=unknown,
            values={key: value for key, value in reloaded.items().items()},
        )
    ]


def _check_state(ctx):
    expected = _version_of(ctx)
    cached = ctx.state.get("version_string")
    if not ctx.state.exists:
        return [Gate("state.json", PASS, "no cache yet: {0}".format(ctx.state.path))]
    if expected and cached and expected != cached:
        return [
            Gate(
                "state.json",
                WARN,
                "cache was built for {0!r} but the installed game is {1!r}: cached ids and probe "
                "results will be recomputed".format(cached, expected),
                cached_version=cached,
                expected_version=expected,
            )
        ]
    return [
        Gate(
            "state.json",
            PASS,
            "cache valid for {0!r}".format(cached or "unknown"),
            keys=sorted(key for key in ctx.state.data if not key.startswith("_")),
        )
    ]


def _check_backups(ctx):
    from krcheat.core import backup

    try:
        snapshots = backup.list_snapshots()
    except Exception as exc:
        return [Gate("backups", WARN, "cannot list: {0}".format(exc))]
    if not snapshots:
        return [Gate("backups", PASS, "none yet in {0}".format(paths.backups_dir()))]
    newest = snapshots[0]
    total = sum(item.get("total_size", 0) for item in snapshots)
    return [
        Gate(
            "backups",
            PASS,
            "{0} snapshot(s), {1:.1f} KB, newest {2}".format(
                len(snapshots), total / 1024.0, newest.get("id")
            ),
            count=len(snapshots),
            newest=newest.get("id"),
            bytes=total,
        )
    ]
