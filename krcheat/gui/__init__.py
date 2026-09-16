"""tkinter front-end over `core/` (foundation §9.5, F16).

The GUI is a **renderer**, not a second implementation. It calls the same `fn(ctx, **args)
-> Result` operations the CLI calls, honours the same safety model with no exceptions, and
never shells out to `krcheat` — doing so would create a divergent path and lose the
structured `Result`.

Two consequences that are enforced here rather than documented:

* **Every long operation runs on a worker thread** and marshals back through a
  `queue.Queue` polled by `root.after`. Tk is single-threaded and not thread-safe, so no
  widget is ever touched from a worker.
* **The confirm dialog is the `--dry-run` diff.** The Apply button first runs the same
  operation with `dry_run=True`, shows what it would change, and only then runs it for
  real. That is why the dialog cannot promise something the CLI would refuse.

The GUI is optional, and on this machine it is also *unavailable*: `tkinter` imports, but
Tk 8.5 aborts the process when it tries to open a window (§7.4). `krcheat.gui.tkprobe`
detects that in a subprocess and refuses cleanly, so a broken Tk costs the user a message
rather than a crash. Tiers 1–3 have no dependency on Tk whatsoever.
"""

from __future__ import annotations


def run(ctx):
    """Start the GUI.

    The preflight runs **before** `krcheat.gui.app` is imported, because that module (and
    `dialogs.py` and `worker.py`) import `tkinter` at the top level. An interpreter with no
    `_tkinter` at all — pyenv builds frequently have none — would otherwise raise
    `ModuleNotFoundError` out of the import and surface as an internal error with a
    traceback, instead of the one sentence that explains it and names the remedy.
    """
    from krcheat.gui import tkprobe

    tkprobe.require()
    from krcheat.gui.app import run as _run

    return _run(ctx)


__all__ = ["run"]
