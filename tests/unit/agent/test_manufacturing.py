"""Synthetic package lifecycle: integrity, source changes, approval and feedback gates."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from yapnr.agent import manufacturing, reviews, workspace


class ManufacturingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "fab"
        self.folder.mkdir()
        (self.root / "board.kicad_pcb").write_text("synthetic board fixture")
        self.card = {
            "schema": "yapnr-order-card-v1",
            "vendor": {"id": "jlcpcb", "title": "JLCPCB"},
            "board": {"name": "demo"},
            "assembly": {"bom": "bom.csv", "cpl": "cpl.csv"},
            "checks": {"summary": {"error": 0}, "drc_errors": 0},
            "files": {"upload": {"sha256": workspace.sha(b"gerbers")}},
        }
        for name, data in [
            ("order-card.json", json.dumps(self.card)),
            ("bom.csv", "BOM"),
            ("cpl.csv", "CPL"),
            ("README.md", "instructions"),
            ("order-card.md", "order card"),
            ("gerbers.zip", "gerbers"),
        ]:
            (self.folder / name).write_text(data)
        self.manifest = {
            "schema": "yapnr-fab-manifest-v1",
            "board": {"sha256": workspace.file_sha(self.root / "board.kicad_pcb")},
            "gerber_zip": {"name": "gerbers.zip", "sha256": workspace.sha(b"gerbers")},
            "files": [
                {"name": p.name, "sha256": workspace.file_sha(p)} for p in self.folder.iterdir()
            ],
        }
        self.write_archive()
        self.controller = {"revision": 2, "accepted_revision": 2, "contract_current": True}
        query = patch.object(manufacturing.workflow, "query", return_value=self.controller)
        query.start()
        self.addCleanup(query.stop)

    def write_archive(self, extra=None):
        (self.folder / "manifest.json").write_text(json.dumps(self.manifest))
        with zipfile.ZipFile(
            self.folder / f"demo-{self.card['vendor']['id']}-bundle.zip", "w"
        ) as archive:
            for entry in self.manifest["files"]:
                archive.write(self.folder / entry["name"], entry["name"])
            archive.write(self.folder / "manifest.json", "manifest.json")
            if extra:
                archive.writestr(extra, "unexpected")

    def prepare(self):
        return manufacturing.prepare(self.root, "fab", "board.kicad_pcb")

    def test_approval_binds_files_and_changed_board_blocks_handoff(self):
        item = self.prepare()
        self.assertFalse(manufacturing.state(self.root)["releases"][0]["handoff_ready"])
        reviews.approve(self.root, [item["id"]])
        release = manufacturing.state(self.root)["releases"][0]
        self.assertTrue(release["handoff_ready"])
        self.assertEqual(len(release["downloads"]), 6)
        (self.root / "board.kicad_pcb").write_text("changed board")
        self.assertFalse(manufacturing.state(self.root)["releases"][0]["handoff_ready"])
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            reviews.approve(self.root, [item["id"]])

    def test_question_blocks_approval_and_vendor_handoff(self):
        item = self.prepare()
        reviews.feedback(self.root, item["id"], "Confirm LED orientation?", question=True)
        with self.assertRaisesRegex(ValueError, "feedback and questions"):
            reviews.approve(self.root, [item["id"]])
        self.assertFalse(manufacturing.state(self.root)["releases"][0]["handoff_ready"])

    def test_mismatched_or_extra_archive_members_rejected(self):
        self.write_archive("extra.txt")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            self.prepare()
        self.write_archive()
        with zipfile.ZipFile(self.folder / "demo-jlcpcb-bundle.zip", "a") as archive:
            archive.writestr("../outside", "bad")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            self.prepare()

    def test_unmanifested_download_rejected(self):
        self.manifest["files"] = [e for e in self.manifest["files"] if e["name"] != "bom.csv"]
        self.write_archive()
        with self.assertRaisesRegex(ValueError, "bound to the checked manifest"):
            self.prepare()

    def test_pcbway_package_handoff(self):
        self.card["vendor"] = {"id": "pcbway", "title": "PCBWay"}
        (self.folder / "order-card.json").write_text(json.dumps(self.card))
        for entry in self.manifest["files"]:
            entry["sha256"] = workspace.file_sha(self.folder / entry["name"])
        self.write_archive()
        item = self.prepare()
        reviews.approve(self.root, [item["id"]])
        release = manufacturing.state(self.root)["releases"][0]
        self.assertTrue(release["handoff_ready"])
        self.assertIn("pcbway.com", release["vendor_page"])

    def test_corrupt_archive_rejected(self):
        (self.folder / "demo-jlcpcb-bundle.zip").write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "Invalid assembly archive"):
            self.prepare()

    def test_unaccepted_requirements_rejected(self):
        self.controller["accepted_revision"] = None
        with self.assertRaisesRegex(ValueError, "accept the current requirements"):
            self.prepare()

    def test_mutated_bom_rejected(self):
        (self.folder / "bom.csv").write_text("changed BOM")
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.prepare()

    def test_requirement_revision_invalidates_approved_package(self):
        item = self.prepare()
        reviews.approve(self.root, [item["id"]])
        self.controller.update(revision=3, accepted_revision=3)
        self.assertFalse(manufacturing.state(self.root)["releases"][0]["handoff_ready"])


if __name__ == "__main__":
    unittest.main()
