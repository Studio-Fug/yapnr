"""PNR_SKIP_PASTE_PADS, ingest side: which pads are paste-only, and the graph without
them (KiCad's Python; skips without ``pcbnew``). The router side is
tests/test_paste_pads.py."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HAVE_PCBNEW = importlib.util.find_spec("pcbnew") is not None

FOOTPRINT = "\n".join(
    [
        '(footprint "QFN_EP"',
        "  (version 20240108)",
        '  (generator "pnr_test")',
        '  (layer "F.Cu")',
        "  (attr smd)",
        "  (fp_rect (start -2 -2) (end 2 2) (stroke (width 0.05) (type solid))"
        ' (fill none) (layer "F.CrtYd"))',
        '  (pad "1" smd rect (at -1.9 0) (size 0.8 0.25) (layers "F.Cu" "F.Paste" "F.Mask"))',
        '  (pad "25" smd rect (at 0 0) (size 2.6 2.6) (layers "F.Cu" "F.Mask"))',
        '  (pad "" smd roundrect (at -0.65 -0.65) (size 1.05 1.05) (layers "F.Paste")'
        " (roundrect_rratio 0.25))",
        '  (pad "" smd roundrect (at 0.65 -0.65) (size 1.05 1.05) (layers "F.Paste")'
        " (roundrect_rratio 0.25))",
        '  (pad "" np_thru_hole circle (at 1.5 1.5) (size 0.5 0.5) (drill 0.5)'
        ' (layers "*.Cu" "*.Mask"))',
        ")",
        "",
    ]
)


@unittest.skipUnless(HAVE_PCBNEW, "needs KiCad's Python (pcbnew)")
class PasteOnlyPadTest(unittest.TestCase):
    def board(self, directory):
        import pcbnew as k

        lib = Path(directory) / "t.pretty"
        lib.mkdir()
        (lib / "QFN_EP.kicad_mod").write_text(FOOTPRINT)
        b = k.BOARD()
        fp = k.FootprintLoad(str(lib), "QFN_EP")
        fp.SetReference("U4")
        b.Add(fp)
        fp.SetPosition(k.VECTOR2I(10000000, 10000000))
        path = str(Path(directory) / "t.kicad_pcb")
        k.SaveBoard(path, b)
        return path, fp

    def test_only_the_copperless_holeless_pads_are_paste(self):
        from pnr.ingest import _paste_only

        with tempfile.TemporaryDirectory() as directory:
            _path, fp = self.board(directory)
            got = sorted((p.GetNumber(), _paste_only(p)) for p in fp.Pads())
        self.assertEqual(got, [("", False), ("", True), ("", True), ("1", False), ("25", False)])

    def test_ingest_leaves_them_off_the_graph_with_the_flag(self):
        from pnr.ingest import load

        with tempfile.TemporaryDirectory() as directory:
            path, _fp = self.board(directory)
            counts = {}
            for flag in ("0", "1"):
                with patch.dict(os.environ, {"PNR_SKIP_PASTE_PADS": flag}):
                    counts[flag] = len(load(path).component("U4").pads)
        self.assertEqual(counts, {"0": 5, "1": 3})


if __name__ == "__main__":
    unittest.main()
