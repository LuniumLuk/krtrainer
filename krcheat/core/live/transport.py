"""The `LiveTransport` seam (foundation §11.1).

The CLI depends only on this interface, so the transport is a configurable strategy
rather than a design input:

    class LiveTransport(Protocol):
        def available(self) -> bool
        def start(self, launch: bool) -> None
        def stop(self) -> None
        def request(self, code: str, timeout: float = 2.0) -> Response

Selection order comes from `config.ini` (`live.preferred_transport`) with a CLI
override, so the same command works whichever transport happens to be installed.

**What is real here and what is not.** The interface, the channel, the protocol and the
snippet library are implemented. The native agent that answers on the other end
(milestone M3) is not, so every concrete transport reports its milestone instead of
failing mysteriously. `live status` is the command that says so plainly.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from krcheat.core.errors import ChannelUnavailable, UsageError, milestone
from krcheat.core.live.protocol import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_TIMEOUT,
    Channel,
    Request,
    Response,
)

TRANSPORT_NAMES = ("dylib", "patched", "frida")


class LiveTransport(object):
    """Base class. Subclasses supply identity; this supplies the honest defaults."""

    name = "abstract"
    milestone_name = "M3"
    description = "the live channel"
    requires = "the injected agent"

    def __init__(self, ctx=None, pid=None):
        self.ctx = ctx
        self.pid = pid
        self._channel = None

    # -- identity ------------------------------------------------------------

    def __repr__(self):
        return "<{0} transport>".format(self.name)

    @property
    def reason(self):
        return "{0} is not implemented in this build (milestone {1}). It needs {2}.".format(
            self.description, self.milestone_name, self.requires
        )

    # -- interface -----------------------------------------------------------

    def available(self):
        """(usable, why not). Never raises: status reporting must not fail."""
        return False, self.reason

    def require(self):
        raise milestone(self.milestone_name, self.reason)

    def start(self, launch=True):
        self.require()

    def stop(self):
        self.require()

    def request(self, code, mode="once", key=None, timeout=None):
        self.require()

    # -- shared plumbing (usable by a future implementation) ------------------

    def channel_for(self, pid):
        if self._channel is None or self._channel.pid != int(pid):
            self._channel = Channel(pid)
        return self._channel

    def send(self, channel, request, timeout=DEFAULT_TIMEOUT):
        """Write a request and wait for its response, with the heartbeat kept alive."""
        payload = channel.write_request(request)
        self.log("channel.request", **{k: v for k, v in payload.items() if k != "code"})
        response = channel.wait_response(request.id, timeout=timeout)
        if response is None:
            self.log("channel.timeout", id=request.id, timeout=timeout)
        return response

    def log(self, event, **fields):
        if self.ctx is not None and getattr(self.ctx, "log", None) is not None:
            self.ctx.log.debug(event, transport=self.name, **fields)

    def heartbeat_timeout(self):
        if self.ctx is not None:
            try:
                return float(self.ctx.config.get_typed("live.heartbeat_timeout"))
            except Exception:
                pass
        return DEFAULT_HEARTBEAT_TIMEOUT

    def timeout(self):
        if self.ctx is not None:
            try:
                return float(self.ctx.config.get_typed("live.request_timeout"))
            except Exception:
                pass
        return DEFAULT_TIMEOUT

    def describe(self):
        usable, reason = self.available()
        return {
            "transport": self.name,
            "available": usable,
            "milestone": self.milestone_name,
            "reason": None if usable else reason,
            "requires": self.requires,
        }


def load(name):
    """Instantiate a transport by name, importing lazily so the tiers stay separate."""
    name = (name or "dylib").strip().lower()
    if name == "dylib":
        from krcheat.core.live.transport_dylib import DylibTransport

        return DylibTransport
    if name == "patched":
        from krcheat.core.live.transport_patched import PatchedLoveTransport

        return PatchedLoveTransport
    if name == "frida":
        from krcheat.core.live.transport_frida import FridaTransport

        return FridaTransport
    raise UsageError(
        "unknown transport {0!r}; expected one of {1}".format(name, ", ".join(TRANSPORT_NAMES))
    )


def select(ctx=None, override=None):
    """The transport to use, unless a later one is explicitly requested."""
    name = override
    if not name and ctx is not None:
        try:
            name = ctx.config.get_typed("live.preferred_transport")
        except Exception:
            name = None
    return load(name or "dylib")(ctx=ctx)


def describe_all(ctx=None):
    """Status of every transport, for `live status` and for `doctor`."""
    out = []
    for name in TRANSPORT_NAMES:
        try:
            transport = load(name)(ctx=ctx)
            out.append(transport.describe())
        except Exception as exc:  # pragma: no cover - defensive
            out.append({"transport": name, "available": False, "reason": str(exc)})
    return out
