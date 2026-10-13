"""Cooperative placement repair: cascades, rollback, constraints and bank motion."""

import copy
import json
import math
import os
import unittest
from unittest.mock import patch

from pnr.constraints import ConstraintError, compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.metrics import hard_violations
from pnr.place.shove import repair
from pnr.placement_banks import members


def board():
    parts = [
        Component(
            r,
            "C_0603",
            xy,
            0.0,
            "top",
            (2, 2),
            (2, 2),
            pads=[Pad("1", "V", (-0.5, 0), (0.4, 0.4)), Pad("2", "G", (0.5, 0), (0.4, 0.4))],
        )
        for r, xy in (("C1", (3, 5)), ("C2", (5.5, 5)), ("C3", (8, 5)))
    ]
    return BoardGraph(
        "cascade",
        parts,
        [Net(n, i, [(c.ref, str(i)) for c in parts]) for i, n in enumerate(("V", "G"), 1)],
        BoardOutline(14, 12),
    )


def constraints(g, **doc):
    return compile_constraints(dict(board=dict(width=14, height=12), **doc), g.refs)


class ShoveTest(unittest.TestCase):
    def test_cascade_reduces_aggregate_displacement_and_is_reproducible(self):
        a = board()
        target = copy.deepcopy(a)
        target.component("C1").pos = (5, 5)
        con = constraints(a)
        baseline = a.to_json()
        records = repair(a, target, con, allow_rotation=False)
        self.assertTrue(records)
        self.assertTrue(any(len(r["moves"]) > 1 for r in records))
        self.assertFalse(any(hard_violations(a, con, 0.2).values()))
        for r in records:
            self.assertLess(sum(r["after_terms"].values()), sum(r["before_terms"].values()))
            checkpoint = BoardGraph.from_json(json.dumps(r["graph"]))
            for move in r["moves"]:
                part = checkpoint.component(move["ref"])
                part.pos, part.rot = move["after"], move["rotation"]
            self.assertFalse(any(hard_violations(checkpoint, con, 0.2).values()))
        again = BoardGraph.from_json(baseline)
        self.assertEqual(records, repair(again, target, con, allow_rotation=False))
        self.assertEqual(a.to_json(), again.to_json())

    def test_fixed_neighbor_and_failed_transactions_do_not_move(self):
        g = board()
        target = copy.deepcopy(g)
        target.component("C1").pos = (5.5, 5)
        con = constraints(g, fixed={"C2": dict(at=[5.5, 5], rot=0), "C3": dict(at=[8, 5], rot=0)})
        before = {r: (g.component(r).pos, g.component(r).rot) for r in ("C2", "C3")}
        repair(g, target, con)
        self.assertEqual(before, {r: (g.component(r).pos, g.component(r).rot) for r in before})
        self.assertFalse(any(hard_violations(g, con, 0.2).values()))

    def test_rotation_recovery_and_hard_orientation(self):
        g = board()
        target = copy.deepcopy(g)
        g.component("C1").rot = 90
        con = constraints(g)
        repair(g, target, con)
        self.assertEqual(g.component("C1").rot, 0)
        g.component("C1").rot = 90
        con = constraints(g, orientation={"C1": 90})
        repair(g, target, con)
        self.assertEqual(g.component("C1").rot, 90)

    def test_bank_translates_together_and_requires_same_two_nets(self):
        g = board()
        target = copy.deepcopy(g)
        target.component("C1").pos = (5, 5)
        con = constraints(
            g,
            placement_bank=[
                dict(name="output", parent="converter", members=["C2", "C3"], max_span_mm=3)
            ],
        )
        with patch.dict(os.environ, {"PNR_PLACEMENT_BANKS": "1"}):
            banks = members(g, con)
            before = tuple(g.component("C3").pos[i] - g.component("C2").pos[i] for i in (0, 1))
            records = repair(g, target, con, banks=banks)
            external = [r for r in records if r["ref"] == "C1"]
            self.assertTrue(external)
            for transaction in external:
                moves = {m["ref"]: m for m in transaction["moves"]}
                if "C2" in moves:
                    self.assertIn("C3", moves)
                    self.assertEqual(
                        tuple(moves["C3"]["after"][i] - moves["C2"]["after"][i] for i in (0, 1)),
                        before,
                    )
            g.component("C3").pads[0].net = "OTHER"
            with self.assertRaises(ValueError):
                members(g, con)

    def test_bank_declarations_reject_unknown_or_shared_members(self):
        g = board()
        bank = dict(name="bank", members=["C1", "MISSING"], max_span_mm=3)
        with self.assertRaises(ConstraintError):
            constraints(g, placement_bank=[bank])
        bank["members"] = ["C1", "C2"]
        with self.assertRaises(ConstraintError):
            constraints(g, placement_bank=[bank, dict(bank, name="other")])

    def test_shove_is_opt_in_and_uses_compiled_constraint_adapter(self):
        from pnr.place.legalize import legalize, legalize_constraint_kwargs

        g = board()
        con = constraints(g)
        with patch.dict(os.environ, {"PNR_LEGALIZE_SHOVE": "0"}):
            off = legalize_constraint_kwargs(g, con, {})
            self.assertNotIn("shove_constraints", off)
        with patch.dict(os.environ, {"PNR_LEGALIZE_SHOVE": "1"}):
            on = legalize_constraint_kwargs(g, con, {})
            self.assertIs(on["shove_constraints"], con)
            placed = legalize(g, 14, 12, **on)
            self.assertIn("shove", placed.legal_motion)
            self.assertFalse(any(hard_violations(placed, con, 0.2).values()))

    def test_cascade_cannot_drag_an_anchor_away_from_hard_group(self):
        g = board()
        target = copy.deepcopy(g)
        target.component("C1").pos = (5, 5)
        con = constraints(
            g,
            fixed={"C3": dict(at=[8, 5], rot=0)},
            group=[dict(anchor="C2", members=["C3"], hard=True, radius_mm=2.6)],
        )
        repair(g, target, con)
        self.assertLessEqual(math.dist(g.component("C2").pos, g.component("C3").pos), 2.6)
        self.assertFalse(any(hard_violations(g, con, 0.2).values()))

    def test_failed_search_restores_all_poses(self):
        g = board()
        target = copy.deepcopy(g)
        target.component("C1").pos = (100, 100)
        baseline = g.to_json()
        self.assertEqual(repair(g, target, constraints(g)), [])
        self.assertEqual(g.to_json(), baseline)

    def test_bank_diameter_survives_legalization_and_independent_check(self):
        from pnr.place.legalize import legalize, legalize_constraint_kwargs

        g = board()
        con = constraints(
            g, placement_bank=[dict(name="bulk", members=["C1", "C3"], max_span_mm=3, weight=20)]
        )
        with patch.dict(os.environ, {"PNR_PLACEMENT_BANKS": "0"}):
            self.assertFalse(any(hard_violations(g, con).values()))
        with patch.dict(os.environ, {"PNR_PLACEMENT_BANKS": "1"}):
            self.assertTrue(hard_violations(g, con)["group_outside"])
            placed = legalize(g, 14, 12, **legalize_constraint_kwargs(g, con, {}))
            self.assertFalse(any(hard_violations(placed, con, 0.2).values()))
            self.assertLessEqual(
                math.dist(placed.component("C1").pos, placed.component("C3").pos), 3 + 1e-9
            )
        # Activation never modifies the reusable flag-off source contract.
        self.assertEqual(con.constraints[0].enforcement.name, "SOFT")


if __name__ == "__main__":
    unittest.main()
