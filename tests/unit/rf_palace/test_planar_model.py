"""yapnr-planar-v1 documents: validation, rings and arcs, port faces, the hash, fit_box and the
stack presets. Stdlib only (the generated lines need no shapely)."""

from __future__ import annotations

import copy
import math
import unittest

from yapnr.rf.planar import adapters, model, stackups


def _doc():
    """A 4 x 2 mm microstrip over a PEC floor with two wave ports."""
    d = model.new("t", [0.0, 4.0, -1.0, 1.0, 0.0, 1.0], {"zmin": "pec"})
    d["stack"]["dielectrics"].append(dict(name="core", z0=0.0, z1=0.1, eps_r=3.5, tan_d=0.003))
    d["stack"]["layers"].append(
        dict(name="L1", z=0.1, t=0.035, sigma=5.8e7, rough_k=1.5, model="sheet")
    )
    d["conductors"].append(
        dict(
            layer="L1",
            net="S",
            polygons=[dict(outer=[[0, -0.1], [4, -0.1], [4, 0.1], [0, 0.1]], holes=[])],
        )
    )
    for name, x, direction in (("P1", 0.5, "+x"), ("P2", 3.5, "-x")):
        d["ports"].append(
            dict(
                name=name,
                kind="wave",
                net="S",
                layer="L1",
                ref="zmin",
                at=[x, 0.0],
                dir=direction,
                width=0.2,
                z0=50.0,
                excite=name == "P1",
            )
        )
    return d


class ValidateTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(model.validate(_doc()), [])

    def test_findings(self):
        cases = [
            (lambda d: d.update(schema="x"), "schema"),
            (lambda d: d["domain"]["boundaries"].update(xmin="open"), "boundaries.xmin"),
            (lambda d: d["conductors"][0].update(layer="L9"), "unknown layer"),
            (
                lambda d: d["vias"].append(
                    {"at": [0.02, 0.5], "drill": 0.15, "from": "zmin", "to": "L1", "net": "G"}
                ),
                "crosses",
            ),
            (
                lambda d: d["vias"].append(
                    {"at": [2.0, 0.5], "drill": 0.15, "from": "L1", "to": "zmin", "net": "G"}
                ),
                "below",
            ),
            (lambda d: d["domain"]["boundaries"].update(xmin="pec"), "PEC"),
            (lambda d: d["ports"][1].update(name="P1"), "repeated"),
            (lambda d: d["stack"]["layers"][0].update(model="solid", z=0.05), "overlap"),
            (
                lambda d: d["conductors"][0]["polygons"][0].update(
                    outer=[[0, 0], {"mid": [1, 1]}, {"mid": [2, 2]}]
                ),
                "bad polygon",
            ),
            (
                lambda d: d["conductors"][0]["polygons"][0]["outer"].__setitem__(1, [9.0, -0.1]),
                "outside the domain",
            ),
        ]
        for mutate, text in cases:
            d = _doc()
            mutate(d)
            errs = model.validate(d)
            self.assertTrue(any(text in e for e in errs), (text, errs))
        with self.assertRaises(model.PlanarError):
            model.check(dict(_doc(), schema="nope"))


class RingTest(unittest.TestCase):
    def test_circle_through(self):
        cx, cy, r = model.circle_through((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0))
        self.assertAlmostEqual(cx, 0.0)
        self.assertAlmostEqual(cy, 0.0)
        self.assertAlmostEqual(r, 1.0)

    def test_arc_ring_area(self):
        # a disc of radius 0.5 as two half arcs, and a stadium (two arcs, two lines)
        disc = [[0.5, 0.0], {"mid": [0.0, 0.5]}, [-0.5, 0.0], {"mid": [0.0, -0.5]}]
        pts = model.ring_points(disc, chord=0.001)
        self.assertAlmostEqual(abs(model.ring_area(pts)), math.pi * 0.25, delta=2e-5)
        for p in pts:
            self.assertAlmostEqual(math.hypot(*p), 0.5, places=9)
        stadium = [
            [0, -0.1],
            [1, -0.1],
            {"mid": [1.1, 0.0]},
            [1, 0.1],
            [0, 0.1],
            {"mid": [-0.1, 0.0]},
        ]
        area = abs(model.ring_area(model.ring_points(stadium, chord=0.0005)))
        self.assertAlmostEqual(area, 0.2 + math.pi * 0.01, delta=1e-5)
        kinds = [e[0] for e in model.ring_edges(stadium)]
        self.assertEqual(kinds, ["line", "arc", "line", "arc"])

    def test_arc_direction_follows_mid(self):
        # the same ends with mid on the other side sweep the other way round
        a = model.ring_points([[1, 0], {"mid": [0, 1]}, [-1, 0], [-1, -2], [1, -2]], chord=0.01)
        b = model.ring_points([[1, 0], {"mid": [0, -1]}, [-1, 0], [-1, -2], [1, -2]], chord=0.01)
        self.assertGreater(max(p[1] for p in a), 0.99)
        self.assertLess(max(p[1] for p in b), 1e-9)


class PortTest(unittest.TestCase):
    def test_wave_faces(self):
        g = model.port_geometry(_doc())
        p1, p2 = g["P1"], g["P2"]
        self.assertEqual((p1["wall"], p2["wall"]), ("xmin", "xmax"))
        self.assertAlmostEqual(p1["offset"], 0.5)
        self.assertAlmostEqual(p2["offset"], 0.5)
        # 0.8 either side, cut at the +-1 mm domain edges? no: 0.8 < 1
        self.assertEqual(p1["lat"], [-0.8, 0.8])
        self.assertAlmostEqual(p1["z"][1], 0.1 + model.FACE_DEFAULT["height"])
        for corner in p1["rect"]:
            self.assertEqual(corner[0], 0.0)
        # voltage path: strip down to the reference
        self.assertEqual(p1["voltage_path"], [[0.0, 0.0, 0.1], [0.0, 0.0, 0.0]])

    def test_faces_share_a_wall(self):
        d = _doc()
        d["conductors"][0]["polygons"].append(
            dict(outer=[[0, 0.5], [4, 0.5], [4, 0.7], [0, 0.7]], holes=[])
        )
        d["ports"].append(
            dict(
                name="P3",
                kind="wave",
                net="S",
                layer="L1",
                ref="zmin",
                at=[0.5, 0.6],
                dir="+x",
                width=0.2,
            )
        )
        g = model.port_geometry(d)
        self.assertAlmostEqual(g["P1"]["lat"][1], 0.3)  # halfway between the lines
        self.assertAlmostEqual(g["P3"]["lat"][0], 0.3)
        self.assertAlmostEqual(g["P3"]["lat"][1], 1.0)  # cut at the domain edge
        d["ports"][2]["at"] = [0.5, 0.15]  # too close: the faces would not clear the line
        with self.assertRaises(model.PlanarError):
            model.port_geometry(d)

    def test_lumped(self):
        d = _doc()
        d["ports"][0].update(kind="lumped")
        g = model.port_geometry(d)["P1"]
        self.assertEqual(
            g["rect"], [[0.5, -0.1, 0.0], [0.5, 0.1, 0.0], [0.5, 0.1, 0.1], [0.5, -0.1, 0.1]]
        )


class HashTest(unittest.TestCase):
    def test_hash_tracks_geometry_only(self):
        a = _doc()
        b = copy.deepcopy(a)
        b["provenance"]["note"] = "x"
        b["mesh"]["edge_h"] = 0.01
        self.assertEqual(model.geometry_hash(a), model.geometry_hash(b))
        b["stack"]["dielectrics"][0]["eps_r"] = 3.6
        self.assertNotEqual(model.geometry_hash(a), model.geometry_hash(b))

    def test_layer_model(self):
        d = model.with_layer_model(_doc(), "pec")
        self.assertEqual(d["stack"]["layers"][0]["model"], "pec")
        self.assertEqual(_doc()["stack"]["layers"][0]["model"], "sheet")


class LinesAndStackTest(unittest.TestCase):
    def test_hammerstad(self):
        # Rq 0.4 um at 62.05 GHz: delta 0.265 um, K 1.81 (the openEMS runs' sigma/K^2)
        self.assertAlmostEqual(stackups.skin_depth(62.05e9) * 1e6, 0.2652, places=3)
        self.assertAlmostEqual(stackups.hammerstad_k(0.4, 62.05e9), 1.806, places=3)
        self.assertAlmostEqual(stackups.hammerstad_k(0.0, 62.05e9), 1.0)

    def test_window_stack(self):
        diel, layers = stackups.radar60("window")
        z = {la["name"]: la["z"] for la in layers}
        self.assertAlmostEqual(z["L2"], 0.1135)
        self.assertAlmostEqual(z["L1"], 0.2151)
        self.assertEqual([d["name"] for d in diel], ["RO4450F", "RO4835"])

    def test_lines(self):
        msl = adapters.line_model("msl", length=5.0)
        gcpw = adapters.line_model("gcpw", length=10.0)
        self.assertEqual(model.validate(msl), [])
        self.assertEqual(model.validate(gcpw), [])
        self.assertEqual(msl["domain"]["box"][:2], [0.0, 6.0])
        g = model.port_geometry(gcpw)
        self.assertAlmostEqual(
            g["P2"]["plane"] - g["P1"]["plane"] - g["P1"]["offset"] - g["P2"]["offset"], 10.0
        )
        xs = sorted({v["at"][0] for v in gcpw["vias"]})
        self.assertGreaterEqual(xs[0], 0.45 - 1e-9)
        self.assertLessEqual(xs[-1], 11.0 - 0.45 + 1e-9)
        self.assertTrue(all(abs(abs(v["at"][1]) - 0.5) < 1e-12 for v in gcpw["vias"]))
        self.assertEqual({c["net"] for c in gcpw["conductors"]}, {"SIG", "GND"})


class FitBoxTest(unittest.TestCase):
    def test_moves_off_barrels(self):
        # a via straddling xmin and one just inside xmax; a copper vertex 20 um from ymax
        box, notes = adapters.fit_box(
            [0.0, 4.0, 0.0, 2.0], [(0.02, 1.0, 0.15), (3.9, 1.0, 0.15)], points=[(2.0, 1.98)]
        )
        x0, x1, y0, y1 = box
        self.assertTrue(x0 <= 0.02 - 0.075 - 0.05 + 1e-9 or x0 >= 0.02 + 0.075 - 1e-9)
        self.assertTrue(x1 >= 3.9 + 0.075 + 0.05 - 1e-9)
        self.assertTrue(y1 >= 1.98 + 0.05 - 1e-9 or abs(y1 - 1.98) < 1e-9)
        self.assertEqual(len(notes), 3)
        same, none = adapters.fit_box([0.0, 4.0, 0.0, 2.0], [(2.0, 1.0, 0.15)])
        self.assertEqual(same, [0.0, 4.0, 0.0, 2.0])
        self.assertEqual(none, [])


if __name__ == "__main__":
    unittest.main()
