"""Workspace fidelity, deterministic archives and fail-closed imports."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yapnr.agent import workspace


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "article"
        self.root.mkdir()
        (self.root / "design.txt").write_text("Recorded design input\n")

    def test_runtime_journal_is_git_ignored_but_still_exported(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        workspace.exclude_runtime_snapshots(self.root)
        workspace.exclude_runtime_snapshots(self.root)
        workspace.record(self.root, {"source": "synthetic-fixture"})
        result = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", ".yapnr/workspace/conversation.jsonl"],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            ".yapnr/workspace/conversation.jsonl",
            {f["path"] for f in workspace.index(self.root)["files"]},
        )
        self.assertEqual(
            (self.root / ".git/info/exclude").read_text().count("/.yapnr/workspace/"), 1
        )

    def test_export_rejects_write_then_restore_race_in_the_bytes_actually_archived(self):
        original = tarfile.TarFile.addfile
        path = self.root / "design.txt"
        before = path.read_bytes()

        def racing_addfile(tar, info, reader=None):
            if info.name != "design.txt":
                return original(tar, info, reader)
            path.write_bytes(b"x" * len(before))
            try:
                return original(tar, info, reader)
            finally:
                path.write_bytes(before)

        archive = self.base / "raced.tar.gz"
        with mock.patch.object(tarfile.TarFile, "addfile", racing_addfile):
            with self.assertRaisesRegex(ValueError, "changed during export"):
                workspace.export(self.root, archive)
        self.assertFalse(archive.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_same_state_exports_identical_bytes_and_import_preserves_all_records(self):
        event = {
            "type": "tool",
            "call": {"name": "route", "seed": 42},
            "result": {"output": "complete"},
        }
        workspace.record(self.root, event)
        artifact = workspace.publish(self.root, "design.txt", "document", "Design")
        first = workspace.export(self.root, self.base / "first.tar.gz")
        os.utime(self.root / "design.txt", (1, 1))
        second = workspace.export(self.root, self.base / "second.tar.gz")
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        restored = self.base / "restored"
        manifest = workspace.import_archive(self.base / "first.tar.gz", restored)
        self.assertEqual(
            (restored / "design.txt").read_bytes(), (self.root / "design.txt").read_bytes()
        )
        self.assertEqual(manifest["artifacts"][0]["id"], artifact["id"])
        log = restored / manifest["conversation"]
        self.assertEqual(json.loads(log.read_text()), event)
        third = workspace.export(restored, self.base / "third.tar.gz")
        self.assertEqual(first["archive_sha256"], third["archive_sha256"])

    def test_published_artifact_is_frozen_when_source_changes(self):
        item = workspace.publish(self.root, "design.txt", "document", "Design")
        (self.root / "design.txt").write_text("Revised design\n")
        self.assertEqual((self.root / item["path"]).read_text(), "Recorded design input\n")

    def archive(self, name, entries):
        path = self.base / name
        with tarfile.open(path, "w:gz") as tar:
            for filename, data, kind in entries:
                item = tarfile.TarInfo(filename)
                item.size = len(data)
                item.type = kind
                if kind == tarfile.SYMTYPE:
                    item.linkname = "outside"
                tar.addfile(item, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        return path

    def test_unsafe_paths_links_and_unindexed_members_never_leave_partial_import(self):
        manifest = workspace.encoded({"schema": "yapnr-workspace-v1", "files": []})
        for filename, kind in (
            ("../escape", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("link", tarfile.SYMTYPE),
            ("unindexed", tarfile.REGTYPE),
        ):
            with self.subTest(filename=filename):
                archive = self.archive(
                    "unsafe.tar.gz",
                    [("workspace.json", manifest, tarfile.REGTYPE), (filename, b"value", kind)],
                )
                with self.assertRaises(ValueError):
                    workspace.import_archive(archive, self.base / "restored")
                self.assertFalse((self.base / "restored").exists())
                self.assertFalse((self.base / "escape").exists())

    def test_import_rejects_corrupt_artifact(self):
        manifest = workspace.encoded(
            {
                "schema": "yapnr-workspace-v1",
                "files": [
                    {
                        "path": "design.txt",
                        "sha256": workspace.sha(b"correct"),
                        "bytes": 7,
                        "mode": 0o644,
                    }
                ],
            }
        )
        archive = self.archive(
            "corrupt.tar.gz",
            [
                ("workspace.json", manifest, tarfile.REGTYPE),
                ("design.txt", b"corrupt", tarfile.REGTYPE),
            ],
        )
        with self.assertRaises(ValueError):
            workspace.import_archive(archive, self.base / "restored")

    def test_export_refuses_external_symlinks_and_existing_destinations(self):
        (self.root / "link").symlink_to(self.base / "external")
        with self.assertRaises(ValueError):
            workspace.export(self.root, self.base / "out.tar.gz")
        (self.root / "link").unlink()
        workspace.export(self.root, self.base / "out.tar.gz")
        with self.assertRaises(ValueError):
            workspace.export(self.root, self.base / "out.tar.gz")


if __name__ == "__main__":
    unittest.main()
