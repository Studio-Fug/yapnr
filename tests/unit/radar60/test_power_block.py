"""examples/radar60/board/power_block.py's pure-Python pieces (no KiCad, no engine): the stage
3c R1 power-stage subcell's block document, its datasheet hot-loop links and its ranking.

``block_doc`` turns the top board's constraints into the block board's: board-level relations
go (fixed poses, regions, the RF fixed block, U1's fanout, the LVDS pairs, the top In3
partition), groups and orientations are kept only inside the block, the floorplan's outer pours
come in with their region named by the block's parts, and only the rails whose every source and
sink is inside the block are IR-checked there. ``hot_links`` reads the floorplan's
``power_stage.hot_loops`` against the netlist. ``rank_key`` orders layouts by open hot-loop
links first, then unconnected items, violations, IR rails over budget, and only then area.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

EXAMPLE = Path(__file__).parents[3] / "examples/radar60"
sys.path.insert(0, str(EXAMPLE / "board"))

import power_block  # noqa: E402

FLOORPLAN = yaml.safe_load((EXAMPLE / "board/floorplan.yaml").read_text())
TOP = yaml.safe_load((EXAMPLE / "board/constraints.yaml").read_text())

BLOCK = [
    "pmic.u2",
    "pmic.l_b0",
    "pmic.l_b1",
    "pmic.l_b2",
    "pmic.l_b3",
    "pmic.c_in[0]",
    "pmic.c_in[1]",
    "pmic.c_in[2]",
    "pmic.c_in[3]",
    "pmic.r_snb0",
    "pmic.c_snb0",
    "pmic.c_vana",
    "pmic.r_sh",
    "pmic.tp_isns",
    "power_in.efuse",
    "power_in.tvs",
    "power_in.c_in_bulk",
    "power_in.c_in_hf",
    "power_in.c_sys_bulk",
    "power_in.r_uv_top",
]


def pad(name, net):
    return SimpleNamespace(name=name, net=net)


def part(ref, address, *pads):
    return SimpleNamespace(ref=ref, address=address, pads=[pad(n, net) for n, net in pads])


class BlockDocTest(unittest.TestCase):
    def setUp(self):
        self.doc = power_block.block_doc(TOP, FLOORPLAN, BLOCK, 20.4, 18.4)

    def test_board_level_relations_go(self):
        for key in ("region", "fixed_block", "fanout", "diff_pair", "length_match", "rf_macro"):
            self.assertNotIn(key, self.doc)
        self.assertEqual(self.doc["fixed"], {})
        self.assertEqual(self.doc["board"]["outline"], {"w": 20.4, "h": 18.4})
        # the net classes stay whole: the block routes its nets at the top board's widths
        self.assertEqual(self.doc["net_class"], TOP["net_class"])

    def test_groups_kept_only_inside(self):
        anchors = {g.get("anchor") for g in self.doc["group"]}
        self.assertIn("@pmic.u2", anchors)
        self.assertNotIn("@radio.u1", anchors)  # the radio's decoupling groups are not the block's
        for g in self.doc["group"]:
            for m in g["members"]:
                self.assertTrue(
                    any(power_block.fnmatch.fnmatchcase(a, m[1:]) for a in BLOCK), (g, m)
                )

    def test_outer_pour_replaces_the_top_partition(self):
        (pour,) = self.doc["plane_partition"]
        self.assertEqual(pour["layer"], "F.Cu")
        self.assertEqual((pour["terminals"], pour["connect"]), ("pad", "solid"))
        self.assertIn("5V_SYS", pour["nets"])
        self.assertIn("@pmic.u2", pour["region"]["refs"])
        self.assertIn("@power_in.efuse", pour["region"]["refs"])
        self.assertIn("@pmic.c_in[[]0]", pour["region"]["refs"])  # address globs are literal
        self.assertNotIn("In3.Cu", [p["layer"] for p in self.doc["plane_partition"]])

    def test_only_rails_inside_the_block(self):
        nets = sorted(r["net"] for r in self.doc["ir_drop"])
        # 1V0_BUCK (L_b2 -> R_SH1), 3V3 (L_b0 to its sense pin) and 5V_SYS (the eFuse output) are
        # inside; 1V0_SH runs to the
        # ferrites outside, 1V0_RF1 from them to U1.
        self.assertEqual(nets, ["1V0_BUCK", "3V3", "5V_SYS"])
        (buck,) = [r for r in self.doc["ir_drop"] if r["net"] == "1V0_BUCK"]
        self.assertEqual(buck["budget_mohm"], 0.5)
        self.assertEqual(buck["sources"], {"@pmic.l_b2": ["2"]})


class HotLinksTest(unittest.TestCase):
    def test_links_follow_the_floorplan_loops(self):
        comps = [
            part(
                "U2",
                "pmic.u2",
                ("9", "5V_SYS"),
                ("11", "GND"),
                ("10", "PMIC_SW_B0"),
                ("5", "I2C_SCL"),
            ),
            part("C14", "pmic.c_in[0]", ("1", "5V_SYS"), ("2", "GND")),
            part("L1", "pmic.l_b0", ("1", "PMIC_SW_B0"), ("2", "3V3")),
            part("R30", "pmic.r_snb0", ("1", "PMIC_SW_B0"), ("2", "snb0")),
            part("U5", "power_in.efuse", ("5", "VIN_5V"), ("6", "5V_SYS"), ("8", "GND")),
            part("D3", "power_in.tvs", ("1", "VIN_5V"), ("2", "GND")),
        ]
        links = power_block.hot_links(FLOORPLAN, comps)
        got = sorted((net, src[0], dst[0][0]) for net, src, dst in links)
        self.assertEqual(
            got,
            [
                ("5V_SYS", ("C14", "1"), "U2"),
                ("GND", ("C14", "2"), "U2"),
                ("GND", ("D3", "2"), "U5"),
                ("PMIC_SW_B0", ("L1", "1"), "U2"),
                ("PMIC_SW_B0", ("R30", "1"), "U2"),
                ("VIN_5V", ("D3", "1"), "U5"),
            ],
        )


class RankTest(unittest.TestCase):
    def rec(self, **kw):
        base = dict(
            status="ok",
            id="x",
            hot_links_open=0,
            unconnected=0,
            violations=0,
            ir_fail=0,
            ir_excess=0.0,
            area=400.0,
            port_debt_mm=10.0,
        )
        base.update(kw)
        return base

    def test_hot_loops_before_ir_before_area(self):
        small_open = self.rec(id="a", hot_links_open=1, area=200.0)
        ir_fail = self.rec(id="b", ir_fail=1, ir_excess=3.0, area=250.0)
        big_clean = self.rec(id="c", area=500.0)
        failed = dict(status="failed", id="d")
        order = sorted([small_open, ir_fail, big_clean, failed], key=power_block.rank_key)
        self.assertEqual([r["id"] for r in order], ["c", "b", "a", "d"])


if __name__ == "__main__":
    unittest.main()
