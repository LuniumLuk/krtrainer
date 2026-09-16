"""Transport A — launch-time dylib injection (foundation §9.3).

    DYLD_INSERT_LIBRARIES=<path>/kr_agent.dylib \\
    SteamAppId=246420 \\
    "<...>/Kingdom Rush.app/Contents/MacOS/love"

The bundle's entitlements allow this without root, without re-signing and without
disabling SIP: `allow-dyld-environment-variables` and `disable-library-validation` are
both set (§3.4). The agent interposes `luaL_newstate` to capture the `lua_State *` and
`SDL_GL_SwapWindow` to get a once-per-frame callback on the main thread, then answers the
file channel.

This is the preferred transport because it modifies nothing: the game installation stays
untouched, and the game can be launched with or without cheats.

**Status: milestone M3.** The C agent (`krcheat/agent/kr_agent.c`) and its Makefile do
not exist yet. What exists today is the launch environment this transport will use, and
`doctor` reports whether the toolchain to build the agent is present.
"""

from __future__ import annotations

import os
from typing import Optional

from krcheat.core.live.transport import LiveTransport

#: Environment the game must see to believe it was launched by Steam.
LAUNCH_ENV = {"SteamAppId": "246420", "SteamGameId": "246420"}

#: The variable the launcher sets. Kept here so the future implementation and the docs
#: cannot drift apart.
DYLD_VARIABLE = "DYLD_INSERT_LIBRARIES"


def agent_path():
    """Where the built agent lives, whether or not it has been built."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agent", "kr_agent.dylib")


class DylibTransport(LiveTransport):
    name = "dylib"
    milestone_name = "M3"
    description = "transport A (launch-time dylib injection)"
    requires = "the injected agent (krcheat/agent/kr_agent.c, built with clang)"

    def launch_env(self, bundle):
        """The environment `krcheat play` will use, once the agent exists."""
        env = dict(os.environ)
        env.update(LAUNCH_ENV)
        env[DYLD_VARIABLE] = os.path.abspath(agent_path())
        return env
