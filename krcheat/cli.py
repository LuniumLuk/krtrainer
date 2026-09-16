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
from typing import Any, Callable, Dict

from krcheat import __version__
from krcheat.core import backup, config as config_mod, context as context_mod
from krcheat.core import data as data_mod, doctor as doctor_mod, log as log_mod, paths
from krcheat.core import profile as profile_mod, selftest as selftest_mod
from krcheat.core import state as state_mod
from krcheat.core.errors import (
    EXIT_CHANNEL,
    EXIT_INTERNAL,
    EXIT_OK,
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


def add_transport_flag(parser):
    """`--transport` selects among the live transports (§11.1).

    Only the tier-2 commands get it: the transport is a strategy for reaching a running
    game, and tier-1 commands do not have one.
    """
    parser.add_argument(
        "--transport",
        metavar="NAME",
        choices=("dylib", "patched", "frida"),
        help="which live transport to use (default: config live.preferred_transport)",
    )


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
    gems.add_argument("value", help="new balance")

    difficulty = setter_sub.add_parser("difficulty", help="set the last-used difficulty (1-4)")
    difficulty.add_argument("value", help="1 easy, 2 normal, 3 hard, 4 impossible")

    upgrades = setter_sub.add_parser("upgrades", help="all=5, or archers=5,barracks=3")
    upgrades.add_argument("spec", help="category=level pairs, or all=<level>")

    stars = setter_sub.add_parser("stars", help="mark levels complete")
    stars.add_argument("scope", choices=("all",), help="the only supported scope")
    stars.add_argument("--stars", type=int, default=3, help="stars per level (0-3, default 3)")
    stars.add_argument("--levels", help="comma-separated level ids (default: every story level)")
    stars.add_argument(
        "--include-endless",
        dest="include_endless",
        action="store_true",
        help="also mark the endless levels the save already has",
    )

    level = setter_sub.add_parser("level", help="tune one level")
    level.add_argument("number", help="level id, e.g. 19 or 81")
    level_sub = level.add_subparsers(dest="level_action", metavar="<action>")
    level_sub.required = True
    level_stars = level_sub.add_parser("stars", help="set the star count")
    level_stars.add_argument("stars", nargs="?", default=None, help="0-3")
    level_stars.add_argument("--mode", choices=tuple(sorted(profile_mod.MODE_INDEX)))
    level_clear = level_sub.add_parser("clear", help="set a mode's completion flag to 0")
    level_clear.add_argument("--mode", choices=tuple(sorted(profile_mod.MODE_INDEX)))

    hero = setter_sub.add_parser("hero", help="hero experience (skills are refused: H6)")
    hero.add_argument("hero", help="hero id, e.g. hero_magnus")
    hero_sub = hero.add_subparsers(dest="hero_action", metavar="<action>")
    hero_sub.required = True
    hero_xp = hero_sub.add_parser("xp", help="set experience")
    hero_xp.add_argument("value")
    hero_skills = hero_sub.add_parser("skills", help="refused: the valid range is unknown (H6)")
    hero_skills.add_argument("spec")

    achievements = setter_sub.add_parser("achievements", help="all | none | ID[,ID...]")
    achievements.add_argument("spec", help="'all', 'none', or one or more ids")

    counters = setter_sub.add_parser("counters", help="set one achievement counter")
    counters.add_argument("achievement", help="counter id, e.g. DIE_HARD")
    counters.add_argument("value")

    seen = setter_sub.add_parser("seen", help="mark every seen.* entry true")
    seen.add_argument("scope", choices=("all",), help="the only supported scope")

    path_set = setter_sub.add_parser(
        "path", help="extension: set any dotted path (e.g. achievements.FIRST_BLOOD true)"
    )
    path_set.add_argument("path", help="dotted path, e.g. levels.7.stars")
    path_set.add_argument("value", help="int, float, true/false, or a string")

    # -- backup --------------------------------------------------------------
    backup_parser = sub.add_parser("backup", help="snapshots")
    add_global_flags(backup_parser, suppress=True)
    backup_sub = backup_parser.add_subparsers(dest="backup_action", metavar="<action>")
    backup_sub.required = True
    backup_sub.add_parser("list", help="list snapshots")
    restore = backup_sub.add_parser("restore", help="restore a snapshot byte-identically")
    restore.add_argument("id", nargs="?", default="latest", help="snapshot id, a unique prefix, or 'latest'")
    restore.add_argument(
        "--verify-only", dest="verify_only", action="store_true",
        help="check the snapshot's hashes without writing anything",
    )
    prune = backup_sub.add_parser("prune", help="keep only the newest N snapshots")
    prune.add_argument("--keep", type=int, default=20, help="how many to keep (default 20)")

    # -- log -----------------------------------------------------------------
    log_parser = sub.add_parser("log", help="the JSONL diagnostic log")
    add_global_flags(log_parser, suppress=True)
    log_sub = log_parser.add_subparsers(dest="log_action", metavar="<action>")
    log_sub.required = True
    tail = log_sub.add_parser("tail", help="print the tail of the log")
    tail.add_argument("--lines", type=int, default=40, help="how many records (default 40)")
    tail.add_argument("--event", help="only records whose event contains this string")
    log_sub.add_parser("path", help="print the active log path")
    log_sub.add_parser("prune", help="apply the retention policy now")

    # -- config --------------------------------------------------------------
    config_parser = sub.add_parser("config", help="config.ini (user-owned)")
    add_global_flags(config_parser, suppress=True)
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="<action>")
    config_sub.required = True
    config_sub.add_parser("list", help="show every key with its effective value")
    config_get = config_sub.add_parser("get", help="read one key")
    config_get.add_argument("key", help="section.key, e.g. logging.level")
    config_set = config_sub.add_parser("set", help="write one key, preserving comments")
    config_set.add_argument("key", help="section.key, e.g. ui.enabled")
    config_set.add_argument("value")
    config_sub.add_parser("path", help="print the config file path")

    # -- data (F15) ----------------------------------------------------------
    data_parser = sub.add_parser("data", help="F15: persistent per-level data (needs spike S2)")
    add_global_flags(data_parser, suppress=True)
    data_sub = data_parser.add_subparsers(dest="data_action", metavar="<action>")
    data_sub.required = True
    data_sub.add_parser("list", help="what can be overridden, and what is installed")
    data_set = data_sub.add_parser("set", help="generate or extend an override")
    data_set_sub = data_set.add_subparsers(dest="target", metavar="<target>")
    data_set_sub.required = True
    data_level = data_set_sub.add_parser("level", help="per-level starting gold or lives")
    data_level.add_argument("number", help="level id")
    data_level.add_argument("field", help="starting_gold | starting_lives")
    data_level.add_argument("value")
    data_wave = data_set_sub.add_parser("wave", help="wave rewards (refused: shape unverified)")
    data_wave.add_argument("number", help="level id")
    data_wave.add_argument("field")
    data_wave.add_argument("value")
    data_wave.add_argument("--mode", default="campaign")
    data_revert = data_sub.add_parser("revert", help="remove overrides; shipped values return")
    data_revert.add_argument("--level", type=int, help="only this level")
    data_revert.add_argument("--all", dest="revert_all", action="store_true")

    # -- live (tier 2) -------------------------------------------------------
    live = sub.add_parser("live", help="tier 2: the in-process channel")
    add_global_flags(live, suppress=True)
    add_transport_flag(live)
    live_sub = live.add_subparsers(dest="live_action", metavar="<action>")
    live_sub.required = True
    live_status = live_sub.add_parser("status", help="channel health, agent, active overrides")
    live_status.add_argument("--agent-log", action="store_true", help="also print the agent's log")
    live_status.add_argument("--stop-keeper", action="store_true",
                             help="ask a background keeper to release its overrides")
    live_status.add_argument("--prune", action="store_true",
                             help="delete channel directories left by dead processes")
    probe = live_sub.add_parser("probe", help="dump the game's globals to resolve field paths")
    probe.add_argument("--out", metavar="FILE", help="write the dump here")
    probe.add_argument("--depth", type=int, default=2, help="how deep to walk (1-4)")
    for name in ("gold", "lives"):
        item = live_sub.add_parser(name, help="write once, force every frame, or release")
        item.add_argument("value", help="N (once) | infinity (every frame) | off (restore)")
        item.add_argument("--keep", action="store_true",
                          help="leave it on after this command exits, via a keeper process")
    speed = live_sub.add_parser("speed", help="simulation multiplier (every frame)")
    speed.add_argument("value", help="multiplier | off")
    speed.add_argument("--keep", action="store_true", help="leave it on after this command exits")
    god = live_sub.add_parser("god", help="disable life checking")
    god.add_argument("state", choices=("on", "off"))
    god.add_argument("--keep", action="store_true", help="leave it on after this command exits")
    off = live_sub.add_parser("off", help="release every override and restore the captured values")
    off.add_argument("--all", action="store_true", help="same thing (explicit)")
    evaluate = live_sub.add_parser("eval", help="evaluate Lua once and print the result")
    evaluate.add_argument("code")
    live_sub.add_parser("watch", help="interactive prompt, until Ctrl-D")

    # -- agent (the injected dylib) -----------------------------------------
    agent = sub.add_parser("agent", help="build and inspect the injected agent (tier 2)")
    add_global_flags(agent, suppress=True)
    agent_sub = agent.add_subparsers(dest="agent_action", metavar="<action>")
    agent_sub.required = True
    agent_build = agent_sub.add_parser("build", help="compile the agent dylib")
    agent_build.add_argument("--force", action="store_true", help="rebuild even if current")
    agent_sub.add_parser("status", help="where the agent is, and whether it is current")

    # -- install / play / patch ---------------------------------------------
    play = sub.add_parser("play", help="launch the game with the agent loaded")
    add_global_flags(play, suppress=True)
    add_transport_flag(play)
    play.add_argument("--no-cheats", dest="no_cheats", action="store_true",
                      help="launch the agent but register no overrides")
    play.add_argument("--rebuild-agent", action="store_true", help="recompile the agent first")
    play.add_argument("--wait", type=float, default=None,
                      help="seconds to wait for the channel (default 45)")
    play.add_argument("steam_args", nargs=argparse.REMAINDER)

    for name, help_text in (
        ("install", "install transport B's bootstrap into the save directory"),
        ("uninstall", "remove every tier-2 artefact"),
        ("repair", "re-apply transport B after an integrity check, and rebuild the agent"),
    ):
        item = sub.add_parser(name, help=help_text)
        add_global_flags(item, suppress=True)
        if name == "install":
            item.add_argument(
                "--check",
                action="store_true",
                help="read the evidence the game wrote, and record the S2 verdict",
            )

    patch = sub.add_parser("patch", help="tier 3: bytecode patching (backlog)")
    add_global_flags(patch, suppress=True)
    patch_sub = patch.add_subparsers(dest="patch_action", metavar="<action>")
    patch_sub.required = True
    scan = patch_sub.add_parser("scan", help="find constants in a module's bytecode")
    scan.add_argument("value")
    scan.add_argument("--module", metavar="PATH", help="which module to scan")
    scan.add_argument("--type", dest="value_type", choices=("number", "int"), default="number")
    patch_sub.add_parser("apply", help="write a patched copy of the archive")
    patch_sub.add_parser("restore", help="restore the pristine archive")

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
    # Persist after every command, not only the tier-1 writes. The live channel keeps its request
    # counter here, and without this a fresh process starts again at id 1 — which is how every
    # `live` command once reported the previous command's result (D14). A second call is free:
    # `State.save()` does nothing when nothing is dirty.
    _persist(ctx)
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

    if level is None:
        if verbose:
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
    """`profile list` is a reference command.

    It answers from the archive when it can, so it must not prompt for a slot: asking
    "which slot?" before printing the game's own id list is noise. A slot is only loaded
    when it was passed explicitly, or when the answer genuinely lives in a save.
    """
    kind = args.kind
    needs_save = kind == "counters"
    profile = None
    if needs_save or getattr(args, "slot", None) is not None:
        try:
            profile = _load_profile(ctx, args)
        except KrcheatError:
            if needs_save:
                raise
            profile = None
    values = profile_mod.list_ids(ctx, kind, profile=profile)
    result = Result(command="profile.list")
    result.set(kind=kind, count=len(values), ids=values)
    if profile is None and kind != "levels":
        result.note("ids mined from the archive; pass --slot N to merge in a save's own keys")
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
            # This run holds the current log open; deleting it underneath would lose the
            # records being written right now.
            protect=ctx.log.path,
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

#: The sentence every `live` command should not have to repeat. Stated once, in one place,
#: because it is the single most surprising thing about this tool: a cheat does not persist
#: by magic, it persists because a process keeps asking for it (§11.7.4).
KEEP_NOTE = (
    "this override is released when this command exits (the agent clears on a stale "
    "heartbeat). Add --keep to leave it running."
)


def _live_error(response):
    """The agent's own words for why a request failed, for the user rather than the log."""
    if response is None:  # pragma: no cover - the transport raises before this
        return "no response"
    return response.error or "the agent reported a failure with no message"


def _live_applied(response):
    """Did the *snippet* say it worked?

    Two different questions, and conflating them hides real failures. The agent's `ok` means "the
    snippet ran without raising a Lua error" — it knows nothing about the game. A snippet reports
    its own verdict in its JSON result (`ok = false`), which is how `speed` says this build has no
    field to write, and how `gold` says an assignment did not take. The CLI has to read that
    verdict, or a cheat that did nothing reads as a cheat that worked.
    """
    if response is None or not response.ok:
        return False
    decoded = response.decoded()
    if isinstance(decoded, dict) and decoded.get("ok") is False:
        return False
    return True


def _live_failure_note(result, response):
    """Explain a failure in whichever of the two places it was reported, and exit accordingly.

    A snippet that refused — `speed` on a build with no such field, an assignment that did not
    take — means the request could not be carried out, so the run exits 3 rather than 0 with a
    warning. A script that only checks the exit code must not read "this build cannot do it" as
    success.
    """
    decoded = response.decoded() if response is not None else None
    if isinstance(decoded, dict) and decoded.get("error"):
        result.warn(decoded["error"])
    elif response is not None and not response.ok:
        result.warn(_live_error(response))
    result.ok = False
    result.exit_code = EXIT_CHANNEL
    return result


def _live_transport(ctx, args, launch=False):
    """Resolve the transport and make sure there is a live channel behind it."""
    from krcheat.core.errors import ChannelUnavailable
    from krcheat.core.live import transport as transport_mod

    transport = transport_mod.select(ctx, getattr(args, "transport", None))
    usable, reason = transport.available()
    if not usable:
        raise ChannelUnavailable(reason)
    if reason:
        ctx.log.info("agent.pending", reason=reason)
    transport.start(launch=launch)
    return transport


def _keep_or_note(ctx, args, transport, keys, result):
    """`--keep` spawns a keeper; otherwise say plainly when the override ends.

    Only called when the override actually took effect. Saying "this is released when the
    command exits" after a failure describes an override that was never registered, which is
    how a refusal gets read as a success.
    """
    from krcheat.core.live import keeper

    if getattr(args, "keep", False):
        pid = keeper.spawn(transport, keys, transport.heartbeat_timeout())
        result.set(keeper_pid=pid, kept=True)
        result.note(
            "kept: a background process (pid {0}) is holding the heartbeat, so the override "
            "survives this command. Stop it with `krcheat live off` or `krcheat live status "
            "--stop-keeper`.".format(pid)
        )
    else:
        result.note(KEEP_NOTE.format())
    return result


def _live_value_command(ctx, args, key, value):
    """`gold`/`lives`: a one-shot write, a per-frame override, or a restore."""
    from krcheat.core.live import snippets

    transport = _live_transport(ctx, args)
    result = Result(command="live.{0}".format(key))

    if value is None or str(value).lower() == "off":
        # §11.7.3: `off` restores the captured value; it does not merely stop enforcing.
        response = transport.clear(key)
        result.set(key=key, mode="clear", agent=response.to_dict())
        result.note(
            "{0}: released, and the value captured when it was registered is restored".format(key)
        )
        return result

    if str(value).lower() == "infinity":
        code, capture, restore = snippets.override_snippets(key, None)
        snippets.assert_safe(code)
        response = transport.always(key, code, capture=capture, restore=restore)
        result.set(key=key, mode="always", agent=response.to_dict())
        if _live_applied(response):
            _keep_or_note(ctx, args, transport, [key], result)
        else:
            # The capture resolved the table or it did not; either way the answer is the report,
            # and there is no override to make promises about.
            _live_failure_note(result, response)
        return result

    try:
        number = int(str(value))
    except ValueError:
        raise UsageError(
            "{0} expects a number, 'infinity' or 'off'; got {1!r}".format(key, value)
        )
    if number < 0:
        raise UsageError("{0} cannot be negative".format(key))
    response = transport.once(snippets.for_override(key, number))
    result.set(key=key, mode="once", value=number, agent=response.to_dict())
    result.note(
        "written once. The game keeps spending from it, so re-run with 'infinity' to hold it."
    )
    return result


def cmd_live(ctx, args):
    from krcheat.core.live import agent as agent_mod
    from krcheat.core.live import protocol, snippets
    from krcheat.core.live import transport as transport_mod

    action = args.live_action

    if action == "status":
        from krcheat.core.live import keeper

        result = Result(command="live.status")
        bundle = ctx.bundle_or_none()
        game = paths.find_game_process(bundle)
        channels = protocol.list_channels()
        preferred = ctx.config.get_typed("live.preferred_transport")
        transports = transport_mod.describe_all(ctx)
        chosen = None
        for item in transports:
            if item.get("transport") == preferred:
                chosen = item
        payload = {
            "game": game,
            "game_running": game is not None,
            "transports": transports,
            "preferred": preferred,
            "channels": channels,
            "stale_channels": len(protocol.stale_channels()),
            "overrides": [],
            "live": False,
            "keeper": {"running": False},
            # Reported whether or not a channel exists: when the channel is down, "is the
            # agent built?" is the first question, and a status command that answers it only
            # on the happy path is not a diagnostic.
            "agent_build": agent_mod.describe(getattr(ctx, "log", None)),
        }
        if args.prune:
            # A minute, not `prune_channels`' conservative hour: an explicit `--prune` is a
            # request to clean up now, and the only thing the age guard protects against is
            # racing a process that has just been launched and has not written its log yet.
            removed = protocol.prune_channels(min_age=60.0)
            payload["pruned"] = removed
            payload["channels"] = protocol.list_channels()
            payload["stale_channels"] = len(protocol.stale_channels(min_age=60.0))
            result.note("pruned {0} abandoned channel(s)".format(len(removed)))

        if channels:
            newest = channels[0]
            payload["channel"] = newest["path"]
            payload["keeper"] = keeper.describe(newest["path"])
            if args.stop_keeper and keeper.describe(newest["path"]).get("running"):
                stopped = keeper.stop(channel_path=newest["path"])
                result.note("asked the keeper to stop: {0}".format("sent" if stopped else "failed"))

        try:
            transport = _live_transport(ctx, args)
        except KrcheatError as exc:
            payload["live"] = False
            payload["live_error"] = str(exc)
            result.set(**payload)
            if chosen and chosen.get("agent", {}).get("built") is None:
                result.note(
                    "the agent is not built yet. It is compiled once, on demand, by "
                    "`krcheat agent build`, or implicitly by `krcheat play`."
                )
            return result

        payload["live"] = True
        payload["transport"] = transport.describe()
        try:
            payload["overrides"] = transport.status().decoded() or {}
        except KrcheatError as exc:
            payload["live"] = False
            payload["live_error"] = str(exc)
        if args.agent_log:
            payload["agent_log"] = transport.agent_log(40)
        result.set(**payload)
        return result

    if action == "probe":
        transport = _live_transport(ctx, args)
        response = transport.once(snippets.probe(depth=getattr(args, "depth", 2)))
        payload = response.decoded()
        result = Result(command="live.probe")
        result.set(ok=response.ok, error=response.error, source=response.result)
        if isinstance(payload, dict) and isinstance(payload.get("paths"), dict):
            paths_found = payload["paths"]
            result.set(paths=paths_found)
            result.note(
                "resolved {0} reachable path(s); the owner chain to use is the one that "
                "contains player_gold".format(len(paths_found))
            )
            if args.out:
                _write_text(args.out, json.dumps(paths_found, indent=2, sort_keys=True))
                result.note("written to {0}".format(args.out))
        elif not response.ok:
            result.ok = False
            result.exit_code = EXIT_CHANNEL
        return result

    if action == "eval":
        transport = _live_transport(ctx, args)
        response = transport.once(snippets.eval_snippet(args.code))
        result = Result(command="live.eval")
        result.set(ok=response.ok, error=response.error, result=response.decoded())
        if not response.ok:
            result.ok = False
            result.exit_code = EXIT_CHANNEL
        return result

    if action == "off":
        transport = _live_transport(ctx, args)
        from krcheat.core.live import keeper

        channel = transport.channel()
        if channel is not None:
            keeper.stop(channel_path=channel.path)
        response = transport.clear_all()
        result = Result(command="live.off")
        result.set(agent=response.to_dict())
        result.note("every override released, and every captured value restored")
        return result

    if action == "watch":
        return _live_watch(ctx, args)

    if action in ("gold", "lives"):
        return _live_value_command(ctx, args, action, args.value)

    if action == "speed":
        transport = _live_transport(ctx, args)
        result = Result(command="live.speed")
        if str(args.value).lower() == "off":
            response = transport.clear("speed")
            result.set(key="speed", mode="clear", agent=response.to_dict())
            result.note("speed: released, and the captured multiplier restored")
            return result
        try:
            multiplier = float(args.value)
        except ValueError:
            raise UsageError("speed expects a number or 'off'; got {0!r}".format(args.value))
        if multiplier <= 0:
            raise UsageError("a speed multiplier must be greater than zero")
        code, capture, restore = snippets.override_snippets("speed", multiplier)
        snippets.assert_safe(code)
        response = transport.always("speed", code, capture=capture, restore=restore)
        result.set(key="speed", mode="always", value=multiplier, agent=response.to_dict())
        if _live_applied(response):
            _keep_or_note(ctx, args, transport, ["speed"], result)
        else:
            _live_failure_note(result, response)
            result.note(snippets.SPEED_UNAVAILABLE_NOTE)
        return result

    if action == "god":
        transport = _live_transport(ctx, args)
        result = Result(command="live.god")
        if args.state == "off":
            response = transport.clear("god")
            result.set(key="god", mode="clear", agent=response.to_dict())
            result.note("god: released, and the captured game_outcome restored")
            return result
        # Refused by default, and this is the honest position rather than caution for its own
        # sake: the sentinel is a guess, `game_outcome` is read by six shipped modules including
        # gameplay ones, and a value they misread could end the level. `lives infinity` is the
        # mechanism that was measured to work, and it needs no sentinel.
        if not ctx.force:
            raise UsageError(
                "god mode is not implemented on the tested build, on purpose. "
                "`game_outcome` is nil while a level runs, so no value for it has been observed; "
                "six shipped modules read that field, including gameplay code, so writing a "
                "guessed value into it could end your level rather than protect it.\n"
                "  What works, and is measured: `krcheat live lives infinity`, which holds the "
                "life counter so it cannot reach zero.\n"
                "  If you want to experiment anyway, `krcheat live god on --force` writes "
                "{0} into it and `live god off` puts back whatever was there.".format(
                    snippets.GOD_SENTINEL
                )
            )
        code, capture, restore = snippets.override_snippets("god")
        snippets.assert_safe(code)
        response = transport.always("god", code, capture=capture, restore=restore)
        result.set(key="god", mode="always", agent=response.to_dict())
        result.warn(
            "god mode writes an unverified sentinel into game_outcome; if the level ends "
            "unexpectedly, that is why. `live god off` restores it."
        )
        if _live_applied(response):
            _keep_or_note(ctx, args, transport, ["god"], result)
        else:
            _live_failure_note(result, response)
        return result

    raise UsageError("unknown live action {0!r}".format(action))


def _live_watch(ctx, args):
    """An interactive prompt. Reads stdin, evaluates one snippet at a time (§10.3)."""
    from krcheat.core.live import snippets

    transport = _live_transport(ctx, args)
    result = Result(command="live.watch")
    evaluated = 0
    stream = sys.stdin
    if stream is None or not stream.isatty():
        raise UsageError(
            "live watch needs a terminal on stdin. In a script or a pipe, use "
            "`krcheat live eval \"<lua>\"` for each snippet instead."
        )
    sys.stdout.write("krcheat live watch — Lua, one snippet per line; Ctrl-D to leave\n")
    sys.stdout.flush()
    while True:
        try:
            line = stream.readline()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            break
        if not line:
            break
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.strip() in ("quit", "exit", "\\q"):
            break
        try:
            response = transport.once(snippets.eval_snippet(line))
        except KrcheatError as exc:
            sys.stdout.write("error: {0}\n".format(exc))
            sys.stdout.flush()
            break
        evaluated += 1
        sys.stdout.write("{0}\n".format(json.dumps(response.to_dict(), ensure_ascii=False)))
        sys.stdout.flush()
    result.set(evaluated=evaluated)
    return result


def cmd_agent(ctx, args):
    from krcheat.core.live import agent as agent_mod

    if args.agent_action == "build":
        result = Result(command="agent.build")
        built = agent_mod.build(force=args.force, logger=ctx.log)
        result.set(path=built, fingerprint=agent_mod.fingerprint()[:16])
        result.note("inject it with `krcheat play`, which sets {0}".format(
            "DYLD_INSERT_LIBRARIES"))
        return result

    result = Result(command="agent.status")
    result.set(**agent_mod.describe(ctx.log))
    return result


def cmd_play(ctx, args):
    from krcheat.core.live.transport_dylib import DylibTransport

    transport = DylibTransport(ctx=ctx)
    result = Result(command="play")
    if args.rebuild_agent:
        transport.agent_path(force_build=True)
    pid = transport.launch(wait=args.wait)
    result.set(pid=pid, channel=transport.channel().path if transport.channel() else None)
    if args.no_cheats:
        result.note("the agent is loaded but no overrides are registered (--no-cheats)")
    else:
        result.note(
            "the game is running with the agent loaded. Use `krcheat live status` to see the "
            "channel, and `krcheat live gold infinity` to add an override."
        )
    if args.steam_args:
        result.warn(
            "extra arguments were given but are not passed to the game: the launcher is the "
            "bundle's own executable, and it does not take Steam's arguments"
        )
    return result


def cmd_install(ctx, args):
    """Transport B: one generated file in the save directory, and then ask the game (S2)."""
    from krcheat.core.live.transport_patched import PatchedLoveTransport

    transport = PatchedLoveTransport(ctx=ctx)
    result = Result(command="install")

    if args.check:
        evidence = transport.check()
        result.set(evidence=evidence, verdict=transport.verdict())
        if evidence.get("loaded"):
            result.note(
                "the game loaded the bootstrap from the save directory, so transport B works "
                "on this install. The hook it used was {0}.".format(evidence.get("hook"))
            )
        elif evidence.get("conclusive"):
            result.note("the bootstrap did not take effect: {0}".format(evidence.get("reason")))
            result.warn(
                "spike S2 has failed for this build, so transport B would have to be the "
                "ZIP-repack variant. Keep using the dylib transport, which needs no game files "
                "changed at all."
            )
        else:
            # Not a failure yet, and saying otherwise would be wrong: the game has simply not
            # been started since the install.
            result.note(evidence.get("reason"))
            result.note(evidence.get("hint"))
        return result

    written = transport.install()
    result.set(**written)
    result.note(
        "written: {0} (shadowing {1} inside game.love)".format(
            written["shadow"], transport_mod_shadow_name()
        )
    )
    result.note(
        "now launch the game once — from Steam is fine — and then run `krcheat install "
        "--check`. Until the game confirms it loaded our file, transport B is unverified and "
        "`live status` will say so."
    )
    return result


def transport_mod_shadow_name():
    from krcheat.core.live.transport_patched import SHADOW_MODULE

    return SHADOW_MODULE


def cmd_uninstall(ctx, args):
    """Remove every tier-2 artefact. Tier 2 touches the game installation only via this."""
    from krcheat.core.live.transport_patched import PatchedLoveTransport

    result = Result(command="uninstall")
    removed = []
    try:
        removed = PatchedLoveTransport(ctx=ctx).uninstall()
    except (KrcheatError, OSError) as exc:
        result.warn("transport B artefacts were not removed: {0}".format(exc))
    result.set(removed_transport_b=removed)

    # F15 shadow modules are tier 1's, and they live in the same save directory.
    data_mod.revert(ctx, result, level=None)

    from krcheat.core.live import agent as agent_mod

    agents = []
    for path in _agent_build_files():
        try:
            os.remove(path)
            agents.append(os.path.basename(path))
        except OSError:
            continue
    result.set(removed_agents=agents)
    result.note(
        "every built agent dylib was removed; the next live command rebuilds it. Transport A "
        "never modifies the game installation, so there is nothing else to undo."
    )
    return result


def _agent_build_files():
    from krcheat.core.live import agent as agent_mod

    directory = agent_mod.build_dir()
    try:
        return [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.startswith("kr_agent-") and name.endswith(".dylib")
        ]
    except OSError:
        return []


def cmd_repair(ctx, args):
    """Re-apply the transport that is in use.

    Transport A modifies nothing, so its only failure mode is a missing dylib, and its repair
    is a rebuild. Transport B is a file in a directory Steam can revert, so its repair is a
    rewrite — and it is only meaningful if it was installed in the first place.
    """
    from krcheat.core.live import agent as agent_mod
    from krcheat.core.live.transport_patched import PatchedLoveTransport

    result = Result(command="repair")
    transport_b = PatchedLoveTransport(ctx=ctx)
    if transport_b.installed():
        written = transport_b.repair()
        result.set(transport_b=written)
        result.note("transport B bootstrap rewritten: {0}".format((written or {}).get("shadow")))
        verdict = transport_b.verdict()
        if not verdict:
            result.note("it is still unverified: run the game and `krcheat install --check`")
    else:
        result.note(
            "transport B is not installed (and does not need to be: it modifies game files, "
            "while the dylib transport modifies nothing)."
        )
    built = agent_mod.build(force=True, logger=ctx.log)
    result.set(agent=built, rebuilt=True)
    result.note("the agent was rebuilt from source at {0}".format(built))
    return result


def cmd_patch(ctx, args):
    from krcheat.core.patch import luajit

    if args.patch_action == "scan":
        return luajit.scan(args.module, args.value, kind=args.value_type)
    if args.patch_action == "apply":
        return luajit.apply(None, [])
    return luajit.restore(None)


# -- gui / self-test ---------------------------------------------------------


def cmd_gui(ctx, args):
    """The GUI is opt-in (decision D10): macOS use is CLI-only, so it is off by default."""
    if not config_mod.gui_enabled(ctx.config):
        raise UsageError(
            "the GUI is disabled (ui.enabled = false). Enable it with '{0}', or keep using "
            "the CLI \u2014 every operation is available there.".format(config_mod.GUI_ENABLE_HINT)
        )
    from krcheat.gui import run as gui_run

    code = gui_run(ctx)
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


def _write_text(path, text):
    """Write a report the user asked for. Not a save file: no snapshot, no gates."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        raise UsageError("cannot write {0}: {1} is not a directory".format(path, directory))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")
    except OSError as exc:
        raise UsageError("cannot write {0}: {1}".format(path, exc))
    return path


HANDLERS: Dict[str, Callable[[Any, Any], Result]] = {
    "doctor": cmd_doctor,
    "profile": cmd_profile,
    "backup": cmd_backup,
    "log": cmd_log,
    "config": cmd_config,
    "data": cmd_data,
    "live": cmd_live,
    "agent": cmd_agent,
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
        renderer = RENDERERS.get(result.command, _render_generic)
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
    source = result.payload.get("path") or "(no log file yet)"
    stream.write("# {0} record(s) from {1}\n".format(len(records), source))
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
    stream.write(
        "game: {0}\n".format(
            "running (pid {0})".format(game["pid"]) if game else "not running"
        )
    )
    stream.write("channel: {0}\n".format("live" if payload.get("live") else "no"))
    if payload.get("live") and payload.get("channel"):
        stream.write("  path: {0}\n".format(payload["channel"]))
    stream.write("preferred transport: {0}\n".format(payload.get("preferred")))
    for item in payload.get("transports", []):
        stream.write(
            "  {0:<8} {1}\n".format(
                item.get("transport"),
                "available" if item.get("available") else "unavailable: {0}".format(item.get("reason")),
            )
        )
    agent = (payload.get("transport") or {}).get("agent") or payload.get("agent_build")
    if agent:
        stream.write("agent: {0}\n".format(agent.get("built") or "not built"))
        if agent.get("reason"):
            stream.write("  {0}\n".format(agent["reason"]))
    keeper = payload.get("keeper") or {}
    if keeper.get("running"):
        stream.write("keeper: holding the heartbeat (pid {0})\n".format(keeper.get("pid")))
    overrides = payload.get("overrides") or {}
    entries = overrides.get("overrides") if isinstance(overrides, dict) else None
    if entries:
        stream.write("overrides:\n")
        for entry in entries:
            stream.write(
                "  {0:<6} applied={1} frames_since={2}{3}\n".format(
                    entry.get("key"),
                    entry.get("applied"),
                    entry.get("frames_since_applied"),
                    "  captured={0}".format(entry.get("saved")) if entry.get("saved") else "",
                )
            )
    else:
        stream.write("overrides: none\n")
    if payload.get("live_error"):
        stream.write("why not live: {0}\n".format(payload["live_error"]))
    channels = payload.get("channels", [])
    stream.write(
        "channels: {0}\n".format(
            " ".join(str(item.get("pid")) for item in channels) if channels else "none"
        )
    )
    stale = payload.get("stale_channels") or 0
    if stale:
        stream.write(
            "  {0} abandoned channel(s) from dead processes; "
            "`krcheat live status --prune` removes them\n".format(stale)
        )
    for line in payload.get("agent_log") or []:
        stream.write("  agent | {0}\n".format(line))
    for note in result.notes:
        stream.write("{0}\n".format(note))


def _render_live_probe(result, stream):
    payload = result.payload
    paths_found = payload.get("paths") or {}
    if payload.get("error"):
        stream.write("error: {0}\n".format(payload["error"]))
    if paths_found:
        for key in sorted(paths_found)[:200]:
            stream.write("{0} = {1}\n".format(key, paths_found[key]))
        if len(paths_found) > 200:
            stream.write("... and {0} more; use --out FILE for the whole dump\n".format(
                len(paths_found) - 200))
    elif not payload.get("error"):
        stream.write("no paths reported\n")
    for note in result.notes:
        stream.write("{0}\n".format(note))


def _render_live_result(result, stream):
    """`live gold`, `lives`, `speed`, `god`, `eval`, `off` — the shape is the same."""
    payload = result.payload
    if payload.get("key"):
        stream.write("{0}: {1}\n".format(payload["key"], payload.get("mode")))
    agent = payload.get("agent") or {}
    if agent.get("result") is not None:
        stream.write("{0}\n".format(_short(agent["result"])))
    if agent.get("error"):
        stream.write("agent error: {0}\n".format(agent["error"]))
    if payload.get("result") is not None:
        stream.write("{0}\n".format(_short(payload["result"])))
    if payload.get("error"):
        stream.write("error: {0}\n".format(payload["error"]))
    for line in result.notes:
        stream.write("{0}\n".format(line))


def _render_agent_status(result, stream):
    payload = result.payload
    if payload.get("path"):
        stream.write("built: {0}\n".format(payload["path"]))
        stream.write("fingerprint: {0}\n".format(payload.get("fingerprint")))
    else:
        stream.write("source: {0}\n".format(payload.get("source_dir")))
        stream.write("fingerprint: {0}\n".format(payload.get("fingerprint")))
        stream.write("built: {0}\n".format(payload.get("built") or "not yet"))
        stream.write("build dir: {0}\n".format(payload.get("build_dir")))
        stream.write("architectures: {0}\n".format(payload.get("archs")))
        tools = payload.get("toolchain") or {}
        stream.write("clang: {0}\n".format(tools.get("clang") or "not found"))
        stream.write("codesign: {0}\n".format(tools.get("codesign") or "not found"))
        stream.write("usable: {0}\n".format("yes" if payload.get("usable") else "no"))
        if payload.get("reason"):
            stream.write("  {0}\n".format(payload["reason"]))
    for line in result.notes:
        stream.write("{0}\n".format(line))


def _render_play(result, stream):
    payload = result.payload
    stream.write("game pid: {0}\n".format(payload.get("pid")))
    if payload.get("channel"):
        stream.write("channel: {0}\n".format(payload["channel"]))
    for line in result.notes:
        stream.write("{0}\n".format(line))


def _render_live_watch(result, stream):
    stream.write("{0} snippet(s) evaluated\n".format(result.payload.get("evaluated", 0)))


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
    "live.probe": _render_live_probe,
    "live.eval": _render_live_result,
    "live.off": _render_live_result,
    "live.gold": _render_live_result,
    "live.lives": _render_live_result,
    "live.speed": _render_live_result,
    "live.god": _render_live_result,
    "live.watch": _render_live_watch,
    "agent.build": _render_agent_status,
    "agent.status": _render_agent_status,
    "play": _render_play,
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
