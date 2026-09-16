"""Worker thread + queue marshalling (§9.5).

Tk is single-threaded and not thread-safe, so every core call — `doctor`, a profile
write, a channel request with its 2 s timeout — runs here and hands its result back
through a queue that the main thread polls with `root.after(50, …)`.

One worker rather than a pool, deliberately: operations mutate the same files and take
snapshots of the same paths, and serialising them removes a class of interleaving that
would be very hard to debug from a GUI.
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any, Callable, Optional

import tkinter as tk

POLL_MS = 50


class Worker(object):
    """Serialises tasks off the Tk main thread."""

    def __init__(self, root, poll_ms=POLL_MS):
        self.root = root
        self.poll_ms = poll_ms
        self._tasks = queue.Queue()
        self._results = queue.Queue()
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name="krcheat-gui-worker", daemon=True)
        self._thread.start()
        self._schedule()

    # -- public API ----------------------------------------------------------

    def submit(self, fn, on_done=None, on_error=None):
        """Queue `fn()`; call `on_done(result)` or `on_error(exception)` on the main thread."""
        self._tasks.put((fn, on_done, on_error))

    def stop(self):
        self._stopped = True
        self._tasks.put(None)

    # -- internals -----------------------------------------------------------

    def _loop(self):
        while not self._stopped:
            item = self._tasks.get()
            if item is None:
                break
            fn, on_done, on_error = item
            try:
                value = fn()
            except Exception as exc:  # the GUI must survive any operation failure
                self._results.put(("error", exc, on_error, traceback.format_exc()))
            else:
                self._results.put(("ok", value, on_done, None))

    def _schedule(self):
        if self._stopped:
            return
        self.root.after(self.poll_ms, self._drain)

    def _drain(self):
        while True:
            try:
                kind, value, callback, detail = self._results.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                if callback is not None:
                    callback(value)
                else:  # pragma: no cover - defensive
                    print(detail)
            elif callback is not None:
                callback(value)
        self._schedule()
