"""The fab checks (design §7.2): every FAB-* code from synthetic facts, and nothing else."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.fab import board, capability, check, profiles, stackups, testing

TODAY = "2026-10-02"


def stats(**over):
    doc = {
        "board": {"has_outline": True},
        "pads": {"castellated": 0},
        "vias": {"through": 1, "blind": 0, "buried": 0, "micro": 0},
        "drill_holes": [
            {"count": 1, "shape": "Round", "x_size": "0.3000 mm", "y_size": "0.3000 mm"}
        ],
    }
    doc.update(over)
    return doc


CLEAN_DRC = {"violations": [], "unconnected_items": [], "ignored_checks": []}


class EvaluateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def facts(self, name="b", **kwargs):
        kwargs.setdefault("slot", False)
        return board.read(testing.write_board(self.dir / name, name, **kwargs))

    def run_check(
        self,
        facts,
        profile="oshpark-4l",
        stackup=None,
        st=None,
        drc=None,
        options=None,
        **kw,
    ):
        p = profiles.load(profile)
        s = stackups.load(stackup or p.default_stackup)
        v = capability.vendor(p.vendor)
        opts = options or check.Options(today=TODAY)
        return check.evaluate(facts, st or stats(), drc or CLEAN_DRC, p, s, v, opts, **kw)

    def codes(self, findings, severity=None):
        return sorted({f.code for f in findings if severity is None or f.severity == severity})

    def test_a_clean_osh_park_board_has_no_findings(self):
        findings = self.run_check(self.facts(layers=4, npth=False))
        self.assertEqual([f.to_dict() for f in findings], [])

    def test_fab_drc_errors_warnings_exclusions_and_unconnected(self):
        drc = {
            "violations": [
                {"type": "clearance", "severity": "error", "description": "Clearance", "items": []},
                {"type": "clearance", "severity": "error", "description": "Clearance", "items": []},
                {"type": "silk", "severity": "warning", "description": "Silk", "items": []},
                {
                    "type": "x",
                    "severity": "error",
                    "excluded": True,
                    "description": "X",
                    "items": [],
                },
            ],
            "unconnected_items": [{}],
            "ignored_checks": [{"key": "missing_courtyard"}],
        }
        findings = self.run_check(self.facts(layers=4), drc=drc)
        drc_findings = [f for f in findings if f.code == "FAB-DRC"]
        self.assertEqual(
            sorted(
                (f.severity, str(f.data.get("type")), f.data.get("count", 0)) for f in drc_findings
            ),
            [
                ("error", "clearance", 2),
                ("error", "unconnected_items", 1),
                ("info", "None", 0),
                ("info", "x", 1),
                ("warning", "silk", 1),
            ],
        )
        self.assertEqual(self.codes(findings), ["FAB-DRC"])

    def test_fab_layers(self):
        findings = self.run_check(self.facts(layers=2), profile="oshpark-4l")
        self.assertEqual(self.codes(findings, "error"), ["FAB-LAYERS"])

    def test_fab_outline_panel_and_missing(self):
        findings = self.run_check(self.facts(layers=4, second_outline=True))
        self.assertEqual(self.codes(findings), ["FAB-OUTLINE"])
        self.assertIn("2.54 mm between outlines", findings[0].message)
        facts = self.facts(layers=4)
        facts.outline = board.Outline([], 1, 0.1)
        findings = self.run_check(facts, st=stats(board={"has_outline": False}))
        self.assertEqual([f.severity for f in findings], ["error", "error"])
        self.assertEqual(self.codes(findings), ["FAB-OUTLINE"])

    def test_fab_size(self):
        findings = self.run_check(self.facts(layers=4, size=(5.0, 20.0)))
        self.assertEqual(self.codes(findings), ["FAB-SIZE"])
        self.assertIn("under the minimum", findings[0].message)

    def test_fab_via_type(self):
        findings = self.run_check(
            self.facts(layers=4), st=stats(vias={"through": 1, "blind": 2, "buried": 0, "micro": 0})
        )
        self.assertEqual(self.codes(findings, "error"), ["FAB-VIA-TYPE"])

    def test_fab_drill_large_holes_and_slots(self):
        holes = [
            {"count": 2, "shape": "Round", "x_size": "7.0000 mm", "y_size": "7.0000 mm"},
            {"count": 1, "shape": "Oval", "x_size": "1.0000 mm", "y_size": "2.0000 mm"},
        ]
        findings = self.run_check(self.facts(layers=4), st=stats(drill_holes=holes))
        self.assertEqual([(f.code, f.severity) for f in findings], [("FAB-DRILL", "warning")] * 2)
        narrow = [{"count": 1, "shape": "Oval", "x_size": "0.4000 mm", "y_size": "2.0000 mm"}]
        findings = self.run_check(self.facts(layers=4), st=stats(drill_holes=narrow))
        self.assertEqual([(f.code, f.severity) for f in findings], [("FAB-DRILL", "error")])
        # JLC's slot minimum is not transcribed (derived): a warning only.
        findings = self.run_check(self.facts(layers=4), "jlc-4l", st=stats(drill_holes=narrow))
        self.assertIn(("FAB-DRILL", "warning"), [(f.code, f.severity) for f in findings])

    def test_fab_castellated(self):
        facts = self.facts(layers=4, castellated=True)
        osh = self.run_check(facts)
        self.assertEqual([(f.code, f.severity) for f in osh], [("FAB-CASTELLATED", "warning")])
        self.assertIn("not guaranteed", osh[0].message)
        jlc = self.run_check(facts, "jlc-4l")  # 0.6 mm holes: JLC's option, at least 0.5 mm
        self.assertIn(("FAB-CASTELLATED", "warning"), [(f.code, f.severity) for f in jlc])
        for fp in facts.footprints:
            for pad in fp.pads:
                if pad.castellated:
                    pad.drill = (0.4, 0.4)
        jlc = self.run_check(facts, "jlc-4l")
        self.assertIn(("FAB-CASTELLATED", "error"), [(f.code, f.severity) for f in jlc])

    def test_fab_stackup_thickness_and_dielectrics(self):
        facts = self.facts(layers=4, thickness=0.8)
        self.assertEqual(self.codes(self.run_check(facts)), ["FAB-STACKUP"])
        facts = self.facts(layers=4)
        facts.stackup = [
            {"name": "d1", "type": "prepreg", "thickness": 0.2, "epsilon_r": 4.5},
            {"name": "d2", "type": "core", "thickness": 0.99, "epsilon_r": 3.61},
            {"name": "d3", "type": "prepreg", "thickness": 0.2, "epsilon_r": 3.61},
        ]
        findings = self.run_check(facts)
        self.assertEqual([(f.code, f.severity) for f in findings], [("FAB-STACKUP", "warning")])
        self.assertIn("Dk 4.5", findings[0].message)

    def test_fab_rf_mismatch_needs_an_explicit_matching_stackup(self):
        facts = self.facts(layers=4, rf={"er": 3.55, "h_mm": 0.813, "tan_delta": 0.0027})
        findings = self.run_check(facts)
        rf = [f for f in findings if f.code == "FAB-RF"]
        self.assertEqual(len(rf), 2)  # no explicit stackup, and the S1 substrate mismatch
        self.assertTrue(all(f.severity == "error" for f in rf))
        self.assertIn("er 3.55 vs 3.61", rf[1].message)
        self.assertEqual(self.codes(findings), ["FAB-ALTERNATE", "FAB-IMPEDANCE", "FAB-RF"])

    def test_fab_rf_matching_board_passes_with_impedance_and_alternate_notes(self):
        facts = self.facts(layers=4, rf={"er": 3.61, "h_mm": 0.1999, "tan_delta": 0.009})
        opts = check.Options(stackup_explicit=True, today=TODAY)
        findings = self.run_check(facts, options=opts)
        self.assertEqual(
            [(f.code, f.severity) for f in findings],
            [("FAB-IMPEDANCE", "warning"), ("FAB-ALTERNATE", "info")],
        )
        self.assertIn("Keep the standard FR408-HR", findings[1].message)
        em = self.run_check(facts, stackup="oshpark-4l-em528", options=opts)
        self.assertIn("FAB-RF", self.codes(em, "error"))

    def test_fab_impedance_on_a_controlled_service_is_a_note(self):
        facts = self.facts(layers=4, rf={"er": 4.4, "h_mm": 0.2104, "tan_delta": 0.017})
        opts = check.Options(stackup_explicit=True, today=TODAY)
        findings = self.run_check(facts, "jlc-4l", options=opts)
        self.assertEqual(
            [(f.code, f.severity) for f in findings],
            [("FAB-IMPEDANCE", "info"), ("FAB-MARKING", "info")],
        )

    def test_fab_qty(self):
        for profile, qty, bad in (
            ("oshpark-4l", 4, True),
            ("oshpark-4l", 6, False),
            ("jlc-4l", 3, True),
        ):
            findings = self.run_check(
                self.facts(layers=4), profile, options=check.Options(qty=qty, today=TODAY)
            )
            self.assertEqual("FAB-QTY" in self.codes(findings, "error"), bad, (profile, qty))

    def test_fab_marking_only_at_assembly_vendors(self):
        (jlc,) = self.run_check(self.facts(layers=4), "jlc-4l")
        self.assertEqual(jlc.code, "FAB-MARKING")
        self.assertIn("'Mark on PCB' (Remove Mark, Order Number, 2D barcode)", jlc.message)
        self.assertEqual(self.codes(self.run_check(self.facts(layers=4))), [])

    def test_fab_profile_draft(self):
        facts = self.facts(layers=4)
        findings = self.run_check(facts, "pcbway-std")
        self.assertIn("FAB-PROFILE-DRAFT", self.codes(findings, "error"))
        allowed = self.run_check(
            facts, "pcbway-std", options=check.Options(allow_draft=True, today=TODAY)
        )
        self.assertIn("FAB-PROFILE-DRAFT", self.codes(allowed, "warning"))
        self.assertEqual(check.errors(allowed), [])

    def test_fab_sources_age(self):
        findings = self.run_check(self.facts(layers=4), options=check.Options(today="2027-09-01"))
        self.assertEqual(self.codes(findings), ["FAB-SOURCES"])

    def test_routed_under_another_profile_is_noted(self):
        findings = self.run_check(self.facts(layers=4), routed_under="jlc-4l")
        self.assertEqual([(f.code, f.severity) for f in findings], [("FAB-DRC", "info")])


class ScratchCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_stricter_rules(self):
        merged = check.stricter_rules(
            {"min_clearance": 0.2, "min_track_width": 0.1, "other": 1},
            {"min_clearance": 0.127, "min_track_width": 0.127, "min_hole_clearance": 0.254},
        )
        self.assertEqual(
            merged,
            {
                "min_clearance": 0.2,
                "min_track_width": 0.127,
                "other": 1,
                "min_hole_clearance": 0.254,
            },
        )

    def test_prepare_writes_the_profile_rules_into_a_copy_only(self):
        src = testing.write_board(self.dir / "src", "b", layers=4)
        before = src.read_bytes()
        (src.with_suffix(".kicad_dru")).write_text(
            '(version 1)\n(rule "mine"\n  (constraint clearance (min 1mm))\n  (condition "A.NetClass == \'x\'"))\n'
        )
        prep = check.prepare(src, profiles.load("oshpark-4l"), self.dir / "work", "renamed")
        scratch = prep["board"]
        self.assertEqual(scratch.name, "renamed.kicad_pcb")
        self.assertEqual(src.read_bytes(), before)
        rules = json.loads(scratch.with_suffix(".kicad_pro").read_text())["board"][
            "design_settings"
        ]["rules"]
        self.assertEqual(rules["min_via_annular_width"], 0.1016)
        self.assertEqual(rules["min_copper_edge_clearance"], 0.381)
        dru = scratch.with_suffix(".kicad_dru").read_text()
        self.assertTrue(
            dru.startswith("(version 1)\n# Generated by pnr.fab_profile (profile oshpark-4l)")
        )
        self.assertIn('(rule "mine"', dru)
        self.assertEqual(dru.count("(version 1)"), 1)
        self.assertEqual(prep["routed_under"], "hand-written rules")

    def test_a_generated_rules_file_is_replaced(self):
        from yapnr.fab.profiles import fab_profile_module

        fp = fab_profile_module()
        src = testing.write_board(self.dir / "src", "b", layers=4)
        src.with_suffix(".kicad_dru").write_text(fp.dru_text(None, "jlc-4l"))
        prep = check.prepare(src, profiles.load("oshpark-4l"), self.dir / "work", "b")
        dru = prep["board"].with_suffix(".kicad_dru").read_text()
        self.assertNotIn("jlc-4l", dru)
        self.assertEqual(prep["routed_under"], "jlc-4l")

    def test_a_board_without_project_needs_the_flag(self):
        src = testing.write_board(self.dir / "src", "b", layers=4, project=False)
        with self.assertRaises(FileNotFoundError):
            check.prepare(src, profiles.load("oshpark-4l"), self.dir / "w", "b")
        prep = check.prepare(src, profiles.load("oshpark-4l"), self.dir / "w", "b", no_project=True)
        self.assertTrue(prep["notes"])

    def test_merged_dru_without_profile_rules_keeps_the_hand_written_file(self):
        self.assertEqual(
            check.merged_dru(None, "(version 1)\n(rule x)\n"), "(version 1)\n(rule x)\n"
        )
        self.assertEqual(check.merged_dru("(version 1)\n", None), "(version 1)\n")


if __name__ == "__main__":
    unittest.main()
