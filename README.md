# krtrainer

Command-line trainer and save editor for **Kingdom Rush** on **macOS**.

The CLI is called `krcheat`. Tier 1 — the save editor — is implemented and needs no injection, no
root and no compiler. Tier 2 — the live channel — is implemented too, and needs a compiler **once**
(about two seconds, on the first live command) and a game started by `krcheat play`, because the
agent is a dylib and a dylib has to be in the environment at launch. Tier 3 is a backlog item, and
says so rather than pretending.

Every command that is not implemented refuses with **exit 3** and a sentence naming what is
missing — never silently, never with a traceback.

```sh
krcheat doctor                     # can this tool work here?
krcheat --slot 1 profile set gems 9999
krcheat play                       # the game, with the live channel attached
krcheat live gold infinity         # 99999, re-applied every frame
krcheat live gold off              # released, and the captured value restored
```

---

## Why this exists

The Windows trainer (in the separate `KingdomRushTrainer` repository) is a .NET WinForms app
that resolves `mono.dll+0x1F2680,60,74,108`-style pointer chains and blind-writes integers.
That approach cannot be ported, because **the macOS release is a completely different engine**:

| | Windows | macOS |
| --- | --- | --- |
| Engine | Unity | **LÖVE (Love2D) 0.10.1** |
| Runtime | Mono / .NET IL | **LuaJIT 2.1** (Lua 5.1) |
| Game logic | compiled assemblies | **555 Lua modules**, shipped as bytecode inside `game.love` |
| `mono.dll` | present | **does not exist** |

Two properties of the macOS build make a *better* trainer possible:

1. **The saves are plain-text Lua tables** — `~/Library/Application Support/kingdom_rush/slot_N.lua`.
   Persistent progression (upgrades, stars, gems, hero XP, achievements) can be edited with
   pure Python, no injection, no root and no compiler.
2. **The bundle exports the full Lua C API** and is signed with permissive entitlements, so an
   in-process agent can evaluate Lua inside the running game. Live values (gold, lives, speed)
   are then addressed **by field name, not by address** — which means the tool does not break
   when the game updates.

---

## Running it

The language floor is **Python 3.9** and there are **no Python dependencies**: the standard
library only, for every tier and both front-ends. Verified on **3.9.6**, **3.13.14** and
**3.14.3** — the interpreters present on the reference machine.

Tier 2 additionally compiles a small C dylib with the system `clang`, on first use, into
`~/.krcheat/agent/`. That is the only external tool either tier needs, and it is needed once:
the built dylib is named after a hash of its sources, so a new build is picked up automatically and
a stale one is impossible.

```sh
cd /path/to/krtrainer
python3 -m krcheat doctor              # can this tool do its job here?
python3 -m krcheat --slot 1 profile show
python3 -m krcheat --slot 1 profile set gems 9999

python3 -m krcheat play                # tier 2: the game, with the live channel attached
python3 -m krcheat live status
python3 -m krcheat live gold infinity --keep
```

Or install the console script:

```sh
python3 -m pip install --user -e .
krcheat doctor
```

`pipx install .` works too, if `pipx` is present.

Two things worth knowing about the console script:

- `pip install --user` writes it to `~/.local/bin`, which is **not** on the default macOS
  `PATH`. If `krcheat: command not found`, either add that directory to `PATH`
  (`export PATH="$HOME/.local/bin:$PATH"`) or keep using `python3 -m krcheat`.
- The script is bound to the interpreter that installed it (its shebang). Switching pyenv
  versions later breaks it with a "bad interpreter" error; re-run the install, or use
  `python3 -m krcheat`, which always uses the current interpreter.

### The commands you will use first

| Command | What it does |
| --- | --- |
| `krcheat doctor` | Every environmental check, pass/warn/fail. `--oracle` also loads a chunk in the game's own LuaJIT. |
| `krcheat --slot N profile show` | Summarise a slot: gems, difficulty, upgrades, stars, hero XP, achievements, counters, `seen`. |
| `krcheat --slot N profile get <path>` | Read one dotted path, e.g. `levels.1.stars`. |
| `krcheat --slot N profile set …` | The mutating surface: `gems`, `difficulty`, `upgrades`, `stars`, `level`, `hero … xp`, `achievements`, `counters`, `seen`, and `path <dotted.path> <value>` as a general escape hatch. |
| `krcheat --slot N profile list <kind>` | Known ids: `achievements`, `heroes`, `levels`, `upgrades`, `counters`. |
| `krcheat backup list` / `restore <id>` / `prune --keep N` | Snapshots. Taken automatically before every write. |
| `krcheat data list` / `data set level …` / `data revert` | F15: persistent per-level data as a shadow module. Requires spike S2; see below. |
| `krcheat log tail` / `log path` / `log prune` | The JSONL diagnostic log. |
| `krcheat config list` / `get` / `set` / `path` | `~/.krcheat/config.ini`, hand-editable, comments preserved. |
| `krcheat --self-test` | Exercises the codec, snapshot, config, state and snippet paths without touching the game. |

Global options go **before** the subcommand — `krcheat --dry-run profile set gems 9999` — and
are also accepted afterwards, because there is no reason to make you retype.

### Slots are explicit, never guessed

`--slot N` is passed per call. Within one trainer session the last explicit value is reused
(`krcheat gui`, `krcheat live watch`); the cache is in memory only, so a new invocation either
receives `--slot` or **asks**. It never falls back to the highest slot number or the newest
mtime, because with several profiles in play a wrong guess edits the wrong save. Nothing about
slot selection is written to `config.ini` or `state.json` (decision D9).

Non-interactively, a missing `--slot` is a usage error (exit 1) rather than a prompt that would
hang a script.

### Diagnosing a run

Every run appends JSONL records to `~/.krcheat/logs/krcheat-YYYYMMDD.jsonl` — one object per
line, flushed per record, so a crash still leaves a usable tail.

```sh
krcheat --log-level debug --slot 1 profile set gems 9999   # verbose into the log
krcheat -v --slot 1 profile show                           # verbose onto stderr as well
krcheat log tail --lines 60 --event write.                 # what the write path did
krcheat log tail --json                                    # the same records as JSON
krcheat log path                                           # for `tail -f` or `open -R`
```

The log is **diagnostic only**: it is never read back as state, and deleting it is always safe.
It records values, not contents — hashes, paths, sizes and counts by default, field values at
debug level, truncated. Rotation is by day with a total size cap, pruned at startup so it cannot
grow without bound.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | usage error (including "no slot given, cannot prompt") |
| 2 | game/app/save/slot not found |
| 3 | channel unavailable (no game running, or the game was not started by `krcheat play`) — and a documented-but-unbuilt milestone |
| 4 | validation failure (schema, mandatory keys, out-of-range value) |
| 5 | backup or restore failure |
| 6 | internal error (always accompanied by a traceback in the log) |

---

## Safety

Cheating is limited to a single-player game the user owns. Every mutating command follows the
same five steps, in this order, with no bypass:

1. **Snapshot before the first write** to `~/.krcheat/backups/<id>/` with a manifest recording
   the source path, SHA-256, game version and the invoking command. **If the snapshot fails, the
   command aborts** (exit 5) and nothing is written.
2. **Validate before swap** — the text is re-parsed, compared against the intended structure,
   checked for lost keys, and (when the game is installed) loaded in the game's own LuaJIT via
   the S8 oracle.
3. **Atomic swap** — write `slot_N.lua.tmp`, `os.replace()` over the original. The original is
   never truncated in place.
4. **Verify after swap** — re-read from disk, re-compare, report a diff summary.
5. **Restore is byte-identical**, verified by hash against the manifest.

Beyond that:

- **Lossless by construction.** Untouched bytes are re-emitted verbatim, so **a command that
  changes nothing produces a byte-identical file** — and in fact does not write at all. That is
  the primary unit test of the codec.
- **Refuses to write while the game is running** (exit 3; `--force` overrides). Steam running is
  a prominent warning gated behind `--yes`, not a refusal.
- **`version_string` mismatch fails closed** (exit 4; `--force` overrides).
- **Never deletes keys.** The game deletes a whole slot whose mandatory data is missing, so the
  write path refuses any edit that removes one. `level <n> clear` writes `0`, it does not delete.
- **Deliberately file-level** (D6): no operation-level undo, no change journal, no bundle
  rollback. What that costs is recorded in the spec.
- Keeps no game assets or bytecode in the repository; the oracle extracts nothing and commits
  nothing.

---

## The GUI is opt-in, and unavailable on this machine

**macOS use is CLI-only (decision D10).** `krcheat gui` is therefore **off by default**
(`ui.enabled = false`), and `doctor` reports it as "not requested" rather than warning about a
component you have decided not to use. Turn it on with:

```sh
krcheat config set ui.enabled true
```

When it is on, it is a tkinter front-end over the same `core/`, with the same safety model and no
GUI-only operation. On this machine it still **cannot run**, for two different reasons depending on
the interpreter — and both are reported rather than crashing:

| Interpreter | What it has | What happens |
| --- | --- | --- |
| `/usr/bin/python3` (3.9.6) | Tk **8.5**, built against an older SDK | `abort()` during window creation: `macOS 15 (1507) or later required, have instead 15 (1506) !` |
| pyenv 3.13.14 / 3.14.3 | **no `_tkinter` at all** | `ModuleNotFoundError: No module named '_tkinter'` |

The first failure is an `abort()` inside Tk's C code — not a catchable Python exception — and
macOS answers it with a **"Python quit unexpectedly" problem-report dialog**. Neither failure is
allowed to reach the user as a crash:

- `krcheat gui` refuses with exit 1 and an explanation, instead of aborting your run;
- `doctor` reports the condition as a WARN, with the remedy;
- the Tk preflight runs **before** any module that imports `tkinter`, so a missing `_tkinter` is
a sentence rather than a traceback;
- `krcheat gui` and `doctor` **never start a Tk process** unless the version numbers say it is
plausible — probing an aborting Tk in a subprocess still pops the dialog, once per check;
- `KRCHEAT_ALLOW_BROKEN_TK=1` overrides the check, for the unlikely case of a working Tk 8.5.

To get a working GUI, install an interpreter built against Tk 8.6+ (a python.org installer, or
`brew install python-tk`). The CLI needs none of this.

---

## Documents

| Document | Purpose |
| --- | --- |
| [`CHEATSHEET.md`](CHEATSHEET.md) | **Start here if you just want to use it.** One page: slots, recipes, undo, the safety gates, exit codes. |
| [`KRCHEAT_FOUNDATION.md`](KRCHEAT_FOUNDATION.md) | **Canonical specification.** Target profile with per-fact evidence, module architecture, persistence model, runtime state model, extension surface, feature spec, CLI contract, live-channel protocol, save codec spec, safety model, testing plan, roadmap, risk register, open questions, appendices. |
| [`MACOS_TRAINER_PROPOSAL.md`](MACOS_TRAINER_PROPOSAL.md) | The earlier, shorter proposal. Superseded by the foundation document where they disagree. |
| [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | How the implementation maps onto the spec: module by module, what is built, what is not, and where the code deliberately goes further. |

Suggested reading order: the cheatsheet to use it, then foundation §2 (executive summary) → §3
(target profile) → §5 (persistence) → §8–§10 (features, architecture, CLI).

---

## Shape

```
krtrainer/
├── KRCHEAT_FOUNDATION.md       # canonical spec
├── CHEATSHEET.md               # one-page command reference
├── MACOS_TRAINER_PROPOSAL.md   # earlier proposal
├── IMPLEMENTATION.md           # implementation notes and deviations
├── pyproject.toml
├── krcheat/
│   ├── cli.py                  # argparse → core → text / --json
│   ├── gui/                    # tkinter renderer over core (optional)
│   │   ├── app.py, worker.py, dialogs.py
│   │   └── tkprobe.py          # refuses to start Tk when Tk would abort
│   ├── core/
│   │   ├── lua_table.py        # lossless codec (read/write/validate) — D1
│   │   ├── profile.py          # tier 1 operations + schema
│   │   ├── safety.py           # §15.3 gates and the §15.2 write path
│   │   ├── backup.py           # snapshot / restore / manifest / prune
│   │   ├── oracle.py           # ctypes → the game's own Lua.framework — D4/S8
│   │   ├── mine.py             # id extraction from game.love
│   │   ├── data.py             # F15 shadow data modules
│   │   ├── doctor.py           # environment checks
│   │   ├── selftest.py         # the regression guard
│   │   ├── log.py, config.py, state.py, paths.py, context.py, errors.py, result.py
│   │   ├── live/               # tier 2: protocol, snippets, transports, keeper
│   │   └── patch/              # tier 3: backlog (M8)
│   └── agent/                  # the injected dylib, its harness and its Makefile
└── tests/                      # 244 tests, stdlib unittest
```

The CLI is the foundation: all behaviour lives in `core/`, and both `cli.py` and `gui/` are thin
renderers over the same functions. A GUI bug cannot diverge from CLI behaviour, because there is
only one implementation.

---

## Tests

```sh
python3 -m unittest discover -s tests -t tests -v     # 244 tests
python3 -m krcheat --self-test                        # the same checks, in the shipped tool
```

Layers, per the spec: **unit** (codec byte-identity over synthetic fixtures and over generated
text, targeted edits, backup hash verification, config preservation, state invalidation, operation
ranges, the no-deletion guard), **oracle** (every fixture loaded in the game's own LuaJIT —
skipped, not failed, when the game is not installed), **golden** (a checked-in *synthetic* save,
never a real one), **CLI end-to-end** (dispatch, exit codes, dry-run, snapshots), **live
integration** (`test_agent_integration.py`: compiles the agent with the production builder, injects
it into a separate process, and drives the real channel against the game's own LuaJIT), and
**regression** (`test_regressions.py`, `test_live_bootstrap.py`: one test per bug found in a review
or while building — both lists are in [`IMPLEMENTATION.md`](IMPLEMENTATION.md)).

---

## Status

| Milestone | State |
| --- | --- |
| Reverse engineering + specification | done |
| Specification review (D1–D9, [§2.1](KRCHEAT_FOUNDATION.md#21-design-decisions-review-of-2026-09-16)) | done |
| Design review after M3–M5 (D11–D17, [§2.2](KRCHEAT_FOUNDATION.md#22-design-decisions-review-of-2026-09-17-after-building-m3m5)) | done |
| M0 — spikes | **S8 implemented**, and S3–S5 answered for the agent's mechanism by a harness; S1, S2, S6, S7 need the game running |
| M1 — Tier 1 core + write path | **done** |
| M2 — Tier 1 complete (F3–F8, `list`) + F15 generation | **done** |
| M3 — the injected agent (channel, `probe`, `eval`, `status`) | **done** |
| M4 — live commands and the override lifecycle (§11.7) | **done**, including `--keep` and the keeper (D11) |
| M5 — Transport B (bootstrap module) | **mechanism done**; `install --check` lets the game answer S2, and `start()` refuses rather than half-working |
| M6 — Packaging and docs | partial (`pyproject.toml`, this README, [`CHEATSHEET.md`](CHEATSHEET.md)) |
| M7 — tkinter GUI (F16) | **written and opt-in** (`ui.enabled`), deferred by D10; unusable on this machine's Tk 8.5 |
| M8 — Bytecode patcher | backlog, as designed |

Two questions still need the game running, and are listed as spikes rather than assumptions:
**S2** (save-directory `require` precedence in LÖVE 0.10.1 — it decides the shape of M5 and
whether M8 is ever needed) and **S6** (the runtime owner chain of `player_gold` / `lives`, plus the
sentinels `god` and `speed` use). Nothing *mechanical* is unverified: the agent, the channel, the
override lifecycle and transport B's install path are all covered by tests that run without the
game. Both open questions are in
[open questions](KRCHEAT_FOUNDATION.md#19-open-questions).

`krcheat data set level …` generates its shadow module today, but its effect is **unverified
until S2 passes** — the command says so on every run, and so does `krcheat install --check`.
