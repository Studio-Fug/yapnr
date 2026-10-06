"""Unit tests for ``pnr.constraints.infer_series_line_groups``: the implicit
``line_group`` a declared ``diff_pair``'s two series parts (one per leg, same
footprint -- a USB board's two 22 ohm resistors, say) get when the design leaves
them otherwise unconstrained (the owner's -rails review, 2026-10-05: "we could use
a line constraint to keep the two resistors adjacent to one another, which would
produce more human-like corridor routing")."""

import unittest

from pnr.constraints import Enforcement, compile_constraints, infer_series_line_groups
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

REFS = ["J1", "R1", "R2", "U1"]
USB_PAIR = dict(name="usb", p="USB_DP", n="USB_DN")
MCU_PAIR = dict(name="usb_mcu", p="D_P", n="D_N")


def bridge(ref, pos, pair_net, other_net, footprint="R_0402", along="x"):
    """A two-pad series part: pad 1 on ``pair_net``, pad 2 on ``other_net``, its own
    two pads offset along ``x`` (the usual passive convention) or ``y``."""
    offsets = [(-0.5, 0.0), (0.5, 0.0)] if along == "x" else [(0.0, -0.5), (0.0, 0.5)]
    pads = [Pad("1", pair_net, offsets[0]), Pad("2", other_net, offsets[1])]
    return Component(ref, footprint, pos, 0.0, "top", (1.0, 0.5), (1.0, 0.5), pads=pads)


def graph(parts, locked=()):
    for c in parts:
        c.locked = c.ref in locked
    nets = {}
    for c in parts:
        for p in c.pads:
            if p.net:
                nets.setdefault(p.net, []).append((c.ref, p.name))
    return BoardGraph(
        "series-line",
        parts,
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(40.0, 30.0),
    )


def mcu_board(along="x", footprint2=None):
    """J1 (the connector, unconnected here), R1/R2 bridging usb <-> usb_mcu, U1 (the
    MCU, a stand-in with no pads) -- the shape of 09-mcu-usb-31."""
    r1 = bridge("R1", (10.0, 10.0), "USB_DP", "D_P", along=along)
    r2 = bridge("R2", (10.0, 12.0), "USB_DN", "D_N", footprint=footprint2 or "R_0402", along=along)
    j1 = Component("J1", "USB_Micro", (2.0, 11.0), 0.0, "top", (4.0, 6.0), (4.0, 6.0))
    u1 = Component("U1", "TQFP-44", (25.0, 15.0), 0.0, "top", (10.0, 10.0), (10.0, 10.0))
    return graph([j1, r1, r2, u1])


class InferSeriesLineGroupsTest(unittest.TestCase):
    def test_no_diff_pair_is_a_no_op(self):
        doc = {"board": {"outline": {"w": 40, "h": 30}, "layers": 2}}
        self.assertEqual(infer_series_line_groups(doc, mcu_board()), doc)

    def test_infers_a_line_group_for_the_bridging_pair(self):
        g = mcu_board(along="x")
        doc = infer_series_line_groups({"diff_pair": [USB_PAIR]}, g)
        self.assertEqual(len(doc["line_group"]), 1)
        (group,) = doc["line_group"]
        self.assertEqual(sorted(group["members"]), ["R1", "R2"])
        # Pads along local x at rot 0 -> turn 90 to run them across the line.
        self.assertEqual(group["rot"], 90.0)
        self.assertNotIn("pitch_mm", group)
        self.assertNotIn("gap_mm", group)

    def test_pads_already_across_the_line_need_no_turn(self):
        doc = infer_series_line_groups({"diff_pair": [USB_PAIR]}, mcu_board(along="y"))
        (group,) = doc["line_group"]
        self.assertEqual(group["rot"], 0.0)

    def test_does_not_mutate_its_input(self):
        original = {"diff_pair": [dict(USB_PAIR)]}
        infer_series_line_groups(original, mcu_board())
        self.assertNotIn("line_group", original)

    def test_two_diff_pairs_sharing_a_bridge_infer_one_group_not_two(self):
        doc = infer_series_line_groups({"diff_pair": [USB_PAIR, MCU_PAIR]}, mcu_board())
        self.assertEqual(len(doc["line_group"]), 1)

    def test_opt_out_flag_skips_that_pair(self):
        pair = dict(USB_PAIR, infer_series_line=False)
        doc = infer_series_line_groups({"diff_pair": [pair]}, mcu_board())
        self.assertNotIn("line_group", doc)

    def test_mismatched_footprints_are_not_inferred_as_a_pair(self):
        g = mcu_board(footprint2="R_0603")
        doc = infer_series_line_groups({"diff_pair": [USB_PAIR]}, g)
        self.assertNotIn("line_group", doc)

    def test_ambiguous_bridge_on_a_leg_is_skipped(self):
        # A second part also sitting on USB_DP makes the bridge on that leg ambiguous.
        g = mcu_board()
        g.components.append(bridge("R9", (10.0, 20.0), "USB_DP", "SPARE"))
        doc = infer_series_line_groups({"diff_pair": [USB_PAIR]}, g)
        self.assertNotIn("line_group", doc)

    def test_a_fixed_bridge_part_is_left_alone(self):
        doc = infer_series_line_groups(
            {"fixed": {"R1": {"at": [5, 5]}}, "diff_pair": [USB_PAIR]}, mcu_board()
        )
        self.assertNotIn("line_group", doc)

    def test_a_locked_source_part_is_left_alone(self):
        g = mcu_board()
        doc = infer_series_line_groups(
            {"diff_pair": [USB_PAIR]}, graph(g.components, locked=["R2"])
        )
        self.assertNotIn("line_group", doc)

    def test_an_authored_line_group_already_covering_both_is_left_alone(self):
        doc = {
            "diff_pair": [USB_PAIR],
            "line_group": [dict(name="usb-series", members=["R1", "R2"], pitch_mm=2.5, rot=90)],
        }
        out = infer_series_line_groups(doc, mcu_board())
        self.assertEqual(len(out["line_group"]), 1)
        self.assertEqual(out["line_group"][0]["name"], "usb-series")

    def test_inferred_group_compiles_to_the_tightest_legal_pitch(self):
        g = mcu_board()
        doc = infer_series_line_groups(
            {
                "board": {"outline": {"w": 40, "h": 30}, "layers": 2, "default_clearance_mm": 0.3},
                "diff_pair": [USB_PAIR],
            },
            g,
        )
        cc = compile_constraints(doc, g.refs)
        (line,) = [c for c in cc.constraints if c.kind == "line_group"]
        self.assertIs(line.enforcement, Enforcement.HARD)
        self.assertIsNone(line.params["pitch_mm"])
        self.assertEqual(line.params["gap_mm"], 0.3)


if __name__ == "__main__":
    unittest.main()
