"""Specs as transfer functions, and their objective groups (design §9)."""

from __future__ import annotations

import math
import os
import tempfile
import unittest

import numpy as np
import torch
import yaml

from yapnr.rf.objectives import build_groups, group_values, phi, violations
from yapnr.rf.spec import (
    Absorbed,
    Band,
    GridSpec,
    Loss,
    Lumped,
    OptimizerSpec,
    Port,
    RadiatedFraction,
    Requirement,
    S,
    Spec,
    StackupSpec,
)

DIVIDER_YAML = """
schema: yapnr-rf-spec/1
name: divider-x10
stackup: { er: 3.55, tan_delta: 0.0027, h_mm: 0.813, f_ref_ghz: 10 }
grid: { pitch_mm: 0.3, substrate_cells: 4 }
design_region: { x_mm: [0, 9.6], y_mm: [-6.0, 6.0] }
symmetry: mirror_y
rules: { min_width_mm: 0.6, min_space_mm: 0.6 }
ports:
  - { n: 1, side: W, at_mm: 0.0, width_cells: auto }
  - { n: 2, side: E, at_mm: 4.2, width_cells: auto }
  - { n: 3, side: E, at_mm: -4.2, width_cells: auto }
bands:
  pass: { ghz: [8.5, 11.5], points: 7 }
requirements:
  - { s: [1, 1], max_db: -20, band: pass }
  - { s: [2, 1], min_db: -3.28, band: pass }
  - { s: [3, 1], min_db: -3.28, band: pass }
optimizer: { betas: [8, 16, 32, 64, 128], iterations_per_beta: 30, budget_min: 45 }
"""


def _small(**kw):
    base = dict(
        name="t",
        stackup=StackupSpec(3.55, 0.0027, 0.813, 10.0),
        grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
        design_region=(0.0, 3.0, -1.5, 1.5),
        ports=(Port(1, "W", 0.0, 6), Port(2, "E", 0.0, 6)),
        bands={"a": Band(8.0, 9.0, 3), "b": Band(9.0, 12.0, 4)},
        requirements=(S(2, 1).at_least_db(-1.0, band="a"), S(1, 1).at_most_db(-15, band="b")),
    )
    base.update(kw)
    return Spec(**base)


class SpecTest(unittest.TestCase):
    def test_yaml_example(self):
        spec = Spec.from_dict(yaml.safe_load(DIVIDER_YAML))
        self.assertEqual(spec.name, "divider-x10")
        self.assertEqual(spec.symmetry, "mirror_y")
        self.assertEqual([p.width_cells for p in spec.ports], [None, None, None])
        self.assertEqual(len(spec.requirements), 3)
        self.assertEqual(spec.excitations, (1,))
        np.testing.assert_allclose(spec.objective_frequencies(), np.linspace(8.5, 11.5, 7) * 1e9)
        self.assertEqual(spec.optimizer.budget_min, 45)

    def test_round_trip_and_hash(self):
        spec = Spec.from_dict(yaml.safe_load(DIVIDER_YAML))
        again = Spec.from_dict(spec.to_dict())
        self.assertEqual(again.canonical_json(), spec.canonical_json())
        self.assertEqual(again.sha256(), spec.sha256())
        self.assertNotEqual(spec.replace(name="other").sha256(), spec.sha256())
        with tempfile.TemporaryDirectory() as d:
            for ext, text in ((".yaml", DIVIDER_YAML), (".json", spec.canonical_json())):
                path = os.path.join(d, "s" + ext)
                with open(path, "w") as fh:
                    fh.write(text)
                self.assertEqual(Spec.load(path).sha256(), spec.sha256())

    def test_requirement_forms(self):
        d = [
            {"s": [2, 1], "between_db": [-3.5, -3.0], "band": "a"},
            {"s": [1, 1], "mask_db": [[8, -10], [9, -20]], "band": "a"},
            {"s": [2, 1], "min_mask_db": [[8, -2], [9, -1]], "band": "a", "scale": 0.5},
            {"s": [2, 1], "phase_deg": 90, "tol_deg": 5, "band": "a"},
            {"radiated": 1, "min": 0.7, "band": "a"},
            {"absorbed": 2, "element": "R1", "min": 0.4, "band": "a"},
            {"loss": 2, "max": 0.08, "band": "a"},
        ]
        reqs = [r for x in d for r in Requirement.from_dict(x)]
        self.assertEqual(len(reqs), 8)
        self.assertEqual((reqs[0].bound, reqs[1].bound), ("min", "max"))
        np.testing.assert_allclose(reqs[2].limit_at([8e9, 8.5e9, 9e9]), [-10, -15, -20])
        self.assertEqual(reqs[3].scale_value, 0.5)
        self.assertEqual(reqs[4].quantity, "phase")
        self.assertEqual(reqs[5].excitation, 1)
        self.assertEqual((reqs[6].excitation, reqs[6].element, reqs[6].scale_value), (2, "R1", 0.1))
        for r in reqs:
            self.assertEqual(Requirement.from_dict(r.to_dict()), [r])
        py = [
            *S(2, 1).between_db(-3.5, -3.0, band="a"),
            S(1, 1).mask_db([(8, -10), (9, -20)], band="a"),
            S(2, 1).mask_db([(8, -2), (9, -1)], kind="min", band="a", scale=0.5),
            S(2, 1).phase_deg(90, tol=5, band="a"),
            RadiatedFraction(1).at_least(0.7, band="a"),
            Absorbed("R1", 2).at_least(0.4, band="a"),
            Loss(2).at_most(0.08, band="a"),
        ]
        self.assertEqual(py, reqs)
        self.assertEqual((reqs[7].excitation, reqs[7].scale_value), (2, 0.1))

    def test_validation(self):
        with self.assertRaises(ValueError):
            _small(requirements=(S(2, 1).at_least_db(-1, band="nope"),))
        with self.assertRaises(ValueError):
            _small(ports=(Port(1, "W", 0.0, 6), Port(3, "E", 0.0, 6)))
        with self.assertRaises(ValueError):
            _small(design_region=(0.0, 3.1, -1.5, 1.5))
        with self.assertRaises(ValueError):
            _small(requirements=(S(4, 1).at_least_db(-1, band="a"),))
        with self.assertRaises(ValueError):
            Requirement("phase", (2, 1), "target", 90.0, "a")  # no tolerance
        r1 = Lumped("R1", (1.0, 1.6), (-0.3, 0.3), "y", 100.0, 0.6)
        absorbed = (S(2, 1).at_least_db(-1, band="a"), Absorbed("R1", 2).at_least(0.4, band="a"))
        self.assertEqual(_small(requirements=absorbed, lumped=(r1,)).excitations, (1, 2))
        with self.assertRaises(ValueError):  # no such element
            _small(requirements=absorbed)
        with self.assertRaises(ValueError):  # absorbed needs an element
            Requirement("absorbed", (2,), "min", 0.4, "a")

    def test_epoch_objectives(self):
        rad = (RadiatedFraction(1).at_least(0.7, band="a"),)
        opt = OptimizerSpec(betas=(2, 8), epoch_objectives=("radiation", "spec"))
        spec = _small(requirements=rad, optimizer=opt)
        again = Spec.from_dict(spec.to_dict())
        self.assertEqual(again.optimizer.epoch_objectives, ("radiation", "spec"))
        self.assertEqual(again.sha256(), spec.sha256())
        # Left out of the hash at its default, so earlier specs keep theirs.
        self.assertNotIn("epoch_objectives", _small().to_dict()["optimizer"])
        with self.assertRaises(ValueError):  # one per β epoch
            _small(
                requirements=rad,
                optimizer=OptimizerSpec(betas=(2, 8), epoch_objectives=("radiation",)),
            )
        with self.assertRaises(ValueError):  # unknown objective
            _small(requirements=rad, optimizer=OptimizerSpec(betas=(8,), epoch_objectives=("x",)))
        with self.assertRaises(ValueError):  # radiation needs a radiated-fraction requirement
            _small(optimizer=OptimizerSpec(betas=(8,), epoch_objectives=("radiation",)))

    def test_frequencies_union(self):
        spec = _small()
        f = spec.objective_frequencies() / 1e9
        np.testing.assert_allclose(f, [8.0, 8.5, 9.0, 10.0, 11.0, 12.0])
        np.testing.assert_array_equal(spec.band_mask("a", f * 1e9), [1, 1, 1, 0, 0, 0])
        np.testing.assert_array_equal(spec.band_mask("b", f * 1e9), [0, 0, 1, 1, 1, 1])
        self.assertIsNotNone(
            _small(requirements=(RadiatedFraction(1).at_least(0.5, band="a"),)).radiation
        )


class ObjectiveTest(unittest.TestCase):
    def _q(self, s21, s11, eta=0.5):
        m = s21.size
        t = lambda v: torch.as_tensor(np.asarray(v, dtype=complex))  # noqa: E731
        return {
            "s": {(2, 1): t(s21), (1, 1): t(s11)},
            "eta": {1: torch.full((m,), eta, dtype=torch.float64)},
        }

    def test_phi_forms(self):
        f = np.array([8e9, 9e9])
        q = self._q(np.array([0.5, 0.5j]), np.array([0.1, 0.01]))
        x21 = 10 * math.log10(0.25 + 1e-10)
        v = phi(S(2, 1).at_least_db(-3.0, band="a"), q, f).numpy()
        np.testing.assert_allclose(v, [(-3.0 - x21) / 1.0] * 2)
        v = phi(S(1, 1).at_most_db(-20.0, band="a"), q, f).numpy()
        np.testing.assert_allclose(v, [(-20.0 + 20.0) / 10, (-40.0 + 20.0) / 10], atol=1e-6)
        # ∠conj(S): S21 = 0.5j internally is −90° in the engineering convention.
        v = phi(S(2, 1).phase_deg(-90, tol=10, band="a"), q, f).numpy()
        self.assertAlmostEqual(float(v[1]), -1.0)
        self.assertGreater(float(v[0]), 0.0)
        v = phi(RadiatedFraction(1).at_least(0.7, band="a"), q, f).numpy()
        np.testing.assert_allclose(v, [2.0, 2.0])
        q["absorbed"] = {("R1", 1): torch.tensor([0.25, 0.45], dtype=torch.float64)}
        v = phi(Absorbed("R1", 1).at_least(0.4, band="a"), q, f).numpy()
        np.testing.assert_allclose(v, [1.5, -0.5])
        q["loss"] = {1: torch.tensor([0.02, 0.18], dtype=torch.float64)}
        v = phi(Loss(1).at_most(0.08, band="a"), q, f).numpy()
        np.testing.assert_allclose(v, [-0.6, 1.0])
        v = phi(S(1, 1).mask_db([(8, -10), (9, -30)], band="a"), q, f).numpy()
        np.testing.assert_allclose(v, [(-20 + 10) / 10, (-40 + 30) / 10], atol=1e-6)

    def test_groups_and_lse(self):
        spec = _small(
            requirements=(
                S(2, 1).at_least_db(-1.0, band="a"),
                S(1, 1).at_most_db(-15, band="b"),
                S(1, 2).at_most_db(-15, band="b"),
            )
        )
        f = spec.objective_frequencies()
        groups = build_groups(spec, f)
        self.assertEqual([g.name for g in groups], ["x1", "x2"])
        np.testing.assert_array_equal(groups[0].frequencies_active, [1, 1, 1, 1, 1, 1])
        np.testing.assert_array_equal(groups[1].frequencies_active, [0, 0, 1, 1, 1, 1])
        rng = np.random.default_rng(0)
        s21 = 0.9 * np.exp(1j * rng.random(f.size))
        s11 = 0.2 * np.exp(1j * rng.random(f.size))
        q = self._q(s21, s11)
        tau = 0.05
        lse = group_values(groups[0], q, f, tau).numpy()
        p = np.array([phi(r, q, f).numpy() for r in groups[0].requirements])
        p = np.where(groups[0].active, p, -np.inf)
        true_max = p.max(axis=0)
        k = groups[0].active.sum(axis=0)
        self.assertTrue(np.all(lse >= true_max - 1e-12))
        self.assertTrue(np.all(lse <= true_max + tau * np.log(k) + 1e-12))
        none = build_groups(spec.replace(optimizer=OptimizerSpec(aggregate="none")), f)
        self.assertEqual(len(none), 3)
        single = group_values(none[0], q, f, tau).numpy()
        np.testing.assert_allclose(single[:3], p[0, :3])
        rep = violations(spec, q, f, excitation=1)
        self.assertEqual(len(rep), 2)
        self.assertTrue(np.isnan(rep["2: |S11| ≤ -15 dB @ b"][0]))


if __name__ == "__main__":
    unittest.main()
