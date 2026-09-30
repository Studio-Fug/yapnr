"""pnr.feedback.report: the PULL-vs-RAND sign test pairs each control with its matched PULL sibling."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pnr.feedback import report


def rec(tag, arm, parent, missing, gen=1, **extra):
    return dict(
        tag=tag,
        arm=arm,
        parent=dict(tag=parent) if parent else None,
        gen=gen,
        stage="native",
        status="ok",
        objective=[0, 0, 0, 10, 0, missing],
        **extra
    )


class PairingTest(unittest.TestCase):
    def test_rand_is_compared_with_its_matched_sibling_only(self):
        # PULL k=1 happens to be better than RAND; the matched k=0 sibling is worse
        kids = [
            rec("P-g1c0-a", "pull", "P", 5, k=0),
            rec("P-g1c1-a", "pull", "P", 1, k=1),
            rec("P-g1r0-a", "rand", "P", 4, matched="P-g1c0-a"),
        ]
        self.assertEqual(report.pull_vs_rand(kids, report._missing, lambda c: c["tag"]), (0, 1, 0))
        # best-of-two (the old comparison) would have called this a PULL win
        self.assertLess(min(k["objective"][5] for k in kids if k["arm"] == "pull"), 4)

    def test_legacy_records_pair_with_the_k0_tag(self):
        kids = [
            rec("P-g1c1-a", "pull", "P", 1),
            rec("P-g1c0-a", "pull", "P", 3),
            rec("P-g1r0-a", "rand", "P", 2),
            rec("Q-g1c0-b", "pull", "Q", 0),
            rec("Q-g1r0-b", "rand", "Q", 0),
        ]
        self.assertEqual(report.pull_vs_rand(kids, report._missing, lambda c: c["tag"]), (0, 1, 1))

    def test_halving_ids(self):
        kids = [
            dict(id="g1c00", arm="pull", parent="h4-p1", gen=1, k=0, objective=[0, 0, 0, 0, 0, 30]),
            dict(id="g1c01", arm="pull", parent="h4-p1", gen=1, k=1, objective=[0, 0, 0, 0, 0, 20]),
            dict(
                id="g1r0",
                arm="rand",
                parent="h4-p1",
                gen=1,
                matched="g1c00",
                objective=[0, 0, 0, 0, 0, 31],
            ),
        ]
        self.assertEqual(
            report.pull_vs_rand(kids, lambda r: r["objective"][5], lambda c: c["id"]), (1, 0, 0)
        )

    def test_sign_line_states_n_and_the_reachable_bound(self):
        line = report.sign_line(3, 0, 1)
        self.assertIn("n=3", line)
        self.assertIn("smallest p reachable at n=3: 0.25", line)
        self.assertIn("cannot reach p<0.05 yet", line)
        self.assertNotIn("cannot reach", report.sign_line(7, 0, 0))
        self.assertIn("no decisive pair yet", report.sign_line(0, 0, 2))


class ReportFilesTest(unittest.TestCase):
    def test_blocks_report(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "tpl"
            t.mkdir()
            trials = [
                rec("P-g0b-x", "rebase", "P", 2, gen=0, code="abc", loadavg_start=[3.0, 2, 1]),
                rec("P-g1c0-a", "pull", "P-g0b-x", 1, k=0, code="abc", loadavg_start=[5.0, 2, 1]),
                rec(
                    "P-g1r0-a",
                    "rand",
                    "P-g0b-x",
                    2,
                    matched="P-g1c0-a",
                    code="abc",
                    loadavg_start=[4.0, 2, 1],
                ),
            ]
            (t / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in trials))
            ranked = [
                dict(r, missing=r["objective"][5])
                for r in sorted(trials, key=lambda r: r["objective"][5])
            ]
            (t / "library.json").write_text(
                json.dumps(
                    dict(
                        blocks=["board.converter"],
                        ranked=ranked,
                        counts=dict(evaluated=3, ok=3, complete=0),
                        stale_imports=1,
                        rebase=dict(
                            planned=1,
                            evaluated=1,
                            pairs=[["P", 3, 2]],
                            delta=[-1],
                            complete_import=0,
                            complete_rebase=0,
                        ),
                        generations=[
                            dict(
                                gen=1,
                                parents=["P-g0b-x"],
                                children=dict(pull=1, rand=1),
                                arms=dict(pull=dict(n=1, delta=[-1]), rand=dict(n=1, delta=[0])),
                            )
                        ],
                    )
                )
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                report.main([d])
        text = out.getvalue()
        self.assertIn("stale-code imports (not ranked): 1", text)
        self.assertIn("rebase (import -> this code, missing): 1 of 1 evaluated", text)
        self.assertIn("PULL vs matched RAND: 1 better, 0 worse, 0 tied; n=1", text)
        self.assertIn("evaluation code abc", text)
        self.assertIn("load at start 3.0-5.0", text)


if __name__ == "__main__":
    unittest.main()
