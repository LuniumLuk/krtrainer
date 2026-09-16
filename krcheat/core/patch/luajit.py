"""Tier 3 — offline LuaJIT bytecode patching (foundation §14). Backlog.

Demoted by decision D3. If spike S2 confirms that the save directory shadows the game
source, F15 is delivered by a generated Lua source module (§9.8, `core/data.py`) and this
tier is only revisited if S2 fails. The technique stays documented because it is the only
offline path that works when the read path cannot be shadowed:

* payloads are LuaJIT bytecode, magic ``\\x1bLJ\\x02``;
* string constants are `GCstr` objects with a length prefix, so names are discoverable by
  scanning;
* a candidate constant is located *by value and by context*, then replaced in place at
  the same width, so no offset anywhere in the file changes;
* the resulting module can be smoke-tested through the S8 oracle before the game sees it,
  which is what removed the old validation blocker.

**Status: milestone M8 (backlog).** Only `is_bytecode` is implemented, because
`core/data.py` needs it to sanity-check what it extracts from the archive.
"""

from __future__ import annotations

from krcheat.core.errors import milestone

MAGIC = b"\x1bLJ\x02"


def is_bytecode(data):
    """True when the blob carries the LuaJIT bytecode header (§14)."""
    return bool(data) and data.startswith(b"\x1bLJ")


def scan(path, value, module=None, kind="auto"):
    """Find constants equal to `value` in a module's bytecode."""
    raise milestone(
        "M8",
        "the bytecode scanner is backlog (foundation 14). It is only pursued if spike S2 fails: "
        "with S2 passing, F15 uses a generated shadow module instead of an in-place patch.",
    )


def apply(path, hits):
    """Write a patched copy of the archive."""
    raise milestone("M8", "bytecode patching is backlog (foundation 14).")


def restore(path=None):
    """Restore the pristine archive kept by a patch."""
    raise milestone("M8", "bytecode patching is backlog (foundation 14).")
