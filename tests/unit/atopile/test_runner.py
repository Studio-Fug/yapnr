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
layout = Path("elec/layout") / build
layout.mkdir(parents=True, exist_ok=True)
board = '(kicad_pcb\n\t(version 20241229)\n\t(footprint "X:Y"\n\t\t(at 1 2)\n\t\t(uuid "%s")\n\t)\n)\n'
(layout / f"{{build}}.kicad_pcb").write_text(board % uuid.uuid4())
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
            **kwargs,
        )
        with mock.patch.dict(os.environ, self.env):
            return runner.build(options, log=lambda _text: None)

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
            options = runner.BuildOptions(project=self.project, out=out, cache=self.cache.location)
            with mock.patch.dict(os.environ, self.env):
                with self.assertRaises(runner.BuildError, msg=str(out)):
                    runner.build(options, log=lambda _text: None)
        self.assertEqual((precious / "notes.txt").read_text(), "keep me")

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
