"""Check published scientific data and strict numerical contracts, not raw fields."""

import copy
import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
from jointopt.frequencies import FREQUENCIES_HZ, LEGACY_POLICY_HZ, require_canonical
from validation import convergence_contract as convergence

ROOT = Path(__file__).resolve().parents[1]


class ArtifactTests(unittest.TestCase):
    def test_p3_complete_first_passing_pair(self):
        record = json.loads((ROOT / "results/p3-convergence.json").read_text())
        result = convergence.validate(record, record["request_binding"])
        self.assertTrue(result["converged"])
        self.assertEqual(record["steps"], 182334)
        self.assertFalse(record["gradient_qualified"])
        self.assertEqual(len(record["history"]), 128)
        self.assertTrue(all(value < 1e-10 for _, value in record["history"][-2:]))
        bad = copy.deepcopy(record)
        bad["history"][-1][1] = 1e-10
        with self.assertRaises(ValueError):
            convergence.validate(bad, bad["request_binding"])

    def test_exact_frequency_rejects_rounding(self):
        self.assertEqual(require_canonical(FREQUENCIES_HZ), FREQUENCIES_HZ)
        with self.assertRaises(ValueError):
            require_canonical(LEGACY_POLICY_HZ)

    def test_baseline_mask_identity_and_block_roundtrip(self):
        rho = np.load(ROOT / "model/inputs/rho.npz", allow_pickle=False)["rho"]
        audit = json.loads((ROOT / "model/inputs/audit.json").read_text())
        self.assertEqual(rho.shape, (368, 296))
        self.assertTrue(np.isin(rho, [0, 1]).all())
        self.assertEqual(
            hashlib.sha256(np.ascontiguousarray(rho).tobytes()).hexdigest(), audit["rho_sha256"]
        )
        np.testing.assert_array_equal(rho[::2, ::2].repeat(2, 0).repeat(2, 1), rho)

    def test_tiny_evidence_scope_and_both_directions(self):
        result = json.loads((ROOT / "results/tiny-native-allrows.json").read_text())
        self.assertEqual(result["status"], "PASS_REAL_NATIVE_COMPARISON")
        self.assertEqual({v["direction_id"] for v in result["comparisons"]}, {0, 1})
        for comparison in result["comparisons"]:
            self.assertTrue(comparison["passed"])
            self.assertEqual(len(comparison["row_passed"]), 5)
        self.assertFalse(result["actual_array_qualified"])

    def test_exact_recovered_source_spectrum(self):
        data = json.loads((ROOT / "contracts/source-normalization.json").read_text())
        spectrum = np.asarray(
            [complex(float.fromhex(a), float.fromhex(b)) for a, b in data["values_hex"]],
            dtype="<c16",
        )
        self.assertEqual(spectrum.nbytes, 96)
        self.assertEqual(hashlib.sha256(spectrum.tobytes()).hexdigest(), data["canonical_sha256"])

    def test_publication_manifest(self):
        path = ROOT / "publication-manifest.json"
        manifest = json.loads(path.read_text())
        for name, expected in manifest["files"].items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected, name)
