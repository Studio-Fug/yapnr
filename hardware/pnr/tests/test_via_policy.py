"""The board's via policy (pnr.via_policy): DRU bans, declared kinds, spans, cost."""

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
    MICRO,
    THROUGH,
    GridVias,
    ViaModel,
    board_policy,
    copper_gaps,
    dru_via_bans,
    merge_ranges,
    resolve,
)

SIX = copper_names(6)
GAPS6 = [0.09, 0.55, 0.2, 0.55, 0.09]
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


class Spans(unittest.TestCase):
    def model(self, code_layers, gaps, decl=HDI, banned=()):
        names = copper_names(code_layers)
        return ViaModel(resolve(decl, names, gaps=gaps, banned=banned), (0.6, 0.3))

    def test_classify(self):
        m = self.model(6, GAPS6)
        self.assertEqual(m.kind_of(0, 5), THROUGH)
        self.assertEqual(m.kind_of(0, 1), MICRO)  # 0.09 mm dielectric, 0.1 mm drill
        self.assertEqual(m.kind_of(4, 5), MICRO)
        self.assertEqual(m.kind_of(0, 2), BLIND)
        self.assertEqual(m.kind_of(2, 5), BLIND)
        self.assertEqual(m.kind_of(1, 3), BURIED)
        self.assertEqual(m.kind_of(1, 2), BURIED)  # adjacent but inner: no microvia
        # A dielectric deeper than the drill: no microvia, a blind via.
        deep = self.model(4, [0.2104, 1.065, 0.2104])
        self.assertEqual(deep.kind_of(0, 1), BLIND)
        # Microvias banned: the adjacent pair is a blind via.
        self.assertEqual(self.model(6, GAPS6, banned={MICRO}).kind_of(0, 1), BLIND)

    def test_two_four_eight_layers(self):
        self.assertEqual(self.model(4, [0.2104, 1.065, 0.2104]).kind_of(1, 2), BURIED)
        eight = self.model(8, [0.09, 0.33, 0.14, 0.33, 0.14, 0.33, 0.09])
        self.assertEqual(eight.kind_of(0, 1), MICRO)
        self.assertEqual(eight.kind_of(6, 7), MICRO)
        self.assertEqual(eight.kind_of(2, 5), BURIED)
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
        """On the SGSGPS grid (F, In2, B) a layer change is a blind via of its own
        span, F to B is through; drops reach the nearest plane."""
        grid = GridVias(
            resolve(HDI, SIX, gaps=GAPS6), ["F.Cu", "In2.Cu", "B.Cu"], 0.25, 0.2, (0.6, 0.3), 3
        )
        fi = grid.span(0, 1)
        self.assertEqual(
            (fi.top, fi.bottom, fi.kind, fi.lo, fi.hi), ("F.Cu", "In2.Cu", BLIND, 0, 1)
        )
        ib = grid.span(1, 2)
        self.assertEqual(
            (ib.top, ib.bottom, ib.kind, ib.lo, ib.hi), ("In2.Cu", "B.Cu", BLIND, 1, 2)
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


if __name__ == "__main__":
    unittest.main()
