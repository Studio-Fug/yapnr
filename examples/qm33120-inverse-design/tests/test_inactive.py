"""Current inactive acceptance/persistence contract; analytic responses only."""

import unittest
from types import SimpleNamespace

import numpy as np
import torch
from jointopt.acceptance import ACCEPTANCE_REVISION
from jointopt.objective import JointObjective, Policy, Response
from jointopt.optimizer import UpdateLoop
from jointopt.trial_diagnostic import review_trial_for_diagnostics


class Tests(unittest.TestCase):
    def response(self):
        o = JointObjective(Policy())
        e = torch.ones((6, 5, len(o.sampling.n), 2, 4), dtype=torch.complex128)
        e[:, :, :, 1, :] = 0.3
        e[:, :, 0] *= 0.05
        response = Response(
            torch.zeros((6, 4, 4), dtype=torch.complex128),
            e,
            torch.ones((6, 4)) * 0.9,
            torch.ones((6, 4)) * 0.95,
        )
        return o, response

    def test_removed_old_acceptance_unavailable(self):
        import jointopt.observable_variation as v

        self.assertFalse(hasattr(v, "accept_local_candidate"))

    def test_optimizer_stays_disabled(self):
        o, response = self.response()
        ev = SimpleNamespace(objective=o)
        loop = UpdateLoop(ev, np.array([0.5]), evidence_persist=lambda *_: True)
        with self.assertRaisesRegex(RuntimeError, "activation blocked"):
            loop.step()
        np.testing.assert_array_equal(loop.x, [0.5])
        self.assertEqual(loop.history, [])

    def test_full_band_binding_and_inactive_decision(self):
        o, a = self.response()
        identity = dict(normalization_sha256="a" * 64, physical_context_sha256="b" * 64)
        events = []
        d = review_trial_for_diagnostics(
            o, a, a, identity, identity, persist=lambda e, p: events.append((e, p)) or True
        )
        self.assertEqual(d["candidate_rule_decision"]["acceptance_revision"], ACCEPTANCE_REVISION)
        self.assertFalse(d["state_update_allowed"])
        self.assertFalse(d["physical_acceptance"])
        self.assertFalse(d["execution_authorized"])
        evidence = events[0][1]["candidate_rule_evidence"]
        self.assertEqual(len(evidence["contexts"]), 2)
        self.assertEqual(evidence["reference_excitation_ids"], (1, 2, 3, 4))

    def test_invalid_identity_saved_and_rejected(self):
        o, a = self.response()
        events = []
        d = review_trial_for_diagnostics(
            o, a, a, {}, {}, persist=lambda e, p: events.append((e, p)) or True
        )
        self.assertIn("context_mismatch", d["candidate_rule_decision"]["blockers"])
        self.assertEqual(len(events), 3)

    def test_all_persistence_boundaries_fail_closed(self):
        o, a = self.response()
        identity = dict(normalization_sha256="a" * 64, physical_context_sha256="b" * 64)
        for stage in (
            "reviewed_rule_evaluated_before_gate",
            "reviewed_rule_decision",
            "inactive_trial_decision",
        ):
            with self.assertRaises(RuntimeError):
                review_trial_for_diagnostics(
                    o, a, a, identity, identity, persist=lambda e, p: e != stage
                )
