"""Tier 2 — the in-process Lua channel (foundation §11).

The CLI depends only on the `LiveTransport` shape below, so the transport is a
configurable strategy rather than a design input. All three transports share this
protocol, which is why the CLI can be written once.

**Status: not implemented in this build.** The protocol, the channel mechanics and the
snippet library are here and are real; the native agent (milestone M3) is not. Every
entry point therefore reports the milestone rather than pretending, and `live status`
answers honestly. Tier 1 (the save editor) has no dependency on any of this.
"""

from krcheat.core.live.protocol import Channel, Request, Response
from krcheat.core.live.transport import LiveTransport, select

__all__ = ["Channel", "Request", "Response", "LiveTransport", "select"]
