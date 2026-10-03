"""PNR_GLOSS end to end: the pass controller with real KiCad workers and native DRC.

PnR runtime python drives the KiCad-python workers and kicad-cli strictly one at a time
(PNR_KICAD_PYTHON / PNR_KICAD_CLI). The fixture is gloss_fixture (built by a KiCad-python
subprocess). Covers: a full pass stays DRC/ERC clean, a corridor transaction, an injected
illegal edit rejected by the native DRC gate and attributed to its edit, phase-end
bisection that drops the failing transaction and replays the rest, and a whole-pass
revert to the byte-identical input.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

KP = os.environ.get("PNR_KICAD_PYTHON")
CLI = os.environ.get("PNR_KICAD_CLI")
READY = bool(KP and CLI and Path(KP).exists() and Path(CLI).exists())
HERE = Path(__file__).resolve().parent


@unittest.skipUnless(READY, "requires PNR_KICAD_PYTHON and PNR_KICAD_CLI")
class GlossEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.tmp.name) / "fixture"
        # hardware/pnr, the tests and the repository root (pnr.fab_profile reads yapnr.fab)
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join([str(HERE.parent), str(HERE), str(HERE.parents[2])]),
        )
        subprocess.run(
            [KP, str(HERE / "gloss_fixture.py"), str(cls.fixture)],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.input = self.run_dir / "input"
        shutil.copytree(self.fixture, self.input)

    def make(self, steps, cls=None, **conf_kw):
        from pnr import gloss

        a = argparse.Namespace(
            board=self.input / "board.kicad_pcb",
            rules=self.input / "rules.json",
            annotation_source=[],
            out=self.run_dir / "out" / "candidate.kicad_pcb",
            work_dir=self.run_dir / "work",
            report=self.run_dir / "out" / "result.json",
            kicad_cli=CLI,
            kicad_python=KP,
            label="e2e",
            seconds=600,
            metrics=False,
        )
        conf = gloss.Settings(
            steps=tuple(steps),
            passes=gloss.LABELS,
            si=False,
            cycle=False,
            seconds=600,
            max_transactions=48,
            batch=16,
            **conf_kw
        )
        return (cls or gloss.GlossPass)(a, conf)

    def drc(self, board):
        from pnr.native_drc import run_drc

        board = Path(board)
        return run_drc(CLI, board, board.with_name(board.stem + ".check.drc.json"), final=True)

    def segments(self, board, net):
        text = Path(board).read_text()
        out = []
        for m in re.finditer(
            r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)\s+"
            r'\(width ([\d.]+)\).*?\(net "?([^")]*)"?\)\s+\(uuid "([^"]+)"\)',
            text,
            re.S,
        ):
            if m.group(6) == net:
                xs = [round(float(v) * 1e6) for v in m.groups()[:5]]
                out.append((m.group(7), (xs[0], xs[1]), (xs[2], xs[3]), xs[4]))
        return out

    def test_full_pass_stays_native_clean(self):
        result = self.make(("normalize", "dekink", "gloss", "corridor")).run()
        self.assertGreaterEqual(result["accepted_transactions"], 2)
        self.assertTrue(
            all(x <= y for x, y in zip(result["objective_after"], result["objective_before"]))
        )
        self.assertIn("normalize", result["edits_by_step"])
        self.assertIn("gloss", result["edits_by_step"])
        before, after = self.drc(self.input / "board.kicad_pcb"), self.drc(
            self.run_dir / "out" / "candidate.kicad_pcb"
        )
        self.assertLessEqual(len(after["violations"]), len(before["violations"]))
        self.assertLessEqual(len(after["unconnected_items"]), len(before["unconnected_items"]))
        self.assertTrue(result["end_gate"]["passed"])
        self.assertEqual(
            len(self.segments(self.run_dir / "out" / "candidate.kicad_pcb", "sig2")), 1
        )
        # per-step deltas of the accepted edits, the adjacency change measured on the applied board
        self.assertIn("adjacency_mm2", result["delta_by_step"]["gloss"])
        self.assertLess(result["delta_by_step"]["gloss"]["length_mm"], 0)
        self.assertEqual(result["planning"]["deadline_hits"], 0)
        self.assertGreater(result["planning"]["min_margin_seconds"], 0)

    def test_corridor_transaction(self):
        result = self.make(("corridor",)).run()
        self.assertEqual(result["edits_by_step"], {"corridor": 1})
        accepted = [t for t in result["transactions"] if t["accepted"]]
        self.assertLess(accepted[0]["delta"]["excess_mm2"], -2.5)
        after = self.drc(self.run_dir / "out" / "candidate.kicad_pcb")
        self.assertEqual(after["violations"], [])

    def test_cross_group_cap_holds_through_a_pass(self):
        # functional groups (owner decision 2026-09-30): packing c1 onto c2 leaves ~11.9 mm at
        # minimum pitch: one group packs it as without groups, two groups (10 mm cap) keep the input
        groups = self.run_dir / "groups.json"
        groups.write_text(json.dumps({"classes": {"bus": {"nets": ["c1"]}, "ctl": ["c2"]}}))
        result = self.make(("corridor",), classes=str(groups)).run()
        self.assertEqual(result["edits_by_step"], {})
        self.assertEqual(result["output_sha256"], result["source_sha256"])
        self.assertEqual(result["cross_group"]["cap_mm"], 10.0)
        same = self.run_dir / "same.json"
        same.write_text(json.dumps({"classes": {"bus": ["c1", "c2"]}}))
        shutil.rmtree(self.run_dir / "work")
        result = self.make(("corridor",), classes=str(same)).run()
        self.assertEqual(result["edits_by_step"], {"corridor": 1})
        self.assertTrue(result["end_gate"]["passed"])
        # the 15 mm cap admits the 11.9 mm run between groups
        shutil.rmtree(self.run_dir / "work")
        result = self.make(("corridor",), classes=str(groups), cross_group_mm=15.0).run()
        self.assertEqual(result["edits_by_step"], {"corridor": 1})

    def test_injected_dangling_edit_rejected_and_attributed(self):
        from pnr import gloss_geometry as g

        board = self.input / "board.kicad_pcb"
        dup = next(
            s
            for s in self.segments(board, "sig3")
            if s[1] == (4500000, 5000000) and s[2] == (6000000, 5000000)
        )
        spec = dict(
            step="normalize",
            net="sig3",
            layer="F.Cu",
            nets=["sig3"],
            delete=[dup[0]],
            modify={},
            old=dict(uuids=[dup[0]], sha256=g.segments_sha256((dup,))),
            old_segments_nm=[[dup[0], list(dup[1]), list(dup[2]), dup[3]]],
            counts={},
            gain=1.0,
            box=[4000000, 4500000, 6500000, 5500000],
            id="normalize:injected-dangling",
        )
        p = self.make(("normalize",))
        p.inject = [spec]
        result = p.run()
        first = result["transactions"][0]
        self.assertFalse(first["accepted"])
        self.assertIn("drc:dangling", first["reasons"])
        self.assertEqual(first["attribution"]["nets"], ["sig3"])
        self.assertTrue(result["transactions"][1]["accepted"])
        self.assertEqual(
            result["transactions"][1]["applied_edits"],
            [s for s in first["edits"] if s != spec["id"]],
        )
        self.assertIn(spec["id"], p.blacklist)
        out = self.run_dir / "out" / "candidate.kicad_pcb"
        self.assertIn(dup[0], [s[0] for s in self.segments(out, "sig3")])
        self.assertEqual(len(self.segments(out, "sig2")), 1)

    def test_end_gate_bisection_drops_failing_transaction(self):
        from pnr import gloss

        class Failing(gloss.GlossPass):
            def end_check(self, entry):
                upto = entry["specs_so_far"]
                bad = any(s["step"] == "normalize" for s in upto)
                return (not bad), (["injected"] if bad else [])

        p = self.make(("normalize", "gloss"), Failing)
        result = p.run()
        end = result["end_gate"]
        self.assertTrue(end["passed"])
        self.assertEqual(end["dropped"][0]["transaction"], 1)
        self.assertEqual(end["rounds"], 1)
        self.assertNotIn("normalize", result["edits_by_step"])
        self.assertIn("gloss", result["edits_by_step"])
        self.assertTrue(any(t.get("replay_of") and t["accepted"] for t in result["transactions"]))
        out = self.run_dir / "out" / "candidate.kicad_pcb"
        self.assertEqual(len(self.segments(out, "sig2")), 4)  # the normalize merge was dropped

    def test_three_failures_revert_byte_identical(self):
        from pnr import gloss

        class Never(gloss.GlossPass):
            def end_check(self, entry):
                return False, ["injected"]

        result = self.make(("normalize", "dekink", "gloss"), Never).run()
        self.assertTrue(result["end_gate"]["reverted"])
        self.assertEqual(result["accepted_transactions"], 0)

        def digest(p):
            return hashlib.sha256(Path(p).read_bytes()).hexdigest()

        self.assertEqual(
            digest(self.run_dir / "out" / "candidate.kicad_pcb"),
            digest(self.input / "board.kicad_pcb"),
        )
        self.assertEqual(result["output_sha256"], result["source_sha256"])


if __name__ == "__main__":
    unittest.main()
