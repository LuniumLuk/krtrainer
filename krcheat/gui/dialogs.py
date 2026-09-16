"""Modal dialogs: the confirm-before-write diff, and the Steam warning (§9.5).

`confirm_write` is the important one. It renders the **`--dry-run` result** of the very
operation the user pressed Apply for, so what the dialog promises and what the operation
does cannot drift: they are the same computation, and the CLI would have printed the same
lines.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from krcheat.core.result import Result


class Dialog(tk.Toplevel):
    """A modal dialog returning `None` when dismissed."""

    def __init__(self, parent, title, width=640, height=420):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.result = None
        self.transient(parent)
        self.geometry("{0}x{1}".format(width, height))
        self.minsize(420, 240)
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def finish(self):
        self.grab_set()
        self.wait_window(self)
        return self.result

    def _cancel(self):
        self.result = None
        self.destroy()


def _scroll_text(parent, lines: List[str], height=14):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, height=height, wrap="none", font=("Menlo", 11))
    scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    text.insert("1.0", "\n".join(lines))
    text.configure(state="disabled")
    return text


def confirm_write(parent, dry_run: Result, title="Confirm changes"):
    """Show the diff and return True when the user agrees.

    The caller then submits the *real* operation to the worker thread; this dialog never
    runs it, so the UI stays responsive and every write goes through the same worker.
    """
    changes = [change.render() for change in dry_run.effective_changes]
    dialog = Dialog(parent, title, height=460)
    header = "This will change {0} value(s).".format(len(changes))
    ttk.Label(dialog.body, text=header, font=("Helvetica", 12, "bold")).pack(anchor="w")
    ttk.Label(
        dialog.body,
        text="A snapshot is taken before the first write, and the file is re-read and verified "
        "afterwards.",
        wraplength=580,
        justify="left",
    ).pack(anchor="w", pady=(2, 8))
    lines = changes or ["(no changes)"]
    if dry_run.notes:
        lines.append("")
        lines.extend(dry_run.notes)
    if dry_run.warnings:
        lines.append("")
        lines.extend("warning: {0}".format(item) for item in dry_run.warnings)
    _scroll_text(dialog.body, lines)

    buttons = ttk.Frame(dialog.body)
    buttons.pack(fill="x", pady=(10, 0))

    def cancel():
        dialog.result = False
        dialog.destroy()

    def apply():
        dialog.result = True
        dialog.destroy()

    ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right")
    ttk.Button(buttons, text="Apply", command=apply).pack(side="right", padx=(0, 8))
    ttk.Label(buttons, text="dry-run diff shown above", foreground="#666").pack(side="left")
    return bool(dialog.finish())


def warn_steam(parent, message):
    """The Steam warning, as a modal rather than a line of text (§15.3)."""
    dialog = Dialog(parent, "Steam is running", width=520, height=260)
    ttk.Label(
        dialog.body,
        text="Steam Cloud sync hazard",
        font=("Helvetica", 12, "bold"),
    ).pack(anchor="w")
    ttk.Label(dialog.body, text=message, wraplength=470, justify="left").pack(anchor="w", pady=(6, 10))
    buttons = ttk.Frame(dialog.body)
    buttons.pack(fill="x")

    def cancel():
        dialog.result = False
        dialog.destroy()

    def proceed():
        dialog.result = True
        dialog.destroy()

    ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right")
    ttk.Button(buttons, text="Proceed anyway", command=proceed).pack(side="right", padx=(0, 8))
    return bool(dialog.finish())


def info(parent, title, lines):
    dialog = Dialog(parent, title, width=560, height=380)
    _scroll_text(dialog.body, list(lines))
    ttk.Button(dialog.body, text="Close", command=dialog._cancel).pack(anchor="e", pady=(10, 0))
    return dialog.finish()
