"""Export: contours, keyholes, rasters, the width and space check, KiCad and Touchstone (§10)."""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from yapnr.rf.export.contour import (
    islands,
    merge_collinear,
    point_in_loop,
    signed_area,
    simplify_loop,
    trace_loops,
)
from yapnr.rf.export.drc import check_width_space
from yapnr.rf.export.kicad import Footprint, PortPad, read_footprint, write_footprint
from yapnr.rf.export.raster import edt, label, rasterize
from yapnr.rf.export.touchstone import read_touchstone, write_touchstone


def _pixels(polys, shape):
    ni, nj = shape
    return rasterize(polys, np.arange(ni) + 0.5, np.arange(nj) + 0.5)


class ContourTest(unittest.TestCase):
    def test_square_is_a_chamfered_square(self):
        m = np.zeros((4, 4), bool)
        m[1:3, 1:3] = True
        (loop,) = trace_loops(m)
        loop = merge_collinear(loop)
        self.assertEqual(loop.shape[0], 8)  # four sides, four half-pixel chamfers
        self.assertAlmostEqual(signed_area(loop), 4.0 - 4 * 0.125)

    def test_ring_nests_into_outer_and_hole(self):
        m = np.zeros((7, 7), bool)
        m[1:6, 1:6] = True
        m[3, 3] = False
        (isl,) = islands(m)
        self.assertEqual(len(isl.holes), 1)
        self.assertGreater(signed_area(isl.outer), 0)
        self.assertLess(signed_area(isl.holes[0]), 0)
        np.testing.assert_array_equal(_pixels([isl.polygon], m.shape), m)

    def test_diagonal_pixels_are_one_island(self):
        m = np.zeros((4, 4), bool)
        m[1, 1] = m[2, 2] = True
        self.assertEqual(len(islands(m)), 1)
        m[2, 2] = False
        m[3, 3] = True
        self.assertEqual(len(islands(m)), 2)

    def test_random_round_trip(self):
        rng = np.random.default_rng(0)
        for _ in range(200):
            ni, nj = rng.integers(2, 22, 2)
            m = rng.random((ni, nj)) < rng.uniform(0.2, 0.8)
            polys = [i.polygon for i in islands(m)]
            np.testing.assert_array_equal(_pixels(polys, m.shape), m)

    def test_fine_area_matches_polygon_area(self):
        rng = np.random.default_rng(1)
        m = rng.random((16, 16)) < 0.55
        isl = islands(m, simplify_tol=0.0)
        area = sum(i.area for i in isl)
        k = 8
        s = (np.arange(16 * k) + 0.5) / k
        fine = rasterize([i.polygon for i in isl], s, s)
        self.assertAlmostEqual(fine.sum() / k**2, area, delta=0.005 * area)

    def test_simplification_tolerance(self):
        t = np.linspace(0, 10, 41)[:-1]
        wiggle = 0.05 * (-1.0) ** np.arange(t.size)
        raw = np.vstack(
            [
                np.column_stack([t, wiggle]),
                np.column_stack([10 + wiggle, t]),
                np.column_stack([10 - t, 10 + wiggle]),
                np.column_stack([wiggle, 10 - t]),
            ]
        )
        simple = simplify_loop(raw, 0.125)
        self.assertLessEqual(simple.shape[0], 8)
        # Every original vertex lies within the tolerance of the simplified boundary.
        a, b = simple, np.roll(simple, -1, axis=0)
        d = b - a
        for p in raw:
            u = np.clip(np.einsum("ij,ij->i", p - a, d) / np.einsum("ij,ij->i", d, d), 0, 1)
            dist = np.hypot(*(a + u[:, None] * d - p).T).min()
            self.assertLessEqual(dist, 0.125 + 1e-12)
        m = np.zeros((40, 16), bool)
        for i in range(40):  # staircase edges keep the pixels after simplification
            m[i, : 3 + i // 3] = True
        np.testing.assert_array_equal(_pixels([i.polygon for i in islands(m)], m.shape), m)

    def test_point_in_loop(self):
        sq = np.array([[0, 0], [2, 0], [2, 2], [0, 2]], float)
        self.assertTrue(point_in_loop((1, 1), sq))
        self.assertFalse(point_in_loop((3, 1), sq))


class RasterTest(unittest.TestCase):
    def test_label(self):
        m = np.array([[1, 1, 0, 1], [0, 1, 0, 1], [1, 0, 0, 0]], bool)
        lab, n = label(m)
        self.assertEqual(n, 3)
        self.assertEqual(lab[0, 0], lab[1, 1])
        self.assertNotEqual(lab[0, 0], lab[2, 0])  # diagonal only: separate (4-connected)

    def test_edt_matches_brute_force(self):
        rng = np.random.default_rng(2)
        m = rng.random((13, 17)) < 0.1
        d = edt(m)
        pts = np.argwhere(m)
        ii, jj = np.meshgrid(np.arange(13), np.arange(17), indexing="ij")
        brute = np.min(np.hypot(ii[..., None] - pts[:, 0], jj[..., None] - pts[:, 1]), axis=-1)
        np.testing.assert_allclose(d, brute, atol=1e-12)
        self.assertTrue(np.all(np.isinf(edt(np.zeros((3, 3), bool)))))


class DRCTest(unittest.TestCase):
    PITCH = 0.3

    def _check(self, m):
        polys = [i.polygon * self.PITCH for i in islands(m)]
        bbox = (0.0, m.shape[0] * self.PITCH, 0.0, m.shape[1] * self.PITCH)
        return check_width_space(polys, bbox, self.PITCH, 0.6, 0.6)

    def test_minimum_features_pass(self):
        n = 20
        m = np.zeros((n, n), bool)
        m[2:18, 8:10] = True  # a 2-pixel (0.6 mm) line
        self.assertTrue(self._check(m).ok)
        m = np.zeros((n, n), bool)
        m[2:18, 4:8] = m[2:18, 10:14] = True  # a 2-pixel gap
        self.assertTrue(self._check(m).ok)
        m = np.zeros((n, n), bool)
        m[2:18, 2:18] = True
        m[8:10, 8:10] = False  # a 2-pixel hole
        self.assertTrue(self._check(m).ok)

    def test_violations_flagged(self):
        n = 20
        cases = {}
        m = np.zeros((n, n), bool)
        m[2:18, 9] = True
        cases["thin line"] = (m, "width")
        m = np.zeros((n, n), bool)
        m[2:18, 4:8] = m[2:18, 9:13] = True
        cases["narrow gap"] = (m, "space")
        m = np.zeros((n, n), bool)
        m[2:10, 2:10] = m[10:18, 10:18] = True
        cases["diagonal neck"] = (m, "width")
        m = np.zeros((n, n), bool)
        m[2:18, 2:18] = True
        m[9, 9] = False
        cases["pin hole"] = (m, "space")
        m = np.zeros((n, n), bool)
        m[2:18, 2:18] = True
        m[9, 2:12] = False
        cases["slot"] = (m, "space")
        for name, (m, kind) in cases.items():
            r = self._check(m)
            self.assertFalse(r.ok, name)
            self.assertIn(kind, {v.kind for v in r.violations}, name)


def _footprint():
    sq = lambda x0, y0, s: np.array(  # noqa: E731
        [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s]], float
    )
    ring = np.array(
        [[0, 0], [3, 0], [3, 1], [1, 1], [1, 2], [3, 2], [3, 1], [3, 0], [4, 0], [4, 3], [0, 3]],
        float,
    )
    return Footprint(
        name="RF_test",
        origin=(2.0, 1.5),
        region=(0.0, 4.0, 0.0, 3.0),
        pads=[
            PortPad(1, (0.3, 1.5), (0.6, 0.6)),
            PortPad(2, (3.7, 1.5), (0.6, 0.6)),
            PortPad(3, (2.0, 0.3), (0.6, 0.6)),
        ],
        islands=[(ring, [1, 2]), (sq(1.7, 0.0, 0.6), [3]), (sq(1.5, 2.4, 0.3), [])],
        description="test footprint",
        seed="abc",
    )


class KiCadTest(unittest.TestCase):
    def test_round_trip(self):
        fp = _footprint()
        text = write_footprint(fp)
        r = read_footprint(text)
        self.assertEqual(r.name, "RF_test")
        self.assertEqual(r.net_tie_groups, ["1, 2"])
        self.assertEqual([p["number"] for p in r.pads], ["1", "2", "3"])
        self.assertEqual([p["shape"] for p in r.pads], ["rect", "rect", "custom"])
        # KiCad coordinates: origin at the region centre, y down.
        ring = fp.islands[0][0]
        np.testing.assert_allclose(
            r.polygons[0], np.column_stack([ring[:, 0] - 2.0, 1.5 - ring[:, 1]])
        )
        prim = r.pads[2]["primitives"][0]
        sq = fp.islands[1][0]
        np.testing.assert_allclose(prim, np.column_stack([sq[:, 0] - 2.0, 1.5 - sq[:, 1]]))
        self.assertEqual(len(r.polygons), 2)  # the net-tie ring and the free island

    def test_deterministic_uuids(self):
        a, b = write_footprint(_footprint()), write_footprint(_footprint())
        self.assertEqual(a, b)
        other = _footprint()
        other.seed = "def"
        self.assertNotEqual(write_footprint(other), a)

    def test_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.kicad_mod")
            write_footprint(_footprint(), path)
            self.assertEqual(read_footprint(path).name, "RF_test")


class TouchstoneTest(unittest.TestCase):
    def test_round_trip(self):
        rng = np.random.default_rng(3)
        f = np.linspace(8e9, 12e9, 5)
        for n in (1, 2, 3, 5):
            s = rng.standard_normal((5, n, n)) + 1j * rng.standard_normal((5, n, n))
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, f"t.s{n}p")
                text = write_touchstone(path, f, s, comments=["hello"])
                ff, ss, z = read_touchstone(path)
            np.testing.assert_allclose(ff, f)
            np.testing.assert_allclose(ss, s, rtol=1e-8, atol=1e-12)
            self.assertEqual(z, 50.0)
            self.assertIn("# GHz S RI R 50", text)
            if n == 2:  # Touchstone's two-port order: S11 S21 S12 S22
                first = text.splitlines()[2].split()
                self.assertAlmostEqual(float(first[3]), s[0, 1, 0].real, places=8)


if __name__ == "__main__":
    unittest.main()
