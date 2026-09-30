"""Fab capability profiles (pnr.fab_profile, PNR_FAB_PROFILE).

Pure python (no pcbnew). The KiCad enforcement of the generated custom rules is
exercised against kicad-cli in test_fab_profile_kicad.py.
"""

import contextlib
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pnr import fab_profile as fp

DEV = Path(__file__).resolve().parents[2] / "splanc_dev"
HAS_YAML = importlib.util.find_spec("yaml") is not None  # absent in KiCad's Python
HAS_NUMPY = importlib.util.find_spec("numpy") is not None
LEGACY_RULES = {
    "fab": dict(fp.LEGACY_FAB),
    "default_clearance_mm": 0.2,
    "net_classes": [
        {
            "name": "power",
            "width_mm": 1.5,
            "clearance_mm": 0.15,
            "nets": ["rail"],
            "plane_layer": None,
        },
        {
            "name": "gnd",
            "width_mm": None,
            "clearance_mm": None,
            "nets": ["lv"],
            "plane_layer": "In1.Cu",
        },
    ],
    "diff_pairs": [{"name": "usb", "p": "dp", "n": "dn", "width_mm": 0.2, "gap_mm": 0.15}],
}


def _rebind():
    """Re-resolve the process geometry (read once per process) for this env."""
    try:
        fp.reset_active()
    except ValueError:  # an unknown profile: the test asserts the failure itself
        pass


@contextlib.contextmanager
def profile(name):
    """Environment with PNR_FAB_PROFILE=name (None = unset, the default)."""
    env = {k: v for k, v in os.environ.items() if k != fp.ENV}
    if name is not None:
        env[fp.ENV] = name
    try:
        with patch.dict(os.environ, env, clear=True):
            _rebind()
            yield
    finally:
        _rebind()


def electrical_fab():
    return json.loads((DEV / "mini-routing-electrical-fab.json").read_text())


def plane_fab():
    return json.loads((DEV / "mini-plane-access-fab.json").read_text())


class SelectionTest(unittest.TestCase):
    def test_unset_selects_jlc_pofv(self):
        with profile(None):
            self.assertEqual(fp.active_name(), "jlc-pofv")
        with profile(""):
            self.assertEqual(fp.active_name(), "jlc-pofv")

    def test_unknown_profile_fails_closed(self):
        with profile("jlc-typo"):
            with self.assertRaises(ValueError):
                fp.active_name()


class LegacyTest(unittest.TestCase):
    """PNR_FAB_PROFILE=legacy reproduces the pre-profile rules exactly."""

    def test_rules_and_fab_models_untouched(self):
        rules = copy.deepcopy(LEGACY_RULES)
        rules["electrical_fab"] = electrical_fab()
        rules["plane_access_fab"] = plane_fab()
        before = json.dumps(rules, sort_keys=True)
        with profile("legacy"):
            self.assertIs(fp.apply_rules(rules), rules)
            self.assertEqual(fp.apply_fab_model(electrical_fab()), electrical_fab())
            self.assertEqual(fp.apply_fab_model(plane_fab()), plane_fab())
            self.assertEqual(fp.apply_fab(rules["fab"]), rules["fab"])
            self.assertIsNone(fp.dru_text())
        self.assertEqual(json.dumps(rules, sort_keys=True), before)

    def test_engine_constants_are_the_former_literals(self):
        with profile("legacy"):
            g = fp.active_geometry()
        # Former hardcoded values (keyhole_region, layered, joint, spatial_conflicts,
        # native_electrical): exact float equality, not approximate.
        self.assertEqual((g.via_diameter, g.via_drill, g.via_radius), (0.6, 0.3, 0.3))
        self.assertEqual(
            (g.hole_gap, g.same_net_via_pitch, g.clearance_gap, g.edge_gap),
            (0.201, 0.501, 0.151, 0.201),
        )
        self.assertEqual(g.via_edge_margin(), 0.501)
        self.assertEqual(g.max_via_hole_gap, 0.2)
        self.assertEqual([g.via_hole_gap(k) for k in (fp.VIA, fp.PTH, fp.NPTH, None)], [0.2] * 4)
        # No drill-size split: every plated pad is a PTH, and only NPTH drills
        # get the engine's hole keep-out (the former 0.201 incl. the 1 um margin).
        self.assertEqual(g.hole_kind(False, True, 0.2), fp.PTH)
        self.assertEqual(
            (g.pad_hole_clearance(fp.PTH), g.pad_hole_clearance(fp.NPTH) + 0.001), (None, 0.201)
        )
        self.assertIsNone(g.pth_hole_clearance)
        self.assertIsNone(g.hole_to_edge)

    def test_project_stamp_is_pre_profile_and_writes_no_dru(self):
        from pnr.writeback import patch_project_rules

        with tempfile.TemporaryDirectory() as d, profile("legacy"):
            pro = Path(d) / "b.kicad_pro"
            patch_project_rules(str(pro), copy.deepcopy(LEGACY_RULES))
            data = json.loads(pro.read_text())
            self.assertEqual(
                data["board"]["design_settings"]["rules"],
                {
                    "min_clearance": 0.15,
                    "min_track_width": 0.2,
                    "min_via_diameter": 0.6,
                    "min_via_annular_width": 0.0,
                    "min_through_hole_diameter": 0.2,
                    "min_hole_clearance": 0.2,
                    "min_hole_to_hole": 0.2,
                    "min_copper_edge_clearance": 0.2,
                },
            )
            self.assertNotIn("via_dimensions", data["board"]["design_settings"])
            default = next(c for c in data["net_settings"]["classes"] if c["name"] == "Default")
            self.assertEqual(
                (default["clearance"], default["via_diameter"], default["via_drill"]),
                (0.15, 0.6, 0.3),
            )
            self.assertFalse(pro.with_suffix(".kicad_dru").exists())

    def test_legacy_removes_only_generated_dru(self):
        with tempfile.TemporaryDirectory() as d:
            board = Path(d) / "b.kicad_pcb"
            with profile(None):
                self.assertTrue(fp.write_dru(board).exists())
            with profile("legacy"):
                self.assertIsNone(fp.write_dru(board))
            self.assertFalse(fp.dru_path(board).exists())
            fp.dru_path(board).write_text("(version 1)\n# hand written\n")
            for name in ("legacy", None):  # a hand-written rules file is never touched
                with profile(name):
                    self.assertIsNone(fp.write_dru(board))
                self.assertEqual(fp.dru_path(board).read_text(), "(version 1)\n# hand written\n")

    @unittest.skipUnless(HAS_YAML, "requires pyyaml (PnR runtime)")
    def test_compiled_rules_carry_no_new_keys(self):
        import yaml
        from pnr.constraints import compile_constraints, compile_routing_rules

        doc = yaml.safe_load((DEV / "mini-constraints.yaml").read_text())
        cc = compile_constraints({"board": doc["board"], "fab": doc["fab"]}, [])
        with profile(None):  # compilation is pure; profiles apply at boundaries
            fab = compile_routing_rules(cc, [])["fab"]
        self.assertEqual(fab, fp.LEGACY_FAB)


class JlcPofvTest(unittest.TestCase):
    """docs/fab-comparison.md 5B on the 5A base."""

    def test_fab_values(self):
        with profile(None):
            fab = fp.apply_fab(fp.LEGACY_FAB)
        expected = {
            "track_width_mm": 0.2,  # default signal track stays the design's
            "min_track_width_mm": 0.127,
            "clearance_mm": 0.127,
            "smd_pad_clearance_mm": 0.15,
            "edge_clearance_mm": 0.30,
            "hole_to_edge_mm": 0.50,
            "hole_clearance_mm": 0.20,
            "pth_hole_clearance_mm": 0.35,
            "npth_hole_clearance_mm": 0.20,
            "hole_to_hole_mm": 0.25,
            "pth_hole_to_hole_mm": 0.50,
            "filled_via_hole_to_hole_mm": 0.45,
            "via_diameter_mm": 0.45,
            "via_drill_mm": 0.30,
            "min_via_diameter_mm": 0.35,
            "min_through_drill_mm": 0.20,
            "via_annular_mm": 0.075,
            "min_npth_drill_mm": 0.50,
            "component_pth_min_drill_mm": 0.30,
        }
        self.assertEqual({k: fab[k] for k in expected}, expected)
        classes = {k: (v["diameter_mm"], v["drill_mm"]) for k, v in fab["via_classes"].items()}
        self.assertEqual(
            classes,
            {
                "default": (0.45, 0.30),
                "escape": (0.45, 0.20),
                "in_pad": (0.35, 0.20),
                "power": (0.60, 0.30),
            },
        )
        for d, h in classes.values():  # every via class meets drill and ring minimums
            self.assertGreaterEqual(h, fab["min_through_drill_mm"])
            self.assertGreaterEqual(round((d - h) / 2, 6), fab["via_annular_mm"])

    def test_apply_rules_is_idempotent_and_refuses_mixing(self):
        with profile(None):
            once = fp.apply_rules(copy.deepcopy(LEGACY_RULES))
            self.assertEqual(once[fp.MARKER], "jlc-pofv")
            self.assertIs(fp.apply_rules(once), once)
        with profile("legacy"), self.assertRaises(ValueError):
            fp.apply_rules(once)

    def test_copper_model_only_gets_stricter(self):
        from pnr.electrical import compile_policy

        currents = [
            dict(
                net="rail",
                rms_current_a=5,
                peak_current_a=5,
                scope="net",
                source=dict(path="x", line=1, sha256="0"),
            )
        ]
        legacy_fab = electrical_fab()
        with profile(None):
            jlc_fab = fp.apply_fab_model(legacy_fab)
        for key in (
            "delta_t_c",
            "plane_access_delta_t_c",
            "via_barrel_loss_budget_w",
            "via_array_peak_drop_v",
            "neck_loss_budget_w",
            "neck_peak_drop_v",
            "copper_resistivity_ohm_mm",
            "power_bus_min_width_mm",
        ):
            self.assertEqual(jlc_fab[key], legacy_fab[key], key)  # current contracts untouched
        self.assertEqual((jlc_fab["outer_copper_oz"], jlc_fab["min_via_plating_um"]), (1.0, 18))
        self.assertAlmostEqual(jlc_fab["inner_copper_oz"] * fp.OZ_UM, 15.2)
        old = compile_policy(LEGACY_RULES, currents, legacy_fab)["electrical_nets"]["rail"]
        new = compile_policy(LEGACY_RULES, currents, jlc_fab)["electrical_nets"]["rail"]
        self.assertEqual(new["outer_width_mm"], old["outer_width_mm"])
        self.assertAlmostEqual(new["inner_width_mm"] / old["inner_width_mm"], 35 / 15.2, places=6)
        self.assertGreater(
            new["via_array"]["barrel_resistance_ohm"], old["via_array"]["barrel_resistance_ohm"]
        )
        self.assertGreaterEqual(new["via_array"]["count"], old["via_array"]["count"])
        self.assertEqual(
            (new["via_array"]["diameter_mm"], new["via_array"]["drill_mm"]), (0.45, 0.30)
        )

    def test_apply_rules_resizes_existing_envelopes(self):
        from pnr.electrical import compile_policy

        currents = [
            dict(
                net="rail",
                rms_current_a=5,
                peak_current_a=5,
                scope="net",
                source=dict(path="x", line=1, sha256="0"),
            )
        ]
        with profile("legacy"):
            legacy = compile_policy(LEGACY_RULES, currents, electrical_fab())
        with profile(None):
            applied = fp.apply_rules(legacy)
            direct = compile_policy(LEGACY_RULES, currents, fp.apply_fab_model(electrical_fab()))
        self.assertEqual(applied["electrical_nets"], direct["electrical_nets"])
        self.assertEqual(applied["electrical_fab"], direct["electrical_fab"])

    def test_project_stamp(self):
        from pnr.writeback import patch_project_rules

        with tempfile.TemporaryDirectory() as d, profile(None):
            pro = Path(d) / "b.kicad_pro"
            patch_project_rules(str(pro), copy.deepcopy(LEGACY_RULES))
            data = json.loads(pro.read_text())
            self.assertEqual(
                data["board"]["design_settings"]["rules"],
                {
                    "min_clearance": 0.127,
                    "min_track_width": 0.127,
                    "min_via_diameter": 0.35,
                    "min_via_annular_width": 0.075,
                    "min_through_hole_diameter": 0.2,
                    "min_hole_clearance": 0.2,
                    "min_hole_to_hole": 0.25,
                    "min_copper_edge_clearance": 0.3,
                },
            )
            classes = {c["name"]: c for c in data["net_settings"]["classes"]}
            self.assertEqual(
                (
                    classes["Default"]["clearance"],
                    classes["Default"]["track_width"],
                    classes["Default"]["via_diameter"],
                    classes["Default"]["via_drill"],
                ),
                (0.127, 0.2, 0.45, 0.3),
            )
            self.assertEqual(classes["power"]["clearance"], 0.15)  # explicit class values kept
            self.assertIn(
                {"diameter": 0.35, "drill": 0.2}, data["board"]["design_settings"]["via_dimensions"]
            )
            dru = pro.with_suffix(".kicad_dru").read_text()
            for rule in (
                "smd_pad_to_pad",
                "via_hole_clearance",
                "npth_hole_clearance",
                "pth_hole_clearance",
                "via_hole_to_hole",
                "filled_via_to_pad_hole",
                "component_pth_hole_to_hole",
                "npth_min_drill",
                "hole_to_edge",
            ):
                self.assertIn(f'"jlc-pofv_{rule}"', dru)
            self.assertTrue(dru.startswith("(version 1)\n" + fp.DRU_HEADER))

    def test_dru_rule_values_and_order(self):
        with profile(None):
            text = fp.dru_text(
                netclass_clearances={"Default": 0.127, "power": 0.15, "wide": 0.3},
                edge_stroke_mm=0.15,
            )
        self.assertIn("A.NetClass != 'wide' && B.NetClass != 'wide'", text)
        self.assertNotIn("NetClass != 'power'", text)  # equal clearance cannot be weakened
        self.assertIn("(constraint hole_clearance (min 0.35mm))", text)
        self.assertIn("(constraint hole_to_hole (min 0.25mm))", text)
        self.assertIn("(constraint hole_to_hole (min 0.45mm))", text)
        self.assertIn("(constraint hole_size (min 0.5mm))", text)
        # 0.50 to the outline centreline = 0.425 to the edge of a 0.15 mm stroke.
        self.assertIn("(constraint physical_hole_clearance (min 0.425mm))", text)
        # A plated pad drill under 0.30 is a via-class hole; 0.30 or more a component PTH.
        rule = lambda name: text[text.index(f'"jlc-pofv_{name}"') :].split("(rule", 1)[0]
        via_class = (
            "(A.Type == 'Via' || (A.Type == 'Pad' && A.Pad_Type == 'Through-hole'"
            " && A.Hole_Size_X < 0.3mm && A.Hole_Size_Y < 0.3mm))"
        )
        self.assertIn(via_class, rule("via_hole_clearance"))
        self.assertIn(via_class, rule("via_hole_to_hole"))
        self.assertIn(
            "(A.Hole_Size_X >= 0.3mm || A.Hole_Size_Y >= 0.3mm)", rule("component_pth_hole_to_hole")
        )
        self.assertIn("!(B.Type", rule("pth_hole_clearance"))
        # Later rules win in KiCad: the component-PTH hole-to-hole rule follows the
        # filled-via rule it overlaps (via to PTH must resolve to 0.50, not 0.45).
        self.assertLess(
            text.index("filled_via_to_pad_hole"), text.index("component_pth_hole_to_hole")
        )

    def test_edge_stroke_is_read_from_board(self):
        with tempfile.TemporaryDirectory() as d:
            board = Path(d) / "b.kicad_pcb"
            board.write_text(
                "(kicad_pcb (gr_line (start 0 0) (end 1 0)\n (stroke (width 0.15) (type solid))"
                ' (layer "Edge.Cuts")) (gr_line (start 0 0) (end 1 0) (stroke (width 0.3)'
                ' (type solid)) (layer "F.SilkS")))'
            )
            self.assertEqual(fp.edge_stroke_width(board), 0.15)
            with profile(None):
                self.assertIn("(min 0.425mm)", fp.write_dru(board).read_text())

    def test_rules_holders_ignore_the_environment(self):
        with profile(None):
            self.assertEqual(fp.geometry({"fab": dict(fp.LEGACY_FAB)}).via_diameter, 0.6)
            self.assertEqual(fp.geometry({}).hole_to_hole, 0.2)
            self.assertEqual(fp.geometry().hole_to_hole, 0.25)
        with profile("legacy"):
            jlc = fp.apply_rules(copy.deepcopy(LEGACY_RULES), "jlc-pofv")
            g = fp.geometry(jlc)
        self.assertEqual((g.via_diameter, g.hole_to_hole, g.pth_hole_to_hole), (0.45, 0.25, 0.5))
        self.assertEqual(g.same_net_via_pitch, 0.551)
        self.assertEqual(g.via_edge_margin(), 0.651)  # hole-to-edge dominates copper-to-edge
        self.assertEqual(
            [g.via_hole_gap(k) for k in (fp.VIA, fp.PTH, fp.NPTH, None)], [0.25, 0.5, 0.45, 0.5]
        )

    def test_footprint_via_class_holes_get_via_rules(self):
        """5A Component PTH: hole 0.30 or more. A plated 0.20/0.45 footprint hole
        (U5.24, U18.21 thermal vias) is a via: 0.20 to copper, 0.25 to a via."""
        g = fp.geometry(fp.apply_rules(copy.deepcopy(LEGACY_RULES), "jlc-pofv"))
        self.assertEqual(g.hole_kind(True), fp.VIA)
        self.assertEqual(g.hole_kind(False, True, 0.2), fp.VIA)
        self.assertEqual(g.hole_kind(False, True, 0.3), fp.PTH)  # inclusive
        self.assertEqual(g.hole_kind(False, True, 1.0), fp.PTH)
        self.assertEqual(g.hole_kind(False, False, 0.2), fp.NPTH)
        self.assertIsNone(g.hole_kind(False, None, 0.2))
        self.assertEqual(g.via_hole_gap(g.hole_kind(False, True, 0.2)), 0.25)
        self.assertEqual(
            [g.pad_hole_clearance(k) for k in (fp.VIA, fp.PTH, fp.NPTH, None)],
            [0.2, 0.35, 0.2, 0.35],
        )

    @unittest.skipUnless(HAS_YAML, "requires pyyaml (PnR runtime)")
    def test_constraints_accept_explicit_hole_rules(self):
        from pnr.constraints import compile_constraints, compile_routing_rules

        cc = compile_constraints(
            {"fab": {"pth_hole_clearance_mm": 0.35, "hole_to_edge_mm": 0.5}}, []
        )
        fab = compile_routing_rules(cc, [])["fab"]
        self.assertEqual((fab["pth_hole_clearance_mm"], fab["hole_to_edge_mm"]), (0.35, 0.5))
        self.assertNotIn("pth_hole_to_hole_mm", fab)


class ActiveGeometryTest(unittest.TestCase):
    """The process geometry is resolved once, not per call (hot paths)."""

    def test_environment_is_read_once_per_process(self):
        with profile("legacy"):
            g = fp.active_geometry()
            with patch.object(fp, "active_name", side_effect=AssertionError("env read per call")):
                self.assertIs(fp.active_geometry(), g)
            with patch.dict(os.environ, {fp.ENV: "jlc-pofv"}):
                self.assertIs(fp.active_geometry(), g)  # fixed until reset
                self.assertEqual(fp.reset_active().via_diameter, 0.45)

    def test_bound_module_globals_follow_a_reset(self):
        from pnr.route.detail import joint, layered, spatial_conflicts

        with profile("legacy"):
            self.assertEqual((joint._VIA_DIAMETER, joint._VIA_PITCH), (0.6, 0.501))
            self.assertEqual((layered._VIA_DRILL, layered._HOLE_GAP), (0.3, 0.201))
            self.assertEqual(spatial_conflicts._VIA_RADIUS, 0.3)
        with profile(None):
            self.assertEqual((joint._VIA_DIAMETER, joint._VIA_PITCH), (0.45, 0.551))
            self.assertEqual((layered._VIA_DRILL, layered._HOLE_GAP), (0.3, 0.251))
            self.assertEqual(spatial_conflicts._VIA_RADIUS, 0.225)

    def test_bad_reset_keeps_the_previous_binding(self):
        from pnr.route.detail import joint

        with profile("legacy"):
            with patch.dict(os.environ, {fp.ENV: "jlc-typo"}), self.assertRaises(ValueError):
                fp.reset_active()
            self.assertEqual(joint._VIA_DIAMETER, 0.6)


class RulesFileTest(unittest.TestCase):
    """The .kicad_dru is present before any pcbnew load and travels with projects."""

    def test_load_board_writes_the_rules_before_loading(self):
        seen = []
        fake = SimpleNamespace(
            LoadBoard=lambda path: seen.append(fp.dru_path(path).exists()) or "board"
        )
        with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, {"pcbnew": fake}):
            board = Path(d) / "b.kicad_pcb"
            board.write_text("(kicad_pcb)")
            with profile(None):
                self.assertEqual(fp.load_board(board), "board")
            with profile("legacy"):
                fp.load_board(board)  # removes only the stale generated file
            self.assertEqual(seen, [True, False])
            with profile(None):
                fp.load_board(Path(d) / "missing.kicad_pcb")  # nothing written for a missing board
            self.assertFalse((Path(d) / "missing.kicad_dru").exists())

    def test_copy_dru(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "a.kicad_pcb", Path(d) / "sub" / "b.kicad_pcb"
            dst.parent.mkdir()
            self.assertIsNone(fp.copy_dru(src, dst))
            fp.dru_path(src).write_text("(version 1)\n# hand written\n")
            self.assertEqual(fp.copy_dru(src, dst), fp.dru_path(dst))
            self.assertEqual(fp.dru_path(dst).read_text(), "(version 1)\n# hand written\n")
            self.assertEqual(fp.copy_dru(src, src), fp.dru_path(src))  # same file: no-op


class RouterModelTest(unittest.TestCase):
    """Regional solvers and the grid router follow the profile's via model."""

    def test_joint_via_conflict_uses_profile_via(self):
        from pnr.route.detail.joint import conflict

        a = SimpleNamespace(net="a", width=0.2, clearance=0.15)
        b = SimpleNamespace(net="b", width=0.2, clearance=0.15)
        va, vb = ("via", None, (0, 0), (0, 0)), ("via", None, (0.7, 0), (0.7, 0))
        with profile("legacy"):
            self.assertTrue(conflict(a, va, b, vb))  # 0.7 < 0.6 + 0.15
        with profile(None):
            self.assertFalse(conflict(a, va, b, vb))  # 0.7 >= 0.45 + 0.15: smaller vias pack
        same = SimpleNamespace(net="a", width=0.2, clearance=0.15)
        near = ("via", None, (0.52, 0), (0.52, 0))
        with profile("legacy"):
            self.assertFalse(conflict(a, va, same, near))  # 0.52 >= 0.501
        with profile(None):
            self.assertTrue(conflict(a, va, same, near))  # distinct drills need 0.30 + 0.25

    def test_layered_same_net_drill_gap(self):
        from pnr.route.detail.layered import via_conflict

        path = [(0, 0, 0), (0, 0, 1)]
        with profile("legacy"):
            self.assertFalse(via_conflict((0.52, 0), path, 0.2, 0.15, same_net=True))
        with profile(None):
            self.assertTrue(via_conflict((0.52, 0), path, 0.2, 0.15, same_net=True))

    @unittest.skipUnless(HAS_NUMPY, "requires numpy (PnR runtime)")
    def test_grid_hole_kinds_and_edge_inset(self):
        from pnr.route.detail.grid import RouteGrid

        g = RouteGrid(4, 4, 0.1, clearance=0.127, track_width=0.2, via_radius=0.225)
        g.via_drill_radius = 0.15
        g.source_drills = [((2.0, 2.0), 0.4)]
        g.source_drill_plated = [True]
        point = (2.0 + 0.4 + 0.15 + 0.3, 2.0)  # 0.30 drill gap to a component PTH
        self.assertTrue(g.hole_site_clear(point))  # pre-profile single 0.20 hole clearance
        g.pth_hole_gap, g.npth_hole_gap = 0.5, 0.45
        self.assertFalse(g.hole_site_clear(point))
        g.source_drill_plated = [False]
        self.assertFalse(g.hole_site_clear(point))
        self.assertTrue(g.hole_site_clear((2.0 + 0.4 + 0.15 + 0.46, 2.0)))
        # A footprint's plated 0.20 drill (via class) keeps the via-to-via 0.25.
        g.source_drills, g.source_drill_plated = [((2.0, 2.0), 0.1)], [True]
        near = (2.0 + 0.1 + 0.15 + 0.3, 2.0)  # 0.30 drill gap
        self.assertFalse(g.hole_site_clear(near))  # without the split: a component PTH (0.50)
        g.component_pth_min_drill, g.via_hole_gap = 0.3, 0.25
        self.assertTrue(g.hole_site_clear(near))
        self.assertFalse(g.hole_site_clear((2.0 + 0.1 + 0.15 + 0.24, 2.0)))
        g.block_edge_inset_split(0.4, 0.65)
        self.assertTrue(g.blocked[0, 20, 3] and g.via_blocked[0, 20, 3])  # centre 0.35 from edge
        self.assertFalse(g.blocked[0, 20, 5])  # 0.55: tracks allowed ...
        self.assertTrue(g.via_blocked[0, 20, 5])  # ... vias are not (hole to edge)
        self.assertFalse(g.via_blocked[0, 20, 7])


class NativeDrcTest(unittest.TestCase):
    def test_run_drc_writes_the_profile_rules_next_to_the_board(self):
        from pnr.native_drc import run_drc

        with tempfile.TemporaryDirectory() as d:
            board = Path(d) / "b.kicad_pcb"
            board.write_text("(kicad_pcb)")
            report = Path(d) / "b.drc.json"
            seen = []

            def cold(cli, board_path, report_path, **kwargs):
                seen.append(fp.dru_path(board_path).exists())
                return dict(violations=[], unconnected_items=[])

            with patch("pnr.native_drc._run_cold", side_effect=cold):
                with profile(None):
                    run_drc("cli", board, report)
                with profile("legacy"):
                    run_drc("cli", board, report)
            self.assertEqual(seen, [True, False])


if __name__ == "__main__":
    unittest.main()
