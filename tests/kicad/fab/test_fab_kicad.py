"""The fab pipeline with the real headless kicad-cli (KiCad lane, tag ``kicad``).

Synthetic 2-, 4- and 6-layer boards (yapnr.fab.testing; no product data) with an NPTH hole, a
slot, a bottom-side part, LCSC fields and, where asked, an RF footprint or a castellated pad:

- every vendor's bundle: file names, the X2 FileFunction of every gerber (the export checks
  them), the drill header format, and byte-identical zips from two builds;
- the planted violations of design §10 under KiCad's own DRC;
- an RF footprint designed for another substrate stops the build (FAB-RF);
- the preview reads back every file KiCad wrote.

Skips when no headless kicad-cli is found (YAPNR_KICAD_CLI, or the discovery of
yapnr.frontends.atopile.kicad).
"""

from __future__ import annotations

import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from yapnr.fab import build, capability, export, kicad, preview, testing

try:
    CLI = kicad.find_cli()
except kicad.Unavailable as err:  # pragma: no cover - depends on the machine
    CLI = None
    WHY = str(err)


@unittest.skipIf(CLI is None, "no headless kicad-cli")
class KicadFabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def board(self, name, **kwargs):
        return testing.write_board(self.dir / "boards" / name, name, **kwargs)

    def build(self, board, vendor, out="out", **kwargs):
        kwargs.setdefault("name", board.stem)
        return build.build(build.Request(board=board, vendor=vendor, out=self.dir / out, **kwargs))

    def check(self, board, profile, **kwargs):
        return build.check_board(
            build.Request(
                board=board,
                vendor=capability.profile(profile)["vendor"],
                profile=profile,
                out=self.dir / "checks",
                name=f"{board.stem}-{profile}",
                **kwargs,
            )
        )

    def drc_types(self, checked):
        return sorted(
            f.data.get("type")
            for f in checked.findings
            if f.code == "FAB-DRC" and f.severity == "error"
        )

    def members(self, built):
        with zipfile.ZipFile(built.gerber_zip) as zf:
            return {name: zf.read(name).decode() for name in zf.namelist()}

    def test_osh_park_2_4_and_6_layer_bundles(self):
        for layers, extra in ((2, []), (4, ["G2L", "G3L"]), (6, ["G2L", "G3L", "G4L", "G5L"])):
            board = self.board(f"osh{layers}", layers=layers)
            built = self.build(board, "oshpark")
            files = self.members(built)
            exts = sorted(name.rsplit(".", 1)[1] for name in files)
            self.assertEqual(
                exts, sorted(["GBL", "GBO", "GBS", "GKO", "GTL", "GTO", "GTS", "XLN"] + extra)
            )
            drill = files[f"osh{layers}.XLN"]
            self.assertIn("; FORMAT={-:-/ absolute / inch / decimal}", drill)
            self.assertIn("INCH", drill)
            self.assertIn("MixedPlating", drill)  # PTH and NPTH merged
            self.assertIn("G85", drill)  # the slot, alternate drill mode
            self.assertNotIn("date 20", drill)  # normalized time stamps
            for name, text in files.items():
                if not name.endswith(".XLN"):
                    function = export.file_function_text(text)
                    self.assertIsNotNone(export.layer_of_function(function), name)
            self.assertEqual(built.card["profile"]["name"], f"oshpark-{layers}l")

    def test_jlc_and_pcbway_bundles(self):
        board = self.board("asm4", layers=4)
        jlc = self.build(board, "jlcpcb", assembly=True)
        files = self.members(jlc)
        self.assertIn("asm4-F_Paste.gtp", files)
        self.assertIn("asm4-In2_Cu.g2", files)
        self.assertIn("; FORMAT={-:-/ absolute / metric / decimal}", files["asm4-PTH.drl"])
        self.assertIn("asm4-NPTH.drl", files)
        self.assertIn("asm4-PTH-drl_map.gbr", files)
        bom = (jlc.bundle_dir / "asm4-jlcpcb-bom.csv").read_text().splitlines()
        self.assertEqual(bom[0], "Comment,Designator,Footprint,JLCPCB Part #")
        pcbway = self.build(board, "pcbway", allow_draft=True, assembly=True)
        files = self.members(pcbway)
        self.assertIn("asm4-F_Cu.gbr", files)
        self.assertIn("asm4.d356", files)
        self.assertIn("inch", files["asm4-PTH.drl"])

    def test_two_builds_are_byte_identical(self):
        board = self.board("repro", layers=4)
        first = self.build(board, "oshpark", out="r1")
        time.sleep(1.5)  # kicad-cli stamps the second; normalization must hide it
        second = self.build(board, "oshpark", out="r2")
        self.assertEqual(first.gerber_zip.read_bytes(), second.gerber_zip.read_bytes())

    def test_planted_violations(self):
        thin = self.board("thin", layers=4, track_width=0.14)
        self.assertEqual(self.drc_types(self.check(thin, "oshpark-4l")), [])
        thin2 = self.board("thin2", layers=2, track_width=0.14)
        self.assertEqual(self.drc_types(self.check(thin2, "oshpark-2l")), ["track_width"])
        hair = self.board("hair", layers=2, track_width=0.10)
        self.assertEqual(self.drc_types(self.check(hair, "oshpark-2l")), ["track_width"])
        small_drill = self.board("drill020", layers=4, via=(0.5, 0.20))
        self.assertEqual(
            self.drc_types(self.check(small_drill, "oshpark-4l")), ["drill_out_of_range"]
        )
        self.assertEqual(self.drc_types(self.check(small_drill, "jlc-4l")), [])
        small_via = self.board("via045", layers=4, via=(0.45, 0.30))
        self.assertEqual(
            self.drc_types(self.check(small_via, "oshpark-4l")), ["annular_width", "via_diameter"]
        )
        self.assertEqual(self.drc_types(self.check(small_via, "jlc-4l")), [])
        in_pad = self.board("inpad", layers=4, via_in_pad=(0.35, 0.20))
        self.assertEqual(
            self.drc_types(self.check(in_pad, "jlc-4l")), ["clearance", "via_diameter"]
        )
        self.assertEqual(self.drc_types(self.check(in_pad, "jlc-pofv")), [])

    def test_an_rf_footprint_for_another_substrate_stops_the_build(self):
        s1 = self.board("rf-s1", layers=4, rf={"er": 3.55, "tan_delta": 0.0027, "h_mm": 0.813})
        with self.assertRaises(build.BuildStopped) as stop:
            self.build(s1, "oshpark", stackup="oshpark-4l-fr408hr")
        self.assertIn("FAB-RF", [f.code for f in stop.exception.checked.findings])
        matched = self.board(
            "rf-osh", layers=4, rf={"er": 3.61, "tan_delta": 0.009, "h_mm": 0.1999}
        )
        built = self.build(matched, "oshpark", stackup="oshpark-4l-fr408hr")
        self.assertIn("RF1 (test-line)", (built.bundle_dir / "README.md").read_text())

    def test_castellated_pads_are_flagged(self):
        board = self.board("cast", layers=4, castellated=True)
        checked = self.check(board, "oshpark-4l")
        self.assertIn("FAB-CASTELLATED", [f.code for f in checked.findings])

    def test_the_preview_reads_back_what_kicad_wrote(self):
        built = self.build(self.board("prev", layers=4), "oshpark", out="prev")
        written = preview.render_zip(built.gerber_zip, self.dir / "preview")
        self.assertIn("composite-top.svg", [p.name for p in written])
        layers = preview.read_zip(built.gerber_zip)
        for key in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu", "Edge.Cuts", "Drill"):
            self.assertIn(key, layers)
            self.assertTrue(layers[key].items, key)


if __name__ == "__main__":
    unittest.main()
