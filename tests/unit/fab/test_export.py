"""Normalization, X2 layer checks and vendor names of the exported fab files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yapnr.fab import capability, export, testing

# Header lines as kicad-cli 10.0.6 writes them (recorded from a run with TZ=UTC).
GERBER_HEAD = """%TF.GenerationSoftware,KiCad,Pcbnew,10.0.6*%
%TF.CreationDate,2026-10-02T21:43:41+00:00*%
%TF.ProjectId,b04,6230342e-6b69-4636-9164-5f7063625858,rev?*%
%TF.FileFunction,Copper,L1,Top*%
G04 Created by KiCad (PCBNEW 10.0.6) date 2026-10-02 21:43:41*
%MOMM*%
"""
DRILL_HEAD = """M48
; DRILL file KiCad 10.0.6 date 2026-10-02T21:43:41
; FORMAT={-:-/ absolute / inch / decimal}
; #@! TF.CreationDate,2026-10-02T21:43:41+00:00
; #@! TF.GenerationSoftware,Kicad,Pcbnew,10.0.6
"""
JOB = """{
  "Header": {
    "CreationDate": "2026-10-02T21:43:41+00:00"
  },
  "GeneralSpecs": {"ProjectId": {"Name": "b04"}}
}
"""


class NormalizeTest(unittest.TestCase):
    def test_every_stamp_is_rewritten_and_nothing_else(self):
        when = export.stamp_time(None)
        for text, count in ((GERBER_HEAD, 2), (DRILL_HEAD, 2), (JOB, 1)):
            new, n = export.normalize_text(text, when)
            self.assertEqual(n, count)
            self.assertNotIn("2026-10-02", new)
            self.assertEqual(len(new.splitlines()), len(text.splitlines()))
        new, _ = export.normalize_text(GERBER_HEAD, when)
        self.assertIn("%TF.CreationDate,1980-01-01T00:00:00+00:00*%", new)
        self.assertIn("date 1980-01-01 00:00:00*", new)
        new, _ = export.normalize_text(DRILL_HEAD, when)
        self.assertIn("; DRILL file KiCad 10.0.6 date 1980-01-01T00:00:00\n", new)

    def test_source_date_epoch(self):
        when = export.stamp_time(1790000000)
        new, _ = export.normalize_text(GERBER_HEAD, when)
        self.assertIn(when.strftime("%Y-%m-%dT%H:%M:%S+00:00"), new)

    def test_two_runs_differ_only_in_stamps(self):
        a = GERBER_HEAD
        b = GERBER_HEAD.replace("21:43:41", "21:43:44")
        when = export.stamp_time(None)
        self.assertEqual(export.normalize_text(a, when)[0], export.normalize_text(b, when)[0])

    def test_crlf_line_endings_are_kept(self):
        crlf = GERBER_HEAD.replace("\n", "\r\n")
        new, n = export.normalize_text(crlf, export.stamp_time(None))
        self.assertEqual(n, 2)
        self.assertEqual(new.count("\r\n"), crlf.count("\r\n"))


class LayersTest(unittest.TestCase):
    def test_x2_functions(self):
        cases = {
            "Copper,L1,Top": "F.Cu",
            "Copper,L4,Bot": "B.Cu",
            "Copper,L2,Inr": "In1.Cu",
            "Copper,L5,Inr": "In4.Cu",
            "Soldermask,Top": "F.Mask",
            "Soldermask,Bot": "B.Mask",
            "Legend,Top": "F.Silkscreen",
            "Paste,Bot": "B.Paste",
            "Profile,NP": "Edge.Cuts",
            "Drillmap": None,
        }
        for function, layer in cases.items():
            self.assertEqual(export.layer_of_function(function), layer, function)

    def test_safe_names(self):
        self.assertEqual(export.safe_name("04 inverter/leds"), "04-inverter-leds")
        with self.assertRaises(export.ExportError):
            export.safe_name("///")


class ExportFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def export(self, vendor, layers=4, cli=None, name="b"):
        board = testing.write_board(self.dir / "src", name, layers=layers)
        return export.export_fab_files(
            cli or testing.FakeKicadCli(),
            board,
            name,
            capability.vendor(vendor),
            testing.copper_names(layers),
            self.dir / "work",
            export.stamp_time(None),
        )

    def test_osh_park_names_merged_inch_drill_and_no_paste(self):
        fab, aux = self.export("oshpark", layers=6)
        self.assertEqual(
            [f.name for f in fab],
            [
                "b.G2L",
                "b.G3L",
                "b.G4L",
                "b.G5L",
                "b.GBL",
                "b.GBO",
                "b.GBS",
                "b.GKO",
                "b.GTL",
                "b.GTO",
                "b.GTS",
                "b.XLN",
            ],
        )
        self.assertEqual([f.name for f in aux], ["b-job.gbrjob"])
        drill = next(f for f in fab if f.kind == "drill").path.read_text()
        self.assertIn("INCH", drill)
        self.assertIn("date 1980-01-01T00:00:00", drill)
        for f in fab:
            self.assertNotIn("2026-10-02", f.path.read_text())

    def test_jlc_keeps_kicad_protel_names_with_separate_drills_and_maps(self):
        fab, _ = self.export("jlcpcb")
        names = [f.name for f in fab]
        self.assertIn("b-F_Paste.gtp", names)
        self.assertIn("b-In1_Cu.g1", names)
        self.assertIn("b-PTH.drl", names)
        self.assertIn("b-PTH-drl_map.gbr", names)
        # KiCad's empty NPTH file (no NPTH holes) is left out.
        self.assertNotIn("b-NPTH.drl", names)
        fab, _ = self.export("jlcpcb", cli=testing.FakeKicadCli(npth=True))
        self.assertIn("b-NPTH.drl", [f.name for f in fab])

    def test_pcbway_kicad_names_inch_drills_and_ipc_netlist(self):
        fab, _ = self.export("pcbway")
        names = [f.name for f in fab]
        self.assertIn("b-F_Cu.gbr", names)
        self.assertIn("b.d356", names)
        self.assertNotIn("b-PTH-drl_map.gbr", names)
        self.assertIn("INCH", next(f for f in fab if f.name == "b-PTH.drl").path.read_text())

    def test_a_gerber_whose_function_does_not_match_its_name_stops_the_export(self):
        class Swapped(testing.FakeKicadCli):
            def gerbers(self, board, outdir, layers, **kw):
                super().gerbers(board, outdir, layers, **kw)
                top = Path(outdir) / f"{Path(board).stem}-F_Cu.gtl"
                top.write_text(top.read_text().replace("Copper,L1,Top", "Copper,L4,Bot"))

        with self.assertRaises(export.ExportError):
            self.export("oshpark", cli=Swapped())

    def test_a_missing_layer_stops_the_export(self):
        class Lazy(testing.FakeKicadCli):
            def gerbers(self, board, outdir, layers, **kw):
                super().gerbers(
                    board, outdir, [layer for layer in layers if layer != "B.Mask"], **kw
                )

        with self.assertRaises(export.ExportError):
            self.export("oshpark", cli=Lazy())


if __name__ == "__main__":
    unittest.main()
