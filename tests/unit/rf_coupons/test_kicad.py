"""Board generation with KiCad's DRC and the fab package (design §11.3, slow lane); KiCad lane
only (tag `kicad`): needs a headless kicad-cli in YAPNR_KICAD_CLI (DEVELOPERS.md, never the
GUI bundle)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile

from yapnr.rf.coupons import fab

CLI = os.environ.get("YAPNR_KICAD_CLI", "")


@unittest.skipUnless(CLI and os.access(CLI, os.X_OK), "no headless kicad-cli in YAPNR_KICAD_CLI")
class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_boards_are_drc_clean(self):
        for sid, letter in (("JLC04161H-7628", "A"), ("JLC06161H-2116C", "B")):
            out = fab.generate(sid, os.path.join(self.dir, sid))
            self.assertEqual(out["drc"]["violations"], {}, out["drc"]["details"][:5])
            self.assertEqual(out["drc"]["unconnected"], 0)
            with zipfile.ZipFile(out["fab_zip"]) as z:
                names = z.namelist()
            self.assertIn(f"board-{letter}/board-{letter}-Edge_Cuts.gm1", names)
            self.assertIn(f"board-{letter}/fab-notes.md", names)
            self.assertTrue(any(n.endswith("-PTH.drl") for n in names))

    def test_launch_check_boards_are_drc_clean(self):
        """The Cinch 142-0701-851 launch of both Order 0 regions under OSH Park's 4-layer rules
        (0.381 mm copper keep-back, 0.127 mm space, 0.254 mm drill to copper)."""
        for reg in ("M", "W"):
            out = fab.generate_launch_check(reg, os.path.join(self.dir, f"launch-{reg}"))
            self.assertEqual(out["drc"]["violations"], {}, out["drc"]["details"][:5])
            self.assertEqual(out["drc"]["unconnected"], 0)

    def test_order0_uploads_are_drc_clean(self):
        """Board O (Order 0 design): the three OSH Park uploads under OSH Park's 4-layer rules,
        the frameless outline with its mouse bites included."""
        for upload in ("M", "W", "D"):
            out = fab.generate(
                "OSHPARK-4L-FR408HR", os.path.join(self.dir, upload), upload=upload, git="test"
            )
            self.assertEqual(out["drc"]["violations"], {}, out["drc"]["details"][:5])
            self.assertEqual(out["drc"]["unconnected"], 0)

    def test_misplaced_mouse_bites_fail_the_drc(self):
        """The board's MB1 rule exempts only bites inside a tab: the review's F1 (bite rows
        laid along the slot's end lines of a tab across a vertical slot) is a DRC error."""
        from yapnr.rf.coupons import layout_o

        finish = layout_o._finish

        def wrong(its, width, height, tabs, meta):
            return finish(its, width, height, tabs, [("h", a, b) for _, a, b in meta])

        layout_o._finish = wrong
        try:
            out = fab.generate("OSHPARK-4L-FR408HR", self.dir, upload="D", git="test")
        finally:
            layout_o._finish = finish
        self.assertGreater(out["drc"]["violations"].get("error:hole_clearance", 0), 0)


if __name__ == "__main__":
    unittest.main()
