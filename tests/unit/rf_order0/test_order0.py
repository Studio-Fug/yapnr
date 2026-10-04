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


class ResonatorTest(unittest.TestCase):
    """The held-out ring A11 and stub A12 as forward runs of our FDTD (design §7 item 2)."""

    def test_ring_and_stub_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for fn in (demos.ring_spec, demos.stub_spec):
                spec, mask, geo = fn("M-eq")
                self.assertEqual(spec.symmetry, "none")
                self.assertEqual(spec.grid.pitch_mm, demos.COUPON_PITCH)
                self.assertEqual([p.side for p in spec.ports], ["W", "E"])
                out = demos.forward_dir(spec, mask, os.path.join(tmp, spec.name), geo)
                again = validate.load_spec(out)
                fp = read_footprint(os.path.join(out, "footprint.kicad_mod"))
                got = validate.footprint_mask(fp, again, again.grid.pitch_mm)
                np.testing.assert_array_equal(got, mask > 0.5)
                self.assertEqual(validate.pad_widths(fp, again), {1: 8, 2: 8})
                info = json.loads(read(out, "forward.json"))
                self.assertEqual(info["pixels"], int(mask.sum()))
                self.assertEqual(info["stick"], geo["stick"])

    def test_ring_geometry(self):
        spec, mask, geo = demos.ring_spec("M-eq-em528")
        self.assertEqual(geo["rp_to_rp_mm"], 36.0)  # the reference planes 36 mm apart
        x0, x1, y0, y1 = spec.design_region
        # the ring (mean radius r, 0.40 mm strip) lies inside the window, south of the port axis
        self.assertLess(y0, geo["centre_mm"][1] - geo["radius_mm"] - 0.2)
        self.assertGreater(y1, geo["centre_mm"][1] + geo["radius_mm"] + 0.2)
        self.assertEqual(spec.stackup, demos.stackup("M-eq-em528"))
        self.assertEqual(spec.solver.max_steps, demos.RING_MAX_STEPS)

    def test_stub_snapped(self):
        _, _, geo = demos.stub_spec()
        self.assertLessEqual(abs(geo["stub_simulated_mm"] - geo["stub_drawn_mm"]), 0.025 + 1e-9)

    def test_predict_writes_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = write.predict(tmp, image="ghcr.io/x/y@sha256:" + "ab" * 32)
            self.assertIn("a11-ring-m-eq", manifest["runs"])
            self.assertIn("line-w-w300-l60-w-eq-em528", manifest["runs"])
            jobs_m = tomllib.loads(read(tmp, "predict-m.toml"))
            self.assertEqual(jobs_m["placement"]["shape"], "c4-highcpu-16")
            self.assertTrue(jobs_m["image"].endswith("ab" * 32))
            ids = {j["id"] for j in jobs_m["jobs"]}
            self.assertIn("line-m-w070-l30-m-nom", ids)
            jobs_w = tomllib.loads(read(tmp, "predict-w.toml"))
            self.assertEqual(len(jobs_w["jobs"]), 4)


class PredictTest(unittest.TestCase):
    """Loss correction per substrate (predict.py)."""

    def test_line_ids(self):
        from yapnr.rf.order0 import predict

        self.assertEqual(
            predict.line_ids("M-eq")["line"][:2], ("line-m-w040-l10", "line-m-w040-l30")
        )
        self.assertEqual(predict.line_ids("W-nom")["line"][1], "line-w-w300-l60-w-nom")
        self.assertEqual(predict.stackup_of("M-eq-em528"), "OSHPARK-4L-EM528")

    def test_correct_all_on_synthetic_data(self):
        from yapnr.rf.order0 import predict

        f = np.linspace(3e9, 7e9, 161)
        with tempfile.TemporaryDirectory() as tmp:
            lines = os.path.join(tmp, "lines")
            os.makedirs(lines)
            for w in ("040", "070"):
                for length in (10, 30):
                    s = np.zeros((f.size, 2, 2), complex)
                    s[:, 1, 0] = s[:, 0, 1] = 10 ** (-0.010 * length / 20)
                    write_touchstone(os.path.join(lines, f"line-m-w{w}-l{length}.s2p"), f, s)
            src = os.path.join(tmp, "pred", "x")
            os.makedirs(src)
            s = np.zeros((f.size, 3, 3), complex)
            s[:, 0, 0] = 0.05
            s[:, 1, 0] = s[:, 2, 0] = s[:, 0, 1] = s[:, 0, 2] = 10 ** (-3.25 / 20)
            write_touchstone(os.path.join(src, "d.s3p"), f, s)
            items = [("d1", "x/d.s3p", "M-eq", "ratio"), ("r1", "x/d.s3p", "M-eq", "r1"),
                     ("gone", "x/none.s3p", "M-eq", "ratio")]  # fmt: skip
            out = predict.correct_all(os.path.join(tmp, "out"), items, os.path.join(tmp, "pred"),
                                      [lines])  # fmt: skip
            self.assertTrue(out["items"]["gone"]["missing"])
            for name in ("d1", "r1"):
                item = out["items"][name]
                self.assertLess(item["corrected"]["s21_min_db"], item["raw"]["s21_min_db"])
                self.assertEqual(item["corrected"]["s11_max_db"], item["raw"]["s11_max_db"])
                self.assertTrue(os.path.isfile(os.path.join(tmp, "out", item["file"])))

    def test_notch(self):
        from yapnr.rf.order0 import predict

        f = np.linspace(5e9, 6e9, 201)
        s21 = np.sqrt(0.01**2 + ((f - 5.4321e9) / 1e9) ** 2)  # a notch 40 dB deep, smooth
        self.assertAlmostEqual(predict.notch(f, s21, 5.4e9) / 1e9, 5.4321, places=4)
