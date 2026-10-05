"""The adjoint gradient of every pattern term against finite differences (design §26.4).

Board models (`testing.tiny_board_spec`, about 2.5e4 cells): a random gray design, the whole
pipeline (filter, projection, FDTD forward and adjoint, the near-to-far transform of the
Huygens box's probes, the pattern terms), against Richardson-extrapolated central differences
along a random direction (`testing.GradientChecks`, float64, exact runs). With
`aggregate: none` each requirement is its own objective, so each term is checked alone:

- every requirement form (gain bounds, ripple, hpbw, front-to-back, cross-pol, both shape
  forms, both efficiencies and the total efficiency of `radiated`) on a free board with its
  ground under the whole board, and with the image ground (`ground: infinite`);
- every polarization (total, Ludwig-3 co and cross, θ, φ, right- and left-hand circular) and
  gain kind (realized, gain, directivity), and both mask kinds, on a board with a ground
  keepout under the design region;
- the excitation's group (every term in one smooth maximum, one adjoint run).

Measured: 1e-12–3e-9 relative.
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import GradientChecks, tiny_board_spec

CUT = {"cut": {"theta_deg": 45, "phi_deg": [30, 270], "step_deg": 120}}
POLS = [
    ("total", "realized"),
    ("co", "gain"),
    ("cross", "directivity"),
    ("theta", "realized"),
    ("phi", "gain"),
    ("rhcp", "directivity"),
    ("lhcp", "realized"),
]


class _Pattern(GradientChecks):
    # The length-scale constraints do not depend on the objective (test_pipeline_gradient).
    test_lengthscale_gradient = None


class _PerRequirement(_Pattern):
    def test_runs_converged(self):
        self.assertTrue(self.ev.converged)
        n = len(self.p.spec.requirements)
        self.assertEqual(len(self.ev.keys), 3 * n)


class FreeBoardFormsGradientTest(_PerRequirement, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup_gradient(tiny_board_spec(aggregate="none"), 1)


class ImageGroundFormsGradientTest(_PerRequirement, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup_gradient(tiny_board_spec(ground="infinite", aggregate="none"), 2)

    def test_image_transform(self):
        self.assertTrue(self.p.far.image)


class PolarizationGradientTest(_PerRequirement, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reqs = [
            {"gain": 1, "pol": pol, "kind": kind, "min_dbi": 0.0, "directions": CUT, "band": "b"}
            for pol, kind in POLS
        ]
        reqs += [
            {"gain": 1, "min_mask_dbi": [[-60, -5], [60, 2]], "band": "b",
             "directions": {"cut": {"phi_deg": 45, "theta_deg": [-60, 60], "step_deg": 30}}},
            {"gain": 1, "kind": "gain", "max_mask_dbi": [[30, 5], [270, 0]], "band": "b",
             "directions": CUT},
        ]  # fmt: skip
        cls.setup_gradient(
            tiny_board_spec(ground="keepout", requirements=reqs, aggregate="none"), 3
        )


class GroupGradientTest(_Pattern, unittest.TestCase):
    """Every term of the free board's requirements in the excitation's smooth maximum."""

    @classmethod
    def setUpClass(cls):
        cls.setup_gradient(tiny_board_spec(), 4)


if __name__ == "__main__":
    unittest.main()
