"""`state.json` — machine-owned cache (foundation §9.7, decision D8).

Separate from `config.ini` on purpose: the two have different owners. Mixing them
means a machine-written blob eventually destroys hand-written comments. This file is
never hand-edited; when it is invalid it is *removed*, not repaired.

It earns its place by being keyed on `version_string` + the `game.love` hash, so:

* repeat runs skip the archive hash and the constant-pool walk, and
* a game update invalidates the cache automatically instead of silently reusing
  stale field paths or mined ids.

Slot selection is deliberately absent (D9). The slot *inventory* is cached — which
slots exist — because that is a fact about the filesystem, not a preference.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional

from krcheat.core import paths

SCHEMA = 1


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class State(object):
    def __init__(self, path=None, data=None, exists=False):
        self.path = path or paths.state_path()
        self.data = data if data is not None else {}
        self.exists = exists
        self.dirty = False

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, path=None):
        """Read the cache. A missing or corrupt file yields an empty state."""
        target = path or paths.state_path()
        if not os.path.exists(target):
            return cls(path=target, data={"_schema": SCHEMA}, exists=False)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("state.json is not an object")
        except (OSError, ValueError):
            # Corrupt cache: drop it. It is a cache, so losing it is always safe.
            try:
                os.replace(target, target + ".corrupt")
            except OSError:
                pass
            return cls(path=target, data={"_schema": SCHEMA}, exists=False)
        data.setdefault("_schema", SCHEMA)
        return cls(path=target, data=data, exists=True)

    # -- access --------------------------------------------------------------

    def get(self, key, default=None):
        return self.data.get(key, default)

    def put(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self.dirty = True
        return self

    def drop(self, key):
        if key in self.data:
            del self.data[key]
            self.dirty = True

    def save(self, force=False):
        """Atomic write. Failure is reported to the caller, not raised at the user."""
        if not self.dirty and not force and self.exists:
            return False
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.data["_updated"] = now_iso()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)
        self.exists = True
        self.dirty = False
        return True

    # -- cache validity (§9.7) ----------------------------------------------

    def cache_matches(self, version_string, archive_hash=None):
        """True when cached derivations are still valid for this install."""
        if self.get("version_string") != version_string:
            return False
        if archive_hash is not None and self.get("archive_hash") != archive_hash:
            return False
        return True

    def stamp_install(self, version_string, archive_hash=None):
        """Record the identity of the install, invalidating stale caches on change."""
        changed = False
        if self.get("version_string") != version_string:
            self.drop("mined_ids")
            self.drop("probed_paths")
            changed = True
        self.put("version_string", version_string)
        if archive_hash is not None:
            self.put("archive_hash", archive_hash)
        return changed

    def invalidate(self, why=None):
        """Forget everything derived. Called when the cache cannot be trusted."""
        for key in ("mined_ids", "probed_paths", "archive_hash", "archive_stamp"):
            self.drop(key)
        if why:
            self.put("invalidated", {"reason": why, "when": now_iso()})
        return self

    # -- convenience ---------------------------------------------------------

    def record_slots(self, inventory):
        self.put("slots", inventory)

    def record_doctor(self, summary):
        self.put("last_doctor", {"when": now_iso(), "summary": summary})

    def record_snapshot(self, snapshot_id):
        self.put("last_snapshot", snapshot_id)
