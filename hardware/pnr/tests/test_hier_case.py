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

    def test_the_report_carries_the_legalization_motion(self):
        """pnr-report.json's ``legal_motion``: the chosen block trials combined and the chosen
        top seed's macro legalization (pnr.place.motion)."""
        motion = self.report["legal_motion"]
        self.assertEqual(set(motion), {"block", "top"})
        for stage in motion.values():
            self.assertLessEqual(stage["moved"], stage["count"])
            self.assertTrue(0.0 <= stage["topology"] <= 1.0)
        rec = dict(count=2, moved=1, turned=0, sum_mm=1.0, max_mm=1.0, topology=1.0)
        seeds = [
            dict(id="top-00", legal_motion=dict(rec, moved=0)),
            dict(id="top-01", legal_motion=rec),
        ]
        synth = [dict(chosen=dict(legal_motion=rec)), dict(chosen=dict(legal_motion=rec))]
        got = hier_case.legal_motion(synth, seeds, "top-01-route")
        self.assertEqual(got["top"], rec)
        self.assertEqual((got["block"]["count"], got["block"]["moved"]), (4, 2))

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

    def test_the_bundle_animates_in_chapters(self):
        from pnr.animate import hier, storyboard
        from pnr.animate.cli import trace_digest

        case = provenance.Trace(self.root / "trace")
        board = storyboard.build(case)
        self.assertEqual(board["kind"], "hier")
        types = [s["type"] for s in board["scenes"]]
        self.assertEqual(types[:4], ["title", "source", "chapter", "block-grid"])
        for kind in ("block-montage", "reuse", "lift", "placement", "route", "end"):
            self.assertIn(kind, types)
        self.assertEqual(types.count("chapter"), 3)
        self.assertTrue(any(n.startswith("block:") for n in board["path"]))
        renderer, frames = hier.make(case, board, 320, 60, 12.0, "showcase")
        kinds = {(v.card or {}).get("kind") for v, _ms in frames}
        self.assertTrue({"title", "chapter", "grid", "reuse"} <= kinds)
        # The knit starts from the joins the block copper makes.
        done = case.kind("top-00-route", "fixed")[0]["connections_done"]
        knit = [v for v, _ms in frames if v.phase in ("negotiation", "commit", "routed")]
        self.assertTrue(knit and all(v.progress[0] >= done for v in knit))
        self.assertTrue(all(v.fixed is not None for v in knit))
        # Rigid macros: every member follows its block (block frame) under the macro pose.
        placed = [v for v, _ms in frames if v.phase == "global-placement" and v.bodies]
        self.assertTrue(placed)
        for view, _ms in frames[:: max(1, len(frames) // 12)]:
            self.assertEqual(renderer.frame(view).size, (renderer.width, renderer.height))
        # The block traces count in the trace digest.
        before = trace_digest(case)
        sub = next((self.root / "trace" / "blocks").iterdir()) / "header.json"
        text = sub.read_text()
        try:
            sub.write_text(text + " ")
            self.assertNotEqual(trace_digest(provenance.Trace(self.root / "trace")), before)
        finally:
            sub.write_text(text)


class RetryTest(unittest.TestCase):
    """A representative-pad retry that wins: the top-seed selection names the retry's own
    route scope, whose recorded copper is the top copper in routes.json."""

    def test_the_winning_retry_is_the_selected_route(self):
        real = hier_case.knit

        def knit(case, k, flat, reps, choice=None, attempt=0):
            result = real(case, k, flat, reps, choice, attempt)
            if attempt == 0:  # pretend the first knit left GND split
                result.update(
                    split_nets=["GND"],
                    missing=result["missing"] + 1,
                    objective=[result["objective"][0] + 1] + result["objective"][1:],
                )
            return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            root.mkdir()
            (root / "design.json").write_text(json.dumps(design()))
            (root / "source-graph.json").write_text(graph().to_json())
            clean = {k: v for k, v in os.environ.items() if not k.startswith("PNR_")}
            clean[trace.ENV_DIR] = str(root / "trace")
            with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(
                hier_case, "knit", side_effect=knit
            ):
                report, _case = hier_case.run(root, 0)
                recorder = trace.current()
                if recorder is not None:
                    recorder.close()
            self.assertEqual(report["hier"]["seeds"][0]["representative_retries"], 1)
            case = provenance.Trace(root / "trace")
            routes = [s.id for s in case.of_type("route")]
            self.assertEqual(routes, ["top-00-route", "top-00-route-r1"])
            (select,) = [s for s in case.selects if s["id"] == "top-seed"]
            self.assertEqual(select["chosen"], "top-00-route-r1")
            (end,) = case.kind(select["chosen"], "route_end")
            recorded = sorted(
                tuple(t[1:5])
                for digest in end["nets"].values()
                for t in (case.blob(digest) or {}).get("tracks", [])
            )
            saved = json.loads((root / "routes.json").read_text())["tracks"]
            top = saved[len(saved) - report["hier"]["top_copper"]["tracks"] :]
            um = trace.um
            self.assertEqual(
                recorded, sorted((um(a[0]), um(a[1]), um(b[0]), um(b[1])) for *_, a, b, _w in top)
            )
            # The animation's critical path runs through the retry, not the first knit.
            order, _competitors, _entry = provenance.critical_path(
                provenance.from_hier(case), "final"
            )
            ids = [n.id for n in order]
            self.assertIn("top-00-route-r1", ids)
            self.assertNotIn("top-00-route", ids)


if __name__ == "__main__":
    unittest.main()
