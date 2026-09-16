"""`Result` — the single return shape of every operation (§9.5).

    fn(ctx, **args) -> Result

A `Result` carries the changed nodes, warnings, the snapshot id, and the exit-code
semantics of §10.5. `cli.py` renders it as text or JSON; `gui/` renders it into
widgets. Neither front-end inspects the filesystem to find out what happened —
which is what keeps them from diverging.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Change:
    """One node the operation intends to alter."""

    path: str
    before: Any = None
    after: Any = None
    #: set when a value was requested but resolved to what was already there
    noop: bool = False

    def to_dict(self):
        out = {"path": self.path, "before": _jsonable(self.before), "after": _jsonable(self.after)}
        if self.noop:
            out["noop"] = True
        return out

    def render(self):
        arrow = "->" if not self.noop else "== (already)"
        return "{0}: {1} {2} {3}".format(self.path, _short(self.before), arrow, _short(self.after))


def _jsonable(value):
    """Everything in a payload must survive json.dumps (§11.4 analogue for tier 1)."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _short(value, limit=60):
    text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


@dataclass
class Result:
    command: str
    ok: bool = True
    exit_code: int = 0
    #: nodes the operation changed (empty for read-only commands)
    changes: List[Change] = field(default_factory=list)
    #: things the user should know but that did not stop the operation
    warnings: List[str] = field(default_factory=list)
    #: informational lines
    notes: List[str] = field(default_factory=list)
    #: structured output, rendered by the front-end; for --json this is the body
    payload: Dict[str, Any] = field(default_factory=dict)
    #: snapshot id taken by this run, if any (§15.2 step 1)
    snapshot: Optional[str] = None
    dry_run: bool = False

    # -- construction helpers ------------------------------------------------

    def add_change(self, path, before=None, after=None, noop=False):
        self.changes.append(Change(path=path, before=before, after=after, noop=noop))
        return self

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)
        return self

    def note(self, message):
        self.notes.append(message)
        return self

    def set(self, **payload):
        self.payload.update(payload)
        return self

    # -- introspection -------------------------------------------------------

    @property
    def effective_changes(self):
        """The changes that actually alter something (a no-op is not a change)."""
        return [change for change in self.changes if not change.noop]

    def to_dict(self):
        return {
            "command": self.command,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "dry_run": self.dry_run,
            "snapshot": self.snapshot,
            "changes": [c.to_dict() for c in self.changes],
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "result": _jsonable(self.payload),
        }
