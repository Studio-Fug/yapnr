"""pnr.fab_profile with data profiles: the built-in profiles stay byte-identical (design §5.4).

``testdata/fab_profile_golden.json`` was recorded from pnr/fab_profile.py before data profiles
existed. Regenerate it only for an intended change of a built-in profile:

    PYTHONPATH=.:hardware/pnr python3 tests/unit/fab/test_engine_parity.py --regenerate \
        > tests/unit/fab/testdata/fab_profile_golden.json
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from yapnr.fab.profiles import fab_profile_module

fp = fab_profile_module()
GOLDEN = Path(__file__).resolve().parent / "testdata" / "fab_profile_golden.json"

RULES = {
    "fab": {
        "track_width_mm": 0.25,
        "clearance_mm": 0.2,
        "via_diameter_mm": 0.6,
        "via_drill_mm": 0.3,
        "hole_clearance_mm": 0.2,
        "edge_clearance_mm": 0.2,
        "min_through_drill_mm": 0.2,
        "via_annular_mm": 0.0,
    },
    "default_clearance_mm": 0.2,
    "net_classes": [{"name": "power", "width_mm": 0.5, "clearance_mm": 0.25, "nets": ["vcc"]}],
    "electrical_fab": {"outer_copper_um": 35.0, "max_temp_rise_c": 10.0},
    "plane_access_fab": {"min_via_plating_um": 20, "via_drill_mm": 0.3},
}
NETCLASSES = {"Default": 0.2, "power": 0.25, "fine": 0.1, "unset": None}


def _geometry(g):
    out = {f.name: getattr(g, f.name) for f in dataclasses.fields(g)}
    out["in_pad"] = None if g.in_pad is None else dataclasses.asdict(g.in_pad)
    for key in (
        "via_radius",
        "hole_gap",
        "same_net_via_pitch",
        "clearance_gap",
        "edge_gap",
        "max_via_hole_gap",
    ):
        out[key] = getattr(g, key)
    out["via_edge_margin"] = g.via_edge_margin()
    out["via_hole_gaps"] = [g.via_hole_gap(k) for k in (fp.VIA, fp.PTH, fp.NPTH, None)]
    out["pad_hole_clearances"] = [g.pad_hole_clearance(k) for k in (fp.PTH, fp.NPTH, None)]
    return out


def snapshot(names=(fp.LEGACY, "jlc-pofv")):
    """Every output of the given profiles (apply_rules, dru_text, board_constraints, geometry)."""
    out = {}
    for name in names:
        rules = fp.apply_rules(copy.deepcopy(RULES), name)
        out[name] = {
            "profile_fab": fp.profile_fab(name),
            "apply_fab_legacy": fp.apply_fab(fp.LEGACY_FAB, name),
            "apply_fab_model": fp.apply_fab_model({"outer_copper_um": 70.0, "x": 1}, name),
            "apply_rules": rules,
            "board_constraints": fp.board_constraints(fp.apply_fab(fp.LEGACY_FAB, name)),
            "board_constraints_rules": fp.board_constraints(rules["fab"]),
            "dru_text": fp.dru_text(None, name, NETCLASSES, 0.15),
            "dru_text_plain": fp.dru_text(None, name),
            "dru_text_rules": fp.dru_text(rules["fab"], name, {"Default": 0.2}, 0.05),
            "geometry_profile": _geometry(fp.geometry(name=name)),
            "geometry_rules": _geometry(fp.geometry(rules)),
        }
    return out


class BuiltinParityTest(unittest.TestCase):
    def test_builtin_profiles_are_byte_identical_to_the_golden(self):
        golden = json.loads(GOLDEN.read_text())
        now = json.loads(json.dumps(snapshot()))
        for name in golden:
            for key in golden[name]:
                self.assertEqual(now[name][key], golden[name][key], f"{name}: {key}")

    def test_the_default_is_still_jlc_pofv(self):
        env = {k: v for k, v in os.environ.items() if k != fp.ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(fp.active_name(), "jlc-pofv")


class DataProfileTest(unittest.TestCase):
    def test_data_profiles_resolve_by_name(self):
        for name in ("oshpark-2l", "oshpark-4l", "oshpark-6l", "jlc-4l", "pcbway-std"):
            self.assertIn(name, fp.PROFILES)
            self.assertEqual(fp.active_name({fp.ENV: name}), name)
            self.assertIsNotNone(fp.PROFILES.get(name))
        self.assertNotIn("jlc-typo", fp.PROFILES)
        self.assertIsNone(fp.PROFILES.get("jlc-typo"))
        with self.assertRaises(ValueError) as err:
            fp.active_name({fp.ENV: "jlc-typo"})
        self.assertIn("oshpark-4l", str(err.exception))
        self.assertIn("oshpark-4l", fp.PROFILES.names())

    def test_a_data_profile_drives_every_engine_function(self):
        snap = snapshot(["oshpark-4l"])["oshpark-4l"]
        self.assertEqual(snap["apply_rules"]["fab_profile"], "oshpark-4l")
        self.assertEqual(snap["apply_rules"]["fab"]["via_annular_mm"], 0.1016)
        self.assertEqual(snap["board_constraints"]["min_copper_edge_clearance"], 0.381)
        self.assertEqual(snap["board_constraints"]["min_via_diameter"], 0.4572)
        self.assertEqual(snap["geometry_profile"]["via_diameter"], 0.55)
        self.assertIsNone(snap["geometry_profile"]["in_pad"])
        self.assertIsNone(snap["geometry_profile"]["filled_via_hole_to_hole"])
        model = snap["apply_fab_model"]
        self.assertEqual((model["inner_copper_um"], model["min_via_plating_um"]), (17.3, 25.4))
        self.assertTrue(model["qualification"].startswith("OSH Park 4 layer"))

    def test_data_dru_text_cites_its_data_file_and_has_no_filled_via_rule(self):
        text = fp.dru_text(None, "oshpark-4l", NETCLASSES, 0.15)
        self.assertTrue(
            text.startswith("(version 1)\n# Generated by pnr.fab_profile (profile oshpark-4l)")
        )
        self.assertIn("yapnr/fab/data/profiles/oshpark-4l.json", text)
        self.assertNotIn("filled_via_to_pad_hole", text)
        self.assertNotIn("5A", text)
        self.assertIn("O-4l (10 mil drill to internal copper)", text)
        self.assertIn('(rule "oshpark-4l_hole_to_edge"', text)
        # The SMD pad rule must not weaken the wider netclasses (KiCad replaces their clearance).
        self.assertIn("A.NetClass != 'Default'", text)
        self.assertNotIn("A.NetClass != 'fine'", text)
        jlc4 = fp.dru_text(None, "jlc-4l")
        self.assertNotIn("filled_via_to_pad_hole", jlc4)
        self.assertIn("A.Type == 'Via' && B.Type == 'Pad'", jlc4)  # no in-pad exemption

    def test_without_yapnr_a_data_profile_is_unknown_and_builtins_still_work(self):
        missing = mock.patch.object(fp, "_data_profile", return_value=None)
        names = mock.patch.object(fp, "_data_profile_names", return_value=[])
        profiles = fp._Profiles({fp.LEGACY: None, "jlc-pofv": fp.PROFILES["jlc-pofv"]})
        with missing, names, mock.patch.object(fp, "PROFILES", profiles):
            self.assertNotIn("oshpark-4l", fp.PROFILES)
            self.assertEqual(fp.PROFILES.names(), ["jlc-pofv", "legacy"])
            self.assertIsNotNone(fp.dru_text(None, "jlc-pofv"))


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        os.environ.pop(fp.ENV, None)
        json.dump(snapshot(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        unittest.main()
