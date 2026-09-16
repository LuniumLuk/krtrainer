# krcheat — cheatsheet

One page. `krcheat` means the installed command; `python3 -m krcheat` is identical and works
without installing. Full detail lives in [`KRCHEAT_FOUNDATION.md`](KRCHEAT_FOUNDATION.md).

```sh
python3 -m pip install --user -e .     # then: krcheat doctor
```

---

## Slots — read this once

`--slot N` is passed **per call**. It is never guessed and never remembered between runs, so a
stale slot can't silently edit the wrong profile.

| Situation | What happens |
| --- | --- |
| `--slot 2` given | used (and reused for that session) |
| omitted, in a terminal | asks which slot, listing what's on disk |
| omitted, in a pipe or script | **exit 1**, naming `--slot` (never hangs) |
| slot doesn't exist | **exit 2** — refused, never created |

Global flags go before the subcommand, but `--slot 2` works after it too.

---

## Everyday commands

| Command | What it does |
| --- | --- |
| `krcheat doctor` | can this tool work here? pass/warn/fail, one line each |
| `krcheat --slot 1 profile show` | summarise a save: gems, difficulty, upgrades, stars, heroes, achievements, counters, seen |
| `krcheat --slot 1 profile get levels.1.stars` | read one dotted path |
| `krcheat --slot 1 profile set …` | change one thing (see below) |
| `krcheat --slot 1 profile list achievements` | the known ids: `achievements heroes levels upgrades counters` |
| `krcheat backup list` | snapshots, newest first |
| `krcheat log tail --lines 40` | the last diagnostic records |

Nothing above prompts for a slot *except* the `set` commands and `list counters` — `doctor`,
`backup`, `log`, `config` and `data` are slot-independent.

---

## Recipes

```sh
kc() { krcheat --slot 1 "$@"; }                 # works in zsh and bash alike

kc profile set gems 9999                        # premium currency
kc profile set difficulty 4                     # 1 easy … 4 impossible
kc profile set upgrades all=5                   # every upgrade to 5 (the "Upgrades = 65" button)
kc profile set upgrades archers=5,mages=3
kc profile set stars all --stars 3              # every story level complete, 3 stars, all modes
kc profile set stars all --levels 1,2,3         # just those levels
kc profile set level 19 stars 3 --mode campaign
kc profile set level 19 clear --mode campaign   # writes 0, never deletes
kc profile set hero hero_magnus xp 500
kc profile set achievements all                 # 'none' to clear, or FIRST_BLOOD,SLAYER
kc profile set counters DIE_HARD 1000
kc profile set seen all                         # silence encyclopedia/tip notifications
kc profile set path levels.81.high_score 99999  # anything else: <dotted.path> <value>
```

Preview anything before it happens — `--dry-run` prints the diff and writes nothing:

```sh
krcheat --dry-run --slot 1 profile set gems 9999
```

Re-running a change you already made is a no-op: **a command that changes nothing writes nothing**,
and says so.

`profile set hero <id> skills` is refused on purpose (the valid range per skill isn't known, and the
game clamps out-of-range values to 0, so a guess would report success and do nothing).

---

## Undo

Every mutating command snapshots the file **before** it writes, and aborts if the snapshot fails —
there is nothing to remember to do first.

```sh
krcheat backup list                             # id, time, files, hashes
krcheat backup restore latest                   # byte-identical, hash-verified
krcheat backup restore 20260916-213652          # or a unique id prefix, copied from the list
krcheat backup restore latest --verify-only     # check the hashes, write nothing
krcheat backup prune --keep 20                  # snapshots are ~7 KB; nothing is auto-deleted
```

---

## F15 — persistent per-level data

Conditional on spike S2; if it hasn't passed, the generated file is inert.

```sh
krcheat data list                                        # what can be overridden, what is installed
krcheat data set level 1 starting_gold 9999              # also: starting_lives
krcheat data revert --all                                # or --level 1
```

---

## When something looks wrong

```sh
krcheat doctor                          # environment, slots, Steam Cloud, oracle, tkinter
krcheat doctor --oracle                 # + load a chunk in the game's own LuaJIT
krcheat --self-test                     # codec, snapshot, config, state — no game needed
krcheat --log-level debug --slot 1 profile set gems 9999    # verbose into the log
krcheat -v --slot 1 profile show                            # verbose to stderr as well
krcheat log tail --lines 60 --event write.                  # what the write path did
krcheat log path                                            # for tail -f
```

A successful write logs its own evidence, with hashes:

```
write.begin      bytes_before=7091 sha256_before=0e1e505b…
write.validated  sha256_intent=1a6ad683…
write.snapshot   snapshot=20260916-213652-profile-set-gems-008c
write.swapped    bytes=7091 sha256=1a6ad683…
write.verified   changed_nodes=1
```

`--json` gives the same information structurally, for scripts:

```sh
krcheat --json --slot 1 profile get gems           # → .result.value
krcheat --json --slot 1 profile show               # → .result.profile
```

---

## Safety gates

| When | Behaviour |
| --- | --- |
| The game is running | **Refused** (exit 3) — it would clobber the edit. `--force` overrides |
| Steam is running | Warning, needs `--yes` — Cloud syncs this save. Not a refusal |
| `version_string` ≠ installed game | **Refused** (exit 4) — the schema may differ. `--force` overrides |
| No slot, no way to ask | **Refused** (exit 1) |
| The snapshot can't be taken | **Aborted** (exit 5), nothing written |
| Any edit that would delete a key | **Refused** (exit 4) — the game deletes a whole slot if mandatory data is missing |

---

## Exit codes

`0` ok · `1` usage · `2` not found · `3` unavailable (or not built in this build) · `4` validation ·
`5` backup · `6` internal (a traceback is in the log)

---

## Paths and settings

| Path | What |
| --- | --- |
| `~/Library/Application Support/kingdom_rush/slot_N.lua` | the saves |
| `~/.krcheat/config.ini` | your settings, hand-editable, comments preserved |
| `~/.krcheat/state.json` | machine-owned cache (never hand-edit) |
| `~/.krcheat/backups/<id>/` | snapshots + manifest |
| `~/.krcheat/logs/krcheat-YYYYMMDD.jsonl` | the diagnostic log, rotated by day |

```sh
krcheat config list                             # every key with its effective value
krcheat config set ui.enabled true              # turn the (opt-in) GUI back on
krcheat config set logging.level debug          # default log level
krcheat --save-dir /tmp/save --game /path/to/Kingdom\ Rush.app doctor   # overrides for testing
```

---

## Not built yet

| Command | Status |
| --- | --- |
| `live gold / lives / speed / god / probe / eval / watch` | needs the injected agent (M3) → **exit 3** |
| `play`, `install`, `repair` | same (M3/M5) |
| `patch scan / apply / restore` | tier 3, backlog (M8) |
| `gui` | written, opt-in via `ui.enabled`, and unusable on this machine's Tk |

They say so rather than pretending. Tier 1 — everything above — needs no injection, no root and no
compiler.
