"""Locating things: the app bundle, the save directory, slots, processes, our own home.

Also the home of **slot resolution** (§10.7, D9), which is deliberately *not* a
guess: a slot is supplied per call, reused only inside one trainer session, and
otherwise asked for. Reading the game's own `active_slot_idx` is a possible future
convenience (H10) but is not on the critical path, so nothing here depends on game
state.

`resolve_slot` takes an `ask` callable rather than prompting itself: `core/` never
prints, and the front-end owns the conversation.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from typing import Callable, Dict, List, Optional

from krcheat.core.errors import NotFoundError, UsageError

APP_NAME = "Kingdom Rush.app"
BUNDLE_ID = "com.ironhidegames.kingdomrush.mac.steam"
SAVE_DIRNAME = "kingdom_rush"
STEAM_APPID = "246420"
LAUNCHER_REL = os.path.join("Contents", "MacOS", "love")
GAME_LOVE_REL = os.path.join("Contents", "Resources", "game.love")
LUA_FW_REL = os.path.join("Contents", "Frameworks", "Lua.framework", "Versions", "A", "Lua")

STEAM_BASE = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Steam"
)
DEFAULT_APP = os.path.join(STEAM_BASE, "steamapps", "common", "Kingdom Rush", APP_NAME)
DEFAULT_SAVE_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", SAVE_DIRNAME)

#: Slot file naming, from `SLOT_FILE_FMT = "slot_%d.lua"` (§5.1).
SLOT_RE = re.compile(r"^slot_(\d+)\.lua$")


# ---------------------------------------------------------------------------
# Our own directories
# ---------------------------------------------------------------------------


def home():
    """`~/.krcheat` — configuration, cache, logs and snapshots live here (D8)."""
    override = os.environ.get("KRCHEAT_HOME")
    return override or os.path.join(os.path.expanduser("~"), ".krcheat")


def backups_dir():
    return os.path.join(home(), "backups")


def logs_dir():
    return os.path.join(home(), "logs")


def config_path():
    return os.path.join(home(), "config.ini")


def state_path():
    return os.path.join(home(), "state.json")


def ensure_home():
    """Create the directories we own. Returns a list of what had to be created."""
    created = []
    for path in (home(), backups_dir(), logs_dir()):
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            created.append(path)
    return created


# ---------------------------------------------------------------------------
# The app bundle
# ---------------------------------------------------------------------------


class AppBundle(object):
    """The installed game. Constructed by `find_app_bundle`."""

    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return "AppBundle({0})".format(self.path)

    @property
    def launcher(self):
        return os.path.join(self.path, LAUNCHER_REL)

    @property
    def game_love(self):
        return os.path.join(self.path, GAME_LOVE_REL)

    @property
    def lua_framework(self):
        return os.path.join(self.path, LUA_FW_REL)

    @property
    def info_plist(self):
        return os.path.join(self.path, "Contents", "Info.plist")

    def exists(self):
        return os.path.exists(self.game_love)

    def short_version(self):
        """`CFBundleShortVersionString` from Info.plist, e.g. `6.4.46`."""
        try:
            import plistlib

            with open(self.info_plist, "rb") as handle:
                data = plistlib.load(handle)
            return data.get("CFBundleShortVersionString")
        except Exception:
            return None

    def bundle_id(self):
        try:
            import plistlib

            with open(self.info_plist, "rb") as handle:
                data = plistlib.load(handle)
            return data.get("CFBundleIdentifier")
        except Exception:
            return None

    def expected_version_string(self):
        """The `version_string` a save for this install should carry.

        Derived (`kr1-desktop-<short version>`) rather than measured, so it is used
        only as a mismatch *signal*; when the plist is unreadable the check is
        skipped rather than guessed (§12.3).
        """
        short = self.short_version()
        if not short:
            return None
        return "kr1-desktop-{0}".format(short)

    def lua_framework_available(self):
        return os.path.exists(self.lua_framework)


def steam_library_candidates():
    """Every plausible `steamapps/common` root, from the Steam library index."""
    roots = [os.path.join(STEAM_BASE, "steamapps", "common")]
    index = os.path.join(STEAM_BASE, "steamapps", "libraryfolders.vdf")
    try:
        with open(index, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            roots.append(os.path.join(match.group(1), "steamapps", "common"))
    except OSError:
        pass
    seen = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


def find_app_bundle(override=None):
    """Locate the game. `override` is the `--game` flag (highest precedence)."""
    if override:
        candidate = os.path.abspath(os.path.expanduser(override))
        if os.path.basename(candidate) != APP_NAME and os.path.isdir(
            os.path.join(candidate, APP_NAME)
        ):
            candidate = os.path.join(candidate, APP_NAME)
        if not os.path.exists(candidate):
            raise NotFoundError(
                "app bundle not found at the --game path: {0}".format(candidate),
                path=candidate,
            )
        return AppBundle(candidate)

    if os.path.exists(DEFAULT_APP):
        return AppBundle(DEFAULT_APP)

    for root in steam_library_candidates():
        candidate = os.path.join(root, "Kingdom Rush", APP_NAME)
        if os.path.exists(candidate):
            return AppBundle(candidate)

    raise NotFoundError(
        "Kingdom Rush.app was not found. Searched {0} and the Steam library index. "
        "Pass --game PATH to point at it.".format(DEFAULT_APP),
        searched=DEFAULT_APP,
    )


# ---------------------------------------------------------------------------
# Save directory and slots
# ---------------------------------------------------------------------------


class SaveDir(object):
    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return "SaveDir({0})".format(self.path)

    def exists(self):
        return os.path.isdir(self.path)

    def writable(self):
        return os.path.isdir(self.path) and os.access(self.path, os.W_OK)

    def slot_path(self, number):
        return os.path.join(self.path, "slot_{0}.lua".format(int(number)))

    def has_slot(self, number):
        return os.path.exists(self.slot_path(number))

    def find_slots(self):
        """Every `slot_N.lua` on disk, ascending. Never invents a slot (H7)."""
        found = []
        try:
            names = os.listdir(self.path)
        except OSError:
            return found
        for name in names:
            match = SLOT_RE.match(name)
            if match:
                found.append(int(match.group(1)))
        return sorted(found)

    def slot_inventory(self):
        """Slot numbers with size and mtime — what `state.json` caches (§9.7)."""
        out = []
        for number in self.find_slots():
            path = self.slot_path(number)
            try:
                stat = os.stat(path)
                out.append(
                    {
                        "slot": number,
                        "path": path,
                        "size": stat.st_size,
                        "mtime": int(stat.st_mtime),
                    }
                )
            except OSError:
                continue
        return out

    def settings_path(self):
        return os.path.join(self.path, "settings.lua")

    def global_path(self):
        return os.path.join(self.path, "global.lua")

    def cloud_marker(self):
        return os.path.join(self.path, "steam_autocloud.vdf")


def find_save_dir(override=None):
    """Locate the save directory. `override` is `--save-dir`."""
    path = override or DEFAULT_SAVE_DIR
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise NotFoundError(
            "save directory not found: {0}. Launch the game once, or pass --save-dir.".format(path),
            path=path,
        )
    return SaveDir(path)


def resolve_slot(save_dir, explicit=None, session=None, ask=None, interactive=True):
    """Decide which slot a profile command operates on (§10.7, D9).

    Order: explicit `--slot` > the session's slot > ask > usage error. There is no
    guess — not the highest number, not the newest mtime — because with several
    profiles in play a wrong guess edits the wrong save.
    """
    available = save_dir.find_slots()
    if explicit is not None:
        number = int(explicit)
        if not save_dir.has_slot(number):
            raise NotFoundError(
                "slot {0} does not exist in {1} (found: {2}). Refusing to create it.".format(
                    number,
                    save_dir.path,
                    ", ".join(str(s) for s in available) or "none",
                ),
                slot=number,
                available=available,
            )
        return number
    if session is not None:
        number = int(session)
        if save_dir.has_slot(number):
            return number
        # A session value that no longer exists on disk is treated as absent rather
        # than as an error: the user may have deleted the slot between commands.
    if not available:
        raise NotFoundError(
            "no slot files found in {0}. Create a profile in the game first.".format(save_dir.path),
            available=[],
        )
    if not interactive or ask is None:
        raise UsageError(
            "no slot selected and this run cannot prompt. Pass --slot N "
            "(available: {0}).".format(", ".join(str(s) for s in available)),
            available=available,
        )
    if len(available) == 1:
        # Still asked, not assumed: one slot on disk is a listing, not a preference.
        pass
    return int(ask(available))


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

_PS_CACHE = {}
#: How long a `ps` snapshot is trusted. Long-lived front-ends (`krcheat gui`) re-check the
#: game's state on a timer, and a cache without an expiry would report the first answer
#: for the rest of the session.
_PS_TTL_SECONDS = 2.0


def _ps_output(max_age=_PS_TTL_SECONDS):
    now = time.time()
    cached = _PS_CACHE.get("out")
    if cached is not None and now - _PS_CACHE.get("at", 0.0) < max_age:
        return cached
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        output = completed.stdout.decode("utf-8", "replace")
    except Exception:
        output = ""
    _PS_CACHE["out"] = output
    _PS_CACHE["at"] = time.time()
    return output


def _ps_lines():
    for line in _ps_output().splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        try:
            pid_number = int(pid)
        except ValueError:
            continue
        yield pid_number, args.strip()


def find_game_process(bundle=None):
    """The running game, if any. Matched on the launcher path, not on a name."""
    needle = None
    if bundle is not None:
        needle = bundle.launcher
    for pid, args in _ps_lines():
        if needle and needle in args:
            return {"pid": pid, "args": args}
        if needle is None and os.path.join("Kingdom Rush.app", LAUNCHER_REL) in args:
            return {"pid": pid, "args": args}
    return None


def is_running(bundle=None):
    return find_game_process(bundle) is not None


def find_steam_process():
    for pid, args in _ps_lines():
        if "/Steam.app/Contents/MacOS/" in args or args.endswith("/Steam.app/Contents/MacOS/steam_osx"):
            return {"pid": pid, "args": args}
    return None


def is_steam_running():
    return find_steam_process() is not None


# ---------------------------------------------------------------------------
# Steam Cloud (§15.5)
# ---------------------------------------------------------------------------


def steam_cloud_state(save_dir, appid=STEAM_APPID):
    """Report whether Steam Cloud mirrors the save directory.

    This title syncs `kingdom_rush/slot_1.lua` (measured in `remotecache.vdf`), which
    is a first-class hazard for a save editor, so it is surfaced rather than ignored.
    """
    info = {
        "userdata_found": False,
        "remotecache": None,
        "syncs_save_dir": False,
        "entries": [],
        "autocloud_marker": os.path.exists(save_dir.cloud_marker()) if save_dir else False,
    }
    userdata = os.path.join(STEAM_BASE, "userdata")
    if not os.path.isdir(userdata):
        return info
    for account in sorted(os.listdir(userdata)):
        if not account.isdigit():
            continue
        candidate = os.path.join(userdata, account, appid, "remotecache.vdf")
        if not os.path.exists(candidate):
            continue
        info["userdata_found"] = True
        info["remotecache"] = candidate
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        entries = sorted(set(re.findall(r'"([A-Za-z0-9_./-]*kingdom_rush/[^"]+)"', text)))
        info["entries"] = entries
        if any(entry.endswith(".lua") for entry in entries):
            info["syncs_save_dir"] = True
        break
    return info


# ---------------------------------------------------------------------------
# The archive fingerprint (§9.7 cache key)
# ---------------------------------------------------------------------------


def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def archive_hash(bundle, state=None):
    """SHA-256 of `game.love`, cached on (size, mtime) so `doctor` stays fast.

    `state.json` keys its cache on this plus `version_string`, so a game update
    invalidates cached probe results and mined ids automatically (§9.7).
    """
    path = bundle.game_love
    if not os.path.exists(path):
        raise NotFoundError("game.love not found: {0}".format(path), path=path)
    stat = os.stat(path)
    stamp = "{0}:{1}".format(stat.st_size, stat.st_mtime_ns)
    if state is not None:
        if state.get("archive_stamp") == stamp and state.get("archive_hash"):
            return state.get("archive_hash")
    digest = sha256_file(path)
    if state is not None:
        state.put("archive_stamp", stamp)
        state.put("archive_hash", digest)
    return digest


def running_summary(bundle=None):
    """Everything the safety gates need, computed once per run (§15.3)."""
    return {
        "game": find_game_process(bundle),
        "steam": find_steam_process(),
    }
