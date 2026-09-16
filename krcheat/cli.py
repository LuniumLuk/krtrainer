"""`krcheat` — argument parsing, dispatch, exit codes, rendering (§10).

This module is a **thin renderer** (§9.5, D7). It parses arguments, calls one operation
in `core/`, and turns the returned `Result` into text or JSON. It contains no operation
logic, because a second implementation is how a GUI diverges from a CLI.

Global options are written *before* the subcommand:

    krcheat --dry-run profile set gems 9999

They are also accepted after the subcommand, because argparse makes that free and nobody
should be punished for typing the flag the other way round. The documented form is the
one before the subcommand, and the help text says so.

Debugging a run is unglamorous but decisive, so the logging surface is explicit:

    krcheat --log-level debug profile set gems 9999      # debug detail into the JSONL log
    krcheat -v profile show                              # debug detail onto stderr as well
    krcheat log tail                                     # read it back
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from krcheat import __version__
from krcheat.core import backup, config as config_mod, context as context_mod
from krcheat.core import data as data_mod, doctor as doctor_mod, log as log_mod, paths
from krcheat.core import profile as profile_mod, selftest as selftest_mod
from krcheat.core import state as state_mod
from krcheat.core.errors import (
    EXIT_INTERNAL,
    EXIT_OK,
    ChannelUnavailable,
    KrcheatError,
    UsageError,
    milestone,
)
from krcheat.core.result import Result

PROGRAM = "krcheat"

DESCRIPTION = """\
Kingdom Rush trainer and save editor for macOS.

Tier 1 (the save editor) needs no injection, no root and no compiler. Every mutating
command snapshots first, validates, swaps atomically and verifies afterwards; a command
that changes nothing leaves the file byte-identical.

  krcheat doctor                          what works here, and what does not
  krcheat --slot 1 profile show           read a profile
  krcheat --slot 1 profile set gems 9999  edit one field (snapshot + verify included)
  krcheat --log-level debug <command>     verbose JSONL diagnostics in ~/.krcheat/logs
"""

EPILOG = """\
Global options go before the subcommand. Run with --slot omitted and the CLI asks which
slot to use; it never guesses (foundation 10.7).

Exit codes: 0 ok, 1 usage, 2 not found, 3 unavailable (or not implemented in this
build), 4 validation, 5 backup, 6 internal.

Tier 2 (live gold/lives/speed) and tier 3 (bytecode patching) are documented in
KRCHEAT_FOUNDATION.md but are not built yet; their commands say so instead of pretending.
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; §10.5 says usage errors are exit 1."""

    def error(self, message):
        raise UsageError(message)


def add_global_flags(parser, suppress=False):
    """Register the global flags. `suppress` keeps subparser copies from clobbering."""
    default = argparse.SUPPRESS if suppress else None

    def add(*names, **kwargs):
        kwargs.setdefault("default", default)
        parser.add_argument(*names, **kwargs)

    add("--game", metavar="PATH", help="override the app bundle path")
    add("--save-dir", metavar="PATH", help="override the save directory")
    add("--slot", metavar="N", type=int, help="profile slot; explicit, reused only within a session")
    add("--json", dest="json_output", action="store_true", help="machine-readable output")
    add("--dry-run", dest="dry_run", action="store_true", help="show what would change, write nothing")
    add("--yes", dest="assume_yes", action="store_true", help="assume yes for confirmations")
    add("--force", dest="force", action="store_true", help="override a safety gate")
    add(
        "--log-level",
        dest="log_level",
        choices=("debug", "info", "warn", "error"),
        help="log verbosity for the JSONL log (default from config)",
    )
    add("--log", dest="log_file", metavar="FILE", help="override the log destination")
    add("--no-log", dest="no_log", action="store_true", help="disable logging for this run")
    add("--no-oracle", dest="no_oracle", action="store_true",
        help="skip the S8 oracle check in the write path")
    add("-v", "--verbose", dest="verbose", action="store_true", help="debug logging to stderr")


def build_parser():
    parser = Parser(
        prog=PROGRAM,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_flags(parser)
    parser.add_argument("--version", action="version", version="{0} {1}".format(PROGRAM, __version__))
    parser.add_argument(
        "--self-test",
        dest="self_test",
        action="store_true",
        help="exercise the codec, snapshot, config and state paths without touching the game",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # -- doctor --------------------------------------------------------------
    doctor = sub.add_parser("doctor", help="environment and diagnostics", parents=[])
    add_global_flags(doctor, suppress=True)
    doctor.add_argument("--oracle", dest="oracle_test", action="store_true",
                        help="also load and run a chunk in the game's own LuaJIT (spike S8)")
    doctor.add_argument("--deep", action="store_true", help="include the slower checks")

    # -- profile -------------------------------------------------------------
    profile = sub.add_parser("profile", help="tier 1: read and edit a save file")
    add_global_flags(profile, suppress=True)
    profile_sub = profile.add_subparsers(dest="profile_action", metavar="<action>")
    profile_sub.required = True

    profile_sub.add_parser("show", help="summarise the profile")

    get = profile_sub.add_parser("get", help="read one dotted path, e.g. levels.1.stars")
    get.add_argument("path")

    listing = profile_sub.add_parser("list", help="enumerate known ids")
    listing.add_argument(
        "kind", choices=("achievements", "heroes", "levels", "upgrades", "counters")
    )

    setter = profile_sub.add_parser("set", help="change one thing")
    setter_sub = setter.add_subparsers(dest="target", metavar="<target>")
    setter_sub.required = True

    gems = setter_sub.add_parser("gems", help="set the premium currency")
    gems.add_argument("value")

    difficulty = setter_sub.add_parser("difficulty", help="set the last-used difficulty (1-4)")
    difficulty.add_argument("value")

    upgrades = setter_sub.add_parser("upgrades", help="all=5, or archers=5,barracks=3")
    upgrades.add_argument("spec")

    stars = setter_sub.add_parser("stars", help="mark levels complete")
    stars.add_argument("scope", choices=("all",))
    stars.add_argument("--stars", type=int, default=3, help="stars per level (0-3, default 3)")
    stars.add_argument("--levels", help="comma-separated level ids (default: every story level)")
    stars.add_argument("--include-endless", dest="include_endless", action="store_true")

    level = setter_sub.add_parser("level", help="tune one level")
    level.add_argument("number")
    level_sub = level.add_subparsers(dest="level_action", metavar="<action>")
    level_sub.required = True
    level_stars = level_sub.add_parser("stars")
    level_stars.add_argument("stars", nargs="?", default=None)
    level_stars.add_argument("--mode", choices=tuple(sorted(profile_mod.MODE_INDEX)))
    level_clear = level_sub.add_parser("clear")
    level_clear.add_argument("--mode", choices=tuple(sorted(profile_mod.MODE_INDEX)))

    hero = setter_sub.add_parser("hero", help="hero experience (skills are refused: H6)")
    hero.add_argument("hero")
    hero_sub = hero.add_subparsers(dest="hero_action", metavar="<action>")
    hero_sub.required = True
    hero_xp = hero_sub.add_parser("xp")
    hero_xp.add_argument("value")
    hero_skills = hero_sub.add_parser("skills")
    hero_skills.add_argument("spec")

    achievements = setter_sub.add_parser("achievements", help="all | none | ID[,ID...]")
    achievements.add_argument("spec")

    counters = setter_sub.add_parser("counters", help="set one achievement counter")
    counters.add_argument("achievement")
    counters.add_argument("value")

    seen = setter_sub.add_parser("seen", help="mark every seen.* entry true")
    seen.add_argument("scope", choices=("all",))

    path_set = setter_sub.add_parser(
        "path", help="extension: set any dotted path (e.g. achievements.FIRST_BLOOD true)"
    )
    path_set.add_argument("path")
    path_set.add_argument("value")

    # -- backup --------------------------------------------------------------
    backup_parser = sub.add_parser("backup", help="snapshots")
    add_global_flags(backup_parser, suppress=True)
    backup_sub = backup_parser.add_subparsers(dest="backup_action", metavar="<action>")
    backup_sub.required = True
    backup_sub.add_parser("list", help="list snapshots")
    restore = backup_sub.add_parser("restore", help="restore a snapshot byte-identically")
    restore.add_argument("id", nargs="?", default="latest")
    restore.add_argument("--verify-only", dest="verify_only", action="store_true")
    prune = backup_sub.add_parser("prune", help="keep only the newest N snapshots")
    prune.add_argument("--keep", type=int, default=20)

    # -- log -----------------------------------------------------------------
    log_parser = sub.add_parser("log", help="the JSONL diagnostic log")
    add_global_flags(log_parser, suppress=True)
    log_sub = log_parser.add_subparsers(dest="log_action", metavar="<action>")
    log_sub.required = True
    tail = log_sub.add_parser("tail", help="print the tail of the log")
    tail.add_argument("--lines", type=int, default=40)
    tail.add_argument("--event", help="only records whose event contains this string")
    log_sub.add_parser("path", help="print the active log path")
    log_sub.add_parser("prune", help="apply the retention policy now")

    # -- config --------------------------------------------------------------
    config_parser = sub.add_parser("config", help="config.ini (user-owned)")
    add_global_flags(config_parser, suppress=True)
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="<action>")
    config_sub.required = True
    config_sub.add_parser("list", help="show every key with its effective value")
    config_get = config_sub.add_parser("get")
    config_get.add_argument("key")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_sub.add_parser("path")

    # -- data (F15) ----------------------------------------------------------
    data_parser = sub.add_parser("data", help="F15: persistent per-level data (needs spike S2)")
    add_global_flags(data_parser, suppress=True)
    data_sub = data_parser.add_subparsers(dest="data_action", metavar="<action>")
    data_sub.required = True
    data_sub.add_parser("list")
    data_set = data_sub.add_parser("set")
    data_set_sub = data_set.add_subparsers(dest="target", metavar="<target>")
    data_set_sub.required = True
    data_level = data_set_sub.add_parser("level")
    data_level.add_argument("number")
    data_level.add_argument("field", help="starting_gold | starting_lives")
    data_level.add_argument("value")
    data_wave = data_set_sub.add_parser("wave")
    data_wave.add_argument("number")
    data_wave.add_argument("field")
    data_wave.add_argument("value")
    data_wave.add_argument("--mode", default="campaign")
    data_revert = data_sub.add_parser("revert")
    data_revert.add_argument("--level", type=int)
    data_revert.add_argument("--all", dest="revert_all", action="store_true")

    # -- live (tier 2) -------------------------------------------------------
    live = sub.add_parser("live", help="tier 2: the in-process channel (not built yet)")
    add_global_flags(live, suppress=True)
    live_sub = live.add_subparsers(dest="live_action", metavar="<action>")
    live_sub.required = True
    live_sub.add_parser("status")
    probe = live_sub.add_parser("probe")
    probe.add_argument("--out", metavar="FILE")
    for name in ("gold", "lives"):
        item = live_sub.add_parser(name)
        item.add_argument("value", help="N | infinity | off")
    speed = live_sub.add_parser("speed")
    speed.add_argument("value", help="multiplier | off")
    god = live_sub.add_parser("god")
    god.add_argument("state", choices=("on", "off"))
    evaluate = live_sub.add_parser("eval")
    evaluate.add_argument("code")
    live_sub.add_parser("watch")

    # -- install / play / patch ---------------------------------------------
    play = sub.add_parser("play", help="launch the game with the agent loaded (not built yet)")
    add_global_flags(play, suppress=True)
    play.add_argument("--no-cheats", dest="no_cheats", action="store_true")
    play.add_argument("steam_args", nargs=argparse.REMAINDER)

    for name, help_text in (
        ("install", "install the tier-2 bootstrap (not built yet)"),
        ("uninstall", "remove installed tier-2 artefacts"),
        ("repair", "re-apply the tier-2 bootstrap after an integrity check"),
    ):
        item = sub.add_parser(name, help=help_text)
        add_global_flags(item, suppress=True)

    patch = sub.add_parser("patch", help="tier 3: bytecode patching (backlog)")
    add_global_flags(patch, suppress=True)
    patch_sub = patch.add_subparsers(dest="patch_action", metavar="<action>")
    patch_sub.required = True
    scan = patch_sub.add_parser("scan")
    scan.add_argument("value")
    scan.add_argument("--module", metavar="PATH")
    scan.add_argument("--type", dest="value_type", choices=("number", "int"), default="number")
    patch_sub.add_parser("apply")
    patch_sub.add_parser("restore")

    # -- selftest / gui ------------------------------------------------------
    sub.add_parser("self-test", help="regression guard: codec, backup, config, state")
    gui = sub.add_parser("gui", help="tkinter front-end over the same core")
    add_global_flags(gui, suppress=True)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

#: Global flags, and whether each takes a value. Used by `split_globals` so the flags
#: work in *any* position, not just the documented one before the subcommand: nobody
#: should be punished for typing `krcheat profile set gems 9999 --slot 2`.
GLOBAL_FLAGS = {
    "--game": True,
    "--save-dir": True,
    "--slot": True,
    "--log-level": True,
    "--log": True,
    "--json": False,
    "--dry-run": False,
    "--yes": False,
    "--force": False,
    "--no-log": False,
    "--no-oracle": False,
    "--verbose": False,
    "-v": False,
}


def split_globals(argv):
    """Hoist global flags to the front so argparse sees them before the subcommand."""
    front = []
    rest = []
    index = 0
    seen_dashdash = False
    while index < len(argv):
        token = argv[index]
        if token == "--":
            seen_dashdash = True
            rest.extend(argv[index:])
            break
        name, sep, value = token.partition("=")
        if not seen_dashdash and name in GLOBAL_FLAGS:
            front.append(name)
            if GLOBAL_FLAGS[name]:
                if sep:
                    front.append(value)
                elif index + 1 < len(argv):
                    index += 1
                    front.append(argv[index])
                else:
                    raise UsageError("{0} needs a value".format(name))
            elif sep:
                raise UsageError("{0} does not take a value".format(name))
            index += 1
            continue
        rest.append(token)
        index += 1
    return front, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    started = time.time()
    try:
        parser = build_parser()
        front, rest = split_globals(argv)
        args = parser.parse_args(front + rest)
    except UsageError as exc:
        sys.stderr.write("{0}: error: {1}\n".format(PROGRAM, exc.message))
        return exc.code
    except SystemExit as exc:  # --help / --version
        return int(exc.code or 0)

    command = "self-test" if getattr(args, "self_test", False) else getattr(args, "command", None)
    if not command:
        build_parser().print_help()
        return EXIT_OK

    ctx = _build_context(args, command, argv)
    result = None
    try:
        result = dispatch(ctx, args)
    except KrcheatError as exc:
        _log_failure(ctx, exc, argv)
        _report_error(ctx, exc)
        ctx.log.close()
        return exc.code
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        ctx.log.warn("command.interrupted", command=command)
        ctx.log.close()
        return 130
    except Exception as exc:  # a bug: always traceback-logged (§9.6)
        ctx.log.exception("command.internal_error", exc, command=command)
        ctx.log.close()
        sys.stderr.write(
            "{0}: internal error: {1}: {2}\n".format(PROGRAM, type(exc).__name__, exc)
        )
        sys.stderr.write("The traceback is in {0}\n".format(ctx.log.path or "the log"))
        return EXIT_INTERNAL

    _render(ctx, result, sys.stdout)
    ctx.log.info(
        "command.end",
        command=command,
        exit_code=result.exit_code,
        ok=result.ok,
        ms=int((time.time() - started) * 1000),
        changes=len(result.effective_changes),
        snapshot=result.snapshot,
        warnings=len(result.warnings),
        dry_run=result.dry_run,
    )
    ctx.log.close()
    return result.exit_code


def _build_context(args, command, argv):
    """Configure logging first, so even a failure to load config is recorded."""
    level = getattr(args, "log_level", None)
    verbose = bool(getattr(args, "verbose", False))
    log_file = getattr(args, "log_file", None)
    no_log = bool(getattr(args, "no_log", False))

    cfg = None
    try:
        cfg = config_mod.Config.load()
        if not os.path.exists(cfg.path):
            cfg.ensure_file()
    except KrcheatError:
        cfg = config_mod.Config(path=paths.config_path(), text="")

    if level:
        pass
    elif verbose:
        level = "debug"
    else:
        try:
            level = cfg.get_typed("logging.level") or "info"
        except Exception:
            level = "info"

    logger = log_mod.configure(
        level=level,
        path=log_file,
        enabled=not no_log,
        command=command,
        argv=argv,
        stderr_level="debug" if verbose else None,
        keep_days=_cfg_int(cfg, "logging.retention_days", log_mod.DEFAULT_KEEP_DAYS),
        max_bytes=_cfg_int(cfg, "logging.max_total_mb", 20) * 1024 * 1024,
    )

    ctx = context_mod.Ctx(
        command=command,
        argv=argv,
        config=cfg,
        state=state_mod.State.load(),
        log=logger,
        dry_run=bool(getattr(args, "dry_run", False)),
        assume_yes=bool(getattr(args, "assume_yes", False)),
        force=bool(getattr(args, "force", False)),
        verbose=verbose,
        json_output=bool(getattr(args, "json_output", False)),
        oracle=not bool(getattr(args, "no_oracle", False)),
        game_override=getattr(args, "game", None),
        save_dir_override=getattr(args, "save_dir", None),
    )
    ctx.interactive = sys.stdin.isatty() and sys.stdout.isatty()
    logger.info(
        "command.start",
        command=command,
        argv=argv,
        dry_run=ctx.dry_run,
        force=ctx.force,
        assume_yes=ctx.assume_yes,
        slot=getattr(args, "slot", None),
        log_level=level,
        log_path=logger.path,
        cwd=os.getcwd(),
        python=sys.version.split()[0],
        krcheat=__version__,
        interactive=ctx.interactive,
    )
    return ctx


def _cfg_int(cfg, key, fallback):
    try:
        value = cfg.get_typed(key)
        return int(value) if value not in (None, "") else fallback
    except Exception:
        return fallback


def _log_failure(ctx, exc, argv):
    ctx.log.error(
        "command.failed",
        command=ctx.command,
        kind=getattr(exc, "kind", "error"),
        exit_code=exc.code,
        message=exc.message,
        **{k: v for k, v in (exc.fields or {}).items() if k != "traceback"}
    )


def _report_error(ctx, exc):
    sys.stderr.write("{0}: {1}\n".format(PROGRAM, exc.message))
    if ctx.log.path and not getattr(ctx, "no_log", False):
        sys.stderr.write("  (logged to {0})\n".format(ctx.log.path))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(ctx, args):
    command = ctx.command
    handler = HANDLERS.get(command)
    if handler is None:
        raise UsageError("unknown command {0!r}".format(command))
    return handler(ctx, args)


def _slot_for(ctx, args):
    """§10.7: explicit > session > ask > usage error."""
    ask = context_mod.prompt_for_slot if ctx.interactive else None
    return ctx.slot(explicit=getattr(args, "slot", None), ask=ask)


def _load_profile(ctx, args):
    save_dir = ctx.save_dir()
    slot = _slot_for(ctx, args)
    profile = profile_mod.Profile.load(save_dir, slot, logger=ctx.log)
    for warning in profile.warnings:
        ctx.log.warn("profile.warning", message=warning)
    return profile


# -- doctor ------------------------------------------------------------------


def cmd_doctor(ctx, args):
    result = doctor_mod.run(
        ctx,
        deep=bool(getattr(args, "deep", False)),
        oracle_test=bool(getattr(args, "oracle_test", False)),
    )
    ctx.state.save()
    return result


# -- profile -----------------------------------------------------------------


def cmd_profile(ctx, args):
    action = args.profile_action
    if action == "list":
        return _profile_list(ctx, args)
    profile = _load_profile(ctx, args)
    if action == "show":
        result = Result(command="profile.show")
        result.set(profile=profile.summary())
        return result
    if action == "get":
        result = Result(command="profile.get")
        try:
            value = profile.get(args.path)
        except KrcheatError:
            raise
        result.set(path=args.path, value=value, slot=profile.slot, file=profile.path)
        return result
    if action == "set":
        return _profile_set(ctx, args, profile)
    raise UsageError("unknown profile action {0!r}".format(action))


def _profile_list(ctx, args):
    kind = args.kind
    profile = None
    if kind == "counters":
        profile = _load_profile(ctx, args)
    elif kind in ("achievements", "heroes", "levels", "upgrades"):
        # Deliberately not slot-dependent when the archive can answer: `list` is a
        # reference command, and prompting for a slot to print the game's own id list
        # would be noise.
        try:
            profile = _load_profile(ctx, args)
        except KrcheatError:
            profile = None
    values = profile_mod.list_ids(ctx, kind, profile=profile)
    result = Result(command="profile.list")
    result.set(kind=kind, count=len(values), ids=values)
    return result


def _profile_set(ctx, args, profile):
    result = Result(command="profile.set")
    result.set(slot=profile.slot, file=profile.path)
    target = args.target
    if target == "gems":
        profile.set_gems(result, args.value)
    elif target == "difficulty":
        profile.set_difficulty(result, args.value)
    elif target == "upgrades":
        profile.set_upgrades(result, args.spec)
    elif target == "stars":
        levels = (
            [item.strip() for item in args.levels.split(",") if item.strip()]
            if args.levels
            else None
        )
        profile.set_stars_all(
            result, ctx=ctx, stars=args.stars, levels=levels, include_endless=args.include_endless
        )
    elif target == "level":
        if args.level_action == "stars":
            profile.set_level_stars(result, args.number, args.stars, mode=args.mode)
        else:
            profile.set_level_clear(result, args.number, mode=args.mode)
    elif target == "hero":
        if args.hero_action == "xp":
            profile.set_hero_xp(result, args.hero, args.value)
        else:
            profile.set_hero_skills(result, args.hero, args.spec)
    elif target == "achievements":
        profile.set_achievements(result, args.spec, ctx=ctx)
    elif target == "counters":
        profile.set_counters(result, args.achievement, args.value)
    elif target == "seen":
        profile.set_seen_all(result, ctx=ctx)
    elif target == "path":
        value = _coerce(args.value)
        if not profile.doc.has(args.path) and not _parent_exists(profile, args.path):
            profile.doc.set(args.path, value, create=True)
            result.add_change(args.path, None, value)
        else:
            before = profile.doc.get(args.path) if profile.doc.has(args.path) else None
            changed = profile.doc.set(args.path, value, create=True)
            result.add_change(args.path, before, value, noop=not changed)
    else:
        raise UsageError("unknown profile set target {0!r}".format(target))

    profile.save(ctx, result, label="profile-set-{0}".format(target))
    _persist(ctx)
    return result


def _parent_exists(profile, path):
    parts = profile_mod.lt.split_path(path)
    try:
        profile.doc.ensure_table(".".join(str(part) for part in parts[:-1]))
        return True
    except Exception:
        return False


def _coerce(text):
    """Best-effort scalar for the `profile set path` extension."""
    body = str(text).strip()
    lowered = body.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("nil", "null"):
        return None
    try:
        return int(body)
    except ValueError:
        pass
    try:
        return float(body)
    except ValueError:
        pass
    return text


# -- backup ------------------------------------------------------------------


def cmd_backup(ctx, args):
    action = args.backup_action
    if action == "list":
        snapshots = backup.list_snapshots(include_incomplete=True)
        result = Result(command="backup.list")
        result.set(snapshots=snapshots, directory=paths.backups_dir(), count=len(snapshots))
        return result
    if action == "restore":
        payload = backup.restore(args.id, verify_only=bool(args.verify_only))
        result = Result(command="backup.restore")
        result.set(restored=[], snapshot=payload["snapshot"])
        manifest = payload["snapshot"]
        result.set(
            restored=[
                {
                    "source": os.path.basename(item["source"]),
                    "target": item["target"],
                    "sha256": item.get("restored_sha256") or item["sha256"],
                    "bytes": item.get("size"),
                }
                for item in payload["files"]
            ],
            verify_only=bool(args.verify_only),
        )
        result.note(
            "restored from snapshot {0} ({1})".format(manifest.get("id"), manifest.get("created"))
        )
        if not args.verify_only:
            result.note(
                "if Steam Cloud is syncing this file, the next Steam launch will see the "
                "restored (older) copy as a local change"
            )
        ctx.log.info(
            "backup.restored",
            snapshot=manifest.get("id"),
            verify_only=bool(args.verify_only),
            files=len(payload["files"]),
        )
        return result
    if action == "prune":
        payload = backup.prune(keep=args.keep, dry_run=ctx.dry_run)
        result = Result(command="backup.prune")
        if ctx.dry_run:
            result.dry_run = True
        result.set(
            kept=[item.get("id") for item in payload["kept"]],
            removed=[item.get("id") for item in payload["removed"]],
            kept_count=len(payload["kept"]),
        )
        ctx.log.info("backup.pruned", removed=len(payload["removed"]), kept=len(payload["kept"]))
        return result
    raise UsageError("unknown backup action {0!r}".format(action))


# -- log ---------------------------------------------------------------------


def cmd_log(ctx, args):
    action = args.log_action
    if action == "path":
        result = Result(command="log.path")
        result.set(path=log_mod.active_log_path(), directory=paths.logs_dir(),
                   files=log_mod.log_files())
        return result
    if action == "tail":
        records, source = log_mod.tail_lines(count=max(1, int(args.lines)))
        if getattr(args, "event", None):
            needle = args.event.lower()
            records = [item for item in records if needle in str(item.get("event", "")).lower()]
        result = Result(command="log.tail")
        result.set(path=source, directory=paths.logs_dir(), records=records, count=len(records))
        return result
    if action == "prune":
        removed = log_mod.prune_logs(
            keep_days=_cfg_int(ctx.config, "logging.retention_days", log_mod.DEFAULT_KEEP_DAYS),
            max_bytes=_cfg_int(ctx.config, "logging.max_total_mb", 20) * 1024 * 1024,
        )
        result = Result(command="log.prune")
        result.set(removed=removed, removed_count=len(removed), kept=log_mod.log_files())
        return result
    raise UsageError("unknown log action {0!r}".format(action))


# -- config ------------------------------------------------------------------


def cmd_config(ctx, args):
    action = args.config_action
    if action == "path":
        result = Result(command="config.path")
        result.set(path=ctx.config.path, exists=os.path.exists(ctx.config.path))
        return result
    if action == "list":
        result = Result(command="config.list")
        result.set(
            path=ctx.config.path,
            items=ctx.config.items(),
            unknown=ctx.config.unknown_keys(),
            defaults=config_mod.DEFAULTS,
        )
        return result
    if action == "get":
        if "." not in args.key:
            raise UsageError("config keys are written section.key, e.g. logging.level")
        result = Result(command="config.get")
        result.set(key=args.key, value=ctx.config.get(args.key))
        return result
    if action == "set":
        if ctx.dry_run:
            result = Result(command="config.set", dry_run=True)
            result.set(key=args.key, value=config_mod.parse_scalar(args.key, args.value))
            result.note("dry run: config.ini was not written")
            return result
        before = ctx.config.get(args.key)
        ctx.config.set(args.key, args.value)
        result = Result(command="config.set")
        result.add_change(args.key, before, ctx.config.get(args.key))
        result.set(path=ctx.config.path)
        ctx.log.info("config.set", key=args.key, value=str(args.value))
        return result
    raise UsageError("unknown config action {0!r}".format(action))


# -- data (F15) --------------------------------------------------------------


def cmd_data(ctx, args):
    action = args.data_action
    if action == "list":
        payload = data_mod.list_overrides(ctx)
        result = Result(command="data.list")
        result.set(**payload)
        return result
    if action == "set":
        result = Result(command="data.set")
        if args.target == "level":
            data_mod.set_level_data(ctx, result, args.number, args.field, args.value)
        else:
            data_mod.set_wave_data(ctx, result, args.number, args.field, args.value, mode=args.mode)
        _persist(ctx)
        return result
    if action == "revert":
        result = Result(command="data.revert")
        data_mod.revert(ctx, result, level=args.level)
        _persist(ctx)
        return result
    raise UsageError("unknown data action {0!r}".format(action))


# -- live (tier 2) -----------------------------------------------------------


def cmd_live(ctx, args):
    action = args.live_action
    if action == "status":
        from krcheat.core.live import protocol, transport as transport_mod

        result = Result(command="live.status")
        try:
            bundle = ctx.bundle()
        except KrcheatError:
            bundle = None
        game = paths.find_game_process(bundle)
        channels = protocol.list_channels()
        doctor_state = ctx.state.get("last_doctor") or {}
        payload = {
            "game": game,
            "game_running": game is not None,
            "transports": transport_mod.describe_all(ctx),
            "preferred": ctx.config.get_typed("live.preferred_transport"),
            "channels": channels,
            "overrides": [],
            "doctor": doctor_state,
            "ready": False,
        }
        result.set(**payload)
        result.note(
            "the live channel is milestone M3 and is not built in this build; tier 1 "
            "(profile/backup/data) is fully functional without it"
        )
        return result

    raise milestone(
        "M3",
        "live {0} needs the injected agent (milestone M3), which is not built in this build. "
        "Tier-1 commands (profile show/get/set, backup, data) work now and need no injection.".format(action),
    )


def cmd_play(ctx, args):
    from krcheat.core.live.transport_dylib import DylibTransport

    transport = DylibTransport(ctx=ctx)
    raise milestone("M3", transport.reason)


def cmd_install(ctx, args):
    from krcheat.core.live.transport_patched import PatchedLoveTransport

    raise milestone("M5", PatchedLoveTransport(ctx=ctx).reason)


def cmd_uninstall(ctx, args):
    # Removing F15 shadow modules is tier 1 and *is* implemented, so point at it.
    result = Result(command="uninstall")
    removed = data_mod.revert(ctx, result, level=None)
    result.note("tier-2 artefacts are not present in this build; nothing else to remove")
    return result


def cmd_repair(ctx, args):
    from krcheat.core.live.transport_patched import PatchedLoveTransport

    raise milestone("M5", PatchedLoveTransport(ctx=ctx).reason)


def cmd_patch(ctx, args):
    from krcheat.core.patch import luajit

    if args.patch_action == "scan":
        return luajit.scan(args.module, args.value, kind=args.value_type)
    if args.patch_action == "apply":
        return luajit.apply(None, [])
    return luajit.restore(None)


# -- gui / self-test ---------------------------------------------------------


def cmd_gui(ctx, args):
    from krcheat.gui import app as gui_app

    code = gui_app.run(ctx)
    result = Result(command="gui", exit_code=int(code))
    return result


def cmd_self_test(ctx, args):
    result = selftest_mod.run(ctx, with_oracle=ctx.oracle)
    ctx.state.save()
    return result


def _persist(ctx):
    """Write the machine cache. Never fatal: it is a cache."""
    try:
        ctx.state.save()
    except OSError as exc:
        ctx.log.warn("state.save_failed", error=str(exc))


HANDLERS: Dict[str, Callable[[Any, Any], Result]] = {
    "doctor": cmd_doctor,
    "profile": cmd_profile,
    "backup": cmd_backup,
    "log": cmd_log,
    "config": cmd_config,
    "data": cmd_data,
    "live": cmd_live,
    "play": cmd_play,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "repair": cmd_repair,
    "patch": cmd_patch,
    "gui": cmd_gui,
    "self-test": cmd_self_test,
}


# ---------------------------------------------------------------------------
# Rendering (this is the only place the CLI formats anything)
# ---------------------------------------------------------------------------


def _render(ctx, result, stream):
    if ctx.json_output:
        stream.write(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
    else:
        renderer = RENDERERS.get(result.command.split(".")[0], _render_generic)
        if result.command in RENDERERS:
            renderer = RENDERERS[result.command]
        try:
            renderer(result, stream)
        except Exception as exc:  # rendering must never lose the result
            _render_generic(result, stream)
            stream.write("(renderer failed: {0})\n".format(exc))
    for warning in result.warnings:
        stream.flush()
        sys.stderr.write("warning: {0}\n".format(warning))
    sys.stderr.flush()


def _render_generic(result, stream):
    for change in result.changes:
        stream.write("  " + change.render() + "\n")
    if result.snapshot:
        stream.write("snapshot: {0}\n".format(result.snapshot))
    for note in result.notes:
        stream.write("{0}\n".format(note))
    if not result.changes and not result.notes:
        stream.write("ok\n")


def _render_doctor(result, stream):
    payload = result.payload
    for check in payload.get("checks", []):
        stream.write(
            "[{0:4}] {1}: {2}\n".format(
                str(check.get("status", "")).upper(), check.get("check"), check.get("detail")
            )
        )
    summary = payload.get("summary", {})
    stream.write(
        "\n{0} passed, {1} warning(s), {2} failed\n".format(
            summary.get("pass", 0), summary.get("warn", 0), summary.get("fail", 0)
        )
    )
    for note in result.notes:
        stream.write("{0}\n".format(note))


def _render_self_test(result, stream):
    _render_doctor(result, stream)


def _render_profile_show(result, stream):
    profile = result.payload.get("profile", {})
    stream.write("slot {0} — {1}\n".format(profile.get("slot"), profile.get("path")))
    rows = [
        ("version_string", profile.get("version_string")),
        ("gems", profile.get("gems")),
        ("difficulty", profile.get("difficulty")),
        ("upgrades", _compact(profile.get("upgrades"))),
        (
            "levels",
            "{0} with data, {1} completed, {2} star(s) total".format(
                profile.get("levels_total", 0),
                len(profile.get("levels_completed") or []),
                profile.get("stars_total", 0),
            ),
        ),
        ("heroes", _hero_line(profile)),
        (
            "achievements",
            "{0} of {1} unlocked".format(
                len(profile.get("achievements_unlocked") or []), profile.get("achievements_total", 0)
            ),
        ),
        ("counters", profile.get("counters")),
        (
            "seen",
            "{0} of {1} true".format(profile.get("seen_unlocked", 0), profile.get("seen_total", 0)),
        ),
        ("top-level keys", ", ".join(profile.get("top_level_keys") or [])),
    ]
    width = max(len(name) for name, _value in rows)
    for name, value in rows:
        stream.write("  {0:<{1}} : {2}\n".format(name, width, value))


def _hero_line(profile):
    heroes = profile.get("heroes") or {}
    selected = profile.get("heroes_selected")
    xp = sorted({value for value in heroes.values() if value is not None})
    return "{0} in status, selected {1}, xp values {2}".format(
        len(heroes), selected, xp if len(xp) < 6 else "{0} distinct".format(len(xp))
    )


def _render_profile_get(result, stream):
    stream.write("{0} = {1}\n".format(result.payload.get("path"), repr(result.payload.get("value"))))


def _render_profile_list(result, stream):
    values = result.payload.get("ids", [])
    stream.write("# {0} {1} id(s)\n".format(result.payload.get("count", 0), result.payload.get("kind")))
    for value in values:
        stream.write("{0}\n".format(value))


def _render_profile_set(result, stream):
    _render_changes(result, stream)


def _render_changes(result, stream, limit=40):
    changes = result.changes
    for change in changes[:limit]:
        stream.write("  " + change.render() + "\n")
    if len(changes) > limit:
        stream.write("  ... and {0} more change(s)\n".format(len(changes) - limit))
    if result.snapshot:
        stream.write("snapshot: {0}\n".format(result.snapshot))
    for note in result.notes:
        stream.write("{0}\n".format(note))


def _render_backup_list(result, stream):
    snapshots = result.payload.get("snapshots", [])
    if not snapshots:
        stream.write("no snapshots in {0}\n".format(result.payload.get("directory")))
        return
    stream.write("{0} snapshot(s) in {1}\n".format(len(snapshots), result.payload.get("directory")))
    for item in snapshots:
        if item.get("incomplete"):
            stream.write("  {0}  [INCOMPLETE - no manifest]\n".format(item.get("id")))
            continue
        files = item.get("files", [])
        stream.write(
            "  {0}  {1}  {2} file(s)\n".format(item.get("id"), item.get("created"), len(files))
        )
        stream.write("      command: {0}\n".format(item.get("command")))
        for entry in files:
            stream.write(
                "      {0}  {1} bytes  {2}\n".format(
                    os.path.basename(entry.get("source", "")), entry.get("size"), entry.get("sha256", "")[:16]
                )
            )


def _render_backup_restore(result, stream):
    for item in result.payload.get("restored", []):
        stream.write(
            "  {0} -> {1} ({2} bytes, sha256 {3}...)\n".format(
                item.get("source"), item.get("target"), item.get("bytes"), str(item.get("sha256"))[:16]
            )
        )
    for note in result.notes:
        stream.write("{0}\n".format(note))


def _render_backup_prune(result, stream):
    for name in result.payload.get("removed", []):
        stream.write("  removed {0}\n".format(name))
    for name in result.payload.get("kept", []):
        stream.write("  kept    {0}\n".format(name))
    if not result.payload.get("removed"):
        stream.write("nothing to remove\n")


def _render_log_tail(result, stream):
    records = result.payload.get("records", [])
    stream.write("# {0} record(s) from {1}\n".format(len(records), result.payload.get("path")))
    for record in records:
        if "raw" in record and len(record) == 1:
            stream.write("  {0}\n".format(record["raw"]))
            continue
        skip = {"ts", "lvl", "run", "seq", "event", "cmd"}
        detail = " ".join(
            "{0}={1}".format(key, _short(value))
            for key, value in record.items()
            if key not in skip
        )
        stream.write(
            "  {0} {1:<5} {2} {3}\n".format(
                str(record.get("ts", ""))[11:23],
                str(record.get("lvl", "")),
                record.get("event", ""),
                detail,
            )
        )


def _render_log_path(result, stream):
    stream.write("{0}\n".format(result.payload.get("path")))


def _render_log_prune(result, stream):
    for path in result.payload.get("removed", []):
        stream.write("  removed {0}\n".format(path))
    stream.write("{0} file(s) kept\n".format(len(result.payload.get("kept", []))))


def _render_config_list(result, stream):
    stream.write("# {0}\n".format(result.payload.get("path")))
    for key, value in sorted(result.payload.get("items", {}).items()):
        stream.write("{0} = {1}\n".format(key, config_mod.format_scalar(value)))
    unknown = result.payload.get("unknown", [])
    if unknown:
        stream.write("# unknown keys (preserved): {0}\n".format(", ".join(unknown)))


def _render_config_get(result, stream):
    stream.write("{0} = {1}\n".format(result.payload.get("key"), result.payload.get("value")))


def _render_config_set(result, stream):
    _render_changes(result, stream)


def _render_config_path(result, stream):
    stream.write("{0}\n".format(result.payload.get("path")))


def _render_data_list(result, stream):
    stream.write("save directory: {0}\n".format(result.payload.get("save_dir")))
    levels = result.payload.get("levels", [])
    stream.write("overridable levels: {0}\n".format(", ".join(str(item) for item in levels)))
    installed = result.payload.get("installed", [])
    if not installed:
        stream.write("installed overrides: none\n")
        return
    stream.write("installed overrides:\n")
    for item in installed:
        stream.write(
            "  level {0}  {1}\n".format(item.get("level"), item.get("module"))
        )
        for key, value in sorted((item.get("fields") or {}).items()):
            stream.write("      {0} = {1}\n".format(key, value))
        stream.write("      file: {0}\n".format(item.get("path")))


def _render_data_set(result, stream):
    _render_changes(result, stream)


def _render_data_revert(result, stream):
    _render_changes(result, stream)


def _render_live_status(result, stream):
    payload = result.payload
    game = payload.get("game")
    stream.write("game: {0}\n".format("running (pid {0})".format(game["pid"]) if game else "not running"))
    stream.write("preferred transport: {0}\n".format(payload.get("preferred")))
    for item in payload.get("transports", []):
        stream.write(
            "  {0:<8} {1}\n".format(
                item.get("transport"),
                "available" if item.get("available") else "unavailable: {0}".format(item.get("reason")),
            )
        )
    channels = payload.get("channels", [])
    stream.write("channels: {0}\n".format(
        ", ".join(str(item.get("pid")) for item in channels) if channels else "none"
    ))
    for note in result.notes:
        stream.write("{0}\n".format(note))


RENDERERS: Dict[str, Callable[[Result, Any], None]] = {
    "doctor": _render_doctor,
    "self-test": _render_self_test,
    "profile.show": _render_profile_show,
    "profile.get": _render_profile_get,
    "profile.list": _render_profile_list,
    "profile.set": _render_profile_set,
    "backup.list": _render_backup_list,
    "backup.restore": _render_backup_restore,
    "backup.prune": _render_backup_prune,
    "log.tail": _render_log_tail,
    "log.path": _render_log_path,
    "log.prune": _render_log_prune,
    "config.list": _render_config_list,
    "config.get": _render_config_get,
    "config.set": _render_config_set,
    "config.path": _render_config_path,
    "data.list": _render_data_list,
    "data.set": _render_data_set,
    "data.revert": _render_data_revert,
    "live.status": _render_live_status,
}


def _compact(mapping):
    if not mapping:
        return "-"
    return " ".join("{0}={1}".format(key, value) for key, value in mapping.items())


def _short(value, limit=70):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
