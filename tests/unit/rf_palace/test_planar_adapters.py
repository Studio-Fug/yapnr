"""The cleaner and the shapely-based adapters: arc refitting, the radar60 patch and feed models,
and the KiCad reader on a small synthetic board. Needs shapely (the FEA environment): skipped
elsewhere, and manual under Bazel.

    python -m unittest tests/unit/rf_palace/test_planar_adapters.py   # in the FEA environment
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest

try:
    import shapely  # noqa: F401
    from shapely.geometry import Point, box

    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

from yapnr.rf.planar import adapters, model

# The single patch as the radar60 record describes it (rfm1-n: W 1.45, calibrated L 1.1506,
# inset 0.30, notch 0.10, 0.2 mm line, window margin 0.15, spacing 2.9).
RECORD = {
    "schema": "radar60-rfmacro/1",
    "geometry_sha256": "0" * 64,
    "params": {"notch": 0.1, "w50": 0.2, "window_margin": 0.15, "spacing": 2.9},
    "dims": {"lambda0_mm": 4.8315, "patch": {"w": 1.45, "l": 1.1506, "inset": 0.3}},
}

BOARD = """(kicad_pcb (version 20260206) (generator "pcbnew")
  (segment (start 101 99) (end 103 99) (width 0.2) (layer "F.Cu") (net "SIG") (uuid "a"))
  (arc (start 103 99) (mid 103.70711 98.70711) (end 104 98) (width 0.2) (layer "F.Cu") (net "SIG") (uuid "b"))
  (segment (start 101 99) (end 103 99) (width 0.2) (layer "B.Cu") (net "SIG") (uuid "c"))
  (via (at 102 97) (size 0.3) (drill 0.15) (layers "F.Cu" "B.Cu") (net "GND") (uuid "d"))
  (zone (net "GND") (net_name "GND") (layer "F.Cu") (uuid "e")
    (filled_polygon (layer "F.Cu")
      (pts (xy 100 96) (xy 100.5 96) (xy 101 96) (xy 105 96) (xy 105 97.5) (xy 100 97.5)))))
"""


@unittest.skipUnless(HAVE_SHAPELY, "shapely not installed (FEA environment only)")
class CleanTest(unittest.TestCase):
    def test_refit_circle(self):
        from yapnr.rf.planar import clean

        pts = [
            (0.3 * math.cos(2 * math.pi * k / 90), 0.3 * math.sin(2 * math.pi * k / 90))
            for k in range(90)
        ]
        ring = clean.refit_arcs(pts, tol=0.001)
        mids = [it for it in ring if isinstance(it, dict)]
        self.assertGreaterEqual(len(mids), 2)
        self.assertLess(len(ring), 12)
        area = abs(model.ring_area(model.ring_points(ring, chord=0.0005)))
        self.assertAlmostEqual(area, math.pi * 0.09, delta=2e-5)

    def test_refit_keeps_corners(self):
        from yapnr.rf.planar import clean

        # a rounded rectangle: four straight sides stay lines, the four corners become arcs
        g = box(0, 0, 2, 1).buffer(0.2, quad_segs=24)
        polys = clean.clean_geometry(g)
        self.assertEqual(len(polys), 1)
        ring = polys[0]["outer"]
        self.assertEqual(sum(1 for it in ring if isinstance(it, dict)), 4)
        self.assertLess(clean.cleaning_change(g, polys), 2e-4)

    def test_merge_and_simplify(self):
        from yapnr.rf.planar import clean

        # KiCad's fills carry clusters of vertices a micrometre apart and collinear runs
        pts = [(0, 0), (0.5, 0), (0.5000005, 0.0000005), (1, 0), (1, 1), (0, 1)]
        from shapely.geometry import Polygon

        out = clean.clean_geometry(Polygon(pts), refit=False)
        self.assertEqual(len(out[0]["outer"]), 4)

    def test_overlaps_and_slivers(self):
        from yapnr.rf.planar import clean

        d = adapters.line_model("gcpw", length=2.0)
        self.assertEqual(clean.overlaps(d), [])
        d["conductors"][1]["polygons"].append(
            dict(outer=[[0.5, -0.05], [0.6, -0.05], [0.6, 0.4], [0.5, 0.4]], holes=[])
        )
        found = clean.overlaps(d)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][:3], ("L1", "SIG", "GND"))
        tongue = box(0, 0, 3, 0.2).union(box(0, 0, 0.6, 2))
        thin = clean.thin_parts(tongue, 0.32)
        self.assertEqual(len(thin), 1)


@unittest.skipUnless(HAVE_SHAPELY, "shapely not installed (FEA environment only)")
class PatchTest(unittest.TestCase):
    def test_w(self):
        d = adapters.patch_from_record(RECORD, variant="w")
        self.assertEqual(model.validate(d), [])
        from yapnr.rf.planar import clean

        rf = clean.to_shapely(d["conductors"][0]["polygons"])
        # patch - notch + line from the wall to the notch bottom (+0.02 overlap into the patch)
        edge = 1.45 - 1.1506 / 2
        p1y = edge - 0.6
        line = (edge + 0.3 - (p1y - 1.0)) * 0.2
        expect = 1.45 * 1.1506 - 0.4 * 0.3 + line
        self.assertAlmostEqual(rf.area, expect, delta=1e-6)
        g = model.port_geometry(d)["P1"]
        self.assertEqual(g["wall"], "ymin")
        self.assertAlmostEqual(g["offset"], 1.0)
        self.assertAlmostEqual(g["z_ref"], 0.1135)  # the line refers to L2, not L3
        l2 = clean.to_shapely(d["conductors"][1]["polygons"])
        self.assertEqual(len(l2.interiors), 1)  # the window
        win = l2.interiors[0].bounds
        self.assertAlmostEqual(win[2] - win[0], 1.45 + 0.3)
        self.assertAlmostEqual(
            d["domain"]["box"][2], d["stack"]["dielectrics"][0]["outline"][0][1]
        )  # board meets the wall

    def test_finite(self):
        d = adapters.patch_from_record(RECORD, variant="finite")
        self.assertEqual(model.validate(d), [])
        self.assertEqual(d["ports"][0]["kind"], "lumped")
        g = model.port_geometry(d)["P1"]
        self.assertAlmostEqual(g["z"][1] - g["z"][0], 0.1016)
        self.assertGreater(
            d["stack"]["dielectrics"][0]["outline"][0][1], d["domain"]["box"][2]
        )  # air all round


@unittest.skipUnless(HAVE_SHAPELY, "shapely not installed (FEA environment only)")
class FeedModelTest(unittest.TestCase):
    def fm(self):
        # prep.py's shape: GND pour with a slot, split into hole-free pieces at x = 2 (inner and
        # zone lists), a line in two pieces, vias [x, y, drill], two ports
        def rect(x0, y0, x1, y1):
            return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

        return dict(
            model="tx12",
            variant="asis",
            box=[0, 4, 0, 3],
            inner=[0.5, 0.5, 3.5, 2.5],
            gnd=[
                [rect(0, 0, 2, 1.2), rect(0, 1.8, 2, 3)],
                [rect(2, 0, 4, 1.2), rect(2, 1.8, 4, 3)],
            ],
            nets={"TX1": [[rect(0, 1.4, 2, 1.6)], [rect(2, 1.4, 4, 1.6)]]},
            vias=[[1.0, 0.8, 0.15], [3.0, 2.2, 0.15], [5.0, 0.8, 0.15]],
            l2_windows=[],
            ports=[dict(name="TX1.P0", net="TX1", at=[1.0, 1.5], dir="x")],
            slivers=[dict(centroid=[1, 1], area=0.1, bounds=[0.9, 0.9, 1.1, 1.1])],
        )

    def test_north_port(self):
        fm = self.fm()
        fm["ports"] = [dict(name="TX1.P1", net="TX1", at=[3.0, 1.5], dir="y")]
        d = adapters.from_feedmodel(fm, box=[0.2, 3.8, 0.2, 2.8])
        g = model.port_geometry(d)["TX1.P1"]
        self.assertEqual((d["ports"][0]["dir"], g["wall"]), ("-y", "ymax"))
        self.assertAlmostEqual(g["offset"], 1.3)

    def test_convert(self):
        d = adapters.from_feedmodel(self.fm(), box=[0.2, 3.8, 0.2, 2.8])
        self.assertEqual(model.validate(d), [])
        from yapnr.rf.planar import clean

        gnd = clean.to_shapely(next(c for c in d["conductors"] if c["net"] == "GND")["polygons"])
        self.assertAlmostEqual(gnd.area, 3.6 * (2.6 - 0.6), delta=1e-6)  # seams merged, cropped
        self.assertEqual(len(clean.parts(gnd)), 2)
        self.assertEqual(len(d["vias"]), 2)  # the via outside the box is dropped
        self.assertEqual([p["dir"] for p in d["ports"]], ["+x"])
        self.assertAlmostEqual(model.port_geometry(d)["TX1.P0"]["offset"], 0.8)
        self.assertEqual(d["domain"]["boundaries"]["zmin"], "pec")
        self.assertEqual(d["features"][0]["name"], "sliver0")


@unittest.skipUnless(HAVE_SHAPELY, "shapely not installed (FEA environment only)")
class KicadTest(unittest.TestCase):
    def test_read(self):
        raw = adapters.read_kicad_copper(BOARD, {"F.Cu": "L1"}, frame=(100.0, 100.0, True))
        self.assertEqual(sorted(raw["copper"]), [("L1", "GND"), ("L1", "SIG")])
        sig = raw["copper"][("L1", "SIG")]
        # 2 mm straight + a quarter arc of radius 1 (length pi/2), 0.2 wide, with round ends
        expect = 0.2 * (2.0 + math.pi / 2) + math.pi * 0.01
        self.assertAlmostEqual(sig.area, expect, delta=2e-4)
        self.assertTrue(sig.contains(Point(2.0, 1.0)))  # y flipped: 100 - 99
        gnd = raw["copper"][("L1", "GND")]
        self.assertAlmostEqual(gnd.area, 5 * 1.5, delta=1e-3)  # the via pad lies inside the fill
        self.assertEqual(raw["vias"][0]["at"], [2.0, 3.0])

    def test_document(self):
        from yapnr.rf.planar import stackups

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.kicad_pcb")
            with open(path, "w") as fh:
                fh.write(BOARD)
            d = adapters.from_kicad(
                path,
                [0.5, 4.8, 0.2, 3.8],
                {"F.Cu": "L1"},
                stackups.radar60("feed"),
                frame=(100.0, 100.0, True),
            )
        self.assertEqual(model.validate(d), [])
        sig = next(c for c in d["conductors"] if c["net"] == "SIG")
        ring = sig["polygons"][0]["outer"]
        self.assertGreaterEqual(
            sum(1 for it in ring if isinstance(it, dict)), 2
        )  # the arc and an end cap refit
        self.assertEqual(len(d["vias"]), 1)
        self.assertLess(max(d["provenance"]["cleaning_xor_mm2"].values()), 5e-4)


if __name__ == "__main__":
    unittest.main()
