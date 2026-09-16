"""Snapshots and restores — step 1 and step 5 of the write path (§15.2, D6).

A snapshot is a directory under `~/.krcheat/backups/` containing byte-exact copies of
the files a command is about to touch plus a `manifest.json` recording each source
path, its SHA-256, the game version and the invoking command.

The manifest is written **last**. A directory without a manifest is an incomplete
snapshot and is ignored by `list` and refused by `restore` — that is what makes
"snapshot failed ⇒ abort, nothing written" (exit 5) enforceable rather than hopeful.

Scope is deliberately file-level (§15.4): the granularity of a restore is the run,
not the operation, there is no change journal, and there is no automatic retention
policy — snapshots accumulate until `krcheat backup prune --keep N` is run.
"""

from __future__ import annotations

import datetime
import json
import os
import random
import shutil
from typing import Any, Dict, List, Optional

from krcheat import __version__
from krcheat.core import paths
from krcheat.core.errors import BackupError, NotFoundError, UsageError

MANIFEST = "manifest.json"


def _new_id(label):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choice("0123456789abcdef") for _ in range(4))
    clean = "".join(char if char.isalnum() or char in "-_" else "-" for char in (label or "snapshot"))
    return "{0}-{1}-{2}".format(stamp, clean[:32].strip("-"), suffix)


def snapshot(files, label, command=None, version_string=None, extra=None):
    """Copy `files` into a new snapshot directory. Raises `BackupError` on failure.

    Callers must treat a failure as fatal: §15.2 step 1 says abort, write nothing.
    """
    targets = [os.path.abspath(path) for path in files]
    if not targets:
        raise BackupError("nothing to snapshot")
    for path in targets:
        if not os.path.exists(path):
            raise BackupError(
                "cannot snapshot {0}: it does not exist".format(path), path=path
            )

    snapshot_id = _new_id(label)
    directory = os.path.join(paths.backups_dir(), snapshot_id)
    try:
        os.makedirs(directory, exist_ok=False)
    except OSError as exc:
        raise BackupError("cannot create snapshot directory: {0}".format(exc), path=directory)

    entries = []
    try:
        used = {}
        for source in targets:
            name = os.path.basename(source)
            if name in used:
                used[name] += 1
                name = "{0}.{1}".format(name, used[name])
            else:
                used[name] = 0
            destination = os.path.join(directory, name)
            shutil.copyfile(source, destination)
            # Preserve the mode: the game's slot files are 0755, and a restored file
            # that suddenly is not writable is a surprise nobody needs.
            try:
                os.chmod(destination, os.stat(source).st_mode & 0o7777)
            except OSError:
                pass
            digest = paths.sha256_file(destination)
            if digest != paths.sha256_file(source):
                raise BackupError("snapshot copy of {0} does not verify".format(source), path=source)
            entries.append(
                {
                    "source": source,
                    "name": name,
                    "sha256": digest,
                    "size": os.path.getsize(destination),
                    "mode": os.stat(source).st_mode & 0o7777,
                }
            )
    except (OSError, BackupError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("snapshot failed: {0}".format(exc), path=directory)

    manifest = {
        "id": snapshot_id,
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": label,
        "command": command,
        "version_string": version_string,
        "krcheat_version": __version__,
        "python": _python_version(),
        "files": entries,
    }
    if extra:
        manifest["extra"] = extra
    try:
        tmp = os.path.join(directory, MANIFEST + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, os.path.join(directory, MANIFEST))
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise BackupError("cannot write the snapshot manifest: {0}".format(exc), path=directory)
    return manifest


def _python_version():
    import platform

    return platform.python_version()


def snapshot_dirs():
    root = paths.backups_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [os.path.join(root, name) for name in names if os.path.isdir(os.path.join(root, name))]


def list_snapshots(include_incomplete=False):
    out = []
    for directory in snapshot_dirs():
        manifest_path = os.path.join(directory, MANIFEST)
        if not os.path.exists(manifest_path):
            if include_incomplete:
                out.append({"id": os.path.basename(directory), "incomplete": True, "path": directory})
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            if include_incomplete:
                out.append(
                    {"id": os.path.basename(directory), "incomplete": True, "path": directory}
                )
            continue
        manifest.setdefault("id", os.path.basename(directory))
        manifest["path"] = directory
        manifest["total_size"] = sum(
            entry.get("size", 0) for entry in manifest.get("files", [])
        )
        out.append(manifest)
    out.sort(key=lambda item: item.get("id", ""), reverse=True)
    return out


def resolve_snapshot(identifier):
    """Accept a full id or an unambiguous prefix (and `latest`)."""
    snapshots = list_snapshots()
    if not snapshots:
        raise NotFoundError("no snapshots in {0}".format(paths.backups_dir()))
    if identifier in (None, "", "latest"):
        return snapshots[0]
    matches = [item for item in snapshots if item.get("id") == identifier]
    if not matches:
        matches = [item for item in snapshots if item.get("id", "").startswith(identifier)]
    if not matches:
        raise NotFoundError("no snapshot matches {0!r}".format(identifier))
    if len(matches) > 1:
        raise UsageError(
            "{0!r} matches {1} snapshots; be more specific".format(identifier, len(matches))
        )
    return matches[0]


def restore(identifier, verify_only=False):
    """Restore a snapshot byte-identically, verifying against its manifest.

    Returns a dict describing what was restored. Raises `BackupError` if either the
    snapshot copy or the restored destination fails its hash check — a restore that
    cannot be verified is not a restore.
    """
    manifest = resolve_snapshot(identifier)
    directory = manifest["path"]
    results = []
    for entry in manifest.get("files", []):
        source = os.path.join(directory, entry["name"])
        if not os.path.exists(source):
            raise BackupError(
                "snapshot {0} is missing {1}".format(manifest["id"], entry["name"]),
                snapshot=manifest["id"],
            )
        digest = paths.sha256_file(source)
        if digest != entry["sha256"]:
            raise BackupError(
                "snapshot {0} failed its own hash check for {1}".format(
                    manifest["id"], entry["name"]
                ),
                snapshot=manifest["id"],
                expected=entry["sha256"],
                found=digest,
            )
        target = entry["source"]
        record = {
            "source": source,
            "target": target,
            "sha256": digest,
            "size": entry.get("size"),
            "verified": True,
        }
        if not verify_only:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            tmp = target + ".krcheat-restore.tmp"
            try:
                shutil.copyfile(source, tmp)
                if entry.get("mode"):
                    os.chmod(tmp, entry["mode"])
                os.replace(tmp, target)
            except OSError as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise BackupError("cannot restore {0}: {1}".format(target, exc), path=target)
            after = paths.sha256_file(target)
            record["restored_sha256"] = after
            if after != entry["sha256"]:
                raise BackupError(
                    "restore of {0} did not verify after the swap".format(target), path=target
                )
        results.append(record)
    return {"snapshot": manifest, "files": results, "verify_only": verify_only}


def latest_for(path):
    """The newest snapshot that contains `path`, or None."""
    target = os.path.abspath(path)
    for manifest in list_snapshots():
        for entry in manifest.get("files", []):
            if os.path.abspath(entry.get("source", "")) == target:
                return manifest
    return None


def prune(keep=20, dry_run=False):
    """Keep the newest `keep` snapshots and remove the rest (§10.4)."""
    snapshots = list_snapshots()
    keep = int(keep)
    if keep < 0:
        raise UsageError("--keep must be zero or greater")
    doomed = snapshots[keep:]
    removed = []
    for manifest in doomed:
        directory = manifest["path"]
        removed.append({"id": manifest["id"], "path": directory, "size": manifest.get("total_size", 0)})
        if not dry_run:
            shutil.rmtree(directory, ignore_errors=True)
    # incomplete directories are cleaned up as well, since nothing can use them
    for directory in snapshot_dirs():
        if not os.path.exists(os.path.join(directory, MANIFEST)) and not dry_run:
            shutil.rmtree(directory, ignore_errors=True)
    return {"kept": snapshots[:keep], "removed": removed, "dry_run": dry_run}
