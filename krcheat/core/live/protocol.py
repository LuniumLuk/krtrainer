"""Request/response framing and channel mechanics (foundation §11.2-§11.3).

The channel is a directory:

    $TMPDIR/krcheat/<gamepid>/
    ├── cmd.json   request      (CLI -> agent)
    ├── out.json   response     (agent -> CLI)
    ├── hb         heartbeat timestamp  (CLI -> agent, §11.7 auto-clear)
    └── log        agent log    (§9.6: copied to ~/.krcheat/logs on clean exit)

`$TMPDIR` rather than a fixed path, so a crashed run is reaped by the OS instead of
leaving a channel behind.

**The handoff is atomic.** The CLI writes `cmd.json.tmp` and renames it over
`cmd.json`; the agent keys on the rename rather than on content. A polling reader and a
writing producer otherwise race, and a torn read surfaces as an intermittent parse
failure that looks like an agent bug. The agent writes `out.json` by the same rule.

This module is real today; what is missing is the agent on the other end (M3).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, Optional

#: §11.6: caps that keep a runaway snippet from filling the channel or our memory.
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

MODE_ONCE = "once"
MODE_ALWAYS = "always"
MODE_CLEAR = "clear"
MODES = (MODE_ONCE, MODE_ALWAYS, MODE_CLEAR)

#: Override keys are short and stable so `live status` can enumerate them (§11.2).
OVERRIDE_KEYS = ("gold", "lives", "speed", "god")

DEFAULT_TIMEOUT = 2.0
DEFAULT_HEARTBEAT_TIMEOUT = 10.0


def channel_root():
    return os.path.join(tempfile.gettempdir(), "krcheat")


def channel_dir(pid):
    return os.path.join(channel_root(), str(int(pid)))


def list_channels():
    """Every channel directory that exists, newest first."""
    root = channel_root()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.isdigit():
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            out.append({"pid": int(name), "path": path, "mtime": os.path.getmtime(path)})
        except OSError:
            continue
    out.sort(key=lambda item: item["mtime"], reverse=True)
    return out


class Channel(object):
    """One agent's channel directory."""

    def __init__(self, pid):
        self.pid = int(pid)
        self.path = channel_dir(self.pid)
        os.makedirs(self.path, exist_ok=True)

    # -- paths ---------------------------------------------------------------

    @property
    def cmd_path(self):
        return os.path.join(self.path, "cmd.json")

    @property
    def out_path(self):
        return os.path.join(self.path, "out.json")

    @property
    def heartbeat_path(self):
        return os.path.join(self.path, "hb")

    @property
    def log_path(self):
        return os.path.join(self.path, "log")

    # -- lifecycle -----------------------------------------------------------

    def exists(self):
        return os.path.isdir(self.path)

    def reset(self):
        """Start a clean channel: a stale response must never be read as a new one."""
        for path in (self.out_path,):
            try:
                os.remove(path)
            except OSError:
                pass
        return self

    def heartbeat_age(self):
        try:
            return max(0.0, time.time() - os.path.getmtime(self.heartbeat_path))
        except OSError:
            return None

    def touch_heartbeat(self):
        """Tell the agent a CLI is still alive, or it clears overrides (§11.7)."""
        try:
            with open(self.heartbeat_path, "a"):
                os.utime(self.heartbeat_path, None)
        except OSError:
            pass

    # -- framing -------------------------------------------------------------

    def write_request(self, request):
        payload = request.to_json()
        blob = json.dumps(payload).encode("utf-8")
        if len(blob) > MAX_REQUEST_BYTES:
            raise ValueError(
                "request is {0} bytes, above the {1} byte cap".format(len(blob), MAX_REQUEST_BYTES)
            )
        tmp = self.cmd_path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.cmd_path)
        return payload

    def read_response(self, request_id=None, deadline=None):
        """Read and parse `out.json`, ignoring a response for a different request."""
        try:
            with open(self.out_path, "rb") as handle:
                blob = handle.read(MAX_RESPONSE_BYTES + 1)
        except OSError:
            return None
        if len(blob) > MAX_RESPONSE_BYTES:
            return Response(
                id=request_id, ok=False, error="response exceeded the size cap", ms=None
            )
        try:
            payload = json.loads(blob.decode("utf-8"))
        except ValueError as exc:
            return Response(id=request_id, ok=False, error="unreadable response: {0}".format(exc))
        response = Response.from_json(payload)
        if request_id is not None and response.id != request_id:
            return None
        if deadline is not None and time.time() > deadline:
            return None
        return response

    def wait_response(self, request_id, timeout=DEFAULT_TIMEOUT, poll=0.005):
        """Poll for the response. The agent answers within a frame, so this is short."""
        deadline = time.time() + float(timeout)
        while time.time() <= deadline:
            response = self.read_response(request_id)
            if response is not None:
                return response
            self.touch_heartbeat()
            time.sleep(poll)
        return None

    def agent_log_tail(self, lines=20):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read().splitlines()[-lines:]
        except OSError:
            return []


class Request(object):
    """One request (§11.2). `key` is required for `always` and `clear`."""

    def __init__(self, code, mode=MODE_ONCE, key=None, request_id=None, label=None):
        if mode not in MODES:
            raise ValueError("unknown mode {0!r}".format(mode))
        if mode in (MODE_ALWAYS, MODE_CLEAR) and not key:
            raise ValueError("mode {0!r} requires a key".format(mode))
        if mode == MODE_CLEAR:
            code = None
        self.code = code
        self.mode = mode
        self.key = key
        self.id = request_id
        self.label = label

    def to_json(self):
        return {"id": self.id, "mode": self.mode, "key": self.key, "code": self.code}

    @classmethod
    def once(cls, code, request_id=None, label=None):
        return cls(code, mode=MODE_ONCE, request_id=request_id, label=label)

    @classmethod
    def always(cls, key, code, request_id=None, label=None):
        return cls(code, mode=MODE_ALWAYS, key=key, request_id=request_id, label=label)

    @classmethod
    def clear(cls, key, request_id=None, label=None):
        return cls(None, mode=MODE_CLEAR, key=key, request_id=request_id, label=label)


class Response(object):
    """One response (§11.2). `result` is a string the snippet produced itself (§11.4)."""

    def __init__(self, id=None, ok=False, result=None, error=None, ms=None, key=None):
        self.id = id
        self.ok = ok
        self.result = result
        self.error = error
        self.ms = ms
        self.key = key

    @classmethod
    def from_json(cls, payload):
        return cls(
            id=payload.get("id"),
            ok=bool(payload.get("ok")),
            result=payload.get("result"),
            error=payload.get("error"),
            ms=payload.get("ms"),
            key=payload.get("key"),
        )

    def decoded(self):
        """`result` is JSON produced by the game's own `lib/json.lua` (§11.4)."""
        if self.result is None:
            return None
        if isinstance(self.result, (dict, list)):
            return self.result
        try:
            return json.loads(self.result)
        except ValueError:
            return self.result

    def to_dict(self):
        return {
            "id": self.id,
            "key": self.key,
            "ok": self.ok,
            "ms": self.ms,
            "error": self.error,
            "result": self.decoded(),
        }
