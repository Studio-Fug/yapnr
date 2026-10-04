"""The radar60 board integration's text and geometry steps (examples/radar60/board): the RF
macro merge with its dummy columns, loads and mask islands (kicad_ops.merge_macro), the macro
digest R1 v2, the fanout exit bands and bottom-site edits (bands.py) and the constraints the
floorplan generates for them (gen_board.py).

Hermetic: the committed rfm1-n macro board and record, no KiCad (pcbnew is imported only by the
KiCad-side steps) and no engine.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).parents[3] / "examples/radar60"
sys.path.insert(0, str(EXAMPLE / "board"))

import bands  # noqa: E402
import kicad_ops  # noqa: E402

MACRO = EXAMPLE / "rf/generated/rfm1-n/rfm1-n.kicad_pcb"
EMPTY_BOARD = '(kicad_pcb\n\t(version 20241229)\n\t(net 0 "")\n)\n'
U1 = (56.0, 49.35)  # U1's centre on the 60 x 47.35 mm board (board (26, 28))


def record():
    return json.loads(MACRO.with_suffix(".json").read_text())


def macro_text():
    return MACRO.read_text()


def footprint(text, ref):
    return next(f for f in kicad_ops._footprints(text) if kicad_ops._reference(f) == ref)


class MergeMacroTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = record()
        cls.text, cls.counts = kicad_ops.merge_macro(EMPTY_BOARD, macro_text(), cls.rec, U1)

    def test_counts_come_from_the_record(self):
        self.assertEqual(self.counts["columns"], 11)
        self.assertEqual(self.counts["dummy_columns"], 4)
        self.assertEqual(self.counts["loads"], 4)
        self.assertEqual(self.counts["mask_polygons"], 12)
        self.assertEqual(self.counts["via"], 1441)

    def test_rfm1_carries_every_column_including_the_dummies(self):
        rfm1 = footprint(self.text, "RFM1")
        pads = {}
        for pad in kicad_ops.children(rfm1, "pad"):
            name = re.match(r'\(pad "([^"]*)"', pad).group(1)
            net = re.search(r'\(net "([^"]*)"\)', pad).group(1)
            pads.setdefault(name, set()).add(net)
        self.assertEqual(
            sorted(pads),
            sorted(n.lower() for n in self.rec["columns"]),
        )
        for name in ("rxd0", "rxd5", "txd0", "txd4"):
            self.assertEqual(pads[name], {"RF_" + name.upper()})
        self.assertIn("exclude_from_bom", rfm1)  # the macro copper is board-only
        field = re.search(r'\(property "geometry_sha256" "([^"]*)"', rfm1).group(1)
        self.assertEqual(field, self.rec["geometry_sha256"])

    def test_loads_are_assembled_locked_parts_on_their_nets(self):
        for column, spec in self.rec["loads"].items():
            fp = footprint(self.text, spec["ref"])
            self.assertIn("(locked yes)", fp)
            self.assertIn("(attr smd)", fp)
            self.assertNotIn("exclude_from_bom", fp)
            nets = re.findall(r'\(net "([^"]*)"\)', fp)
            self.assertEqual(sorted(nets), sorted(["RF_" + column, "GND"]))
            x, y, _rot = kicad_ops._at(fp)
            # the record's load centre is in U1's y-up frame; KiCad's y runs down
            cx, cy = spec["centre"]
            self.assertAlmostEqual(x, U1[0] + cx, places=4)  # the record rounds to 0.1 um
            self.assertAlmostEqual(y, U1[1] - cy, places=4)

    def test_load_centres_are_the_board_frame_numbers(self):
        frame = self.rec["board_frame"]["dummy_loads"]
        for spec in self.rec["loads"].values():
            fp = footprint(self.text, spec["ref"])
            x, y, _ = kicad_ops._at(fp)
            bx, by = x - 30.0, 30.0 + 47.35 - y
            self.assertAlmostEqual(bx, frame[spec["ref"]]["centre"][0], places=3)
            self.assertAlmostEqual(by, frame[spec["ref"]]["centre"][1], places=3)

    def test_digest_v2_equals_the_macro_board(self):
        loads = [s["ref"] for s in self.rec["loads"].values()]
        want = kicad_ops.macro_digest(
            macro_text(), kicad_ops.MACRO_CENTRE, kicad_ops.macro_nets(self.rec), loads
        )
        got = kicad_ops.macro_digest(self.text, U1, None, loads)
        self.assertEqual(want, got)
        self.assertEqual(got[1], {"copper": 1664, "load_pads": 8, "mask_polygons": 12})
        # the copper-only digest (R1 v1) agrees too
        self.assertEqual(
            kicad_ops.copper_digest(
                macro_text(), kicad_ops.MACRO_CENTRE, kicad_ops.macro_nets(self.rec)
            ),
            kicad_ops.copper_digest(self.text, U1),
        )

    def test_digest_v2_sees_a_moved_load_and_a_changed_mask(self):
        loads = [s["ref"] for s in self.rec["loads"].values()]
        base = kicad_ops.macro_digest(self.text, U1, None, loads)[0]
        rt1 = footprint(self.text, "RT1")
        x, y, _ = kicad_ops._at(rt1)
        moved = self.text.replace(
            "(at %s %s)" % (kicad_ops._fmt(x), kicad_ops._fmt(y)),
            "(at %s %s)" % (kicad_ops._fmt(x + 0.001), kicad_ops._fmt(y)),
            1,
        )
        self.assertNotEqual(kicad_ops.macro_digest(moved, U1, None, loads)[0], base)
        mask = re.sub(r"\(xy -13\.401 -5\.25\)", "(xy -13.4 -5.25)", self.text, count=1)
        self.assertNotEqual(kicad_ops.macro_digest(mask, U1, None, loads)[0], base)

    def test_schematic_fields_go_onto_the_load(self):
        held = {"RT2": {"atopile_address": "rf_macro.rt[1]", "Value": "49.9R", "LCSC": "C1"}}
        text, _ = kicad_ops.merge_macro(EMPTY_BOARD, macro_text(), self.rec, U1, held)
        fp = footprint(text, "RT2")
        self.assertIn('(property "atopile_address" "rf_macro.rt[1]"', fp)
        self.assertIn('(property "Value" "49.9R"', fp)
        self.assertIn('(property "LCSC" "C1"', fp)

    def test_an_unknown_footprint_is_refused(self):
        bad = macro_text().replace("radar60:RFM1_MASK", "radar60:SOMETHING_ELSE", 1)
        with self.assertRaises(ValueError):
            kicad_ops.merge_macro(EMPTY_BOARD, bad, self.rec, U1)

    def test_a_record_that_disagrees_is_refused(self):
        rec = copy.deepcopy(self.rec)
        rec["columns"].pop("TXD4")
        with self.assertRaises(ValueError):
            kicad_ops.merge_macro(EMPTY_BOARD, macro_text(), rec, U1)
        rec = copy.deepcopy(self.rec)
        rec["loads"]["TXD4"]["ref"] = "RT9"
        with self.assertRaises(ValueError):
            kicad_ops.merge_macro(EMPTY_BOARD, macro_text(), rec, U1)
        rec = copy.deepcopy(self.rec)
        rec["columns"]["RX1"]["dummy"] = True
        with self.assertRaises(ValueError):
            kicad_ops.merge_macro(EMPTY_BOARD, macro_text(), rec, U1)

    def test_macro_nets_follow_the_record(self):
        nets = kicad_ops.macro_nets(self.rec)
        self.assertEqual(nets["RXD0"], "RF_RXD0")
        self.assertEqual(nets["TX3"], "RF_TX3")
        self.assertEqual(nets["GND"], "GND")
        self.assertEqual(kicad_ops.MACRO_NETS, nets)


TERMINALS = {
    # south exits of a part whose courtyard is [20, 22, 32, 34]
    "A1": {
        "kind": "surface",
        "layer": "F.Cu",
        "net": "N1",
        "exit": [24.0, 21.9],
        "outward": [0, -1],
    },
    "A2": {
        "kind": "surface",
        "layer": "F.Cu",
        "net": "N2",
        "exit": [24.3, 21.9],
        "outward": [0, -1],
    },
    "A3": {
        "kind": "surface",
        "layer": "F.Cu",
        "net": "N3",
        "exit": [26.0, 21.9],
        "outward": [0, -1],
    },
    # west exit
    "B1": {
        "kind": "surface",
        "layer": "F.Cu",
        "net": "N4",
        "exit": [19.9, 30.0],
        "outward": [-1, 0],
    },
    # a dog-bone on In2 and a drop: no strip
    "C1": {
        "kind": "dogbone",
        "layer": "In2.Cu",
        "net": "N5",
        "exit": [30, 21.9],
        "outward": [0, -1],
    },
    "D1": {"kind": "drop", "net": "VDD"},
}
COURTYARD = [20.0, 22.0, 32.0, 34.0]


class ExitBandTest(unittest.TestCase):
    def strips(self):
        return bands.exit_strips(TERMINALS, COURTYARD, 0.5, 2.5)

    def test_one_strip_per_surface_exit_from_the_courtyard_edge(self):
        s = {x["ball"]: x for x in self.strips()}
        self.assertEqual(sorted(s), ["A1", "A2", "A3", "B1"])
        self.assertEqual(s["A1"]["rect"], [23.75, 19.5, 24.25, 22.0])
        self.assertEqual(s["B1"]["rect"], [17.5, 29.75, 20.0, 30.25])
        self.assertEqual(s["B1"]["edge"], "west")

    def test_touching_strips_merge_and_far_ones_do_not(self):
        merged = bands.merge_strips(self.strips())
        south = [m for m in merged if m["edge"] == "south"]
        self.assertEqual([m["balls"] for m in south], [["A1", "A2"], ["A3"]])
        self.assertEqual(south[0]["rect"], [23.75, 19.5, 24.55, 22.0])
        entries = bands.keepout_entries(merged)
        self.assertEqual(entries[0]["name"], "exit_south_1")
        self.assertEqual(len(entries[0]["polygon"]), 4)

    def test_a_slot_on_its_own_net_takes_the_band_and_another_is_refused(self):
        own = {"name": "cap", "rect": [23.8, 20.0, 24.0, 21.0], "nets": {"N1"}}
        kept, mine, errors = bands.judge_slots(self.strips(), [own])
        self.assertEqual([s["ball"] for s in mine], ["A1"])
        self.assertNotIn("A1", [s["ball"] for s in kept])
        self.assertEqual(errors, [])
        foreign = {"name": "other", "rect": [25.9, 20.0, 26.5, 21.0], "nets": {"N9"}}
        _kept, _mine, errors = bands.judge_slots(self.strips(), [foreign])
        self.assertEqual(len(errors), 1)
        self.assertIn("slot other meets the exit band of A3 (N3)", errors[0])

    def test_touching_is_not_meeting(self):
        slot = {"name": "beside", "rect": [26.25, 20.0, 27.0, 21.0], "nets": {"N9"}}
        self.assertEqual(bands.judge_slots(self.strips(), [slot])[2], [])

    def test_site_parts_leave_their_regions_and_turns(self):
        doc = {
            "region": [
                {"name": "east", "refs": ["@radio.c_apll"], "rect": [0, 0, 1, 1]},
                {"name": "west", "refs": ["@radio.c_rf1", "@radio.c_bb[[]*"], "rect": [0, 0, 1, 1]},
                {"name": "other", "refs": ["@j2"], "rect": [0, 0, 1, 1]},
            ],
            "orientation": {"@radio.c_apll": 0, "@j2": 90},
        }
        removed = bands.drop_site_parts(doc, ["radio.c_apll", "radio.c_rf1"])
        self.assertEqual([r["name"] for r in doc["region"]], ["west", "other"])
        self.assertEqual(doc["region"][0]["refs"], ["@radio.c_bb[[]*"])
        self.assertEqual(doc["orientation"], {"@j2": 90})
        self.assertEqual(sorted(removed["region"]), ["east:@radio.c_apll", "west:@radio.c_rf1"])


class GeneratedConstraintsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import gen_board
        except ImportError as error:  # PyYAML
            raise unittest.SkipTest(str(error))
        cls.gen = gen_board
        cls.fp = gen_board.load()
        cls.doc = gen_board.constraints(cls.fp)

    def test_the_macro_is_a_fixed_block(self):
        (block,) = self.doc["fixed_block"]
        self.assertEqual(block["group"], kicad_ops.MACRO_GROUP)
        self.assertEqual(block["anchor"], "@radio.u1")

    def test_copper_keepouts_are_v1_and_exempt_the_macro(self):
        ko = {k["name"]: k for k in self.doc["copper_keepout"]}
        self.assertEqual(ko["rf_region_1"]["exempt_groups"], [kicad_ops.MACRO_GROUP])
        self.assertEqual(ko["rf_guard_1"]["allow_classes"], ["RF", "PWR", "GND", "ANALOG"])
        self.assertEqual(ko["flash_ep_vias"]["items"], ["vias"])
        self.assertEqual(ko["flash_ep_vias"]["ref"], "@flash.u3")
        for k in ko.values():  # no v0 entry (U1-frame, every layer) is left
            self.assertTrue("rect" in k or "polygon" in k or "rect_mm" in k)
            self.assertIn("name", k)

    def test_pad_anchored_groups(self):
        groups = [g for g in self.doc["group"] if "anchor_pad" in g]
        anchors = {(g["anchor"], g["anchor_pad"]) for g in groups}
        self.assertIn(("@pmic.l_b2", "1"), anchors)
        self.assertIn(("@radio.u1", "H5"), anchors)
        self.assertIn(("@pmic.fb_rf1", "2"), anchors)
        self.assertIn(("@pmic.r_damp_rf1", "2"), anchors)

    def test_window_parts_are_regions_and_the_fanout_is_declared(self):
        names = {r["name"] for r in self.doc["region"]}
        self.assertTrue({"lvds_header", "jtag"} <= names)
        self.assertNotIn("@debug.j2", self.doc["fixed"])
        (fan,) = self.doc["fanout"]
        self.assertEqual(fan["ref"], "@radio.u1")
        self.assertIn("A2", fan["skip_pads"])
        self.assertIn("@radio.c_vbgap", fan["bottom_sites"]["parts"])
        self.assertNotIn("exit_bands", fan)
        self.assertFalse(self.doc["board"]["plane_fallback_drops"])
        self.assertEqual(self.doc["legalize"]["order"], "scarcity")

    def test_slots_clear_the_planned_exit_bands(self):
        """The ball-anchored slots against the exit bands of the committed placement's plan
        (reva/placement-report.json carries them; skipped before a placement exists)."""
        report = EXAMPLE / "board/reva/placement-report.json"
        if not report.is_file():
            self.skipTest("no placement report")
        rep = json.loads(report.read_text())
        bands_ = (rep.get("fanout") or {}).get("bands")
        if not bands_:
            self.skipTest("placement report without exit bands")
        slots = [
            dict(name=name, rect=spec["rect"])
            for name, spec in self.fp.doc["regions"].items()
            if spec.get("slot") and "rect" in spec
        ]
        for band in bands_.values():
            for slot in slots:
                if bands.overlap(band["rect"], slot["rect"]):
                    # only a slot on the band's own path may meet it; integrate.py drops those
                    self.fail("slot %s meets band %s" % (slot["name"], band["balls"]))


if __name__ == "__main__":
    unittest.main()
