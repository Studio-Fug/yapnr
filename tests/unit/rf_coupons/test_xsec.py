"""The 2D cross-section solver against design §4.3 (Tier 1 of design §11.3). Needs scikit-fem
and gmsh (the FEA environment): skipped elsewhere, and manual under Bazel.

    python -m unittest tests/unit/rf_coupons/test_xsec.py   # in the FEA environment
"""

from __future__ import annotations

import math
import unittest

try:
    import gmsh  # noqa: F401
    import skfem  # noqa: F401

    HAVE_FEA = True
except ImportError:
    HAVE_FEA = False

from yapnr.rf.coupons import families

C = 299792458.0


@unittest.skipUnless(HAVE_FEA, "scikit-fem and gmsh not installed (FEA environment only)")
class CrossSectionTest(unittest.TestCase):
    def _z_eps(self, fam, values=None, mode=""):
        out = families.solve_point(fam, values or {}, "fit", "c")
        p = f"{mode}:" if mode else ""
        c, c0 = math.exp(out[p + "lnC"]), math.exp(out[p + "lnC0"])
        return 1 / (C * math.sqrt(c * c0)), c / c0

    def test_design_table(self):
        for fam, z0, eps in (
            ("P", 50.0, 3.180),
            ("M", 50.0, 3.403),
            ("S", 50.0, 4.479),
            ("P-MO", 52.6, 2.868),
            ("S1.4", 42.2, 4.478),
        ):
            z, e = self._z_eps(fam)
            self.assertAlmostEqual(z, z0, delta=0.1, msg=fam)
            self.assertAlmostEqual(e, eps, delta=0.002, msg=fam)

    def test_etch_does_not_move_masked_gcpw_phase(self):
        """Design §3: on masked GCPW the etch changes Z0 (+6 Ω per 25 µm) but not εeff."""
        z0, e0 = self._z_eps("P")
        z1, e1 = self._z_eps("P", {"L1.etch": 0.025})
        self.assertAlmostEqual(z1 - z0, 6.1, delta=0.5)
        self.assertLess(abs(e1 - e0), 0.01)

    def test_shipped_table_matches(self):
        t = families.load("JLC04161H-7628")["P"]
        v = {
            "pp.dk": 4.6,
            "pp1.h": 0.22,
            "L1.etch": 0.01,
            "L1.t": 0.03,
            "mask.scale": 1.2,
            "mask.dk": 3.7,
        }
        out = t(v)
        z_t = 1 / (C * math.sqrt(math.exp(out["lnC"]) * math.exp(out["lnC0"])))
        z, _ = self._z_eps("P", v)
        self.assertAlmostEqual(z_t, z, delta=0.1)


if __name__ == "__main__":
    unittest.main()
