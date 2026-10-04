"""The board, far-field and pattern sections of the spec (design §25.3, §26), and the target
densities and direction sets (`patterns`).

- The radiation box: the removed feed-window keys are an error, the defaults serialize to {},
  `solver.port_extraction` "modal" (default) or "vi".
- A board spec in YAML (the 5.8 GHz omni demo's shape) round-trips with its hash; lumped ports,
  the ground's keepouts, the frame and the targets are kept.
- Refusals: pattern requirements without a board, line ports on a board, lumped ports without
  one, an unknown target, a bad hpbw span, the window keys, seeds on a board.
- Direction sets: points, conical and elevation cuts (the far side at negative θ), cones; a
  frame turns them.
- Targets: every preset and the grid and harmonic forms integrate to 4π by the quadrature, a
  beam's half-power width is its hpbw, an omni's is uniform in azimuth; the closed-form
  surface-wave share is 0.27 on S2 at 10 GHz.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import yaml

from yapnr.rf.farfield import Frame, sphere_quadrature
from yapnr.rf.patterns import PatternRequirement, parse_directions, parse_target
from yapnr.rf.spec import RadiationBox, Spec
from yapnr.rf.stackup import surface_wave_share
from yapnr.rf.testing import tiny_board_spec, tiny_spec

OMNI_YAML = """
schema: yapnr-rf-spec/1
name: omni-5g8
stackup: { er: 4.3, tan_delta: 0.02, h_mm: 0.246, f_ref_ghz: 5.8 }
grid: { pitch_mm: 0.3, substrate_cells: 2 }
design_region: { x_mm: [-14.4, 14.4], y_mm: [0.6, 13.8] }
symmetry: mirror_x
rules: { min_width_mm: 0.3, min_space_mm: 0.3 }
ports:
  - { n: 1, kind: lumped, x_mm: [-0.3, 0.3], y_mm: [-0.3, -0.3], ohms: 50 }
board:
  x_mm: [-15, 15]
  y_mm: [-46, 14]
  thickness_mm: 1.6
  ground: { x_mm: [-15, 15], y_mm: [-46, 0] }
  copper: [{ x_mm: [-0.3, 0.3], y_mm: [-0.3, 0.6] }]
far_field: { frame: { axis: "+y", zero: "+x" } }
patterns:
  omni: { preset: omni, axis: "+y", hpbw_deg: 90 }
bands:
  match: { ghz: [5.7, 5.9], points: 3 }
  pat: { lo_ghz: 5.725, hi_ghz: 5.875, points: 3, ghz_points: [5.725, 5.8, 5.875] }
requirements:
  - { s: [1, 1], max_db: -12, band: match }
  - { gain: 1, min_dbi: -1, directions: { cut: { theta_deg: 90, points: 12 } }, band: pat }
  - { ripple: 1, max_db: 5, directions: { cut: { theta_deg: 90, points: 36 } }, band: pat }
  - { efficiency: 1, kind: total, min: 0.6, band: pat }
  - { shape: 1, target: omni, max_rms_db: 3, band: pat }
"""


class SchemaTest(unittest.TestCase):
    def test_radiation_box_keys(self):
        self.assertEqual(RadiationBox().to_dict(), {})
        self.assertEqual(RadiationBox(clearance_cells=3, height_mm=4.0).to_dict(),
                         {"clearance_cells": 3, "height_mm": 4.0})  # fmt: skip
        with self.assertRaisesRegex(ValueError, "removed"):
            RadiationBox(window_margin_mm=1.0)
        with self.assertRaisesRegex(ValueError, "removed"):
            Spec.from_dict({**tiny_spec(radiated=True).to_dict(),
                            "radiation": {"window_height_mm": 3.0}})  # fmt: skip
        # The round-2 files (null window keys) still load.
        d = tiny_spec(radiated=True).to_dict()
        d["radiation"] = {"offset_mm": 2.4, "height_mm": 8.0, "window_margin_mm": None,
                          "window_height_mm": None}  # fmt: skip
        self.assertEqual(Spec.from_dict(d).radiation.offset_mm, 2.4)

    def test_port_extraction(self):
        spec = tiny_spec(extraction="modal")
        self.assertNotIn("port_extraction", spec.to_dict()["solver"])  # the default
        d = tiny_spec().to_dict()
        self.assertEqual(d["solver"]["port_extraction"], "vi")
        d["solver"]["port_extraction"] = "both"
        with self.assertRaises(ValueError):
            Spec.from_dict(d)

    def test_board_yaml_round_trip(self):
        spec = Spec.from_dict(yaml.safe_load(OMNI_YAML))
        self.assertEqual(spec.board.thickness_mm, 1.6)
        self.assertEqual(spec.ports[0].kind, "lumped")
        self.assertEqual(spec.frame, Frame("+y", "+x"))
        self.assertEqual(sorted(spec.patterns), ["omni"])
        self.assertEqual(
            [r.quantity for r in spec.requirements], ["s", "gain", "ripple", "efficiency", "shape"]
        )
        again = Spec.from_dict(spec.to_dict())
        self.assertEqual(again.sha256(), spec.sha256())
        labels = [r.label for r in spec.requirements]
        self.assertEqual(labels[1], "Gr1 ≥ -1 dBi @ pat")
        self.assertEqual(labels[3], "e_tot1 ≥ 0.6 @ pat")

    def test_tiny_board_round_trip(self):
        for ground in ("board", "keepout", "infinite"):
            spec = tiny_board_spec(ground=ground)
            self.assertEqual(Spec.from_dict(spec.to_dict()).sha256(), spec.sha256())

    def test_refusals(self):
        base = yaml.safe_load(OMNI_YAML)

        def bad(**changes):
            d = {**base, **changes}
            with self.assertRaises(ValueError) as cm:
                Spec.from_dict(d)
            return str(cm.exception)

        self.assertIn("board model", bad(board=None))
        self.assertIn("lumped", bad(ports=[{"n": 1, "side": "W", "at_mm": 0.0}]))
        reqs = base["requirements"][:-1] + [
            {"shape": 1, "target": "nope", "max_rms_db": 3, "band": "pat"}
        ]
        self.assertIn("unknown target", bad(requirements=reqs))
        reqs = base["requirements"] + [{"hpbw": 1, "between_deg": [90, 60], "band": "pat"}]
        bad(requirements=reqs)
        reqs = base["requirements"] + [{"gain": 1, "min_dbi": 0, "max_dbi": 3, "band": "pat",
                                        "directions": "sphere"}]  # fmt: skip
        bad(requirements=reqs)
        bad(optimizer={"seed": "star"})
        bad(radiation={"offset_mm": 1.0})
        # Pattern requirements on the infinite substrate.
        d = tiny_spec().to_dict()
        d["requirements"] = d["requirements"] + [
            {"gain": 1, "min_dbi": 0, "directions": {"point": {"theta_deg": 0}}, "band": "b"}
        ]
        with self.assertRaisesRegex(ValueError, "board model"):
            Spec.from_dict(d)


class DirectionsTest(unittest.TestCase):
    def test_sets(self):
        fr = Frame()
        p = parse_directions({"point": {"theta_deg": 30, "phi_deg": 90}}, fr)
        np.testing.assert_allclose(fr.directions(p.theta, p.phi), [[0, 0.5, math.sqrt(3) / 2]],
                                   atol=1e-12)  # fmt: skip
        c = parse_directions({"cut": {"theta_deg": 90, "points": 12}}, fr)
        self.assertEqual(c.size, 12)
        np.testing.assert_allclose(c.angle, np.arange(12) * 30.0)
        e = parse_directions({"cut": {"phi_deg": 0, "theta_deg": [-90, 90], "step_deg": 45}}, fr)
        v = fr.directions(e.theta, e.phi)
        np.testing.assert_allclose(v[0], [-1, 0, 0], atol=1e-12)  # θ = −90°: the far side
        np.testing.assert_allclose(v[-1], [1, 0, 0], atol=1e-12)
        cone = parse_directions(
            {"cone": {"toward": "-z", "half_angle_deg": 20, "step_deg": 10}}, fr
        )
        w = fr.directions(cone.theta, cone.phi)
        self.assertTrue(np.all(w[:, 2] <= -math.cos(math.radians(20)) + 1e-9))
        self.assertEqual(parse_directions("upper", fr).quadrature, "upper")

    def test_frame(self):
        fr = Frame("+y", "+x")
        np.testing.assert_allclose(fr.directions(0.0, 0.0), [0, 1, 0], atol=1e-12)
        np.testing.assert_allclose(fr.directions(math.pi / 2, 0.0), [1, 0, 0], atol=1e-12)
        np.testing.assert_allclose(fr.directions(math.pi / 2, math.pi / 2), [0, 0, -1], atol=1e-12)
        th, ph = fr.unit_vectors(math.pi / 2, 0.0)
        np.testing.assert_allclose(th, [0, -1, 0], atol=1e-12)
        np.testing.assert_allclose(ph, [0, 0, -1], atol=1e-12)
        with self.assertRaises(ValueError):
            Frame("+y", "+y")


class TargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dirs, cls.w, cls.theta, cls.phi = sphere_quadrature(40)

    def norm(self, d, upper=False):
        t = parse_target(d)
        q, _ = t.normalized(self.dirs, self.w, Frame())
        self.assertAlmostEqual(float(q @ self.w), 1.0, places=12)
        return q

    def test_beam(self):
        q = self.norm({"preset": "beam", "toward": "+z", "hpbw_deg": 70, "back_db": -40,
                       "floor_db": -40})  # fmt: skip
        # Half power at 35° off the beam.
        t = parse_target({"preset": "beam", "toward": "+z", "hpbw_deg": 70})
        v = t.density(np.array([[0, 0, 1.0], [math.sin(math.radians(35)), 0, math.cos(math.radians(35))]]),
                      Frame())  # fmt: skip
        self.assertAlmostEqual(v[1] / v[0], 0.5, places=9)
        # Its peak directivity: about 2(q + 1) = 9.5 dBi for hpbw 70° (q = 3.47).
        self.assertAlmostEqual(10 * math.log10(4 * math.pi * q.max()), 9.5, delta=0.2)

    def test_omni_is_uniform_in_azimuth(self):
        q = self.norm({"preset": "omni", "axis": "+z", "hpbw_deg": "dipole"})
        ring = np.abs(self.theta - self.theta[np.argmin(np.abs(self.theta - math.pi / 2))]) < 1e-12
        self.assertLess(np.ptp(q[ring]) / q[ring].mean(), 1e-9)
        self.assertAlmostEqual(4 * math.pi * q.max(), 1.5, delta=0.01)  # a dipole's 1.5

    def test_grid_and_harmonics(self):
        th = np.linspace(0, 180, 19)
        ph = np.arange(0, 360, 30.0)
        dbi = 10 * np.log10(np.maximum(1.5 * np.sin(np.radians(th)) ** 2, 1e-3))[:, None]
        q = self.norm({"grid": {"theta_deg": th.tolist(), "phi_deg": ph.tolist(),
                                "dbi": np.repeat(dbi, ph.size, 1).tolist()}})  # fmt: skip
        self.assertAlmostEqual(4 * math.pi * q.max(), 1.5, delta=0.02)
        # Y00 alone: an isotropic density.
        q = self.norm({"sh": {"lmax": 1, "coef": [1.0, 0.0, 0.0, 0.0]}})
        self.assertLess(np.ptp(q), 1e-12)

    def test_surface_wave_share(self):
        self.assertAlmostEqual(surface_wave_share(3.55, 1.524e-3, 10e9), 0.27, delta=0.005)


class RequirementTest(unittest.TestCase):
    def test_forms(self):
        r = PatternRequirement.from_dict(
            {"gain": 2, "kind": "gain", "pol": "rhcp", "max_dbi": 3, "band": "b",
             "directions": {"cone": {"toward": "+z", "half_angle_deg": 30}}}  # fmt: skip
        )
        self.assertEqual((r.quantity, r.ports, r.bound, r.excitation), ("gain", (2,), "max", 2))
        self.assertEqual(r.scale_value, 1.0)
        self.assertEqual(PatternRequirement.from_dict(r.to_dict()), r)
        with self.assertRaises(ValueError):
            PatternRequirement.from_dict({"gain": 1, "pol": "diagonal", "min_dbi": 0,
                                          "directions": "sphere", "band": "b"})  # fmt: skip
        with self.assertRaises(ValueError):
            PatternRequirement.from_dict({"efficiency": 1, "kind": "aperture", "min": 0.5,
                                          "band": "b"})  # fmt: skip
        x = PatternRequirement.from_dict({"cross_pol": 1, "max_db": -15, "band": "b",
                                          "directions": {"point": {"theta_deg": 0}}})  # fmt: skip
        self.assertEqual(x.scale_value, 3.0)


if __name__ == "__main__":
    unittest.main()
