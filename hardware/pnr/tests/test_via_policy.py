"""The board's via policy (pnr.via_policy): DRU bans, declared kinds, buildable
spans, the build (a laminar family priced per drill pair), cost, return ties."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))

from hard_rungs import dru_text, hard_rungs  # noqa: E402
from stackup import stackup_text  # noqa: E402

from pnr.stack import copper_names, stackup_rows  # noqa: E402
from pnr.via_policy import (  # noqa: E402
    BLIND,
    BURIED,
    DEPTH,
    LAMINATE,
    LASER,
    MICRO,
    THROUGH,
    GridVias,
    ViaModel,
    board_policy,
    compatible,
    copper_bonds,
    copper_gaps,
    dru_via_bans,
    merge_ranges,
    reference_planes,
    resolve,
    return_tie_rule,
    select_build,
    stack_eps_r,
)

SIX = copper_names(6)
GAPS6 = [0.09, 0.55, 0.2, 0.55, 0.09]
# The rungs' 6-layer build: prepreg / core (In1-In2) / prepreg / core (In3-In4) / prepreg.
BONDS6 = ["prepreg", "core", "prepreg", "core", "prepreg"]
BONDS4 = ["prepreg", "core", "prepreg"]
BONDS8 = ["prepreg", "core", "prepreg", "core", "prepreg", "core", "prepreg"]
# What the 6-layer chaser rungs need (pnr.via_policy.board_needs on their graph).
NEEDS6 = [
    ("F.Cu", "In1.Cu", 13.0, "drop"),
    ("F.Cu", "In2.Cu", 12.0, "signal"),
    ("F.Cu", "In4.Cu", 7.0, "drop"),
    ("F.Cu", "B.Cu", 12.0, "signal"),
    ("In1.Cu", "In3.Cu", 1.0, "floor"),
    ("In1.Cu", "In3.Cu", 12.0, "tie"),
]
HDI = dict(allowed=[THROUGH, BLIND, BURIED, MICRO], microvia=dict(diameter_mm=0.3, drill_mm=0.1))
BB = dict(allowed=[THROUGH, BLIND, BURIED])


def rung(name):
    return next(r for r in hard_rungs() if r["name"] == name)


class DruBans(unittest.TestCase):
    def test_each_rung_policy(self):
        """The judge's own rules for each 6-layer rung give the bans it states."""
        through = dru_via_bans(dru_text(rung("07-chaser-20-6L-SGSGPS")))[0]
        bb = dru_via_bans(dru_text(rung("07-chaser-20-6L-SGSGPS-BB")))[0]
        hdi = dru_via_bans(dru_text(rung("07-chaser-20-6L-SGSGPS-HDI")))[0]
        self.assertEqual(through, {BLIND, BURIED, MICRO})
        self.assertEqual(bb, {MICRO})
        self.assertEqual(hdi, set())

    def test_plane_rules_ban_no_via(self):
        text = (
            '(version 1)\n(rule "In1.Cu is a GND plane: no tracks"\n  (layer "In1.Cu")\n'
            "  (condition \"A.Type == 'Track'\")\n  (constraint disallow track))\n"
        )
        self.assertEqual(dru_via_bans(text), (set(), []))

    def test_limited_ban_counts_everywhere(self):
        text = '(version 1)\n(rule "no micro on top"\n  (layer "F.Cu")\n  (constraint disallow micro_via))\n'
        banned, notes = dru_via_bans(text)
        self.assertEqual(banned, {MICRO})
        self.assertEqual(len(notes), 1)

    def test_item_names(self):
        self.assertEqual(
            dru_via_bans("(rule a (constraint disallow via))")[0], {BLIND, BURIED, MICRO}
        )
        self.assertEqual(
            dru_via_bans("(rule a (constraint disallow buried_via))")[0], {BLIND, BURIED}
        )
        self.assertEqual(dru_via_bans("(rule a (constraint disallow blind_via))")[0], {BLIND})
        # A commented-out rule bans nothing.
        self.assertEqual(dru_via_bans("# (rule a (constraint disallow via))")[0], set())
        self.assertEqual(dru_via_bans(None), (set(), []))


class Resolve(unittest.TestCase):
    def test_no_declared_policy_is_through_only(self):
        self.assertIsNone(resolve(None, SIX, gaps=GAPS6))
        self.assertIsNone(resolve(dict(allowed=[THROUGH]), SIX, gaps=GAPS6))
        # Declared kinds the rules ban all: through only, as before.
        self.assertIsNone(resolve(BB, SIX, gaps=GAPS6, banned={BLIND, BURIED, MICRO}))

    def test_two_layers_is_through_only(self):
        self.assertIsNone(resolve(HDI, copper_names(2), gaps=[1.51]))

    def test_declared_less_bans(self):
        policy = resolve(HDI, SIX, gaps=GAPS6, banned={MICRO})
        self.assertEqual(policy["allowed"], [THROUGH, BLIND, BURIED])
        self.assertEqual(policy["sizes"], {BLIND: [0.6, 0.3], BURIED: [0.6, 0.3]})
        self.assertTrue(any("micro" in w for w in policy["warnings"]))
        hdi = resolve(HDI, SIX, gaps=GAPS6)
        self.assertEqual(hdi["sizes"][MICRO], [0.3, 0.1])

    def test_microvia_size(self):
        decl = dict(allowed=[THROUGH, MICRO])
        # The project's microvia when the design gives none; none at all: unused.
        self.assertEqual(
            resolve(decl, SIX, gaps=GAPS6, microvia=(0.25, 0.1))["sizes"][MICRO], [0.25, 0.1]
        )
        self.assertIsNone(resolve(decl, SIX, gaps=GAPS6))
        # Under the board's minimum: not used.
        self.assertIsNone(resolve(decl, SIX, gaps=GAPS6, microvia=(0.15, 0.1)))
        with self.assertRaises(ValueError):
            resolve(dict(allowed=["laser"]), SIX)
        # A ring under the board's minimum annular width: widened to meet it.
        wide = resolve(HDI, SIX, gaps=GAPS6, min_annular=0.15)
        self.assertEqual(wide["sizes"][MICRO], [0.4, 0.1])
        self.assertTrue(any("widened" in w for w in wide["warnings"]))

    def test_board_files(self):
        """board_policy reads the stackup block, the DRU and the project beside it."""
        spec = rung("07-chaser-20-6L-SGSGPS-HDI")
        with tempfile.TemporaryDirectory() as tmp:
            pcb = Path(tmp) / "source.kicad_pcb"
            pcb.write_text("(kicad_pcb\n\t(setup\n" + stackup_text(spec["stackup"]) + "\n\t)\n)\n")
            (Path(tmp) / "source.kicad_dru").write_text(dru_text(rung("07-chaser-20-6L-SGSGPS-BB")))
            project = dict(
                board=dict(
                    design_settings=dict(
                        rules=dict(min_microvia_diameter=0.2, min_microvia_drill=0.1)
                    )
                ),
                net_settings=dict(
                    classes=[dict(name="Default", microvia_diameter=0.3, microvia_drill=0.1)]
                ),
            )
            (Path(tmp) / "source.kicad_pro").write_text(json.dumps(project))
            rules = dict(
                layers=6, fab=dict(via_diameter_mm=0.6, via_drill_mm=0.3, via_annular_mm=0.15)
            )
            policy = board_policy(spec["via_policy"], pcb, rules)
            self.assertEqual(policy["layers"], SIX)
            self.assertEqual(policy["gaps_mm"], GAPS6)
            self.assertEqual(policy["bonds"], BONDS6)
            self.assertNotIn("build", policy)  # no graph: no build
            self.assertAlmostEqual(policy["return_tie"]["max_mm"], 7.0662, places=3)
            self.assertEqual(policy["allowed"], [THROUGH, BLIND, BURIED])  # the BB rules ban micro
            (Path(tmp) / "source.kicad_dru").write_text(dru_text(spec))
            hdi = board_policy(spec["via_policy"], pcb, rules)
            self.assertIn(MICRO, hdi["allowed"])
            self.assertEqual(hdi["sizes"][MICRO], [0.4, 0.1])
            self.assertIsNone(board_policy(None, pcb, rules))

    def test_copper_gaps(self):
        rows = stackup_rows(
            "(setup " + stackup_text(rung("07-chaser-20-6L-SGSGPS")["stackup"]) + ")"
        )
        self.assertEqual(copper_gaps(rows, SIX), GAPS6)
        self.assertIsNone(copper_gaps(rows, copper_names(4)))
        self.assertEqual(copper_bonds(rows, SIX), BONDS6)
        self.assertIsNone(copper_bonds(rows, copper_names(4)))
        self.assertEqual(stack_eps_r(rows), 4.5)
        # A gap of unstated kind is unknown; a prepreg row anywhere in a gap is a bond line.
        rows = [
            dict(name="F.Cu", type="copper", thickness_mm=0.035, epsilon_r=None),
            dict(name="dielectric 1", type="foo", thickness_mm=0.1, epsilon_r=None),
            dict(name="In1.Cu", type="copper", thickness_mm=0.035, epsilon_r=None),
            dict(name="dielectric 2", type="core", thickness_mm=0.5, epsilon_r=None),
            dict(name="dielectric 2a", type="prepreg", thickness_mm=0.1, epsilon_r=None),
            dict(name="B.Cu", type="copper", thickness_mm=0.035, epsilon_r=None),
        ]
        self.assertEqual(copper_bonds(rows, ["F.Cu", "In1.Cu", "B.Cu"]), [None, "prepreg"])
        self.assertIsNone(stack_eps_r(rows))


class Spans(unittest.TestCase):
    def model(self, code_layers, gaps, decl=HDI, banned=(), bonds=None):
        names = copper_names(code_layers)
        bonds = bonds or {4: BONDS4, 6: BONDS6, 8: BONDS8}[code_layers]
        return ViaModel(resolve(decl, names, gaps=gaps, banned=banned, bonds=bonds), (0.6, 0.3))

    def test_classify(self):
        m = self.model(6, GAPS6)
        self.assertEqual(m.kind_of(0, 5), THROUGH)
        self.assertEqual(m.how(0, 1), (MICRO, LASER))  # 0.09 mm dielectric, 0.1 mm drill
        self.assertEqual(m.kind_of(4, 5), MICRO)
        self.assertEqual(m.how(0, 2), (BLIND, LAMINATE))  # F foil + the In1-In2 core
        self.assertEqual(m.how(3, 5), (BLIND, LAMINATE))
        self.assertEqual(m.how(0, 4), (BLIND, LAMINATE))
        self.assertEqual(m.how(1, 4), (BURIED, LAMINATE))
        self.assertEqual(m.how(1, 2), (BURIED, LAMINATE))  # one core; inner: no microvia
        # A deeper dielectric than the drill: no microvia, a controlled-depth blind via.
        deep = self.model(4, [0.2104, 1.065, 0.2104])
        self.assertEqual(deep.how(0, 1), (BLIND, DEPTH))
        # Microvias banned: the adjacent pair is a controlled-depth blind via.
        self.assertEqual(self.model(6, GAPS6, banned={MICRO}).how(0, 1), (BLIND, DEPTH))

    def test_spans_that_split_a_core_cannot_be_built(self):
        """In3 is the top face of the In3-In4 core and In2 the bottom face of the
        In1-In2 core: no sub-laminate ends there, and each is deeper than a drill
        from the outside (review finding: F-In3 blind and In1-In3 buried vias)."""
        m = self.model(6, GAPS6)
        for t, b in ((0, 3), (1, 3), (2, 5), (2, 4), (2, 3)):
            self.assertIsNone(m.how(t, b), (t, b))
        # Without the dielectrics' kinds no laminated span qualifies (and it says so).
        bare = resolve(BB, SIX, gaps=GAPS6)
        self.assertTrue(any("cores and which prepreg" in w for w in bare["warnings"]))
        m = ViaModel(bare, (0.6, 0.3))
        self.assertEqual(m.how(0, 1), (BLIND, DEPTH))
        self.assertIsNone(m.how(0, 2))
        self.assertIsNone(m.how(1, 4))

    def test_two_four_eight_layers(self):
        self.assertEqual(self.model(4, [0.2104, 1.065, 0.2104]).kind_of(1, 2), BURIED)
        eight = self.model(8, [0.09, 0.33, 0.14, 0.33, 0.14, 0.33, 0.09])
        self.assertEqual(eight.kind_of(0, 1), MICRO)
        self.assertEqual(eight.kind_of(6, 7), MICRO)
        self.assertEqual(eight.kind_of(1, 6), BURIED)
        self.assertEqual(eight.kind_of(3, 4), BURIED)
        self.assertIsNone(eight.kind_of(2, 5))  # In2 and In5 are core faces
        self.assertEqual(eight.kind_of(0, 7), THROUGH)

    def test_cost(self):
        m = self.model(6, GAPS6)
        self.assertEqual(m.cost(0, 5, THROUGH), 1.0)
        costs = [m.cost(0, b, m.kind_of(0, b)) for b in range(1, 5)]
        self.assertEqual(costs, sorted(costs))  # deeper is dearer
        self.assertTrue(all(0.5 < c < 1.0 for c in costs))
        self.assertAlmostEqual(m.cost(0, 1, MICRO), 0.5 + 0.5 * 0.09 / 1.48)

    def test_cover_without_buried(self):
        m = self.model(6, GAPS6, decl=dict(allowed=[THROUGH, BLIND]))
        top, bottom, kind = m.cover(1, 3)
        self.assertEqual(kind, BLIND)
        self.assertTrue(top == 0 or bottom == 5)
        self.assertEqual(m.cover(0, 5), (0, 5, THROUGH))

    def test_grid_spans(self):
        """On the SGSGPS grid (F, In2, B), every buildable span a candidate: F to In2
        is the F-In2 sub-laminate's blind via, In2 to B the In1-B one (no span may
        start at In2, a core face), F to B through; drops reach the nearest plane."""
        grid = GridVias(
            resolve(HDI, SIX, gaps=GAPS6, bonds=BONDS6),
            ["F.Cu", "In2.Cu", "B.Cu"],
            0.25,
            0.2,
            (0.6, 0.3),
            3,
        )
        fi = grid.span(0, 1)
        self.assertEqual(
            (fi.top, fi.bottom, fi.kind, fi.lo, fi.hi), ("F.Cu", "In2.Cu", BLIND, 0, 1)
        )
        ib = grid.span(1, 2)
        self.assertEqual(
            (ib.top, ib.bottom, ib.kind, ib.lo, ib.hi), ("In1.Cu", "B.Cu", BLIND, 1, 2)
        )
        fb = grid.span(0, 2)
        self.assertEqual((fb.kind, fb.keepout, fb.cost), (THROUGH, 3, 1.0))
        micro = grid.to_layer(0, "In1.Cu")
        self.assertEqual((micro.kind, micro.lo, micro.hi, micro.diameter), (MICRO, 0, 0, 0.3))
        self.assertEqual(micro.keepout, 2)  # ceil((0.15 + 0.3 + 0.2) / 0.25) - 1
        self.assertEqual(grid.to_layer(2, "In4.Cu").kind, MICRO)
        vcc = grid.to_layer(0, "In4.Cu")
        self.assertEqual((vcc.kind, vcc.lo, vcc.hi), (BLIND, 0, 1))
        self.assertTrue(grid.reaches(vcc, "In3.Cu") and not grid.reaches(micro, "In3.Cu"))
        # Same-net vias at one site that share a layer are one barrel.
        self.assertEqual([s.kind for s in grid.merged([fi, ib])], [THROUGH])
        self.assertEqual(len(grid.merged([micro, grid.to_layer(2, "In4.Cu")])), 2)

    def test_merge_ranges(self):
        self.assertEqual(merge_ranges([(0, 2), (2, 5)]), [(0, 5)])
        self.assertEqual(merge_ranges([(0, 1), (4, 5)]), [(0, 1), (4, 5)])
        self.assertEqual(merge_ranges([(2, 3), (0, 2), (4, 5)]), [(0, 3), (4, 5)])


class Build(unittest.TestCase):
    def policy(self, decl, **kw):
        return resolve(decl, SIX, gaps=GAPS6, bonds=BONDS6, **kw)

    def test_compatible(self):
        self.assertTrue(compatible((0, 2), (3, 5)))  # disjoint halves
        self.assertTrue(compatible((0, 2), (0, 4)))  # nested
        self.assertTrue(compatible((1, 2), (1, 4)))
        self.assertFalse(compatible((0, 4), (3, 5)))  # overlap, neither inside
        self.assertFalse(compatible((0, 2), (1, 4)))

    def test_chaser_build(self):
        """The 6-layer chaser rungs: the GND drops' F-In1 span (laser microvia, or a
        controlled-depth blind via without microvias) and the signals' F-In2
        sub-laminate earn their drill pairs; F-In4 (3 % cheaper than through for the
        VCC drops) and In1-In4 do not."""
        for decl, banned, first in ((HDI, (), (MICRO, LASER)), (BB, {MICRO}, (BLIND, DEPTH))):
            build = select_build(self.policy(decl, banned=banned), NEEDS6)
            self.assertEqual(
                build["spans"],
                [["F.Cu", "In1.Cu", *first], ["F.Cu", "In2.Cu", BLIND, LAMINATE]],
            )
            self.assertLess(build["price"], build["through_price"])
            model = ViaModel(dict(self.policy(decl, banned=banned), build=build), (0.6, 0.3))
            self.assertEqual(model.cover(0, 4), (0, 5, THROUGH))  # VCC drops: through
            self.assertEqual(model.cover(1, 3), (0, 5, THROUGH))  # GND ties: through
            self.assertEqual(model.cover(2, 5), (0, 5, THROUGH))
            self.assertEqual(model.cover(0, 1)[2], first[0])

    def test_drill_pair_price_decides(self):
        free = select_build(self.policy(dict(BB, drill_pair_cost=0.0)), NEEDS6)
        spans = [tuple(s[:2]) for s in free["spans"]]
        self.assertIn(("F.Cu", "In4.Cu"), spans)  # free pairs: every saving counts
        laminated = [s for s in free["spans"] if s[3] == LAMINATE]
        index = SIX.index
        for a in laminated:
            for b in laminated:
                self.assertTrue(
                    compatible((index(a[0]), index(a[1])), (index(b[0]), index(b[1]))), (a, b)
                )
        dear = select_build(self.policy(dict(BB, drill_pair_cost=100.0)), NEEDS6)
        self.assertEqual(dear["spans"], [])
        # resolve: no span earns its pair -> through vias only.
        self.assertIsNone(self.policy(dict(BB, drill_pair_cost=100.0), needs=NEEDS6))
        self.assertEqual(self.policy(BB, needs=NEEDS6)["build"]["spans"][1][:2], ["F.Cu", "In2.Cu"])

    def test_ties_without_signals_favour_buried(self):
        """Many In1-In3 ties and no signal changes: the In1-In4 sub-laminate (a
        buried tie, 0.94 of a through via) earns its pair once it saves more than
        the pair's price (2.0): 40 ties save 2.4, 30 only 1.8."""
        self.assertEqual(
            select_build(self.policy(BB), [("In1.Cu", "In3.Cu", 30.0, "tie")])["spans"], []
        )
        needs = [("In1.Cu", "In3.Cu", 40.0, "tie")]
        build = select_build(self.policy(BB), needs)
        self.assertEqual(build["spans"], [["In1.Cu", "In4.Cu", BURIED, LAMINATE]])

    def test_model_keeps_to_the_build(self):
        policy = self.policy(BB, needs=NEEDS6)
        model = ViaModel(policy, (0.6, 0.3))
        self.assertIsNone(model.kind_of(0, 4))  # buildable, not in the build
        self.assertEqual(model.how(0, 4), (BLIND, LAMINATE))
        self.assertEqual(model.kind_of(0, 2), BLIND)


class ReturnTies(unittest.TestCase):
    def test_rule(self):
        rule = return_tie_rule(None, 4.5)
        # 0.1 * 1000 ps / (2 * sqrt(4.5) / c) = 7.07 mm
        self.assertAlmostEqual(rule["max_mm"], 0.1 * 1000 / (2 * 4.5**0.5 / 0.299792458), 3)
        self.assertEqual(rule["t_rise_ns"], 1.0)
        self.assertIn("default", rule["t_rise_source"])
        slow = return_tie_rule(dict(t_rise_ns=2.0), 4.5)
        self.assertAlmostEqual(slow["max_mm"], 2 * rule["max_mm"], 3)
        self.assertEqual(return_tie_rule(None, None)["eps_r"], 4.5)
        with self.assertRaises(ValueError):
            return_tie_rule(dict(t_rise_ns=0), 4.5)

    def test_reference_planes(self):
        dedicated = [("In1.Cu", "GND"), ("In3.Cu", "GND"), ("In4.Cu", "VCC")]
        self.assertEqual(reference_planes(SIX, dedicated, "F.Cu"), [("In1.Cu", "GND")])
        self.assertEqual(
            reference_planes(SIX, dedicated, "In2.Cu"), [("In1.Cu", "GND"), ("In3.Cu", "GND")]
        )
        self.assertEqual(reference_planes(SIX, dedicated, "B.Cu"), [("In4.Cu", "VCC")])


if __name__ == "__main__":
    unittest.main()
