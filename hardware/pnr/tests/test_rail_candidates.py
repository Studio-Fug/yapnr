"""Plane or trace, per candidate rail (pnr.plane_partition._rail_decision, for_route).

A ``plane_partition`` entry's ``nets`` (without a ``region``) list CANDIDATE rails of
the layer: :func:`pnr.plane_partition.for_route` decides each one plane or trace (its
own declared IR budget, else its declared current, else its terminal count) before
partitioning, and reports the decision with its reason. A rail decided ``trace`` gets
no territory at all; its terminals become foreign copper, same as any net the entry
does not name, and the router routes it as an ordinary trace elsewhere. An entry with
a ``region`` (an outer pour, :func:`pnr.plane_partition._outer`) is untouched: every
one of its candidates still gets a piece of the pour, regardless of current or
terminal count.
"""

import unittest

import pnr.stack
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.plane_partition import CANDIDATE_CURRENT_FLOOR_A, CANDIDATE_TERMINALS_MIN, for_route
from pnr.power_spec import parse_partition
from pnr.route.detail.grid import RouteGrid

W, H = 24.0, 20.0


def part(ref, at, nets, pitch=1.2):
    pads = [
        Pad(str(k + 1), net, ((k - (len(nets) - 1) / 2) * pitch, 0.0), (0.6, 0.6))
        for k, net in enumerate(nets)
    ]
    return Component(ref, "c", at, 0.0, "top", (len(nets) * pitch, 1.0), (1.0, 1.0), pads=pads)


def header(ref, at, net):
    """A one-pad, through-hole terminal of ``net`` (an exact ``via`` terminal, as a
    connector pin or a BGA ball's drop via would be)."""
    pads = [Pad("1", net, (0.0, 0.0), (1.0, 1.0), through_hole=True)]
    return Component(ref, "conn", at, 0.0, "top", (1.0, 1.0), (1.0, 1.0), pads=pads)


def board(components):
    nets = {}
    for c in components:
        for p in c.pads:
            if p.net:
                nets.setdefault(p.net, []).append((c.ref, p.name))
    return BoardGraph(
        name="rails",
        components=components,
        nets=[Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        outline=BoardOutline(W, H),
    )


def stack_of(*, rules):
    record = {
        "layers": [
            {"name": "F.Cu", "type": "signal", "zones": []},
            {"name": "In1.Cu", "type": "power", "zones": [], "copper_mm": 0.035},
            {"name": "In2.Cu", "type": "power", "zones": [], "copper_mm": 0.035},
            {"name": "B.Cu", "type": "signal", "zones": []},
        ]
    }
    return pnr.stack.resolve(rules, record)


def grid_of(g):
    return RouteGrid.from_graph(
        g, W, H, pitch=0.25, clearance=0.2, track_width=0.25, via_radius=0.3
    )


def route(components, entry, extra_rules=None):
    """``for_route`` on a fresh ``In2.Cu`` dedicated plane of ``entry`` over
    ``components``; returns its single :class:`Partition`."""
    g = board(components)
    (parsed,) = parse_partition([dict(entry, layer="In2.Cu")])
    rules = dict(
        net_classes=[dict(nets=[n.name for n in g.nets], plane_layer="In2.Cu")],
        plane_partition=[parsed],
        **(extra_rules or {}),
    )
    stack = stack_of(rules=rules)
    (part,) = for_route(grid_of(g), g, rules, stack, W, H)
    return part


# A VDD-like rail: 14 BGA-style balls plus a header, 0.15 A (the real -rails rung).
def vdd_rail():
    comps = [header("J6", (2.0, 2.0), "VDD")]
    comps += [part("U1R%d" % k, (10.0 + k * 0.8, 10.0), ["VDD"]) for k in range(13)]
    return comps


class DecisionTest(unittest.TestCase):
    def test_a_well_spread_substantial_current_rail_is_a_plane(self):
        comps = vdd_rail() + [header("J7", (20.0, 18.0), "VBAT")]
        part = route(comps, dict(nets=["VDD", "VBAT"], currents={"VDD": 0.15, "VBAT": 0.001}))
        self.assertEqual(part.report["candidates"]["VDD"]["decision"], "plane")
        self.assertIn("0.15", part.report["candidates"]["VDD"]["reason"])

    def test_a_trickle_rail_with_few_terminals_is_a_trace(self):
        comps = vdd_rail() + [header("J7", (20.0, 18.0), "VBAT")]
        part = route(comps, dict(nets=["VDD", "VBAT"], currents={"VDD": 0.15, "VBAT": 0.001}))
        cand = part.report["candidates"]["VBAT"]
        self.assertEqual(cand["decision"], "trace")
        self.assertIn("0.001", cand["reason"])
        self.assertNotIn("VBAT", {r.net for r in part.regions})

    def test_a_traced_rail_is_the_same_as_one_never_declared(self):
        # VDD's territory with a traced VBAT in the candidate list must match VDD's
        # territory on the same board with VBAT never named at all: a traced
        # candidate is dropped before partitioning, same as any net the entry does
        # not list (its terminals become foreign copper either way).
        comps = vdd_rail() + [header("J7", (10.4, 15.0), "VBAT")]
        with_vbat = route(comps, dict(nets=["VDD", "VBAT"], currents={"VDD": 0.15, "VBAT": 0.001}))
        self.assertEqual(with_vbat.report["candidates"]["VBAT"]["decision"], "trace")
        without_vbat = route(comps, dict(nets=["VDD"], currents={"VDD": 0.15}))
        self.assertEqual(
            sorted((r.net, r.priority, r.outline, r.holes) for r in with_vbat.regions),
            sorted((r.net, r.priority, r.outline, r.holes) for r in without_vbat.regions),
        )
        self.assertEqual(
            with_vbat.report["nets"]["VDD"]["area_mm2"],
            without_vbat.report["nets"]["VDD"]["area_mm2"],
        )

    def test_enough_terminals_earns_a_plane_despite_the_current(self):
        # A trickle rail (0.001 A) spread over many terminals still gets a mesh.
        trickle = [
            header("T%d" % k, (3.0 + k * 1.5, 2.0 + (k % 3) * 1.5), "SENSE") for k in range(8)
        ]
        comps = vdd_rail() + trickle
        part = route(comps, dict(nets=["VDD", "SENSE"], currents={"VDD": 0.15, "SENSE": 0.001}))
        self.assertGreaterEqual(8, CANDIDATE_TERMINALS_MIN)
        cand = part.report["candidates"]["SENSE"]
        self.assertEqual(cand["decision"], "plane")
        self.assertIn("terminals", cand["reason"])

    def test_a_declared_budget_earns_a_plane_despite_the_current_and_terminals(self):
        comps = vdd_rail() + [header("J7", (20.0, 18.0), "VBAT")]
        part = route(
            comps,
            dict(
                nets=["VDD", "VBAT"],
                currents={"VDD": 0.15, "VBAT": 0.001},
                budgets_mohm={"VBAT": 5.0},
            ),
        )
        cand = part.report["candidates"]["VBAT"]
        self.assertEqual(cand["decision"], "plane")
        self.assertIn("IR budget", cand["reason"])

    def test_a_plain_ir_drop_check_does_not_by_itself_earn_a_plane(self):
        # Every rail checked for IR drop (ordinary good practice) is not the same as
        # every rail needing a plane: only a budget the partition entry itself
        # declares (budgets_mohm) does.
        comps = vdd_rail() + [header("J7", (20.0, 18.0), "VBAT")]
        part = route(
            comps,
            dict(nets=["VDD", "VBAT"], currents={"VDD": 0.15, "VBAT": 0.001}),
            extra_rules=dict(
                ir_drop=[
                    dict(
                        net=n,
                        sources=["%s:1" % ref],
                        sinks="all",
                        current_a=cur,
                        budget_mv=33.0,
                    )
                    for n, ref, cur in (("VDD", "J6", 0.15), ("VBAT", "J7", 0.001))
                ]
            ),
        )
        self.assertEqual(part.report["candidates"]["VDD"]["decision"], "plane")
        self.assertEqual(part.report["candidates"]["VBAT"]["decision"], "trace")

    def test_undeclared_current_is_kept_a_plane_not_guessed_a_trace(self):
        # Two rails, only one with a declared current: the other is not punished
        # for a number nobody gave it.
        comps = [
            part("J1", (2.0, 10.0), ["V1", "V2"]),
            part("C1", (10.0, 4.0), ["V1"]),
            part("C2", (16.0, 4.0), ["V1"]),
            part("C3", (10.0, 16.0), ["V2"]),
            part("C4", (16.0, 16.0), ["V2"]),
        ]
        rail_part = route(comps, dict(nets=["V1", "V2"], currents={"V1": 2.0}))
        self.assertEqual(rail_part.report["candidates"]["V1"]["decision"], "plane")
        v2 = rail_part.report["candidates"]["V2"]
        self.assertEqual(v2["decision"], "plane")
        self.assertIn("not declared", v2["reason"])

    def test_the_sole_candidate_on_a_layer_is_always_a_plane(self):
        comps = [header("J7", (20.0, 18.0), "VBAT")]
        part = route(comps, dict(nets=["VBAT"], currents={"VBAT": 0.0001}))
        self.assertEqual(part.report["candidates"]["VBAT"]["decision"], "plane")
        self.assertIn("only candidate", part.report["candidates"]["VBAT"]["reason"])

    def test_current_floor_is_a_module_constant_above_zero(self):
        self.assertGreater(CANDIDATE_CURRENT_FLOOR_A, 0.0)
        self.assertGreater(CANDIDATE_TERMINALS_MIN, 1)


def _contains(region, p):
    from pnr.stack import _inside

    if region.outline is None:
        return True
    return _inside(region.outline, p) and not any(_inside(h, p) for h in region.holes)


class OuterPourUnaffectedTest(unittest.TestCase):
    """An outer pour (``region``) keeps every candidate, regardless of current or
    terminal count: the decision rule is only for a dedicated plane's territory."""

    def test_every_candidate_of_an_outer_pour_still_gets_a_piece(self):
        comps = [
            part("U1", (10.0, 10.0), ["VIN", "SW", "GND"]),
        ]
        g = board(comps)
        entry = dict(
            layer="F.Cu",
            nets=["VIN", "SW", "GND"],
            region=dict(refs=["U1"], margin_mm=2.0),
            terminals="pad",
            split_gap_mm=0.2,
            min_width_mm=0.25,
            currents={"VIN": 2.0},  # SW and GND: no current at all, one terminal each
        )
        rules = dict(plane_partition=[entry])
        (part_,) = for_route(grid_of(g), g, rules, None, W, H, outer=True)
        got = {r.net for r in part_.regions}
        self.assertEqual(got, {"VIN", "SW", "GND"})
        self.assertNotIn("candidates", part_.report)  # the decision never runs here


if __name__ == "__main__":
    unittest.main()
