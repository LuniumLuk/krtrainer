"""Snapshot and restore (§15.2, D6)."""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from krcheat.core import backup, paths
from krcheat.core.errors import BackupError, NotFoundError, UsageError


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="krcheat-test-home-")
        self.work = tempfile.mkdtemp(prefix="krcheat-test-work-")
        os.environ["KRCHEAT_HOME"] = self.home
        self.target = os.path.join(self.work, "slot_1.lua")
        self.content = "local obj1 = {\n\t[\"gems\"] = 1;\n}\nreturn obj1\n"
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write(self.content)

    def tearDown(self):
        os.environ.pop("KRCHEAT_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)

    def digest(self, path=None):
        with open(path or self.target, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()


class TestSnapshot(BackupCase):
    def test_snapshot_records_hash_and_command(self):
        manifest = backup.snapshot([self.target], label="test", command="krcheat profile set gems 1")
        self.assertEqual(manifest["command"], "krcheat profile set gems 1")
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["files"][0]["sha256"], self.digest())
        self.assertTrue(os.path.exists(os.path.join(manifest["id"] and paths.backups_dir(), manifest["id"], "manifest.json")))

    def test_missing_file_aborts(self):
        with self.assertRaises(BackupError):
            backup.snapshot([os.path.join(self.work, "nope.lua")], label="test")

    def test_no_files_is_an_error(self):
        with self.assertRaises(BackupError):
            backup.snapshot([], label="test")

    def test_incomplete_snapshot_is_ignored_and_unusable(self):
        directory = os.path.join(paths.backups_dir(), "20260101-000000-broken-aaaa")
        os.makedirs(directory)
        with open(os.path.join(directory, "slot_1.lua"), "w", encoding="utf-8") as handle:
            handle.write("half a snapshot")
        self.assertEqual(backup.list_snapshots(), [])
        self.assertEqual(len(backup.list_snapshots(include_incomplete=True)), 1)
        with self.assertRaises(NotFoundError):
            backup.restore("20260101-000000-broken-aaaa")


class TestRestore(BackupCase):
    def test_restore_is_byte_identical_and_verified(self):
        manifest = backup.snapshot([self.target], label="test")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("damaged")
        payload = backup.restore(manifest["id"])
        with open(self.target, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.content)
        self.assertEqual(payload["files"][0]["restored_sha256"], self.digest())

    def test_latest_is_the_newest(self):
        first = backup.snapshot([self.target], label="first")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write(self.content + "-- second\n")
        second = backup.snapshot([self.target], label="second")
        self.assertEqual(backup.resolve_snapshot("latest")["id"], second["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_prefix_resolution(self):
        manifest = backup.snapshot([self.target], label="test")
        self.assertEqual(backup.resolve_snapshot(manifest["id"][:12])["id"], manifest["id"])

    def test_tampered_snapshot_is_refused(self):
        manifest = backup.snapshot([self.target], label="test")
        copy = os.path.join(paths.backups_dir(), manifest["id"], "slot_1.lua")
        with open(copy, "w", encoding="utf-8") as handle:
            handle.write("not what was snapshotted")
        with self.assertRaises(BackupError):
            backup.restore(manifest["id"])

    def test_verify_only_does_not_write(self):
        manifest = backup.snapshot([self.target], label="test")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("edited")
        before = self.digest()
        backup.restore(manifest["id"], verify_only=True)
        self.assertEqual(self.digest(), before)

    def test_unknown_id_is_not_found(self):
        backup.snapshot([self.target], label="test")
        with self.assertRaises(NotFoundError):
            backup.restore("20990101-000000-nope-zzzz")


class TestPrune(BackupCase):
    def test_prune_keeps_the_newest(self):
        ids = []
        for index in range(4):
            with open(self.target, "w", encoding="utf-8") as handle:
                handle.write(self.content + "-- {0}\n".format(index))
            ids.append(backup.snapshot([self.target], label="keep{0}".format(index))["id"])
        payload = backup.prune(keep=2)
        self.assertEqual(len(payload["removed"]), 2)
        self.assertEqual(len(backup.list_snapshots()), 2)
        self.assertEqual(backup.list_snapshots()[0]["id"], ids[-1])

    def test_prune_dry_run_removes_nothing(self):
        backup.snapshot([self.target], label="test")
        payload = backup.prune(keep=0, dry_run=True)
        self.assertEqual(len(payload["removed"]), 1)
        self.assertEqual(len(backup.list_snapshots()), 1)

    def test_negative_keep_is_refused(self):
        with self.assertRaises(UsageError):
            backup.prune(keep=-1)


if __name__ == "__main__":
    unittest.main()
