"""S8 — drive the *shipped* `Lua.framework` from Python via `ctypes` (decision D4).

The game ships LuaJIT 2.1 as a Mach-O dylib that exports the whole Lua C API,
including `luaL_newstate` and `luaL_loadbuffer` (verified). That is the same VM the
game uses, so "does the game accept this file?" becomes a question we can answer
before writing rather than after.

Two uses, in order of value:

* **Validate a save** (`mode="run"`). A save chunk is pure data — no `require`, no
  `love.*` — so it loads and runs standalone, and the resulting table can be compared
  against what we intended. This is the authoritative check of §15.2 step 2.
* **Validate generated Lua source** (`mode="load"`). A generated shadow module
  (F15, §9.8) calls into LÖVE and cannot run out of process, but *compiling* it is
  exactly what `luaL_loadbuffer` does, so a syntax error is still caught.

Two deliberate constraints:

* **It runs in a subprocess.** A badly behaved chunk, or a bug in this binding, can
  take the process down; that must cost us a child process, not the user's run. The
  parent reports a crash as an oracle failure and carries on.
* **No libraries are opened.** We never call `luaL_openlibs`, so the sandbox has no
  `io`, `os` or `os.execute` to reach for. Data chunks do not need them, and not
  having them is strictly better than having them.

This module is optional at every call site: when the game is not installed, or the
framework cannot be loaded, `available()` returns False and callers skip the check
rather than fail (§16.2 — oracle tests are skipped, not failed, without the game).
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from krcheat.core import paths

# Lua type tags (lua.h)
LUA_TNONE = -1
LUA_TNIL = 0
LUA_TBOOLEAN = 1
LUA_TLIGHTUSERDATA = 2
LUA_TNUMBER = 3
LUA_TSTRING = 4
LUA_TTABLE = 5
LUA_TFUNCTION = 6
LUA_TUSERDATA = 7
LUA_TTHREAD = 8

LUA_OK = 0

MAX_DEPTH = 24
MAX_ENTRIES = 200000

CHILD_MODULE = "krcheat.core.oracle"


def framework_path(bundle=None):
    """The `Lua.framework` binary, or None when it is not installed."""
    if bundle is not None:
        candidate = bundle.lua_framework
        return candidate if os.path.exists(candidate) else None
    try:
        bundle = paths.find_app_bundle()
    except Exception:
        return None
    return bundle.lua_framework if os.path.exists(bundle.lua_framework) else None


def available(bundle=None):
    """Cheap check: is there a framework we *could* load? Does not load it."""
    return framework_path(bundle) is not None


# ---------------------------------------------------------------------------
# Parent-side entry point
# ---------------------------------------------------------------------------


def check(text, name="<chunk>", mode="run", bundle=None, timeout=20.0, python=None):
    """Load (and optionally run) `text` in the game's own VM, in a subprocess.

    Returns a dict: `{ok, mode, error, value, crash, skipped}`. Never raises: the
    oracle is an enhancement, so its failure must be reportable, not fatal.
    """
    path = framework_path(bundle)
    if path is None:
        return {"ok": False, "skipped": True, "error": "Lua.framework not found", "mode": mode}
    payload = json.dumps({"framework": path, "name": name, "mode": mode, "source": text})
    try:
        completed = subprocess.run(
            [python or sys.executable, "-m", CHILD_MODULE],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "mode": mode, "error": "oracle timed out after {0}s".format(timeout)}
    except Exception as exc:  # pragma: no cover - exotic failure
        return {"ok": False, "mode": mode, "error": "oracle could not start: {0}".format(exc)}

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = stderr[-1] if stderr else ""
        return {
            "ok": False,
            "mode": mode,
            "crash": True,
            "returncode": completed.returncode,
            "error": "oracle process failed (rc={0}) {1}".format(completed.returncode, tail),
        }
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except ValueError:
        return {"ok": False, "mode": mode, "error": "oracle produced unreadable output"}
    result.setdefault("mode", mode)
    return result


def normalize(value):
    """Make a Python value comparable with what the oracle reports.

    The oracle stringifies table keys (Lua tables are keyed on anything); this does
    the same on the Python side, so `{1: ...}` and `{"1": ...}` compare equal.
    """
    if isinstance(value, dict):
        return {_key(key): normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def _key(key):
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float) and key.is_integer():
        return str(int(key))
    return str(key)


# ---------------------------------------------------------------------------
# Child-side: the actual LuaJIT binding
# ---------------------------------------------------------------------------


def _bind(lib):
    """Declare the handful of C signatures the oracle needs."""
    c_void_p = ctypes.c_void_p
    c_int = ctypes.c_int
    c_char_p = ctypes.c_char_p
    c_size_t = ctypes.c_size_t
    p_size_t = ctypes.POINTER(ctypes.c_size_t)

    lib.luaL_newstate.restype = c_void_p
    lib.luaL_newstate.argtypes = []

    lib.lua_close.restype = None
    lib.lua_close.argtypes = [c_void_p]

    lib.luaL_loadbuffer.restype = c_int
    lib.luaL_loadbuffer.argtypes = [c_void_p, c_char_p, c_size_t, c_char_p]

    lib.luaL_loadbufferx.restype = c_int
    lib.luaL_loadbufferx.argtypes = [c_void_p, c_char_p, c_size_t, c_char_p, c_char_p]

    lib.lua_pcall.restype = c_int
    lib.lua_pcall.argtypes = [c_void_p, c_int, c_int, c_int]

    lib.lua_gettop.restype = c_int
    lib.lua_gettop.argtypes = [c_void_p]

    lib.lua_settop.restype = None
    lib.lua_settop.argtypes = [c_void_p, c_int]

    lib.lua_pushnil.restype = None
    lib.lua_pushnil.argtypes = [c_void_p]

    lib.lua_type.restype = c_int
    lib.lua_type.argtypes = [c_void_p, c_int]

    lib.lua_typename.restype = c_char_p
    lib.lua_typename.argtypes = [c_void_p, c_int]

    lib.lua_next.restype = c_int
    lib.lua_next.argtypes = [c_void_p, c_int]

    lib.lua_tolstring.restype = c_void_p
    lib.lua_tolstring.argtypes = [c_void_p, c_int, p_size_t]

    lib.lua_tonumberx.restype = ctypes.c_double
    lib.lua_tonumberx.argtypes = [c_void_p, c_int, ctypes.POINTER(c_int)]

    lib.lua_toboolean.restype = c_int
    lib.lua_toboolean.argtypes = [c_void_p, c_int]

    return lib


def _absolute(lib, state, index):
    if index < 0:
        return lib.lua_gettop(state) + index + 1
    return index


def _to_python(lib, state, index, depth=0, budget=None):
    if budget is None:
        budget = [MAX_ENTRIES]
    absolute = _absolute(lib, state, index)
    tag = lib.lua_type(state, absolute)
    if tag == LUA_TNIL or tag == LUA_TNONE:
        return None
    if tag == LUA_TBOOLEAN:
        return bool(lib.lua_toboolean(state, absolute))
    if tag == LUA_TNUMBER:
        is_number = ctypes.c_int(0)
        value = lib.lua_tonumberx(state, absolute, ctypes.byref(is_number))
        if value == int(value) and abs(value) < 2 ** 53:
            return int(value)
        return value
    if tag == LUA_TSTRING:
        size = ctypes.c_size_t(0)
        pointer = lib.lua_tolstring(state, absolute, ctypes.byref(size))
        return ctypes.string_at(pointer, size.value).decode("utf-8", "replace")
    if tag == LUA_TTABLE:
        if depth >= MAX_DEPTH:
            return "<table: depth limit>"
        out = {}
        lib.lua_pushnil(state)
        while lib.lua_next(state, absolute) != 0:
            budget[0] -= 1
            if budget[0] <= 0:
                lib.lua_settop(state, -2)
                out["<truncated>"] = True
                break
            key = _to_python(lib, state, -2, depth + 1, budget)
            value = _to_python(lib, state, -1, depth + 1, budget)
            out[key if isinstance(key, str) else _plain_key(key)] = value
            lib.lua_settop(state, -2)  # pop the value, keep the key for lua_next
        return out
    return "<{0}>".format((lib.lua_typename(state, tag) or b"?").decode("utf-8", "replace"))


def _plain_key(key):
    if key is None:
        return "nil"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float) and key.is_integer():
        return str(int(key))
    return str(key)


def evaluate(framework, source, name="<chunk>", mode="run"):
    """The child-side work. Returns the dict the parent parses."""
    library = ctypes.CDLL(framework)
    _bind(library)
    state = library.luaL_newstate()
    if not state:
        return {"ok": False, "error": "luaL_newstate returned NULL"}
    try:
        blob = source.encode("utf-8")
        status = library.luaL_loadbuffer(state, blob, len(blob), name.encode("utf-8"))
        if status != LUA_OK:
            return {"ok": False, "phase": "load", "error": _error_text(library, state)}
        if mode == "load":
            return {"ok": True, "phase": "load", "value": None}
        status = library.lua_pcall(state, 0, 1, 0)
        if status != LUA_OK:
            return {"ok": False, "phase": "run", "error": _error_text(library, state)}
        value = _to_python(library, state, -1)
        return {"ok": True, "phase": "run", "value": value}
    finally:
        library.lua_close(state)


def _error_text(library, state):
    size = ctypes.c_size_t(0)
    pointer = library.lua_tolstring(state, -1, ctypes.byref(size))
    if not pointer:
        return "unknown Lua error"
    return ctypes.string_at(pointer, size.value).decode("utf-8", "replace")


def _main(argv=None):
    """Child entry point: read a JSON job on stdin, print a JSON result on stdout."""
    try:
        job = json.loads(sys.stdin.read())
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": "bad job: {0}".format(exc)}))
        return 2
    try:
        result = evaluate(
            job["framework"], job.get("source", ""), job.get("name", "<chunk>"), job.get("mode", "run")
        )
    except Exception as exc:  # pragma: no cover - defensive
        result = {"ok": False, "error": "{0}: {1}".format(type(exc).__name__, exc)}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
