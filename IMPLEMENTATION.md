# Implementation notes

How the code maps onto [`KRCHEAT_FOUNDATION.md`](KRCHEAT_FOUNDATION.md): what exists, what does
not, and where the implementation deliberately goes further than the spec.

The spec remains canonical. Where this document and the spec disagree, the spec is right and the
code is a bug.

---

## 1. What is built

| Spec ref | Module | State |
| --- | --- | --- |
| §12 (D1) | `core/lua_table.py` | **done** — lossless parser and writer |
| §9.2, §10.2 | `core/profile.py` | **done** — F3–F8, `show`/`get`/`list`/`set` |
| §15 (D6) | `core/safety.py` | **done** — §15.3 gates, §15.2 write path |
| §10.4, §15.2 | `core/backup.py` | **done** — snapshot, restore, manifest, prune |
| D4 / S8 | `core/oracle.py` | **done** — the shipped `Lua.framework` via `ctypes`, in a subprocess |
| §13 | `core/mine.py` | **done (heuristic)** — ids from the archive's constant pool |
| §9.8 (F15) | `core/data.py` | **generation done**, effect unverified until spike S2 |
| §9.6 (D8) | `core/log.py` | **done** — JSONL, rotation, pruning |
| §9.7 (D8) | `core/config.py`, `core/state.py` | **done** — comment-preserving `config.ini`, keyed cache |
| §10.1 | `core/doctor.py` | **done** — every check of §10.1 |
| §10.7 (D9) | `core/paths.py` | **done** — `find_slots`, `resolve_slot` |
| §16.2 | `core/selftest.py` | **done** — the regression guard behind `--self-test` |
| §10 | `cli.py` | **done** — the whole documented surface, with renderers |
| §11.2–11.3 | `core/live/protocol.py` | **done** — framing, atomic handoff, heartbeat |
| §11.6 | `core/live/snippets.py` | **done** — templates plus the loop guard |
| §11.1 | `core/live/transport*.py` | **interface only** — every transport reports its milestone |
| §14 | `core/patch/luajit.py` | **backlog** — `is_bytecode` only |
| §9.5 (F16) | `gui/` | **written**, unusable on this machine's Tk 8.5 (see §5) |
| §11 | `agent/kr_agent.c` | **not written** (milestone M3) |

Feature status, per §8.1:

| Feature | State |
| --- | --- |
| F3 upgrades, F4 stars, F5 gems, F6 hero XP, F7 achievements, F8 `seen` | working |
| F14 backup / restore / doctor | working |
| F15 per-level data | generated; effect unverified pending S2 |
| F1 gold, F2 lives, F9 speed, F10 god, F11 eval, F12 probe | not built (tier 2, M3) |
| F13 bytecode patching | backlog (M8) |
| F16 tkinter GUI | written; blocked by the interpreter's Tk |

Anything unimplemented fails with **exit 3** and a sentence naming the milestone, never silently
and never with a traceback.

---

## 2. Where the code goes further than the spec

Kept deliberately small, and each one is visible in `--help`:

| Addition | Why |
| --- | --- |
| `core/safety.py`, `core/context.py`, `core/doctor.py`, `core/selftest.py` | The spec's §13 table lists key APIs, not an exhaustive file list. The write path and the gates are needed by both `profile.py` and `data.py`, so they live in one place rather than being duplicated; `Ctx` is the `ctx` of the `fn(ctx, **args)` contract. |
| `profile set path <dotted.path> <value>` | A general escape hatch, useful for fields the CLI has no verb for. It goes through the same write path, the same no-deletion check and the same oracle validation as every other edit. |
| `krcheat self-test` (as well as `--self-test`) | §16.2 specifies the flag; the subcommand is the same thing, where a subcommand is more natural. |
| `--no-oracle` | The oracle costs ~0.6 s per write. On by default; switchable per run and in `config.ini` (`safety` holds the switch in future). |
| `krcheat gui` refuses before starting Tk | Forced by a real defect, not a preference — see §5. |
| `wallet`-style `backup restore --verify-only` | §15.2 step 5 verification without writing, which is what makes a restore auditable. |

Deliberate **non**-additions: `profile set hero <id> skills` raises (H6 — the valid range is
unknown and the game clamps out-of-range values to 0, so guessing would report success and do
nothing), and `data set wave` raises for the same reason (the wave table's shape is unmeasured).

---

## 3. Decisions the implementation had to make concrete

These are places where the spec left room, and the choice is recorded here so it can be
disagreed with.

1. **A no-op writes nothing at all.** §12.2 says a command that changes nothing produces a
   byte-identical file; the implementation goes one step further and *does not write*, asserting
   in-memory that `render() == source` first. Writing identical bytes would only add a swap and a
   snapshot for no change. The invariant is still tested directly (`--self-test`, golden tests).
2. **`clear` writes `0`, it never deletes.** §12.3 forbids deleting keys; `0` is how the game
   itself represents "not completed".
3. **New numeric keys are inserted in ascending order.** Existing entries are never reordered
   (§12.2); a *new* `levels[19]` lands between `[18]` and `[20]` so a hand-read file stays
   readable.
4. **A post-write mismatch is reported, not auto-restored.** §15.2 step 4 says the check catches
   a third party writing between steps 2 and 3. Because we cannot tell our own failure from
   somebody else's write, the command refuses to silently revert the other writer: it exits 6 and
   names the snapshot id to restore from.
5. **The oracle also validates saves, not just generated modules.** A save chunk is pure data, so
   it loads and runs standalone in the game's own VM — which is a stronger check than "our parser
   accepts our own output", and it is what makes §15.2 step 2 authoritative rather than circular.
6. **Slot discovery is confined to the directories where shadow modules can live**
   (`kr1/data/levels`, `kr1/data/waves`). Walking the whole save directory also found the
   *snapshot copies* whenever `~/.krcheat` happened to sit inside it, which reported a reverted
   override as still installed.
7. **The oracle opens no Lua libraries.** `luaL_openlibs` is never called, so the sandbox has no
   `io`/`os` to reach for. A data chunk needs none of them, and a smaller surface is strictly
   better than a larger one.

---

## 4. Verification

```sh
python3 -m unittest discover -s tests      # 190 tests
python3 -m krcheat --self-test             # 10 checks, in the shipped tool
python3 -m krcheat doctor --oracle         # environment + a real VM load
```

What the tests actually pin down:

- **Byte identity.** `parse → render` is byte-identical over the synthetic fixture and over
  eight edge shapes (empty tables, hex and negative numbers, `%.14g` floats, escapes, the
  `multiRefObjects` preamble, commas, comments, odd whitespace). A one-value edit changes exactly
  that value's bytes and nothing else.
- **The write path.** Dry-run writes nothing and takes no snapshot; a real write changes only the
  intended bytes and leaves a hash-verified snapshot; a no-op leaves the file untouched; a
  restore returns the original.
- **Refusals.** Losing a key, losing a mandatory key, a structural mismatch, unparseable text, an
  out-of-range value, an unknown id, an absent hero, a nonexistent slot — each has a test.
- **The oracle agrees with the parser** on the same text, reports int/float distinctly, and is
  sandboxed (`os` is unreachable).
- **Snippets.** Every template encodes its result; the loop guard rejects `while`/`for`/`repeat`/
  `goto`/`::` but ignores those words inside strings and comments; every template compiles in the
  game's own LuaJIT.
- **Config and state.** Comments and unknown keys survive a rewrite; booleans and integers are
  type-checked; the cache invalidates on `version_string` change; a corrupt `state.json` is
  replaced rather than trusted.
- **D9.** No slot key exists in `config.ini` or `state.json`; `resolve_slot` asks rather than
  guesses; a nonexistent slot is exit 2 and is never created.
- **CLI end to end.** Exit codes 0/1/2/3/4 for the documented cases, `--json` shape, dry-run,
  snapshot creation, `log tail --event`, `--no-log`, `config` round trip, `data set`/`revert`.
- **No Tk process is ever started** when the static verdict says Tk would abort (§5).

Evidence kept for a real-save check, per §16.3: the `doctor` summary, the `write.*` records in
the JSONL log (target, byte counts, SHA-256 before and after, changed-node count, snapshot id),
and the game's own storage-layer log lines.

---

## 5. The Tk defect, and the crash-dialog bug it caused

`krcheat gui` was written and then found to be unrunnable on the reference machine. The
interpreter's Tk is 8.5, built against an older macOS SDK, and it **`abort()`s** when it tries to
open a window:

```
macOS 15 (1507) or later required, have instead 15 (1506) !
```

This is not a catchable exception, and macOS responds with a **"Python quit unexpectedly"
problem-report dialog**. Both `/usr/bin/python3` and the pyenv interpreter are affected.

The first implementation of the preflight made this *worse*: it probed Tk in a subprocess, which
still aborted — a child instead of the parent — and still showed the dialog, once per check. That
produced 25 crash reports and a stream of dialogs during development.

The fix, in `krcheat/gui/tkprobe.py`, is a **static verdict first**:

- the known-bad combination (macOS + Tk < 8.6) is recognised from version numbers that cost
  nothing to read, and `certain: True` means *do not start Tk anywhere*, in-process or in a
  subprocess;
- only a *plausible* Tk is ever handed to a subprocess probe;
- `krcheat gui` refuses with exit 1 and the remedy, `doctor` reports it as a WARN, and
  `KRCHEAT_ALLOW_BROKEN_TK=1` overrides the check;
- a test asserts that `doctor` spawns no Tk child when the verdict is certain, and a second
  asserts that `import krcheat.gui` does not import `tkinter` at all.

Verified: `doctor`, `gui`, `--self-test` and the full test suite now produce **zero** new crash
reports on a machine where the combination is known-bad.

### The second failure mode, and a defect it exposed

On this machine the pyenv interpreters (3.13.14, 3.14.3) have **no `_tkinter` at all** — the
exact hazard §7.4 predicted for pyenv builds — while `/usr/bin/python3` (3.9.6) has the
aborting Tk 8.5. Two interpreters, two different reasons the GUI cannot run, and the tool was
installed under the pyenv one, so this path was exercised for real.

It failed, and the failure was instructive: `krcheat gui` printed an **internal error with a
traceback and exited 6**, because `krcheat/gui/__init__.py` imported `krcheat.gui.app` — which
imports `tkinter` at module level — *before* the preflight could refuse. A missing optional
dependency was surfacing as a bug in the tool.

The fix is the ordering: `tkprobe.require()` now runs before any module that needs Tk, so the
refusal stays a sentence and exit 1 whichever way Tk is broken. Two tests guard it — one calls
the public entry point and asserts a `UsageError`, one runs the CLI in a subprocess and asserts
**no traceback** appears.

### Interpreter matrix, measured 2026-09-16

| Interpreter | Version | `tkinter` | GUI | Test suite |
| --- | --- | --- | --- | --- |
| `/usr/bin/python3` (CLT) | 3.9.6 | Tk 8.5, aborts | refused (`abort()` avoided) | 161 tests, OK |
| pyenv | 3.13.14 | absent | refused (no `_tkinter`) | 161 tests, OK (1 skipped) |
| pyenv | 3.14.3 | absent | refused (no `_tkinter`) | 161 tests, OK (1 skipped) |

3.9.6 is the documented floor and 3.14.3 is what `python3` now resolves to on this machine
(`pyenv global` is `3.14`); both are covered, and the skipped test is the one that needs a Tk
old enough to refuse statically.

---

## 6. Next steps, in the order the spec sets them

1. **Spike S2** — drop a shadow `main_globals.lua` into the save directory and see whether LÖVE
   loads it. Pass: M5 collapses to a file writer, M8 is never needed, and F15's meaning is
   confirmed. Fail: F15 moves to tier 3.
2. **Spike S1** — hand-edit a slot and confirm the game accepts it (the acceptance test for the
   codec against the real storage layer).
3. **Spike S6** — `probe` for the owner chain of `player_gold` / `lives`, with the read-back
   check the risk register demands.
4. **M3** — `krcheat/agent/kr_agent.c`: interpose `luaL_newstate` and `SDL_GL_SwapWindow`, then
   the channel loop. The protocol, the snippets and the override lifecycle are already specified
   and coded on the Python side, so M3 is native code and wiring, not design.
5. **M7** — *deferred by D10.* The GUI is written, shares `core/` and is opt-in
   (`ui.enabled`), so nothing here blocks it; macOS use is CLI-only, and a front-end would be
   picked up only if that decision changes (and only after M3, since the live panel is the part
   worth showing).

---

## 7. Review of 2026-09-16 — what it found

The review pass (static scan for unused imports and undeclared attribute accesses, the dev-mode
suite with `ResourceWarning` promoted to an error, a property test for the codec, and a
read-through of the whole tree) fixed these. Each has a regression test in
`tests/test_regressions.py`, named after the behaviour that was wrong.

| # | Finding | Why it mattered |
| --- | --- | --- |
| 1 | `Ctx.slot` passed the session's slot as *explicit* | A session slot whose file had been deleted raised exit 2 for the rest of the session, instead of falling back to the prompt. The two functions disagreed about what "session" means. |
| 2 | `profile list` resolved a profile (and so prompted for a slot) before consulting the archive | Asking "which slot?" before printing the game's own id list is noise, and in a pipe it was a usage error. `list` is a reference command. |
| 3 | `data set level` / `data revert` never ran the §15.3 gates | §10.6 says the F15 commands are subject to the same gates as a save edit, because they write into the game's read path. They did their own snapshot-and-write with nothing checked. |
| 4 | Log pruning could delete the log file the current run had open | On macOS the handle survives the unlink, so the run would keep writing into a file nobody can find — losing the diagnostics it was collected for. |
| 5 | `krcheat gui` imported the module that imports `tkinter` before the preflight | On a pyenv interpreter, where `_tkinter` is absent, a missing optional dependency surfaced as an internal error, a traceback and exit 6. See §5. |
| 6 | The oracle child was started as `-m krcheat.core.oracle` | That depends on the working directory or on the package being pip-installed. It is now a bootstrap that puts the package's parent on `sys.path`. |
| 7 | The `ps` snapshot was cached for the lifetime of the process | `krcheat gui` re-reads the game's state on a timer, so it reported whichever state was true when the window opened. The cache now has a 2 s TTL. |
| 8 | `safety.oracle_check` read `ctx._bundle` directly | A private attribute, and it bypassed the caching in `Ctx.bundle()`. There is now a public `bundle_or_none()` for the optional checks. |
| 9 | `write_path` had an unused `extra_snapshot_files` parameter | Dead, and a trap: it fed `backup.snapshot`, which refuses to run when any listed file is missing. Removed. |
| 10 | `Archive` never closed its `ZipFile`; `Result.changed` duplicated `effective_changes`; unused imports throughout | Hygiene: a 342 MB archive held open, a second name for one property, ~60 unused `typing` imports. |
| 11 | The codec had no property test | The new one generates random tables in the game's own style and asserts byte identity, value survival and stability, plus that a scalar edit rewrites exactly one line. It found two flaws in the *test* (empty tables, and table→scalar collapsing lines) before it found none in the codec. |

Two things the review deliberately did **not** change: the no-op-writes-nothing behaviour (§3.1) and
the refusal to auto-restore after a failed post-write verification (§3.4). Both are decisions, not
oversights.
