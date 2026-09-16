"""The main window: profile panel, live panel, log pane, menu (§9.5, F16).

Every button calls a `core/` operation — the same one the CLI calls — through the worker
thread, and renders the returned `Result`. There is no GUI-only operation, and the Apply
path is: run with `dry_run=True`, show that diff in the confirm dialog, then run it for
real. Safety parity with the CLI is therefore structural rather than a promise.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from krcheat import __version__
from krcheat.core import backup, log as log_mod, paths, profile as profile_mod
from krcheat.core import data as data_mod, doctor as doctor_mod
from krcheat.core.errors import KrcheatError, UsageError
from krcheat.core.result import Result
from krcheat.gui import dialogs
from krcheat.gui.worker import Worker


def run(ctx):
    """Start the GUI. Returns an exit code (0 on a clean close)."""
    # Preflight in a subprocess: a broken Tk aborts the whole process, and an abort in a
    # child is a return code while an abort here is the user's run (§7.4, tkprobe).
    from krcheat.gui import tkprobe

    tkprobe.require()
    try:
        window = MainWindow(ctx)
    except tk.TclError as exc:
        raise UsageError(
            "cannot open a Tk window: {0}. This usually means there is no display, or the "
            "interpreter is not a framework build (§7.4).".format(exc)
        )
    window.mainloop()
    return 0


class MainWindow(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = tk.Tk()
        self.root.title("krcheat {0} — Kingdom Rush trainer".format(__version__))
        self.root.geometry(ctx.config.get_typed("ui.window_geometry") or "1000x680")
        self.worker = Worker(self.root)
        self.profile = None
        self.dry_run = tk.BooleanVar(value=False)
        self.slot_choice = tk.StringVar()
        self.status = tk.StringVar(value="Ready. Loading…")
        self._build_menu()
        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh_all()

    # -- construction --------------------------------------------------------

    def _build_menu(self):
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="Open save directory", command=self._open_save_dir)
        file_menu.add_command(label="Open backups", command=self._open_backups)
        file_menu.add_command(label="Open log", command=self._open_log)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._close)
        menu.add_cascade(label="File", menu=file_menu)

        tools = tk.Menu(menu, tearoff=0)
        tools.add_command(label="Doctor", command=self.show_doctor)
        tools.add_command(label="Self-test", command=self.show_self_test)
        tools.add_command(label="List snapshots", command=self.show_backups)
        tools.add_command(label="Restore latest snapshot", command=self.restore_latest)
        tools.add_command(label="F15 level data", command=self.show_data)
        menu.add_cascade(label="Tools", menu=tools)

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(
            label="Tier 2 is not built in this build", command=self.show_live_help
        )
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)

    def _build_layout(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="Slot:").pack(side="left")
        self.slot_box = ttk.Combobox(top, textvariable=self.slot_choice, width=8, state="readonly")
        self.slot_box.pack(side="left", padx=(4, 8))
        ttk.Button(top, text="Load", command=self.load_profile).pack(side="left")
        ttk.Checkbutton(top, text="dry run", variable=self.dry_run).pack(side="left", padx=12)
        ttk.Button(top, text="Refresh", command=self.refresh_all).pack(side="left")
        ttk.Label(top, textvariable=self.status, foreground="#345").pack(side="right")

        notebooks = ttk.Notebook(outer)
        notebooks.pack(fill="both", expand=True, pady=(10, 0))
        self.notebooks = notebooks

        self.profile_tab = ttk.Frame(notebooks, padding=10)
        self.live_tab = ttk.Frame(notebooks, padding=10)
        self.log_tab = ttk.Frame(notebooks, padding=10)
        notebooks.add(self.profile_tab, text="Profile (tier 1)")
        notebooks.add(self.live_tab, text="Live (tier 2)")
        notebooks.add(self.log_tab, text="Log")

        self._build_profile_tab()
        self._build_live_tab()
        self._build_log_tab()

    def _build_profile_tab(self):
        form = ttk.LabelFrame(self.profile_tab, text="Values", padding=10)
        form.pack(fill="x")
        self.gems = tk.StringVar()
        self.stars = tk.IntVar(value=3)
        self.upgrade_value = tk.IntVar(value=5)
        self.hero_xp = tk.StringVar()

        ttk.Label(form, text="Gems").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.gems, width=12).grid(row=0, column=1, sticky="w")
        ttk.Button(form, text="Set gems", command=lambda: self.apply_gems()).grid(
            row=0, column=2, sticky="w", padx=6
        )

        ttk.Label(form, text="Upgrades (all categories)").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Spinbox(form, from_=0, to=5, textvariable=self.upgrade_value, width=5).grid(
            row=1, column=1, sticky="w"
        )
        ttk.Button(form, text="Max upgrades", command=lambda: self.apply_upgrades()).grid(
            row=1, column=2, sticky="w", padx=6
        )

        ttk.Label(form, text="Stars per level (all levels, all modes)").grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Spinbox(form, from_=0, to=3, textvariable=self.stars, width=5).grid(
            row=2, column=1, sticky="w"
        )
        ttk.Button(form, text="Give stars", command=lambda: self.apply_stars()).grid(
            row=2, column=2, sticky="w", padx=6
        )

        ttk.Label(form, text="Hero XP (all heroes)").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.hero_xp, width=12).grid(row=3, column=1, sticky="w")
        ttk.Button(form, text="Set XP", command=lambda: self.apply_hero_xp()).grid(
            row=3, column=2, sticky="w", padx=6
        )

        bulk = ttk.LabelFrame(self.profile_tab, text="Unlocks", padding=10)
        bulk.pack(fill="x", pady=(10, 0))
        ttk.Button(bulk, text="Unlock all achievements", command=lambda: self.apply_bulk("achievements")).pack(side="left")
        ttk.Button(bulk, text="Clear achievements", command=lambda: self.apply_bulk("achievements-none")).pack(side="left", padx=6)
        ttk.Button(bulk, text="Mark all seen", command=lambda: self.apply_bulk("seen")).pack(side="left")

        self.summary = tk.Text(self.profile_tab, height=16, wrap="none", font=("Menlo", 11))
        self.summary.pack(fill="both", expand=True, pady=(10, 0))
        self.summary.configure(state="disabled")

    def _build_live_tab(self):
        ttk.Label(
            self.live_tab,
            text=(
                "Tier 2 (live gold / lives / speed / god) needs the injected agent, which is "
                "milestone M3 and is not built in this build.\n\nThe controls are shown so the "
                "shape is visible, and are disabled for the same reason the CLI refuses: there "
                "is no channel to reach."
            ),
            wraplength=760,
            justify="left",
        ).pack(anchor="w")
        panel = ttk.LabelFrame(self.live_tab, text="Overrides", padding=10)
        panel.pack(fill="x", pady=10)
        self.live_vars = {}
        for index, (key, label) in enumerate(
            (("gold", "Gold (infinite)"), ("lives", "Lives (infinite)"), ("speed", "Game speed x2"), ("god", "God mode"))
        ):
            var = tk.BooleanVar(value=False)
            self.live_vars[key] = var
            ttk.Checkbutton(panel, text=label, variable=var, state="disabled").grid(
                row=index // 2, column=index % 2, sticky="w", padx=8, pady=4
            )
        self.live_status = tk.Text(self.live_tab, height=14, wrap="none", font=("Menlo", 11))
        self.live_status.pack(fill="both", expand=True)
        self.live_status.configure(state="disabled")

    def _build_log_tab(self):
        bar = ttk.Frame(self.log_tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh log", command=self.load_log).pack(side="left")
        ttk.Button(bar, text="Reveal in Finder", command=self._open_log).pack(side="left", padx=6)
        ttk.Label(bar, text="diagnostic log (JSONL, one record per line)").pack(side="left", padx=8)
        self.log_view = tk.Text(self.log_tab, wrap="none", font=("Menlo", 10))
        scroll = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=scroll.set)
        self.log_view.pack(side="left", fill="both", expand=True, pady=(8, 0))
        scroll.pack(side="right", fill="y", pady=(8, 0))
        self.load_log()

    # -- helpers -------------------------------------------------------------

    def _set_status(self, message):
        self.status.set(message)

    def _fail(self, exc):
        message = str(exc)
        self._set_status("failed: {0}".format(message.split("\n")[0][:80]))
        messagebox.showerror("krcheat", message, parent=self.root)

    def _slot_options(self):
        try:
            return [str(number) for number in self.ctx.save_dir().find_slots()]
        except KrcheatError:
            return []

    def _selected_slot(self):
        text = self.slot_choice.get().strip()
        if not text:
            raise UsageError("choose a slot first")
        number = int(text)
        self.ctx.remember_slot(number)
        return number

    def _write_text(self, widget, lines):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(lines))
        widget.configure(state="disabled")

    # -- operations (all on the worker thread) -------------------------------

    def refresh_all(self):
        self._set_status("refreshing…")
        options = self._slot_options()
        self.slot_box["values"] = options
        if options and not self.slot_choice.get():
            self.slot_choice.set(options[0])
        self.worker.submit(self._doctor_task, on_done=self._doctor_done, on_error=self._fail)
        self.load_log()

    def _doctor_task(self):
        return doctor_mod.run(self.ctx)

    def _doctor_done(self, result):
        lines = [
            "[{0:4}] {1}: {2}".format(
                str(item.get("status", "")).upper(), item.get("check"), item.get("detail")
            )
            for item in result.payload.get("checks", [])
        ]
        summary = result.payload.get("summary", {})
        lines.append("")
        lines.append(
            "{0} passed, {1} warning(s), {2} failed".format(
                summary.get("pass", 0), summary.get("warn", 0), summary.get("fail", 0)
            )
        )
        self._write_text(self.summary, lines)
        self._set_status("doctor: {0} passed, {1} failed".format(summary.get("pass", 0), summary.get("fail", 0)))
        self.load_profile()

    def load_profile(self):
        if not self.slot_choice.get():
            self._set_status("no slot selected")
            return
        self.worker.submit(self._load_task, on_done=self._load_done, on_error=self._fail)

    def _load_task(self):
        save_dir = self.ctx.save_dir()
        profile = profile_mod.Profile.load(save_dir, self._selected_slot(), logger=self.ctx.log)
        return profile

    def _load_done(self, profile):
        self.profile = profile
        summary = profile.summary()
        lines = [
            "slot {0} — {1}".format(summary["slot"], summary["path"]),
            "version_string : {0}".format(summary["version_string"]),
            "gems           : {0}".format(summary["gems"]),
            "difficulty     : {0}".format(summary["difficulty"]),
            "upgrades       : {0}".format(summary["upgrades"]),
            "levels         : {0} with data, {1} completed, {2} stars".format(
                summary["levels_total"], len(summary["levels_completed"]), summary["stars_total"]
            ),
            "heroes         : {0} in status, selected {1}".format(
                len(summary["heroes"]), summary["heroes_selected"]
            ),
            "achievements   : {0} of {1}".format(
                len(summary["achievements_unlocked"]), summary["achievements_total"]
            ),
            "counters       : {0}".format(summary["counters"]),
            "seen           : {0} of {1}".format(summary["seen_unlocked"], summary["seen_total"]),
        ]
        self._write_text(self.summary, lines)
        self.gems.set(str(summary.get("gems") or ""))
        self._set_status("loaded slot {0}".format(summary["slot"]))
        self.load_live_status()

    def load_live_status(self):
        from krcheat.core.live import transport as transport_mod

        lines = []
        try:
            bundle = self.ctx.bundle()
        except KrcheatError:
            bundle = None
        game = paths.find_game_process(bundle)
        lines.append("game: {0}".format("running (pid {1})".format(game["pid"]) if game else "not running"))
        lines.append("steam: {0}".format("running" if paths.is_steam_running() else "not running"))
        for item in transport_mod.describe_all(self.ctx):
            lines.append(
                "  {0:<8} {1}".format(
                    item.get("transport"),
                    "available" if item.get("available") else item.get("reason"),
                )
            )
        lines.append("")
        lines.append("overrides: none (tier 2 not built)")
        self._write_text(self.live_status, lines)

    def load_log(self):
        records, source = log_mod.tail_lines(count=250)
        lines = ["# {0}".format(source)]
        for record in records:
            skip = {"ts", "lvl", "run", "seq", "event", "cmd"}
            detail = " ".join(
                "{0}={1}".format(key, value)
                for key, value in record.items()
                if key not in skip
            )
            lines.append(
                "{0} {1:<5} {2} {3}".format(
                    str(record.get("ts", ""))[11:23], record.get("lvl", ""), record.get("event", ""), detail
                )
            )
        self._write_text(self.log_view, lines)

    # -- write operations ----------------------------------------------------

    def _apply(self, describe, mutate, label):
        """Run `mutate(profile, result)` twice: once dry, once for real.

        The first run is the dialog's diff and the second is the write, so the two cannot
        disagree — they are the same code with the same arguments.
        """
        if self.profile is None:
            self._fail(UsageError("load a profile first"))
            return

        def dry_task():
            save_dir = self.ctx.save_dir()
            profile = profile_mod.Profile.load(save_dir, self._selected_slot(), logger=self.ctx.log)
            result = Result(command="gui.apply")
            mutate(profile, result)
            profile.save(self.ctx, result, label=label)
            return result

        def on_dry(result):
            if not result.effective_changes:
                messagebox.showinfo(
                    "krcheat", "Nothing to change: {0}".format("; ".join(result.notes) or "already set"), parent=self.root
                )
                return
            if not dialogs.confirm_write(self.root, result, title="Confirm: " + describe):
                return
            if self.dry_run.get():
                self._set_status("dry run: nothing written")
                return
            self.worker.submit(lambda: self._real_task(mutate, label), on_done=on_real, on_error=self._fail)

        self.worker.submit(dry_task, on_done=on_dry, on_error=self._fail)

    def _real_task(self, mutate, label):
        save_dir = self.ctx.save_dir()
        profile = profile_mod.Profile.load(save_dir, self._selected_slot(), logger=self.ctx.log)
        result = Result(command="gui.apply")
        mutate(profile, result)
        profile.save(self.ctx, result, label=label)
        return result

    def _on_real(self, result):
        self._set_status(
            "applied {0} change(s), snapshot {1}".format(
                len(result.effective_changes), result.snapshot or "-"
            )
        )
        for warning in result.warnings:
            messagebox.showwarning("krcheat", warning, parent=self.root)
        if not result.effective_changes:
            for note in result.notes:
                messagebox.showinfo("krcheat", note, parent=self.root)
        self.load_profile()
        self.load_log()

    # -- individual actions --------------------------------------------------

    def apply_gems(self):
        try:
            value = int(self.gems.get())
        except ValueError:
            self._fail(UsageError("gems must be a number"))
            return
        self._apply("set gems", lambda profile, result: profile.set_gems(result, value), "gui-set-gems")

    def apply_upgrades(self):
        value = int(self.upgrade_value.get())
        self._apply(
            "set every upgrade to {0}".format(value),
            lambda profile, result: profile.set_upgrades(result, "all={0}".format(value)),
            "gui-set-upgrades",
        )

    def apply_stars(self):
        stars = int(self.stars.get())
        self._apply(
            "give {0} star(s) on every level".format(stars),
            lambda profile, result: profile.set_stars_all(result, ctx=self.ctx, stars=stars),
            "gui-set-stars",
        )

    def apply_hero_xp(self):
        try:
            value = int(self.hero_xp.get())
        except ValueError:
            self._fail(UsageError("hero xp must be a number"))
            return

        def mutate(profile, result):
            for hero in list(profile.doc.keys_at("heroes.status")):
                profile.set_hero_xp(result, hero, value)

        self._apply("set hero xp to {0}".format(value), mutate, "gui-set-hero-xp")

    def apply_bulk(self, what):
        if what == "achievements":
            self._apply(
                "unlock every achievement",
                lambda profile, result: profile.set_achievements(result, "all", ctx=self.ctx),
                "gui-set-achievements",
            )
        elif what == "achievements-none":
            self._apply(
                "clear every achievement",
                lambda profile, result: profile.set_achievements(result, "none", ctx=self.ctx),
                "gui-set-achievements",
            )
        else:
            self._apply(
                "mark every seen entry",
                lambda profile, result: profile.set_seen_all(result, ctx=self.ctx),
                "gui-set-seen",
            )

    # -- menu actions --------------------------------------------------------

    def show_doctor(self):
        self.refresh_all()
        self.notebooks.select(0)

    def show_self_test(self):
        from krcheat.core import selftest as selftest_mod

        def done(result):
            lines = [
                "[{0:4}] {1}: {2}".format(
                    str(item.get("status", "")).upper(), item.get("check"), item.get("detail")
                )
                for item in result.payload.get("checks", [])
            ]
            dialogs.info(self.root, "Self-test", lines)

        self.worker.submit(lambda: selftest_mod.run(self.ctx), on_done=done, on_error=self._fail)

    def show_backups(self):
        def done(snapshots):
            lines = []
            for item in snapshots:
                lines.append(
                    "{0}  {1}  {2} file(s)  {3}".format(
                        item.get("id"), item.get("created"), len(item.get("files", [])), item.get("command")
                    )
                )
            dialogs.info(self.root, "Snapshots", lines or ["no snapshots yet"])

        self.worker.submit(lambda: backup.list_snapshots(include_incomplete=True), on_done=done, on_error=self._fail)

    def restore_latest(self):
        snapshots = backup.list_snapshots()
        if not snapshots:
            messagebox.showinfo("krcheat", "no snapshots yet", parent=self.root)
            return
        newest = snapshots[0]
        if not messagebox.askyesno(
            "krcheat",
            "Restore {0} ({1})?\n\nThis overwrites the file(s) it contains with their "
            "snapshot contents, verified by hash.".format(newest.get("id"), newest.get("created")),
            parent=self.root,
        ):
            return

        def done(payload):
            self._set_status("restored {0}".format(payload["snapshot"].get("id")))
            self.load_profile()

        self.worker.submit(
            lambda: backup.restore(newest["id"]), on_done=done, on_error=self._fail
        )

    def show_data(self):
        def done(payload):
            lines = ["save directory: {0}".format(payload.get("save_dir")), ""]
            installed = payload.get("installed", [])
            if not installed:
                lines.append("no F15 overrides installed")
            for item in installed:
                lines.append(
                    "level {0}  {1}".format(item.get("level"), item.get("module"))
                )
                for key, value in sorted((item.get("fields") or {}).items()):
                    lines.append("    {0} = {1}".format(key, value))
            lines.append("")
            lines.append("F15 requires spike S2; set overrides with 'krcheat data set'.")
            dialogs.info(self.root, "F15 level data", lines)

        self.worker.submit(lambda: data_mod.list_overrides(self.ctx), on_done=done, on_error=self._fail)

    def show_live_help(self):
        dialogs.info(
            self.root,
            "Live channel",
            [
                "Tier 2 is milestone M3 and is not built in this build.",
                "",
                "The protocol, the channel mechanics and the snippet library exist in core/live/;",
                "what is missing is the native agent that answers on the other side.",
                "",
                "Run 'krcheat live status' in a terminal for the machine-readable answer.",
            ],
        )

    def _open_save_dir(self):
        try:
            subprocess.Popen(["open", self.ctx.save_dir().path])
        except KrcheatError as exc:
            self._fail(exc)

    def _open_backups(self):
        paths.ensure_home()
        subprocess.Popen(["open", paths.backups_dir()])

    def _open_log(self):
        subprocess.Popen(["open", "-R", log_mod.active_log_path()])

    # -- lifecycle -----------------------------------------------------------

    def _close(self):
        try:
            self.ctx.config.set("ui.window_geometry", self.root.winfo_geometry())
        except Exception:
            pass
        self.worker.stop()
        self.ctx.log.close()
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()
