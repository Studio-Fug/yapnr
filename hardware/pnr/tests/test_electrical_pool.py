import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from pnr.electrical_pool import candidate_placements, choose_completed, validate_evaluation


class ElectricalPoolTests(unittest.TestCase):
    def test_pool_keeps_signal_loser_and_last_source_round(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "placed.json").write_text('{"placement":3}')
            pool = p / "initial-pool"
            pool.mkdir()
            (pool / "report.json").write_text(
                json.dumps(
                    dict(selected="start-06", route_finalists=["start-02", "start-05", "start-06"])
                )
            )
            for i, n in enumerate(["start-02", "start-05", "start-06"]):
                (pool / n).mkdir()
                (pool / n / "placed.json").write_text(json.dumps(dict(placement=i)))
            self.assertEqual(
                [n for n, _ in candidate_placements(p)],
                ["source-selected", "start-02", "start-05", "start-06"],
            )

    def test_identical_graphs_deduplicated_and_unsafe_names_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "placed.json").write_text('{"b":2,"a":1}')
            pool = p / "initial-pool"
            (pool / "start-02").mkdir(parents=True)
            (pool / "start-02/placed.json").write_text('{"a":1,"b":2}')
            report = pool / "report.json"
            report.write_text('{"route_finalists":["start-02"]}')
            self.assertEqual(len(candidate_placements(p)), 1)
            report.write_text('{"route_finalists":["../../injected"]}')
            with self.assertRaises(ValueError):
                candidate_placements(p)

    def test_native_electrical_score_overrules_signal_screening(self):
        results = {
            "signal-winner": dict(completed=True, validated=True, objective=[0, 0, 0, 172, 1, 69]),
            "signal-loser": dict(completed=True, validated=True, objective=[0, 0, 0, 160, 0, 57]),
            "provisional-zero": dict(
                completed=False, validated=False, objective=[0, 0, 0, 0, 0, 0]
            ),
        }
        self.assertEqual(choose_completed(results)[0], "signal-loser")
        with self.assertRaises(RuntimeError):
            choose_completed({"bad": results["provisional-zero"]})

    def fixture(self, p):
        for n in range(9):
            (p / "phases" / f"{n:02d}-test").mkdir(parents=True)
        f = p / "phases/09-final-audit"
        f.mkdir()
        (p / "electrical").mkdir()
        board = f / "diagnostic.kicad_pcb"
        board.write_bytes(b"saved-board")
        h = hashlib.sha256(board.read_bytes()).hexdigest()
        meta = dict(sha256=h, opens=7, violations=0)
        (f / "phase.json").write_text(json.dumps(meta))
        (f / "diagnostic.drc.json").write_text(
            json.dumps(dict(violations=[], unconnected_items=[{}] * 7))
        )
        entries = dict(blocked=[])
        audit = dict(
            qualified=False,
            subwidth_track_count=3,
            pairs=[dict(length_match_qualified=False)],
            reference_failures=[],
        )
        (p / "electrical/pad-entry.json").write_text(json.dumps(entries))
        (p / "electrical/audit.json").write_text(json.dumps(audit))
        result = dict(
            all_phases_completed=True,
            score_scope="post-electrical-final-refill",
            final=meta,
            objective=[0, 0, 0, 3, 1, 7],
        )
        (p / "evaluation.json").write_text(json.dumps(result))
        return result

    def test_counts_hashes_phases_and_unknown_qualification(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            result = self.fixture(p)
            checked = validate_evaluation(p)
            self.assertFalse(checked["qualified"])
            self.assertEqual(checked["objective"][-1], 7)
            wrong = copy.deepcopy(result)
            wrong["objective"][-1] = 0
            (p / "evaluation.json").write_text(json.dumps(wrong))
            with self.assertRaisesRegex(ValueError, "counts"):
                validate_evaluation(p)
            (p / "evaluation.json").write_text(json.dumps(result))
            (p / "phases/03-test").rmdir()
            with self.assertRaisesRegex(ValueError, "missing"):
                validate_evaluation(p)
            (p / "phases/03-test").mkdir()
            (p / "phases/09-final-audit/diagnostic.kicad_pcb").write_text("changed")
            with self.assertRaisesRegex(ValueError, "hash"):
                validate_evaluation(p)

    def test_signal_only_or_failed_guard_never_selected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            result = self.fixture(p)
            result["all_phases_completed"] = False
            (p / "evaluation.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "finish"):
                validate_evaluation(p)
            result["all_phases_completed"] = True
            result["guard_valid"] = False
            (p / "evaluation.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "guards"):
                validate_evaluation(p)


class ElectricalPoolControllerTest(unittest.TestCase):
    def test_parallel_pool_runs_all_candidates_and_preserves_source(self):
        from unittest.mock import patch
        from pnr.electrical_pool import main
        from types import SimpleNamespace
        import os, threading, time

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            diag = root / "diagnostics"
            diag.mkdir()
            out = root / "run"
            for name, value in [
                ("source.kicad_pcb", "source"),
                ("source.kicad_pro", "project"),
                ("rules.json", "{}"),
                ("fp-lib-table", "table"),
                ("placed.json", '{"placement":0}'),
            ]:
                (diag / name).write_text(value)
            pool = diag / "initial-pool"
            (pool / "start-01").mkdir(parents=True)
            (pool / "report.json").write_text('{"route_finalists":["start-01"]}')
            (pool / "start-01/placed.json").write_text('{"placement":1}')
            for n in ["constraints.yaml", "plane.json", "electrical.json", "source.ato"]:
                (root / n).write_text("{}")
            original = {
                p: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in diag.rglob("*")
                if p.is_file()
            }
            active = [0, 0]
            lock = threading.Lock()
            seen = []

            def execute(cmd, **kwargs):
                folder = Path(cmd[3])
                env = kwargs["env"]
                with lock:
                    active[0] += 1
                    active[1] = max(active)
                    seen.append((folder.name, cmd, env))
                time.sleep(0.03)
                result = ElectricalPoolTests().fixture(folder)
                (folder / "phases/09-final-audit/diagnostic.kicad_pro").write_text("{}")
                (folder / "phases/09-final-audit/fp-lib-table").write_text("table")
                (folder / "electrical/native-loop").mkdir()
                (folder / "electrical/native-loop/progress.json").write_text(
                    '{"opens":8,"termination":"time_budget"}'
                )
                (folder / "evaluated-rules.json").write_text("{}")
                (folder / "evaluated-placed.json").write_text("{}")
                (folder / "electrical/plane-access.json").write_text(
                    json.dumps(dict(candidate=folder.name))
                )
                with lock:
                    active[0] -= 1
                return SimpleNamespace(returncode=0)

            args = [
                "--diagnostics",
                str(diag),
                "--out-dir",
                str(out),
                "--constraints",
                str(root / "constraints.yaml"),
                "--electrical-fab",
                str(root / "electrical.json"),
                "--plane-fab",
                str(root / "plane.json"),
                "--annotation-source",
                str(root / "source.ato"),
                "--seconds",
                "123",
                "--workers",
                "2",
                "--route-workers",
                "4",
                "--kicad-python",
                "ki-python",
                "--kicad-cli",
                "ki-cli",
            ]
            bounds = []

            def bounded(cmd, timeout=None, session=True, **kwargs):
                bounds.append((timeout, session))
                return execute(cmd, **kwargs).returncode, False

            with patch("pnr.proc.run_status", bounded), patch("pnr.live.emit"), patch.dict(
                os.environ, {"PNR_EVALUATION_TIMEOUT": ""}
            ):
                selected = main(args)
            self.assertEqual(
                bounds, [(172800.0, False)] * 2
            )  # PNR_EVALUATION_TIMEOUT default, caller's process group
            progress = json.loads((out / "progress.json").read_text())
            self.assertEqual(progress["opens"], 7)
            self.assertEqual(progress["native_pre_cleanup_opens"], 8)
            self.assertEqual(progress["best"], str(selected))
            self.assertEqual(
                progress["best_sha256"], hashlib.sha256(selected.read_bytes()).hexdigest()
            )
            self.assertTrue(selected.exists())
            self.assertEqual(active[1], 2)
            self.assertEqual(len(seen), 2)
            self.assertEqual(
                original, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in original}
            )
            for name, cmd, env in seen:
                self.assertEqual(cmd[cmd.index("--seconds") + 1], "123")
                self.assertEqual(env["PNR_SINGLE_TRACK_WORKERS"], "4")
                self.assertEqual(env["PNR_CANDIDATE_WORKERS"], "2")
            termination = json.loads((out / "termination.json").read_text())
            self.assertFalse(termination["plateau_observed"])
            self.assertFalse(termination["qualified"])
            self.assertEqual(termination["reason"], "full_electrical_finalist_budget_completed")

            self.assertEqual(
                json.loads((out / "selected-plane-access.json").read_text())["candidate"],
                termination["selected"],
            )
            self.assertIn(
                str((diag / "source.kicad_pro").resolve()),
                json.loads((out / "input-hashes.json").read_text()),
            )


if __name__ == "__main__":
    unittest.main()
