"""Opt-in end-to-end SI on the H6 best routed board p027 (PNR_SI_LIVE=1).

Real pcbnew read (headless KiCad python), real KIBIS conversion (headless kicad-cli,
PNR_KICAD_CLI), real libngspice. One corner (typ driver, 1 m lead) for both LED
channels, with the 100 ohm termination (user decision 2026-09-29, simulated as an
override because p027 was routed with the 33 ohm part) and with the 33 ohm part it
carries. The board is referenced by path (1.5 MB, not frozen; PNR_SI_E2E_BOARD overrides).
Vendor IBIS: fetched into PNR_SI_CACHE (or a temp cache) by URL + sha256;
PNR_SI_VENDOR_SEED can point at a local copy.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_si_extract import p027_intents

from pnr.si import report as R

TESTDATA = Path(__file__).resolve().parents[1] / "testdata/si"
HIER = Path(__file__).resolve().parents[4]


def board_path():
    src = json.loads((TESTDATA / "SOURCE.json").read_text())
    return Path(
        os.environ.get("PNR_SI_E2E_BOARD") or HIER / src["board"].split("output/hier/", 1)[1]
    )


@unittest.skipUnless(os.environ.get("PNR_SI_LIVE") == "1", "live SI end-to-end (PNR_SI_LIVE=1)")
class P027LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        board = board_path()
        if not board.exists():
            raise unittest.SkipTest("p027 board not found: %s" % board)
        cls.board = board
        cls._tmp = tempfile.TemporaryDirectory(prefix="pnr-si-live-")  # removed in tearDownClass
        cls.cache = os.environ.get("PNR_SI_CACHE") or cls._tmp.name
        rules = json.loads((board.parent.parent / "evaluated-rules.json").read_text())
        cls.rules = rules
        cls.intents = p027_intents()
        cls.reports, cls.wall = {}, {}
        for ohm in (100, 33):
            t0 = time.monotonic()
            cls.reports[ohm] = R.post_route_report(
                board,
                rules,
                intents=cls.intents,
                series_ohm=ohm,
                cache=cls.cache,
                corners=[("typ", 1.0)],
            )
            cls.wall[ohm] = time.monotonic() - t0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def metrics(self, ohm, name):
        req = next(r for r in self.reports[ohm]["requirements"] if r["name"] == name)
        self.assertEqual(len(req["results"]), 1)
        return req, req["results"][0]

    def test_extraction_and_status(self):
        for ohm in (100, 33):
            rep = self.reports[ohm]
            self.assertEqual(rep["summary"]["errors"], 0, json.dumps(rep, indent=1)[:2000])
            self.assertEqual(rep["summary"]["failed"], 0)
            req, _ = self.metrics(ohm, "led0_data")
            self.assertEqual([g["vias"] for g in req["geometry"]], [3, 0])
            req, _ = self.metrics(ohm, "led1_data")
            self.assertEqual([g["vias"] for g in req["geometry"]], [2, 1])
            self.assertLess(self.wall[ohm], 120)

    def test_100_ohm_edges(self):
        # design sec. 12 scratch (100 ohm, 120 ohm / 1 m): 3.29 ns, 5.36 V at the pixel
        for name in ("led0_data", "led1_data"):
            req, x = self.metrics(100, name)
            m = x["metrics"]
            self.assertEqual(x["status"], "pass")
            self.assertAlmostEqual(m["rise_10_90_ns"], 3.29, delta=3.29 * 0.15)
            self.assertLess(m["vmax_v"], 5.5)
            self.assertEqual(m["nonmonotonic_edges"], 0)
            self.assertEqual(x["report_violations"], [])
            self.assertEqual(x["series"][0]["ohm"], 100.0)

    def test_33_ohm_edges_overshoot_is_report_only(self):
        # design sec. 12 scratch (33 ohm, 120 ohm / 1 m): led0 1.56 ns / led1 1.60 ns, ~6.1 V
        for name, rise in (("led0_data", 1.56), ("led1_data", 1.60)):
            req, x = self.metrics(33, name)
            m = x["metrics"]
            self.assertAlmostEqual(m["rise_10_90_ns"], rise, delta=rise * 0.15)
            self.assertGreater(m["overshoot_v"], 0.5)
            self.assertIn("overshoot_v", [v["metric"] for v in x["report_violations"]])
            self.assertEqual(x["status"], "pass")  # overshoot does not gate (uncalibrated)
            self.assertGreater(m["conn_rise_10_90_ns"], 5.0)  # connector-side staircase
        self.assertEqual(R.side_fields(self.reports[33])["si_layout_failures"], 0)

    def test_real_classification_with_gated_overshoot(self):
        """With overshoot made a gate (test-only models copy), 33 ohm on led0 shows both classes:
        0 m - the routed board rings above VDD + 0.5 V, the ideal board does not -> layout;
        1 m - the cable reflection overshoots on the ideal board too -> design."""
        import shutil

        from pnr.si import models

        with tempfile.TemporaryDirectory() as d:
            for f in models.MODELS_DIR.glob("*.json"):
                shutil.copy(f, d)
            p = Path(d) / "profile-ws2812b-din.json"
            prof = json.loads(p.read_text())
            prof["limits"]["overshoot_v"]["gate"] = True
            p.write_text(json.dumps(prof))
            led0 = [i for i in self.intents if i["name"] == "led0_data"]
            rep = R.post_route_report(
                self.board,
                self.rules,
                intents=led0,
                series_ohm=33,
                cache=self.cache,
                library=models.Library(d),
                corners=[("typ", 0.0), ("typ", 1.0)],
            )
        res = {x["cable_m"]: x for x in rep["requirements"][0]["results"]}
        self.assertEqual((res[0.0]["status"], res[0.0]["cls"]), ("fail", "layout"))
        self.assertEqual(res[0.0]["reference"]["status"], "pass")
        self.assertEqual((res[1.0]["status"], res[1.0]["cls"]), ("fail", "design"))
        self.assertEqual(rep["requirements"][0]["cause"], "layout")
        self.assertEqual(R.side_fields(rep)["si_layout_failures"], 1)


if __name__ == "__main__":
    unittest.main()
