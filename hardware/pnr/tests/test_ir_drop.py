"""The rail IR-drop solver (pnr.ir_drop) against analytic resistances."""

import math
import os
import tempfile
import unittest

from pnr.ir_drop import barrel_ohm, resistivity, solve

T_OUT = 0.035
T_IN = 0.0175
LAYERS = [
    {"name": "F.Cu", "z_mm": 0.0, "copper_mm": T_OUT},
    {"name": "In1.Cu", "z_mm": 0.3, "copper_mm": T_IN},
    {"name": "B.Cu", "z_mm": 1.0, "copper_mm": T_OUT},
]
RHO = resistivity(25.0)


def dot(ref, at, layer="F.Cu", size=0.02, pad="1", **extra):
    """A pad smaller than a cell: one copper cell, its own node."""
    x, y = at
    h = size / 2
    return dict(
        ref=ref,
        pad=pad,
        at=list(at),
        layers=[layer],
        polygon=[[x - h, y - h], [x + h, y - h], [x + h, y + h], [x - h, y + h]],
        **extra,
    )


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def circle(c, r, n=720):
    return [
        [c[0] + r * math.cos(2 * math.pi * k / n), c[1] + r * math.sin(2 * math.pi * k / n)]
        for k in range(n)
    ]


def copper(**parts):
    return dict(
        net="V",
        layers=LAYERS,
        **{k: parts.get(k, []) for k in ("zones", "tracks", "arcs", "vias", "pads")},
    )


def ohms(report):
    return report["r_eff_mohm"] / 1e3


class TrackTest(unittest.TestCase):
    def test_a_track_is_rho_l_over_wt(self):
        c = copper(
            pads=[dot("S", (0.0, 0.0)), dot("L", (10.0, 0.0))],
            tracks=[dict(layer="F.Cu", a=[0.0, 0.0], b=[10.0, 0.0], width_mm=0.2)],
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0)
        expect = RHO * 10.0 / (0.2 * T_OUT)
        self.assertEqual(r["status"], "pass")
        self.assertAlmostEqual(ohms(r), expect, delta=expect * 1e-3)
        self.assertLessEqual(r["residual"], 1e-10)
        self.assertAlmostEqual(r["loss_w"], expect, delta=expect * 1e-3)  # I = 1 A

    def test_two_parallel_tracks_halve_it(self):
        c = copper(
            pads=[dot("S", (0.0, 0.0)), dot("L", (10.0, 0.0))],
            tracks=[
                dict(layer="F.Cu", a=[0.0, 0.0], b=[5.0, 3.0], width_mm=0.2),
                dict(layer="F.Cu", a=[5.0, 3.0], b=[10.0, 0.0], width_mm=0.2),
                dict(layer="F.Cu", a=[0.0, 0.0], b=[5.0, -3.0], width_mm=0.2),
                dict(layer="F.Cu", a=[5.0, -3.0], b=[10.0, 0.0], width_mm=0.2),
            ],
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0)
        one = 2 * RHO * math.hypot(5.0, 3.0) / (0.2 * T_OUT)
        self.assertAlmostEqual(ohms(r), one / 2, delta=one * 1e-3)

    def test_a_tree_equals_its_path_sums(self):
        # Source -> junction -> two sinks (the trial-2 path measure on a tree): the
        # two-point resistance of each sink is the sum along its path; a T join in the
        # middle of a track counts.
        c = copper(
            pads=[dot("S", (0.0, 0.0)), dot("A", (8.0, 4.0)), dot("B", (8.0, -4.0))],
            tracks=[
                dict(layer="F.Cu", a=[0.0, 0.0], b=[8.0, 0.0], width_mm=0.3),
                dict(layer="F.Cu", a=[4.0, 0.0], b=[8.0, 4.0], width_mm=0.15),
                dict(layer="F.Cu", a=[4.0, 0.0], b=[4.0, -4.0], width_mm=0.2),
                dict(layer="F.Cu", a=[4.0, -4.0], b=[8.0, -4.0], width_mm=0.2),
            ],
        )
        r = solve(c, sources=[0], sinks=[1, 2], current_a=2.0)

        def seg(length, w):
            return RHO * length / (w * T_OUT)

        trunk = seg(4.0, 0.3)
        expect = {"A.1": trunk + seg(math.hypot(4, 4), 0.15), "B.1": trunk + 2 * seg(4.0, 0.2)}
        for row in r["two_point"]:
            want = expect[row["sink"]]
            self.assertAlmostEqual(row["r_mohm"] / 1e3, want, delta=want * 1e-3)
        # Superposition: each sink draws 1 A; A's drop is trunk*2 + its branch*1.
        drops = {row["sink"]: row["drop_mv"] / 1e3 for row in r["sinks"]}
        self.assertAlmostEqual(
            drops["A.1"], 2 * trunk + seg(math.hypot(4, 4), 0.15), delta=drops["A.1"] * 1e-3
        )

    def test_an_island_is_an_open(self):
        c = copper(
            pads=[dot("S", (0.0, 0.0)), dot("L", (5.0, 0.0)), dot("X", (9.0, 9.0))],
            tracks=[dict(layer="F.Cu", a=[0.0, 0.0], b=[5.0, 0.0], width_mm=0.2)],
        )
        r = solve(c, sources=[0], sinks=[1, 2], current_a=1.0)
        self.assertEqual(r["status"], "open")
        self.assertEqual(r["opens"], ["X.1"])
        self.assertNotIn("r_eff_mohm", r)
        self.assertTrue(r["sinks"][1]["open"])
        self.assertIn("not joined", r["warnings"][0]["statement"])


class PlaneTest(unittest.TestCase):
    def test_a_strip_between_bus_bars(self):
        # A 10 x 2 mm strip on In1 with full-width bars at its ends (one cell column
        # each): R = R_square * (L - h) / W between the bar centres.
        h = 0.05
        bars = [
            dict(
                ref=ref,
                pad="1",
                at=[x, 1.0],
                layers=["In1.Cu"],
                polygon=rect(x - 0.02, 0, x + 0.02, 2.0),
            )
            for ref, x in (("S", 0.025), ("L", 9.975))
        ]
        c = copper(
            zones=[dict(layer="In1.Cu", polygons=[dict(outline=rect(0, 0, 10, 2), holes=[])])],
            pads=bars,
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0, h=h)
        expect = RHO / T_IN * (10.0 - h) / 2.0
        self.assertAlmostEqual(ohms(r), expect, delta=expect * 0.01)

    def test_an_annulus(self):
        # Held inner disc of radius a, the load drawn evenly round the outer ring of
        # radius b: R = R_square ln(b / a) / 2 pi.
        a, b, h = 1.0, 5.0, 0.05
        c = copper(
            zones=[dict(layer="In1.Cu", polygons=[dict(outline=circle((0, 0), b), holes=[])])],
            pads=[
                dict(ref="S", pad="1", at=[0, 0], layers=["In1.Cu"], polygon=circle((0, 0), a)),
                dict(
                    ref="L",
                    pad="1",
                    at=[b, 0],
                    layers=["In1.Cu"],
                    polygon=circle((0, 0), b),
                    holes=[circle((0, 0), b - h)],
                ),
            ],
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0, h=h, two_point=False)
        expect = RHO / T_IN * math.log((b - h / 2) / a) / (2 * math.pi)
        self.assertAlmostEqual(ohms(r), expect, delta=expect * 0.03)

    def test_a_hole_in_a_zone_is_not_copper(self):
        # The same strip with a 1 mm hole across most of its width costs more.
        bars = [
            dict(
                ref=ref,
                pad="1",
                at=[x, 1.0],
                layers=["In1.Cu"],
                polygon=rect(x - 0.02, 0, x + 0.02, 2.0),
            )
            for ref, x in (("S", 0.025), ("L", 9.975))
        ]
        plain = copper(
            zones=[dict(layer="In1.Cu", polygons=[dict(outline=rect(0, 0, 10, 2), holes=[])])],
            pads=bars,
        )
        holed = copper(
            zones=[
                dict(
                    layer="In1.Cu",
                    polygons=[dict(outline=rect(0, 0, 10, 2), holes=[rect(4.5, 0.2, 5.5, 2.0)])],
                )
            ],
            pads=bars,
        )
        r0 = solve(plain, sources=[0], sinks=[1], current_a=1.0, two_point=False)
        r1 = solve(holed, sources=[0], sinks=[1], current_a=1.0, two_point=False)
        self.assertGreater(r1["r_eff_mohm"], r0["r_eff_mohm"] * 1.2)
        # The neck is where the current is densest, and it is flagged.
        d = r1["density"]["In1.Cu"]
        self.assertTrue(4.4 <= d["at"][0] <= 5.6, d)


class ViaTest(unittest.TestCase):
    def test_a_via_barrel(self):
        c = copper(
            pads=[dot("S", (1.0, 1.0)), dot("L", (1.0, 1.0), layer="B.Cu")],
            vias=[dict(at=[1.0, 1.0], drill_mm=0.2, diameter_mm=0.4, top="F.Cu", bottom="B.Cu")],
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0)
        expect = barrel_ohm(1.0, 0.2, rho=RHO)
        self.assertAlmostEqual(ohms(r), expect, delta=expect * 1e-3)
        # 1.0 mm of 0.2 mm barrel plated 20 um: about 1.24 mOhm at 20 C.
        self.assertAlmostEqual(barrel_ohm(1.0, 0.2), 1.244e-3, delta=0.01e-3)

    def test_a_via_joins_a_plane(self):
        # A top pad, a via to In1, a strip on In1 to a far bar: via plus strip.
        h = 0.05
        c = copper(
            zones=[dict(layer="In1.Cu", polygons=[dict(outline=rect(0, 0, 10, 2), holes=[])])],
            pads=[
                dot("S", (0.5, 1.0)),
                dict(
                    ref="L",
                    pad="1",
                    at=[9.975, 1.0],
                    layers=["In1.Cu"],
                    polygon=rect(9.955, 0, 9.995, 2.0),
                ),
            ],
            vias=[dict(at=[0.5, 1.0], drill_mm=0.2, diameter_mm=0.4, top="F.Cu", bottom="In1.Cu")],
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0, h=h, two_point=False)
        self.assertGreater(ohms(r), barrel_ohm(0.3, 0.2, rho=RHO) + RHO / T_IN * 9.4 / 2.0)


class NetworkTest(unittest.TestCase):
    def test_merged_ends_leave_no_self_loop(self):
        # A track inside one via land: both ends merge into the land's node.
        from pnr.ir_drop import build

        c = copper(
            pads=[dot("S", (1.0, 1.0)), dot("L", (3.0, 1.0), layer="B.Cu")],
            tracks=[
                dict(layer="F.Cu", a=[1.0, 1.0], b=[3.0, 1.0], width_mm=0.25),
                dict(layer="F.Cu", a=[2.95, 1.0], b=[3.05, 1.0], width_mm=0.25),
            ],
            vias=[dict(at=[3.0, 1.0], drill_mm=0.2, diameter_mm=0.4, top="F.Cu", bottom="B.Cu")],
        )
        i, j, _g, _kind = build(c)["edges"]
        self.assertTrue(len(i) and (i != j).all())
        r = solve(c, sources=[0], sinks=[1], current_a=1.0, two_point=False)
        expect = RHO * 2.0 / (0.25 * T_OUT) + barrel_ohm(1.0, 0.2, rho=RHO)
        self.assertLess(abs(ohms(r) - expect), 0.15 * expect)

    def test_a_solve_short_of_its_tolerance_is_unsolved(self):
        c = copper(
            pads=[dot("S", (0.0, 0.0)), dot("L", (5.0, 0.0))],
            tracks=[
                dict(layer="F.Cu", a=[0.0, 0.0], b=[2.0, 0.0], width_mm=0.2),
                dict(layer="F.Cu", a=[2.0, 0.0], b=[5.0, 0.0], width_mm=0.3),
            ],
        )
        r = solve(c, sources=[0], sinks=[1], current_a=1.0, budget_mohm=1e9, max_iter=0)
        self.assertEqual(r["status"], "unsolved")
        self.assertIsNone(r["r_eff_mohm"])
        self.assertIn("residual", r["warnings"][0]["statement"])
        self.assertEqual(solve(c, sources=[0], sinks=[1], current_a=1.0)["status"], "pass")


class ReportTest(unittest.TestCase):
    def test_budget_warnings_and_heatmap(self):
        c = copper(
            pads=[dot("S", (0.0, 0.0)), dot("A", (5.0, 1.0)), dot("B", (5.0, -1.0))],
            tracks=[
                dict(layer="F.Cu", a=[0.0, 0.0], b=[5.0, 1.0], width_mm=0.1),
                dict(layer="F.Cu", a=[0.0, 0.0], b=[5.0, -1.0], width_mm=0.3),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            r = solve(
                c,
                sources=[0],
                sinks=[1, 2],
                current_a=2.0,
                budget_mohm=1.0,
                heatmap=os.path.join(tmp, "v"),
            )
            self.assertTrue(r["heatmaps"])
            with open(r["heatmaps"][0], "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertEqual(r["status"], "fail")
        statements = " ".join(w["statement"] for w in r["warnings"])
        self.assertIn("splits equal among 2 sinks", statements)
        self.assertIn("mOhm", statements)
        # 1 A in a 0.1 mm outer track is over an IPC-2221 trace for the whole 2 A.
        self.assertTrue(r["density"]["F.Cu"]["neck"])
        self.assertEqual(r["density"]["F.Cu"]["kind"], "track")


class SpecTest(unittest.TestCase):
    """The plane_partition and ir_drop sections (pnr.power_spec) through the compiler."""

    DOC = {
        "schema": "v0",
        "board": {"outline": {"w": 20, "h": 20}, "layers": 4},
        "net_class": {"rails": {"nets": ["V*"], "plane_layer": "In2.Cu"}},
    }

    def compile(self, **sections):
        from pnr.constraints import compile_constraints, compile_routing_rules

        c = compile_constraints(dict(self.DOC, **sections), ["U1"])
        return compile_routing_rules(c, ["GND", "V1", "V2", "SIG"])

    def test_absent_sections_add_nothing(self):
        rules = self.compile()
        self.assertNotIn("plane_partition", rules)
        self.assertNotIn("ir_drop", rules)

    def test_sections_compile(self):
        rules = self.compile(
            plane_partition=[{"layer": "In2.Cu", "nets": ["V*"], "fill": "GND"}],
            ir_drop=[
                {
                    "net": "V1",
                    "sources": ["FB3:2"],
                    "sinks": {"U1": ["G5", "H5"]},
                    "budget_mohm": 4,
                    "temperature_c": 60,
                }
            ],
        )
        (part,) = rules["plane_partition"]
        self.assertEqual(part["nets"], ["V1", "V2"])
        self.assertEqual(part["split_gap_mm"], 0.3)
        self.assertEqual(part["min_width_mm"], 1.0)
        self.assertTrue(part["core_no_vias"])
        (ir,) = rules["ir_drop"]
        self.assertEqual(ir["sinks"], ["U1:G5", "U1:H5"])
        self.assertEqual(ir["sources"], ["FB3:2"])
        self.assertEqual(ir["budget_mohm"], 4.0)
        self.assertFalse(ir["hard"])

    def test_bad_input_names_its_key(self):
        from pnr.constraints import ConstraintError

        for sections in (
            dict(plane_partition=[{"layer": "In2.Cu", "nets": ["V1"], "gap": 1}]),
            dict(plane_partition=[{"layer": "In2.Cu", "nets": []}]),
            dict(ir_drop=[{"net": "V1", "sources": []}]),
            dict(ir_drop=[{"net": "V1", "sources": ["A:1"], "budget_mohm": 1, "budget_mv": 1}]),
            dict(ir_drop=[{"net": "V1", "sources": ["A:1"], "split": "half"}]),
            dict(ir_drop=[{"net": "V1", "sources": ["A1"]}]),
        ):
            with self.assertRaises(ConstraintError):
                self.compile(**sections)

    def test_terminals_by_ref_or_address(self):
        from pnr.power_spec import PowerSpecError, rail_current, resolve_terminal

        parts = {
            "FB3": dict(address="top.pmic.fb_rf1", pads=["1", "2"]),
            "U1": dict(address="top.radio.u1", pads=["G5"]),
        }
        self.assertEqual(resolve_terminal("@pmic.fb_rf1:2", parts), [("FB3", "2")])
        self.assertEqual(resolve_terminal("U1:G5", parts), [("U1", "G5")])
        with self.assertRaises(PowerSpecError):
            resolve_terminal("@fb_rf9:2", parts)
        rules = dict(
            electrical_nets={"V1": {"peak_current_a": 2.5}},
            net_classes=[dict(nets=["V2"], current_a=1.0)],
        )
        self.assertEqual(rail_current(rules, "V1"), 2.5)
        self.assertEqual(rail_current(rules, "V2"), 1.0)
        self.assertEqual(rail_current(rules, "V2", 0.5), 0.5)
        self.assertIsNone(rail_current(rules, "V3"))


if __name__ == "__main__":
    unittest.main()
