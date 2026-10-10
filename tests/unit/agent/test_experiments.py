"""Typed attempts retain failed runs, captured artifacts and content provenance."""

import json
import tempfile
import unittest
from pathlib import Path

from yapnr import experiments


class ExperimentTest(unittest.TestCase):
    def test_recorded_pipeline_snapshots_inputs_outputs_and_links_exact_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.ato").write_text("module Main:\n    pass\n")
            build = experiments.begin(root, "schematic", "Build", ["main.ato"])
            (root / "board.kicad_pcb").write_text("synthetic board")
            completed = experiments.finish(root, build["id"], "passed", ["board.kicad_pcb"])
            route = experiments.begin(root, "pnr", "Route", ["board.kicad_pcb"], {"seed": 7})
            experiments.finish(root, route["id"], "failed")
            runs = experiments.attempts(root)
            self.assertEqual(runs[1]["dependencies"][0]["attempt"], build["id"])
            self.assertEqual(runs[1]["status"], "failed")
            self.assertEqual(runs[1]["parameters"]["seed"], 7)
            artifact = completed["outputs"][0]
            self.assertEqual((root / artifact["object_path"]).read_text(), "synthetic board")
            (root / "board.kicad_pcb").write_text("later edit")
            self.assertEqual((root / artifact["object_path"]).read_text(), "synthetic board")
            self.assertEqual(
                (root / build["inputs"][0]["object_path"]).read_text(), "module Main:\n    pass\n"
            )
            self.assertEqual(
                len((root / ".yapnr/workspace/conversation.jsonl").read_text().splitlines()), 4
            )
            with self.assertRaises(ValueError):
                experiments.finish(root, route["id"], "passed")

    def test_legacy_schematic_to_pnr_provenance_requires_matching_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "experiments/schematic"
            build.mkdir(parents=True)
            pcb = build / "board.kicad_pcb"
            pcb.write_text("synthetic")
            sha = experiments.digest(pcb.read_bytes())
            (build / "result.json").write_text(
                json.dumps(
                    {
                        "atopile": "0.15.8",
                        "returncode": 0,
                        "outputs": {"pcb": pcb.name},
                        "sha256": {"pcb": sha},
                    }
                )
            )
            route = root / "experiments/pnr"
            route.mkdir()
            (route / "source.kicad_pcb").write_bytes(pcb.read_bytes())
            (route / "experiment.json").write_text(
                json.dumps(
                    {
                        "stage": "EXPERIMENT",
                        "input_sha256": {"experiments/pnr/source.kicad_pcb": sha},
                        "budget": {"seed": 0},
                    }
                )
            )
            (route / "worker-result.json").write_text('{"returncode":1}')
            runs = experiments.attempts(root)
            pnr = next(run for run in runs if run["kind"] == "pnr")
            self.assertEqual(pnr["dependencies"][0]["attempt"], "experiments/schematic")
            (route / "source.kicad_pcb").write_text("changed source")
            pnr = next(run for run in experiments.attempts(root) if run["kind"] == "pnr")
            self.assertEqual(pnr["dependencies"], [])
            self.assertEqual(pnr["inputs"][0]["verification"], "changed")

    def test_identical_later_build_is_not_provenance_for_an_earlier_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "board.kicad_pcb").write_text("same bytes")
            first = experiments.begin(root, "schematic", "First build")
            produced = experiments.finish(root, first["id"], "passed", ["board.kicad_pcb"])
            route = experiments.begin(root, "pnr", "Route", ["board.kicad_pcb"])
            later = experiments.begin(root, "schematic", "Later build")
            experiments.finish(root, later["id"], "passed", ["board.kicad_pcb"])
            experiments.finish(root, route["id"], "passed")
            run = next(run for run in experiments.attempts(root) if run["id"] == route["id"])
            self.assertEqual([link["attempt"] for link in run["dependencies"]], [first["id"]])
            self.assertEqual(run["inputs"][0]["artifact"], produced["outputs"][0]["artifact"])

    def test_growing_registry_preserves_records_beyond_report_read_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / experiments.REGISTRY
            path.parent.mkdir(parents=True)
            path.write_bytes(
                experiments.encoded(
                    {"schema": experiments.SCHEMA, "attempts": [], "retained": "x" * (1024 * 1024)}
                )
            )
            item = experiments.begin(root, "validation", "Validation")
            self.assertEqual(item["id"], "E000001")
            self.assertEqual(len(experiments.load(root)["retained"]), 1024 * 1024)

    def test_live_lanes_are_typed_without_inventing_provenance(self):
        runs = experiments.live_attempts(
            {
                "lanes": {
                    "pair": {"progress": {"state": "done", "fraction": 1}},
                    "pair/search": {"progress": {"state": "running"}},
                }
            }
        )
        self.assertEqual(runs[0]["status"], "finished")
        self.assertEqual(runs[1]["parent"], "route:pair")
        self.assertEqual(runs[1]["kind"], "discovery")
        self.assertEqual(runs[0]["inputs"], [])
        self.assertEqual(runs[0]["dependencies"], [])
        self.assertEqual(runs[0]["progress"]["fraction"], 1)

    def test_missing_telemetry_ancestors_keep_parallel_candidates_and_progress(self):
        runs = experiments.live_attempts(
            {
                "lanes": {
                    "campaign/s0/start-00": {
                        "progress": {"state": "running", "fraction": 0.25, "phase_label": "Routing"}
                    },
                    "campaign/s0/start-01": {"progress": {"state": "done", "fraction": 1}},
                }
            }
        )
        indexed = {run["id"]: run for run in runs}
        self.assertEqual(indexed["route:campaign/s0/start-00"]["parent"], "route:campaign/s0")
        self.assertEqual(indexed["route:campaign/s0"]["counts"], {"running": 1, "done": 1})
        self.assertEqual(indexed["route:campaign/s0"]["progress"]["fraction"], 0.625)
        self.assertEqual(indexed["route:campaign/s0"]["parent"], "route:campaign")

    def test_completed_schematic_capture_is_not_a_stalled_routing_worker(self):
        runs = experiments.live_attempts(
            {
                "lanes": {
                    "schematic/build": {
                        "kind": "schematic_capture",
                        "progress": {"state": "stalled", "fraction": 0.5},
                    }
                }
            }
        )
        self.assertTrue(all(run["kind"] == "schematic" for run in runs))
        self.assertTrue(all(run["progress"]["fraction"] == 1 for run in runs))

    def test_corrupt_registry_and_escaping_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = Path(directory)
            outside = Path(other) / "file.json"
            outside.write_text("{}")
            (root / "escape.json").symlink_to(outside)
            with self.assertRaises(ValueError):
                experiments.begin(root, "simulation", "Sim", ["escape.json"])
            path = root / experiments.REGISTRY
            path.parent.mkdir(parents=True)
            path.write_text("{bad")
            with self.assertRaises(ValueError):
                experiments.begin(root, "simulation", "Sim")


if __name__ == "__main__":
    unittest.main()
