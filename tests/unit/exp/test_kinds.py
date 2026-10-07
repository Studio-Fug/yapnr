"""Sharding helpers: the benchmark cells, Monte Carlo stages and RF runs expand into the tasks
the design describes, and their assembly writes the layouts the local tools read."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import kinds, spec, testing
from yapnr.exp.kinds import mc

CID = "20261002-bench-abcdef"


def context(tmp, visibility="public", **resources):
    return kinds.Context(
        campaign_id=CID if visibility == "public" else "20261002-p-abcdef",
        image="ghcr.io/studio-fug/yapnr@" + testing.DIGEST,
        visibility=visibility,
        determinism="wall_clock_budgeted",
        resources=dict(dict(cpus=1, memory_gb=3, disk_gb=4, max_wall_s=3600), **resources),
        source_input={"dest": "src", "bundle": "a" * 64, "kind": "source"},
        base_dir=Path(tmp),
        bundle_dir=Path(tmp) / "bundles",
    )


BENCH = {
    "schema": spec.CAMPAIGN_SCHEMA,
    "kind": "bench-cell",
    "matrix": {"rung": ["r1", "r2"], "tool": ["yapnr", "freerouting"], "seed": [0]},
    "tools": {
        "yapnr": {
            "command": ["${PYTHON}", "route.py", "{rung_dir}", "--out", "{out}"],
            "judge": ["${KICAD_PYTHON}", "measure.py", "{out}"],
        },
        "freerouting": {
            "command": ["java", "-jar", "fr.jar", "-mt", "4", "{rung_dir}"],
            "cpus": 4,
            "image": "ghcr.io/studio-fug/yapnr-bench-freerouting@" + testing.DIGEST,
        },
    },
}


class BenchTest(unittest.TestCase):
    def test_expand(self):
        kind = kinds.get("bench-cell")
        self.assertEqual(kind.check(BENCH), [])
        with tempfile.TemporaryDirectory() as tmp:
            tasks = kind.expand(BENCH, context(tmp))
        self.assertEqual(
            [t["id"] for t in tasks],
            [
                "bench/r1/yapnr/s0",
                "bench/r1/freerouting/s0",
                "bench/r2/yapnr/s0",
                "bench/r2/freerouting/s0",
            ],
        )
        yapnr, freerouting = tasks[0], tasks[1]
        script = yapnr["command"][2]
        self.assertIn("route.py bench/r1 --out out/r1/results/yapnr-s0", script)
        self.assertIn("measure.py out/r1/results/yapnr-s0", script)
        self.assertEqual(yapnr["env"]["OMP_NUM_THREADS"], "1")
        self.assertEqual(freerouting["resources"]["cpus"], 4)
        self.assertNotIn("OMP_NUM_THREADS", freerouting["env"])
        self.assertTrue(
            freerouting["image"].startswith("ghcr.io/studio-fug/yapnr-bench-freerouting@")
        )

    def test_unknown_tool_and_options_are_reported(self):
        campaign = dict(
            BENCH, matrix=dict(BENCH["matrix"], tool=["kicad"]), tools={"yapnr": {"cmd": []}}
        )
        errors = kinds.get("bench-cell").check(campaign)
        self.assertTrue(any("no [tools.kicad]" in e for e in errors), errors)
        self.assertTrue(any("tools.yapnr.cmd" in e for e in errors), errors)

    def test_assembly_writes_the_harness_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tasks = kinds.get("bench-cell").expand(BENCH, context(tmp))
            fetched = tmp / "fetched"
            task = tasks[0]
            base = fetched / task["id"].replace("/", "~")
            (base / "summary" / "r1" / "results" / "yapnr-s0").mkdir(parents=True)
            (base / "summary" / "r1" / "results" / "yapnr-s0" / "measure.json").write_text("{}")
            (base / "_DONE").write_text("{}")
            report = kinds.get("bench-cell").assemble({}, tasks, fetched, tmp / "out")
            self.assertEqual(report["tasks"], 1)
            self.assertEqual(len(report["missing"]), 3)
            self.assertTrue(
                (tmp / "out" / "r1" / "results" / "yapnr-s0" / "measure.json").is_file()
            )
            cells = json.loads((tmp / "out" / "cells.json").read_text())["cells"]
            self.assertEqual(cells[0]["tool"], "yapnr")


class MonteCarloTest(unittest.TestCase):
    def test_stage_plan_becomes_evaluation_tasks_with_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "cand-03").mkdir()
            (tmp / "cand-03" / "placement.json").write_text("{}")
            lines = [
                {
                    "id": "rung1/cand-03",
                    "command": ["${PYTHON}", "-m", "pnr.full_iteration", "--out", "out/eval"],
                    "inputs": [{"dest": "candidate", "path": "cand-03"}],
                    "record": "out/eval/record.json",
                    "resources": {"cpus": 2, "memory_gb": 8, "max_wall_s": 7200},
                    "labels": {"rung": "1"},
                },
                {
                    "id": "rung1/cand-04",
                    "command": ["${PYTHON}", "-m", "pnr.full_iteration"],
                    "record": "out/eval/record.json",
                },
            ]
            (tmp / "stage.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
            campaign = {
                "schema": spec.CAMPAIGN_SCHEMA,
                "kind": "mc-eval",
                "config": {"stage_plan": "stage.jsonl"},
            }
            kind = kinds.get("mc-eval")
            self.assertEqual(kind.check(campaign), [])
            ctx = context(
                tmp, visibility="private", cpus=2, memory_gb=8, disk_gb=10, max_wall_s=14400
            )
            tasks = kind.expand(campaign, ctx)
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                self.assertRegex(task["id"], r"^p/[0-9a-f]{16}$")
            first = tasks[0]
            self.assertEqual([i["dest"] for i in first["inputs"]], ["src", "candidate"])
            self.assertEqual(first["inputs"][1]["kind"], "private")
            self.assertTrue(
                (tmp / "bundles" / ("%s.tar.gz" % first["inputs"][1]["bundle"])).is_file()
            )
            self.assertEqual(first["resources"]["max_wall_s"], 7200)
            self.assertEqual(tasks[1]["resources"]["max_wall_s"], 14400)
            self.assertEqual(first["labels"]["candidate"], "rung1/cand-03")
            # Assembly: the records in campaign order, ready for the halving driver's import.
            fetched = tmp / "fetched"
            base = fetched / first["id"].replace("/", "~")
            (base / "summary" / "eval").mkdir(parents=True)
            (base / "summary" / "eval" / "record.json").write_text(json.dumps({"score": 1.5}))
            (base / "_DONE").write_text("{}")
            report = kind.assemble({}, tasks, fetched, tmp / "out")
            dataset = [
                json.loads(x) for x in (tmp / "out" / "dataset.jsonl").read_text().splitlines()
            ]
            self.assertEqual(dataset[0]["candidate"], "rung1/cand-03")
            self.assertEqual(dataset[0]["record"], {"score": 1.5})
            self.assertEqual(report["missing"], [tasks[1]["id"]])

    def test_stage_plan_line_can_be_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lines = [
                {
                    "id": "rf/divider",
                    "command": ["${PYTHON}", "-m", "yapnr.rf.cases", "run", "divider"],
                    "record": "out/divider/validation.json",
                    "checkpoint": {"path": "out/divider"},
                    "prune": ["divider/cache"],
                    "verdict": {"file": "out/divider/validation.json", "json_path": "ok"},
                },
                {
                    "id": "rf/antenna",
                    "command": ["${PYTHON}", "-m", "yapnr.rf.cases", "run", "antenna"],
                    "record": "out/antenna/validation.json",
                    "checkpoint": {"path": "out/antenna", "sync_every_s": 120, "on_signal": False},
                },
                {"id": "plain", "command": ["true"], "record": "out/r.json"},
            ]
            (tmp / "stage.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
            campaign = {
                "schema": spec.CAMPAIGN_SCHEMA,
                "kind": "mc-eval",
                "config": {"stage_plan": "stage.jsonl"},
            }
            first, second, plain = kinds.get("mc-eval").expand(campaign, context(tmp))
        self.assertEqual(first["restart"], "resume")
        self.assertEqual(
            first["checkpoint"], {"path": "out/divider", "sync_every_s": 300, "on_signal": True}
        )
        self.assertEqual(first["outputs"]["prune"], ["divider/cache"])
        self.assertEqual(first["verdict"]["json_path"], "ok")
        self.assertEqual(
            second["checkpoint"], {"path": "out/antenna", "sync_every_s": 120, "on_signal": False}
        )
        self.assertEqual(plain["restart"], "scratch")
        self.assertIsNone(plain["checkpoint"])
        self.assertIsNone(plain["verdict"])

    def test_malformed_stage_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage.jsonl"
            path.write_text(
                json.dumps({"id": "x", "command": [], "record": "elsewhere.json"}) + "\n"
            )
            with self.assertRaises(ValueError):
                mc.read_stage_plan(path)
            for bad in ({"checkpoint": "out/run"}, {"checkpoint": {"path": "out/r", "every": 1}}):
                line = dict({"id": "x", "command": ["true"], "record": "out/r.json"}, **bad)
                path.write_text(json.dumps(line) + "\n")
                with self.assertRaises(ValueError):
                    mc.read_stage_plan(path)


class RfTest(unittest.TestCase):
    def test_rf_runs_are_resumable(self):
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "rf-run",
            "matrix": {"case": ["div", "hybrid"]},
            "config": {"sync_every_s": 120},
        }
        kind = kinds.get("rf-run")
        self.assertEqual(kind.check(campaign), [])
        with tempfile.TemporaryDirectory() as tmp:
            tasks = kind.expand(campaign, context(tmp, cpus=4))
        task = tasks[0]
        self.assertEqual(task["restart"], "resume")
        self.assertEqual(
            task["checkpoint"], {"path": "out/div", "sync_every_s": 120, "on_signal": True}
        )
        self.assertEqual(task["env"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(task["command"][:4], ["${PYTHON}", "-m", "yapnr.rf.cases", "run"])


class LadderOptionsTest(unittest.TestCase):
    def test_the_bundle_carries_the_fab_profile_data(self):
        # run.py FAB_DATA_SOURCES and the profiles: a data profile (jlc-6l-hdi, which the
        # UFBGA-201 rungs declare) resolves from the cell's own checkout, not the image's.
        from yapnr.exp.kinds.ladder import LadderCell

        self.assertEqual(LadderCell.source_paths, ("hardware/pnr", "hardware/tools"))
        self.assertEqual(
            LadderCell.optional_source_paths,
            (
                "yapnr/__init__.py",
                "yapnr/fab/__init__.py",
                "yapnr/fab/capability.py",
                "yapnr/fab/data",
            ),
        )

    def test_compact_shrink_gloss_and_hard_reach_the_runner(self):
        from yapnr.exp.kinds import ladder

        args = ladder.runner_arguments(
            dict(compact=True, compact_off=["RANK"], shrink=True, gloss=True, hard=True)
        )
        for flag in ("--compact", "--shrink", "--gloss", "--hard"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--compact-off") + 1], "RANK")
        self.assertNotIn("--compact", ladder.runner_arguments({}))
        kind = kinds.get("ladder-cell")
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "ladder-cell",
            "matrix": {"case": ["01-connector-led-2"], "seed": [0], "config": ["off", "on"]},
            "configs": {"off": {}, "on": {"compact": True, "compact_off": ["GP"]}},
        }
        self.assertEqual(kind.check(campaign), [])
        campaign["configs"]["bad"] = {"compact_off": ["SPREAD"]}
        campaign["matrix"]["config"].append("bad")
        errors = kind.check(campaign)
        self.assertTrue(any("compact_off names" in e for e in errors), errors)
        self.assertTrue(any("compact_off needs compact" in e for e in errors), errors)

    def test_maze_kernel_reaches_the_runner_only_when_named(self):
        # Without the option the runner's default kernel applies (one place: run.py reads
        # hardware/pnr/pnr/route/detail/kernels.py); either kernel can be named for an A/B.
        from yapnr.exp.kinds import ladder

        self.assertNotIn("--maze-kernel", ladder.runner_arguments({}))
        for kernel in ("packed", "native"):
            args = ladder.runner_arguments(dict(maze_kernel=kernel))
            self.assertEqual(args[args.index("--maze-kernel") + 1], kernel)
        kind = kinds.get("ladder-cell")
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "ladder-cell",
            "matrix": {"case": ["01-connector-led-2"], "seed": [0], "config": ["py", "c"]},
            "configs": {"py": {"maze_kernel": "packed"}, "c": {"maze_kernel": "native"}},
        }
        self.assertEqual(kind.check(campaign), [])
        campaign["configs"]["c"]["maze_kernel"] = "reference"
        errors = kind.check(campaign)
        self.assertTrue(any("maze_kernel is one of" in e for e in errors), errors)

    def test_legalizer_switches_reach_the_runner(self):
        from yapnr.exp.kinds import ladder

        args = ladder.runner_arguments(
            dict(gp_channels=1, legalize_hpwl=4.0, legalize_reorient=True, pool_source_clamp=True)
        )
        self.assertEqual(args[args.index("--gp-channels") + 1], "1.0")
        self.assertEqual(args[args.index("--legalize-hpwl") + 1], "4.0")
        for flag in ("--legalize-reorient", "--pool-source-clamp"):
            self.assertIn(flag, args)
        self.assertNotIn("--gp-polish", args)
        self.assertIn("--gp-polish", ladder.runner_arguments(dict(gp_polish=True)))
        wire = ladder.runner_arguments(dict(legalize_reorient_wire=True))
        self.assertEqual(wire[wire.index("--legalize-reorient") + 1], "wire")
        more = ladder.runner_arguments(
            dict(legalize_channel_clearance_fab=True, line_satellites=True, channel_layers=True)
        )
        self.assertIn("--channel-layers", more)
        self.assertEqual(more[more.index("--legalize-channel-clearance") + 1], "fab")
        self.assertIn("--line-satellites", more)
        kind = kinds.get("ladder-cell")
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "ladder-cell",
            "matrix": {"case": ["01-connector-led-2"], "seed": [0], "config": ["on", "bad"]},
            "configs": {"on": {"legalize_hpwl": 4}, "bad": {"gp_channels": 0}},
        }
        errors = kind.check(campaign)
        self.assertEqual(errors, ["configs.bad.gp_channels is a positive weight"])

    def test_the_rail_allocation_arm_reaches_the_runner(self):
        from yapnr.exp.kinds import ladder

        args = ladder.runner_arguments(dict(rail_alloc="static"))
        self.assertEqual(args[args.index("--rail-alloc") + 1], "static")
        self.assertNotIn("--rail-alloc", ladder.runner_arguments({}))
        kind = kinds.get("ladder-cell")
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "ladder-cell",
            "matrix": {"case": ["01-connector-led-2"], "seed": [0], "config": ["a", "b"]},
            "configs": {"a": {"rail_alloc": "search"}, "b": {"rail_alloc": "rule"}},
        }
        self.assertEqual(kind.check(campaign), ["configs.b.rail_alloc is search or static"])

    def test_compact_parts_match_the_engine(self):
        """The kind's PNR_COMPACT parts are the engine's (hardware/pnr/pnr/compact_flags.py,
        read without importing the engine)."""
        import ast

        from yapnr.exp.kinds import ladder

        source = Path(__file__).resolve().parents[3] / "hardware/pnr/pnr/compact_flags.py"
        tree = ast.parse(source.read_text())
        (parts,) = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "PARTS" for t in node.targets)
        ]
        self.assertEqual(ladder.COMPACT_PARTS, parts)

    def test_route_compact_options(self):
        """route_compact (true or a list of parts), macro_hull and hull_dovetail become the
        runner's --route-compact, --macro-hull and --hull-dovetail; the parts are the
        engine's (hardware/pnr/pnr/place/route_compact.py PARTS, read without importing)."""
        import ast

        from yapnr.exp.kinds import ladder

        self.assertEqual(
            ladder.runner_arguments(dict(route_compact=True))[-2:], ["--route-compact", "1"]
        )
        self.assertEqual(
            ladder.runner_arguments(dict(route_compact="TOP,BLOCK"))[-2:],
            ["--route-compact", "TOP,BLOCK"],
        )
        args = ladder.runner_arguments(dict(macro_hull=True, hull_dovetail=1.5))
        self.assertIn("--macro-hull", args)
        self.assertEqual(args[args.index("--hull-dovetail") + 1], "1.5")
        self.assertNotIn("--route-compact", ladder.runner_arguments({}))
        # An explicit `route_compact = false` (distinct from leaving the key out) opts a
        # hierarchical cell out of the driver's default bundle (run.py --route-compact 0).
        self.assertEqual(
            ladder.runner_arguments(dict(route_compact=False))[-2:], ["--route-compact", "0"]
        )
        # hull_nest (PNR_HULL_NEST) is explicit both ways; leaving it out passes nothing.
        self.assertEqual(ladder.runner_arguments(dict(hull_nest=True))[-2:], ["--hull-nest", "1"])
        self.assertEqual(ladder.runner_arguments(dict(hull_nest=False))[-2:], ["--hull-nest", "0"])
        self.assertNotIn("--hull-nest", ladder.runner_arguments({}))
        kind = kinds.get("ladder-cell")
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "ladder-cell",
            "matrix": {"case": ["01-connector-led-2"], "seed": [0], "config": ["on", "bad"]},
            "configs": {"on": {"route_compact": True}, "bad": {"route_compact": "TOP,EDGE"}},
        }
        self.assertEqual(
            kind.check(campaign),
            ["configs.bad.route_compact is true or a comma list of TOP, BLOCK, FLAT"],
        )
        source = Path(__file__).resolve().parents[3] / "hardware/pnr/pnr/place/route_compact.py"
        tree = ast.parse(source.read_text())
        (parts,) = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "PARTS" for t in node.targets)
        ]
        self.assertEqual(ladder.ROUTE_COMPACT_PARTS, parts)

    def test_case_names_allow_the_hard_rungs_stackup_suffix(self):
        """matrix.case accepts hard_rungs.py's with_stackup names (e.g. "...-6L-SGSGPS"), whose
        layer codes are upper case; CASE_RE used to accept lower case only, so no stackup
        variant of a hard rung -- most of them -- could ever be a campaign's case."""
        kind = kinds.get("ladder-cell")
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "ladder-cell",
            "matrix": {
                "case": ["11-ufbga201-fanout-6L-SGSGPS-rails", "09-mcu-usb-31-4L-SGPS"],
                "seed": [0],
            },
        }
        self.assertEqual(kind.check(campaign), [])
        bad = dict(campaign, matrix=dict(campaign["matrix"], case=["-leading-dash-not-allowed"]))
        self.assertTrue(kind.check(bad))


if __name__ == "__main__":
    unittest.main()
