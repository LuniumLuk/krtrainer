"""Transport A — launch-time dylib injection (foundation §9.3).

    DYLD_INSERT_LIBRARIES=<path>/kr_agent-<hash>.dylib \\
    SteamAppId=246420 \\
    "<...>/Kingdom Rush.app/Contents/MacOS/love"

The bundle's entitlements allow this without root, without re-signing and without
disabling SIP: `allow-dyld-environment-variables` and `disable-library-validation` are
both set (§3.4). The agent interposes `luaL_newstate` to capture the `lua_State *` and
`SDL_GL_SwapWindow` to get a once-per-frame callback on the main thread, then answers the
file channel.

This is the preferred transport because it modifies nothing: the game installation stays
untouched, and the game can be launched with or without cheats.

**The one thing this transport cannot do** is attach to a game that is already running: the
dylib has to be in the environment at launch, and there is no way to add one retroactively
without a debugger (that is transport C). So `start()` distinguishes the cases and says which
one it is, instead of reporting a generic timeout:

* no game running, launch allowed  -> launch it with the agent, wait for the channel;
* a game running, no channel       -> refuse, and say to restart it with `krcheat play`;
* a game running, channel present  -> use it.
"""

from __future__ import annotations

import os
import subprocess
import time

from krcheat.core import paths
from krcheat.core.errors import ChannelUnavailable, NotFoundError, UsageError
from krcheat.core.live import agent as agent_mod
from krcheat.core.live.protocol import (
    MODE_ALWAYS,
    MODE_CLEAR,
    MODE_ONCE,
    MODE_STATUS,
    Channel,
    Request,
)
from krcheat.core.live.transport import LiveTransport

#: Environment the game must see to believe it was launched by Steam.
LAUNCH_ENV = {"SteamAppId": "246420", "SteamGameId": "246420"}

#: The variable the launcher sets.
DYLD_VARIABLE = "DYLD_INSERT_LIBRARIES"

#: How long to wait for the channel to appear after launching the game.
LAUNCH_TIMEOUT = 45.0


class DylibTransport(LiveTransport):
    name = "dylib"
    description = "transport A (launch-time dylib injection)"
    requires = "clang (to build the agent once) and a game launched by krcheat"

    def __init__(self, ctx=None, pid=None):
        LiveTransport.__init__(self, ctx=ctx, pid=pid)
        self._process = None
        self._request_seq = None

    # -- identity ------------------------------------------------------------

    @property
    def reason(self):
        usable, why = self.available()
        return why or "the agent is ready"

    # -- availability --------------------------------------------------------

    def available(self):
        """(usable, why). `why` is set even when usable, to explain a pending build."""
        usable, reason = agent_mod.available()
        if not usable:
            return False, reason
        try:
            self.bundle()
        except (NotFoundError, UsageError) as exc:
            return False, str(exc)
        return True, reason

    def bundle(self):
        if self.ctx is None:
            return paths.find_app_bundle()
        return self.ctx.bundle()

    def agent_path(self, force_build=False):
        return agent_mod.ensure(force=force_build, logger=getattr(self.ctx, "log", None))

    # -- launching -----------------------------------------------------------

    def launch_env(self, extra_env=None):
        """The environment `krcheat play` uses to start the game with the agent loaded."""
        env = dict(os.environ)
        env.update(LAUNCH_ENV)
        env[DYLD_VARIABLE] = self.agent_path()
        if extra_env:
            env.update(extra_env)
        return env

    def executable(self):
        return os.path.join(self.bundle().path, "Contents", "MacOS", "love")

    def launch(self, extra_env=None, wait=LAUNCH_TIMEOUT):
        """Start the game with the agent, then wait for its channel to appear."""
        executable = self.executable()
        if not os.path.exists(executable):
            raise NotFoundError("the game's launcher is missing: {0}".format(executable))
        env = self.launch_env(extra_env)
        self.log("agent.launch", executable=executable, dylib=env[DYLD_VARIABLE])
        self._process = subprocess.Popen([executable], env=env, cwd=os.path.dirname(executable))
        pid = self._process.pid
        if wait:
            self.wait_for_channel(pid, timeout=wait)
        return pid

    def wait_for_channel(self, pid, timeout=LAUNCH_TIMEOUT, poll=0.05):
        """Block until the agent has created its channel, or explain why it has not."""
        deadline = time.time() + float(timeout)
        while time.time() <= deadline:
            channel = Channel(pid)
            if os.path.exists(channel.log_path):
                # `log` is written by the agent's constructor, before any Lua state exists:
                # the earliest proof that the injection took, not merely that the process
                # started.
                self.pid = pid
                return channel
            time.sleep(poll)
        raise ChannelUnavailable(
            "the game started (pid {0}) but the agent never reported in, so the injection was "
            "refused or the agent crashed on load. Check `krcheat live status`.".format(pid)
        )

    # -- the transport interface ---------------------------------------------

    def start(self, launch=True, args=None):
        """Resolve a live channel: reuse a running game, or launch one."""
        self.agent_path()  # fail with a build error here rather than mid-launch
        current = paths.find_game_process(self._bundle_or_none())
        if current is not None:
            pid = current["pid"]
            channel = Channel(pid)
            if os.path.exists(channel.log_path):
                self.pid = pid
                self.log("agent.reuse", pid=pid)
                return pid
            raise ChannelUnavailable(
                "the game is already running (pid {0}) but without the agent. A dylib has to be "
                "in the environment at launch, so there is nothing to attach to: quit the game "
                "and start it with `krcheat play`.".format(pid)
            )
        if not launch:
            raise ChannelUnavailable(
                "the game is not running. Start it with `krcheat play` so the agent is loaded."
            )
        return self.launch()

    def _bundle_or_none(self):
        if self.ctx is not None:
            return self.ctx.bundle_or_none()
        try:
            return paths.find_app_bundle()
        except Exception:  # pragma: no cover - defensive
            return None

    def stop(self, quit_game=False):
        """Release every override, and optionally end a game we launched.

        Ending it takes two signals. SDL installs its own SIGINT/SIGTERM handlers and turns
        them into SDL_QUIT events, so the game survives a plain SIGTERM unless it happens to
        be polling events at that moment; SIGKILL is what actually stops it. Verified on this
        host rather than assumed, because "the command says it quit the game" is a promise
        worth keeping.
        """
        if self.channel() is not None:
            try:
                self.clear_all()
            except Exception:
                pass
        process = self._process
        if not quit_game or process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.log("agent.kill_failed", pid=process.pid)

    # -- channel -------------------------------------------------------------

    def channel(self):
        """The channel for the target process, or None if there is not one."""
        if self.pid is None and self.ctx is not None:
            self.pid = self.ctx.state.get("last_game_pid")
        if self.pid is None:
            return None
        channel = self.channel_for(self.pid)
        return channel if os.path.exists(channel.log_path) else None

    def require_channel(self):
        channel = self.channel()
        if channel is None:
            current = paths.find_game_process(self._bundle_or_none())
            if current is not None:
                raise ChannelUnavailable(
                    "the game is running (pid {0}) without the agent, so there is no channel. "
                    "Quit it and start it with `krcheat play`.".format(current["pid"])
                )
            raise ChannelUnavailable(
                "no live channel: the game is not running, or was not started by `krcheat play`."
            )
        return channel

    def request(self, code, mode=MODE_ONCE, key=None, timeout=None, capture=None, restore=None,
                heartbeat_seconds=None, touch=True):
        """One request/response round trip (§11.2)."""
        channel = self.require_channel()
        request = Request(
            code,
            mode=mode,
            key=key,
            request_id=self.next_request_id(),
            capture=capture,
            restore=restore,
            heartbeat_seconds=heartbeat_seconds,
        )
        if timeout is None:
            timeout = self.timeout()
        response = self.send(channel, request, timeout=timeout, touch=touch)
        if response is None:
            raise ChannelUnavailable(
                "the agent did not answer within {0:.1f}s. The game may be loading a level, "
                "since requests are only served between frames. The agent log is at {1}.".format(
                    timeout, channel.log_path
                )
            )
        return response

    def next_request_id(self):
        """A strictly increasing id.

        This is a correctness requirement, not bookkeeping. If two requests share an id, the
        response to the first is indistinguishable from the response to the second, and a
        caller reads a stale answer as its own — which is exactly what happened here: with a
        counter that fell back to 1, every `live` command answered with the previous
        command's result. The id is additionally persisted so a fresh process does not reuse
        ids that an earlier run left in `out.json`.
        """
        if self._request_seq is None:
            previous = 0
            if self.ctx is not None:
                try:
                    previous = int(self.ctx.state.get("live.last_request_id") or 0)
                except (TypeError, ValueError):
                    previous = 0
            self._request_seq = max(previous, 0)
        self._request_seq += 1
        if self.ctx is not None:
            try:
                self.ctx.state.put("live.last_request_id", self._request_seq)
            except Exception:  # pragma: no cover - the cache is not worth failing over
                pass
        return self._request_seq

    # -- convenience wrappers the CLI uses -----------------------------------

    def once(self, code, timeout=None, touch=True):
        return self.request(code, mode=MODE_ONCE, timeout=timeout, touch=touch)

    def always(self, key, code, capture=None, restore=None, timeout=None, heartbeat_seconds=None):
        """Register a per-frame override (§11.7.1-2).

        The capture is sent in the *same* request as the code, and the agent runs it first.
        Splitting them into two requests would leave a window in which the code has been
        applied but nothing was captured, and `off` would then restore the forced value.
        """
        self.touch_heartbeat()
        return self.request(
            code,
            mode=MODE_ALWAYS,
            key=key,
            capture=capture,
            restore=restore,
            heartbeat_seconds=heartbeat_seconds or self.heartbeat_timeout(),
            timeout=timeout,
        )

    def clear(self, key, timeout=None):
        return self.request(None, mode=MODE_CLEAR, key=key, timeout=timeout)

    def clear_all(self, timeout=None):
        return self.request(None, mode=MODE_CLEAR, key="*", timeout=timeout)

    def status(self, timeout=None, touch=True):
        """The agent's own override table (§11.7.5)."""
        return self.request(None, mode=MODE_STATUS, timeout=timeout, touch=touch)

    def touch_heartbeat(self):
        channel = self.channel()
        if channel is not None:
            channel.touch_heartbeat()

    # -- diagnostics ---------------------------------------------------------

    def agent_log(self, lines=40):
        channel = self.channel()
        if channel is None:
            return []
        return channel.agent_log_tail(lines)

    def collect_log(self, destination_dir=None):
        """Copy the channel log to `~/.krcheat/logs/agent-<pid>.log` (§9.6).

        The channel lives under `$TMPDIR`, which the OS is free to reap; this copy is what
        makes a failed session diagnosable afterwards.
        """
        channel = self.channel()
        if channel is None or not os.path.exists(channel.log_path):
            return None
        destination_dir = destination_dir or paths.logs_dir()
        os.makedirs(destination_dir, exist_ok=True)
        destination = os.path.join(destination_dir, "agent-{0}.log".format(channel.pid))
        try:
            with open(channel.log_path, "rb") as source:
                blob = source.read(2 * 1024 * 1024)
            with open(destination, "wb") as handle:
                handle.write(blob)
        except OSError:
            return None
        return destination

    def describe(self):
        usable, reason = self.available()
        info = LiveTransport.describe(self)
        info["available"] = usable
        info["reason"] = reason
        info["agent"] = agent_mod.describe(getattr(self.ctx, "log", None))
        return info


__all__ = ["DylibTransport", "LAUNCH_ENV", "DYLD_VARIABLE", "LAUNCH_TIMEOUT"]

