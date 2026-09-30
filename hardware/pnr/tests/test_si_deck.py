"""ngspice deck generation: golden deck, cable/no-cable, series override, sections, hashing."""

import copy
import json
import re
import unittest
from pathlib import Path

from pnr.si import deck as D
from pnr.si import extract as X
from pnr.si import models, physics
from tests.test_si_extract import p027_intents

TESTDATA = Path(__file__).resolve().parents[1] / "testdata/si"
DRV = dict(subckt="U1.SN74LVC1T45_DBV", rail_v=5.0, sha256="0" * 64)


class DeckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = models.Library()
        cls.st = physics.stackup({})
        cls.intents = {i["name"]: i for i in p027_intents()}
        cls.dump = json.loads((TESTDATA / "p027-led-dump.json").read_text())

    def build(self, name="led1_data", geometry=None, **kw):
        it = self.intents[name]
        geo = geometry or X.routed_geometry(self.dump, it, self.st)
        kw.setdefault("corner", "typ")
        kw.setdefault("cable_m", 1.0)
        kw.setdefault("window", 90.0)
        return D.build(it, geo, driver=kw.pop("driver", DRV), library=self.lib, st=self.st, **kw)

    def test_golden_led1_typ_1m(self):
        tpl, meta = self.build()
        self.assertEqual(tpl, (TESTDATA / "golden-led1-typ-1m.cir").read_text())
        self.assertEqual(meta["nodes"], ["drv", "conn", "rx"])
        self.assertAlmostEqual(meta["t_fall_s"], 92e-9)
        self.assertAlmostEqual(meta["tstop_s"], 182e-9)
        self.assertAlmostEqual(meta["vih_v"], 3.5)
        self.assertAlmostEqual(meta["vil_v"], 1.5)
        self.assertEqual(len(meta["vias"]), 3)
        self.assertEqual(meta["series"][0]["ohm"], 33.0)

    def test_board_is_lumped_only_cable_is_a_line(self):
        tpl, meta = self.build()
        tlines = [l for l in tpl.splitlines() if re.match(r"^T\w+ ", l)]
        self.assertEqual(tlines, ["TCAB0 cabs0_3 0 rx 0 Z0=120 TD=4.90535e-09"])
        # per metre: R_dc 0.168 ohm + 4 R||L skin-effect stages in front of the lossless T
        self.assertIn("RCAB0 n13 cabr0 0.167736", tpl)
        self.assertEqual(len([l for l in tpl.splitlines() if l.startswith(("RSK0_", "LSK0_"))]), 8)
        self.assertEqual(
            (meta["cable_r_ohm"], meta["cable_segments"], meta["cable_z0_ohm"], meta["cable_loss"]),
            (6.3497, 1, 120.0, "skin_ladder"),
        )
        self.assertIn(".tran 10p 182n 0 25p", tpl)
        tpl0, meta0 = self.build(cable_m=0.0, window=40.0)
        self.assertNotIn("TCAB", tpl0)
        self.assertNotIn("RSK", tpl0)
        self.assertIn("L10 conn rx 5e-09", tpl0)  # connector straight into the pixel
        self.assertEqual(meta0["cable_td_ns"], 0.0)

    def with_cable(self, **loss):
        lib = copy.deepcopy(self.lib)
        cable = copy.deepcopy(lib.get("cable", "awg24-jst-ph"))
        if loss.get("loss") is None and "loss" in loss:
            cable.pop("loss")
        else:
            cable["loss"].update(loss)
        lib._by[("cable", "awg24-jst-ph")] = dict(lib._by[("cable", "awg24-jst-ph")], data=cable)
        it = self.intents["led1_data"]
        return D.build(
            it,
            X.routed_geometry(self.dump, it, self.st),
            driver=DRV,
            library=lib,
            st=self.st,
            corner="typ",
            cable_m=1.0,
            window=90.0,
        )

    def test_cable_corners_segments_and_loss_options(self):
        tpl, meta = self.build(cable_m=3.0, window=190.0, cable_z0=150.0)
        self.assertIn("cable=3m z0=150", tpl.splitlines()[0])
        tl = [l for l in tpl.splitlines() if l.startswith("TCAB")]
        self.assertEqual(len(tl), 3)  # one T per metre
        self.assertTrue(all("Z0=150 TD=4.90535e-09" in l for l in tl))
        self.assertEqual(len([l for l in tpl.splitlines() if l.startswith("RCAB")]), 3)
        self.assertEqual(len([l for l in tpl.splitlines() if l.startswith("RSK")]), 12)
        self.assertAlmostEqual(meta["cable_r_ohm"], 3 * 6.34967, places=3)
        self.assertEqual(meta["cable_z0_ohm"], 150.0)
        tpl, meta = self.with_cable(model="flat")  # one frequency-flat R per segment
        self.assertEqual(
            [l for l in tpl.splitlines() if l.startswith(("TCAB", "RCAB", "RSK"))],
            ["RCAB0 n13 cabr0 6.34967", "TCAB0 cabr0 0 rx 0 Z0=120 TD=4.90535e-09"],
        )
        self.assertEqual(meta["cable_loss"], "flat")
        tpl, meta = self.with_cable(loss=None)
        self.assertEqual(
            [l for l in tpl.splitlines() if l.startswith(("TCAB", "RCAB", "RSK"))],
            ["TCAB n13 0 rx 0 Z0=120 TD=4.90535e-09"],
        )  # the v1 lossless line
        self.assertEqual((meta["cable_r_ohm"], meta["cable_loss"]), (0.0, None))

    def test_series_override_and_ideal_board(self):
        tpl, meta = self.build(series_ohm=100)
        self.assertIn("RS0 n8 n9 100", tpl)
        self.assertTrue(meta["series"][0]["overridden"])
        ideal = X.ideal_geometry(self.intents["led1_data"])
        tpl, meta = self.build(geometry=ideal)
        self.assertIn("RS0 drv n4 33", tpl)  # driver pad straight into the resistor
        self.assertEqual(meta["segments"], [])
        self.assertEqual(meta["vias"], [])
        self.assertIn("L1 n4 conn 4e-10", tpl)  # ESL lands on the connector pad

    def test_long_segments_are_split_into_sections(self):
        it = self.intents["led1_data"]
        geo = X.ideal_geometry(it)
        geo["legs"][1]["elements"] = [
            dict(kind="seg", layer="F.Cu", width_mm=0.2, length_mm=30.0, n_merged=1)
        ]
        tpl, meta = self.build(geometry=geo)
        td = physics.line(self.st, "F.Cu", 0.2)["td_ps_per_mm"] * 30
        self.assertEqual(meta["segments"][0]["sections"], 4)  # ceil(173 ps / 50 ps)
        self.assertAlmostEqual(meta["segments"][0]["td_ps"], td, places=1)
        tpl2, meta2 = self.build(geometry=geo, max_section_ps=1000.0)
        self.assertEqual(meta2["segments"][0]["sections"], 1)

    def test_windows_and_hash(self):
        prof, cable = self.lib.get("profile", "ws2812b_din"), self.lib.get("cable", "awg24-jst-ph")
        self.assertEqual(
            [D.window_ns(prof, cable, m) for m in (0.0, 1.0, 3.0)], [40.0, 90.0, 190.0]
        )
        tpl, _ = self.build()
        h = D.deck_hash(tpl, DRV["sha256"])
        self.assertEqual(h, D.deck_hash(tpl, DRV["sha256"]))
        self.assertNotEqual(h, D.deck_hash(tpl, "1" * 64))
        self.assertIn('.include "/x/driver.lib"', D.materialize(tpl, "/x/driver.lib"))
        self.assertNotIn(D.PLACEHOLDER, D.materialize(tpl, "/x/driver.lib"))

    def test_shunt_pad_and_open_leg(self):
        it = copy.deepcopy(self.intents["led0_data"])
        geo = X.ideal_geometry(it)
        geo["legs"][1]["attach"].append(dict(at=0, kind="pad", ref="TP1", pad="11"))
        tpl, meta = self.build("led0_data", geometry=geo)
        self.assertIn("C1 conn 0 3e-13", tpl)  # zero-length leg: pad sits on the connector node
        self.assertTrue(meta["warnings"])
        geo["legs"][0]["status"] = "open"
        with self.assertRaises(ValueError):
            self.build("led0_data", geometry=geo)


if __name__ == "__main__":
    unittest.main()
