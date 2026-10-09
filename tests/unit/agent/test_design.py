"""Live capture reflects source edits without presenting incomplete netlist writes."""

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.agent import design


class DesignTest(unittest.TestCase):
    def test_live_sources_complete_exports_and_explicit_graph_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "main.ato"
            source.write_text("module Main:\n    pass\n")
            graph = root / "graph.json"
            graph.write_text(json.dumps({"components": [], "nets": []}))
            first = design.snapshot(root)
            self.assertEqual(first["status"], "ready")
            source.write_text("module Main:\n    led = new LED\n")
            self.assertNotEqual(
                first["sources"][0]["sha256"], design.snapshot(root)["sources"][0]["sha256"]
            )
            graph.write_text('{"components":')
            self.assertEqual(design.snapshot(root)["status"], "waiting")
            graph.write_text(json.dumps({"components": [], "nets": []}))
            build = root / "build"
            build.mkdir()
            (build / "graph.json").write_text(graph.read_text())
            self.assertIsNone(design.snapshot(root)["schematic"])
            config = root / ".yapnr/workspace/design.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"graph": "build/graph.json"}))
            self.assertEqual(design.snapshot(root)["schematic"]["path"], "build/graph.json")

    def test_dependency_sources_and_symlink_escapes_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as external:
            root = Path(folder)
            secret = Path(external) / "outside.ato"
            secret.write_text("outside")
            (root / "escape.ato").symlink_to(secret)
            dependency = root / ".ato"
            dependency.mkdir()
            (dependency / "part.ato").write_text("dependency")
            self.assertEqual(design.snapshot(root)["sources"], [])


if __name__ == "__main__":
    unittest.main()
