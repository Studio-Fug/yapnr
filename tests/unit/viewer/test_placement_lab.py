import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from yapnr.viewer.services.placement_lab import PlacementLab, checked, write
from yapnr.viewer.services.placement_objective import DEFAULT, parse


class Objective(unittest.TestCase):
    def test_linear_expression(self):
        self.assertEqual(parse(DEFAULT)["w_locality"], 4)
        self.assertEqual(
            parse("2*(wirelength + 3*pad_locality) + source_constraints")["w_locality"], 6
        )

    def test_rejects_code_nonfinite_nonlinear_negative_and_removed_source_intent(self):
        for expr in [
            '__import__("os")',
            "wirelength**2",
            "wirelength*outline",
            "-wirelength + source_constraints",
            "1e999*outline + source_constraints",
            "wirelength",
            "source_constraints+1",
            "1001*wirelength+source_constraints",
            "a.b",
            "[x for x in y]",
        ]:
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                parse(expr)

    def test_hash_bound_capture_rejects_tampering_and_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root / "a.json", dict(a=1))
            from yapnr.viewer.services.placement_lab import digest

            item = dict(path=str(root / "a.json"), sha256=digest(root / "a.json"))
            self.assertEqual(checked(root, item), {"a": 1})
            write(root / "a.json", dict(a=2))
            with self.assertRaises(ValueError):
                checked(root, item)
            item["sha256"] = digest(root / "a.json")
            with self.assertRaises(ValueError):
                checked(root / "child", item)

    def test_run_freezes_engine_limits_concurrency_and_records_cancellation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = root / "engine"
            write(engine / "pnr.py", {"original": True})
            lab = PlacementLab(root, engine)
            write(lab.root / "sources/demo.json", {"title": "Demo", "graph": {"components": []}})
            process = MagicMock()
            process.poll.return_value = None
            with patch(
                "yapnr.viewer.services.placement_lab.subprocess.Popen", return_value=process
            ), patch("yapnr.viewer.services.placement_lab.threading.Thread"):
                result = lab.command({"action": "start", "source": "demo", "iterations": 20})
            folder = lab.folder(result["id"])
            before = (folder / "engine/pnr.py").read_bytes()
            write(engine / "pnr.py", {"original": False})
            self.assertEqual((folder / "engine/pnr.py").read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "One local placement run"):
                lab.command({"action": "start", "source": "demo"})
            lab.command({"action": "pause", "id": result["id"]})
            self.assertTrue(json.loads((folder / "control.json").read_text())["paused"])
            lab.command(
                {
                    "action": "apply",
                    "id": result["id"],
                    "expression": "2*wirelength + source_constraints",
                }
            )
            lab.command({"action": "stop", "id": result["id"]})
            process.terminate.assert_called_once()
            process.wait.return_value = -15
            lab._wait(process, folder)
            self.assertEqual(json.loads((folder / "status.json").read_text())["state"], "stopped")
            from yapnr.experiments import load

            self.assertEqual(load(root)["attempts"][0]["status"], "cancelled")

    def test_missing_worker_is_interrupted_not_running_forever(self):
        with tempfile.TemporaryDirectory() as td:
            lab = PlacementLab(td, td)
            folder = lab.folder("trial")
            write(folder / "status.json", dict(state="running"))
            write(folder / "request.json", {})
            self.assertEqual(lab.get("trial")["status"]["state"], "interrupted")
            self.assertEqual(lab.catalog()["runs"][0]["state"], "interrupted")
            with self.assertRaises(ValueError):
                lab.folder("../escape")


if __name__ == "__main__":
    unittest.main()
