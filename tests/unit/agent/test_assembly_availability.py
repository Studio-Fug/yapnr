"""Inventory evidence binds the immutable BOM, quantity and source board."""

import json
import unittest
from unittest.mock import patch

from tests.unit.agent import test_manufacturing_packages as fixture
from yapnr.agent import assembly_availability as stock
from yapnr.agent import manufacturing_packages, workspace
from yapnr.frontends.atopile.picker import availability


class AssemblyAvailabilityTest(unittest.TestCase):
    setUp = fixture.PrototypePackageTest.setUp
    publish = fixture.PrototypePackageTest.publish

    def test_report_is_published_and_candidate_exposes_current_quantity_evidence(self):
        with patch.object(availability, "fetch", side_effect=OSError("offline")):
            report = stock.check(self.root, self.item["id"], "jlcpcb", 5)
        self.assertFalse(report["all_available"])
        self.assertEqual(report["board_sha256"], self.native.sha256)
        candidate = manufacturing_packages.candidates(self.root)[0]
        self.assertEqual(candidate["availability"][0]["artifact"], report["artifact"])
        self.assertTrue(candidate["availability"][0]["fresh"])
        record = workspace.read_json(self.root / ".yapnr/workspace/artifacts.json", {})
        item = next(a for a in record["artifacts"] if a["id"] == report["artifact"])
        data = json.loads(workspace.relative_file(self.root, item["path"]).read_bytes())
        self.assertEqual(data["quantity"], 5)
        self.assertNotIn("signed", repr(data))

    def test_changed_board_blocks_query_and_pcbway_never_calls_jlc(self):
        with patch.object(availability, "fetch") as fetch:
            report = stock.check(self.root, self.item["id"], "pcbway", 5)
            fetch.assert_not_called()
        self.assertTrue(all(p["status"] == "quote_required" for p in report["results"]))
        (self.design / "board.kicad_pcb").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Refresh"):
            stock.check(self.root, self.item["id"], "jlcpcb", 5)


if __name__ == "__main__":
    unittest.main()
