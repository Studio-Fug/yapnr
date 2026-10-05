"""E2, the hier -> fixed_block bridge (pnr.hier.macro.fixed_block_from_macro): the
``fixed``/``fixed_block`` constraint pair a placed, expanded macro derives. Pure
python (no KiCad); the copper side (pnr.hier.assemble --zones --group --anchor) is
covered by tests/test_hier_assemble_kicad.py, KiCad Python only."""

import unittest

from pnr.constraints import BoardSpec, CompiledConstraints
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.hier.blocks import Block
from pnr.hier.macro import collapse, fixed_block_from_macro


def _comp(ref, x, y, pads=()):
    return Component(
        ref=ref,
        footprint="fp:" + ref,
        pos=(x, y),
        rot=0.0,
        side="top",
        courtyard=(2.0, 2.0),
        bbox=(2.0, 2.0),
        pads=[Pad(name=n, net=net, offset=(dx, dy)) for n, net, dx, dy in pads],
    )


def _plan():
    """A 2-member power-stage block (U2 the chosen anchor, L1 beside it) on a
    10x10 mm sub-board, plus an outside part U9 on the top board."""
    members = [
        _comp("U2", 5.0, 5.0, pads=[("1", "SW", -1.0, 0.0), ("2", "GND", 1.0, 0.0)]),
        _comp("L1", 8.0, 5.0, pads=[("1", "SW", -0.5, 0.0), ("2", "1V0", 0.5, 0.0)]),
    ]
    sub = BoardGraph(
        name="blk:power", components=members, nets=[Net("SW", 1, [("U2", "1"), ("L1", "1")])]
    )
    source = BoardGraph(
        name="top",
        components=members + [_comp("U9", 20.0, 20.0)],
        nets=[Net("SW", 1, [("U2", "1"), ("L1", "1")])],
    )
    block = Block(name="power", refs=["L1", "U2"], template="power")
    con = CompiledConstraints(board=BoardSpec())
    mgraph, _mcon, _mrules, plan = collapse(source, con, {}, [(block, sub, 10.0, 10.0)])
    return mgraph, source, plan


def _flat(pos=(31.5, 12.25), rot=90.0, side="top"):
    """``plan.expand``'s output with the macro placed at ``pos``/``rot``/``side``,
    as if the global placer had posed it (no placer run here: collapse + a hand
    pose is the whole placement-side contract :func:`fixed_block_from_macro`
    depends on)."""
    mgraph, source, plan = _plan()
    placed = BoardGraph.from_json(mgraph.to_json())
    mc = placed.component("MB00")
    mc.pos, mc.rot, mc.side = pos, rot, side
    return plan.expand(placed, source), plan


class FixedBlockFromMacro(unittest.TestCase):
    def test_derives_fixed_and_fixed_block_entries(self):
        flat, plan = _flat()
        fixed, block = fixed_block_from_macro(
            flat, plan, "MB00", "power_stage", "POWER_STAGE", "U2", solid_layers=["In3.Cu"]
        )
        u2 = flat.component("U2")
        self.assertEqual(
            fixed, dict(at=[round(u2.pos[0], 6), round(u2.pos[1], 6)], rot=u2.rot, side=u2.side)
        )
        self.assertEqual(
            block,
            dict(
                name="power_stage",
                group="POWER_STAGE",
                anchor="U2",
                refs=["L1"],
                solid_layers=["In3.Cu"],
            ),
        )

    def test_anchor_pose_is_the_macros_actual_placed_pose(self):
        # The macro turned 90deg at (31.5, 12.25): U2 sits at the macro's own
        # frame origin (collapse() centred the 10x10 block there), so it lands
        # exactly on the macro's pose, not merely near it.
        flat, plan = _flat(pos=(31.5, 12.25), rot=90.0, side="top")
        fixed, _block = fixed_block_from_macro(flat, plan, "MB00", "n", "G", "U2")
        self.assertEqual(fixed, dict(at=[31.5, 12.25], rot=90.0, side="top"))

    def test_solid_layers_default_to_an_empty_list(self):
        flat, plan = _flat()
        _fixed, block = fixed_block_from_macro(flat, plan, "MB00", "n", "G", "U2")
        self.assertEqual(block["solid_layers"], [])

    def test_rejects_an_anchor_outside_the_macro(self):
        flat, plan = _flat()
        with self.assertRaises(ValueError):
            fixed_block_from_macro(flat, plan, "MB00", "n", "G", "U9")

    def test_rejects_an_unknown_macro(self):
        flat, plan = _flat()
        with self.assertRaises(ValueError):
            fixed_block_from_macro(flat, plan, "MB99", "n", "G", "U2")


if __name__ == "__main__":
    unittest.main()
