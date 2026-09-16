"""Error types and the exit-code contract (§10.5).

Exit codes are part of the CLI contract, so they live on the exception rather than
being decided by the front-end. A caller that catches `KrcheatError` already knows
what to return.
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NOT_FOUND = 2
EXIT_CHANNEL = 3
EXIT_VALIDATION = 4
EXIT_BACKUP = 5
EXIT_INTERNAL = 6

EXIT_MEANING = {
    EXIT_OK: "success",
    EXIT_USAGE: "usage error",
    EXIT_NOT_FOUND: "game/app/save not found",
    EXIT_CHANNEL: "channel unavailable (game not running / agent absent)",
    EXIT_VALIDATION: "validation failure (schema, mandatory keys, out-of-range value)",
    EXIT_BACKUP: "backup or restore failure",
    EXIT_INTERNAL: "internal error",
}


class KrcheatError(Exception):
    """Base class. Carries the exit code the CLI must return."""

    code = EXIT_INTERNAL
    kind = "error"

    def __init__(self, message, **fields):
        super().__init__(message)
        self.message = message
        #: structured detail for the diagnostic log (§9.6) and for --json
        self.fields = fields

    def __str__(self):
        return self.message


class UsageError(KrcheatError):
    """Bad invocation: an unparseable argument, or a missing required selection."""

    code = EXIT_USAGE
    kind = "usage"


class NotFoundError(KrcheatError):
    """The app bundle, the archive, the save directory or a named slot is absent."""

    code = EXIT_NOT_FOUND
    kind = "not_found"


class ChannelUnavailable(KrcheatError):
    """Tier 2/3 surface that this build cannot reach.

    Used both for a genuine channel failure (game not running, agent absent) and
    for a documented-but-not-yet-implemented milestone, because the observable
    condition is the same: the capability is not reachable from here.
    """

    code = EXIT_CHANNEL
    kind = "unavailable"


class ValidationError(KrcheatError):
    """The save, or the text we were about to write, violates the schema (§12.3)."""

    code = EXIT_VALIDATION
    kind = "validation"


class BackupError(KrcheatError):
    """A snapshot could not be taken, or a restore did not verify (§15.2)."""

    code = EXIT_BACKUP
    kind = "backup"


class InternalError(KrcheatError):
    """A bug. Always accompanied by a traceback in the log (§9.6)."""

    code = EXIT_INTERNAL
    kind = "internal"


def milestone(name, detail):
    """A `ChannelUnavailable` for a capability documented but not built yet."""
    return ChannelUnavailable(
        "not implemented in this build ({0}): {1}".format(name, detail),
        milestone=name,
    )
