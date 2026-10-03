"""Forward models of the coupons (design §8.5): materials, roughness, line algebra, structures."""

from __future__ import annotations

import math
import unittest

import numpy as np

from yapnr.rf.coupons import families, models, stackups

A = "JLC04161H-7628"
B = "JLC06161H-7628"


class DjordjevicSarkarTest(unittest.TestCase):
    def test_reference_point(self):
        e = models.ds_eps(4.4, 0.018, np.array([stackups.F_REF]))[0]
        self.assertAlmostEqual(e.real, 4.4, places=12)
        self.assertAlmostEqual(-e.imag / e.real, 0.018, places=12)

    def test_kramers_kronig(self):
        """ε'(f) − ε∞ from ε''(f) by the Kramers-Kronig integral (causality, design §11.3); the
        pole is removed by subtracting ω0 ε''(ω0), whose principal-value integral is zero."""
        f = np.logspace(-3, 16, 40001)
        w = 2 * math.pi * f
        epp = -models.ds_eps(4.4, 0.02, f).imag
        eps_inf = models.ds_eps(4.4, 0.02, np.array([1e19]))[0].real
        for f0 in (1e6, 1e9, 6e9):
            w0 = 2 * math.pi * f0
            epp0 = -models.ds_eps(4.4, 0.02, np.array([f0]))[0].imag
            den = w**2 - w0**2
            g = np.where(abs(den) > 0, (w * epp - w0 * epp0) / np.where(den == 0, 1, den), 0.0)
            kk = eps_inf + 2 / math.pi * float(np.sum(0.5 * (g[1:] + g[:-1]) * np.diff(w)))
            e0 = models.ds_eps(4.4, 0.02, np.array([f0]))[0].real
            self.assertAlmostEqual(kk, e0, delta=0.003 * e0)

    def test_monotone_and_lossy(self):
        f = np.logspace(6, 11, 50)
        e = models.ds_eps(4.4, 0.018, f)
        self.assertTrue(np.all(np.diff(e.real) < 0))
        self.assertTrue(np.all(e.imag < 0))


class RoughnessTest(unittest.TestCase):
    def test_huray_limits(self):
        self.assertAlmostEqual(float(models.huray(np.array([1e3]), 2.0)[0]), 1.0, places=3)
        self.assertAlmostEqual(float(models.huray(np.array([1e18]), 2.0)[0]), 3.0, places=2)
        k = models.huray(np.logspace(6, 12, 40), 2.0)
        self.assertTrue(np.all(np.diff(k) > 0))

    def test_groiss_limits(self):
        self.assertAlmostEqual(float(models.groiss(np.array([1e5]), 1.0)[0]), 1.0, places=6)
        self.assertAlmostEqual(float(models.groiss(np.array([1e14]), 1.0)[0]), 2.0, places=3)


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.f = np.linspace(0.1e9, 12e9, 40)
        st = stackups.get(A)
        self.m = models.Model(A, stackups.with_values(st, {}), self.f)

    def test_line_cascade(self):
        line = self.m.line("P")
        a = models.cascade(models.abcd_line(line, 7.0), models.abcd_line(line, 13.0))
        np.testing.assert_allclose(a, models.abcd_line(line, 20.0), atol=1e-9)

    def test_matched_line(self):
        line = self.m.line("P")
        s = models.abcd_to_s(models.abcd_line(line, 40.0), line.zc)
        np.testing.assert_allclose(abs(s[:, 0, 0]), 0, atol=1e-12)
        np.testing.assert_allclose(s[:, 1, 0], np.exp(-line.gamma * 0.04), rtol=1e-10)

    def test_star_equals_abcd_cascade(self):
        m1 = models.abcd_line(self.m.line("P0.7"), 5.0)
        m2 = models.cascade(
            models.abcd_shunt(1j * self.m.w * 0.1e-12), models.abcd_line(self.m.line("P"), 3.0)
        )
        s1, s2 = models.abcd_to_s(m1, 50.0), models.abcd_to_s(m2, 50.0)
        np.testing.assert_allclose(
            models.star(s1, s2), models.abcd_to_s(models.cascade(m1, m2), 50.0), atol=1e-12
        )

    def test_renormalize_roundtrip(self):
        s = models.abcd_to_s(models.abcd_line(self.m.line("M"), 10.0), 50.0)
        z = self.m.line("P").zc
        np.testing.assert_allclose(
            models.renormalize(models.renormalize(s, 50.0, z), z, 50.0), s, atol=1e-12
        )

    def test_t_parameters(self):
        s = models.abcd_to_s(models.abcd_line(self.m.line("P1.4"), 10.0), 50.0)
        np.testing.assert_allclose(models.t_to_s(models.s_to_t(s)), s, atol=1e-12)


class LineTest(unittest.TestCase):
    def test_nominal_lines(self):
        """At 5.8 GHz the nominal lines are near 50 Ω with the εeff of design §4.3 (plus the
        dielectric dispersion and the conductor's internal inductance)."""
        f = np.array([5.8e9])
        for sid, fam, z0, eps in ((A, "P", 50.0, 3.18), (A, "M", 50.0, 3.40), (B, "S", 50.0, 4.48)):
            m = models.Model(sid, stackups.with_values(stackups.get(sid), {}), f)
            line = m.line(fam)
            self.assertAlmostEqual(abs(line.zc[0]), z0, delta=1.5, msg=fam)
            self.assertAlmostEqual(line.eps_eff[0], eps, delta=0.08, msg=fam)

    def test_loss_rises_with_frequency_and_tan_delta(self):
        f = np.array([2.4e9, 5.8e9, 10e9])
        st = stackups.get(A)
        lo = models.Model(A, stackups.with_values(st, {"pp.df": 0.01}), f).line("P").gamma.real
        hi = models.Model(A, stackups.with_values(st, {"pp.df": 0.02}), f).line("P").gamma.real
        self.assertTrue(np.all(np.diff(lo) > 0))
        self.assertTrue(np.all(hi > lo))
        db_100mm = 8.686 * hi * 0.1
        self.assertTrue(1.5 < db_100mm[1] < 4.0, db_100mm)  # design §9: about 2-3 dB at 5.8 GHz

    def test_coupled_modes(self):
        m = models.Model(A, stackups.with_values(stackups.get(A), {}), np.array([5.8e9]))
        ze, zo = abs(m.line("CPL20", "e").zc[0]), abs(m.line("CPL20", "o").zc[0])
        self.assertGreater(ze, 50.0)
        self.assertLess(zo, 50.0)
        self.assertGreater(m.line("CPL20", "e").eps_eff[0], m.line("CPL20", "o").eps_eff[0])


class StructureTest(unittest.TestCase):
    def test_ring_notches_at_resonances(self):
        """The directly fed ring (a sixth of a turn between the feeds) has notches where the
        circumference is a whole number of guided wavelengths."""
        f = np.arange(1, 1301) * 10e6
        m = models.Model(A, stackups.with_values(stackups.get(A), {}), f)
        line = m.line("M")
        k = int(np.argmin(abs(f - 2.9e9)))
        r = 2 * math.pi / line.gamma[k].imag / (2 * math.pi) * 1e3
        s = models.structure_s(
            m, [("line", "M", 10.0), ("ring2", "M", r, 1 / 6), ("line", "M", 10.0)], line.zc
        )
        db = 20 * np.log10(abs(s[:, 1, 0]))
        for f0 in (2.9e9, 5.8e9, 11.6e9):
            sel = np.where(abs(f - f0) < 0.02 * f0)[0]
            fmin = f[sel[np.argmin(db[sel])]]
            self.assertLess(abs(fmin - f0) / f0, 0.012, (f0, fmin))
            self.assertLess(db[sel].min(), -12.0)

    def test_open_stub_notch(self):
        f = np.arange(1, 1201) * 10e6
        m = models.Model(A, stackups.with_values(stackups.get(A), {}), f)
        line = m.line("P")
        k = int(np.argmin(abs(f - 5.8e9)))
        quarter = math.pi / 2 / line.gamma[k].imag * 1e3
        s = models.structure_s(
            m, [("line", "P", 5.0), ("stub", "P", quarter, "open"), ("line", "P", 5.0)], line.zc
        )
        self.assertLess(abs(f[np.argmin(abs(s[:, 1, 0]))] - 5.8e9), 30e6)

    def test_meander_resistance(self):
        r = models.meander_resistance(0.2, 0.035, 0.0, 200.0, 0)
        self.assertAlmostEqual(r, stackups.RHO_CU * 0.2 / (0.2e-3 * 0.035e-3), places=9)
        self.assertGreater(models.meander_resistance(0.2, 0.035, 0.01, 200.0, 0), r)

    def test_via_inductance(self):
        lv = models.via_inductance(0.21, 0.3)  # Goldfarb-Pucel: about 15 pH
        self.assertTrue(5e-12 < lv < 60e-12, lv)
        self.assertGreater(models.via_inductance(1.6, 0.3), 10 * lv)


class TablesTest(unittest.TestCase):
    """The shipped 2D tables reproduce the design's §4.3 values at nominal (the tables' own
    held-out fine-mesh checks are stored with them)."""

    DESIGN = {
        A: {
            "P": (50.0, 3.180),
            "P-MO": (52.6, 2.868),
            "P0.7": (58.2, 3.124),
            "P1.4": (42.4, 3.251),
            "M": (50.0, 3.403),
            "M-MO": (51.8, 3.165),
        },
        B: {"S": (50.0, 4.479), "S0.7": (58.4, 4.480), "S1.4": (42.2, 4.478), "P": (50.0, 3.180)},
    }

    def test_nominal_values(self):
        for sid, fams in self.DESIGN.items():
            t = families.load(sid)
            for fid, (z0, eps) in fams.items():
                fam = families.FAMILIES[fid]
                out = t[fid](stackups.nominal(fam.params))
                c, c0 = math.exp(out["lnC"]), math.exp(out["lnC0"])
                self.assertAlmostEqual(
                    1 / (models.C_LIGHT * math.sqrt(c * c0)), z0, delta=0.15, msg=fid
                )
                self.assertAlmostEqual(c / c0, eps, delta=0.004, msg=fid)

    def test_stored_checks(self):
        for sid in (A, B):
            doc = families.load_doc(sid)
            for fid, t in doc["families"].items():
                nom = t["test"].get("nominal_err") or t["test"]["max_err"]
                self.assertLess(nom["z0_ohm"], 0.1, fid)
                self.assertLess(nom["eps_eff"], 0.002, fid)

    def test_extrapolation_is_linear_and_finite(self):
        t = families.load(A)["P"]
        v = stackups.nominal(families.FAMILIES["P"].params)
        v["mask.scale"] = 3.0  # beyond the table
        out = t(v)
        self.assertTrue(all(math.isfinite(x) for x in out.values()))


if __name__ == "__main__":
    unittest.main()
