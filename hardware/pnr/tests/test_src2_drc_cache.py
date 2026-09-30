import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pnr.native_drc import run_drc


class DrcCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.fail = False
        self.mutate = None
        lib = self.root / "lib" / "Parts"
        lib.mkdir(parents=True)
        (lib / "R0402.kicad_mod").write_text('(footprint "R0402")')
        self.board = self.write(
            "run1/board.kicad_pcb", '(kicad_pcb (footprint "Parts:R0402" (at 1 1)))'
        )
        self.write("run1/board.kicad_pro", "{}")
        self.write(
            "run1/fp-lib-table",
            '(fp_lib_table (lib (name "Parts") (type "KiCad") (uri "%s") (options "") (descr "")))'
            % lib,
        )
        self.env = dict(
            PNR_DRC_CACHE_DIR=str(self.root / "cache"),
            KICAD_CONFIG_HOME=str(self.root / "config"),
            HOME=str(self.root),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def invoke(self, command, **kwargs):
        if command[1] == "version":
            return SimpleNamespace(stdout=b"Version: 10.0.6, release build")
        self.calls.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(1, command)
        if self.mutate:
            self.mutate()
        report = Path(command[-1])
        report.write_text(
            json.dumps(
                dict(
                    date=str(len(self.calls)),
                    source=Path(command[3]).name,
                    violations=[],
                    unconnected_items=[{}],
                ),
                indent=4,
            )
        )
        kwargs["stdout"].write("Saved DRC Report to %s\n" % report)

    def drc(self, board, report, env=None):
        report = self.root / report
        report.parent.mkdir(parents=True, exist_ok=True)
        with patch("pnr.native_drc.subprocess.run", side_effect=self.invoke):
            return run_drc("cli", board, report, env=self.env if env is None else env), report

    def stats(self):
        return json.loads((self.root / "cache" / "stats.json").read_text())

    def test_hit_rewrites_identical_report_and_log(self):
        first, a = self.drc(self.board, "out1/board.drc.json")
        second, b = self.drc(self.board, "out2/board.drc.json")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        log = b.with_suffix(".log").read_text()
        self.assertIn("Saved DRC Report to %s\n" % b, log)
        self.assertNotIn(str(a), log)
        self.assertIn("Native DRC cache hit", log)
        self.assertTrue(b.with_suffix(".timing.json").exists())
        stats = self.stats()
        self.assertEqual((stats["hits"], stats["misses"], stats["stores"]), (1, 1, 1))

    def test_identical_inputs_in_another_folder_hit(self):
        self.drc(self.board, "out1/board.drc.json")
        for name in ("board.kicad_pcb", "board.kicad_pro", "fp-lib-table"):
            self.write("run2/" + name, (self.root / "run1" / name).read_text())
        self.drc(self.root / "run2" / "board.kicad_pcb", "out2/board.drc.json")
        self.assertEqual(len(self.calls), 1)

    def test_every_input_changes_the_key(self):
        self.drc(self.board, "out/board.drc.json")
        changes = [
            lambda: self.write(
                "run1/board.kicad_pcb", '(kicad_pcb (footprint "Parts:R0402" (at 2 1)))'
            ),
            lambda: self.write("run1/board.kicad_pro", '{"board": {}}'),
            lambda: self.write("run1/board.kicad_dru", "(version 1)"),
            lambda: self.write("lib/Parts/R0402.kicad_mod", '(footprint "R0402" (attr smd))'),
            lambda: self.write(
                "run1/fp-lib-table", (self.root / "run1" / "fp-lib-table").read_text() + " "
            ),
            lambda: self.write("config/10.0/fp-lib-table", "(fp_lib_table)"),
            lambda: (self.root / "run1" / "fp-lib-table").unlink(),
        ]
        for count, change in enumerate(changes, 2):
            change()
            self.drc(self.board, "out/board.drc.json")
            self.assertEqual(len(self.calls), count)
        renamed = self.write("run1/other.kicad_pcb", self.board.read_text())
        self.drc(renamed, "out/other.drc.json")
        self.assertEqual(len(self.calls), len(changes) + 2)
        self.drc(
            self.board, "out/board.drc.json", env=dict(self.env, KICAD10_FOOTPRINT_DIR="/elsewhere")
        )
        self.assertEqual(len(self.calls), len(changes) + 3)

    def test_disabled_without_env_and_failures_not_stored(self):
        env = dict(self.env)
        env.pop("PNR_DRC_CACHE_DIR")
        self.drc(self.board, "a/board.drc.json", env=env)
        self.drc(self.board, "a/board.drc.json", env=env)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.root / "cache").exists())
        self.fail = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.drc(self.board, "b/board.drc.json")
        self.assertFalse((self.root / "b" / "board.drc.json").exists())
        self.fail = False
        self.assertEqual(len(self.calls), 4)
        self.drc(self.board, "c/board.drc.json")
        self.assertEqual(len(self.calls), 5)

    def test_input_changed_during_run_is_not_stored(self):
        self.mutate = lambda: self.write(
            "run1/board.kicad_pcb", '(kicad_pcb (footprint "Parts:R0402" (at 3 1)))'
        )
        self.drc(self.board, "a/board.drc.json")
        self.mutate = None
        self.assertEqual(self.stats().get("unstored"), 1)
        self.write("run1/board.kicad_pcb", '(kicad_pcb (footprint "Parts:R0402" (at 1 1)))')
        self.drc(self.board, "b/board.drc.json")
        self.assertEqual(len(self.calls), 2)

    def test_corrupt_entry_falls_back_to_real_run(self):
        self.drc(self.board, "a/board.drc.json")
        for entry in (self.root / "cache" / "entries").glob("*/*/report.json.gz"):
            entry.write_bytes(b"broken")
        result, report = self.drc(self.board, "b/board.drc.json")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["date"], "2")
        self.assertEqual(self.stats()["corrupt"], 1)
        self.assertEqual(json.loads(report.read_text()), result)
        self.drc(self.board, "c/board.drc.json")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.stats()["hits"], 1)


if __name__ == "__main__":
    unittest.main()
