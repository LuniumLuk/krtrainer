"""Append-only JSONL diagnostic log (foundation §9.6, decision D8).

One JSON object per line, flushed per record, so a crash still leaves a usable tail.
This is the file the user reads after a run — including the debug-mode run that is
used to diagnose a capability — so the record shape is designed for `grep`/`jq`
rather than for pretty printing.

Rules from §9.6:

1. Log **values, not contents** — hashes, paths, sizes and counts by default; field
   values only at debug, truncated.
2. Rotate by day, and cap total size; prune at startup so the log cannot grow without
   bound.
3. Diagnostic only. It is never read back as state; deleting it must be safe.
4. A logging failure is never fatal.

Everything here is best-effort by construction: every write is wrapped, and a failure
downgrades to silence rather than to an exception.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import uuid
from typing import Any, Dict, List, Optional

from krcheat.core import paths

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "off": 100}

#: Long values are truncated unless we are at debug (§9.6 rule 1).
TRUNCATE_DEFAULT = 160
TRUNCATE_DEBUG = 2000

DEFAULT_KEEP_DAYS = 14
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def log_path_for_day(when=None):
    when = when or datetime.datetime.now()
    return os.path.join(paths.logs_dir(), "krcheat-{0:%Y%m%d}.jsonl".format(when))


class Logger(object):
    """A tiny structured logger. Never raises, never reads state back."""

    def __init__(
        self,
        path=None,
        level="info",
        stderr_level=None,
        run_id=None,
        command=None,
        argv=None,
        enabled=True,
    ):
        self.path = path
        self.level = LEVELS.get(str(level).lower(), LEVELS["info"])
        #: `-v` turns on debug logging to stderr *in addition* to the file
        self.stderr_level = LEVELS.get(str(stderr_level).lower()) if stderr_level else None
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.command = command
        self.argv = list(argv or [])
        self.enabled = bool(enabled)
        self.seq = 0
        self.failures = 0
        self._lock = threading.Lock()
        self._handle = None
        if self.enabled and self.path:
            self._open()

    # -- lifecycle -----------------------------------------------------------

    def _open(self):
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")
        except Exception:
            self._handle = None
            self.failures += 1

    def close(self):
        try:
            if self._handle is not None:
                self._handle.close()
        except Exception:
            pass
        finally:
            self._handle = None

    # -- emission ------------------------------------------------------------

    def _prepare(self, value, truncate):
        if isinstance(value, str):
            if len(value) > truncate:
                return value[:truncate] + "...<{0} chars total>".format(len(value))
            return value
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): self._prepare(v, truncate) for k, v in list(value.items())[:60]}
        if isinstance(value, (list, tuple)):
            return [self._prepare(v, truncate) for v in list(value)[:60]]
        return self._prepare(repr(value), truncate)

    def emit(self, level, event, **fields):
        numeric = LEVELS.get(level, LEVELS["info"])
        truncate = TRUNCATE_DEBUG if numeric <= LEVELS["debug"] else TRUNCATE_DEFAULT
        record = {
            "ts": now_iso(),
            "lvl": level,
            "run": self.run_id,
            "seq": self.seq,
            "event": event,
        }
        if self.command and event.endswith(("start", "end", "error")):
            record["cmd"] = self.command
        for key, value in fields.items():
            if value is None:
                continue
            record[key] = self._prepare(value, truncate)
        self.seq += 1

        if self.stderr_level is not None and numeric >= self.stderr_level:
            try:
                sys.stderr.write("[{0}] {1}\n".format(level, _human(record)))
            except Exception:
                pass
        if not self.enabled or numeric < self.level:
            return
        if self._handle is None:
            return
        try:
            with self._lock:
                self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._handle.flush()
        except Exception:
            self.failures += 1

    # -- convenience ---------------------------------------------------------

    def debug(self, event, **fields):
        self.emit("debug", event, **fields)

    def info(self, event, **fields):
        self.emit("info", event, **fields)

    def warn(self, event, **fields):
        self.emit("warn", event, **fields)

    def error(self, event, **fields):
        self.emit("error", event, **fields)

    def exception(self, event, exc, **fields):
        """Log a traceback. Only exit 6 carries one (§9.6)."""
        import traceback

        fields = dict(fields)
        fields["exc_type"] = type(exc).__name__
        fields["exc"] = str(exc)
        fields["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        self.emit("error", event, **fields)


class NullLogger(Logger):
    """`--no-log`: keeps call sites identical, writes nothing."""

    def __init__(self):
        Logger.__init__(self, path=None, level="off", enabled=False)

    def emit(self, level, event, **fields):  # noqa: D401 - deliberate no-op
        return


def _human(record):
    skip = {"ts", "lvl", "run", "seq", "event", "cmd"}
    detail = " ".join(
        "{0}={1}".format(key, value)
        for key, value in record.items()
        if key not in skip
    )
    return "{0} {1} {2}".format(record["event"], record.get("cmd", ""), detail).strip()


# ---------------------------------------------------------------------------
# Module-level singleton (configured by the front-end, used by core)
# ---------------------------------------------------------------------------

_LOGGER = NullLogger()


def get_logger():
    return _LOGGER


def set_logger(logger):
    global _LOGGER
    _LOGGER = logger
    return logger


def configure(
    level="info",
    path=None,
    enabled=True,
    command=None,
    argv=None,
    stderr_level=None,
    prune=True,
    keep_days=DEFAULT_KEEP_DAYS,
    max_bytes=DEFAULT_MAX_BYTES,
):
    """Build and install the process logger. Returns it (never raises)."""
    logger = NullLogger() if not enabled else Logger(
        path=path or log_path_for_day(),
        level=level,
        stderr_level=stderr_level,
        command=command,
        argv=argv,
        enabled=True,
    )
    set_logger(logger)
    if prune and enabled:
        try:
            prune_logs(keep_days=keep_days, max_bytes=max_bytes)
        except Exception:
            pass
    return logger


# ---------------------------------------------------------------------------
# Reading and pruning the log
# ---------------------------------------------------------------------------


def log_files():
    """Every krcheat log file, oldest first."""
    directory = paths.logs_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        os.path.join(directory, name)
        for name in names
        if name.startswith("krcheat-") and name.endswith(".jsonl")
    ]


def read_records(path, limit=None):
    """Parse a log file into records, skipping lines that do not parse."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"raw": line})
    except OSError:
        return out
    if limit is not None:
        return out[-limit:]
    return out


def tail_lines(count=40):
    """The last `count` records across the newest log files."""
    records = []
    for path in reversed(log_files()):
        records = read_records(path) + records
        if len(records) >= count:
            break
    return records[-count:], (log_files()[-1] if log_files() else None)


def prune_logs(keep_days=DEFAULT_KEEP_DAYS, max_bytes=DEFAULT_MAX_BYTES, now=None):
    """Apply the retention policy (§9.6 rule 2). Returns what was removed."""
    removed = []
    now = now or datetime.datetime.now()
    files = log_files()
    # by age
    for path in files:
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            continue
        if (now - mtime).days >= int(keep_days):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    # by total size, oldest first
    remaining = log_files()
    sizes = []
    for path in remaining:
        try:
            sizes.append((path, os.path.getsize(path)))
        except OSError:
            continue
    total = sum(size for _path, size in sizes)
    for path, size in sizes:
        if total <= int(max_bytes):
            break
        try:
            os.remove(path)
            removed.append(path)
            total -= size
        except OSError:
            pass
    return removed


def active_log_path():
    """Where the *current* log goes: today's file if it exists, else where it will."""
    files = log_files()
    return files[-1] if files else log_path_for_day()
