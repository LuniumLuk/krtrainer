# krtrainer

Command-line trainer and save editor for **Kingdom Rush** on **macOS**.

This repository currently holds the specification. Implementation (the `krcheat` CLI) has not
started yet — see the [roadmap](KRCHEAT_FOUNDATION.md#17-roadmap).

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

## Documents

| Document | Purpose |
| --- | --- |
| [`KRCHEAT_FOUNDATION.md`](KRCHEAT_FOUNDATION.md) | **Canonical specification.** Target profile with per-fact evidence, module architecture, persistence model, runtime state model, extension surface, feature spec, CLI contract, live-channel protocol, save codec spec, safety model, testing plan, roadmap, risk register, open questions, appendices. |
| [`MACOS_TRAINER_PROPOSAL.md`](MACOS_TRAINER_PROPOSAL.md) | The earlier, shorter proposal. Superseded by the foundation document where they disagree. |

Suggested reading order: foundation §2 (executive summary) → §3 (target profile) → §5
(persistence) → §8–§10 (features, architecture, CLI).

---

## Planned shape

```
krtrainer/
├── KRCHEAT_FOUNDATION.md       # canonical spec
├── MACOS_TRAINER_PROPOSAL.md   # earlier proposal
└── krcheat/                    # not yet implemented
    ├── cli.py                  # argparse → core → text / --json
    ├── gui/                    # tkinter → core → widgets (optional)
    ├── core/
    │   ├── profile.py          # Tier 1: save editing
    │   ├── lua_table.py        # lossless save codec (read/write/validate)
    │   ├── oracle.py           # ctypes → the game's own Lua.framework
    │   ├── mine.py             # id extraction from the archive
    │   ├── backup.py           # snapshot / restore
    │   ├── log.py              # append-only JSONL diagnostic log
    │   ├── config.py           # config.ini (user settings)
    │   ├── state.py            # state.json (machine-owned cache)
    │   ├── data.py             # F15: shadow data modules
    │   ├── paths.py
    │   ├── live/               # Tier 2: in-process Lua channel
    │   └── patch/              # Tier 3: LuaJIT bytecode patcher (backlog)
    └── agent/                  # the injected dylib
```

The CLI is the foundation: all behaviour lives in `core/`, and both `cli.py` and `gui/` are thin
renderers over the same functions. A GUI bug cannot diverge from CLI behaviour, because there is
only one implementation.

Three tiers, in delivery order:

1. **Tier 1 — save editor** (pure Python, stdlib only): upgrades, stars, gems, hero XP,
   achievements, `seen` unlocks, and persistent per-level data (starting gold/lives, wave
   rewards); with snapshots, schema validation and atomic writes. No injection, no root.
2. **Tier 2 — live channel**: gold, lives, speed and god mode via an in-process agent, reached
   by launching the game with an injected dylib (preferred) or by patching `game.love` (no
   compiler required).
3. **Tier 3 — offline bytecode patching** (backlog): permanent tweaks in a copy of the archive.
   Expected to be unnecessary — if spike S2 confirms that the save directory shadows the game
   source, tier 1 delivers the same permanent effects with a generated Lua module instead.

An optional **tkinter GUI** (`krcheat gui`) wraps the same core, and is a renderer rather than a
second implementation. The CLI remains complete on its own.

---

## Status

| Milestone | State |
| --- | --- |
| Reverse engineering + specification | done |
| Specification review (D1–D8, see [§2.1](KRCHEAT_FOUNDATION.md#21-design-decisions-review-of-2026-09-16)) | done |
| M0 — assumption spikes, **S2 first** | not started |
| M1 — Tier 1 core + write path | not started |
| M2 — Tier 1 complete + F15 | not started |
| M3–M4 — live channel | not started |
| M5–M6 — transport B, packaging | not started |
| M7 — tkinter GUI | not started |
| M8 — bytecode patcher (backlog) | not started |

Two questions are explicitly unresolved and are listed as spikes rather than assumptions:
spike **S2** (save-directory `require` precedence in LÖVE 0.10.1 — expected to pass, and it
changes the shape of M5 and M8) and **S6** (the runtime owner chain of `player_gold` / `lives`).
Both are in [open questions](KRCHEAT_FOUNDATION.md#19-open-questions).

---

## Safety

Cheating is limited to a single-player game the user owns. The project deliberately:

- **snapshots before it writes** — every mutating command copies the save file to
  `~/.krcheat/backups/` first, and aborts if the snapshot fails;
- **writes atomically** — a temp file and `os.replace()`, never a truncate in place;
- **is lossless** — untouched bytes are re-emitted verbatim, so a command that changes nothing
  produces a byte-identical file ([§12](KRCHEAT_FOUNDATION.md#12-save-codec-specification));
- **refuses to write while the game is running**, and warns before writing while Steam is;
- **never modifies the game installation** unless explicitly asked (`install` / `patch`);
- keeps no game assets or bytecode in the repository;
- tracks the Steam Cloud hazard on the save file, since this title syncs `slot_1.lua`.

The scope is deliberately file-level: there is no operation-level undo, no change journal and no
bundle rollback. What that costs is recorded in
[§15.4](KRCHEAT_FOUNDATION.md#154-what-we-deliberately-accept).
