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

    def test_directory_files_and_read_only_file_boundaries(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            (root / "requirements").mkdir()
            text = root / "requirements/spec.yaml"
            text.write_text("requirements: []\n")
            (root / "binary.bin").write_bytes(b"a\0b")
            (root / "linked.yaml").symlink_to(Path(outside) / "secret.yaml")
            self.assertEqual(
                {f["path"] for f in design.snapshot(root)["files"]},
                {"requirements/spec.yaml", "binary.bin"},
            )
            self.assertEqual(
                design.read_source(root, "requirements/spec.yaml")["text"], text.read_text()
            )
            for name in ["../secret.yaml", "linked.yaml", "binary.bin"]:
                with self.assertRaises((ValueError, OSError)):
                    design.read_source(root, name)

    def test_source_search_returns_exact_lines_and_excludes_dependency_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.ato").write_text("module Main:\n    bypass = new Capacitor\n")
            (root / ".ato").mkdir()
            (root / ".ato/part.ato").write_text("bypass")
            matches = design.search(root, "BYPASS")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["line"], 2)
            self.assertEqual(matches[0]["file"], "main.ato")
            self.assertEqual(design.search(root, " "), [])

    def test_recorded_build_attempts_keep_failures_and_exclude_unrelated_reports(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            for name, code in [("first", 1), ("second", 0)]:
                path = root / "reports" / name / "result.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"atopile": "0.15.8", "returncode": code, "seconds": 2}))
            unrelated = root / "reports" / "result.json"
            unrelated.write_text('{"returncode": 0}')
            external = Path(outside) / "result.json"
            external.write_text('{"atopile": "0.15.8", "returncode": 0}')
            (root / "reports" / "escape").symlink_to(Path(outside), target_is_directory=True)
            runs = design.experiments(root)
            self.assertEqual([r["status"] for r in runs], ["failed", "passed"])
            self.assertEqual(runs[0]["path"], "reports/first/result.json")
            self.assertEqual(len(runs[0]["sha256"]), 64)

    def test_source_edit_is_optimistic_and_excludes_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "main.ato"
            source.write_text("module Main:\n    pass\n")
            sha = design.snapshot(root)["sources"][0]["sha256"]
            saved = design.update(root, "main.ato", "module Main:\n    x = 1\n", sha)
            self.assertNotEqual(saved["sha256"], sha)
            with self.assertRaisesRegex(ValueError, "concurrently"):
                design.update(root, "main.ato", "stale edit", sha)
            with self.assertRaisesRegex(ValueError, "dependency"):
                design.update(root, ".ato/part.ato", "change", sha)
            self.assertEqual(source.read_text(), "module Main:\n    x = 1\n")


if __name__ == "__main__":
    unittest.main()
