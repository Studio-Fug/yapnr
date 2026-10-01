"""The re-validation raster and the case criteria (design §11.5; `validate`, `cases`).

- the footprint raster ("inside or on" at pixel centres) reproduces the binary design pixel for
  pixel on the optimization grid, for random designs written through the KiCad footprint;
- on a grid twice as fine, a straight-edged shape maps to its 2 × 2 blocks, a concave corner
  gains exactly the one fine pixel its chamfer half covers, and a mirror-symmetric design stays
  mirror symmetric;
- the port pad widths come back in cells;
- the criteria evaluate |S| limits, imbalance, the radiated fraction and passivity, and every
  preset spec builds at both scales.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from yapnr.rf import cases
from yapnr.rf.export.contour import islands, point_in_loop
from yapnr.rf.export.kicad import Footprint, PortPad, read_footprint, write_footprint
from yapnr.rf.spec import Band, GridSpec, Port, S, Spec, StackupSpec
from yapnr.rf.validate import footprint_mask, pad_widths

PITCH = 0.5


def _spec(ni: int, nj: int) -> Spec:
    return Spec(
        name="t",
        stackup=StackupSpec(3.55, 0.0027, 0.813, 10.0),
        grid=GridSpec(pitch_mm=PITCH, substrate_cells=2),
        design_region=(0.0, ni * PITCH, 0.0, nj * PITCH),
        ports=(Port(1, "W", 1.0), Port(2, "E", 1.5)),
        bands={"b": Band(9.0, 11.0, 3)},
        requirements=(S(2, 1).at_least_db(-1.0, band="b"),),
    )


def _footprint(mask: np.ndarray, spec: Spec, touched=()):
    """Write `mask` (window pixels) as a footprint with port pads 1 (2 cells wide) and 2 (4
    cells) and read it back; islands containing pixel (i, j) of `touched` [((i, j), pad)] are
    marked as touching that pad (a custom pad)."""
    x0, x1, y0, y1 = spec.design_region
    shapes = []
    for isl in islands(mask):
        pads = [n for (i, j), n in touched if point_in_loop((i + 0.5, j + 0.5), isl.polygon)]
        poly = np.column_stack([x0 + isl.polygon[:, 0] * PITCH, y0 + isl.polygon[:, 1] * PITCH])
        shapes.append((poly, pads))
    fp = Footprint(
        name="t",
        origin=(0.5 * (x0 + x1), 0.5 * (y0 + y1)),
        region=(x0, x1, y0, y1),
        pads=[
            PortPad(1, (x0 + PITCH, 1.0), (2 * PITCH, 2 * PITCH)),
            PortPad(2, (x1 - PITCH, 1.5), (2 * PITCH, 4 * PITCH)),
        ],
        islands=shapes,
        seed="t",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "f.kicad_mod")
        write_footprint(fp, path)
        return read_footprint(path)


def _with_pads(mask: np.ndarray) -> np.ndarray:
    m = mask.copy()
    m[0:2, 1:3] = True  # pad 1: x 0..1 mm, y 0.5..1.5 mm
    m[-2:, 1:5] = True  # pad 2: y 0.5..2.5 mm
    return m


class RasterTest(unittest.TestCase):
    def test_random_designs_round_trip_on_the_same_grid(self):
        rng = np.random.default_rng(3)
        spec = _spec(12, 9)
        for _ in range(25):
            m = _with_pads(rng.random((12, 9)) < 0.5)
            got = footprint_mask(_footprint(m, spec), spec, PITCH)
            np.testing.assert_array_equal(got, m)

    def test_finer_grid(self):
        spec = _spec(12, 9)
        rect = np.zeros((12, 9), bool)
        rect[3:8, 4:7] = True
        fine = footprint_mask(_footprint(_with_pads(rect), spec), spec, PITCH / 2)
        np.testing.assert_array_equal(fine, np.kron(_with_pads(rect), np.ones((2, 2), bool)))
        ell = np.zeros((12, 9), bool)
        ell[4:9, 6:8] = True
        ell[4:6, 3:6] = True
        m = _with_pads(ell)
        fine = footprint_mask(_footprint(m, spec), spec, PITCH / 2)
        base = np.kron(m, np.ones((2, 2), bool))
        self.assertTrue(np.all(fine >= base))
        extra = np.argwhere(fine & ~base)
        # the L's concave corner at pixel (6, 5): its fine pixel nearest the corner node (6, 6)
        self.assertEqual(extra.tolist(), [[12, 11]])

    def test_mirror_symmetry_is_kept(self):
        rng = np.random.default_rng(5)
        nj = 10
        spec = Spec(
            name="t",
            stackup=StackupSpec(3.55, 0.0027, 0.813, 10.0),
            grid=GridSpec(pitch_mm=PITCH, substrate_cells=2),
            design_region=(0.0, 6.0, -2.5, 2.5),
            ports=(Port(1, "W", 0.0),),
            bands={"b": Band(9.0, 11.0, 3)},
            requirements=(S(1, 1).at_most_db(-10, band="b"),),
        )
        for _ in range(10):
            half = rng.random((12, nj // 2)) < 0.5
            m = np.concatenate([half, half[:, ::-1]], axis=1)
            shapes = [
                (np.column_stack([i.polygon[:, 0] * PITCH, -2.5 + i.polygon[:, 1] * PITCH]), [])
                for i in islands(m)
            ]
            fp = Footprint("t", (3.0, 0.0), spec.design_region, [], shapes, seed="t")
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "f.kicad_mod")
                write_footprint(fp, path)
                fine = footprint_mask(read_footprint(path), spec, PITCH / 2)
            np.testing.assert_array_equal(fine, fine[:, ::-1])

    def test_pad_widths(self):
        spec = _spec(12, 9)
        m = _with_pads(np.zeros((12, 9), bool))
        fp = _footprint(m, spec)
        self.assertEqual(pad_widths(fp, spec), {1: 2, 2: 4})
        m[5:10, 0:7] = True  # an island on pad 2 only: pad 2 becomes a custom pad
        fp = _footprint(m, spec, touched=[((11, 2), 2)])
        self.assertEqual([p["shape"] for p in fp.pads], ["rect", "custom"])
        self.assertEqual(pad_widths(fp, spec), {1: 2, 2: 4})
        np.testing.assert_array_equal(footprint_mask(fp, spec, PITCH), m)


class CriteriaTest(unittest.TestCase):
    def test_checks(self):
        f = np.array([8.0, 9.0, 10.0, 11.0]) * 1e9
        s = np.zeros((4, 3, 3), complex)
        s[:, 0, 0] = 10 ** (np.array([-12.0, -18.0, -20.0, -16.0]) / 20)
        s[:, 1, 0] = 10 ** (-3.3 / 20)
        s[:, 2, 0] = 10 ** (np.array([-3.4, -3.4, -3.5, -3.3]) / 20)
        r = cases.Check("a", "s_max", (1, 1), -15.0, (9.0, 11.0)).evaluate(f, s)
        self.assertAlmostEqual(r["worst"], -16.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["points"], 3)
        r = cases.Check("b", "s_min", (3, 1), -3.45, (9.0, 11.0)).evaluate(f, s)
        self.assertAlmostEqual(r["worst"], -3.5)
        self.assertFalse(r["ok"])
        r = cases.Check("c", "imbalance", (2, 1, 3, 1), 0.25).evaluate(f, s)
        self.assertAlmostEqual(r["worst"], 0.2)
        r = cases.Check("d", "eta_min", (1,), 0.6, at_ghz=(9.0, 11.0)).evaluate(
            f, s, {1: np.array([0.1, 0.7, 0.2, 0.65])}
        )
        self.assertAlmostEqual(r["worst"], 0.65)
        self.assertEqual(r["points"], 2)
        r = cases.Check("e", "passivity", limit=-1e-3).evaluate(f, s)
        self.assertTrue(r["ok"])
        s[0, 1, 0] = 1.2
        self.assertFalse(cases.Check("e", "passivity", limit=-1e-3).evaluate(f, s)["ok"])
        with self.assertRaises(ValueError):
            cases.Check("x", "s_max", (1, 1), 0.0, (20.0, 21.0)).evaluate(f, s)

    def test_presets_build(self):
        for name in cases.CASES:
            for scale in cases.SCALES:
                spec = cases.spec_for(name, scale)
                self.assertEqual(cases.case_of(spec), name)
                self.assertEqual(Spec.from_dict(spec.to_dict()).sha256(), spec.sha256())
                freqs = cases.sweep_frequencies(name, spec)
                for level in ("coarse", "fine"):
                    for c in cases.CRITERIA[name][level]:
                        self.assertTrue(c.mask(freqs).any(), (name, c.name))


if __name__ == "__main__":
    unittest.main()
