"""`Ctx` — what every operation receives (§9.5: `fn(ctx, **args) -> Result`).

The context carries configuration, the machine cache, the logger, the global flags
and the session state. It is the only way an operation reaches its environment, which
is what makes operations callable from the GUI, from tests and from `--self-test`
without a terminal in sight.

The session dictionary is per-process. It exists so that passing `--slot` once per
call is bearable inside a longer session (`krcheat gui`, `krcheat live watch`), and it
is **in memory only** — never written to `config.ini` or `state.json` (§10.7, D9). A
new invocation therefore begins with no slot and either receives one or asks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from krcheat.core import config as config_mod
from krcheat.core import log as log_mod
from krcheat.core import paths
from krcheat.core import state as state_mod
from krcheat.core.errors import KrcheatError, UsageError


@dataclass
class Ctx(object):
    command: str = "krcheat"
    argv: List[str] = field(default_factory=list)
    config: config_mod.Config = None
    state: state_mod.State = None
    log: log_mod.Logger = None

    # -- global flags (§10) --------------------------------------------------
    dry_run: bool = False
    assume_yes: bool = False
    force: bool = False
    interactive: bool = True
    verbose: bool = False
    json_output: bool = False

    #: run the S8 oracle as part of the write path (§15.2 step 2, D4)
    oracle: bool = True

    #: per-process slot memory; never persisted (D9)
    session: Dict[str, object] = field(default_factory=dict)

    game_override: Optional[str] = None
    save_dir_override: Optional[str] = None

    _bundle: object = None
    _save_dir: object = None

    # -- environment ---------------------------------------------------------

    def bundle(self):
        """The app bundle, resolved once. May raise `NotFoundError`."""
        if self._bundle is None:
            override = self.game_override or self.config.get("paths.game")
            self._bundle = paths.find_app_bundle(override)
            if self.log is not None:
                self.log.debug("paths.bundle", path=self._bundle.path,
                               version=self._bundle.short_version(),
                               expected_version_string=self._bundle.expected_version_string())
        return self._bundle

    def bundle_or_none(self):
        """The app bundle, or None when it cannot be found.

        For checks that are optional by design — the oracle, the version-string signal —
        where a missing game must degrade the check rather than fail the run.
        """
        try:
            return self.bundle()
        except KrcheatError:
            return None

    def save_dir(self):
        """The save directory, resolved once. May raise `NotFoundError`."""
        if self._save_dir is None:
            override = self.save_dir_override or self.config.get("paths.save_dir")
            self._save_dir = paths.find_save_dir(override)
            if self.log is not None:
                self.log.debug("paths.save_dir", path=self._save_dir.path,
                               slots=self._save_dir.find_slots())
        return self._save_dir

    def slot(self, explicit=None, ask=None):
        """Resolve the slot for a profile command (§10.7 step 1 > 2 > 3 > 4).

        The session's slot is passed as *session*, not as *explicit*: a session value whose
        file has since been deleted must fall through to the prompt, whereas an explicit
        `--slot` for a missing file is a hard error. Conflating the two made a deleted slot
        unaskable-about for the rest of the session.
        """
        number = paths.resolve_slot(
            self.save_dir(),
            explicit=explicit,
            session=self.session.get("slot"),
            ask=ask,
            interactive=self.interactive,
        )
        self.session["slot"] = number
        if self.log is not None:
            self.log.debug("paths.slot", slot=number, explicit=explicit is not None)
        return number

    def remember_slot(self, number):
        self.session["slot"] = int(number)
        return number

    # -- logging helpers -----------------------------------------------------

    def log_debug(self, event, **fields):
        if self.log is not None:
            self.log.debug(event, **fields)

    def log_info(self, event, **fields):
        if self.log is not None:
            self.log.info(event, **fields)

    def log_warn(self, event, **fields):
        if self.log is not None:
            self.log.warn(event, **fields)


def prompt_for_slot(available):
    """The interactive fallback of §10.7 step 3.

    The front-end owns this because `core/` must not print. It lists what exists and
    requires an answer; it does not offer a default, because a default is a guess.
    """
    import sys

    listing = ", ".join(str(number) for number in available)
    while True:
        sys.stderr.write("Which slot? [{0}]: ".format(listing))
        sys.stderr.flush()
        answer = sys.stdin.readline()
        if answer == "":
            raise UsageError("no slot selected and input ended; pass --slot N")
        answer = answer.strip()
        if not answer:
            continue
        try:
            number = int(answer)
        except ValueError:
            sys.stderr.write("  not a number: {0!r}\n".format(answer))
            continue
        if number not in available:
            sys.stderr.write("  slot {0} does not exist (found: {1})\n".format(number, listing))
            continue
        return number


def build(command="krcheat", argv=None, **flags):
    """Construct a context: load config and state, configure the log."""
    cfg = config_mod.Config.load()
    if not os.path.exists(cfg.path):
        try:
            cfg.ensure_file()
        except OSError:
            pass
    state = state_mod.State.load()
    ctx = Ctx(command=command, argv=list(argv or []), config=cfg, state=state, **flags)
    return ctx
