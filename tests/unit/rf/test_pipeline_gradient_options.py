"""The pipeline gradient (test_pipeline_gradient.py) with the options the cases do not use:
the reactive interpolation (alone and with the copper-edge correction and modal source), the
radiation objective of `optimizer.epoch_objectives`, the lost fraction (`Loss`) and the modal
port waves with the closed radiation box (design §25); split from
test_pipeline_gradient.py so that the two run side by side (one thread each).
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf.spec import Loss, Lumped, OptimizerSpec, RadiatedFraction
from yapnr.rf.testing import GradientChecks, tiny_spec


class ReactiveEdgeCorrectionPipelineGradientTest(GradientChecks, unittest.TestCase):
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
        cls.setup_gradient(spec, 8)

    def test_both_models_on(self):
        self.assertTrue(self.p.sim.structure.inductive)
        self.assertTrue(self.p.sim.structure.edge_correction)


class ModalPortsPipelineGradientTest(GradientChecks, unittest.TestCase):
    """The modal port waves (`ports.ModalPlane`, design §25.1) and the closed radiation box's
    non-guided fraction (its feed faces separated modally on the full transverse planes, §25.2):
    linear functionals of the plane probes, whose adjoint sources fill the planes."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True, extraction="modal")
        spec = spec.replace(solver=replace(spec.solver, port_source="mode"))
        cls.setup_gradient(spec, 9)

    def test_modal_planes_are_probed(self):
        names = {p.name for p in self.p.port_probes + self.p.box_probes}
        self.assertIn("p1_mode_x+_ey", names)
        self.assertIn("rad_p2_x+_hz_lo", names)


class RadiationObjectivePipelineGradientTest(GradientChecks, unittest.TestCase):
    """The same with the radiation objective (`optimizer.epoch_objectives`): one scalar,
    log(1 + R̄) − log η̄ over the band, whose gradient sums the per-frequency recombinations of
    one adjoint run."""

    OBJECTIVE = "radiation"

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        reqs = spec.requirements[:-1] + (RadiatedFraction(1).at_least(0.5, band="b"),)
        spec = spec.replace(requirements=reqs, optimizer=OptimizerSpec(damping=0.5))
        cls.setup_gradient(spec, 6)

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


class LossPipelineGradientTest(GradientChecks, unittest.TestCase):
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
        cls.setup_gradient(spec, 10)

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


class ReactivePipelineGradientTest(GradientChecks, unittest.TestCase):
    """The same with the reactive interpolation: gray pixels are inductive sheets R_s − iωL(ρ̄)
    with branch currents in the engine, and the gradient is 2 Re[K ∂Y_d/∂L] L'(ρ̄)."""

    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(optimizer=OptimizerSpec(interpolation="reactive"))
        cls.setup_gradient(spec, 4)

    def test_sheet_is_inductive(self):
        self.assertTrue(self.p.sim.structure.inductive)


if __name__ == "__main__":
    unittest.main()
