"""The hierarchical ladder driver (regression/hier_case.py) on a synthetic 10-part design:
two identical 3-part channel blocks, a 2-part driver block, a fixed connector and a
top-level capacitor, with tiny budgets."""

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hier_case

from pnr import provenance, trace
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.geometry import pad_rects

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

PARTS = [
    ("J1", "top.j1", "VCC", "GND"),
    ("C1", "top.c_bulk", "VCC", "GND"),
    ("R1", "top.drv.r1", "VCC", "SIG"),
    ("R2", "top.drv.r2", "SIG", "GND"),
    ("R3", "top.ch_a.r", "SIG", "LA"),
    ("D1", "top.ch_a.d", "GND", "LA"),
    ("C2", "top.ch_a.c", "VCC", "GND"),
    ("R4", "top.ch_b.r", "SIG", "LB"),
    ("D2", "top.ch_b.d", "GND", "LB"),
    ("C3", "top.ch_b.c", "VCC", "GND"),
]


def design():
    parts = [dict(ref=r, address=a, pins={"1": n1, "2": n2}) for r, a, n1, n2 in PARTS]
    constraints = dict(
        schema="v0",
        board=dict(outline=dict(w=34, h=26), layers=2, default_clearance_mm=0.4),
        fab=FAB,
        fixed={"J1": dict(at=[4, 13], rot=0, side="top")},
        net_class={"supply": dict(nets=["VCC"], width_mm=0.4)},
    )
    budget = dict(
        utilisations=[0.3],
        aspects=[1.5],
        trial_seeds=[0, 1],
        block_iters=80,
        top_seeds=1,
        top_iters=80,
    )
    return dict(name="hier-test-10", parts=parts, constraints=constraints, hier=budget)


def graph():
    comps = []
    for i, (ref, _address, a, b) in enumerate(PARTS):
        if ref == "J1":
            pads = [
                Pad("1", a, (0.0, 0.0), (1.7, 1.7), True, (1.0, 1.0), True),
                Pad("2", b, (0.0, -2.54), (1.7, 1.7), True, (1.0, 1.0), True),
            ]
            comps.append(
                Component(
                    ref,
                    "PinHeader_1x02",
                    (40.0 + 5 * i, 40.0),
                    0.0,
                    "top",
                    (3.0, 5.6),
                    (3.0, 5.6),
                    pads=pads,
                )
            )
            continue
        pads = [Pad("1", a, (-0.95, 0.0), (1.0, 1.45)), Pad("2", b, (0.95, 0.0), (1.0, 1.45))]
        comps.append(
            Component(
                ref,
                "0805",
                (40.0 + 5 * i, 40.0),
                0.0,
                "top",
                (3.49, 2.05),
                (3.45, 2.01),
                pads=pads,
                smd_body=True,
            )
        )
    nets = {}
    for comp in comps:
        for pad in comp.pads:
            nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "hier-test",
        sorted(comps, key=lambda c: c.ref),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(34.0, 26.0),
    )


class HierCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "case"
        cls.root.mkdir()
        (cls.root / "design.json").write_text(json.dumps(design()))
        (cls.root / "source-graph.json").write_text(graph().to_json())
        clean = {k: v for k, v in os.environ.items() if not k.startswith("PNR_")}
        clean[trace.ENV_DIR] = str(cls.root / "trace")
        with mock.patch.dict(os.environ, clean, clear=True):
            cls.report, cls.case = hier_case.run(cls.root, 0)
            recorder = trace.current()
            if recorder is not None:
                recorder.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def load(self):
        return hier_case.load(self.root)

    def test_twins_share_a_template_and_one_layout(self):
        templates = self.report["hier"]["templates"]
        self.assertEqual(sorted(len(t["blocks"]) for t in templates), [1, 2])
        twins = next(t for t in templates if len(t["blocks"]) == 2)
        self.assertEqual(twins["blocks"], ["top.ch_a", "top.ch_b"])
        self.assertEqual(len(twins["trials"]), 2)
        macros = self.report["hier"]["macros"]
        chosen = {m["block"]: m["trial"] for m in macros.values()}
        self.assertEqual(chosen["top.ch_a"], chosen["top.ch_b"])
        self.assertEqual(
            sorted(macros), ["MB00", "MB01", "MB02"]
        )  # ch_a, ch_b share a template; drv
        self.assertEqual({m["block"] for m in macros.values()}, {"top.ch_a", "top.ch_b", "top.drv"})

    def test_block_frame_moves_rigidly_onto_the_members(self):
        placed = BoardGraph.from_json((self.root / "placed.json").read_text())
        frames = self.case["frames"]
        for name, frame in frames.items():
            centre, turn = hier_case.macro_pose(frame, placed)
            self.assertIn(turn, (0.0, 90.0, 180.0, 270.0))
            for comp in frame["sub"].components:
                board = placed.component(comp.ref)
                self.assertEqual(board.rot, (comp.rot + turn) % 360)
                for (pad, _net, local), (_pad, _net2, there) in zip(
                    pad_rects(comp), pad_rects(board)
                ):
                    moved = hier_case.to_board((local.cx, local.cy), frame, centre, turn)
                    self.assertLess(math.dist(moved, (there.cx, there.cy)), 1e-6, (name, pad))
            # Block copper that starts on a pad centre in the block frame ends on it on the board.
            tracks, _ = hier_case.block_copper(frame, centre, turn)
            local_tracks, _ = hier_case.block_copper(frame)
            centres = {
                (round(r.cx, 6), round(r.cy, 6)): (c.ref, p)
                for c in frame["sub"].components
                for p, _n, r in pad_rects(c)
            }
            board_centres = {
                (c.ref, p): (r.cx, r.cy) for c in placed.components for p, _n, r in pad_rects(c)
            }
            hits = 0
            for (_n, _la, a, _b, _w), (_n2, _la2, a2, _b2, _w2) in zip(local_tracks, tracks):
                key = (round(a[0], 6), round(a[1], 6))
                if key in centres:
                    hits += 1
                    self.assertLess(math.dist(a2, board_centres[centres[key]]), 1e-6)
            self.assertGreater(hits, 0)

    def test_representatives_are_deterministic(self):
        frames = self.case["frames"]
        reps = hier_case.representatives(frames)
        self.assertEqual(reps, self.case["representatives"])
        self.assertEqual(list(reps), sorted(reps))
        for (block, net), candidates in reps.items():
            frame = frames[block]
            w, h = frame["width"], frame["height"]
            distances = []
            for ref, pad in candidates:
                r = next(r for p, _n, r in pad_rects(frame["sub"].component(ref)) if p == pad)
                distances.append(min(r.cx, w - r.cx, r.cy, h - r.cy))
            self.assertEqual(distances, sorted(distances))
        self.assertEqual(
            sorted(self.report["hier"]["representatives"]), sorted("%s/%s" % k for k in reps)
        )

    def test_every_net_is_connected(self):
        self.assertTrue(self.report["legal"])
        self.assertTrue(self.report["converged"], self.report["summary"])
        self.assertEqual(self.report["unrouted"], [])
        placed = BoardGraph.from_json((self.root / "placed.json").read_text())
        routes = json.loads((self.root / "routes.json").read_text())
        groups = hier_case.pin_groups(placed, routes["tracks"], routes["vias"], 0.6)
        for net in placed.nets:
            self.assertEqual(groups[net.name], [list(range(len(net.pins)))], net.name)
        # Without the top-level copper the blocks are separate islands.
        top = self.report["hier"]["top_copper"]
        self.assertGreater(top["tracks"], 0)

    def test_trace_bundle(self):
        case = provenance.Trace(self.root / "trace")
        blocks = [e for e in case.events() if e["kind"] == "blocks"]
        self.assertEqual(len(blocks), 1)
        entries = {e["block"]: e for e in blocks[0]["blocks"]}
        self.assertEqual(set(entries), {"top.ch_a", "top.ch_b", "top.drv"})
        self.assertEqual(entries["top.ch_a"]["template"], entries["top.ch_b"]["template"])
        for entry in entries.values():
            sub = provenance.Trace(self.root / "trace" / entry["trace"])
            self.assertTrue(sub.kind("start-00", "poses"))
            self.assertTrue(any(s["id"] == "block-rank" for s in sub.selects))
            self.assertIn(entry["trial"], [s for s in sub.scopes if s.startswith("start-")])
            self.assertTrue(case.blob(entry["copper"])["tracks"])
        tops = case.of_type("start")
        self.assertEqual([s.id for s in tops], ["top-00"])
        poses = [e for e in case.kind("top-00", "poses") if e["stage"] == "global"]
        self.assertTrue(poses)
        refs = {p[0] for p in poses[0]["poses"]}
        self.assertEqual(refs, {r for r, *_ in PARTS})
        self.assertEqual(sorted(poses[0]["group_members"]), ["MB00", "MB01", "MB02"])
        fixed = case.kind("top-00-route", "fixed")
        self.assertEqual(len(fixed), 1)
        self.assertGreater(fixed[0]["connections_done"], 0)
        begin = case.kind("top-00-route", "route_begin")[0]
        self.assertEqual(begin["progress"]["done"], fixed[0]["connections_done"])
        end = case.kind("top-00-route", "route_end")[0]
        self.assertEqual(end["progress"]["done"], end["progress"]["total"])
        self.assertEqual([s["id"] for s in case.selects], ["top-seed"])
        self.assertFalse((self.root / "trace" / "errors.json").exists())


if __name__ == "__main__":
    unittest.main()
