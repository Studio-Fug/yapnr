"""Published synthetic prototype discovery and local supplier quote conversion."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yapnr.agent import manufacturing
from yapnr.agent import manufacturing_packages as packages
from yapnr.agent import reviews, workspace
from yapnr.fab import assembly, board, build, bundle, capability, testing


class PrototypePackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.design = self.root / "design"
        self.design.mkdir()
        path = testing.write_board(self.design, "board")
        self.native = board.read(path)
        workspace.publish(self.root, "design/board.kicad_pcb", "board", "Synthetic source board")
        parts, _ = assembly.collect(self.native)
        columns = ["Reference", "Value", "Manufacturer", "MPN", "LCSC"]
        rows = [[p.reference, p.value, p.manufacturer, p.mpn, p.lcsc] for p in parts]
        self.data = {
            "cad/board.kicad_pcb": path.read_bytes(),
            "assembly/BOM-all-parts.csv": assembly.csv_text(columns, rows).encode(),
            "assembly/SMT-CPL-jlc.csv": assembly.outputs(capability.vendor("jlcpcb"), parts)[
                "cpl"
            ].encode(),
            "settings.json": json.dumps({"width_mm": 30, "height_mm": 20}).encode(),
            "checks/drc.json": json.dumps({"violations": [], "unconnected_items": []}).encode(),
            "fabrication/board.gtl": b"synthetic export fixture",
            "README.md": b"Synthetic prototype; vendor and physical checks remain open.",
        }
        self.manifest = {
            "schema": "yapnr-prototype-manufacturing-v1",
            "board_sha256": self.native.sha256,
            "requirements_sha256": workspace.sha(workspace.encoded({})),
            "files": {n: workspace.sha(v) for n, v in self.data.items()},
        }
        self.controller = {"revision": 2, "accepted_revision": 2, "contract_current": True}
        query = patch.object(packages.workflow, "query", return_value=self.controller)
        query.start()
        self.addCleanup(query.stop)
        self.item = self.publish()

    def publish(self, extra=None):
        path = self.root / "manufacturing/package.zip"
        path.parent.mkdir(exist_ok=True)
        members = [(n, v) for n, v in self.data.items()] + [
            ("manifest.json", workspace.encoded(self.manifest))
        ]
        if extra:
            members.append(extra)
        bundle.write_zip(path, members)
        return workspace.publish(
            self.root, "manufacturing/package.zip", "report", "Synthetic prototype package"
        )

    def test_regular_artifact_discovered_without_handoff_metadata(self):
        candidates = packages.candidates(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["current"])
        self.assertIn("unqualified", candidates[0]["qualification"])
        self.assertEqual(
            manufacturing.state(self.root)["candidates"][0]["artifact"], self.item["id"]
        )

    def test_both_supplier_outputs_and_review_are_bound_and_reproducible(self):
        for vendor in ("jlcpcb", "pcbway"):
            with self.subTest(vendor=vendor):
                item = packages.prepare(self.root, self.item["id"], vendor, 5, "ENIG")
                repeated = packages.prepare(self.root, self.item["id"], vendor, 5, "ENIG")
                self.assertEqual(item["id"], repeated["id"])
                release = next(
                    r
                    for r in manufacturing.state(self.root)["releases"]
                    if r["artifact"] == item["id"]
                )
                self.assertTrue(release["prototype"])
                self.assertFalse(release["handoff_ready"])
                self.assertIn("J1", " ".join(release["card"]["assembly"]["notes"]))
                expected = "JLCPCB Part #" if vendor == "jlcpcb" else "Mfg Part #"
                self.assertIn(expected, release["bom"]["columns"])
                reviews.approve(self.root, [item["id"]])
                release = next(
                    r
                    for r in manufacturing.state(self.root)["releases"]
                    if r["artifact"] == item["id"]
                )
                self.assertTrue(release["handoff_ready"])
                self.assertIn("remain open", release["open_verification"])

    def test_turnkey_request_includes_header_without_claiming_vendor_acceptance(self):
        item = packages.prepare(
            self.root, self.item["id"], "pcbway", 5, "ENIG", include_through_hole=True
        )
        release = next(
            r for r in manufacturing.state(self.root)["releases"] if r["artifact"] == item["id"]
        )
        self.assertTrue(any("J1" in row for row in release["cpl"]["rows"]))
        self.assertIn("confirm service", " ".join(release["card"]["assembly"]["notes"]))
        self.assertFalse(release["handoff_ready"])

    def test_regular_native_bundle_discovered_and_keeps_checked_settings(self):
        native = testing.write_board(self.design, "native", layers=4)
        fake = testing.FakeKicadCli()
        with patch.object(build.kicad, "find_cli", return_value=Path("kicad-cli")), patch.object(
            build.kicad, "Cli", return_value=fake
        ):
            built = build.build(
                build.Request(
                    board=native,
                    vendor="jlcpcb",
                    assembly=True,
                    consign=["J1"],
                    out=self.root / "reports/ready",
                    name="demo",
                )
            )
        workspace.publish(
            self.root, "design/native.kicad_pcb", "board", "Synthetic native bundle source"
        )
        archive = built.bundle_dir / "demo-jlcpcb-bundle.zip"
        source = workspace.publish(
            self.root, str(archive.relative_to(self.root)), "report", "Synthetic native fab bundle"
        )
        candidate = next(c for c in packages.candidates(self.root) if c["artifact"] == source["id"])
        self.assertEqual(candidate["kind"], "qualified")
        with self.assertRaisesRegex(ValueError, "settings are bound"):
            packages.prepare(
                self.root, source["id"], "pcbway", candidate["quantity"], candidate["finish"]
            )
        item = packages.prepare(
            self.root, source["id"], "jlcpcb", candidate["quantity"], candidate["finish"]
        )
        release = next(
            r for r in manufacturing.state(self.root)["releases"] if r["artifact"] == item["id"]
        )
        self.assertEqual(release["source_artifact"], source["id"])
        self.assertFalse(release.get("prototype", False))

    def test_changed_source_board_blocks_preparation(self):
        (self.design / "board.kicad_pcb").write_text("changed board")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            packages.prepare(self.root, self.item["id"], "jlcpcb", 5, "ENIG")

    def test_changed_requirement_hash_blocks_preparation(self):
        self.manifest["requirements_sha256"] = "0" * 64
        item = self.publish()
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            packages.prepare(self.root, item["id"], "jlcpcb", 5, "ENIG")

    def test_member_tamper_and_traversal_rejected(self):
        self.data["README.md"] = b"changed after manifest"
        item = self.publish()
        with self.assertRaisesRegex(ValueError, "hash changed"):
            packages.contents(self.root, item["id"])
        item = self.publish(("../outside", b"bad"))
        with self.assertRaisesRegex(ValueError, "Invalid package"):
            packages.contents(self.root, item["id"])

    def test_packaged_drc_errors_block_review(self):
        self.data["checks/drc.json"] = json.dumps(
            {"violations": [{"severity": "error"}], "unconnected_items": []}
        ).encode()
        self.manifest["files"]["checks/drc.json"] = workspace.sha(self.data["checks/drc.json"])
        item = self.publish()
        with self.assertRaisesRegex(ValueError, "DRC errors"):
            packages.prepare(self.root, item["id"], "jlcpcb", 5, "ENIG")

    def test_prepare_validates_options(self):
        with self.assertRaisesRegex(ValueError, "Choose a vendor"):
            packages.prepare(self.root, self.item["id"], "unknown", 5, "ENIG")
        with self.assertRaisesRegex(ValueError, "surface finish"):
            packages.prepare(self.root, self.item["id"], "jlcpcb", 5, "")


if __name__ == "__main__":
    unittest.main()
