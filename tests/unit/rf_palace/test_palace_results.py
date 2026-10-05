"""yapnr.rf.palace.results: reading Palace's tables and turning them into the sign-off numbers
(openEMS's 50-ohm waves, effective permittivity and loss from a line pair, notch, dip and Re Zin
peak frequencies, lumped-port de-embedding, the two-solver comparison). numpy only."""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from yapnr.rf.palace import results

C0 = 299792458.0
PORT_S = (
    "        f (GHz),             |S[1][1]| (dB),        arg(S[1][1]) (deg.),"
    "             |S[2][1]| (dB),        arg(S[2][1]) (deg.)\n"
    " 6.00000000e+01,        -2.000000000000e+01,        +9.000000000000e+01,"
    "        -1.000000000000e+00,        -4.500000000000e+01\n"
    " 6.20000000e+01,        -2.600000000000e+01,        -1.800000000000e+02,"
    "        -2.000000000000e+00,        +3.000000000000e+01\n"
)
PORT_Z = (
    "        f (GHz),          Re{Z_PV[1]} (Ohm),          Im{Z_PV[1]} (Ohm),"
    "          Re{Z_PV[2]} (Ohm),          Im{Z_PV[2]} (Ohm)\n"
    " 6.00000000e+01,        +5.300000000000e+01,        +1.000000000000e-01,"
    "        +4.900000000000e+01,        +0.000000000000e+00\n"
)
# Palace's BoundaryMode tables (line-gcpw-5mm-solid at 62 GHz, order 3)
MODE_KN = (
    "        m,               Re{kn} (1/m),               Im{kn} (1/m),"
    "                  Re{n_eff},                  Im{n_eff},              Error (Bkwd.),"
    "               Error (Abs.)\n"
    " 1.00e+00,        +2.121031425803e+03,        -7.514734628882e+00,"
    "        +1.632285971950e+00,        -5.783127853946e-03,        +2.030462044422e-13,"
    "        +3.756257652265e-09\n"
)
MODE_Z = (
    "        m,              Z_PV[1] (Ohm),              L_PV[1] (H/m),              C_PV[1] (F/m)\n"
    " 1.00e+00,        +5.384337784732e+01,        +2.931621126459e-07,        +1.011214405757e-10\n"
)


def k0(f_ghz):
    return 2 * np.pi * np.asarray(f_ghz) * 1e9 / C0 * 1e-3  # rad/mm


class TablesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_port_tables(self):
        f, S = results.port_s(self.write("port-S.csv", PORT_S))
        self.assertEqual(list(f), [60.0, 62.0])
        self.assertAlmostEqual(abs(S[(2, 1)][0]), 10 ** (-1 / 20))
        self.assertAlmostEqual(np.degrees(np.angle(S[(2, 1)][1])), 30.0)
        self.assertAlmostEqual(S[(1, 1)][0].imag, 0.1)  # -20 dB at +90 degrees
        f, Z = results.port_z(self.write("port-Z.csv", PORT_Z))
        self.assertEqual((Z[1][0], Z[2][0]), (53 + 0.1j, 49 + 0j))
        self.assertIsNone(results.port_z(os.path.join(self.tmp, "none.csv")))

    def test_mode_tables(self):
        self.write("mode/mode-kn.csv", MODE_KN)
        self.write("mode/mode-Z.csv", MODE_Z)
        m = results.mode_results(os.path.join(self.tmp, "mode"))
        self.assertAlmostEqual(m["eeff"], 1.632285971950**2)
        self.assertAlmostEqual(m["loss_db_mm"], 0.06527, places=5)  # 8.686 x 7.515 / 1000
        self.assertAlmostEqual(m["z_pv"], 53.84337784732)

    def test_stage_dirs(self):
        for d in ("stage-2-palace-amr", "stage-1-palace-uniform", "stage-10-x", "postpro"):
            os.makedirs(os.path.join(self.tmp, "t", d))
        names = [n for n, _ in results.stage_dirs(os.path.join(self.tmp, "t"))]
        self.assertEqual(
            names, ["stage-1-palace-uniform", "stage-2-palace-amr", "stage-10-x", "main"]
        )


class ConversionTest(unittest.TestCase):
    def test_to_50(self):
        # a matched line of Z (ports matched to their own modes): S11 = 0, S21 = e^-gamma l
        s21 = np.array([0.9 * np.exp(-1j * 1.0)])
        S = {(1, 1): np.array([0j]), (2, 1): s21}
        same = results.to_50(S, {1: np.array([50.0]), 2: np.array([50.0])})
        self.assertAlmostEqual(same[(1, 1)][0], 0)
        self.assertAlmostEqual(same[(2, 1)][0], s21[0])
        out = results.to_50(S, {1: np.array([53.0]), 2: np.array([53.0])})
        self.assertAlmostEqual(out[(1, 1)][0], 3 / 103)  # the line's own mismatch to 50 ohm
        self.assertAlmostEqual(out[(2, 1)][0], s21[0])  # terminated in its own Z: transmitted
        # unequal ends: the voltage ratio of power waves, sqrt(Z2/Z1), and the waves' scaling
        out = results.to_50(S, {1: np.array([53.0]), 2: np.array([49.0])})
        v2_over_v1 = s21[0] * np.sqrt(49 / 53)
        a1 = 1.0
        v1 = a1 * (1 + 3 / 103)
        b2 = v2_over_v1 * v1 * (1 + 50 / 49) / 2
        self.assertAlmostEqual(out[(2, 1)][0], b2)

    def test_deembed_lumped(self):
        f = np.array([60.0, 62.0, 64.0])
        z0 = np.array([50.5, 50.4, 50.3])
        gamma = 0.002 + 1j * 1.6 * k0(f)
        zl = np.array([30 + 10j, 45 - 5j, 80 + 20j])
        t = np.tanh(gamma * 1.2)
        zin = z0 * (zl + z0 * t) / (z0 + zl * t)
        s_in = (zin - 50) / (zin + 50)
        got = results.deembed_lumped(s_in, z0, gamma, 1.2)
        np.testing.assert_allclose(got, (zl - 50) / (zl + 50), atol=1e-12)

    def test_eeff_loss(self):
        f = np.linspace(54, 70, 33)
        eeff, alpha = 2.66, 0.0075  # Np/mm
        gamma = alpha + 1j * np.sqrt(eeff) * k0(f)
        s5, s10 = np.exp(-gamma * 5), np.exp(-gamma * 10)
        e, loss = results.eeff_loss(f, s5, s10, 5.0, n_guess=1.6)
        np.testing.assert_allclose(e, eeff, rtol=1e-9)
        np.testing.assert_allclose(loss, 20 / np.log(10) * alpha, rtol=1e-9)


class FeatureTest(unittest.TestCase):
    def test_notch_and_dip(self):
        f = np.linspace(55, 66, 441)
        y = -1.5 - 8.0 / (1 + ((f - 59.83) / 0.4) ** 2)
        fn, depth = results.notch(f, y, 55, 66)
        self.assertAlmostEqual(fn, 59.83, places=3)
        self.assertAlmostEqual(depth, -9.5, places=2)
        self.assertEqual(results.dip, results.notch)
        self.assertEqual(
            results.band_below(f, y, -5.0)[0] < 59.83 < results.band_below(f, y, -5.0)[1], True
        )
        self.assertIsNone(results.band_below(f, y, -20.0))
        with self.assertRaises(ValueError):
            results.notch(f, y, 80, 90)

    def test_re_peak(self):
        # a parallel resonance: Re Zin peaks at f0 whatever the line before it
        f = np.linspace(58, 66, 321)
        z = 120 / (1 + 1j * 25 * (f / 62.9 - 62.9 / f))
        fp, v = results.re_peak(f, z, 58, 66)
        self.assertAlmostEqual(fp, 62.9, places=2)
        self.assertAlmostEqual(v, 120, delta=0.5)

    def test_compare(self):
        f = np.linspace(55, 66, 441)

        def line(f0, loss):
            mag = -loss - 8.0 / (1 + ((f - f0) / 0.4) ** 2)
            return 10 ** (mag / 20) * np.exp(-1j * np.radians(10 * f))

        a, b = (f, line(59.81, 1.7)), (f, line(59.85, 1.6))
        out = results.compare(a, b, [60.3, 62.05, 63.8], feature=(55, 66))
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["notch"]["rel"], (59.81 - 59.85) / 59.85, places=4)
        self.assertLess(abs(out["points"][1]["d_deg"]), 1e-6)
        off = results.compare((f, line(61.18, 1.7)), b, [62.05], feature=(55, 66))
        self.assertFalse(off["ok"])  # 2.2 % apart
        loud = results.compare((f, line(59.85, 2.4)), b, [62.05])
        self.assertFalse(loud["ok"])  # 0.8 dB


if __name__ == "__main__":
    unittest.main()
