"""pnr.provenance: critical paths and competitors on synthetic DAGs, traces, halving runs and
synthesis libraries."""

import json
import tempfile
import unittest
from pathlib import Path

from pnr import provenance, trace
from pnr.provenance import Dag, critical_path


def ids(nodes):
    return [n.id for n in nodes]


class CriticalPathTest(unittest.TestCase):
    def test_selection_contributes_only_its_chosen_candidate(self):
        dag = Dag()
        dag.add("source", "artifact", order=0)
        for i, name in enumerate(("a", "b", "c")):
            dag.add(name, "experiment", order=i + 1)
            dag.derive("source", name)
        dag.add("pick", "selection", order=5)
        dag.select("pick", ["a", "b", "c"], "b")
        dag.add("route", "experiment", order=6)
        dag.derive("pick", "route")
        dag.add("final", "artifact", order=7)
        dag.derive("route", "final")
        order, competitors, entry = critical_path(dag, "final")
        self.assertEqual(ids(order), ["source", "b", "pick", "route", "final"])
        self.assertEqual(competitors, {"pick": ["a", "c"]})
        self.assertEqual(entry["b"], 1)  # the source comes first, outside b's ancestry

    def test_independent_inputs_stay_contiguous_in_time_order(self):
        dag = Dag()
        dag.add("source", "artifact", order=0)
        for block, order in (("x", 3), ("y", 1)):
            dag.add(block + "1", "experiment", order=order)
            dag.add(block + "2", "experiment", order=order + 0.5)
            dag.derive("source", block + "1")
            dag.derive(block + "1", block + "2")
        dag.add("assemble", "experiment", order=9)
        dag.derive("x2", "assemble")
        dag.derive("y2", "assemble")
        dag.add("final", "artifact", order=10)
        dag.derive("assemble", "final")
        order, _competitors, entry = critical_path(dag, "final")
        self.assertEqual(ids(order), ["source", "y1", "y2", "x1", "x2", "assemble", "final"])
        self.assertEqual((entry["y2"], entry["x2"]), (1, 3))

    def test_rounds_after_the_best_are_competitors(self):
        dag = Dag()
        dag.add("source", "artifact", order=0)
        previous = "source"
        for r in (1, 2, 3):
            dag.add("r%d" % r, "experiment", order=r)
            dag.derive(previous, "r%d" % r)
            previous = "r%d" % r
        dag.add("best", "selection", order=4)
        dag.select("best", ["r1", "r2", "r3"], "r2")
        dag.add("final", "artifact", order=5)
        dag.derive("best", "final")
        order, competitors, _entry = critical_path(dag, "final")
        self.assertEqual(ids(order), ["source", "r1", "r2", "best", "final"])
        self.assertEqual(competitors["best"], ["r3"])

    def test_dag_json_and_errors(self):
        dag = Dag()
        dag.add("a", "artifact")
        with self.assertRaises(ValueError):
            dag.add("a", "artifact")
        with self.assertRaises(KeyError):
            dag.derive("a", "missing")
        self.assertEqual(dag.to_json()["schema"], "pnr-provenance-v1")


def pool_trace(root):
    """A ladder-shaped trace: a pool of three starts, two routed finalists, one round."""
    rec = trace.Recorder(root)
    rec.section("round-01", "round")
    rec.enter("initial-pool", "pool")
    for start in ("start-00", "start-01", "start-02"):
        rec.enter(start, "start", kind="global")
        rec.poses("global", [["R1", 0, 0, 0.0, "top"]], iter=0, iters=10)
        rec.event("legal", order=[["R1", 1000, 1000, 0.0, "top"]], backtracks=0)
        rec.leave()
    rec.select(
        "shortlist",
        ["start-00", "start-01", "start-02"],
        ["start-00", "start-02"],
        "capacity-proxy",
        {"start-00": 2.0, "start-01": 3.0, "start-02": 1.0},
    )
    for start in ("start-00", "start-02"):
        rec.enter(start + "-route", "route", start=start)
        rec.event("route_end", nets={}, groups={}, unrouted=[], progress=dict(done=1, total=1))
        rec.leave()
    rec.select(
        "chosen",
        ["start-00-route", "start-02-route"],
        "start-02-route",
        "route-objective",
        {"start-00-route": [0, 0, 3, 10.0], "start-02-route": [0, 0, 2, 9.0]},
    )
    rec.leave(type="pool")
    rec.enter("route", "route", reused=True)
    rec.leave()
    rec.leave(type="round")
    rec.select("best-round", ["round-01"], "round-01", "missing-connections", {"round-01": 0})
    rec.close()
    native = trace.Recorder(root, lane="native")
    copper = dict(tracks=[], vias=[], zones=[])
    for stage in ("writeback", "planes", "refill"):
        native.board(
            stage,
            dict(copper=copper, poses=[], frame=(0.0, 0.0)),
            dict(unconnected_items=[], violations=[]),
            1,
        )
    native.result(passed=True, opens=0, violations={}, vias=2, copper_length_mm=9.0)
    native.close()


class TraceAdapterTest(unittest.TestCase):
    def test_pool_winner_path_and_competitors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "trace"
            pool_trace(root)
            (root / "header.json").write_text(
                json.dumps(dict(components=[], nets=[], copper_layers=["F.Cu", "B.Cu"]))
            )
            loaded = provenance.Trace(root)
        dag = provenance.from_trace(loaded)
        order, competitors, entry = critical_path(dag, "final")
        pool = "round-01/initial-pool/"
        self.assertEqual(
            ids(order),
            [
                "source",
                pool + "start-02",
                pool + "shortlist[%sstart-02]" % pool,
                pool + "start-02-route",
                pool + "chosen",
                "round-01/route",
                "best-round",
                "native:writeback",
                "native:planes",
                "native:refill",
                "final",
            ],
        )
        self.assertEqual(
            competitors[pool + "shortlist[%sstart-02]" % pool],
            [pool + "start-00", pool + "start-01"],
        )
        self.assertEqual(competitors[pool + "chosen"], [pool + "start-00-route"])
        self.assertEqual(entry[pool + "start-02"], entry[pool + "start-02-route"])
        self.assertEqual(dag.nodes["final"].metrics["vias"], 2)
        self.assertEqual(
            dag.nodes[pool + "shortlist[%sstart-02]" % pool].selected,
            [pool + "start-00", pool + "start-02"],
        )

    def test_detect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(provenance.detect(root))
            (root / "dataset.jsonl").write_text("")
            self.assertEqual(provenance.detect(root), "halving")


class HalvingAndSynthesisTest(unittest.TestCase):
    def test_halving_winner_lineage(self):
        records = [
            dict(id="p000", stage="place", status="legal", proxy_score=3.0),
            dict(id="p001", stage="place", status="legal", proxy_score=1.0),
            dict(id="p002", stage="place", status="legal", proxy_score=2.0),
            dict(id="p001", stage="screen", status="ok", objective=[0, 0, 1]),
            dict(id="p002", stage="screen", status="ok", objective=[0, 0, 2]),
            dict(id="p001", stage="native", status="ok", objective=[0, 0, 0, 0, 0, 3]),
            dict(id="p002", stage="native", status="ok", objective=[0, 0, 0, 0, 0, 1]),
            dict(id="g1p0", stage="gen-place", status="legal", parent="p002", gen=1),
            dict(id="g1p0", stage="native", status="ok", objective=[0, 0, 0, 0, 0, 0]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "dataset.jsonl").write_text(
                "\n".join(json.dumps(r) for r in records) + "\nnot json\n"
            )
            dag = provenance.from_halving(run)
        order, competitors, _entry = critical_path(dag, "final")
        path = ids(order)
        self.assertEqual(path[-2:], ["halving:best-native", "final"])
        self.assertIn("halving:g1p0/native", path)
        self.assertIn("halving:g1p0/place", path)
        # The winner descends from p002's native evaluation, which descends from its screen.
        self.assertLess(path.index("halving:p002/screen"), path.index("halving:g1p0/place"))
        self.assertIn("halving:p001/native", competitors["halving:best-native"])

    def test_synthesis_ranked_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for template in ("ldo", "usb"):
                (root / template).mkdir()
                ranked = [dict(tag="%s-%d" % (template, k), objective=[0, k]) for k in range(3)]
                (root / template / "library.json").write_text(
                    json.dumps(dict(template_id=template, ranked=ranked))
                )
            self.assertEqual(provenance.detect(root), "synthesis")
            dag = provenance.from_synthesis(root)
        order, competitors, _entry = critical_path(dag, "final")
        self.assertEqual(
            [n.id for n in order if n.kind == "experiment"], ["block:ldo/ldo-0", "block:usb/usb-0"]
        )
        self.assertEqual(competitors["block:usb/rank"], ["block:usb/usb-1", "block:usb/usb-2"])


if __name__ == "__main__":
    unittest.main()
