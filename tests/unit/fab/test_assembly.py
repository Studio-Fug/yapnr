"""Assembly outputs: BOM grouping, LCSC ids and the parts lock, CPL rotation, consigned parts."""

from __future__ import annotations

import csv
import io
import math
import tempfile
import unittest
from pathlib import Path

from yapnr.fab import assembly, board, capability, testing


def part(
    ref,
    value="10k",
    lcsc="C1",
    side="top",
    rot=0.0,
    fp="R_0603_1608Metric",
    mpn="",
    consigned=False,
):
    return assembly.Part(ref, value, fp, side, 10.0, -5.0, rot, "SMD", lcsc, mpn, "", consigned)


def rows(text):
    return list(csv.reader(io.StringIO(text)))


class BomTest(unittest.TestCase):
    def test_jlc_groups_by_lcsc_value_and_footprint_in_natural_order(self):
        parts = [
            part("R10"),
            part("R2"),
            part("R1", lcsc="C2"),
            part("C1", "1u", "C3", fp="C_0603"),
        ]
        self.assertEqual(
            assembly.jlc_bom(parts),
            [
                ["1u", "C1", "C_0603", "C3"],
                ["10k", "R1", "R_0603_1608Metric", "C2"],
                ["10k", "R2,R10", "R_0603_1608Metric", "C1"],
            ],
        )

    def test_csv_has_the_vendor_columns(self):
        out = assembly.outputs(capability.vendor("jlcpcb"), [part("R1")])
        self.assertEqual(
            rows(out["bom"])[0], ["Comment", "Designator", "Footprint", "JLCPCB Part #"]
        )
        self.assertEqual(rows(out["cpl"])[0], ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        self.assertEqual(rows(out["bom"])[1], ["10k", "R1", "R_0603_1608Metric", "C1"])
        out = assembly.outputs(capability.vendor("pcbway"), [part("R1", mpn="RC0603")])
        self.assertEqual(rows(out["bom"])[0][:3], ["Item #", "Designator", "Qty"])
        self.assertEqual(
            rows(out["bom"])[1],
            ["1", "R1", "1", "", "RC0603", "10k", "R_0603_1608Metric", "SMD", "LCSC C1"],
        )


class CplTest(unittest.TestCase):
    def test_rotation_follows_fabrication_toolkit(self):
        """Golden: top r, bottom 180 - r as seen from the top, both modulo 360 [FT]."""
        cases = [
            ("top", 0, 0),
            ("top", -90, 270),
            ("top", 450, 90),
            ("bottom", 0, 180),
            ("bottom", 90, 90),
            ("bottom", 270, 270),
            ("bottom", 180, 0),
            ("bottom", 45, 135),
        ]
        for side, r, want in cases:
            self.assertEqual(assembly.jlc_rotation(part("U1", side=side, rot=r)), want, (side, r))

    def test_jlc_cpl_rows(self):
        self.assertEqual(
            assembly.jlc_cpl([part("R1", rot=90), part("R2", side="bottom", rot=90)]),
            [["R1", "10", "-5", "Top", "90"], ["R2", "10", "-5", "Bottom", "90"]],
        )

    def test_centroid_is_the_pad_box_centre_at_any_angle(self):
        """Mid X/Mid Y is the part's centre, not its anchor: a header anchored on pin 1 [FT]."""

        def header(angle):
            a = math.radians(angle)
            pads = [
                board.Pad(
                    str(i + 1),
                    "thru_hole",
                    "rect",
                    at=(10 + u * math.cos(a), 20 - u * math.sin(a), angle),
                    size=(1.7, 1.7),
                )
                for i, u in enumerate((0.0, 2.54))
            ]
            return board.Footprint("J1", "", "x:H", "F.Cu", (10.0, 20.0, angle), {}, [], "", pads)

        for angle in (0, 90, 45, 180):
            a = math.radians(angle)
            x, y = assembly.centroid(header(angle))
            self.assertAlmostEqual(x, 10 + 1.27 * math.cos(a), 6, angle)
            self.assertAlmostEqual(y, 20 - 1.27 * math.sin(a), 6, angle)
        bare = board.Footprint("H1", "", "x:H", "F.Cu", (3.0, 4.0, 0.0), {}, [], "")
        self.assertEqual(assembly.centroid(bare), (3.0, 4.0))

    def test_parts_excluded_from_position_files_stay_in_the_bom(self):
        parts = [part("R1"), part("R2")]
        parts[1].in_pos = False
        self.assertEqual([r[0] for r in assembly.jlc_cpl(parts)], ["R1"])
        self.assertEqual([r[0] for r in assembly.pcbway_cpl(parts)], ["R1"])
        self.assertEqual(assembly.jlc_bom(parts)[0][1], "R1,R2")

    def test_consigned_parts_are_not_placed(self):
        parts = [part("R1"), part("J1", consigned=True)]
        self.assertEqual([r[0] for r in assembly.jlc_cpl(parts)], ["R1"])
        self.assertEqual([r[1] for r in assembly.jlc_bom(parts)], ["R1"])


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def facts(self, **kwargs):
        return board.read(testing.write_board(Path(self.tmp.name), "b", **kwargs))

    def test_parts_from_the_board(self):
        parts, skipped = assembly.collect(
            self.facts(layers=4, rf={"er": 3.61, "h_mm": 0.2, "tan_delta": 0.01})
        )
        self.assertEqual([p.reference for p in parts], ["J1", "R1", "R2"])
        self.assertEqual(skipped["rf"], ["RF1"])
        self.assertEqual(skipped["excluded"], ["H1", "H2"])
        r1 = next(p for p in parts if p.reference == "R1")
        self.assertEqual((r1.lcsc, r1.mpn, r1.manufacturer), ("C990000001", "YP-R0805-1K", "Yapnr"))
        self.assertEqual((r1.x_mm, r1.y_mm, r1.side), (108.0, -106.0, "top"))
        self.assertEqual(next(p for p in parts if p.reference == "R2").side, "bottom")

    def test_findings_missing_ids_lock_conflicts_rotation_and_bottom(self):
        jlc = capability.vendor("jlcpcb")
        parts, skipped = assembly.collect(self.facts(layers=4, lcsc=False))
        found = assembly.findings(jlc, parts, skipped)
        self.assertEqual(
            [(f.severity, f.message.split(" ")[0]) for f in found if f.severity == "error"],
            [("error", "3")],
        )
        parts, skipped = assembly.collect(self.facts(layers=4), consign=["J1"])
        found = assembly.findings(jlc, parts, skipped, lock_lcsc=["C990000001"])
        messages = " | ".join(f"{f.severity}: {f.message}" for f in found)
        self.assertIn("error: LCSC ids not in the parts lock: R2", messages)
        self.assertIn("warning: check rotation in JLCPCB's placement preview: R1, R2", messages)
        self.assertIn("warning: bottom-side parts (R2)", messages)
        self.assertIn("info: not assembled by the vendor (consigned): J1", messages)

    def test_pcbway_wants_an_mpn_but_takes_the_lcsc_hint(self):
        pcbway = capability.vendor("pcbway")
        parts, skipped = assembly.collect(self.facts(layers=4))
        found = assembly.findings(pcbway, parts, skipped)
        self.assertEqual([f.severity for f in found if "only an LCSC id" in f.message], ["warning"])
        self.assertFalse(any(f.severity == "error" for f in found))


if __name__ == "__main__":
    unittest.main()
