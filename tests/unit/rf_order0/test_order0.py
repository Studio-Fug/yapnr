"""yapnr.rf.order0: the D1/D2 specs and formulations, the forward-run directories (their
footprints re-rasterize to the drawn copper), the criteria, the loss correction and D-O0-11."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf import validate
from yapnr.rf.export.kicad import read_footprint
from yapnr.rf.export.touchstone import write_touchstone
from yapnr.rf.order0 import demos, write


def read(*parts) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


class SpecTest(unittest.TestCase):
    def test_substrates(self):
        data = demos.eq_data()
        eq = data["substrates"]["oshpark-4l-fr408hr:M"]["coupon"]["equivalent"]
        st = demos.stackup("M-eq", data)
        self.assertEqual(
            (st.er, st.h_mm, st.tan_delta),
            (round(eq["er"], 4), round(eq["h_mm"], 4), round(eq["tan_delta"], 5)),
        )
        # The thickness-equivalent substrate is thinner and of lower εr than the prepreg.
        nom = demos.stackup("M-nom", data)
        self.assertLess(st.h_mm, nom.h_mm)
        self.assertLess(st.er, nom.er)
        self.assertEqual(nom.h_mm, 0.1999)
        self.assertGreater(demos.stackup("M-eq-em528", data).er, st.er)
        self.assertGreater(demos.stackup("W-eq", data).h_mm, 1.3)
        with self.assertRaises(ValueError):
            demos.stackup("M-nom-em528", data)

    def test_demo_windows_and_formulations(self):
        d1 = demos.demo_spec("M", s21_min=-3.3)
        self.assertEqual(d1.design_region, (0.0, 12.0, -7.5, 7.5))
        self.assertEqual([(p.side, p.at_mm, p.width_cells) for p in d1.ports],
                         [("W", 0.0, 4), ("N", 6.0, 4), ("S", 6.0, 4)])  # fmt: skip
        self.assertEqual(d1.grid.pitch_mm, 0.1)
        self.assertEqual(d1.symmetry, "mirror_y")
        limits = {(r.ports, r.bound): r.limit for r in d1.requirements}
        self.assertEqual(limits[((2, 1), "min")], -3.3)
        self.assertEqual(limits[((1, 1), "max")], -20.0)
        d2 = demos.demo_spec("W")
        self.assertEqual(d2.design_region, (0.0, 20.0, -12.0, 12.0))
        self.assertEqual([p.width_cells for p in d2.ports], [6, 6, 6])
        # Each variant changes the optimizer only, and only where it says.
        base = demos.FORMULATIONS["base"][0]
        changed = {}
        for name, (opt, _) in demos.FORMULATIONS.items():
            spec = demos.demo_spec("M", name)
            self.assertEqual(
                replace(spec, optimizer=base, name=d1.name),
                replace(demos.demo_spec("M"), name=d1.name),
            )
            changed[name] = {k for k in vars(base) if getattr(opt, k) != getattr(base, k)}
        self.assertEqual(changed["base"], set())
        self.assertEqual(changed["robust"], {"eta_variants", "robust_from_beta"})
        self.assertEqual(changed["star"], {"seed"})
        self.assertEqual(changed["sched"], {"iterations_per_beta", "trust_reference"})
        self.assertEqual(len({demos.demo_spec("M", n).sha256() for n in demos.FORMULATIONS}), 4)

    def test_criteria(self):
        c = demos.criteria(-3.3)
        coarse = {x["name"]: x["limit"] for x in c["coarse"]}
        fine = {x["name"]: x["limit"] for x in c["fine"]}
        self.assertEqual(coarse["|S21| min dB"], -3.35)
        self.assertEqual(fine["|S31| min dB"], -3.5)
        self.assertEqual(fine["|S11| max dB"], -15.0)
        self.assertEqual(c["dense_ghz"], [[4.0, 6.0, 81]])


class ForwardTest(unittest.TestCase):
    def test_raster(self):
        spec, rects = demos.reference_spec("M")
        m = demos.raster(spec, rects)
        self.assertEqual(m.shape, (188, 240))
        self.assertEqual(int(m.sum()), 180 * 14 + 8 * 240)
        with self.assertRaises(ValueError):
            demos.raster(spec, [(0.0, 1.0, -0.33, 0.33)])

    def test_forward_dir_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            for spec, rects in (demos.reference_spec("W"), demos.line_spec("M", 0.40, 10.0)):
                out = demos.forward_dir(spec, rects, os.path.join(tmp, spec.name))
                again = validate.load_spec(out)
                self.assertEqual(again.sha256(), spec.sha256())
                fp = read_footprint(os.path.join(out, "footprint.kicad_mod"))
                mask = validate.footprint_mask(fp, again, again.grid.pitch_mm)
                np.testing.assert_array_equal(mask, demos.raster(spec, rects) > 0.5)
                self.assertTrue(validate.connectivity(fp, again)["ports_joined"])
                cells = {p.n: p.width_cells for p in spec.ports}
                self.assertEqual(validate.pad_widths(fp, again), cells)

    def test_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, rects = demos.reference_spec("M")
            run = demos.forward_dir(spec, rects, os.path.join(tmp, "run"))
            out = demos.forward_variant(run, os.path.join(tmp, "nom"), "M-nom")
            again = validate.load_spec(out)
            self.assertEqual(again.stackup, demos.stackup("M-nom"))
            self.assertIn("wide", again.bands)
            self.assertEqual(read(out, "footprint.kicad_mod"), read(run, "footprint.kicad_mod"))
            self.assertFalse(os.path.exists(os.path.join(out, "checkpoint.npz")))


class LossTest(unittest.TestCase):
    def test_rule(self):
        self.assertEqual(demos.d_o0_11(-3.29), -3.4)
        self.assertEqual(demos.d_o0_11(-3.30), -3.4)
        self.assertEqual(demos.d_o0_11(-3.31), -3.5)

    def test_alpha_and_path(self):
        f = np.linspace(4e9, 6e9, 5)
        alpha = 0.012 + 0.002 * (f - 5e9) / 1e9  # dB/mm

        def line(path, length):
            s = np.zeros((f.size, 2, 2), complex)
            s[:, 1, 0] = s[:, 0, 1] = 10 ** (-alpha * length / 20) * np.exp(-1j * f / 1e9)
            write_touchstone(path, f, s)

        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.s2p"), os.path.join(tmp, "b.s2p")
            line(a, 10.0)
            line(b, 30.0)
            ff, got = demos.solver_alpha(a, b, 20.0)
        np.testing.assert_allclose(got, alpha, atol=1e-6)
        corr = demos.path_correction(ff, {"arm": np.full(5, 0.002), "line": np.full(5, 0.003)},
                                     [("arm", 9.2), ("line", 6.0)])  # fmt: skip
        np.testing.assert_allclose(corr, -(0.002 * 9.2 + 0.003 * 6.0))
        self.assertEqual(demos.r1_path("M"), [("arm", 9.2), ("line", 6.0)])

    def test_loss_end_to_end(self):
        f = np.linspace(3e9, 7e9, 161)
        with tempfile.TemporaryDirectory() as tmp:
            for w in ("040", "070"):
                for length in (10, 30):
                    d = os.path.join(tmp, "tasks", f"mc~line-m-w{w}-l{length}", "result", "out")
                    d = os.path.join(d, f"line-m-w{w}-l{length}")
                    os.makedirs(d)
                    s = np.zeros((f.size, 2, 2), complex)
                    s[:, 1, 0] = s[:, 0, 1] = 10 ** (-0.009 * length / 20)
                    write_touchstone(os.path.join(d, "coarse_dense.s2p"), f, s)
            d = os.path.join(tmp, "r1-m-eq")
            os.makedirs(d)
            s = np.zeros((f.size, 3, 3), complex)
            s[:, 0, 0] = 0.05
            s[:, 1, 0] = s[:, 2, 0] = s[:, 0, 1] = s[:, 0, 2] = 10 ** (-3.2 / 20)
            write_touchstone(os.path.join(d, "coarse_dense.s3p"), f, s)
            out = write.loss(tmp)
        self.assertAlmostEqual(out["lines"]["line"]["solver_db_per_cm_5ghz"], 0.09, places=4)
        corr = out["r1"]["worst_s21_corrected_db"]
        self.assertLess(corr, -3.2)  # the coupon model loses more than the solver
        self.assertEqual(out["d_o0_11"]["criterion_db"], demos.d_o0_11(corr))
        self.assertLess(out["r1"]["ratio_check"]["worst_s21_corrected_db"], -3.2)

    def test_line_loss_any_reference(self):
        # a 35 Ω line, 12 mm, α 0.01 dB/mm, β L of several radians, seen from 50 Ω ports
        f = np.linspace(3e9, 7e9, 9)
        gl = 0.012 * 12 / (20 * np.log10(np.e)) * 10 + 1j * 2 * np.pi * f / 1e9 * 1.3
        zc, z0 = 35.0, 50.0
        a, b = np.cosh(gl), zc * np.sinh(gl)
        c, d = np.sinh(gl) / zc, np.cosh(gl)
        den = a + b / z0 + c * z0 + d
        s = np.zeros((f.size, 2, 2), complex)
        s[:, 0, 0] = (a + b / z0 - c * z0 - d) / den
        s[:, 1, 1] = (-a + b / z0 - c * z0 + d) / den
        s[:, 0, 1] = s[:, 1, 0] = 2 / den
        self.assertLess(np.max(20 * np.log10(np.abs(s[:, 0, 0]))), -5)  # badly matched
        np.testing.assert_allclose(demos.line_loss_db(s), 0.012 * 12 * 10, rtol=1e-9)

    def test_ratio_correction(self):
        s = np.zeros((2, 3, 3), complex)
        s[:, 1, 0] = s[:, 2, 0] = np.sqrt([0.5, 0.45])  # lossless; 10 % dissipated
        corr = demos.ratio_correction(s, np.array([1.5, 1.5]))
        self.assertAlmostEqual(corr[0], 0.0)
        self.assertAlmostEqual(corr[1], 10 * np.log10(0.85 / 0.9))


class WriteTest(unittest.TestCase):
    def test_layout_and_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = write.write(tmp, s21=-3.5, offset=0.12)
            self.assertEqual(manifest["s21_optimizer_db"], -3.38)
            spec = json.loads(read(tmp, "specs", "d1-star.json"))
            self.assertEqual(spec["optimizer"]["seed"], "star")
            run0b = tomllib.loads(read(tmp, "run0b.toml"))
            ids = [j["id"] for j in run0b["jobs"]]
            self.assertEqual(ids[:2], ["diag", "r1-m-eq"])
            self.assertEqual(len([i for i in ids if i.startswith("line-")]), 4)
            self.assertTrue(run0b["defaults"]["require_native"])
            for job in run0b["jobs"][1:]:
                self.assertTrue(os.path.isfile(os.path.join(tmp, job["validate"], "spec.json")))
                self.assertTrue(os.path.isfile(os.path.join(tmp, job["criteria"])))
            m = tomllib.loads(read(tmp, "compute-m.toml"))
            w = tomllib.loads(read(tmp, "compute-w.toml"))
            self.assertEqual(m["placement"]["shape"], "c4-highcpu-16")
            self.assertEqual(w["placement"]["shape"], "c4d-highcpu-16")
            self.assertEqual(
                [j["id"] for j in m["jobs"]],
                ["d1-base", "d1-robust", "d1-star", "d1-sched", "r1-m-nom", "r1-m-eq-em528"],
            )


if __name__ == "__main__":
    unittest.main()
