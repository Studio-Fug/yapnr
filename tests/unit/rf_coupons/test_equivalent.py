"""yapnr.rf.coupons.equivalent: the shipped thickness-equivalent substrates reproduce their
targets, and (FEA environment) the zero-thickness sheet solve agrees with Hammerstad-Jensen."""

from __future__ import annotations

import json
import unittest

from yapnr.rf.coupons import equivalent

try:
    import gmsh  # noqa: F401
    import skfem  # noqa: F401

    FEA = True
except ImportError:
    FEA = False


class DataTest(unittest.TestCase):
    def setUp(self):
        with open(equivalent.data_path(), encoding="utf-8") as fh:
            self.doc = json.load(fh)

    def test_every_region_and_board(self):
        self.assertEqual(self.doc["schema"], equivalent.SCHEMA)
        self.assertEqual(self.doc["use"], "coupon")
        keys = sorted(self.doc["substrates"])
        self.assertEqual(
            keys,
            [
                "oshpark-4l-em528:M",
                "oshpark-4l-em528:W",
                "oshpark-4l-fr408hr:M",
                "oshpark-4l-fr408hr:W",
            ],
        )
        for key, entry in self.doc["substrates"].items():
            nom = entry["nominal"]
            for target in ("geometric", "coupon"):
                t = entry[target]
                # the sheet on the equivalent substrate reproduces the real line it was solved for
                self.assertAlmostEqual(t["check"]["z0_ohm"], t["real"]["z0_ohm"], places=2, msg=key)
                self.assertAlmostEqual(
                    t["check"]["eps_eff"], t["real"]["eps_eff"], places=4, msg=key
                )
                # thinner and lower εr than the nominal substrate: the copper's thickness
                self.assertLess(t["equivalent"]["h_mm"], nom["h_mm"], key)
                self.assertLess(t["equivalent"]["er"], nom["er"] + 0.05, key)
                # the closed form on the equivalent substrate is within 0.5 % (Z0) of the solve
                hj_z = t["hammerstad_jensen_on_equivalent"][0]
                self.assertLess(abs(hj_z / t["check"]["z0_ohm"] - 1), 0.005, key)
            # the zero-thickness sheet on the nominal substrate reads high (the bias it removes)
            self.assertGreater(nom["sheet_z0_ohm"], entry["coupon"]["real"]["z0_ohm"], key)

    def test_order0_values(self):
        m = self.doc["substrates"]["oshpark-4l-fr408hr:M"]["coupon"]["equivalent"]
        # order0-design D-O0-9's closed-form estimate was εr' 3.40, h' 0.176 mm
        self.assertAlmostEqual(m["er"], 3.40, delta=0.06)
        self.assertAlmostEqual(m["h_mm"], 0.176, delta=0.006)


@unittest.skipUnless(FEA, "needs the FEA environment (scikit-fem, gmsh)")
class SheetTest(unittest.TestCase):
    def test_sheet_against_hammerstad_jensen(self):
        z, e = equivalent.sheet_line(0.40, 0.1999, 3.561, "M")
        hz, he = equivalent.hammerstad_check(0.40, 0.1999, 3.561)
        self.assertLess(abs(z / hz - 1), 0.001)  # HJ: 0.01 % in air, the solve's mesh 0.02 %
        self.assertLess(abs(e / he - 1), 0.002)  # HJ: 0.2 %


if __name__ == "__main__":
    unittest.main()
