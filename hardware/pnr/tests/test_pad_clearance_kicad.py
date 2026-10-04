"""Pad-local clearance and mask margin read from KiCad footprints (pnr.ingest), under
KiCad's Python (it skips without pcbnew). Footprints are written inline as KiCad 10's
library draws them, so no footprint library is needed."""

import importlib.util
import os
import unittest

# Footprints as KiCad 10's library draws them: Fiducial_1mm_Mask2mm's pad (its own
# clearance and mask margin), a land under a footprint-wide 0.05 mm margin (the
# UFBGA-201 footprint's) and one under a footprint-wide clearance.
FOOTPRINTS = {
    "Fid": '(pad "" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")'
    " (solder_mask_margin 0.5) (clearance 0.6))",
    "Bga": '(pad "A1" smd circle (at 0 0) (size 0.3 0.3) (layers "F.Cu" "F.Mask"))',
    "Wide": '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))',
}
FOOTPRINT_LOCAL = {"Fid": "", "Bga": "(solder_mask_margin 0.05)", "Wide": "(clearance 0.3)"}


@unittest.skipUnless(importlib.util.find_spec("pcbnew"), "requires native KiCad")
class IngestTest(unittest.TestCase):
    def test_pad_and_footprint_values(self):
        import tempfile

        import pcbnew

        from pnr.ingest import _pad_local_rules

        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, "Local.pretty")
            os.mkdir(lib)
            for name, pad in FOOTPRINTS.items():
                with open(os.path.join(lib, name + ".kicad_mod"), "w", encoding="utf-8") as fh:
                    fh.write(
                        '(footprint "%s" (version 20240108) (generator "test") (layer "F.Cu")'
                        " %s %s)\n" % (name, FOOTPRINT_LOCAL[name], pad)
                    )
            got = {}
            for name in FOOTPRINTS:
                fp = pcbnew.FootprintLoad(lib, name)
                got[name] = _pad_local_rules(next(iter(fp.Pads())), fp)
        self.assertEqual(got["Fid"], {"clearance_mm": 0.6, "mask_margin_mm": 0.5})
        # The library's 0.05 mm land margin is not recorded (MASK_MARGIN_FLOOR_MM).
        self.assertEqual(got["Bga"], {})
        self.assertEqual(got["Wide"], {"clearance_mm": 0.3})


if __name__ == "__main__":
    unittest.main()
