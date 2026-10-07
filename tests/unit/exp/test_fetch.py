"""Fetch and assembly: ladder shards become the run directory one run.py would have written, and
private campaigns stay below the private results root."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from tools.ci import ladder_summary
from yapnr.exp import fetch, kinds, testing
from yapnr.exp.store import LocalStore

CID = "20261002-ladder-abcdef"


def ladder_task(case, seed):
    return {
        "id": "ladder/%s/s%d" % (case, seed),
        "kind": "ladder-cell",
        "labels": {"case": case, "seed": str(seed), "config": "default"},
        "outputs": {"root": "out", "summary": [], "prune": []},
        "done": {"file": "out/run/summary.json", "json": {"complete": True}},
    }


def write_shard(store, task, passed=True, platform="linux-x86_64", attempt="s1r0"):
    """What the wrapper leaves in the store for one ladder cell."""
    case, seed = task["labels"]["case"], int(task["labels"]["seed"])
    key = task["id"].replace("/", "~")
    base = Path(store) / "campaigns" / CID / "tasks" / key
    summary = base / attempt / "summary" / "run"
    directory = "%s-seed-%d" % (case, seed)
    (summary / directory).mkdir(parents=True)
    result = {
        "case": case,
        "seed": seed,
        "passed": passed,
        "reasons": [] if passed else ["native_drc_violations"],
        "directory": directory,
        "elapsed_seconds": 12.5,
        "opens": 0,
        "violations": {} if passed else {"clearance": 1},
        "vias": 2,
        "copper_length_mm": 10.0,
        "stages": {},
    }
    (summary / directory / "result.json").write_text(json.dumps(result))
    (summary / "summary.json").write_text(
        json.dumps(
            {
                "passed": passed,
                "complete": True,
                "source_changed_during_run": [],
                "results": [result],
            }
        )
    )
    provenance = {
        "schema": "pnr-regression-v1",
        "sources_sha256": "s" * 64,
        "engine_revision": "e" * 40,
        "platform": platform,
        "fab_profile": "legacy",
        "seeds": [seed],
        "arguments": {"case": [case], "seed": [seed], "out": "out/run", "repo": "src"},
    }
    (summary / "provenance.json").write_text(json.dumps(provenance))
    (summary / "python-version.txt").write_text("numpy==1.26.4\n")
    record = {
        "task": task["id"],
        "kind": task["kind"],
        "labels": task["labels"],
        "attempt": attempt,
        "backend": "gcp-batch",
        "wall_s": 20.0,
        "verdict": "pass" if passed else "fail",
        "machine": {"machine_type": "c4d-highcpu-16", "platform": platform},
        "image": {"ref": "img"},
    }
    (base / attempt / "record.json").write_text(json.dumps(record))
    marker = {"task": task["id"], "attempt": attempt, "verdict": record["verdict"]}
    (base / "_DONE").write_text(json.dumps(marker) + "\n")


class FetchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp, gcp=False)
        self.store = self.tmp / "store"
        self.tasks = [
            ladder_task(c, s) for c in ("01-connector-led-2", "02-resistor-led-3") for s in (0, 1)
        ]
        base = self.store / "campaigns" / CID
        base.mkdir(parents=True)
        meta = {"id": CID, "kind": "ladder-cell", "visibility": "public", "image": {"ref": "img"}}
        (base / "campaign.json").write_text(json.dumps(meta))
        (base / "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in self.tasks))

    def tearDown(self):
        self._tmp.cleanup()

    def summary_check(self, run):
        with contextlib.redirect_stdout(io.StringIO()):
            return ladder_summary.main([str(run), "--check"])

    def test_assembled_ladder_reads_like_one_run(self):
        for task in self.tasks:
            write_shard(self.store, task)
        report = fetch.fetch(LocalStore(self.store), CID, self.tmp / "out", self.config)
        self.assertEqual(report["done"], 4)
        run = self.tmp / "out" / CID / "assembled" / "default"
        summary = json.loads((run / "summary.json").read_text())
        self.assertTrue(summary["complete"])
        self.assertTrue(summary["passed"])
        self.assertEqual(
            [(r["case"], r["seed"]) for r in summary["results"]],
            [
                ("01-connector-led-2", 0),
                ("01-connector-led-2", 1),
                ("02-resistor-led-3", 0),
                ("02-resistor-led-3", 1),
            ],
        )
        for result in summary["results"]:
            self.assertTrue((run / result["directory"] / "result.json").is_file())
        provenance = json.loads((run / "provenance.json").read_text())
        self.assertEqual(provenance["seeds"], [0, 1])
        self.assertEqual(
            provenance["arguments"]["case"], ["01-connector-led-2", "02-resistor-led-3"]
        )
        self.assertNotIn("out", provenance["arguments"])
        self.assertEqual(len(provenance["shards"]), 4)
        self.assertEqual(provenance["shards"][0]["machine_type"], "c4d-highcpu-16")
        suite = ET.parse(run / "junit.xml").getroot()
        self.assertEqual(suite.get("tests"), "4")
        self.assertEqual(suite.get("failures"), "0")
        self.assertEqual(self.summary_check(run), 0)

    def test_the_fetch_command_feeds_the_durations_history(self):
        from yapnr.exp import cli, packing
        from yapnr.exp import plan as planning

        for task in self.tasks:
            write_shard(self.store, task)
        plan = self.tmp / "plan"
        plan.mkdir()
        meta = {
            "schema": planning.PLAN_SCHEMA,
            "id": CID,
            "kind": "ladder-cell",
            "visibility": "public",
            "classes": [],
            "backend": {"name": "gcp-batch"},
        }
        (plan / "campaign.json").write_text(json.dumps(meta))
        (plan / "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in self.tasks))
        config = testing.write_config(self.tmp)
        args = ["--config", str(config), "fetch", str(plan), "--from", str(self.store)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args + ["--into", str(self.tmp / "f1")]), 0)
        history = packing.load(str(self.tmp / "store" / "durations.json"))
        cells = history["cells"]["ladder-cell"]
        self.assertEqual(len(cells), 2)  # two cases; seeds share a cell
        self.assertEqual(len(history["seen"]), 4)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args + ["--into", str(self.tmp / "f2")]), 0)
        self.assertEqual(len(packing.load(str(self.tmp / "store" / "durations.json"))["seen"]), 4)

    def test_a_failed_cell_fails_the_assembled_run(self):
        for n, task in enumerate(self.tasks):
            write_shard(self.store, task, passed=n != 2)
        fetch.fetch(LocalStore(self.store), CID, self.tmp / "out", self.config)
        run = self.tmp / "out" / CID / "assembled" / "default"
        self.assertFalse(json.loads((run / "summary.json").read_text())["passed"])
        self.assertEqual(self.summary_check(run), 1)

    def test_missing_cells_leave_the_run_incomplete(self):
        for task in self.tasks[:3]:
            write_shard(self.store, task)
        report = fetch.fetch(LocalStore(self.store), CID, self.tmp / "out", self.config)
        self.assertEqual(report["missing"], [self.tasks[3]["id"]])
        run = self.tmp / "out" / CID / "assembled" / "default"
        self.assertFalse(json.loads((run / "summary.json").read_text())["complete"])

    def test_mixed_platforms_need_allow_mixed(self):
        for n, task in enumerate(self.tasks):
            write_shard(self.store, task, platform="linux-x86_64" if n else "linux-aarch64")
        with self.assertRaises(ValueError):
            fetch.fetch(LocalStore(self.store), CID, self.tmp / "out", self.config)
        report = fetch.fetch(
            LocalStore(self.store), CID, self.tmp / "out2", self.config, allow_mixed=True
        )
        self.assertIn("platform", report["assembled"]["warnings"][0])

    def test_full_fetch_extracts_the_result(self):
        import tarfile

        write_shard(self.store, self.tasks[0])
        attempt = self.store / "campaigns" / CID / "tasks" / "ladder~01-connector-led-2~s0" / "s1r0"
        payload = self.tmp / "payload" / "out" / "run"
        payload.mkdir(parents=True)
        (payload / "big.kicad_pcb").write_text("board")
        with tarfile.open(attempt / "result.tar.gz", "w:gz") as tar:
            tar.add(str(self.tmp / "payload" / "out"), arcname="out")
        fetch.fetch(
            LocalStore(self.store), CID, self.tmp / "summary-only", self.config, assemble=False
        )
        self.assertFalse(list((self.tmp / "summary-only").rglob("result.tar.gz")))
        fetch.fetch(
            LocalStore(self.store), CID, self.tmp / "full", self.config, full=True, assemble=False
        )
        extracted = (
            self.tmp
            / "full"
            / CID
            / "tasks"
            / "ladder~01-connector-led-2~s0"
            / "result"
            / "out"
            / "run"
        )
        self.assertEqual((extracted / "big.kicad_pcb").read_text(), "board")

    def test_private_campaign_stays_below_the_private_root(self):
        meta_path = self.store / "campaigns" / CID / "campaign.json"
        meta = json.loads(meta_path.read_text())
        meta["visibility"] = "private"
        meta_path.write_text(json.dumps(meta))
        with self.assertRaises(fetch.FetchError):
            fetch.fetch(LocalStore(self.store), CID, self.tmp / "elsewhere", self.config)
        fetch.fetch(LocalStore(self.store), CID, self.tmp / "private" / "fetched", self.config)

    def test_public_checkout_is_detected(self):
        repo = testing.fixture_repo(self.tmp)
        testing.git(repo, "remote", "add", "origin", "https://github.com/Studio-Fug/yapnr.git")
        found = fetch.inside_public_checkout(repo / "results", self.config.local.public_remotes)
        self.assertIn("github.com", found)
        self.assertIsNone(fetch.inside_public_checkout(self.tmp, self.config.local.public_remotes))

    def test_kind_registry(self):
        self.assertEqual(
            sorted(kinds.KINDS),
            ["bench-cell", "ladder-cell", "mc-eval", "mc-place", "rf-run", "smoke"],
        )


if __name__ == "__main__":
    unittest.main()
