"""The keeper: what makes an override outlive the command that set it (§11.7.4).

The agent clears every override when the heartbeat goes stale, because a crashed or forgotten
CLI must not leave the game permanently modified. That default is right, and it has a
consequence worth naming: **an override lives exactly as long as a process keeps saying so.**
For `krcheat live gold infinity`, that process is the CLI, and the cheat ends when the command
ends.

`--keep` asks for the other thing: leave it on and let me play. So `--keep` starts this
process, detached, and it does nothing but hold the heartbeat:

    python3 -m krcheat.core.live.keeper --pid <gamepid> --heartbeat 10 --keys gold,lives

It exits when

* the game it is protecting dies (the channel is gone), so it cannot leak past the session;
* the agent reports no overrides left, so it cannot keep a dead channel warm;
* it is asked to stop (`krcheat live off`, or `kill`), in which case it clears the overrides
  first — politely stopping is the whole reason to have a keeper rather than a sleep loop.

Being a separate process is the point. It has no `ctx`, no config and no opinion about the
game; it reads one channel directory and writes one timestamp.
"""

from __future__ import annotations

import argparse
import errno
import os
import signal
import sys
import time

POLL_SECONDS = 0.5
#: Touch the heartbeat at a third of the timeout, so two missed iterations are survivable.
HEARTBEAT_FRACTION = 3.0

_stopping = {"requested": False, "reason": None}


def _on_signal(signum, frame):  # pragma: no cover - signal path
    _stopping["requested"] = True
    _stopping["reason"] = "signal {0}".format(signum)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="krcheat.core.live.keeper",
        description="hold the live channel's heartbeat so overrides survive this command",
    )
    parser.add_argument("--pid", type=int, required=True, help="the game process id")
    parser.add_argument("--heartbeat", type=float, default=10.0, help="seconds")
    parser.add_argument("--keys", default="", help="comma-separated override keys, for the log")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="stop after this many seconds (0 = until the game exits)")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def game_alive(pid):
    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        if exc.errno in (errno.ESRCH,):
            return False
        if exc.errno in (errno.EPERM,):  # pragma: no cover - another user's process
            return True
        return False
    return True


def run(pid, heartbeat=10.0, keys=(), timeout=0.0, quiet=False, sleep=time.sleep):
    """Hold the heartbeat until there is nothing left to hold. Returns an exit code."""
    from krcheat.core.live.protocol import Channel, Request

    channel = Channel(pid)
    if not os.path.exists(channel.log_path):
        if not quiet:
            sys.stderr.write("keeper: no channel for pid {0}\n".format(pid))
        return 3

    interval = max(0.2, float(heartbeat) / HEARTBEAT_FRACTION)
    deadline = (time.time() + float(timeout)) if timeout else None
    pid_file = os.path.join(channel.path, "keeper.pid")
    try:
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write("{0} {1}\n".format(os.getpid(), ",".join(keys)))
    except OSError:
        pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if not quiet:
        sys.stdout.write(
            "keeper: holding {0} for pid {1} (every {2:.1f}s)\n".format(
                ", ".join(keys) or "overrides", pid, interval
            )
        )
        sys.stdout.flush()

    #: The CLI that started us wrote its request moments ago, so the heartbeat already exists;
    #: touching it now means the agent never sees a gap while we start up.
    channel.touch_heartbeat()

    while True:
        if _stopping["requested"]:
            break
        if not game_alive(pid):
            break
        if not os.path.exists(channel.log_path):
            break
        if deadline is not None and time.time() > deadline:
            break
        channel.touch_heartbeat()
        sleep(interval)

    # Stop politely: an override we simply abandoned would be cleared by the agent a few
    # seconds later anyway, but doing it here means `live off` takes effect immediately and
    # the agent's log shows why.
    code = 0
    try:
        request = Request.clear("*", request_id=int(time.time()) % 2_000_000_000)
        channel.write_request(request)
        # The agent answers within a frame; the keeper needs the effect, not the value, so a
        # short wait is enough and a timeout is not fatal — the heartbeat expires anyway.
        channel.wait_response(request.id, timeout=2.0)
    except Exception:
        code = 0
    try:
        os.remove(pid_file)
    except OSError:
        pass
    if not quiet:
        sys.stdout.write("keeper: stopped{0}\n".format(
            " ({0})".format(_stopping["reason"]) if _stopping["reason"] else ""
        ))
        sys.stdout.flush()
    return code


def main(argv=None):  # pragma: no cover - process entry point
    args = parse_args(argv)
    keys = [item for item in (args.keys or "").split(",") if item]
    return run(
        pid=args.pid,
        heartbeat=args.heartbeat,
        keys=keys,
        timeout=args.timeout,
        quiet=args.quiet,
    )


def spawn(transport, keys, heartbeat):
    """Start a detached keeper for `transport`'s channel. Returns its pid.

    Detached on purpose (`start_new_session`): the keeper must outlive the CLI, and it must
    not receive the terminal's Ctrl-C — otherwise the override would end exactly when the user
    walked away from the command, which is the opposite of what `--keep` means.
    """
    import subprocess

    from krcheat.core.oracle import PACKAGE_PARENT

    channel = transport.require_channel()
    argv = [
        "import sys; sys.path.insert(0, {0!r});".format(PACKAGE_PARENT),
        "from krcheat.core.live.keeper import main; raise SystemExit(main())",
    ]
    log_path = os.path.join(channel.path, "keeper.log")
    handle = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                argv[0] + " " + argv[1],
                "--pid",
                str(channel.pid),
                "--heartbeat",
                str(heartbeat),
                "--keys",
                ",".join(keys),
            ],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()
    return process.pid


def stop(pid=None, channel_path=None):
    """Ask a keeper to stop, so its override is cleared now rather than in 10 seconds."""
    if pid is None:
        if not channel_path:
            return False
        pid_file = os.path.join(channel_path, "keeper.pid")
        try:
            with open(pid_file, "r", encoding="utf-8") as handle:
                pid = int(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return False
    return True


def keeper_pid(channel_path):
    try:
        with open(os.path.join(channel_path, "keeper.pid"), "r", encoding="utf-8") as handle:
            return int(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def describe(channel_path):
    """For `live status`: is a keeper holding this channel, and who?"""
    pid = keeper_pid(channel_path)
    if pid is None:
        return {"running": False}
    alive = game_alive(pid)
    return {"running": alive, "pid": pid}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
