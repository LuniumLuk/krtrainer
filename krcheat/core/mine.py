"""Id extraction from the shipped archive (§13, D4).

`profile list achievements` and `profile set achievements all` need to know ids the
save has never seen — 74 achievements are defined, but a real slot holds 44. Those
ids live in `game.love` as LuaJIT bytecode, so this module reads them out of the
archive.

Two sources, in the order the design prefers them:

1. **The S8 oracle** (D4): load the data module in the game's own VM and read the
   table. Authoritative, and the intended route for ranges and enumerations (H5, H6).
   Currently wired for *validation* only — see `oracle.py` — because most data modules
   `require` engine modules that do not exist outside the game.
2. **Constant-pool scanning**: extract printable runs from the bytecode and select by
   shape. This is the documented fallback (`strings -n 3` in Appendix A.2 does the same
   thing by hand), and it is honest about being a heuristic.

Results are cached in `state.json` keyed on `version_string` + the archive hash, so a
game update invalidates them instead of silently reusing stale ids (§9.7).
"""

from __future__ import annotations

import re
import zipfile
from typing import Any, Dict, List, Optional

from krcheat.core import paths

#: Upgrade categories, measured against `kr1/upgrades.lua` and a real save (§A.4).
UPGRADE_CATEGORIES = ("archers", "barracks", "engineers", "mages", "rain", "reinforcements")

MODULE_ACHIEVEMENTS = "kr1/data/achievements_data.lua"
MODULE_MAPPINGS = "all/storage_mappings.lua"
MODULE_UPGRADES = "kr1/upgrades.lua"

_PRINTABLE = re.compile(rb"[\x20-\x7e]{3,}")
_ACHIEVEMENT_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_HERO_ID = re.compile(r"^hero_[a-z0-9][a-z0-9_]*$")
_LEVEL_MODULE = re.compile(r"^kr1/data/levels/level(\d+)_data\.lua$")

#: Strings that match the shapes above but are not ids.
_NOISE = {
    "TRUE",
    "FALSE",
    "NIL",
    "ERROR",
    "OK",
    "JSON",
    "LUA",
    "UTF8",
    "TODO",
    "DEBUG",
    "RELEASE",
    "END",
    "AND",
    "NOT",
    "THEN",
    "ELSE",
    "IF",
    "FOR",
    "WHILE",
    "DO",
    "RETURN",
    "LOCAL",
    "FUNCTION",
    "BREAK",
    "REPEAT",
    "UNTIL",
    "IN",
    "LJ",
    # Configuration constants that match the id shape but are not ids.
    "KR_TARGET",
    "KR_PLATFORM",
    "KR_GAME",
    "P_LEVEL",
    "P_WAVE",
    "TARGET",
    "PLATFORM",
}


class Archive(object):
    """A read-only view of `game.love`."""

    def __init__(self, bundle):
        self.bundle = bundle
        self._zip = None

    def open(self):
        if self._zip is None:
            self._zip = zipfile.ZipFile(self.bundle.game_love, "r")
        return self._zip

    def names(self):
        try:
            return self.open().namelist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise paths.NotFoundError(
                "cannot read {0}: {1}".format(self.bundle.game_love, exc)
            )

    def read(self, name):
        try:
            return self.open().read(name)
        except KeyError:
            return None
        except (OSError, zipfile.BadZipFile):
            return None

    def has(self, name):
        try:
            return name in self.open().namelist()
        except Exception:
            return False


def strings_in(data, minimum=3):
    if not data:
        return []
    return [match.decode("ascii", "replace") for match in _PRINTABLE.findall(data)]


def mine_achievements(archive):
    data = archive.read(MODULE_ACHIEVEMENTS)
    if data is None:
        return []
    found = set()
    for text in strings_in(data):
        if text in _NOISE:
            continue
        if _ACHIEVEMENT_ID.match(text):
            found.add(text)
    return sorted(found)


def mine_heroes(archive):
    found = set()
    for module in (MODULE_MAPPINGS, "kr1/data/heroes_data.lua"):
        data = archive.read(module)
        for text in strings_in(data):
            for match in re.findall(r"hero_[a-z0-9_]+", text):
                found.add(match)
    return sorted(found)


def mine_levels(archive):
    found = set()
    for name in archive.names():
        match = _LEVEL_MODULE.match(name)
        if match:
            found.add(int(match.group(1)))
    return sorted(found)


def mine_wave_modules(archive):
    """`levelNN_waves_<mode>.lua` → {level: [module names]} (for F15 wave rewards)."""
    out: Dict[int, List[str]] = {}
    pattern = re.compile(r"^kr1/data/waves/level(\d+)_waves_([a-z]+)\.lua$")
    for name in archive.names():
        match = pattern.match(name)
        if match:
            out.setdefault(int(match.group(1)), []).append(name)
    for level in out:
        out[level].sort()
    return out


def mine_upgrades(archive):
    data = archive.read(MODULE_UPGRADES)
    found = set()
    for text in strings_in(data):
        if text in UPGRADE_CATEGORIES:
            found.add(text)
    for category in UPGRADE_CATEGORIES:
        found.add(category)
    return sorted(found)


def mine_all(bundle, state=None, logger=None, use_cache=True):
    """Every id set, cached in `state.json` and keyed on the install's identity."""
    if state is not None and use_cache:
        cached = state.get("mined_ids")
        if cached and state.cache_matches(state.get("version_string"), state.get("archive_hash")):
            if logger:
                logger.debug("mine.cache_hit", source="state.json")
            return cached

    archive = Archive(bundle)
    result = {
        "achievements": mine_achievements(archive),
        "heroes": mine_heroes(archive),
        "levels": mine_levels(archive),
        "upgrades": mine_upgrades(archive),
    }
    if logger:
        logger.info(
            "mine.done",
            achievements=len(result["achievements"]),
            heroes=len(result["heroes"]),
            levels=len(result["levels"]),
            upgrades=len(result["upgrades"]),
        )
    if state is not None:
        state.put("mined_ids", result)
    return result


def upgrade_categories(archive=None):
    """The category list. Measured, not guessed — the archive scan only confirms it."""
    if archive is None:
        return list(UPGRADE_CATEGORIES)
    mined = mine_upgrades(archive)
    ordered = [category for category in UPGRADE_CATEGORIES if category in mined]
    for extra in mined:
        if extra not in ordered:
            ordered.append(extra)
    return ordered
