"""Tracing never changes a result: route_and_place gives byte-identical placements and routes
with PNR_TRACE_DIR unset, set and unset again (baseline loop and initial pool)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnr import trace
from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.route.feedback import route_and_place

SPEC = dict(
    schema="v0",
    board=dict(outline=dict(w=24, h=18), layers=2, default_clearance_mm=0.4),
    fab=dict(
        track_width_mm=0.25,
        clearance_mm=0.2,
        via_diameter_mm=0.6,
        via_drill_mm=0.3,
        hole_clearance_mm=0.25,
        edge_clearance_mm=0.3,
        min_through_drill_mm=0.3,
        via_annular_mm=0.15,
    ),
    fixed={"J1": dict(at=[4, 9], rot=0, side="top")},
    net_class={
        "supply": dict(nets=["VCC"], width_mm=0.4),
        "return": dict(nets=["GND"], width_mm=0.4),
    },
)


def board():
    def smd(ref, x, a, b):
        pads = [Pad("1", a, (-0.95, 0.0), (1.0, 1.45)), Pad("2", b, (0.95, 0.0), (1.0, 1.45))]
        return Component(
            ref, "R_0805", (x, 30.0), 0.0, "top", (3.4, 2.0), (3.4, 2.0), pads=pads, smd_body=True
        )

    j1 = Component(
        "J1",
        "PinHeader_1x02",
        (40.0, 30.0),
        0.0,
        "top",
        (3.0, 5.6),
        (3.0, 5.6),
        pads=[
            Pad("1", "VCC", (0.0, 1.27), (1.7, 1.7), True, (1.0, 1.0), True),
            Pad("2", "GND", (0.0, -1.27), (1.7, 1.7), True, (1.0, 1.0), True),
        ],
    )
    parts = [
        j1,
        smd("R1", 50.0, "VCC", "LED_A"),
        smd("D1", 55.0, "GND", "LED_A"),
        smd("R2", 60.0, "VCC", "LED_B"),
        smd("D2", 65.0, "GND", "LED_B"),
    ]
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    graph = BoardGraph(
        "noop",
        sorted(parts, key=lambda c: c.ref),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(24.0, 18.0),
    )
    constraints = compile_constraints(SPEC, graph.refs)
    rules = compile_routing_rules(constraints, [n.name for n in graph.nets])
    return graph, constraints, rules


def untimed(value):
    """``value`` without wall-clock fields (the only thing tracing may change)."""
    if isinstance(value, dict):
        return {k: untimed(v) for k, v in value.items() if "seconds" not in k and k != "time"}
    if isinstance(value, list):
        return [untimed(v) for v in value]
    return value


def run(pool):
    graph, constraints, rules = board()
    placed, report = route_and_place(
        graph,
        constraints,
        seed=0,
        iters=120,
        max_rounds=2,
        detail_rules=rules,
        detail_pitch_mm=0.25,
        detail_iters=4,
        spread=1.3,
        initial_pool=dict(starts=3, route_finalists=2, proxy_budget=3) if pool else False,
    )
    route = report.detail_result
    pool_report = untimed(json.loads(json.dumps(report.initial_pool, default=str)))
    return dict(
        placed=placed.to_json(),
        routes=json.dumps(
            dict(tracks=route.tracks, vias=route.vias, unrouted=route.result.unrouted)
            if route is not None
            else None
        ),
        report=json.dumps(
            dict(
                rounds=report.rounds,
                best=report.best_round,
                history=report.connection_history,
                termination=report.termination,
                pool=pool_report,
            ),
            sort_keys=True,
        ),
    )


class TraceNoOpTest(unittest.TestCase):
    def check(self, pool):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("PNR_")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, clean, clear=True):
            before = run(pool)
            os.environ["PNR_TRACE_DIR"] = str(Path(tmp) / "trace")
            traced = run(pool)
            trace.current().close()
            del os.environ["PNR_TRACE_DIR"]
            after = run(pool)
            self.assertEqual(before, traced)
            self.assertEqual(before, after)
            root = Path(tmp) / "trace"
            self.assertFalse((root / "errors.json").exists())
            events = [
                json.loads(line)
                for line in (root / "streams" / "engine.jsonl").read_text().splitlines()
            ]
            kinds = {e["kind"] for e in events}
            self.assertTrue({"scope_begin", "poses", "legal", "select", "route_end"} <= kinds)
            self.assertIn("net", kinds)
            return events

    def test_baseline_loop(self):
        events = self.check(pool=False)
        self.assertTrue(any(e["kind"] == "scope_begin" and e["type"] == "attempt" for e in events))

    def test_initial_pool(self):
        events = self.check(pool=True)
        selects = [e["id"] for e in events if e["kind"] == "select"]
        self.assertEqual(
            selects,
            ["round-01/initial-pool/shortlist", "round-01/initial-pool/chosen", "best-round"],
        )


if __name__ == "__main__":
    unittest.main()
