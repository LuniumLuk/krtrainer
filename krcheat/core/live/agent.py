"""Building and locating the injected agent (foundation §9.3, §13).

The agent is C, and the user has a compiler or they do not. Either way the CLI has to give
one of two answers, and never a third:

* here is the dylib to inject; it matches the sources in this build, or
* here is exactly why there is not one (no clang, no codesign, compile error text).

So the dylib is built **once per source revision**, into `~/.krcheat/agent/`, named after a
hash of the sources it came from. That has three consequences worth stating:

* two checkouts of this repo do not fight over one dylib — different sources, different
  names, no staleness;
* a package installed read-only still works, because the output never goes inside the
  package;
* a rebuilt source tree is picked up automatically, and a *stale* dylib is impossible
  rather than merely unlikely (the failure mode it prevents is a confusing one: old C
  answering new Python).

Universal by default (`-arch x86_64 -arch arm64`), because the game bundle is universal and
the dylib has to load into whichever slice the host runs. Ad-hoc signing (`codesign -s -`) is
required, not optional: an unsigned arm64 dylib is refused outright by dyld.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from typing import Dict

from krcheat.core import paths
from krcheat.core.errors import ChannelUnavailable

#: The translation unit, its header, and the file the build mirrors. Only the first two
#: affect the binary; the Makefile is hashed too so a change in flags is noticed here.
SOURCES = ("kr_agent.c", "kr_agent.h")
MAKEFILE = "Makefile"

#: What the Makefile compiles with, kept in step by hand. If these change, the Makefile
#: changes too, which changes the fingerprint and forces a rebuild. The install name is
#: omitted because it depends on the output path, which is derived rather than chosen.
CFLAGS = ("-O2", "-Wall", "-Wextra", "-Wno-unused-parameter", "-fPIC")
LINK_FLAGS = ("-dynamiclib", "-install_name", "<output>", "-undefined", "dynamic_lookup")

UNIVERSAL_ARCHS = ("-arch", "x86_64", "-arch", "arm64")


def source_dir():
    """Where `kr_agent.c` lives inside the installed package.

    `__file__` is `<package>/core/live/agent.py`, so the package root is three levels up and
    the sources sit in its `agent/` directory beside `core/`.
    """
    here = os.path.abspath(__file__)
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.path.join(package_root, "agent")


def build_dir():
    """`~/.krcheat/agent` — outside the package, so a read-only install still works."""
    return os.path.join(paths.home(), "agent")


def host_arch():
    machine = os.uname().machine
    return "arm64" if machine in ("arm64", "aarch64") else "x86_64"


def fingerprint(sources_dir=None):
    """A hash of everything that can change the built dylib."""
    directory = sources_dir or source_dir()
    digest = hashlib.sha256()
    for name in SOURCES + (MAKEFILE,):
        path = os.path.join(directory, name)
        digest.update(name.encode("utf-8"))
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            digest.update(b"<missing>")
    digest.update(" ".join(CFLAGS + LINK_FLAGS).encode("utf-8"))
    return digest.hexdigest()


def cached_path(sources_dir=None):
    """The dylib for the current sources, if it has already been built."""
    path = os.path.join(build_dir(), "kr_agent-{0}.dylib".format(fingerprint(sources_dir)[:16]))
    return path if os.path.exists(path) else None


def toolchain():
    """`{"clang": path or None, "codesign": path or None}`."""
    return {
        "clang": shutil.which("clang") or ("/usr/bin/clang" if os.path.exists("/usr/bin/clang") else None),
        "codesign": shutil.which("codesign") or ("/usr/bin/codesign" if os.path.exists("/usr/bin/codesign") else None),
    }


def available(sources_dir=None):
    """(usable, reason).

    `usable` answers "can the agent be brought up?" — sources present and a compiler to
    build them — not "is it built?". `reason` carries the nuance: an absent dylib is a
    build away and says so, which is the difference between a setup problem and a first run.

    Never raises: `doctor` and `live status` call this.
    """
    directory = sources_dir or source_dir()
    missing = [name for name in SOURCES if not os.path.exists(os.path.join(directory, name))]
    if missing:
        return False, "agent sources are missing from this installation: {0}".format(", ".join(missing))
    tools = toolchain()
    if not tools["clang"]:
        return False, "clang is required to build the agent once, and was not found"
    if cached_path(directory):
        return True, None
    return True, "the agent has not been built yet; the first live command will build it"


def _run(argv):
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:  # pragma: no cover - defensive
        return 127, str(exc)
    output = completed.stdout.decode("utf-8", "replace")
    return completed.returncode, output


def build(force=False, sources_dir=None, logger=None):
    """Compile the agent and return its path. Raises `ChannelUnavailable` on failure."""
    directory = sources_dir or source_dir()
    usable, reason = available(directory)
    if not usable:
        raise ChannelUnavailable(reason)

    tools = toolchain()
    if not tools["codesign"]:
        raise ChannelUnavailable(
            "codesign is required: an unsigned arm64 dylib is refused by dyld, so the agent "
            "must be ad-hoc signed even though it is not distributed"
        )

    if not force:
        existing = cached_path(directory)
        if existing:
            if logger is not None:
                logger.debug("agent.cached", path=existing)
            return existing

    out_dir = build_dir()
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, "kr_agent-{0}.dylib".format(fingerprint(directory)[:16]))
    tmp = final + ".tmp"

    # The link is done to a temporary file and renamed, so a half-written dylib is never
    # observable. That makes `-install_name` necessary rather than cosmetic: without it the
    # library records its own path as `...dylib.tmp`, which is what `otool -L` would then
    # report for the agent actually running in the game.
    link = [
        "-dynamiclib",
        "-install_name",
        final,
        "-undefined",
        "dynamic_lookup",
    ]

    def command(archs):
        return (
            [tools["clang"]]
            + list(CFLAGS)
            + list(archs)
            + ["-I", directory]
            + link
            + ["-o", tmp, os.path.join(directory, "kr_agent.c")]
        )

    # Universal first (the game is universal); fall back to the host slice if the SDK cannot
    # cross-compile, which is unusual but not impossible on an older toolchain.
    attempts = [list(UNIVERSAL_ARCHS), ["-arch", host_arch()]]
    last_output = ""
    for archs in attempts:
        code, output = _run(command(archs))
        last_output = output
        if code == 0:
            break
    else:
        _cleanup(tmp)
        raise ChannelUnavailable(
            "the agent failed to compile. This is a bug in krcheat rather than in your setup; "
            "the compiler said:\n{0}".format(last_output.strip() or "(no output)")
        )

    code, output = _run([tools["codesign"], "-s", "-", "--force", "--timestamp=none", tmp])
    if code != 0:
        _cleanup(tmp)
        raise ChannelUnavailable(
            "the agent built but could not be signed, so dyld will refuse to load it:\n{0}".format(
                output.strip()
            )
        )

    os.replace(tmp, final)
    if logger is not None:
        logger.info("agent.built", path=final, archs=" ".join(attempts[0]))
    return final


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass


def ensure(force=False, logger=None):
    """The dylib path, building it if this source revision has not been built yet."""
    return build(force=force, logger=logger)


def describe(logger=None) -> Dict[str, object]:
    """For `doctor` and `live status`. Reports state, never raises."""
    directory = source_dir()
    usable, reason = available(directory)
    try:
        path = cached_path(directory)
    except OSError:  # pragma: no cover - defensive
        path = None
    tools = toolchain()
    return {
        "source_dir": directory,
        "fingerprint": fingerprint(directory)[:16],
        "built": path,
        "usable": usable,
        "reason": reason,
        "toolchain": tools,
        "archs": " ".join(UNIVERSAL_ARCHS[1::2]),
        "build_dir": build_dir(),
    }


def parse_agent_status(text):
    """Turn the agent's one-line status into a dict (`kr_agent_status_text`).

    The format is `attached=yes frames=57 overrides=0 reentry_skips=0`; anything
    unparseable is returned as `{"raw": ...}` rather than raising, because this is
    diagnostics, not a contract.
    """
    out = {}
    for token in (text or "").split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key] = int(value) if value.isdigit() else value
    if not out:
        return {"raw": text}
    return out
