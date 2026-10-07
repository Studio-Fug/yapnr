"""Default-off engine wiring and fail-closed joint-refill acceptance contracts."""

import dataclasses
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pnr import energy_track, gloss


def electrical(*, skew=0.01, pairs=0, status="pass", resistance=5, rf=False):
    return dict(
        unqualified_pairs=pairs,
        skews={"pair": skew},
        energy=dict(
            immutable="same",
            project="same-project",
            rules="same-rules",
            ir={"VCC": dict(status=status, r_eff_mohm=resistance, budget_mohm=10, opens=[])},
            ir_contracts=["VCC"],
            rf_si_requires_external_verification=rf,
            plane_changes=[],
        ),
    )


class SettingsTests(unittest.TestCase):
    def test_default_off_settings_key_unchanged(self):
        conf = gloss.settings({})
        self.assertFalse(conf.energy)
        legacy = {
            f.name: getattr(conf, f.name) for f in dataclasses.fields(conf) if f.name != "energy"
        }
        expected = hashlib.sha256(
            json.dumps(legacy, sort_keys=True, default=list).encode()
        ).hexdigest()[:12]
        self.assertEqual(gloss.settings_key(conf), expected)
        enabled = gloss.settings({"PNR_GLOSS_ENERGY": "1"})
        self.assertTrue(enabled.energy)
        self.assertNotEqual(gloss.settings_key(enabled), expected)

    def test_new_subflag_does_not_enable_master(self):
        self.assertFalse(gloss.enabled({"PNR_GLOSS_ENERGY": "1"}))
        with self.assertRaises(ValueError):
            gloss.settings({"PNR_GLOSS_ENERGY": "maybe"})

    def test_importing_off_path_never_imports_shapely(self):
        script = (
            'import sys; import pnr.gloss; assert "shapely" not in sys.modules; '
            'assert "pnr.energy_track_geometry" not in sys.modules'
        )
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            timeout=30,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        )

    def test_enabled_worker_private_abi_path(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "python-version.txt").write_text("%d.%d" % sys.version_info[:2])
            before = list(sys.path)
            try:
                with patch.dict(os.environ, {"PNR_ENERGY_SITE_PACKAGES": root}):
                    energy_track.configure_worker_dependencies()
                # Temporary roots may be symlink aliases; only the canonical ABI path is added.
                self.assertEqual(sys.path, before + [str(directory.resolve())])
            finally:
                sys.path[:] = before

    def test_worker_wrong_abi_refuses_without_path_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "python-version.txt").write_text("2.7")
            before = list(sys.path)
            with patch.dict(os.environ, {"PNR_ENERGY_SITE_PACKAGES": root}):
                with self.assertRaises(RuntimeError):
                    energy_track.configure_worker_dependencies()
            self.assertEqual(sys.path, before)

    def test_code_key_closure_respects_both_flags(self):
        from pnr.feedback import signals

        names = set(signals.ENERGY_MODULES)
        legacy = signals.eval_code_modules(signals.PNR_ROOT, "plain", gloss=True, energy=False)
        enabled = signals.eval_code_modules(signals.PNR_ROOT, "plain", gloss=True, energy=True)
        disabled = signals.eval_code_modules(signals.PNR_ROOT, "plain", gloss=False, energy=True)
        self.assertFalse(names & set(legacy))
        self.assertLessEqual(names, set(enabled))
        self.assertFalse(names & set(disabled))

    def test_extra_facts_do_not_change_legacy_compare(self):
        # Integration is conditional on energy facts; legacy native paths have
        # no eager import/callback into the new electrical comparator.
        before = dict(
            bad_entries=0,
            entries={},
            partition=[],
            reference=[],
            forbidden=[],
            subwidth=0,
            unjustified=[],
            unqualified_pairs=0,
            skews={},
        )
        drc = dict(violations=[], unconnected_items=[])
        with patch("pnr.energy_track.compare", side_effect=AssertionError("off path")):
            ok, reasons, _ = gloss.compare(drc, before, drc, before)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])


class ElectricalGateTests(unittest.TestCase):
    def test_local_edit_ignores_unchanged_unrelated_unknown_pair(self):
        before = electrical(skew=None, pairs=1)
        self.assertEqual(energy_track.compare(before, before), [])
        self.assertTrue(energy_track.compare(before, before, ["VCC"]))

    def test_joint_plane_requires_measured_qualified_pairs(self):
        for values in (dict(skew=None), dict(skew=math.nan), dict(pairs=1)):
            before = electrical(**values)
            self.assertTrue(energy_track.compare(before, before, ["VCC"]))

    def test_joint_plane_allows_in_budget_ir_and_rejects_over_budget(self):
        before = electrical()
        after = electrical(resistance=5.1)
        self.assertEqual(energy_track.compare(before, after, ["VCC"]), [])
        after = electrical(resistance=10.1)
        self.assertIn("energy:ir_budget:VCC", energy_track.compare(before, after, ["VCC"]))

    def test_missing_plane_contract_is_hard_refusal(self):
        before = electrical()
        self.assertIn(
            "energy:missing_ir_contract:VDD", energy_track.compare(before, before, ["VDD"])
        )
        self.assertIn("energy:missing_facts", energy_track.compare({}, before))

    def test_failed_unrelated_rail_unchanged_is_not_a_new_failure(self):
        before = electrical()
        before["energy"]["ir"]["OTHER"] = dict(status="open", r_eff_mohm=None, opens=["sink"])
        after = json.loads(json.dumps(before))
        self.assertEqual(energy_track.compare(before, after, ["VCC"]), [])
        self.assertIn("energy:ir_not_pass:OTHER", energy_track.compare(before, after, ["OTHER"]))

    def test_rf_and_si_joint_refill_requires_external_verification(self):
        before = electrical(rf=True)
        self.assertEqual(energy_track.compare(before, before), [])
        self.assertIn(
            "energy:rf_si_verification_required", energy_track.compare(before, before, ["VCC"])
        )

    def test_immutable_and_unknown_ir_fail_closed(self):
        before = electrical()
        after = electrical()
        after["energy"]["immutable"] = "different"
        self.assertIn("energy:immutable", energy_track.compare(before, after))
        for resistance in (None, math.nan, math.inf):
            after = electrical(resistance=resistance)
            self.assertIn("energy:ir_unknown:VCC", energy_track.compare(before, after, ["VCC"]))

    def test_no_ir_configuration_is_ok_only_without_plane_change(self):
        before = electrical()
        before["energy"]["ir"] = {}
        before["skews"] = {}
        self.assertEqual(energy_track.compare(before, before), [])
        self.assertTrue(energy_track.compare(before, before, ["VCC"]))

    def test_unknown_or_tampered_dependency_ids_rejected(self):
        class Model:
            zones = []
            lid = {"F.Cu": 0}

        for ids in (["missing"], [{}], ["x", "x"]):
            self.assertIsNotNone(
                energy_track.dependencies(
                    Model(), {"step": "gloss", "layer": "F.Cu", "energy": {"dependent_zones": ids}}
                )
            )


if __name__ == "__main__":
    unittest.main()
