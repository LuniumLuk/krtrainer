"""Is Tk usable here — asked *without* crashing anything (§7.4, F16's precondition).

The problem this module exists for
-----------------------------------
`import tkinter` succeeding proves nothing. On this machine the interpreter's Tk 8.5 is
built against a macOS SDK the OS refuses, and the refusal is an `abort()` inside Tk's C
code:

    macOS 15 (1507) or later required, have instead 15 (1506) !

`abort()` is **not** a catchable Python exception, and macOS answers it with a "Python
quit unexpectedly" problem-report dialog. So there are two ways to get this wrong, and
this module is written to avoid both:

1. Calling `tkinter.Tk()` in-process kills the user's run. (Wrong.)
2. Probing it in a *subprocess* still aborts — a child this time — and macOS still shows
   the dialog, once per check. A `doctor` run that pops a crash report is worse than no
   check at all. (Also wrong, and the bug this file now fixes.)

So the verdict is **static first**. The known-bad combination is recognised from version
numbers that cost nothing to read, and only a *plausible* Tk is ever handed to a
subprocess. On a machine where Tk is broken, running `krcheat` never spawns a Tk process
at all.

The escape hatch is `KRCHEAT_ALLOW_BROKEN_TK=1`, for the unlikely case of a Tk 8.5 that
works anyway.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from typing import Dict, Optional

from krcheat.core.errors import UsageError

#: Bypass the check. Documented as "may abort the process", because it may.
ALLOW_ENV = "KRCHEAT_ALLOW_BROKEN_TK"

#: Below this, on macOS, Tk predates the OS's expectations and aborts during init. The
#: observed message is quoted in REASON_OLD_TK so the diagnosis is recognisable.
MIN_SAFE_TK = 8.6

REASON_OLD_TK = (
    "Tk {tk} is too old for macOS {macos}: starting it aborts the process "
    '("macOS 15 (1507) or later required, have instead 15 (1506) !"). '
    "This interpreter's Tk was built against an older SDK."
)

REMEDY = (
    "Install an interpreter built against Tk 8.6 or newer (a python.org installer, or "
    "'brew install python-tk'), or keep using the CLI — every operation is available there."
)

PROBE_SOURCE = (
    "import json, tkinter\n"
    "root = tkinter.Tk()\n"
    "root.withdraw()\n"
    "root.destroy()\n"
    "print(json.dumps({'ok': True, 'tk': tkinter.TkVersion}))\n"
)


# ---------------------------------------------------------------------------
# Static checks (no Tk process, cannot crash)
# ---------------------------------------------------------------------------


def import_status():
    """(`import tkinter` works, Tk version or None, error or None). Import only."""
    try:
        import tkinter  # noqa: F401

        return True, getattr(tkinter, "TkVersion", None), None
    except Exception as exc:
        return False, None, "{0}: {1}".format(type(exc).__name__, exc)


def framework_build():
    """A non-framework Python produces Tk windows that open behind the terminal (§7.4)."""
    import sysconfig

    return bool(sysconfig.get_config_var("PYTHONFRAMEWORK")) and sys.executable


def static_verdict():
    """A verdict from version numbers alone.

    `certain=True` means "do not attempt Tk, in this process or any other": the failure
    mode we know about is a hard abort, and an abort is not worth a dialog.
    """
    imported, version, error = import_status()
    macos = platform.mac_ver()[0] if platform.system() == "Darwin" else None
    if not imported:
        return {
            "usable": False,
            "certain": True,
            "source": "import",
            "tk_version": None,
            "macos": macos,
            "reason": "tkinter is not importable in this interpreter ({0})".format(error),
        }
    if macos and version is not None and float(version) < MIN_SAFE_TK:
        return {
            "usable": False,
            "certain": True,
            "source": "static",
            "tk_version": str(version),
            "macos": macos,
            "reason": REASON_OLD_TK.format(tk=version, macos=macos),
        }
    return {
        "usable": True,
        "certain": False,
        "source": "static",
        "tk_version": str(version) if version is not None else None,
        "macos": macos,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# The probe (only ever reached when the static verdict says it is safe)
# ---------------------------------------------------------------------------


def probe(timeout=15.0):
    """Open and close a Tk window in a subprocess. Call only when it is plausible.

    Returns `{ok, tk, error, returncode, skipped, source}`. Never raises.
    """
    imported, version, error = import_status()
    if not imported:
        return {"ok": False, "tk": None, "error": error, "skipped": True, "source": "import"}
    try:
        completed = subprocess.run(
            [sys.executable, "-c", PROBE_SOURCE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "tk": version,
            "source": "probe",
            "error": "Tk did not open a window within {0}s".format(timeout),
        }
    except Exception as exc:  # pragma: no cover - exotic
        return {
            "ok": False,
            "tk": version,
            "source": "probe",
            "error": "probe could not start: {0}".format(exc),
        }
    if completed.returncode != 0:
        detail = (
            completed.stderr.decode("utf-8", "replace").strip().splitlines()
            or completed.stdout.decode("utf-8", "replace").strip().splitlines()
        )
        tail = detail[0] if detail else ""
        return {
            "ok": False,
            "tk": version,
            "source": "probe",
            "returncode": completed.returncode,
            "error": "Tk failed to start (rc={0}){1}".format(
                completed.returncode, ": " + tail if tail else ""
            ),
        }
    try:
        payload = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = {"ok": True, "tk": version}
    payload.setdefault("error", None)
    payload["source"] = "probe"
    return payload


def verdict(allow_probe=True):
    """The decision, cheapest first: static checks, then a probe only if plausible."""
    static = static_verdict()
    if static["certain"] or not allow_probe:
        return static
    result = probe()
    return {
        "usable": bool(result.get("ok")),
        "certain": False,
        "source": "probe",
        "tk_version": static.get("tk_version"),
        "macos": static.get("macos"),
        "reason": None if result.get("ok") else result.get("error"),
        "probe": result,
    }


def describe(result=None):
    """One sentence for `doctor`, plus the remedy when the verdict is negative."""
    result = verdict() if result is None else result
    if result.get("usable"):
        if result.get("source") == "static":
            return "Tk {0} passes the static check (not opened)".format(result.get("tk_version"))
        return "Tk {0} opened a window successfully".format(result.get("tk_version"))
    return "{0} {1}".format(result.get("reason") or "Tk is not usable.", REMEDY)


def require():
    """Refuse to start the GUI when Tk cannot work, instead of aborting the process."""
    if os.environ.get(ALLOW_ENV):
        return None
    result = verdict()
    if not result.get("usable"):
        raise UsageError(
            "{0} The GUI is optional: every operation is available from the CLI. "
            "Set {1}=1 to try anyway (it may abort the process).".format(
                result.get("reason") or "Tk is not usable here.", ALLOW_ENV
            )
        )
    return result


def summary():
    """Everything `doctor` reports about Tk, in one dict. Never starts a Tk process
    when the static verdict is negative."""
    imported, version, error = import_status()
    result = {
        "available": imported,
        "tk_version": str(version) if version is not None else None,
        "framework_build": bool(framework_build()),
        "import_error": error,
        "platform": platform.system(),
        "macos": platform.mac_ver()[0] if platform.system() == "Darwin" else None,
    }
    if not imported:
        result.update({"usable": False, "certain": True, "source": "import",
                       "reason": "tkinter is not importable ({0})".format(error)})
        return result
    static = static_verdict()
    result.update(static if static["certain"] else verdict())
    return result
