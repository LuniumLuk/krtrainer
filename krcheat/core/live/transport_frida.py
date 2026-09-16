"""Transport C — attach to a Steam-launched process with Frida (foundation §9.3).

The last resort, and the only part of the project with a third-party dependency:
`pip install frida`, and in practice `sudo` on macOS. It exists so that a save file is
never the only way in, and it is never the recommended path.

**Status: not implemented, and deliberately last.** `frida` is not installed on the
reference machine (§7.4), so there is nothing here to test against.
"""

from __future__ import annotations

from krcheat.core.live.transport import LiveTransport


class FridaTransport(LiveTransport):
    name = "frida"
    milestone_name = "optional"
    description = "transport C (attach with Frida)"
    requires = "the optional frida dependency, and usually sudo"

    def available(self):
        try:
            import frida  # noqa: F401
        except Exception:
            return False, (
                "frida is not installed. Transport C is optional and last: prefer transport A, "
                "which needs no third-party dependency."
            )
        return False, self.reason
