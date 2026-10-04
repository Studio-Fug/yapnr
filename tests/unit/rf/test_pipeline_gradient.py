"""The gradient of the whole optimization pipeline against finite differences (design §6.7).

x (half the DOF, mirror symmetry) → material grid with fixed pads and the feed ring → conic
filter → tanh projection → log-interpolated sheet conductance with damping → FDTD → port waves
with de-embedding and the radiated fraction → log-sum-exp group per frequency; one adjoint run
gives every per-frequency gradient, pulled back through the parameterization. Compared with
Richardson-extrapolated central differences along a random direction (measured: 1e-10
relative); also the length-scale constraint gradients at β = 128 (`testing.GradientChecks`).
This file has the settings the cases use (the copper-edge correction and modal source, the
50 Ω reference, a lumped resistor's share); test_pipeline_gradient_options.py the other
options, so that the two run side by side (one thread each).
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf.spec import Absorbed, Lumped, OptimizerSpec, RadiatedFraction, S
from yapnr.rf.testing import GradientChecks, tiny_spec


class PipelineGradientTest(GradientChecks, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        cls.setup_gradient(spec.replace(optimizer=OptimizerSpec(damping=0.5)), 3)


class EdgeCorrectionPipelineGradientTest(GradientChecks, unittest.TestCase):
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
        cls.setup_gradient(spec, 5)

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


class ReferenceOhmPipelineGradientTest(GradientChecks, unittest.TestCase):
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
        cls.setup_gradient(spec, 7)

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


class AbsorbedPipelineGradientTest(GradientChecks, unittest.TestCase):
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
        cls.setup_gradient(spec, 9)

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


if __name__ == "__main__":
    unittest.main()
