"""pnr.trace: the pnr-trace-v1 recorder (format, scopes, bounds, self-disable, no paths)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnr import trace
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad


def _graph():
    pads = [Pad("1", "A", (-0.5, 0.0), (0.6, 0.8)), Pad("2", "B", (0.5, 0.0), (0.6, 0.8))]
    comps = [
        Component("R1", "R_0603", (2.0, 3.0), 0.0, "top", (2.0, 1.2), (2.0, 1.2), pads=list(pads)),
        Component("R2", "R_0603", (6.0, 3.0), 90.0, "top", (2.0, 1.2), (2.0, 1.2), pads=list(pads)),
    ]
    nets = [Net("A", 1, [("R1", "1"), ("R2", "1")]), Net("B", 2, [("R1", "2"), ("R2", "2")])]
    return BoardGraph("t", comps, nets, BoardOutline(10.0, 8.0))


class _Tensor:
    def __init__(self, rows):
        self.rows = rows

    def detach(self):
        return self

    def tolist(self):
        return self.rows


def _events(root, lane="engine"):
    path = Path(root) / "streams" / (lane + ".jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


class OffTest(unittest.TestCase):
    def test_unset_is_a_no_op(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(trace.ENV_DIR, None)
            self.assertIsNone(trace.current())
            self.assertIsNone(trace.placement_tracer([], 10))
            self.assertIsNone(trace.route_hook())
            with trace.scope("x", "round") as sid:
                self.assertIsNone(sid)
            trace.note(status="ok")
            trace.select("s", ["a"], "a", "c")
            trace.legal([], _graph())


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "trace"

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self, root=None, **kwargs):
        rec = trace.Recorder(root or self.root, **kwargs)
        self.addCleanup(rec.close)
        return rec

    def test_header_and_units(self):
        rec = self.recorder()
        self.assertTrue(rec.begin_board(_graph(), None, {"fab": {"track_width_mm": 0.25}}))
        header = json.loads((self.root / "header.json").read_text())
        self.assertEqual(header["schema"], "pnr-trace-v1")
        self.assertEqual(header["outline"], {"w": 10000, "h": 8000, "polygon": None})
        self.assertEqual(header["copper_layers"], ["F.Cu", "B.Cu"])
        self.assertEqual(header["connections_total"], 2)
        r1 = header["components"][0]
        self.assertEqual((r1["ref"], r1["pos"], r1["rot"]), ("R1", [2000, 3000], 0.0))
        self.assertEqual(r1["pads"][0]["offset"], [-500, 0])
        self.assertEqual(
            header["nets"][0],
            dict(name="A", pins=[["R1", "1"], ["R2", "1"]], width=250, plane=None),
        )
        # A second writer adopts the header; another part set disables it.
        other = self.recorder()
        self.assertFalse(other.begin_board(_graph()))
        graph = _graph()
        graph.components.pop()
        other.begin_board(graph)
        self.assertFalse(other.active)
        self.assertIn("other components", (self.root / "errors.json").read_text())

    def test_scopes_selections_and_streams(self):
        rec = self.recorder()
        rec.section("round-01", "round")
        with mock.patch.dict(os.environ, {trace.ENV_DIR: str(self.root)}):
            with mock.patch.object(trace, "_RECORDER", rec):
                with trace.scope("attempt-0", "attempt", seed=3):
                    trace.note(hpwl=1.5)
                with self.assertRaises(RuntimeError):
                    with trace.scope("attempt-1", "attempt"):
                        raise RuntimeError("x")
                rec.section("round-02", "round")
                rec.leave(type="round")
                trace.select(
                    "best-round",
                    ["round-01", "round-02"],
                    "round-02",
                    "missing-connections",
                    {"round-01": 3, "round-02": 0.0},
                )
        rec.close()
        events = _events(self.root)
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        begins = [(e["scope"], e["type"]) for e in events if e["kind"] == "scope_begin"]
        self.assertEqual(
            begins,
            [
                ("round-01", "round"),
                ("round-01/attempt-0", "attempt"),
                ("round-01/attempt-1", "attempt"),
                ("round-02", "round"),
            ],
        )
        ends = {e["scope"]: e for e in events if e["kind"] == "scope_end"}
        self.assertEqual(ends["round-01/attempt-0"]["metrics"], {"hpwl": 1.5})
        self.assertEqual(ends["round-01/attempt-1"]["status"], "failed")
        select = [e for e in events if e["kind"] == "select"][0]
        self.assertEqual(
            (select["id"], select["chosen"], select["scope"]), ("best-round", "round-02", "")
        )
        self.assertEqual(select["scores"], {"round-01": 3, "round-02": 0.0})
        # Every line is canonical JSON with sorted keys.
        for line in (self.root / "streams" / "engine.jsonl").read_text().splitlines():
            self.assertEqual(
                line, json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
            )

    def test_second_writer_gets_its_own_stream(self):
        a, b = self.recorder(), self.recorder()
        self.assertEqual((a.stream_name, b.stream_name), ("engine.jsonl", "engine.1.jsonl"))
        with self.assertRaises(ValueError):
            trace.Recorder(self.root, lane="../x")

    def test_blobs_are_content_addressed(self):
        rec = self.recorder()
        one = rec.blob({"tracks": [[0, 1, 2, 3, 4, 5]], "vias": []})
        two = rec.blob({"vias": [], "tracks": [[0, 1, 2, 3, 4, 5]]})
        self.assertEqual(one, two)
        self.assertEqual(len(list((self.root / "blobs").iterdir())), 1)
        data = (self.root / "blobs" / (one + ".json")).read_bytes()
        self.assertEqual(data, b'{"tracks":[[0,1,2,3,4,5]],"vias":[]}')

    def test_placement_snapshots_and_legal_order(self):
        rec = self.recorder()
        graph = _graph()
        tracer = trace.PlacementTracer(rec, graph.components, 50)
        self.assertEqual(tracer.every, 3)
        due = [s for s in range(50) if tracer.due(s)]
        self.assertEqual(due[:3], [0, 3, 6])
        self.assertEqual(due[-1], 49)
        tracer.snapshot(
            0, _Tensor([[1.0, 2.0], [3.0, 4.0]]), _Tensor([[0.1, 0.9, 0, 0], [1, 0, 0, 0]])
        )
        tracer.finish({"R1": (1.5, 2.0), "R2": (3.0, 4.0)}, {"R1": 90.0, "R2": 0.0})
        rec.legal(["R2", "R1"], graph, backtracks=2)
        events = _events(self.root)
        self.assertEqual(
            events[0]["poses"], [["R1", 1000, 2000, 90.0, "top"], ["R2", 3000, 4000, 0.0, "top"]]
        )
        self.assertEqual((events[1]["iter"], events[1]["iters"]), (50, 50))
        self.assertEqual([p[0] for p in events[2]["order"]], ["R2", "R1"])
        self.assertEqual(events[2]["backtracks"], 2)

    def test_pose_expansion_names_members_and_keeps_the_rigid_bodies(self):
        """Snapshots and the legal order of a macro graph name its members (pnr.hier.macro)."""
        from types import SimpleNamespace

        from pnr.constraints import compile_constraints
        from pnr.hier.macro import collapse

        flat = _graph()
        cc = compile_constraints({"board": {"outline": {"w": 10, "h": 8}}}, flat.refs)
        sub = BoardGraph("line", [c for c in _graph().components])
        sub.components[0].pos, sub.components[1].pos = (1.0, 0.6), (4.0, 0.6)
        sub.components[1].rot = 0.0
        mgraph, _, _, plan = collapse(
            flat, cc, {}, [(SimpleNamespace(name="g"), sub, 5.0, 1.2)], prefix="LG", margin=0.0
        )
        rec = self.recorder()
        with mock.patch.dict(os.environ, {trace.ENV_DIR: str(self.root)}):
            with mock.patch.object(trace, "_RECORDER", rec):
                with trace.pose_expansion(plan.trace_rows):
                    tracer = trace.PlacementTracer(rec, mgraph.components, 10)
                    tracer.snapshot(0, _Tensor([[5.0, 4.0]]), _Tensor([[0.0, 1.0, 0.0, 0.0]]))
                    placed = BoardGraph.from_json(mgraph.to_json())
                    placed.component("LG00").pos = (5.0, 4.0)
                    placed.component("LG00").rot = 90.0
                    rec.legal(["LG00"], placed)
                rec.poses("round", [["R1", 1, 2, 0.0, "top"]])
        events = _events(self.root)
        expanded = plan.expand(placed, flat)
        rows = [
            [c.ref, trace.um(c.pos[0]), trace.um(c.pos[1]), c.rot, c.side]
            for c in expanded.components
        ]
        self.assertEqual(events[0]["poses"], rows)
        self.assertEqual(events[0]["groups"], [["LG00", 5000, 4000, 90.0, "top"]])
        self.assertEqual(events[0]["group_members"], {"LG00": ["R1", "R2"]})
        self.assertEqual(events[1]["order"], rows)
        self.assertEqual(events[1]["groups"], [["LG00", 5000, 4000, 90.0, "top"]])
        self.assertNotIn("groups", events[2])
        self.assertEqual(rec.expanders, [])

    def test_header_constraints_only_when_declared(self):
        from pnr.constraints import compile_constraints

        graph = _graph()
        plain = trace.board_header(graph, compile_constraints({}, graph.refs))
        self.assertNotIn("constraints", plain)
        doc = {
            "edge_align": {"R1": {"edge": "south"}},
            "group": [{"members": ["R2"], "anchor": "R1", "radius_mm": 3}],
        }
        header = trace.board_header(graph, compile_constraints(doc, graph.refs))
        self.assertEqual(
            header["constraints"],
            [
                dict(kind="edge_align", refs=["R1"], hard=False, edge="south"),
                dict(kind="group", refs=["R2"], hard=False, anchor="R1", radius_um=3000),
            ],
        )
        doc = {"edge_align": {"R2": {"edge": "north", "hard": True, "tolerance_mm": 1.5}}}
        header = trace.board_header(graph, compile_constraints(doc, graph.refs))
        self.assertEqual(
            header["constraints"],
            [dict(kind="edge_align", refs=["R2"], hard=True, edge="north", tolerance_um=1500)],
        )
        doc = {"line_group": [{"name": "pair", "members": ["R1", "R2"], "pitch_mm": 2.5}]}
        header = trace.board_header(graph, compile_constraints(doc, graph.refs))
        self.assertEqual(
            header["constraints"],
            [
                dict(
                    kind="line_group",
                    refs=["R1", "R2"],
                    hard=True,
                    name="pair",
                    edge="none",
                    pitch_um=2500,
                    rot=0.0,
                )
            ],
        )

    def test_size_budget_drops_provisional_then_all_but_essentials(self):
        rec = self.recorder(max_mb=0.01)  # 10 KiB
        for i in range(400):
            rec._emit("net", True, net="N%d" % i, op="add", provisional=True)
            rec._emit("net", False, net="N%d" % i, op="commit", provisional=False)
        rec.select("s", ["a", "b"], "a", "c")
        rec.close()
        events = _events(self.root)
        levels = [e["level"] for e in events if e["kind"] == "truncated"]
        self.assertEqual(levels, [1, 2])
        self.assertEqual(events[-1]["kind"], "select")
        self.assertLess(rec.written, 0.95 * rec.max_bytes + 2000)
        first = next(i for i, e in enumerate(events) if e["kind"] == "truncated")
        self.assertFalse(any(e.get("provisional") for e in events[first:]))

    def test_errors_disable_and_never_raise(self):
        rec = self.recorder()
        self.assertIsNone(rec.poses("global", object()))  # not a pose list: an internal error
        self.assertFalse(rec.active)
        self.assertIsNone(rec.select("s", ["a"], "a", "c"))
        error = json.loads((self.root / "errors.json").read_text())
        self.assertEqual(error["where"], "poses")
        self.assertNotIn(str(self.root), json.dumps(error))

    def test_error_messages_are_path_free(self):
        trace.record_error(self.root, "native", "x", OSError("cannot open /a/b/c.kicad_pcb"))
        self.assertNotIn("/a/b", (self.root / "errors.json").read_text())

    def test_traces_hold_no_absolute_paths(self):
        rec = self.recorder()
        rec.begin_board(_graph())
        rec.section("round-01", "round")
        rec.poses("round", _graph())
        rec.congestion([[0.0, 2.0], [1.0, 4.0]], 2.5, {"R1": 1.4})
        rec.leave(type="round")
        rec.close()
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.tmp.name, path.read_text())
        congestion = [e for e in _events(self.root) if e["kind"] == "congestion"][0]
        self.assertEqual(congestion["cells"], [[0, 128], [64, 255]])
        self.assertEqual((congestion["nx"], congestion["ny"], congestion["pitch"]), (2, 2, 2500))

    def test_deterministic_bytes(self):
        def record(root):
            rec = self.recorder(root)
            rec.begin_board(_graph())
            rec.section("round-01", "round")
            rec.poses("round", _graph())
            rec.select("best", ["round-01"], "round-01", "missing-connections", {"round-01": 0})
            rec.close()
            return {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }

        self.assertEqual(record(self.root), record(Path(self.tmp.name) / "again"))


class DrcTest(unittest.TestCase):
    def test_drc_summary_uses_the_engine_frame(self):
        report = {
            "unconnected_items": [
                {"items": [{"pos": {"x": 11.0, "y": 48.0}}, {"pos": {"x": 13.0, "y": 47.0}}]}
            ],
            "violations": [
                {"type": "clearance"},
                {
                    "type": "clearance",
                    "description": "Clearance violation (rule 'fab_via_to_smd_pad' clearance"
                    " 0.1270 mm; actual 0.0000 mm)",
                    "items": [{"pos": {"x": 12.5, "y": 49.0}}, {"pos": {"x": 12.0, "y": 49.0}}],
                },
                {"type": "track_dangling"},
            ],
        }
        summary = trace.drc_summary(report, (10.0, 50.0))
        self.assertEqual(summary["open_pairs"], [[1000, 2000, 3000, 3000]])
        self.assertEqual(summary["by_type"], {"clearance": 2, "track_dangling": 1})
        self.assertEqual(
            summary["by_rule"], {"clearance": 1, "fab_via_to_smd_pad": 1, "track_dangling": 1}
        )
        self.assertEqual((summary["unconnected"], summary["violations"]), (1, 3))
        # A finding is marked at its first item (the via of a via-to-pad finding).
        self.assertEqual(summary["findings"], [[2500, 1000]])

    def test_placement_tracer_errors_disable_the_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = trace.Recorder(Path(tmp) / "t")
            tracer = trace.PlacementTracer(recorder, [], 4)
            tracer.snapshot(0, object(), object())  # not tensors: must not raise
            self.assertFalse(recorder.active)
            tracer.finish({}, {})
            self.assertTrue((Path(tmp) / "t" / "errors.json").is_file())


if __name__ == "__main__":
    unittest.main()
