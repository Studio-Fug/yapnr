"""The plane allocation search (pnr.rail_alloc): every alternative a spec leaves open,
pruned by hard constraints only, screened on the real partition's quality and traced
rails' estimated cost, its finalists routed and the best kept; forcing, the static
A/B arm, determinism and the decision held for later routes of the same design.

The router is stood in for by ``FakeRouter``: its probe and partitions are the real
``for_route`` on a real routing grid; only the signal routing is replaced by a
straight trace per traced rail (its estimated length) so the routed objective is
known exactly."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import pnr.stack
from pnr import rail_alloc
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.plane_partition import for_route
from pnr.power_spec import parse_partition
from pnr.rail_alloc import Choice, choices, hard_reason, routed_rules, trace_estimate, with_fills
from pnr.route.detail.grid import RouteGrid

W, H = 24.0, 20.0
RECORD = {
    "layers": [
        {"name": "F.Cu", "type": "signal", "zones": []},
        {"name": "In1.Cu", "type": "power", "zones": [], "copper_mm": 0.035},
        {"name": "In2.Cu", "type": "power", "zones": [], "copper_mm": 0.035},
        {"name": "B.Cu", "type": "signal", "zones": []},
    ]
}


def ball(ref, at, net):
    pads = [Pad("1", net, (0.0, 0.0), (0.4, 0.4))]
    return Component(ref, "c", at, 0.0, "top", (0.6, 0.6), (0.6, 0.6), pads=pads)


def header(ref, at, net):
    pads = [Pad("1", net, (0.0, 0.0), (1.0, 1.0), through_hole=True)]
    return Component(ref, "conn", at, 0.0, "top", (1.0, 1.0), (1.0, 1.0), pads=pads)


def board(components):
    nets = {}
    for c in components:
        for p in c.pads:
            nets.setdefault(p.net, []).append((c.ref, p.name))
    g = BoardGraph(
        name="alloc",
        components=components,
        nets=[Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        outline=BoardOutline(W, H),
    )
    g.stack = RECORD
    return g


def n0001():
    """The N-0001 situation: a main rail (VDD, 0.15 A) with a header and a dozen
    balls, and a backup rail (VBAT, 1 mA) from a far corner header to one ball; GND
    has a dedicated plane of its own (In1) and is offered as the leftover's taker."""
    comps = [header("J6", (2.0, 2.0), "VDD"), header("J7", (21.0, 17.0), "VBAT")]
    comps += [
        ball("V%d" % k, (8.0 + 1.2 * (k % 6), 8.0 + 1.6 * (k // 6)), "VDD") for k in range(12)
    ]
    comps += [ball("B1", (8.6, 10.4), "VBAT"), ball("G1", (4.0, 16.0), "GND")]
    comps += [header("JG", (20.0, 3.0), "GND")]
    return board(comps)


ENTRY = dict(
    layer="In2.Cu",
    nets=["VDD", "VBAT"],
    currents={"VDD": 0.15, "VBAT": 0.001},
    fill_candidates=["GND"],
    min_width_mm=1.0,
    split_gap_mm=0.3,
    h_mm=0.2,
)


def rules_of(entry=ENTRY, **extra):
    (parsed,) = parse_partition([entry])
    rules = dict(
        net_classes=[
            dict(name="gnd", nets=["GND"], plane_layer="In1.Cu", width_mm=0.25),
            dict(name="vdd", nets=["VDD"], plane_layer="In2.Cu", width_mm=0.25),
            dict(name="vbat", nets=["VBAT"], plane_layer="In2.Cu", width_mm=0.25),
        ],
        plane_partition=[parsed],
        fab=dict(track_width_mm=0.25, via_drill_mm=0.3),
    )
    rules.update(extra)
    return rules


def stack_of(rules):
    return pnr.stack.resolve(rules, RECORD)


class FakeRouter:
    """``route_once`` for :func:`rail_alloc.route` (see the module doc)."""

    def __init__(self):
        self.calls = []

    def __call__(self, graph, constraints, rules, *, allocation=None, probe=None, **kw):
        self.calls.append("probe" if probe is not None else "route")
        rules = with_fills(rules, allocation)
        stack = stack_of(rules)
        grid = RouteGrid.from_graph(
            graph, W, H, pitch=0.25, clearance=0.2, track_width=0.25, via_radius=0.3
        )
        parts = for_route(
            grid, graph, rules, stack, W, H, decisions=allocation["decisions"], capture=probe
        )
        if probe is not None:
            return None
        tracks, vias = [], []
        for net, (decision, _r) in sorted(allocation["decisions"].items()):
            if decision == "trace":
                (entry,) = rules["plane_partition"]
                e = trace_estimate(graph, rules, entry, net)
                tracks.append((net, "F.Cu", (0.0, 0.0), (e["length_mm"], 0.0), 0.25))
                vias += [(net, 0.0, 0.0)] * e["vias"]
        return SimpleNamespace(
            result=SimpleNamespace(unrouted=[], nets={}),
            deferred_nets=set(),
            tracks=tracks,
            vias=vias,
            length_report=None,
            escape_diagnostics={"plane_partition": [p.report for p in parts]},
        )


def decide(graph, rules, mode="search"):
    rail_alloc._MEMO.clear()
    router = FakeRouter()
    with mock.patch.dict(os.environ, {"PNR_RAIL_ALLOC": mode}):
        board_ = rail_alloc.route(graph, None, rules, {}, router)
    return board_, router


class ChoicesTest(unittest.TestCase):
    def test_every_split_and_leftover_once(self):
        got = [c.key() for c in choices(ENTRY, ["VDD", "VBAT"])]
        self.assertEqual(len(got), len(set(got)))
        # {VDD}: owner or GND; {VBAT}: likewise; both: shared, VDD, VBAT or GND.
        self.assertEqual(len(got), 8)
        self.assertIn("In2.Cu: plane VDD | trace VBAT | leftover VDD", got)
        self.assertIn("In2.Cu: plane VDD+VBAT | trace - | leftover shared", got)
        self.assertNotIn("In2.Cu: plane VDD | trace VBAT | leftover shared", got)  # canonical

    def test_a_spec_may_force_a_rail_or_the_leftover(self):
        traced = choices(dict(ENTRY, must_trace=["VBAT"]), ["VDD", "VBAT"])
        self.assertTrue(all(c.trace == ("VBAT",) for c in traced))
        planed = choices(dict(ENTRY, must_plane=["VBAT"]), ["VDD", "VBAT"])
        self.assertTrue(all("VBAT" in c.plane for c in planed))
        forced = choices(dict(ENTRY, fill="VDD", fill_candidates=None), ["VDD", "VBAT"])
        self.assertTrue(all(c.fill == "VDD" and "VDD" in c.plane for c in forced))
        with self.assertRaises(ValueError):
            choices(dict(ENTRY, must_plane=["VDD"], must_trace=["VDD"]), ["VDD", "VBAT"])

    def test_one_candidate_with_a_forced_fill_leaves_nothing_to_decide(self):
        self.assertEqual(len(choices(dict(ENTRY, nets=["VDD"], fill="GND"), ["VDD"])), 1)


class HardConstraintTest(unittest.TestCase):
    def test_a_rail_too_heavy_for_its_trace_width_is_never_traced(self):
        g = n0001()
        rules = rules_of(dict(ENTRY, currents={"VDD": 3.0, "VBAT": 0.001}))
        (entry,) = rules["plane_partition"]
        est = {n: trace_estimate(g, rules, entry, n) for n in ("VDD", "VBAT")}
        why = hard_reason(Choice("In2.Cu", ("VBAT",), ("VDD",), "VBAT"), est)
        self.assertIn("VDD traced", why)
        self.assertIn("class width", why)
        self.assertIsNone(hard_reason(Choice("In2.Cu", ("VDD",), ("VBAT",), "VDD"), est))

    def test_a_trace_over_its_ir_budget_is_never_chosen(self):
        g = n0001()
        rules = rules_of(dict(ENTRY, budgets_mohm={"VBAT": 1.0}))
        (entry,) = rules["plane_partition"]
        est = {n: trace_estimate(g, rules, entry, n) for n in ("VDD", "VBAT")}
        why = hard_reason(Choice("In2.Cu", ("VDD",), ("VBAT",), "VDD"), est)
        self.assertIn("IR budget", why)


class SearchTest(unittest.TestCase):
    def test_the_n0001_situation_gives_the_whole_plane_to_vdd_and_traces_vbat(self):
        board_, router = decide(n0001(), rules_of())
        report = board_.escape_diagnostics["rail_allocation"]
        self.assertEqual(report["chosen"], ["In2.Cu: plane VDD | trace VBAT | leftover VDD"])
        self.assertEqual(report["rails"]["VBAT"]["decision"], "trace")
        self.assertIn("allocation search", report["rails"]["VBAT"]["summary"])
        # Bounded: one probe and at most FINALISTS routed alternatives.
        self.assertEqual(router.calls[0], "probe")
        self.assertLessEqual(router.calls.count("route"), rail_alloc.FINALISTS)
        self.assertEqual(len(report["finalists"]), router.calls.count("route"))
        # The redundant GND fill never wins (GND has In1 to itself).
        for row in report["screened"]:
            if row["choice"].endswith("leftover GND"):
                self.assertIn("redundant_fill", row["penalty"])
        # The winning board's partition says why, with the numbers.
        (part,) = board_.escape_diagnostics["plane_partition"]
        self.assertEqual(part["candidates"]["VBAT"]["decision"], "trace")
        self.assertIn("mm-eq", part["candidates"]["VBAT"]["reason"])

    def test_the_decision_is_deterministic(self):
        a, _ = decide(n0001(), rules_of())
        b, _ = decide(n0001(), rules_of())
        ra = a.escape_diagnostics["rail_allocation"]
        rb = b.escape_diagnostics["rail_allocation"]
        self.assertEqual(ra["chosen"], rb["chosen"])
        self.assertEqual(ra["finalists"], rb["finalists"])
        self.assertEqual(ra["screened"], rb["screened"])

    def test_the_decision_is_held_for_later_routes_of_the_design(self):
        rail_alloc._MEMO.clear()
        g, rules = n0001(), rules_of()
        first = FakeRouter()
        rail_alloc.route(g, None, rules, {}, first)
        again = FakeRouter()
        board_ = rail_alloc.route(g, None, rules, {}, again)
        self.assertEqual(again.calls, ["route"])
        report = board_.escape_diagnostics["rail_allocation"]
        self.assertTrue(report["held"])
        self.assertEqual(report["chosen"], ["In2.Cu: plane VDD | trace VBAT | leftover VDD"])

    def test_a_forced_rail_is_kept(self):
        board_, _ = decide(n0001(), rules_of(dict(ENTRY, must_plane=["VBAT"])))
        (chosen,) = board_.escape_diagnostics["rail_allocation"]["chosen"]
        self.assertIn("plane VDD+VBAT", chosen)

    def test_the_static_arm_is_the_rule_of_thumb_routed_once(self):
        board_, router = decide(n0001(), rules_of(), mode="static")
        report = board_.escape_diagnostics["rail_allocation"]
        self.assertEqual(report["mode"], "static")
        self.assertEqual(report["decisions"]["VBAT"]["decision"], "trace")
        self.assertEqual(router.calls, ["route"])

    def test_an_outer_pour_only_board_is_routed_once_untouched(self):
        rules = dict(plane_partition=[dict(ENTRY, region=dict(refs=["J6"]))])
        calls = []

        def once(graph, constraints, rules, **kw):
            calls.append(kw)
            return SimpleNamespace(escape_diagnostics={})

        rail_alloc.route(n0001(), None, rules, {}, once)
        self.assertEqual(calls, [{}])


class RealRouterTest(unittest.TestCase):
    """The search through the real router (route_board) on a small four-layer board."""

    def test_route_board_decides_every_candidate_and_routes_the_winner(self):
        from pnr.constraints import compile_constraints, compile_routing_rules
        from pnr.route.detail.router import route_board

        def part(ref, at, nets):
            pads = [
                Pad(str(k + 1), net, ((k - (len(nets) - 1) / 2) * 1.0, 0.0), (0.6, 0.6))
                for k, net in enumerate(nets)
            ]
            return Component(ref, "c", at, 0.0, "top", (len(nets), 1.0), (1.0, 1.0), pads=pads)

        comps = [
            part("J1", (2.0, 6.0), ["V1", "GND", "V2"]),
            part("C1", (8.0, 3.0), ["V1", "GND"]),
            part("C2", (16.0, 3.0), ["V1", "GND"]),
            part("C3", (8.0, 9.0), ["V2", "GND"]),
            part("C4", (16.0, 9.0), ["V2", "GND"]),
        ]
        g = board(comps)
        g.outline = BoardOutline(20, 12)
        g.stack = {
            "layers": [
                {"name": "F.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
                {"name": "In1.Cu", "type": "power", "zones": [], "copper_mm": 0.0175},
                {"name": "In2.Cu", "type": "power", "zones": [], "copper_mm": 0.0175},
                {"name": "B.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
            ]
        }
        doc = {
            "schema": "v0",
            "board": {"outline": {"w": 20, "h": 12}, "layers": 4},
            "fixed": {c.ref: {"at": list(c.pos), "rot": 0, "side": "top"} for c in comps},
            "net_class": {
                "gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"},
                "v1": {"nets": ["V1"], "plane_layer": "In2.Cu"},
                "v2": {"nets": ["V2"], "plane_layer": "In2.Cu"},
            },
            "plane_partition": [
                {"layer": "In2.Cu", "nets": ["V*"], "currents": {"V1": 2.0, "V2": 0.005}}
            ],
        }
        c = compile_constraints(doc, g.refs)
        rules = compile_routing_rules(c, [n.name for n in g.nets])
        rail_alloc._MEMO.clear()
        with mock.patch.dict(os.environ, {"PNR_RAIL_ALLOC": "search"}):
            r = route_board(g, c, rules, max_iters=4)
        report = r.escape_diagnostics["rail_allocation"]
        self.assertEqual(report["mode"], "search")
        self.assertEqual(sorted(report["rails"]), ["V1", "V2"])
        self.assertTrue(1 <= len(report["finalists"]) <= rail_alloc.FINALISTS)
        self.assertEqual(r.result.unrouted, [])
        (part_report,) = r.escape_diagnostics["plane_partition"]
        self.assertIn("quality", part_report)
        self.assertIn("penalty_mm", part_report["quality"])
        # V1 (2 A, beyond a 0.25 mm trace) can never be traced.
        self.assertEqual(report["rails"]["V1"]["decision"], "plane")
        traced = [n for n, row in report["rails"].items() if row["decision"] == "trace"]
        self.assertEqual(sorted(r.traced_rails), sorted(traced))


class RulesTest(unittest.TestCase):
    def test_a_traced_rail_leaves_its_plane_class_for_the_later_stages(self):
        rules = rules_of()
        out = routed_rules(rules, ["VBAT"])
        vbat = [c for c in out["net_classes"] if "VBAT" in c["nets"]]
        self.assertEqual(len(vbat), 1)
        self.assertNotIn("plane_layer", vbat[0])
        self.assertEqual(vbat[0]["width_mm"], 0.25)
        self.assertEqual(out["traced_rails"], ["VBAT"])
        self.assertIs(routed_rules(rules, []), rules)

    def test_the_allocation_sets_each_layers_leftover(self):
        rules = rules_of()
        alloc = dict(decisions={}, fills={"In2.Cu": "VDD"})
        self.assertEqual(with_fills(rules, alloc)["plane_partition"][0]["fill"], "VDD")
        self.assertIsNone(rules["plane_partition"][0].get("fill"))
        self.assertIs(with_fills(rules, dict(decisions={}, fills={})), rules)


if __name__ == "__main__":
    unittest.main()
