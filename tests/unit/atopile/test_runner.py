"""The runner with a fake atopile: isolation, parts from the cache, environment, outputs, ids,
deadlines. The real atopile runs in tests/e2e/atopile."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from yapnr.frontends.atopile import parts, runner, testing
from yapnr.partcache import importer
from yapnr.partcache.client import LocalPartCache

# A stand-in for atopile's interpreter: answers the version probe and fakes `-m atopile build`
# from the instructions in the project's fake.json.
FAKE_PYTHON = r"""#!{python}
import json, os, subprocess, sys, time, urllib.request, uuid
from pathlib import Path

args = sys.argv[1:]
if "-c" in args and "importlib.metadata" in args[args.index("-c") + 1]:
    print("0.15.8")
    sys.exit(0)
assert args[:3] == ["-m", "atopile", "build"], args
build = args[args.index("-b") + 1]
plan = json.loads(Path("fake.json").read_text()) if Path("fake.json").exists() else {{}}
Path("seen-env.json").write_text(json.dumps(dict(os.environ)))
Path("seen-files.json").write_text(
    json.dumps(sorted(str(p) for p in Path(".").rglob("*") if p.is_file()))
)
Path("seen-args.json").write_text(json.dumps(args))
if plan.get("query"):
    request = urllib.request.Request(
        os.environ["ATO_SERVICES_COMPONENTS_URL"] + "/v0/query",
        data=json.dumps({{"queries": plan["query"]}}).encode(),
        headers={{"Content-Type": "application/json"}},
    )
    Path("answer.json").write_text(urllib.request.urlopen(request, timeout=10).read().decode())
if plan.get("hang"):
    child = subprocess.Popen(["sleep", "300"])
    Path(plan["hang"]).write_text(str(child.pid))
    time.sleep(300)
if plan.get("leave"):  # a child left running in the process group after a normal exit
    child = subprocess.Popen(["sleep", "300"])
    Path(plan["leave"]).write_text(str(child.pid))
board_path = Path(plan.get("layout") or f"elec/layout/{{build}}/{{build}}.kicad_pcb")
board_path.parent.mkdir(parents=True, exist_ok=True)
board = '(kicad_pcb\n\t(version 20241229)\n\t(footprint "X:Y"\n\t\t(at 1 2)\n\t\t(uuid "%s")\n\t)\n)\n'
board_path.write_text(board % uuid.uuid4())
out = Path("build/builds") / build
out.mkdir(parents=True, exist_ok=True)
(out / f"{{build}}.bom.csv").write_text("Designator,LCSC\nR1,C990000001\n")
(out / f"{{build}}.variables.ato.json").write_text("{{}}")
Path("build/manifest.json").write_text(json.dumps({{"path": str(Path.cwd())}}))
sys.exit(plan.get("exit", 0))
"""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        fake = root / "fake/bin/python"
        fake.parent.mkdir(parents=True)
        fake.write_text(FAKE_PYTHON.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        cli = root / "fake/bin/kicad-cli"
        cli.write_text("#!/bin/sh\necho 10.0.0\n")
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        footprints = root / "footprints/Resistor_SMD.pretty"
        footprints.mkdir(parents=True)
        self.env = {
            "YAPNR_ATO_PYTHON": str(fake),
            "YAPNR_KICAD_FOOTPRINTS": str(footprints.parent),
            "ANTHROPIC_API_KEY": "must-not-leak",
        }
        self.cli = cli
        self.cache = LocalPartCache(root / "cache", create=True)
        source = root / "source-parts"
        testing.write_part(source)
        importer.import_part_dirs(self.cache, importer.part_dirs_under(source), "unit test")
        self.cache.put_catalog(testing.catalog_entry(), {"source": "unit test"})
        self.project = testing.write_project(root / "project")
        doc = {
            "schema": parts.LOCK_SCHEMA,
            "parts_dir": "elec/src/parts",
            "parts": [{"name": testing.SYNTHETIC_PART, "id": self.cache.find()[0]["id"]}],
        }
        (self.project / parts.LOCK_NAME).write_text(parts.dump(doc))
        self.root = root

    def build(self, **kwargs):
        options = runner.BuildOptions(
            project=self.project,
            out=self.root / "out",
            cache=self.cache.location,
            kicad_cli=str(self.cli),
            timeout=60,
            work_root=self.root / "work",  # inside the test's temporary directory
            **kwargs,
        )
        with mock.patch.dict(os.environ, self.env):
            return runner.build(options, log=lambda _text: None)

    def test_authoring_captures_parts_for_a_self_contained_offline_build(self):
        authored = self.build(offline=False)
        self.assertTrue(authored.ok, authored.summary)
        self.assertEqual(authored.summary["part_capture_errors"], [])
        self.assertTrue((self.project / ".yapnr/parts/cache").is_dir())
        self.assertTrue((self.project / "elec/src/parts" / testing.SYNTHETIC_PART).is_dir())
        options = runner.BuildOptions(
            project=self.project,
            out=self.root / "replayed",
            cache=str(self.root / "unavailable-operator-cache"),
            kicad_cli=str(self.cli),
            timeout=60,
            work_root=self.root / "work",
            offline=True,
        )
        with mock.patch.dict(os.environ, self.env):
            replayed = runner.build(options, log=lambda _text: None)
        self.assertTrue(replayed.ok, replayed.summary)
        self.assertEqual(replayed.summary["parts"], authored.summary["parts"])
        self.assertEqual(replayed.input_id, authored.input_id)

    def test_build_copies_named_outputs_only(self):
        result = self.build(keep_work=True)
        self.assertTrue(result.ok)
        self.assertEqual(sorted(result.outputs), ["bom_csv", "pcb", "variables"], result.summary)
        self.assertFalse((result.out / "manifest.json").exists())
        summary = json.loads((result.out / "result.json").read_text())
        self.assertEqual(summary["input_id"], result.input_id)
        self.assertEqual(summary["parts"], [testing.SYNTHETIC_PART])
        self.assertEqual(summary["kicad_cli"], "10.0.0")
        # The source tree is untouched; the parts came from the cache into the copy.
        self.assertFalse((self.project / "elec/src/parts").exists())
        self.assertFalse((self.project / "build").exists())
        copy = result.work / "project"
        self.assertTrue((copy / "elec/src/parts" / testing.SYNTHETIC_PART).is_dir())

    def test_environment_is_the_allowlist(self):
        result = self.build(keep_work=True)
        seen = json.loads((result.work / "project/seen-env.json").read_text())
        self.assertNotIn("ANTHROPIC_API_KEY", seen)
        self.assertNotIn("YAPNR_ATO_PYTHON", seen)
        self.assertEqual(seen["YAPNR_ATO_HOOK"], "1")
        self.assertEqual(seen["ATO_SERVICES_COMPONENTS_URL"], seen["YAPNR_ATO_PICKER_URL"])
        self.assertTrue(seen["HOME"].startswith(str(result.work)))
        args = json.loads((result.work / "project/seen-args.json").read_text())
        for target in runner.DEFAULT_TARGETS:
            self.assertIn(target, args)
        for excluded in runner.ALWAYS_EXCLUDED:
            self.assertIn(excluded, args)

    def test_two_builds_have_the_same_input_id(self):
        first = self.build().input_id
        second = self.build().input_id
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_picker_answers_from_the_locked_parts_catalog(self):
        (self.project / "fake.json").write_text(
            json.dumps({"query": [{"lcsc": 990000001, "quantity": 1}, {"lcsc": 42, "quantity": 1}]})
        )
        result = self.build(keep_work=True)
        answer = json.loads((result.work / "project/answer.json").read_text())
        found = [[c["lcsc"] for c in r["components"]] for r in answer["results"]]
        self.assertEqual(found, [[990000001], []])
        self.assertEqual(result.summary["catalog_parts"], 1)
        paths = [r["path"] for r in result.summary["picker_requests"]]
        self.assertEqual(paths, ["/v0/query"])

    def test_failure_is_reported(self):
        (self.project / "fake.json").write_text(json.dumps({"exit": 3}))
        result = self.build()
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.outputs, {})
        self.assertTrue((result.out / "ato.log").is_file())

    def test_authoring_failure_without_selected_parts_has_no_capture_error(self):
        (self.project / parts.LOCK_NAME).unlink()
        (self.project / "fake.json").write_text(json.dumps({"exit": 3}))
        result = self.build(offline=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.summary["native_returncode"], 3)
        self.assertEqual(result.summary["part_capture_errors"], [])
        self.assertFalse((self.project / ".yapnr/parts/cache").exists())

    def test_timeout_kills_the_process_tree(self):
        pid_file = self.root / "grandchild.pid"
        (self.project / "fake.json").write_text(json.dumps({"hang": str(pid_file)}))
        started = time.monotonic()
        options = runner.BuildOptions(
            project=self.project,
            out=self.root / "out",
            cache=self.cache.location,
            kicad_cli=str(self.cli),
            timeout=3,
            work_root=self.root / "work",
        )
        with mock.patch.dict(os.environ, self.env):
            result = runner.build(options, log=lambda _text: None)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 60)
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the grandchild outlived the build's deadline")

    def test_a_differing_part_in_the_tree_is_an_error(self):
        testing.write_part(self.project / "elec/src/parts")
        (self.project / "elec/src/parts" / testing.SYNTHETIC_PART / "extra.md").write_text("x")
        with self.assertRaisesRegex(Exception, "exists with other content"):
            self.build()
        self.assertTrue(self.build(replace_parts=True).ok)

    def test_output_directory_is_replaced_only_when_it_holds_a_build(self):
        self.assertTrue(self.build().ok)  # the output directory now holds result.json
        self.assertTrue(self.build().ok)
        precious = self.root / "precious"
        precious.mkdir()
        (precious / "notes.txt").write_text("keep me")
        for out in (precious, self.project, self.project.parent):
            options = runner.BuildOptions(
                project=self.project, out=out, cache=self.cache.location, work_root=self.root
            )
            with mock.patch.dict(os.environ, self.env):
                with self.assertRaises(runner.BuildError, msg=str(out)):
                    runner.build(options, log=lambda _text: None)
        self.assertEqual((precious / "notes.txt").read_text(), "keep me")

    def assert_gone(self, pid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail(f"process {pid} outlived the build")

    def test_nothing_outlives_a_finished_build(self):
        pid_file = self.root / "left.pid"
        (self.project / "fake.json").write_text(json.dumps({"leave": str(pid_file)}))
        self.assertTrue(self.build().ok)
        self.assert_gone(int(pid_file.read_text()))

    def test_layout_follows_ato_yaml(self):
        yaml_path = self.project / "ato.yaml"
        yaml_path.write_text(yaml_path.read_text() + "paths:\n  layout: pcb\n")
        (self.project / "fake.json").write_text(
            json.dumps({"layout": "pcb/default/default.kicad_pcb"})
        )
        result = self.build(keep_work=True, update_layout=True, stock_footprints="all")
        self.assertTrue(result.ok)
        self.assertEqual(result.outputs["pcb"], "default.kicad_pcb")
        self.assertEqual(result.summary["layout"], "pcb/default/default.kicad_pcb")
        self.assertIsNotNone(result.input_id)
        self.assertTrue((result.work / "project/pcb/default/fp-lib-table").is_file())
        self.assertTrue((self.project / "pcb/default/default.kicad_pcb").is_file())
        self.assertFalse((self.project / "elec/layout").exists())

    def test_a_single_layout_of_another_name_is_used(self):
        seeded = self.project / "elec/layout/default/mini.kicad_pcb"
        seeded.parent.mkdir(parents=True)
        seeded.write_text("(kicad_pcb)\n")
        (seeded.parent / "_autosave-mini.kicad_pcb").write_text("(kicad_pcb)\n")
        (self.project / "fake.json").write_text(
            json.dumps({"layout": "elec/layout/default/mini.kicad_pcb"})
        )
        result = self.build(keep_work=True)
        self.assertEqual(result.summary["layout"], "elec/layout/default/mini.kicad_pcb")
        self.assertEqual(result.outputs["pcb"], "default.kicad_pcb")
        self.assertTrue((result.out / "default.kicad_pcb").read_text().startswith("(kicad_pcb"))
        (seeded.parent / "other.kicad_pcb").write_text("(kicad_pcb)\n")
        with self.assertRaisesRegex(runner.BuildError, "2 layouts"):
            self.build()

    def test_layout_paths_stay_inside_the_project(self):
        yaml_path = self.project / "ato.yaml"
        yaml_path.write_text(yaml_path.read_text() + "paths:\n  layout: ../elsewhere\n")
        with self.assertRaisesRegex(runner.BuildError, "outside the project"):
            self.build()

    def test_only_the_listed_files_are_copied(self):
        (self.project / "elec/layout").mkdir(parents=True)
        (self.project / "elec/layout/untracked.txt").write_text("not an input")
        (self.project / "elec/src/build").mkdir(parents=True)
        (self.project / "elec/src/build/nested.ato").write_text("# a nested build directory\n")
        result = self.build(keep_work=True)
        seen = json.loads((result.work / "project/seen-files.json").read_text())
        self.assertIn("elec/layout/untracked.txt", seen)
        self.assertIn("elec/src/build/nested.ato", seen)  # only the top-level build/ is skipped
        listed = ["elec/src/main.ato", parts.LOCK_NAME]
        result = self.build(keep_work=True, files=listed)
        seen = json.loads((result.work / "project/seen-files.json").read_text())
        self.assertNotIn("elec/layout/untracked.txt", seen)
        self.assertIn("elec/src/main.ato", seen)
        self.assertIn("ato.yaml", seen)
        with self.assertRaisesRegex(runner.BuildError, "outside"):
            self.build(files=["../escape"])
        with self.assertRaisesRegex(runner.BuildError, "does not exist"):
            self.build(files=["missing.ato"])

    def test_disallowed_targets(self):
        for target in ("default", "all", "datasheets"):
            with self.assertRaises(runner.BuildError):
                self.build(targets=[target])

    def test_normalized_board(self):
        a = '(uuid "1b4e28ba-2fa1-11d2-883f-0016d3cca427")'
        b = '(uuid "6fa459ea-ee8a-3ca4-894e-db77e160355e")'
        self.assertEqual(runner.normalized_board(a), runner.normalized_board(b))


if __name__ == "__main__":
    unittest.main()
