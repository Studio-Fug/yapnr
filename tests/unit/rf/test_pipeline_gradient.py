"""The gradient of the whole optimization pipeline against finite differences (design §6.7).

x (half the DOF, mirror symmetry) → material grid with fixed pads and the feed ring → conic
filter → tanh projection → log-interpolated sheet conductance with damping → FDTD → port waves
with de-embedding and the radiated fraction → log-sum-exp group per frequency; one adjoint run
gives every per-frequency gradient, pulled back through the parameterization. Compared with
Richardson-extrapolated central differences along a random direction (measured: 1e-10
relative); also the length-scale constraint gradients at β = 128.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf.problem import Problem
from yapnr.rf.spec import Absorbed, Loss, Lumped, OptimizerSpec, RadiatedFraction, S
from yapnr.rf.testing import nominal_calibration, tiny_spec


class PipelineGradientTest(unittest.TestCase):
    OBJECTIVE = "spec"

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(optimizer=OptimizerSpec(damping=0.5))
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(3)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta), objective=cls.OBJECTIVE)
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def _f(self, x):
        rho = self.p.param.rho_bar(x, self.beta)
        return self.p.evaluate(rho, gradients=False, objective=self.OBJECTIVE).values

    def test_runs_converged(self):
        self.assertTrue(self.ev.converged)
        self.assertEqual(len(self.ev.keys), 3)

    def test_directional_derivative(self):
        h = 1e-4
        x, v = self.x, self.v
        d1 = (self._f(x + h * v) - self._f(x - h * v)) / (2 * h)
        d2 = (self._f(x + 0.5 * h * v) - self._f(x - 0.5 * h * v)) / h
        fd = (4 * d2 - d1) / 3
        adj = self.grad @ v
        np.testing.assert_allclose(adj, fd, rtol=1e-6)

    def test_lengthscale_gradient(self):
        ls = self.p.lengthscale
        self.assertIsNotNone(ls)
        h = 1e-5
        g, dg = self.p.param.lengthscale(self.x, 128.0, ls)
        gp, _ = self.p.param.lengthscale(self.x + h * self.v, 128.0, ls)
        gm, _ = self.p.param.lengthscale(self.x - h * self.v, 128.0, ls)
        np.testing.assert_allclose(dg @ self.v, (gp - gm) / (2 * h), rtol=1e-5)


class EdgeCorrectionPipelineGradientTest(PipelineGradientTest):
    """The same with the copper-edge correction (`edges`: ε and μ next to the copper's edges
    follow ρ̄) and the modal port source; the correction's part of the gradient comes from E
    and H DTFTs around the copper plane (`edges.kernels`). Measured: 1e-10 relative."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(
            optimizer=OptimizerSpec(damping=0.5),
            solver=replace(spec.solver, edge_correction=True, port_source="mode"),
        )
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(5)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_edge_part_matters(self):
        p = self.p
        self.assertTrue(p.sim.structure.edge_correction)
        orig = p._edge_gradient
        p._edge_gradient = lambda gr: 0.0
        try:
            ev = p.evaluate(p.param.rho_bar(self.x, self.beta))
        finally:
            p._edge_gradient = orig
        without = p.param.vjp(self.x, self.beta, ev.grads) @ self.v
        full = self.grad @ self.v
        self.assertGreater(np.abs(full - without).min(), 1e-3 * np.abs(full).max())


class ReactiveEdgeCorrectionPipelineGradientTest(PipelineGradientTest):
    """The reactive interpolation together with the copper-edge correction and the modal source
    (the Wilkinson-type combiner's settings): both parts of the design gradient, the inductive
    branches' and the edge factors', in one adjoint run."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(
            optimizer=OptimizerSpec(interpolation="reactive"),
            solver=replace(spec.solver, edge_correction=True, port_source="mode"),
        )
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(8)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_both_models_on(self):
        self.assertTrue(self.p.sim.structure.inductive)
        self.assertTrue(self.p.sim.structure.edge_correction)


class RadiationObjectivePipelineGradientTest(PipelineGradientTest):
    """The same with the radiation objective (`optimizer.epoch_objectives`): one scalar,
    log(1 + R̄) − log η̄ over the band, whose gradient sums the per-frequency recombinations of
    one adjoint run."""

    OBJECTIVE = "radiation"

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        reqs = spec.requirements[:-1] + (RadiatedFraction(1).at_least(0.5, band="b"),)
        spec = spec.replace(requirements=reqs, optimizer=OptimizerSpec(damping=0.5))
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(6)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta), objective=cls.OBJECTIVE)
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_runs_converged(self):
        self.assertTrue(self.ev.converged)
        self.assertEqual(len(self.ev.keys), 1)
        self.assertEqual(self.ev.keys[0][0], "radiation1")

    def test_value(self):
        s11 = np.abs(self.ev.s[:, 0, 0]) ** 2
        eta = self.ev.eta[1]
        band = self.p.spec.band_mask(self.p.spec.requirements[-1].band, self.p.freqs)
        expect = np.log(1.0 + s11[band].mean()) - np.log(eta[band].mean())
        self.assertAlmostEqual(float(self.ev.values[0]), float(expect), places=12)


class ReferenceOhmPipelineGradientTest(PipelineGradientTest):
    """A one-port spec judged at a 50 Ω reference (`optimizer.reference_ohm`): the reflection
    renormalized from the feed's Z_c (72 Ω in the nominal calibration) inside the objective."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(
            ports=spec.ports[:1],
            requirements=(
                S(1, 1).at_most_db(-15, band="b"),
                RadiatedFraction(1).at_least(0.5, band="b"),
            ),
            optimizer=OptimizerSpec(damping=0.5, reference_ohm=50.0),
        )
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(7)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_runs_converged(self):
        self.assertTrue(self.ev.converged)
        self.assertEqual(len(self.ev.keys), 3)

    def test_matches_the_validators_renormalization(self):
        from yapnr.rf import sparams

        p = self.p
        sw = p.sweep(p.param.rho_bar(self.x, self.beta), freqs=p.freqs)
        s50 = sparams.renormalize(sw["s"], sw["zc"], 50.0)
        np.testing.assert_allclose(self.ev.s[:, 0, 0], s50[:, 0, 0], rtol=1e-9, atol=1e-12)
        self.assertGreater(np.max(np.abs(sw["s"][:, 0, 0] - s50[:, 0, 0])), 0.05)


class AbsorbedPipelineGradientTest(PipelineGradientTest):
    """A lumped resistor's share of the incident power (`Absorbed`, the Wilkinson-type
    combiner's resistor requirement): ½ c_ω Σ σ_e V_e |Ê_e|² over the part's edges, an adjoint
    source on them, with the copper-edge correction and the modal source as in the cases."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec()
        spec = spec.replace(
            lumped=(Lumped("R1", (0.8, 1.6), (-0.4, 0.4), "y", 50.0, 0.4),),
            requirements=spec.requirements + (Absorbed("R1", 1).at_least(0.3, band="b"),),
            optimizer=OptimizerSpec(damping=0.5),
            solver=replace(spec.solver, edge_correction=True, port_source="mode"),
        )
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(9)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_absorbed_value(self):
        from yapnr.rf.fdtd.monitors import dissipated_power

        p = self.p
        rho = p.param.rho_bar(self.x, self.beta)
        p.set_design(rho)
        fwd = p.forward(1, design=False)
        q = p.quantities(fwd.dft, 1)
        a = q["absorbed"][("R1", 1)]
        self.assertTrue(np.all(a > 1e-4) and np.all(a < 1.0), a)
        # The same as the monitor's dissipation on the part's edges less the sheet's share.
        probe, _ = p.lumped_probes["R1"]
        st = p.sim.structure
        sheet = (
            st.sigma(probe.comp).reshape(-1)[probe.index]
            - st._extra[probe.comp].reshape(-1)[probe.index]
        )
        total = dissipated_power(st, [probe], fwd.dft, p.omega, p.dt)
        c = np.cos(p.omega * p.dt / 2.0)
        vol = p.grid.volume(probe.comp).reshape(-1)[probe.index]
        own = total - 0.5 * c * (np.abs(fwd.dft[probe.name]) ** 2 * sheet * vol).sum(-1)
        from yapnr.rf import sparams

        p_inc = sparams.incident_power(q["waves"][1][0], p.cal[1], p.omega)
        np.testing.assert_allclose(a, own / p_inc, rtol=1e-9)
        phi = self.ev.phi[f"{len(p.spec.requirements)}: R1/P1 ≥ 0.3 @ b"]
        np.testing.assert_allclose(phi, (0.3 - a) / 0.1, rtol=1e-9, atol=1e-12)


class LossPipelineGradientTest(PipelineGradientTest):
    """The lost fraction (`Loss`, the combiner's bound on gray copper's dissipation): the net
    power into the device less the net power out of the other ports and into the lumped
    resistor, over the incident power, from the waves and the resistor's probes."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec()
        spec = spec.replace(
            lumped=(Lumped("R1", (0.8, 1.6), (-0.4, 0.4), "y", 50.0, 0.4),),
            requirements=spec.requirements + (Loss(1).at_most(0.02, band="b"),),
            optimizer=OptimizerSpec(damping=0.5),
            solver=replace(spec.solver, edge_correction=True, port_source="mode"),
        )
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(10)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_loss_value(self):
        from yapnr.rf import sparams

        p = self.p
        self.assertIn("R1", p.lumped_probes)  # every resistor is probed for the loss
        p.set_design(p.param.rho_bar(self.x, self.beta))
        fwd = p.forward(1, design=False)
        q = p.quantities(fwd.dft, 1)
        w = q["waves"]
        pw = {
            n: (
                sparams.incident_power(a, p.cal[n], p.omega),
                sparams.incident_power(b, p.cal[n], p.omega),
            )
            for n, (a, b) in w.items()
        }
        p_in = pw[1][0] - pw[1][1]
        p_out = pw[2][1] - pw[2][0]
        share = q["absorbed"][("R1", 1)]
        expect = (p_in - p_out) / pw[1][0] - share
        np.testing.assert_allclose(q["loss"][1], expect, rtol=1e-12, atol=1e-14)
        self.assertTrue(np.all(q["loss"][1] > 0.0) and np.all(q["loss"][1] < 1.0), q["loss"][1])
        # Lossy gray copper: the lost fraction is far above the binary line's.
        self.assertGreater(float(np.min(q["loss"][1])), 0.05)


class ReactivePipelineGradientTest(PipelineGradientTest):
    """The same with the reactive interpolation: gray pixels are inductive sheets R_s − iωL(ρ̄)
    with branch currents in the engine, and the gradient is 2 Re[K ∂Y_d/∂L] L'(ρ̄)."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(optimizer=OptimizerSpec(interpolation="reactive"))
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(4)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def test_sheet_is_inductive(self):
        self.assertTrue(self.p.sim.structure.inductive)


if __name__ == "__main__":
    unittest.main()
