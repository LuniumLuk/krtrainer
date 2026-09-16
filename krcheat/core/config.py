"""`config.ini` — user-owned settings (foundation §9.7, decision D8).

Hand-editable, so **unknown keys and comments survive a rewrite**. `configparser`
would happily drop both, so the writer edits the text line by line and leaves every
byte it did not have to touch alone — the same instinct as the save codec, for the
same reason.

What is deliberately *not* here: slot selection. A stale default slot is the failure
mode that matters (D9, §10.7), so no slot key exists in either file.

Precedence is **CLI flag > config > state > built-in default**.
"""

from __future__ import annotations

import configparser
import os
from typing import Any, Dict, List, Optional, Tuple

from krcheat.core import paths
from krcheat.core.errors import UsageError

#: The file written on first run. Comments are part of the product (§9.7).
DEFAULT_TEXT = """\
# krcheat configuration - hand-editable, owned by you (foundation document 9.7).
# Unknown keys and comments are preserved when krcheat rewrites this file.
# Slot selection is deliberately NOT stored here (D9): pass --slot N on each call.

[paths]
# Override discovery. Empty means "use the built-in default".
game =
save_dir =

[ui]
# The tkinter GUI is opt-in and is not part of the intended product on macOS: the CLI is
# the whole tool (decision D10). Set this to true to enable `krcheat gui`.
enabled = false
# The GUI asks for confirmation before it writes, showing the --dry-run diff.
confirm_before_write = true
window_geometry = 1000x680
last_tab = profile

[live]
# Tier 2 (foundation 11). Not implemented in this build yet.
preferred_transport = dylib
request_timeout = 2.0
heartbeat_timeout = 10.0

[logging]
# debug | info | warn | error
level = info
retention_days = 14
max_total_mb = 20

[safety]
# Steam running is a warning gated behind --yes (foundation 15.3), not a refusal.
require_yes_when_steam_running = true
"""

#: Built-in defaults. Every key is `section.key`.
DEFAULTS: Dict[str, Any] = {
    "paths.game": None,
    "paths.save_dir": None,
    "ui.enabled": False,
    "ui.confirm_before_write": True,
    "ui.window_geometry": "1000x680",
    "ui.last_tab": "profile",
    "live.preferred_transport": "dylib",
    "live.request_timeout": 2.0,
    "live.heartbeat_timeout": 10.0,
    "logging.level": "info",
    "logging.retention_days": 14,
    "logging.max_total_mb": 20,
    "safety.require_yes_when_steam_running": True,
}

_BOOL_KEYS = {key for key, value in DEFAULTS.items() if isinstance(value, bool)}
_INT_KEYS = {key for key, value in DEFAULTS.items() if isinstance(value, int) and not isinstance(value, bool)}
_FLOAT_KEYS = {key for key, value in DEFAULTS.items() if isinstance(value, float)}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def parse_scalar(key, text):
    """Coerce a config string using the default's type. Raises `UsageError` on junk."""
    if text is None:
        return None
    body = str(text).strip()
    if body == "":
        return None if DEFAULTS.get(key) is None else ""
    if key in _BOOL_KEYS:
        lowered = body.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise UsageError("{0} expects a boolean, got {1!r}".format(key, text))
    if key in _INT_KEYS:
        try:
            return int(body)
        except ValueError:
            raise UsageError("{0} expects an integer, got {1!r}".format(key, text))
    if key in _FLOAT_KEYS:
        try:
            return float(body)
        except ValueError:
            raise UsageError("{0} expects a number, got {1!r}".format(key, text))
    return body


def format_scalar(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def split_key(key):
    if "." not in key:
        raise UsageError("config keys are written section.key, e.g. logging.level (got {0!r})".format(key))
    section, _, name = key.partition(".")
    section = section.strip()
    name = name.strip()
    if not section or not name:
        raise UsageError("invalid config key: {0!r}".format(key))
    return section, name


#: The command that turns the GUI on, quoted in messages so the user never has to guess.
GUI_ENABLE_HINT = "krcheat config set ui.enabled true"


def gui_enabled(cfg):
    """Whether `krcheat gui` is switched on (decision D10: it is opt-in, macOS use is CLI-only).

    A config that cannot answer the question counts as "off": the GUI is optional, and a
    broken config must not be the reason it starts.
    """
    try:
        return bool(cfg.get_typed("ui.enabled"))
    except Exception:
        return False


class Config(object):
    """A parsed `config.ini` plus the raw text needed to rewrite it safely."""

    def __init__(self, path=None, text=""):
        self.path = path or paths.config_path()
        self.text = text
        self._parser = configparser.ConfigParser(interpolation=None)
        self._parser.optionxform = str  # keep case as written
        if text.strip():
            try:
                self._parser.read_string(text)
            except configparser.Error as exc:
                raise UsageError("config.ini is not parseable: {0}".format(exc), path=self.path)
        self._explicit = set()
        for section in self._parser.sections():
            for option in self._parser.options(section):
                self._explicit.add("{0}.{1}".format(section, option))

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, path=None):
        """Read the file. A missing file is not an error — defaults apply."""
        target = path or paths.config_path()
        if not os.path.exists(target):
            return cls(path=target, text="")
        try:
            with open(target, "r", encoding="utf-8") as handle:
                return cls(path=target, text=handle.read())
        except OSError as exc:
            raise UsageError("cannot read {0}: {1}".format(target, exc), path=target)

    def ensure_file(self):
        """Write the commented default file if there is none. Returns True if created."""
        if os.path.exists(self.path):
            return False
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(DEFAULT_TEXT)
        self.text = DEFAULT_TEXT
        self._parser = configparser.ConfigParser(interpolation=None)
        self._parser.optionxform = str
        self._parser.read_string(DEFAULT_TEXT)
        for section in self._parser.sections():
            for option in self._parser.options(section):
                self._explicit.add("{0}.{1}".format(section, option))
        return True

    # -- reading -------------------------------------------------------------

    def get(self, key, default=None):
        section, name = split_key(key)
        try:
            raw = self._parser.get(section, name, fallback=None)
        except (configparser.Error, ValueError):
            raw = None
        if raw is None:
            if default is not None:
                return default
            return DEFAULTS.get(key, default)
        value = parse_scalar(key, raw)
        if value == "" and DEFAULTS.get(key) is None:
            # An explicitly empty `game =` means "unset", not "the empty path".
            return None
        return value

    def get_typed(self, key):
        """`get` but with the built-in default substituted for empty values."""
        value = self.get(key)
        return DEFAULTS.get(key) if value in (None, "") else value

    def items(self):
        """Every known key with its effective value, plus unknown keys as read."""
        out = {}
        for key in DEFAULTS:
            out[key] = self.get_typed(key)
        for key in sorted(self._explicit):
            if key not in out:
                out[key] = self.get(key)
        return out

    def unknown_keys(self):
        return sorted(key for key in self._explicit if key not in DEFAULTS)

    # -- writing -------------------------------------------------------------

    def render_with(self, key, value):
        """Return new file text with `section.key` set, preserving everything else."""
        if key not in DEFAULTS:
            section, _ = split_key(key)
            known = {k.split(".")[0] for k in DEFAULTS}
            if section not in known:
                raise UsageError(
                    "unknown config section {0!r} (known: {1})".format(
                        section, ", ".join(sorted(known))
                    )
                )
        return _set_in_text(self.text or DEFAULT_TEXT, *split_key(key), format_scalar(value))

    def set(self, key, value, write=True):
        """Set a key. Validates the type against the default's type first."""
        parsed = parse_scalar(key, value)
        if key in DEFAULTS and DEFAULTS[key] is not None and parsed is None:
            raise UsageError("{0} cannot be empty".format(key))
        text = self.render_with(key, value)
        if write:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, self.path)
        self.text = text
        reloaded = Config(path=self.path, text=text)
        self._parser = reloaded._parser
        self._explicit = reloaded._explicit
        return self


def _set_in_text(text, section, name, value):
    """Edit one key in place inside the file text, byte-preserving elsewhere."""
    lines = text.split("\n")
    header = "[{0}]".format(section)
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == header.lower():
            start = index
            break
    if start is None:
        body = text
        if body and not body.endswith("\n"):
            body += "\n"
        if body and not body.endswith("\n\n"):
            body += "\n"
        return body + "{0}\n{1} = {2}\n".format(header, name, value)

    end = len(lines)
    for index in range(start + 1, len(lines)):
        body = lines[index].strip()
        if body.startswith("[") and body.endswith("]"):
            end = index
            break

    for index in range(start + 1, end):
        body = lines[index].strip()
        if not body or body.startswith(("#", ";")):
            continue
        existing = body.split("=", 1)[0].strip()
        if existing.lower() == name.lower():
            indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
            lines[index] = "{0}{1} = {2}".format(indent, name, value)
            return "\n".join(lines)

    insert_at = start + 1
    for index in range(start + 1, end):
        if lines[index].strip():
            insert_at = index + 1
    lines.insert(insert_at, "{0} = {1}".format(name, value))
    return "\n".join(lines)
