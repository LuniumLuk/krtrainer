"""The safety model, in one place (§15, decision D6).

Two things live here, because both `profile.py` (tier 1) and `data.py` (F15) need
them and neither should own them:

* `check_gates` — the preconditions of §15.3, evaluated *before* anything is touched.
* `write_path`  — the five steps of §15.2, which no mutating command may bypass.

Scope is file-level on purpose (§15.4): snapshot per command run, validate, atomic
swap, verify after swap, byte-identical restore. Not per-operation backups, no change
journal, no bundle rollback.

The order matters and is not negotiable: snapshot before the first write, and abort if
the snapshot fails. Snapshots 3 and 5 in the threat model — our own serializer being
rejected by the game (which *deletes the slot*), and the original being lost — are the
only two that can destroy data irretrievably, and both are handled before a single byte
of the original is touched.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from krcheat.core import backup, lua_table as lt, paths
from krcheat.core.errors import (
    BackupError,
    ChannelUnavailable,
    NotFoundError,
    UsageError,
    ValidationError,
)

PASS = "pass"
WARN = "warn"
FAIL = "fail"


class Gate(object):
    """One precondition result, for `doctor` and for the log."""

    def __init__(self, name, status, detail, **extra):
        self.name = name
        self.status = status
        self.detail = detail
        self.extra = extra

    def to_dict(self):
        out = {"check": self.name, "status": self.status, "detail": self.detail}
        out.update(self.extra)
        return out

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Gate({0}, {1})".format(self.name, self.status)


# ---------------------------------------------------------------------------
# §15.3 Preconditions
# ---------------------------------------------------------------------------


def check_gates(ctx, bundle=None, save_dir=None, version_string=None, target_path=None):
    """Evaluate the preconditions. Returns warnings; raises on a refusal.

    The asymmetry between the first two rows is deliberate: editing while the game is
    running is *always* wrong (it either gets clobbered or resurrects stale state),
    while Steam running is usually fine — it sees our newer local file on the next
    launch and uploads it, which is the outcome we want.
    """
    warnings = []
    if save_dir is None:
        save_dir = ctx.save_dir()

    if not save_dir.writable():
        raise ValidationError(
            "the save directory is not writable: {0}".format(save_dir.path), path=save_dir.path
        )

    running = paths.find_game_process(bundle)
    if running is not None:
        if not ctx.force:
            raise ChannelUnavailable(
                "the game is running (pid {0}). It rewrites the slot when it saves, so an "
                "edit now would be clobbered or would resurrect stale state. Close the game, "
                "or pass --force.".format(running["pid"]),
                pid=running["pid"],
            )
        warnings.append(
            "the game is running (pid {0}) and --force was given; the edit may be "
            "clobbered when the game next saves".format(running["pid"])
        )
        ctx.log_warn("gate.game_running_forced", pid=running["pid"])

    steam = paths.find_steam_process()
    if steam is not None:
        require_yes = ctx.config.get_typed("safety.require_yes_when_steam_running")
        message = (
            "Steam is running (pid {0}). Steam Cloud syncs slot files for this title: your "
            "edit will be uploaded on the next launch, and a cloud restore can overwrite it. "
            "Closing Steam first is recommended.".format(steam["pid"])
        )
        if require_yes and not ctx.assume_yes:
            raise UsageError(message + " Pass --yes to proceed.", pid=steam["pid"])
        warnings.append(message)
        ctx.log_warn("gate.steam_running", pid=steam["pid"], overridden=True)

    if version_string:
        expected = None
        if bundle is not None:
            expected = bundle.expected_version_string()
        if expected and expected != version_string:
            if not ctx.force:
                raise ValidationError(
                    "version mismatch: the save says {0!r} but the installed game is {1!r}. "
                    "The schema this build validates against is not the schema on disk. "
                    "Pass --force to proceed anyway.".format(version_string, expected),
                    found=version_string,
                    expected=expected,
                )
            warnings.append(
                "version mismatch ({0!r} vs {1!r}) overridden by --force".format(
                    version_string, expected
                )
            )
            ctx.log_warn("gate.version_mismatch_forced", found=version_string, expected=expected)

    if target_path and not os.path.exists(target_path):
        raise NotFoundError("file not found: {0}".format(target_path), path=target_path)
    return warnings


# ---------------------------------------------------------------------------
# §15.2 The write path
# ---------------------------------------------------------------------------


def validate_text(text, intended, original, name="<edited>"):
    """Step 2 groundwork: re-parse what we are about to write and check it.

    Three questions, in order: is it still valid Lua-table text (we re-parse it, not
    trust the renderer), does it still say what we meant (structural comparison), and
    did we lose anything (no key present before may be absent after).
    """
    try:
        reparsed = lt.parse(text)
    except lt.LuaTableError as exc:
        raise ValidationError(
            "the text we were about to write does not parse: {0}".format(exc), stage="reparse"
        )
    actual = reparsed.python()

    diff = lt.structural_diff(intended, actual)
    if diff:
        raise ValidationError(
            "the rendered text does not match the intended structure: {0}".format(
                "; ".join(diff[:5])
            ),
            stage="structural",
            differences=diff[:20],
        )

    if original is not None:
        lost = sorted(lt.flatten_paths(original) - lt.flatten_paths(actual))
        if lost:
            raise ValidationError(
                "the edit would remove {0} key(s), starting with {1}. Keys are never "
                "deleted: the game deletes the whole slot when mandatory data is "
                "missing.".format(len(lost), ", ".join(lost[:5])),
                stage="no-deletion",
                removed=lost[:20],
            )
        for key in lt.MANDATORY_TOP_LEVEL:
            if key in original and key not in actual:
                raise ValidationError(
                    "mandatory key {0!r} would be removed".format(key), stage="mandatory"
                )
    return actual


def oracle_check(ctx, text, name, run=True, warnings=None):
    """Step 2: load the text in the game's own VM (S8, D4).

    Skipped — not failed — when the framework is unavailable: the oracle enhances the
    write path, it is not its only defence (§18).
    """
    from krcheat.core import oracle as oracle_mod

    if not ctx.oracle or not oracle_mod.available(ctx._bundle if ctx._bundle else None):
        ctx.log_debug("oracle.skipped", reason="disabled or unavailable", name=name)
        return {"ok": False, "skipped": True}
    result = oracle_mod.check(text, name=name, mode="run" if run else "load")
    ctx.log_debug(
        "oracle.result",
        name=name,
        mode=result.get("mode"),
        ok=result.get("ok"),
        phase=result.get("phase"),
        error=result.get("error"),
        skipped=result.get("skipped"),
    )
    if result.get("ok"):
        return result
    if result.get("skipped"):
        return result
    if run and result.get("phase") == "run":
        # The chunk compiled but refused to execute. For generated Lua that calls into
        # LÖVE this is expected, so it is reported rather than treated as fatal.
        if warnings is not None:
            warnings.append("oracle could not execute {0}: {1}".format(name, result.get("error")))
        return result
    raise ValidationError(
        "the game's own LuaJIT rejected the text we were about to write: {0}".format(
            result.get("error")
        ),
        stage="oracle",
        oracle=result,
    )


def write_path(
    ctx,
    result,
    target,
    text,
    original_text,
    intended,
    original,
    label,
    version_string=None,
    extra_snapshot_files=None,
):
    """Steps 1-4 of §15.2 for one file. Returns the snapshot manifest (or None).

    `result` is the `Result` being built; warnings and notes are appended to it.
    """
    ctx.log_debug(
        "write.begin",
        target=target,
        label=label,
        bytes_before=len(original_text) if original_text else None,
        bytes_after=len(text),
        sha256_before=paths.sha256_file(target) if os.path.exists(target) else None,
    )

    # -- step 2: validate before swap ---------------------------------------
    validate_text(text, intended, original, name=os.path.basename(target))
    if ctx.oracle:
        oracle_check(ctx, text, os.path.basename(target), run=True, warnings=result.warnings)
    intent_hash = _hash_text(text)
    ctx.log_debug("write.validated", target=target, sha256_intent=intent_hash)

    # -- --dry-run stops here, without snapshotting (§15.2) ------------------
    if ctx.dry_run:
        result.dry_run = True
        result.note("dry run: nothing was written and no snapshot was taken")
        ctx.log_info("write.dry_run", target=target, changes=len(result.effective_changes))
        return None

    # -- step 1: snapshot before the first write ----------------------------
    files = [target] + list(extra_snapshot_files or [])
    manifest = backup.snapshot(
        files,
        label=label,
        command=" ".join(ctx.argv) if ctx.argv else ctx.command,
        version_string=version_string,
    )
    result.snapshot = manifest["id"]
    ctx.state.record_snapshot(manifest["id"])
    ctx.log_info(
        "write.snapshot",
        snapshot=manifest["id"],
        files=[entry["name"] for entry in manifest["files"]],
        sha256=[entry["sha256"] for entry in manifest["files"]],
    )

    # -- step 3: atomic swap -------------------------------------------------
    atomic_write(target, text)
    ctx.log_info(
        "write.swapped",
        target=target,
        bytes=len(text),
        sha256=paths.sha256_file(target),
    )

    # -- step 4: verify after swap ------------------------------------------
    try:
        with open(target, "r", encoding="utf-8") as handle:
            on_disk = handle.read()
        actual = lt.parse(on_disk).python()
    except (OSError, lt.LuaTableError) as exc:
        raise ValidationError(
            "the file we just wrote could not be read back: {0}. Restore it with "
            "'krcheat backup restore {1}'.".format(exc, manifest["id"]),
            stage="post-write",
            snapshot=manifest["id"],
        )
    diff = lt.structural_diff(intended, actual)
    if diff:
        # Deliberately *not* auto-restored: we cannot tell our own failure apart from a
        # third party writing between step 2 and step 3, and silently reverting someone
        # else's write would be worse than reporting it.
        raise ValidationError(
            "the file on disk does not match what we wrote ({0}). A snapshot exists at "
            "{1}: 'krcheat backup restore {1}' returns the original.".format(
                "; ".join(diff[:3]), manifest["id"]
            ),
            stage="post-write",
            snapshot=manifest["id"],
            differences=diff[:20],
        )
    if paths.sha256_file(target) != intent_hash:
        result.warn("the written file's hash differs from the text we rendered (newline translation?)")
        ctx.log_warn("write.hash_mismatch", target=target)
    ctx.log_info(
        "write.verified",
        target=target,
        bytes=len(on_disk),
        sha256=paths.sha256_file(target),
        changed_nodes=len(getattr(result, "effective_changes", [])),
    )
    return manifest


def atomic_write(target, text):
    """Write `target` via a temp file in the same directory and `os.replace` (§15.2 step 3).

    The original is never truncated in place: on APFS the rename is atomic, so a
    power loss cannot leave a half-written save.
    """
    directory = os.path.dirname(os.path.abspath(target))
    tmp = os.path.join(directory, os.path.basename(target) + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise BackupError("cannot write {0}: {1}".format(target, exc), path=target)


def _hash_text(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
