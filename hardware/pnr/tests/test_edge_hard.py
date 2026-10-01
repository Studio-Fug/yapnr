"""Opt-in hard edge alignment (edge_align ``hard: true``): the parser, the legalizer's
edge band, the violation checks, and an unchanged soft path."""

import unittest
from unittest import mock

from pnr.constraints import ConstraintError, Enforcement, compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place import placer
from pnr.place.geometry import edge_distance, hard_edge_bands
from pnr.place.metrics import hard_violations, translation_checker

FAB = dict(
    track_width_mm=0.25,
    clearance_mm=0.2,
    via_diameter_mm=0.6,
    via_drill_mm=0.3,
    hole_clearance_mm=0.25,
    edge_clearance_mm=0.3,
    min_through_drill_mm=0.3,
    via_annular_mm=0.15,
)
EDGE = {
    "J1": dict(edge="south", hard=True, tolerance_mm=1.0),
    "SW1": dict(edge="south", hard=True, tolerance_mm=1.0),
    "D1": dict(edge="south", hard=True, tolerance_mm=1.0),
}
ORIENTATION = {"J1": 90, "SW1": 0, "D1": 0}


def two_pin(ref, a, b, size=(3.45, 1.99), offset=0.95, pad=(1.0, 1.45)):
    pads = [Pad("1", a, (-offset, 0.0), pad), Pad("2", b, (offset, 0.0), pad)]
    return Component(ref, ref[:1], (0.0, 0.0), 0.0, "top", size, size, pads=pads, smd_body=True)


def board():
    """The 12 parts of the ``edge-io-12`` showcase (a 555 "hold to blink" board), with
    footprint-sized synthetic pads."""
    timer = ["GND", "TIMING", "CLOCK", "RESET", "CONTROL", "TIMING", "DISCHARGE", "VCC"]
    parts = [
        Component(
            "J1",
            "PinHeader_1x02",
            (0.0, 0.0),
            0.0,
            "top",
            (3.63, 8.73),
            (3.63, 8.73),
            pads=[
                Pad("1", "VCC", (0.0, 0.0), (1.7, 1.7), True, (1.0, 1.0), True),
                Pad("2", "GND", (0.0, -2.54), (1.7, 1.7), True, (1.0, 1.0), True),
            ],
        ),
        Component(
            "U1",
            "SOIC-8",
            (0.0, 0.0),
            0.0,
            "top",
            (7.49, 5.72),
            (7.49, 5.72),
            pads=[
                Pad(
                    str(i + 1),
                    net,
                    (-2.475 if i < 4 else 2.475, 1.905 - 1.27 * (i % 4)),
                    (1.95, 0.6),
                )
                for i, net in enumerate(timer[:4] + timer[4:][::-1])
            ],
            smd_body=True,
        ),
        two_pin("R1", "VCC", "DISCHARGE"),
        two_pin("R2", "DISCHARGE", "TIMING"),
        two_pin("R3", "CLOCK", "LED_A"),
        two_pin("R4", "RESET", "GND"),
        two_pin("C1", "TIMING", "GND", (3.49, 2.05)),
        two_pin("C2", "CONTROL", "GND", (3.49, 2.05)),
        two_pin("C3", "VCC", "GND", (3.49, 2.05)),
        two_pin("C4", "VCC", "GND", (3.49, 2.05)),
        two_pin("D1", "GND", "LED_A", (3.49, 2.04), 0.9375, (0.975, 1.4)),
        two_pin("SW1", "VCC", "RESET", (7.9, 4.0), 2.7, (2.0, 1.6)),
    ]
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "edge-io",
        sorted(parts, key=lambda c: c.ref),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(36.0, 26.0),
    )


def compiled(graph, edge=EDGE, orientation=ORIENTATION):
    doc = dict(
        schema="v0",
        board=dict(outline=dict(w=36, h=26), layers=2, default_clearance_mm=0.4),
        fab=FAB,
        net_class={"supply": dict(nets=["VCC"], width_mm=0.4)},
    )
    if edge:
        doc["edge_align"] = edge
    if orientation:
        doc["orientation"] = orientation
    return compile_constraints(doc, graph.refs)


class ParserTest(unittest.TestCase):
    def test_hard_is_opt_in(self):
        refs = board().refs
        soft = compile_constraints({"edge_align": {"SW1": {"edge": "south"}}}, refs)
        (con,) = soft.constraints
        self.assertIs(con.enforcement, Enforcement.SOFT)
        self.assertEqual(con.params, {"edge": "south", "side": None})
        self.assertEqual(hard_edge_bands(soft), {})
        hard = compile_constraints({"edge_align": {"SW1": {"edge": "north", "hard": True}}}, refs)
        (con,) = hard.constraints
        self.assertIs(con.enforcement, Enforcement.HARD)
        self.assertEqual(con.params["tolerance_mm"], 1.0)
        self.assertEqual(con.weight, 5.0)
        self.assertEqual(hard_edge_bands(hard), {"SW1": ("north", 1.0)})
        self.assertEqual(
            hard_edge_bands(
                compile_constraints(
                    {"edge_align": {"D1": {"edge": "west", "hard": True, "tolerance_mm": 1.5}}},
                    refs,
                )
            ),
            {"D1": ("west", 1.5)},
        )
        for bad in (
            {"edge": "south", "hard": "yes"},
            {"edge": "south", "hard": True, "tolerance_mm": 0.2},
            {"edge": "south", "hard": True, "tolerance_mm": float("nan")},
            {"edge": "south", "tolerance_mm": True},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConstraintError):
                compile_constraints({"edge_align": {"SW1": bad}}, refs)


class PlaceTest(unittest.TestCase):
    def test_hard_edge_parts_stay_within_tolerance(self):
        graph = board()
        cc = compiled(graph)
        orders = set()
        for seed in range(8):
            with self.subTest(seed=seed):
                placed, report = placer.place(graph, cc, seed=seed, iters=150, spread=1.3)
                self.assertTrue(report.legal, report.summary())
                for ref in EDGE:
                    comp = placed.component(ref)
                    self.assertLessEqual(edge_distance(comp, "south", 36.0, 26.0), 1.0 + 1e-6)
                    self.assertEqual(comp.rot, float(ORIENTATION[ref]))
                orders.add(tuple(sorted(EDGE, key=lambda r: placed.component(r).pos[0])))
        self.assertGreater(len(orders), 1)  # the order along the edge is the placer's choice

    def test_inflation_is_capped_inside_the_band(self):
        graph = board()
        cc = compiled(graph)
        placed, report = placer.place(
            graph, cc, seed=3, iters=150, spread=1.3, inflation={"SW1": 2.5, "J1": 2.0}
        )
        self.assertTrue(report.legal, report.summary())
        for ref in EDGE:
            self.assertLessEqual(edge_distance(placed.component(ref), "south", 36, 26), 1.0 + 1e-6)

    def test_soft_edge_align_takes_the_unchanged_path(self):
        graph = board()
        soft = {ref: dict(edge="south") for ref in EDGE}
        cc = compiled(graph, edge=soft)
        real = placer.legalize
        calls = []

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(placer, "legalize", side_effect=spy):
            spied, _ = placer.place(graph, cc, seed=1, iters=120, spread=1.3)
        self.assertTrue(calls)
        self.assertTrue(all("edge_bands" not in kwargs for kwargs in calls))
        plain, _ = placer.place(graph, cc, seed=1, iters=120, spread=1.3)
        self.assertEqual(spied.to_json(), plain.to_json())

    def test_violations(self):
        graph = board()
        cc = compiled(graph)
        placed, report = placer.place(graph, cc, seed=0, iters=150, spread=1.3)
        self.assertTrue(report.legal)
        legal = translation_checker(placed, cc)
        sw1 = placed.component("SW1")
        old = sw1.pos
        sw1.pos = (old[0], 2.0 + 1.0 + 0.1)  # courtyard bottom 1.1 mm above the edge
        self.assertIn("SW1", hard_violations(placed, cc)["group_outside"])
        self.assertFalse(legal(sw1))
        sw1.pos = (old[0], 2.0 + 0.9)
        self.assertNotIn("SW1", hard_violations(placed, cc)["group_outside"])
        sw1.pos = old
        self.assertTrue(legal(sw1))


if __name__ == "__main__":
    unittest.main()
