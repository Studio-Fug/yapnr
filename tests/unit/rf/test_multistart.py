"""Multi-start schema, derivation and perturbation (design `multistart.md` §1)."""

from __future__ import annotations

import dataclasses
import unittest

import numpy as np

from yapnr.rf.multistart import _uniform01, derive_starts, perturb
from yapnr.rf.spec import Spec
from yapnr.rf.testing import tiny_spec


class SchemaTest(unittest.TestCase):
    def test_no_starts_unchanged_hash(self):
        """A spec with no `starts` key hashes exactly as it did before `starts` existed."""
        s = tiny_spec()
        self.assertNotIn("starts", s.to_dict())
        self.assertIsNone(s.starts)

    def test_default_perturbation_fields_excluded_from_hash(self):
        """`perturb_amplitude`/`perturb_seed` at their defaults never change a spec's hash."""
        base = tiny_spec()
        same = Spec.from_dict(base.to_dict())
        self.assertEqual(base.sha256(), same.sha256())
        self.assertNotIn("perturb_amplitude", base.to_dict()["optimizer"])
        self.assertNotIn("perturb_seed", base.to_dict()["optimizer"])

    def test_nonzero_perturbation_changes_hash(self):
        base = tiny_spec()
        perturbed = base.replace(
            optimizer=dataclasses.replace(base.optimizer, perturb_amplitude=0.1)
        )
        self.assertNotEqual(base.sha256(), perturbed.sha256())

    def test_vary_key_must_be_on_allow_list(self):
        with self.assertRaises(ValueError):
            tiny_spec().replace(starts={"vary": {"betas": [[8.0], [16.0]]}})

    def test_unknown_top_level_key_refused(self):
        with self.assertRaises(ValueError):
            tiny_spec().replace(starts={"vary": {"move": [0.2, 0.18]}, "bogus": 1})

    def test_zip_length_mismatch_refused(self):
        with self.assertRaises(ValueError):
            tiny_spec().replace(
                starts={
                    "vary": {"move": [0.2, 0.18, 0.19], "seed": ["a", "b"]},
                    "combine": "zip",
                }
            )

    def test_zip_broadcasts_length_one(self):
        s = tiny_spec().replace(starts={"vary": {"move": [0.2, 0.18, 0.19], "perturb_seed": [0]}})
        self.assertEqual(s.starts["vary"]["perturb_seed"], [0])

    def test_starts_field_excluded_from_generated_hash(self):
        """L2: `starts` must not be in dataclass's `__hash__` tuple (it's a plain dict).

        `Spec` is unhashable regardless (the pre-existing `bands: dict` field, on `main`
        before this branch) and fixing that is out of this branch's scope; this only checks
        that `starts` itself was not left as a second, branch-introduced cause of the same
        `TypeError`.
        """
        starts_field = next(f for f in dataclasses.fields(Spec) if f.name == "starts")
        self.assertFalse(starts_field.hash)

    def test_rung_missing_after_epoch_is_value_error(self):
        """M4: a malformed rung must raise ValueError, not KeyError."""
        with self.assertRaises(ValueError):
            tiny_spec().replace(
                starts={
                    "vary": {"move": [0.2, 0.18]},
                    "halving": {"rungs": [{"keep": 0.5}]},
                }
            )

    def test_rung_keep_must_be_numeric(self):
        """M4: a string `keep` used to raise TypeError from the `0 < keep < 1` comparison."""
        with self.assertRaises(ValueError):
            tiny_spec().replace(
                starts={
                    "vary": {"move": [0.2, 0.18]},
                    "halving": {"rungs": [{"after_epoch": 0, "keep": "0.5"}]},
                }
            )

    def test_rung_after_last_epoch_refused(self):
        """M4: a rung at or past the schedule's last epoch would be silently skipped."""
        base = tiny_spec()
        n_epochs = len(base.optimizer.betas)
        with self.assertRaises(ValueError):
            derive_starts(
                base.replace(
                    starts={
                        "vary": {"move": [0.2, 0.18]},
                        "halving": {"rungs": [{"after_epoch": n_epochs - 1, "keep": 0.5}]},
                    }
                )
            )

    def test_perturb_seed_without_amplitude_refused(self):
        """H1: a `perturb_seed` varied with `perturb_amplitude` at 0 does nothing."""
        with self.assertRaises(ValueError):
            derive_starts(tiny_spec().replace(starts={"vary": {"perturb_seed": [0, 1, 2]}}))

    def test_keep_must_lie_in_open_unit_interval(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                tiny_spec().replace(
                    starts={
                        "vary": {"move": [0.2, 0.18]},
                        "halving": {"rungs": [{"after_epoch": 0, "keep": bad}]},
                    }
                )

    def test_rung_epochs_must_increase(self):
        with self.assertRaises(ValueError):
            tiny_spec().replace(
                starts={
                    "vary": {"move": [0.2, 0.18, 0.19]},
                    "halving": {
                        "rungs": [
                            {"after_epoch": 1, "keep": 0.5},
                            {"after_epoch": 1, "keep": 0.5},
                        ]
                    },
                }
            )

    def test_n_at_most_64(self):
        with self.assertRaises(ValueError):
            tiny_spec().replace(
                starts={
                    "vary": {
                        "move": [round(0.1 + 0.02 * i, 3) for i in range(9)],
                        "seed": [str(i) for i in range(9)],
                    },
                    "combine": "product",
                }
            )


class DeriveTest(unittest.TestCase):
    def _spec(self, moves, combine="zip"):
        return tiny_spec().replace(starts={"vary": {"move": moves}, "combine": combine})

    def test_zip_derivation_count_and_overrides(self):
        s = self._spec([0.2, 0.18, 0.19, 0.21, 0.22])
        derived = derive_starts(s)
        self.assertEqual(len(derived), 5)
        self.assertEqual([d.optimizer.move for d in derived], [0.2, 0.18, 0.19, 0.21, 0.22])

    def test_start_zero_equals_base(self):
        s = self._spec([0.2, 0.18])
        derived = derive_starts(s)
        base = s.replace(starts=None)
        self.assertEqual(derived[0].sha256(), base.sha256())

    def test_duplicate_design_sha_refused(self):
        s = self._spec([0.2, 0.2])
        with self.assertRaises(ValueError):
            derive_starts(s)

    def test_product_combine(self):
        base_move = tiny_spec().optimizer.move
        base_seed = tiny_spec().optimizer.perturb_seed
        s = tiny_spec().replace(
            optimizer=dataclasses.replace(tiny_spec().optimizer, perturb_amplitude=0.05),
            starts={
                "vary": {"move": [base_move, 0.18], "perturb_seed": [base_seed, 1]},
                "combine": "product",
            },
        )
        derived = derive_starts(s)
        pairs = {(d.optimizer.move, d.optimizer.perturb_seed) for d in derived}
        self.assertEqual(
            pairs,
            {
                (base_move, base_seed),
                (base_move, 1),
                (0.18, base_seed),
                (0.18, 1),
            },
        )

    def test_derived_spec_hash_distinct_per_start(self):
        s = self._spec([0.2, 0.18, 0.19])
        derived = derive_starts(s)
        shas = {d.sha256() for d in derived}
        self.assertEqual(len(shas), 3)

    def test_starts_stripped_from_derived_specs(self):
        s = self._spec([0.2, 0.18])
        for d in derive_starts(s):
            self.assertIsNone(d.starts)


class PerturbTest(unittest.TestCase):
    def test_zero_amplitude_is_identity(self):
        x0 = np.array([0.1, 0.5, 0.9, 0.0, 1.0])
        np.testing.assert_array_equal(perturb(x0, 0.0, seed=7), x0)

    def test_deterministic_on_seed_and_index(self):
        x0 = np.full(100, 0.5)
        a = perturb(x0, 0.1, seed=42)
        b = perturb(x0, 0.1, seed=42)
        np.testing.assert_array_equal(a, b)

    def test_different_seeds_differ(self):
        x0 = np.full(100, 0.5)
        a = perturb(x0, 0.1, seed=1)
        b = perturb(x0, 0.1, seed=2)
        self.assertFalse(np.array_equal(a, b))

    def test_clipped_to_unit_interval(self):
        x0 = np.array([0.0, 1.0] * 50)
        out = perturb(x0, 0.5, seed=0)
        self.assertTrue(np.all(out >= 0.0) and np.all(out <= 1.0))

    def test_uniform01_in_unit_interval_and_roughly_symmetric(self):
        draws = np.array([_uniform01(0, i) for i in range(5000)])
        self.assertTrue(np.all(draws >= 0.0) and np.all(draws < 1.0))
        self.assertAlmostEqual(draws.mean(), 0.5, delta=0.03)


class OptimizerWiringTest(unittest.TestCase):
    """H1: `perturb_amplitude`/`perturb_seed` must actually reach `Optimizer`'s x0."""

    def test_nonzero_amplitude_changes_x0(self):
        from yapnr.rf.driver import Optimizer
        from yapnr.rf.problem import Problem

        base = tiny_spec()
        perturbed = base.replace(
            optimizer=dataclasses.replace(base.optimizer, perturb_amplitude=0.2, perturb_seed=1)
        )
        x0_base = Optimizer(Problem(base)).state.x
        x0_perturbed = Optimizer(Problem(perturbed)).state.x
        self.assertFalse(np.array_equal(x0_base, x0_perturbed))


if __name__ == "__main__":
    unittest.main()
