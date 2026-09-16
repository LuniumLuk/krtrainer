"""Tier 1 — the save editor (§9.2, §10.2).

This is the first shippable milestone: pure Python, stdlib only, no injection, no root
and no compiler. It cannot corrupt anything permanently, because every write goes
through the write path of §15.2 — snapshot, validate, atomic swap, verify.

Two pieces of knowledge live here, and only here:

* the **schema** — which top-level keys exist, which values the game clamps, which
  edits are refused outright (§12.3), and
* the **operations** — what `profile set gems 9999` actually does.

Field access is by name. Nothing is addressed by offset, so a game update changes ids
at worst, not correctness.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from krcheat.core import lua_table as lt
from krcheat.core import mine, paths, safety
from krcheat.core.errors import (
    InternalError,
    KrcheatError,
    NotFoundError,
    UsageError,
    ValidationError,
    milestone,
)

#: Upgrade categories (§A.4). Confirmed against the archive by `mine`.
UPGRADE_CATEGORIES = mine.UPGRADE_CATEGORIES

#: Highest upgrade level observed in a real save. Higher values are accepted with a
#: warning rather than refused, because the game clamps what it does not understand
#: and the user may know something we do not.
MAX_UPGRADE_OBSERVED = 5
MAX_UPGRADE_SANE = 99

#: `DIFFICULTY_EASY|NORMAL|HARD|IMPOSSIBLE` (§A.4). Whether the stored value is 1-based
#: over these is inferred and recorded as H4 — we accept 1..4 and warn outside it.
DIFFICULTY_MIN, DIFFICULTY_MAX = 1, 4
DIFFICULTY_NAMES = {1: "easy", 2: "normal", 3: "hard", 4: "impossible"}

#: `levels[n][1..3]`: campaign/heroic/iron for story levels, casual/normal/veteran for
#: endless ones (§5.4, H5).
MODE_INDEX = {"campaign": 1, "heroic": 2, "iron": 3}
MODE_NAMES = {1: "campaign/casual", 2: "heroic/normal", 3: "iron/veteran"}

#: Story level range (§5.4). Used when the archive cannot be read.
DEFAULT_STORY_LEVELS = list(range(1, 27))
#: Endless levels, which use `high_score` / `waves_survived` and a different meaning
#: for the same three sub-keys.
ENDLESS_LEVELS = (81, 82)

MAX_GEMS = 2 ** 31 - 1


def require_int(value, what):
    """Coerce a CLI value to int, or explain why it is not one."""
    if isinstance(value, bool):
        raise UsageError("{0} expects a number".format(what))
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise UsageError("{0} expects a number, got {1!r}".format(what, value))


def parse_pairs(spec, what="assignment"):
    """Parse `all=5` or `archers=5,barracks=3` into a list of (key, int) pairs."""
    if isinstance(spec, (list, tuple)):
        return [(str(key).strip(), require_int(value, what)) for key, value in spec]
    out = []
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise UsageError("{0} expects key=value (got {1!r})".format(what, chunk))
        key, _, raw = chunk.partition("=")
        key = key.strip()
        if not key:
            raise UsageError("{0} expects a name before '='".format(what))
        out.append((key, require_int(raw, what)))
    if not out:
        raise UsageError("{0} expects at least one key=value".format(what))
    return out


class Profile(object):
    """One loaded slot. Mutations accumulate; `save` performs the write path."""

    def __init__(self, doc, path, slot, save_dir=None):
        self.doc = doc
        self.path = path
        self.slot = slot
        self.save_dir = save_dir
        self.text = doc.text
        #: the structure as read, for the no-deletion check (§12.3)
        self.original_python = doc.python()
        self.warnings: List[str] = []

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, save_dir, slot, logger=None):
        path = save_dir.slot_path(slot)
        if not os.path.exists(path):
            raise NotFoundError(
                "slot {0} does not exist: {1}".format(slot, path), path=path, slot=slot
            )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise NotFoundError("cannot read {0}: {1}".format(path, exc), path=path)
        try:
            doc = lt.parse(text)
        except lt.LuaTableError as exc:
            raise ValidationError(
                "{0} does not parse as a save chunk: {1}".format(path, exc), path=path
            )
        if logger is not None:
            logger.info(
                "profile.loaded",
                path=path,
                slot=slot,
                bytes=len(text),
                top_level_keys=len(doc.keys()),
                version_string=_safe_get(doc, "version_string"),
                nodes=len(list(lt.iter_scalars(doc.root))),
            )
        profile = cls(doc, path, slot, save_dir)
        missing = [key for key in lt.EXPECTED_TOP_LEVEL if not doc.has(key)]
        if missing:
            profile.warnings.append(
                "this slot has no {0}; it may be a partially written file".format(", ".join(missing))
            )
        return profile

    # -- reading -------------------------------------------------------------

    @property
    def version_string(self):
        return _safe_get(self.doc, "version_string")

    def get(self, path):
        try:
            return self.doc.get(path)
        except lt.LuaTableError as exc:
            raise UsageError(str(exc))

    def keys(self):
        return self.doc.keys()

    def summary(self):
        """Everything `profile show` reports, as plain data."""
        python = self.doc.python()
        levels = python.get("levels") or {}
        stars = 0
        completed = []
        for level_id, entry in levels.items():
            if not isinstance(entry, dict):
                continue
            value = entry.get("stars")
            if isinstance(value, int):
                stars += value
            if any(entry.get(index) for index in MODE_INDEX.values()):
                completed.append(level_id)
        achievements = python.get("achievements") or {}
        seen = python.get("seen") or {}
        heroes = (python.get("heroes") or {}).get("status") or {}
        return {
            "slot": self.slot,
            "path": self.path,
            "version_string": python.get("version_string"),
            "gems": python.get("gems"),
            "difficulty": python.get("difficulty"),
            "upgrades": python.get("upgrades") or {},
            "levels_total": len(levels),
            "levels_completed": sorted(completed),
            "stars_total": stars,
            "heroes": {hero: (data or {}).get("xp") for hero, data in heroes.items()},
            "heroes_selected": (python.get("heroes") or {}).get("selected"),
            "achievements_unlocked": sorted(
                key for key, value in achievements.items() if value
            ),
            "achievements_total": len(achievements),
            "counters": len(python.get("achievement_counters") or {}),
            "seen_total": len(seen),
            "seen_unlocked": sum(1 for value in seen.values() if value),
            "top_level_keys": sorted(python.keys()),
        }

    # -- primitive mutations -------------------------------------------------

    def _set(self, result, path, value, create=False):
        before = self.doc.get(path) if self.doc.has(path) else None
        changed = self.doc.set(path, value, create=create)
        if changed:
            result.add_change(path, before, value)
        else:
            result.add_change(path, before, before, noop=True)
        return changed

    # -- F5 gems -------------------------------------------------------------

    def set_gems(self, result, value):
        value = require_int(value, "gems")
        if value < 0:
            raise ValidationError("gems cannot be negative (got {0})".format(value))
        if value > MAX_GEMS:
            raise ValidationError("gems must be below {0}".format(MAX_GEMS))
        self._set(result, "gems", value)
        return result

    # -- difficulty ----------------------------------------------------------

    def set_difficulty(self, result, value):
        value = require_int(value, "difficulty")
        if not DIFFICULTY_MIN <= value <= DIFFICULTY_MAX:
            raise ValidationError(
                "difficulty must be {0}-{1} per DIFFICULTY_* ({2}); got {3}".format(
                    DIFFICULTY_MIN,
                    DIFFICULTY_MAX,
                    ", ".join("{0}={1}".format(k, v) for k, v in sorted(DIFFICULTY_NAMES.items())),
                    value,
                )
            )
        self._set(result, "difficulty", value)
        return result

    # -- F3 upgrades ---------------------------------------------------------

    def set_upgrades(self, result, spec):
        pairs = parse_pairs(spec, "upgrades")
        known = self._known_upgrades()
        for key, value in pairs:
            targets = known if key == "all" else [key]
            if key != "all" and key not in known:
                raise ValidationError(
                    "unknown upgrade category {0!r} (known: {1}, or 'all')".format(
                        key, ", ".join(known)
                    )
                )
            if value < 0 or value > MAX_UPGRADE_SANE:
                raise ValidationError(
                    "upgrade level must be 0-{0}; got {1}".format(MAX_UPGRADE_SANE, value)
                )
            if value > MAX_UPGRADE_OBSERVED:
                result.warn(
                    "upgrade level {0} is above the highest level observed in a real save "
                    "({1}); the game may clamp it".format(value, MAX_UPGRADE_OBSERVED)
                )
            for category in targets:
                self._set(result, "upgrades.{0}".format(category), value, create=True)
        return result

    def _known_upgrades(self):
        """Categories in the file, in measured order, then anything extra it carries."""
        present = [category for category in UPGRADE_CATEGORIES if self.doc.has("upgrades.{0}".format(category))]
        for category in self.doc.keys_at("upgrades"):
            if isinstance(category, str) and category not in present:
                present.append(category)
        return [category for category in UPGRADE_CATEGORIES if category in present] or list(
            UPGRADE_CATEGORIES
        )

    # -- F4 stars ------------------------------------------------------------

    def level_ids(self, ctx=None):
        """Story levels: the archive's list first, the documented range second."""
        if ctx is not None:
            try:
                ids = ctx.state.get("mined_ids", {}).get("levels") or []
                if ids:
                    return sorted(int(value) for value in ids)
            except Exception:
                pass
        return list(DEFAULT_STORY_LEVELS)

    def set_stars_all(self, result, ctx=None, stars=3, levels=None, include_endless=False):
        stars = require_int(stars, "stars")
        if stars < 0 or stars > 3:
            raise ValidationError("stars per level are 0-3 (got {0})".format(stars))
        targets = (
            [require_int(level, "level") for level in levels]
            if levels
            else self.level_ids(ctx)
        )
        for level in targets:
            self._complete_level(result, level, stars)
        if include_endless:
            for level in ENDLESS_LEVELS:
                if self.doc.has("levels.{0}".format(level)):
                    self._complete_level(result, level, stars)
        result.note(
            "{0} level(s) marked complete with {1} star(s) in all three modes".format(
                len(targets), stars
            )
        )
        return result

    def _complete_level(self, result, level, stars, mode=None):
        cases = [MODE_INDEX[mode]] if mode else sorted(MODE_INDEX.values())
        for index in cases:
            self._set(result, "levels.{0}.{1}".format(level, index), 1, create=True)
        if stars is not None:
            self._set(result, "levels.{0}.stars".format(level), stars, create=True)

    def set_level_stars(self, result, level, stars, mode=None):
        level = require_int(level, "level")
        stars = None if stars is None else require_int(stars, "stars")
        if stars is not None and not 0 <= stars <= 3:
            raise ValidationError("stars per level are 0-3 (got {0})".format(stars))
        if mode is not None and mode not in MODE_INDEX:
            raise UsageError(
                "--mode must be one of {0}".format(", ".join(sorted(MODE_INDEX)))
            )
        self._complete_level(result, level, stars, mode)
        return result

    def set_level_clear(self, result, level, mode=None):
        """Clear completion flags.

        The flag is set to 0 rather than removed: deleting keys is never allowed
        (§12.3), because the game re-validates what it reads and deletes the whole slot
        when mandatory data is missing.
        """
        level = require_int(level, "level")
        if mode is None:
            raise UsageError("'clear' needs --mode campaign|heroic|iron")
        if mode not in MODE_INDEX:
            raise UsageError("--mode must be one of {0}".format(", ".join(sorted(MODE_INDEX))))
        self._set(result, "levels.{0}.{1}".format(level, MODE_INDEX[mode]), 0, create=True)
        return result

    # -- F6 hero XP ----------------------------------------------------------

    def _known_heroes(self, ctx=None):
        found = list(self.doc.keys_at("heroes.status"))
        if ctx is not None:
            try:
                for hero in ctx.state.get("mined_ids", {}).get("heroes") or []:
                    if hero not in found:
                        found.append(hero)
            except Exception:
                pass
        return [hero for hero in found if isinstance(hero, str)]

    def set_hero_xp(self, result, hero, value):
        value = require_int(value, "xp")
        if value < 0:
            raise ValidationError("hero xp cannot be negative (got {0})".format(value))
        present = self.doc.keys_at("heroes.status")
        if hero not in present:
            raise ValidationError(
                "hero {0!r} is not in this save (present: {1}). The game adds missing heroes "
                "from its template, but writing xp for one it has never seen has no "
                "meaning.".format(hero, ", ".join(str(h) for h in present) or "none")
            )
        self._set(result, "heroes.status.{0}.xp".format(hero), value)
        return result

    def set_hero_skills(self, result, hero, spec):
        """Refused on purpose until the ranges are known (H6).

        `all/storage.lua` clamps out-of-range hero skills to 0 rather than rejecting the
        file, so an invented value silently becomes zero. Guessing here would produce a
        cheerful "success" message and no change in the game, which is worse than
        refusing.
        """
        raise milestone(
            "H6",
            "hero skill editing is deliberately unimplemented: the valid range per skill is "
            "not yet known, and the game clamps out-of-range values to 0. The intended route "
            "is spike S8 (krcheat doctor --oracle), then this command.",
        )

    # -- F7 achievements -----------------------------------------------------

    def known_achievements(self, ctx=None):
        found = list(self.doc.keys_at("achievements"))
        if ctx is not None:
            try:
                for achievement in ctx.state.get("mined_ids", {}).get("achievements") or []:
                    if achievement not in found:
                        found.append(achievement)
            except Exception:
                pass
        return [item for item in found if isinstance(item, str)]

    def set_achievements(self, result, spec, ctx=None):
        known = self.known_achievements(ctx)
        token = str(spec).strip()
        if token == "all":
            targets = known or self.doc.keys_at("achievements")
            if not targets:
                raise ValidationError(
                    "no achievement ids are known: this save has none and the archive could not "
                    "be read"
                )
            for achievement in targets:
                self._set(result, "achievements.{0}".format(achievement), True, create=True)
            result.note("unlocked {0} achievement(s)".format(len(targets)))
            return result
        if token == "none":
            targets = self.doc.keys_at("achievements")
            for achievement in targets:
                self._set(result, "achievements.{0}".format(achievement), False)
            result.note("cleared {0} achievement flag(s)".format(len(targets)))
            return result
        targets = [item.strip() for item in token.split(",") if item.strip()]
        if not targets:
            raise UsageError("achievements expects 'all', 'none', or a comma-separated id list")
        unknown = [item for item in targets if item not in known and item not in self.doc.keys_at("achievements")]
        if unknown:
            raise ValidationError(
                "unknown achievement id(s): {0}. Use 'krcheat profile list achievements' to see "
                "the {1} known ids.".format(", ".join(unknown[:8]), len(known))
            )
        for achievement in targets:
            self._set(result, "achievements.{0}".format(achievement), True, create=True)
        result.note("unlocked {0} achievement(s)".format(len(targets)))
        return result

    def set_counters(self, result, achievement, value):
        value = require_int(value, "counter")
        if value < 0:
            raise ValidationError("achievement counters cannot be negative (got {0})".format(value))
        if not self.doc.has("achievement_counters"):
            raise ValidationError("this save has no achievement_counters table")
        present = self.doc.keys_at("achievement_counters")
        if achievement not in present:
            raise ValidationError(
                "unknown achievement counter {0!r} (present: {1})".format(
                    achievement, ", ".join(str(item) for item in present[:8])
                )
            )
        self._set(result, "achievement_counters.{0}".format(achievement), value)
        return result

    # -- F8 seen -------------------------------------------------------------

    def set_seen_all(self, result, ctx=None):
        keys = self.doc.keys_at("seen")
        if not keys:
            raise ValidationError("this save has no 'seen' table")
        changed = 0
        for key in keys:
            if self._set(result, "seen.{0}".format(key), True):
                changed += 1
        result.note("marked {0} of {1} 'seen' entries true".format(changed, len(keys)))
        return result

    # -- writing -------------------------------------------------------------

    def save(self, ctx, result, label="profile-set"):
        """The write path of §15.2 for this slot."""
        if not self.doc.dirty:
            rendered = self.doc.render()
            if rendered != self.text:
                raise InternalError(
                    "codec invariant violated: render() differs from the source although no "
                    "node is dirty",
                    path=self.path,
                )
            result.note("no changes: the file was not rewritten (byte-identity check passed)")
            ctx.log_info(
                "profile.no_changes",
                path=self.path,
                slot=self.slot,
                bytes=len(self.text),
                sha256=paths.sha256_file(self.path),
            )
            return result

        text = self.doc.render()
        intended = self.doc.python()
        ctx.log_debug(
            "profile.render",
            path=self.path,
            bytes_before=len(self.text),
            bytes_after=len(text),
            changed_nodes=len(result.effective_changes),
        )

        try:
            result.warnings.extend(
                safety.check_gates(
                    ctx,
                    bundle=_maybe_bundle(ctx),
                    save_dir=self.save_dir,
                    version_string=self.version_string,
                    target_path=self.path,
                )
            )
        except KrcheatError as exc:
            if not ctx.dry_run:
                raise
            result.note("a real run would refuse here: {0}".format(exc.message))

        safety.write_path(
            ctx,
            result,
            target=self.path,
            text=text,
            original_text=self.text,
            intended=intended,
            original=self.original_python,
            label=label,
            version_string=self.version_string,
        )
        return result


def _maybe_bundle(ctx):
    try:
        return ctx.bundle()
    except KrcheatError:
        return None


def _safe_get(doc, key):
    try:
        return doc.get(key)
    except lt.LuaTableError:
        return None


# ---------------------------------------------------------------------------
# `profile list`
# ---------------------------------------------------------------------------


def list_ids(ctx, kind, profile=None, bundle=None):
    """Enumerate known ids, preferring the archive and falling back to the save.

    The save is a legitimate source for ids the game has already written; the archive
    is the source for ids it has not (74 achievements are defined, 44 are in a real
    slot).
    """
    kind = str(kind).strip().lower()
    from_save = {}
    if profile is not None:
        from_save = {
            "achievements": profile.doc.keys_at("achievements"),
            "heroes": profile.doc.keys_at("heroes.status"),
            "levels": profile.doc.keys_at("levels"),
            "upgrades": profile.doc.keys_at("upgrades"),
            "counters": profile.doc.keys_at("achievement_counters"),
        }
    if kind not in ("achievements", "heroes", "levels", "upgrades", "counters"):
        raise UsageError(
            "unknown list kind {0!r}; expected achievements, heroes, levels, upgrades or "
            "counters".format(kind)
        )
    if bundle is None:
        try:
            bundle = ctx.bundle()
        except KrcheatError:
            bundle = None
    mined = {}
    if bundle is not None and kind != "counters":
        mined = mine.mine_all(bundle, state=ctx.state, logger=ctx.log)
    values = list(mined.get(kind, []))
    if kind == "heroes":
        # The save's own hero list is authoritative (the game re-adds missing heroes from
        # its template, §5.1); the archive scan is a superset that includes heroes from
        # other Ironhide titles sharing `storage_mappings.lua` (H7). Ordering the save's
        # list first puts the editable ones where the user is looking.
        values = [item for item in from_save.get(kind, []) if item not in mined.get(kind, [])]
        values += [item for item in mined.get(kind, []) if item not in values]
    else:
        for value in from_save.get(kind, []):
            if value not in values:
                values.append(value)
    if kind == "levels":
        values = sorted(int(value) for value in values)
    elif kind == "upgrades":
        values = [category for category in UPGRADE_CATEGORIES if category in values]
    else:
        values = sorted(str(value) for value in values)
    return values
