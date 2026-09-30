"""SI orchestration with a fake driver model and fake simulator: classification, waivers, flags, schema."""

import atexit
import copy
import json
import math
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_si_annotations import fixture_components

from pnr.si import SCHEMA
from pnr.si import annotations as A
from pnr.si import report as R

TESTDATA = Path(__file__).resolve().parents[1] / "testdata/si"
# Every temporary file/dir of these tests lives under one directory removed at interpreter exit
# (TemporaryDirectory's finalizer), plus per-test directories removed by addCleanup.
_SCRATCH = tempfile.TemporaryDirectory(prefix="pnr-si-test-")
atexit.register(_SCRATCH.cleanup)


def tmpdir(test, prefix):
    """A fresh directory removed when ``test`` finishes."""
    d = tempfile.mkdtemp(prefix=prefix, dir=_SCRATCH.name)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def cases(profile_corners=None):
    """The default sweep: 3 driver corners x (0 m + 1 m and 3 m at 3 cable impedances)."""
    return {
        (c, m, z)
        for c in ("typ", "min", "max")
        for m, zs in ((0.0, (120.0,)), (1.0, (120.0, 100.0, 150.0)), (3.0, (120.0, 100.0, 150.0)))
        for z in zs
    }


def fake_kibis(manifest, pin, corner, window_ns, td_ns, rail_v=None, cache=None, env=None, **kw):
    rail = manifest["corners"][corner]["rail_v"]
    return dict(
        path="/fake/driver-%s-%g.lib" % (corner, window_ns),
        sha256=("%s%g" % (corner, window_ns)).ljust(64, "0"),
        subckt="U1.SN74LVC1T45_DBV",
        pins=["GND", "PIN"],
        rail_v=rail,
        vendor_sha256="v" * 64,
        cached=True,
    )


class FakeSim:
    """Waveforms from the deck text: RC edges; tau chosen by ``tau(deck)``; None -> error."""

    def __init__(self, tau):
        self.tau = tau
        self.calls = 0

    def __call__(
        self, deck_text, nodes, tstop=None, timeout=None, work_dir=None, check=None, env=None, **kw
    ):
        self.calls += 1
        tau = self.tau(deck_text)
        if tau is None:
            return dict(
                status="error", error="fake: singular matrix", attempts=[{}, {}], runtime_s=0.01
            )
        rail = float(re.search(r"^VRX vrx 0 (\S+)", deck_text, re.M).group(1))
        tstop_ns = float(re.search(r"^\.tran \S+ (\S+)n", deck_text, re.M).group(1))
        w = (tstop_ns - 2.0) / 2
        t = [i * 2e-10 for i in range(int(tstop_ns / 0.2) + 1)]

        def edge(x, tau):
            if x < 2e-9:
                return 0.0
            if x < (2 + w) * 1e-9:
                return rail * (1 - math.exp(-(x - 2e-9) / tau))
            hi = rail * (1 - math.exp(-w * 1e-9 / tau))
            return hi * math.exp(-(x - (2 + w) * 1e-9) / tau)

        v = dict(
            drv=[edge(x, 2e-10) for x in t],
            conn=[edge(x, tau) for x in t],
            rx=[edge(x, tau) for x in t],
        )
        if check:
            why = check(t, v)
            if why:
                return dict(status="error", error=why, attempts=[{}], runtime_s=0.01)
        return dict(status="ok", t=t, v=v, attempts=[{}], runtime_s=0.01)


def intents(extra=()):
    lines = (TESTDATA / "p027-si.ato").read_text().splitlines()[1:] + list(extra)
    f = tempfile.NamedTemporaryFile("w", suffix=".ato", delete=False, dir=_SCRATCH.name)
    f.write("\n".join(lines) + "\n")
    f.close()
    reqs, waivers = A.parse([f.name])
    from pnr.si import models

    return A.resolve(reqs, waivers, fixture_components(), models.Library(), env={}), f.name


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.cache = tmpdir(self, "pnr-si-cache-")
        self.dump = json.loads((TESTDATA / "p027-led-dump.json").read_text())
        p = mock.patch("pnr.si.models.kibis_driver", fake_kibis)
        p.start()
        self.addCleanup(p.stop)

    def post(self, sim, its, **kw):
        with mock.patch("pnr.si.runner.run_deck", sim):
            return R.post_route_report(
                "board.kicad_pcb", {}, intents=its, dump=self.dump, cache=self.cache, env={}, **kw
            )

    def test_all_pass_schema_and_side_fields(self):
        its, _ = intents()
        with tempfile.TemporaryDirectory() as out:
            rep = self.post(FakeSim(lambda d: 1e-9), its, out_dir=out)
            self.assertTrue((Path(out) / "si.json").exists())
            self.assertTrue(
                list((Path(out) / "waves/led0_data").glob("routed-typ-1m.json"))
            )  # nominal Z0
            self.assertTrue(list((Path(out) / "waves/led0_data").glob("routed-typ-3m-z150.json")))
        self.assertEqual(rep["schema"], SCHEMA)
        self.assertEqual(rep["mode"], "post-route")
        s = rep["summary"]
        self.assertEqual((s["requirements"], s["passed"], s["decks"], s["errors"]), (2, 2, 42, 0))
        r0 = rep["requirements"][0]
        self.assertEqual(len(r0["results"]), 21)  # 3 driver corners x (0 m + 2 lengths x 3 Z0)
        x = r0["results"][0]
        for key in (
            "corner",
            "cable_m",
            "cable_z0_ohm",
            "cable_r_ohm",
            "status",
            "cls",
            "metrics",
            "deck_sha256",
            "models",
            "rail_v",
            "vih_v",
            "runtime_s",
        ):
            self.assertIn(key, x)
        self.assertEqual(
            set(x["models"]),
            {
                "profile",
                "parts",
                "receiver",
                "cable",
                "connector",
                "driver_lib",
                "vendor_ibis",
                "driver_manifest",
            },
        )
        self.assertEqual(
            {(y["corner"], y["cable_m"], y["cable_z0_ohm"]) for y in r0["results"]}, cases()
        )
        self.assertEqual({y["rail_v"] for y in r0["results"]}, {4.5, 5.0, 5.5})
        self.assertEqual(r0["geometry"][0]["vias"], 3)
        self.assertEqual(R.side_fields(rep)["si_layout_failures"], 0)
        # second run: every deck comes from the sim cache
        sim = FakeSim(lambda d: 1e-9)
        rep2 = self.post(sim, its)
        self.assertEqual(sim.calls, 0)
        self.assertEqual(rep2["summary"]["cached_decks"], 42)

    def test_cache_key_covers_thresholds_and_models(self):
        """Receiver thresholds are not in the deck text: changing them must not reuse cached metrics."""
        from pnr.si import models

        its, _ = intents()
        its = its[:1]
        corner = [("typ", 1.0)]
        lib = models.Library()
        first = self.post(FakeSim(lambda d: 1e-9), its, corners=corner, library=lib)[
            "requirements"
        ][0]["results"][0]
        self.assertEqual((first["cached"], first["vih_v"]), (False, 3.5))
        again = self.post(FakeSim(lambda d: 1e-9), its, corners=corner, library=lib)[
            "requirements"
        ][0]["results"][0]
        self.assertTrue(again["cached"])
        moved = copy.deepcopy(lib)
        rx = copy.deepcopy(moved.get("receiver", "ws2812b-classic"))
        rx["vih_frac_vdd"] = 0.99
        moved._by[("receiver", "ws2812b-classic")] = dict(
            moved._by[("receiver", "ws2812b-classic")], data=rx, sha256="f" * 64
        )
        sim = FakeSim(lambda d: 1e-9)
        x = self.post(sim, its, corners=corner, library=moved)["requirements"][0]["results"][0]
        self.assertEqual((sim.calls, x["cached"], x["vih_v"]), (1, False, 4.95))
        self.assertGreater(
            x["metrics"]["delay_ns"], first["metrics"]["delay_ns"]
        )  # measured at the new VIH
        # the same deck with only the thresholds changed (model hash kept): still a new key
        moved._by[("receiver", "ws2812b-classic")]["sha256"] = lib.entry(
            "receiver", "ws2812b-classic"
        )["sha256"]
        sim = FakeSim(lambda d: 1e-9)
        x = self.post(sim, its, corners=corner, library=moved)["requirements"][0]["results"][0]
        self.assertEqual((sim.calls, x["vih_v"]), (1, 4.95))

    def test_poured_planes_become_references(self):
        its, _ = intents()
        corner = [("typ", 1.0)]
        rep = self.post(FakeSim(lambda d: 1e-9), its[:1], corners=corner)
        self.assertEqual(rep["stackup"]["planes"], ["In1.Cu"])
        self.dump = dict(
            self.dump,
            board_area_mm2=100.0,
            pours=[
                dict(net="GND", layer="B.Cu", filled_mm2=70.0),
                dict(net="lv", layer="F.Cu", filled_mm2=20.0),
            ],
        )
        rep2 = self.post(FakeSim(lambda d: 1e-9), its[:1], corners=corner)
        self.assertEqual(
            rep2["stackup"]["planes"], ["In1.Cu", "B.Cu"]
        )  # 70 % >= 50 %, F.Cu 20 % is not
        self.assertEqual(rep2["stackup"]["pour_coverage"], {"B.Cu": 0.7, "F.Cu": 0.2})
        seg = lambda r: [s for s in r["requirements"][0]["results"][0]["deck_sha256"]]
        self.assertNotEqual(seg(rep), seg(rep2))  # In2 copper now a stripline
        self.assertEqual(R.board_planes(dict(board_area_mm2=0)), {})

    def test_layout_design_error_classes(self):
        its, _ = intents()
        slow = 20e-9  # 10-90 = 44 ns > 25 ns limit
        # routed decks carry '* seg' lines, the ideal reference decks do not
        rep = self.post(FakeSim(lambda d: slow if "* seg " in d else 1e-9), its)
        self.assertEqual(rep["summary"]["layout_failures"], 2)
        r0 = rep["requirements"][0]
        self.assertEqual((r0["status"], r0["cause"]), ("fail", "layout"))
        self.assertTrue(
            all(x["cls"] == "layout" and x["reference"]["status"] == "pass" for x in r0["results"])
        )
        self.assertEqual(r0["worst"]["metric"], "fall_10_90_ns")
        self.assertEqual(R.side_fields(rep)["si_layout_failures"], 2)
        self.cache = tmpdir(
            self, "pnr-si-cache-"
        )  # the sim cache trusts deck hashes: new fake, new cache
        rep = self.post(FakeSim(lambda d: slow), its)
        self.assertEqual(
            (rep["summary"]["layout_failures"], rep["summary"]["design_failures"]), (0, 2)
        )
        self.assertEqual(
            R.side_fields(rep)["si_layout_failures"], 0
        )  # design failures never count against a layout
        self.cache = tmpdir(self, "pnr-si-cache-")
        rep = self.post(FakeSim(lambda d: None if "cable=3m" in d else 1e-9), its)
        self.assertEqual(rep["summary"]["errors"], 2)
        self.assertEqual(rep["requirements"][0]["status"], "error")
        self.assertEqual(sum(x["status"] == "error" for x in rep["requirements"][0]["results"]), 9)

    def test_open_net_is_a_layout_failure_without_simulation(self):
        its, _ = intents()
        self.dump = dict(
            self.dump, tracks=[t for t in self.dump["tracks"] if t["net"] != "data_out"]
        )
        sim = FakeSim(lambda d: 1e-9)
        rep = self.post(sim, its[:1])
        self.assertEqual(sim.calls, 0)
        self.assertEqual(rep["requirements"][0]["cause"], "layout")
        self.assertEqual(
            rep["requirements"][0]["results"][0]["gate_failures"][0]["metric"], "copper_path"
        )

    def test_extraction_failure_fails_closed(self):
        its, _ = intents()
        with mock.patch("pnr.si.extract.read_board", side_effect=RuntimeError("pcbnew crashed")):
            rep = R.post_route_report("board.kicad_pcb", {}, intents=its, cache=self.cache, env={})
        self.assertEqual(rep["summary"]["errors"], 2)
        self.assertEqual(R.side_fields(None)["si_errors"], 1)
        self.assertIsNone(R.side_fields(None)["si_layout_failures"])

    def test_series_override_recorded(self):
        its, _ = intents()
        rep = self.post(
            FakeSim(lambda d: 1e-9 if re.search(r"^RS0 \S+ \S+ 100$", d, re.M) else None),
            its,
            series_ohm=100,
        )
        self.assertEqual(rep["overrides"], {"series_ohm": 100})
        self.assertEqual(rep["summary"]["errors"], 0)
        self.assertTrue(rep["requirements"][0]["results"][0]["series"][0]["overridden"])


class CandidateHookTest(unittest.TestCase):
    def test_flag_gated_and_never_raises(self):
        with mock.patch(
            "pnr.si.report.post_route_report", side_effect=AssertionError("must not run")
        ):
            self.assertEqual(R.candidate_side_fields("b", {}, out_dir="x", env={}), {})
        with mock.patch("pnr.si.report.post_route_report", side_effect=RuntimeError("boom")):
            out = R.candidate_side_fields("b", {}, out_dir="x", env={"PNR_SI": "1"})
        self.assertEqual((out["si_errors"], out["si_layout_failures"]), (1, None))
        self.assertIn("boom", out["si_error"])
        fake = dict(
            summary=dict(layout_failures=1, design_failures=0, errors=0, requirements=2),
            path="/r/si.json",
        )
        with mock.patch("pnr.si.report.post_route_report", return_value=fake):
            out = R.candidate_side_fields("b", {}, out_dir="x", env={"PNR_SI": "1"})
        self.assertEqual(set(out), set(R.SIDE_FIELDS))
        self.assertEqual(
            (out["si_layout_failures"], out["si_design_failures"], out["si_errors"]), (1, 0, 0)
        )
        self.assertEqual(
            (out["si_summary"]["requirements"], out["si_summary"]["report"]), (2, "/r/si.json")
        )


class CompileTest(unittest.TestCase):
    def setUp(self):
        self.cache = tmpdir(self, "pnr-si-cache-")
        p = mock.patch("pnr.si.models.kibis_driver", fake_kibis)
        p.start()
        self.addCleanup(p.stop)

    def test_flag_off_is_identity(self):
        rules = dict(fab={"track_width_mm": 0.2})
        broken = tempfile.NamedTemporaryFile("w", suffix=".ato", delete=False, dir=_SCRATCH.name)
        broken.write("# @pnr-si {not json\n")
        broken.close()
        out = R.compile_rules(rules, [broken.name], [], env={})
        self.assertIs(out, rules)
        self.assertEqual(rules, dict(fab={"track_width_mm": 0.2}))
        out = R.compile_rules(rules, [broken.name], [], env={"PNR_SI": "0"})
        self.assertIs(out, rules)

    def test_flag_on_adds_intents_and_design_check(self):
        _, path = intents()
        with mock.patch("pnr.si.runner.run_deck", FakeSim(lambda d: 1e-9)):
            out = R.compile_rules(
                {"x": 1}, [path], fixture_components(), env={"PNR_SI": "1"}, cache=self.cache
            )
        self.assertEqual(out["x"], 1)
        self.assertEqual([i["name"] for i in out["si_intents"]], ["led0_data", "led1_data"])
        self.assertEqual(out["si_design"]["status"], "pass")
        json.dumps(out)

    def test_design_failure_fails_compile_unless_waived(self):
        _, path = intents()
        slow_3m = FakeSim(lambda d: 20e-9 if "cable=3m" in d else 1e-9)
        with mock.patch("pnr.si.runner.run_deck", slow_3m):
            with self.assertRaises(R.SIDesignError) as ctx:
                R.compile_rules(
                    {}, [path], fixture_components(), env={"PNR_SI": "1"}, cache=self.cache
                )
        self.assertEqual(
            ctx.exception.report["summary"]["unwaived_failures"], 18
        )  # 2 x 3 corners x 3 Z0 at 3 m
        _, path = intents(
            [
                '# @pnr-si-waiver {"name":"led0_data","reason":"no 3 m leads on led0 (test)","cable_m":[3]}',
                '# @pnr-si-waiver {"name":"led1_data","reason":"no 3 m leads on led1 (test)","cable_m":[3],'
                '"metrics":["rise_10_90_ns","fall_10_90_ns"]}',
            ]
        )
        with mock.patch("pnr.si.runner.run_deck", slow_3m):
            out = R.compile_rules(
                {}, [path], fixture_components(), env={"PNR_SI": "1"}, cache=self.cache
            )
        self.assertEqual(out["si_design"]["status"], "waived")
        self.assertEqual(len(out["si_design"]["waived"]), 2)
        self.assertEqual(len(out["si_design"]["waived"][0]["cases"]), 9)
        # a waiver narrowed to the 150 ohm cable corner leaves the other impedances failing
        _, path = intents(
            [
                '# @pnr-si-waiver {"name":"led0_data","reason":"150 ohm leads only (test)","cable_m":[3],'
                '"cable_z0_ohm":[150]}',
                '# @pnr-si-waiver {"name":"led1_data","reason":"no 3 m leads on led1 (test)","cable_m":[3]}',
            ]
        )
        with mock.patch("pnr.si.runner.run_deck", slow_3m):
            with self.assertRaises(R.SIDesignError) as ctx:
                R.compile_rules(
                    {}, [path], fixture_components(), env={"PNR_SI": "1"}, cache=self.cache
                )
        rep = ctx.exception.report
        self.assertEqual(rep["summary"]["unwaived_failures"], 6)  # led0 at 120 and 100 ohm
        self.assertIn("Z0 1", str(ctx.exception))

    def test_errors_fail_compile(self):
        _, path = intents()
        with mock.patch("pnr.si.runner.run_deck", FakeSim(lambda d: None)):
            with self.assertRaises(R.SIDesignError):
                R.compile_rules(
                    {}, [path], fixture_components(), env={"PNR_SI": "1"}, cache=self.cache
                )
        with self.assertRaises(A.AnnotationError):
            bad = tempfile.NamedTemporaryFile("w", suffix=".ato", delete=False, dir=_SCRATCH.name)
            bad.write(
                '# @pnr-si {"name":"a","profile":"ws2812b_din","driver":"nope:4","series":"x","connector":"y:2"}\n'
            )
            bad.close()
            R.compile_rules(
                {}, [bad.name], fixture_components(), env={"PNR_SI": "1"}, cache=self.cache
            )


if __name__ == "__main__":
    unittest.main()
