"""Analytic linear-field fixture: not native FDTD or full-array qualification."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import torch
from jointopt.algebra_fixture import LinearFieldRunner, LinearResponse, contract
from jointopt.bridge import Forward, joint_wirtinger
from jointopt.evaluator import JointEvaluator
from jointopt.fidelity import pulse_fidelity
from jointopt.frequencies import FREQUENCIES_HZ
from jointopt.mapping import Chain
from jointopt.objective import JointObjective, Policy, upper_max
from jointopt.optimizer import UpdateLoop
from jointopt.validation import check_direction

from yapnr.rf.design.filters import ConicFilter
from yapnr.rf.design.material_grid import MaterialGrid

torch.set_num_threads(1)


def native_frequency_stub():
    f = np.asarray(FREQUENCIES_HZ)
    return SimpleNamespace(freqs=f, omega=2 * np.pi * f)


def fixture(etas=(0.5,), beta=2.0):
    shape = (8, 8)
    rho = np.zeros(shape)
    rho[2:6, 2:6] = 1
    fixed = np.zeros(shape, bool)
    fixed[0, :] = True
    fixed[-1, :] = True
    fixed[2:4, 2:4] = True
    filt = ConicFilter(0.15, 0.1)
    material = MaterialGrid(shape, fixed=fixed, fixed_value=rho, ring_width=filt.ring_width)
    chain = Chain(rho, material, filt)
    obj = JointObjective(Policy(n_theta=2))
    runner = LinearFieldRunner(shape)
    ev = JointEvaluator(
        obj,
        chain,
        runner,
        LinearResponse(obj.sampling),
        contract(obj.policy),
        ("q",),
        beta,
        etas,
        evidence_persist=lambda event, payload: True,
    )
    x = np.full(chain.n, 0.45)
    return ev, x


class AnalyticTest(unittest.TestCase):
    def setUp(self):
        # LinearFieldRunner is an algebra fixture, not a native Problem. Mock ONLY
        # its persistence/normalization envelope, never field solves or VJP math.
        # This synthetic identity cannot serve as native scientific evidence.
        observer = patch(
            "jointopt.operational_evidence.normalization_receipt",
            return_value={
                "identity": {"normalization_sha256": "a" * 64, "physical_context_sha256": "b" * 64}
            },
        )
        observer.start()
        self.addCleanup(observer.stop)


class ObjectiveTests(AnalyticTest):
    def test_both_bands_and_all_ports(self):
        ev, x = fixture()
        v = ev.evaluate(x, False)
        self.assertEqual(len(v.f), 2)
        self.assertEqual(len(v.g), 4)
        self.assertTrue(any("ch5" in k[1] for k in v.keys_f))
        self.assertTrue(any("ch9" in k[1] for k in v.keys_f))
        self.assertEqual(v.diagnostics[0]["isolation_25dB"], "recommendation_objective_not_hard")

    def test_conservative_aggregation(self):
        a = torch.tensor([-1.0, -0.4, 0.8], dtype=torch.float64)
        t = 0.02
        out = float(upper_max(a, t))
        self.assertGreaterEqual(out, 0.8)
        self.assertLessEqual(out, 0.8 + t * np.log(3) + 1e-14)

    def test_no_angle_dependent_offset(self):
        with self.assertRaises(ValueError):
            Policy(offset_rad=(0.1,) * 6)
        with self.assertRaises(ValueError):
            Policy(offset_mode="fit_each_angle")

    def test_no_zero_field_escape(self):
        ev, x = fixture()
        rho = ev.chain.density(x, 2.0)
        c = ev._contract(rho, 0.5)
        q = {
            j: {"q": torch.tensor(ev.runner.forward(rho, j, c, "values").fields["q"])}
            for j in (1, 2, 3, 4)
        }
        r = ev.response_builder(q)
        z = replace(
            r, e=torch.zeros_like(r.e), total_efficiency=torch.zeros_like(r.total_efficiency)
        )
        out = ev.objective.rows(z)
        self.assertTrue(torch.isfinite(out.objective).all())
        self.assertGreater(float(out.hard.max()), 0)
        for b in out.diagnostics["bands"].values():
            self.assertEqual(b["valid_fraction"], 0.0)
            self.assertGreaterEqual(b["terms"]["floor"], 1.0)

    def test_reverse_or_collapsed_baseline_penalized(self):
        ev, x = fixture()
        rho = ev.chain.density(x, 2.0)
        c = ev._contract(rho, 0.5)
        q = {
            j: {"q": torch.tensor(ev.runner.forward(rho, j, c, "values").fields["q"])}
            for j in (1, 2, 3, 4)
        }
        r = ev.response_builder(q)
        original = ev.objective.rows(r)
        rev = replace(r, e=r.e.flip(-1))
        wrong = ev.objective.rows(rev)
        self.assertGreater(
            wrong.diagnostics["bands"]["ch9"]["terms"]["slope"],
            original.diagnostics["bands"]["ch9"]["terms"]["slope"],
        )
        e = r.e.clone()
        e[..., 1] = e[..., 0]
        e[..., 3] = e[..., 2]
        collapsed = ev.objective.rows(replace(r, e=e))
        self.assertGreater(collapsed.diagnostics["bands"]["ch9"]["terms"]["slope"], 0.1)

    def test_hard_violation_cannot_average_away(self):
        ev, x = fixture()
        rho = ev.chain.density(x, 2.0)
        c = ev._contract(rho, 0.5)
        q = {
            j: {"q": torch.tensor(ev.runner.forward(rho, j, c, "values").fields["q"])}
            for j in (1, 2, 3, 4)
        }
        r = ev.response_builder(q)
        s = r.s.clone()
        s[3, 3, 3] = 0.95
        out = ev.objective.rows(replace(r, s=s))
        self.assertGreaterEqual(float(out.hard[-1]), 0.95**2 / 0.1 - 1 - 1e-12)

    def test_advice_does_not_change_hard_gate(self):
        ev, x = fixture()
        rho = ev.chain.density(x, 2.0)
        c = ev._contract(rho, 0.5)
        q = {
            j: {"q": torch.tensor(ev.runner.forward(rho, j, c, "values").fields["q"])}
            for j in (1, 2, 3, 4)
        }
        r = ev.response_builder(q)
        a = ev.objective.rows(r)
        b = JointObjective(replace(ev.objective.policy, isolation_recommendation_db=-30.0)).rows(r)
        torch.testing.assert_close(a.hard, b.hard, rtol=0, atol=0)

    def test_pair_block_value_and_cotangents_match_full_objective(self):
        ev, x = fixture()
        rho = ev.chain.density(x, 2.0)
        c = ev._contract(rho, 0.5)
        records = {j: ev.runner.forward(rho, j, c, "gradient") for j in (1, 2, 3, 4)}
        full = [Forward(j, r.fields, c, True) for j, r in records.items()]

        def full_fn(q):
            return ev.objective.rows(ev.response_builder(q)).objective[1]

        a = joint_wirtinger(full_fn, full, ("q",))
        pair = JointObjective(ev.objective.policy, bands=("ch9",))
        response = LinearResponse(pair.sampling, (3, 4))

        def pair_fn(q):
            return pair.rows(response(q)).objective[0]

        b = joint_wirtinger(pair_fn, [f for f in full if f.excitation in (3, 4)], ("q",))
        self.assertAlmostEqual(a.value, b.value, places=13)
        for j in (3, 4):
            np.testing.assert_allclose(
                a.cotangents[j]["q"], b.cotangents[j]["q"], rtol=1e-13, atol=1e-14
            )
        for j in (1, 2):
            self.assertFalse(np.any(a.cotangents[j]["q"]))
        for j in (3, 4):
            self.assertTrue(np.any(b.cotangents[j]["q"]))
        pe = JointEvaluator(
            pair,
            ev.chain,
            ev.runner,
            response,
            c,
            ("q",),
            2.0,
            (0.5,),
            evidence_persist=lambda event, payload: True,
        )
        with self.assertRaises(ValueError):
            UpdateLoop(pe, x)

    def test_pair_block_missing_field_is_not_fabricated(self):
        ev, x = fixture()
        rho = ev.chain.density(x, 2.0)
        c = ev._contract(rho, 0.5)
        record = ev.runner.forward(rho, 3, c, "values")
        response = LinearResponse(ev.objective.sampling, (3, 4))
        with self.assertRaises(ValueError):
            response({3: {"q": torch.tensor(record.fields["q"])}})


class MapAndGradientTests(AnalyticTest):
    def test_filter_projection_full_vjp(self):
        ev, x = fixture()
        rng = np.random.default_rng(8)
        d = rng.normal(size=x.size)
        g = rng.normal(size=ev.chain.baseline.shape)
        for eta in (0.4, 0.5, 0.6):
            analytic = float(ev.chain.vjp(x, 2.0, g, eta) @ d)
            h = 1e-6
            fd = float(
                np.sum(
                    g
                    * (
                        ev.chain.density(x + h * d, 2.0, eta)
                        - ev.chain.density(x - h * d, 2.0, eta)
                    )
                )
                / (2 * h)
            )
            self.assertAlmostEqual(analytic, fd, places=7)

    def test_fixed_copper_and_seed_disclosure(self):
        ev, x = fixture()
        for eta in (0.4, 0.5, 0.6):
            r = ev.chain.density(x, 2.0, eta)
            np.testing.assert_array_equal(r[ev.chain.fixed], ev.chain.fixed_value[ev.chain.fixed])
        self.assertTrue(
            any(v["changed_pixels"] > 0 for v in ev.chain.initialization(2.0, (0.5, 0.4, 0.6)))
        )

    def test_all_rows_true_field_adjoint_vs_fd(self):
        ev, x = fixture()
        v = ev.evaluate(x, True)
        rng = np.random.default_rng(36)
        for _ in range(2):
            d = rng.normal(size=len(x))
            d /= np.linalg.norm(d)
            h = 2e-6
            a, b = ev.evaluate(x + h * d, False), ev.evaluate(x - h * d, False)
            adj = np.r_[v.df @ d, v.dg @ d]
            fd = np.r_[(a.f - b.f) / (2 * h), (a.g - b.g) / (2 * h)]
            err = np.max(abs(adj - fd) / np.maximum(1e-8, abs(adj)))
            self.assertLess(err, 1e-5)
        self.assertGreater(ev.runner.adjoint_calls, 0)

    def test_every_morphology_contributes_rows_and_gradients(self):
        ev, x = fixture((0.5, 0.4, 0.6))
        v = ev.evaluate(x, True)
        self.assertEqual(v.f.shape, (6,))
        self.assertEqual(v.g.shape, (12,))
        self.assertEqual(v.df.shape, (6, len(x)))
        self.assertEqual(len(set(v.density_hashes)), 3)
        h = 1e-6
        d = np.ones_like(x) / np.sqrt(len(x))
        a, b = ev.evaluate(x + h * d, False), ev.evaluate(x - h * d, False)
        np.testing.assert_allclose(v.df @ d, (a.f - b.f) / (2 * h), rtol=1e-5, atol=1e-7)
        np.testing.assert_allclose(v.dg @ d, (a.g - b.g) / (2 * h), rtol=1e-5, atol=1e-7)

    def test_saved_value_validator_checks_all_rows(self):
        ev, x = fixture()
        v = ev.evaluate(x, True)
        d = np.ones(len(x)) * 0.1
        values = {}
        for h in (2e-4, 1e-4, 5e-5):
            q = ev.evaluate(x + h * d, False)
            values[h] = np.r_[q.f, q.g]
        jac = np.r_[v.df, v.dg]
        r = check_direction(np.r_[v.f, v.g], jac, jac, d, values)
        self.assertTrue(r["passed"])
        self.assertFalse(r["finite_differences_supply_optimizer_gradients"])
        bad = check_direction(np.r_[v.f, v.g], -jac, -jac, d, values)
        self.assertFalse(bad["passed"])
        with self.assertRaises(ValueError):
            check_direction(np.r_[v.f, v.g], jac, jac, d, values, relative_gate=0.1)


class FidelityTests(unittest.TestCase):
    def test_constant_delay_not_dispersion_removed(self):
        f = np.linspace(7.7376e9, 8.2368e9, 33)
        x = np.ones(33)
        w = np.ones(33)
        tau = np.array([-100e-12, 0, 100e-12])
        h = torch.tensor(np.exp(-2j * np.pi * f * 100e-12))[:, None]
        v, valid = pulse_fidelity(h, f, x, w, tau)
        self.assertTrue(bool(valid.all()))
        self.assertAlmostEqual(float(v[0]), 1.0, places=12)
        distorted = h * torch.tensor(np.exp(1j * 5 * ((f - f.mean()) / np.ptp(f)) ** 2))[:, None]
        d, _ = pulse_fidelity(distorted, f, x, w, tau)
        self.assertLess(float(d[0]), 0.99)

    def test_zero_link_is_not_perfect_fidelity(self):
        f = np.linspace(6.24e9, 6.7392e9, 12)
        v, valid = pulse_fidelity(
            torch.zeros((12, 1), dtype=torch.complex128), f, np.ones(12), np.ones(12), [0.0]
        )
        self.assertFalse(bool(valid[0]))
        self.assertEqual(float(v[0]), 0.0)

    def test_sparse_derivative_samples_are_not_waveform_evidence(self):
        with self.assertRaises(ValueError):
            pulse_fidelity(
                torch.ones(6, dtype=torch.complex128), np.arange(6.0), np.ones(6), np.ones(6), [0.0]
            )
