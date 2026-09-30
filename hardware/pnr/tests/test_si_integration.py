"""SI v1 wiring into the flows (PNR_SI=1): compile sites, post-route side fields, block filter.

Fake KIBIS driver + fake simulator (tests.test_si_report) for the pnr-runtime paths:

* native_loop parent: :func:`pnr.si.report.gate_rules_file` after the pcbnew ``prepare``
  worker (ideal-board gate, report-only placement estimate, si-design.json, clear
  failure message with a waiver template, waivers);
* route/__main__ ``--dump-rules``: compile before placement, estimate after it, exit 3
  on a design failure, rules.json unchanged with the flag off;
* full_iteration: ``si_side_fields`` writes electrical/si-report.json and returns only
  the named side fields;
* hier.blocks.sub_board keeps an SI requirement only when its whole chain is inside.

The opt-in live case (``PNR_SI_LIVE=1``) runs the real ``prepare`` worker (headless
KiCad python, pcbnew) on the H6 p027 board, then the real design check (KIBIS + ngspice).
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnr.si import report as R
from pnr.si import annotations as A
from pnr.si import extract as X
from tests.test_si_annotations import fixture_components
from tests.test_si_report import FakeSim, fake_kibis, intents, tmpdir

TESTDATA = Path(__file__).resolve().parents[1] / "testdata/si"
PNR_ROOT = Path(__file__).resolve().parents[1]
SLOW = 20e-9  # 10-90 % = 44 ns > the 25 ns gate


def slow_on(marker):
    return FakeSim(lambda d: SLOW if marker in d else 1e-9)


class _Fake(unittest.TestCase):
    def setUp(self):
        self.cache = tmpdir(self, "pnr-si-cache-")
        self.tmp = Path(tmpdir(self, "pnr-si-int-"))
        self.env = dict(PNR_SI="1", PNR_SI_CACHE=self.cache)  # never the user's ~/.cache/pnr-si
        for p in (
            mock.patch("pnr.si.models.kibis_driver", fake_kibis),
            mock.patch.dict(os.environ, {"PNR_SI_CACHE": self.cache}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def rules_file(self, extra=()):
        its, _ = intents(extra)
        path = self.tmp / "policy" / "prepare.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(x=1, si_intents=its), indent=2) + "\n")
        est = self.tmp / "policy" / "si-estimate.json"
        est.write_text(json.dumps(X.estimate_geometries(its, fixture_components())))
        return path, est


class GateRulesFileTest(_Fake):
    def test_flag_off_is_a_no_op(self):
        path, est = self.rules_file()
        before = path.read_bytes()
        with mock.patch("pnr.si.runner.run_deck", side_effect=AssertionError("must not simulate")):
            self.assertIsNone(R.gate_rules_file(path, out_dir=path.parent, estimates=est, env={}))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((path.parent / "si-design.json").exists())

    def test_pass_writes_design_report_and_rules_summary(self):
        path, est = self.rules_file()
        with mock.patch("pnr.si.runner.run_deck", FakeSim(lambda d: 1e-9)):
            rep = R.gate_rules_file(
                path, out_dir=path.parent, estimates=est, env=self.env, save_waves=False
            )
        design = json.loads((path.parent / "si-design.json").read_text())
        self.assertEqual((design["mode"], design["status"]), ("design", "pass"))
        self.assertEqual(design["summary"]["decks"], 42)  # 2 x 21 (Z0 corners at 1 m / 3 m)
        self.assertEqual(
            design["summary"]["estimate"], dict(requirements=2, failed=0, errors=0, gates=False)
        )
        est0 = design["requirements"][0]["estimate"]
        # report-only estimate: nominal cable impedance only (3 driver corners x 3 lengths)
        self.assertEqual(
            (est0["kind"], est0["gates"], len(est0["results"])), ("estimate-nominal", False, 9)
        )
        rules = json.loads(path.read_text())
        self.assertEqual(rules["x"], 1)
        self.assertEqual(rules["si_design"]["status"], "pass")
        self.assertEqual(rules["si_design"]["report"], str(path.parent / "si-design.json"))
        self.assertTrue(path.read_text().endswith("}\n"))  # native_loop.save format
        self.assertFalse((path.parent / "si-design-waves").exists())
        self.assertEqual(rep["status"], "pass")

    def test_estimate_failure_is_report_only(self):
        path, est = self.rules_file()
        # estimate decks carry '* seg ' lines (placement copper); the ideal decks do not
        with mock.patch("pnr.si.runner.run_deck", slow_on("* seg ")):
            rep = R.gate_rules_file(
                path, out_dir=path.parent, estimates=est, env=self.env, save_waves=False
            )
        self.assertEqual(rep["status"], "pass")
        self.assertEqual(rep["summary"]["estimate"]["failed"], 2)
        self.assertEqual(rep["requirements"][0]["estimate"]["status"], "fail")

    def test_design_failure_fails_with_clear_message_until_waived(self):
        path, est = self.rules_file()
        with mock.patch("pnr.si.runner.run_deck", slow_on("cable=3m")):
            with self.assertRaises(R.SIDesignError) as ctx:
                R.gate_rules_file(
                    path, out_dir=path.parent, estimates=est, env=self.env, save_waves=False
                )
        msg = str(ctx.exception)
        self.assertIn("SI design check FAILED (PNR_SI=1)", msg)
        self.assertIn("led0_data", msg)
        self.assertIn("rise_10_90_ns", msg)
        self.assertIn(str(path.parent / "si-design.json"), msg)
        waiver = [l.strip() for l in msg.splitlines() if l.strip().startswith("# @pnr-si-waiver ")]
        self.assertEqual(len(waiver), 1)
        body = json.loads(waiver[0].split("# @pnr-si-waiver ", 1)[1])
        self.assertEqual((body["name"], body["cable_m"]), ("led0_data", [3.0]))
        self.assertNotIn("si_design", json.loads(path.read_text()))
        self.assertEqual(json.loads((path.parent / "si-design.json").read_text())["status"], "fail")
        # the template, with a real reason, is a valid waiver: both requirements waived -> compile passes
        lines = []
        for name in ("led0_data", "led1_data"):
            lines.append(
                "# @pnr-si-waiver "
                + json.dumps(dict(body, name=name, reason="strip leads are <= 1 m (test)"))
            )
        path, est = self.rules_file(lines)
        with mock.patch("pnr.si.runner.run_deck", slow_on("cable=3m")):
            rep = R.gate_rules_file(
                path, out_dir=path.parent, estimates=est, env=self.env, save_waves=False
            )
        self.assertEqual(rep["status"], "waived")
        self.assertEqual(json.loads(path.read_text())["si_design"]["status"], "waived")

    def test_models_dir_follows_env(self):
        # PNR_SI_MODELS in the env argument (not only os.environ) selects the limits
        import shutil

        models = self.tmp / "models"
        shutil.copytree(PNR_ROOT / "si_models", models)
        prof = models / "profile-ws2812b-din.json"
        d = json.loads(prof.read_text())
        d["limits"]["rise_10_90_ns"]["max"] = 0.1
        prof.write_text(json.dumps(d))
        path, est = self.rules_file()
        with mock.patch("pnr.si.runner.run_deck", FakeSim(lambda d: 1e-9)):
            with self.assertRaises(R.SIDesignError):
                R.gate_rules_file(
                    path,
                    out_dir=path.parent,
                    env=dict(self.env, PNR_SI_MODELS=str(models)),
                    save_waves=False,
                )
            self.assertEqual(
                R.gate_rules_file(path, out_dir=path.parent, env=self.env, save_waves=False)[
                    "status"
                ],
                "pass",
            )

    def test_errors_fail_closed(self):
        path, est = self.rules_file()
        with mock.patch("pnr.si.runner.run_deck", FakeSim(lambda d: None)):
            with self.assertRaises(R.SIDesignError) as ctx:
                R.gate_rules_file(
                    path, out_dir=path.parent, estimates=est, env=self.env, save_waves=False
                )
        self.assertIn("could not evaluate 2 requirement(s)", str(ctx.exception))

    def test_rules_without_intents(self):
        path = self.tmp / "prepare.json"
        path.write_text("{}\n")
        with self.assertRaises(R.SIDesignError) as ctx:
            R.gate_rules_file(path, env=self.env)
        self.assertIn("no si_intents", str(ctx.exception))

    def test_zero_requirements_still_reports(self):
        path = self.tmp / "prepare.json"
        path.write_text(json.dumps(dict(si_intents=[])) + "\n")
        with mock.patch(
            "pnr.si.runner.run_deck", side_effect=AssertionError("nothing to simulate")
        ):
            rep = R.gate_rules_file(path, out_dir=self.tmp, env=self.env)
        self.assertEqual((rep["status"], rep["summary"]["requirements"]), ("pass", 0))
        self.assertTrue((self.tmp / "si-design.json").exists())


class EstimateGeometriesTest(unittest.TestCase):
    def test_estimates_and_missing_parts(self):
        its, _ = intents()
        est = X.estimate_geometries(its, fixture_components())
        self.assertEqual(sorted(est), ["led0_data", "led1_data"])
        self.assertTrue(
            all(g["kind"] == "estimate-nominal" and g["status"] == "ok" for g in est.values())
        )
        est = X.estimate_geometries(its, [c for c in fixture_components() if c.ref != "R21"])
        self.assertIn("error", est["led0_data"])
        self.assertNotIn("error", est["led1_data"])


class FullIterationHookTest(_Fake):
    def setUp(self):
        super().setUp()
        dump = json.loads((TESTDATA / "p027-led-dump.json").read_text())
        for p in (
            mock.patch("pnr.si.extract.read_board", return_value=dump),
            mock.patch.dict(os.environ, {"PNR_SI": "1"}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_side_fields_and_report_location(self):
        from pnr.full_iteration import si_side_fields

        its, src = intents()
        work = self.tmp / "electrical"
        work.mkdir()
        with mock.patch("pnr.si.runner.run_deck", FakeSim(lambda d: 1e-9)):
            out = si_side_fields(
                work / "board.kicad_pcb", dict(si_intents=its), work, [src], dict(graph={})
            )
        self.assertEqual(set(out), set(R.SIDE_FIELDS))
        self.assertEqual(
            (out["si_layout_failures"], out["si_design_failures"], out["si_errors"]), (0, 0, 0)
        )
        self.assertEqual(out["si_summary"]["report"], str(work / "si-report.json"))
        self.assertEqual(sorted(out["si_summary"]["by_requirement"]), ["led0_data", "led1_data"])
        rep = json.loads((work / "si-report.json").read_text())
        self.assertEqual((rep["mode"], rep["summary"]["decks"]), ("post-route", 42))
        self.assertTrue(list((work / "si-waves" / "led0_data").glob("routed-*.json")))
        self.assertFalse((work / "si.json").exists())
        json.dumps(out)

    def test_layout_failure_counts_and_fallback_resolution(self):
        from pnr.full_iteration import si_side_fields
        from pnr.graph import BoardGraph

        _, src = intents()
        g = BoardGraph(name="fixture")
        g.components = fixture_components()
        work = self.tmp / "electrical"
        work.mkdir()
        with mock.patch("pnr.si.runner.run_deck", slow_on("* seg ")):
            out = si_side_fields(
                work / "board.kicad_pcb", {}, work, [src], dict(graph=json.loads(g.to_json()))
            )
        self.assertEqual((out["si_layout_failures"], out["si_design_failures"]), (2, 0))
        self.assertEqual(out["si_summary"]["by_requirement"]["led0_data"]["cause"], "layout")

    def test_crash_is_an_error_never_a_pass(self):
        from pnr.full_iteration import si_side_fields

        work = self.tmp / "electrical"
        work.mkdir()
        with mock.patch("pnr.si.report.post_route_report", side_effect=RuntimeError("boom")):
            out = si_side_fields(
                work / "board.kicad_pcb", dict(si_intents=[]), work, [], dict(graph={})
            )
        self.assertEqual((out["si_layout_failures"], out["si_errors"]), (None, 1))


class RouteMainTest(_Fake):
    """route/__main__ with placement mocked: only the rules compile path is exercised."""

    def run_main(self, src, env, sim):
        from pnr.graph import BoardGraph, BoardOutline

        g = BoardGraph(name="fixture")
        g.components = fixture_components()
        g.outline = BoardOutline(60.0, 60.0)
        graph = self.tmp / "graph.json"
        graph.write_text(g.to_json())
        cons = self.tmp / "constraints.yaml"
        cons.write_text("board:\n  outline: {w: 60, h: 60}\n")
        rules = self.tmp / "out" / "rules.json"
        rules.parent.mkdir(exist_ok=True)
        import pnr.route.__main__ as M

        fake_report = mock.Mock(converged=True, detail_result=None)
        fake_report.summary.return_value = "fake placement"
        with tempfile.TemporaryFile("w+") as err:
            with mock.patch.dict(os.environ, env), mock.patch.object(
                M, "route_and_place", return_value=(g, fake_report)
            ), mock.patch("pnr.si.runner.run_deck", sim), mock.patch("sys.stderr", err), mock.patch(
                "sys.stdout", new_callable=lambda: open(os.devnull, "w")
            ) as out:
                code = M.main(
                    [str(graph), str(cons), "--annotation-source", src, "--dump-rules", str(rules)]
                )
                out.close()
            err.seek(0)
            return code, rules, err.read()

    def test_flag_off_rules_unchanged(self):
        _, src = intents()
        code, rules, _ = self.run_main(src, {"PNR_SI": ""}, FakeSim(lambda d: None))
        self.assertEqual(code, 0)
        self.assertFalse({"si_intents", "si_design"} & set(json.loads(rules.read_text())))
        self.assertFalse((rules.parent / "si-design.json").exists())

    def test_flag_on_pass_and_estimate(self):
        _, src = intents()
        code, rules, _ = self.run_main(src, {"PNR_SI": "1"}, FakeSim(lambda d: 1e-9))
        self.assertEqual(code, 0)
        r = json.loads(rules.read_text())
        self.assertEqual([i["name"] for i in r["si_intents"]], ["led0_data", "led1_data"])
        self.assertEqual(r["si_design"]["status"], "pass")
        design = json.loads((rules.parent / "si-design.json").read_text())
        self.assertEqual(design["summary"]["estimate"]["requirements"], 2)  # added after placement
        self.assertTrue(list((rules.parent / "si-design-waves" / "led0_data").glob("ideal-*.json")))

    def test_design_failure_exit_code_and_message(self):
        _, src = intents()
        code, rules, err = self.run_main(src, {"PNR_SI": "1"}, slow_on("cable=3m"))
        self.assertEqual(code, 3)
        self.assertFalse(rules.exists())
        self.assertIn("pnr.si: SI design check FAILED", err)
        self.assertIn("# @pnr-si-waiver {", err)


class SubBoardFilterTest(unittest.TestCase):
    def setUp(self):
        try:
            import pnr.hier.blocks  # noqa: F401 - the hierarchical flow is not part of the Bazel :pnr library
        except ImportError as error:
            self.skipTest("pnr.hier unavailable: %s" % error)
        from pnr.constraints import compile_constraints
        from pnr.graph import BoardGraph, BoardOutline

        self.g = BoardGraph(name="fixture")
        self.g.components = fixture_components()
        self.g.outline = BoardOutline(60.0, 60.0)
        self.c = compile_constraints(
            {"board": {"outline": {"w": 60, "h": 60}}}, [c.ref for c in self.g.components]
        )
        self.its, _ = intents()

    def sub(self, refs, rules):
        from pnr.hier.blocks import Block, sub_board

        return sub_board(self.g, self.c, rules, Block(name="b", refs=refs), 10.0, 10.0)[2]

    def test_filters_only_present_keys(self):
        r = self.sub(["U9", "R21"], {})
        self.assertNotIn("si_intents", r)
        self.assertNotIn("terminal_width_intents", r)

    def test_requirement_kept_only_when_whole_chain_inside(self):
        rules = dict(
            si_intents=self.its,
            terminal_width_intents=[dict(ref="R21", pads=["2"]), dict(ref="C53", pads=["2"])],
        )
        r = self.sub(["U9", "R21"], rules)  # the led0 block: CN1 is top-level
        self.assertEqual(r["si_intents"], [])
        self.assertEqual([i["ref"] for i in r["terminal_width_intents"]], ["R21"])
        led0 = self.its[0]
        chain = (
            {led0["driver"]["ref"], led0["connector"]["ref"]}
            | {s["ref"] for s in led0["series"]}
            | {s["ref"] for s in led0["shunts"]}
        )
        r = self.sub(sorted(chain), rules)
        self.assertEqual([i["name"] for i in r["si_intents"]], ["led0_data"])


class RankKeyTest(unittest.TestCase):
    """PNR_SI=1: si_layout_failures ranks right after the legality terms; flag off: keys unchanged."""

    def setUp(self):
        try:
            from pnr.hier import synth_native  # noqa: F401 - not part of the Bazel :pnr library
            from pnr.mc import halving  # noqa: F401
        except ImportError as error:
            self.skipTest("hierarchical/halving flow unavailable: %s" % error)

    @staticmethod
    def recs():
        # objective = [violations, blocked, reference, subwidth, unqualified_pairs, unconnected]
        return [
            dict(id="a", objective=[0, 0, 0, 5, 0, 0], si_layout_failures=1, hot_loops_open=0),
            dict(id="b", objective=[0, 0, 0, 9, 0, 0], si_layout_failures=0, hot_loops_open=0),
            dict(
                id="c", objective=[0, 0, 0, 1, 0, 0], si_layout_failures=None, hot_loops_open=0
            ),  # SI crashed
            dict(
                id="d", objective=[0, 0, 0, 0, 0, 2], si_layout_failures=0, hot_loops_open=0
            ),  # 2 opens
            dict(id="e", objective=[0, 0, 0, 2, 0, 0], hot_loops_open=0),
        ]  # no SI result

    def order(self, key):
        return [r["id"] for r in sorted(self.recs(), key=key)]

    def test_halving_native_key(self):
        from pnr.mc.halving import _rank_key

        with mock.patch.dict(os.environ, {"PNR_SI": "0"}):
            key = _rank_key("native")
            self.assertEqual(self.order(key), ["c", "e", "a", "b", "d"])
            r = self.recs()[0]
            o = r["objective"]
            self.assertEqual(
                key(r), (o[0], o[5], o[4], o[2], o[1], o[3], "a")
            )  # the pre-SI key, unchanged
        with mock.patch.dict(os.environ, {"PNR_SI": "1"}):
            self.assertEqual(self.order(_rank_key("native")), ["b", "a", "c", "e", "d"])
            self.assertEqual(self.order(_rank_key("deep")), ["b", "a", "c", "e", "d"])
            self.assertEqual(_rank_key("native")(dict(id="z")), (math.inf,) * 7 + ("z",))

    def test_synth_native_key_and_aggregate(self):
        from pnr.hier import synth_native as S

        with mock.patch.dict(os.environ, {"PNR_SI": "0", "PNR_POWER_FIRST": "0"}):
            self.assertEqual(self.order(S.rank_key), ["c", "e", "a", "b", "d"])
            r = self.recs()[0]
            o = r["objective"]
            self.assertEqual(
                S.rank_key(r), (o[5], o[0], o[1], o[2], o[3], o[4], math.inf, math.inf)
            )
        with mock.patch.dict(os.environ, {"PNR_SI": "1", "PNR_POWER_FIRST": "0"}):
            self.assertEqual(self.order(S.rank_key), ["b", "a", "c", "e", "d"])
        with mock.patch.dict(os.environ, {"PNR_SI": "1", "PNR_POWER_FIRST": "1"}):
            recs = self.recs()
            recs[1]["hot_loops_open"] = 1  # b: best SI but an open hot loop
            self.assertEqual(
                [r["id"] for r in sorted(recs, key=S.rank_key)], ["a", "c", "e", "b", "d"]
            )
            runs = [
                dict(self.recs()[0], status="ok", repeat=0, si_layout_failures=0),
                dict(self.recs()[0], status="ok", repeat=1, si_layout_failures=2),
            ]
            out = S.aggregate(runs)
            self.assertEqual(out["si_layout_failures"], 2)  # the worst repeat, like hot loops
            self.assertEqual([x["si_layout_failures"] for x in out["runs"]], [0, 2])
            runs[1]["si_layout_failures"] = None
            self.assertIsNone(S.aggregate(runs)["si_layout_failures"])
        with mock.patch.dict(os.environ, {"PNR_SI": "0", "PNR_POWER_FIRST": "1"}):
            runs = [
                dict(self.recs()[1], status="ok", repeat=0),
                dict(self.recs()[1], status="ok", repeat=1),
            ]
            self.assertEqual(
                sorted(S.aggregate(runs)["runs"][0]),
                ["dirs", "hot_loops_open", "objective", "repeat", "status"],
            )


@unittest.skipUnless(
    os.environ.get("PNR_SI_LIVE") == "1", "live prepare worker + design check (PNR_SI_LIVE=1)"
)
class LivePrepareTest(unittest.TestCase):
    """Real native_loop 'prepare' worker (headless KiCad python) + real design check on p027."""

    def test_prepare_worker_and_gate(self):
        from tests.test_si_live import board_path

        board = board_path()
        if not board.exists():
            self.skipTest("p027 board not found: %s" % board)
        ato = PNR_ROOT.parents[0] / "splanc_dev/elec/src/splanc_mini.ato"
        fab = PNR_ROOT.parents[0] / "splanc_dev/mini-routing-electrical-fab.json"
        out = Path(tmpdir(self, "pnr-si-prepare-")) / "policy"
        out.mkdir()
        env = dict(os.environ, PNR_SI="1", PYTHONPATH=str(PNR_ROOT))
        cmd = [
            X.kicad_python()[0],
            "-m",
            "pnr.native_loop",
            str(board),
            "--worker",
            "prepare",
            "--rules",
            str(board.parent.parent / "rules.json"),
            "--report",
            str(out / "prepare.json"),
            "--annotation-source",
            str(ato),
            "--electrical-fab",
            str(fab),
        ]
        from pnr.proc import run

        with open(out / "prepare.log", "wb") as log:
            code = run(cmd, timeout=600, env=env, stdout=log, stderr=log)
        self.assertEqual(code, 0, (out / "prepare.log").read_text()[-2000:])
        rules = json.loads((out / "prepare.json").read_text())
        self.assertEqual([i["name"] for i in rules["si_intents"]], ["led0_data", "led1_data"])
        self.assertEqual(
            [
                (i["driver"]["ref"], i["series"][0]["ref"], i["connector"]["ref"])
                for i in rules["si_intents"]
            ],
            [("U9", "R21", "CN1"), ("U12", "R26", "CN2")],
        )
        rep = R.gate_rules_file(
            out / "prepare.json",
            out_dir=out,
            estimates=out / "si-estimate.json",
            env=dict(os.environ, PNR_SI="1"),
            save_waves=False,
        )
        self.assertEqual((rep["status"], rep["summary"]["errors"]), ("pass", 0))
        self.assertEqual(rep["summary"]["estimate"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
