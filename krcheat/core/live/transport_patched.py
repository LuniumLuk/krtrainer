"""Transport B — a generated bootstrap file (foundation §9.3, §16.1).

If spike S2 passes, this collapses to *dropping one generated Lua source file into the
save directory*, because LÖVE mounts the save directory over the game source and
`require` finds our file first. No ZIP repack, no pristine archive, no `repair`, no
signature question — and it is reversible by deleting a file.

If S2 fails, this becomes the ZIP-repack version: replace the bytecode
`main_globals.lua` (119 bytes, three string constants) with equivalent Lua source that
also installs a per-frame poller, keep a pristine copy, and repack.

**Status: milestone M5**, and conditional on S2 which has not been run. The plan for both
outcomes is in §16.1, and the branch is recorded there rather than guessed at here.
"""

from __future__ import annotations

from krcheat.core.live.transport import LiveTransport


class PatchedLoveTransport(LiveTransport):
    name = "patched"
    milestone_name = "M5"
    description = "transport B (bootstrap module in the save directory or in game.love)"
    requires = "spike S2 to decide which shape it takes"
