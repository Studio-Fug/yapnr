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

    def test_pose_caches_preserve_every_transaction_and_channel_cost(self):
        from pnr.constraints import compile_routing_rules
        from pnr.place.channels import ChannelModel
        from pnr.place.power_first import PourChannels

        original_interactions = ChannelModel.interactions
        for poured in (False, True):
            for power_channels in ("0", "1"):
                results = []
                calls = []
                for caching in (False, True):
                    g = board()
                    target = copy.deepcopy(g)
                    target.component("C1").pos = (5, 5)
                    target.component("C1").rot = 90
                    con = constraints(g)
                    model = ChannelModel(g, compile_routing_rules(con, ["V", "G"]))
                    if poured:
                        model = PourChannels(
                            model,
                            dict(
                                elements=[
                                    dict(
                                        kind="bbox",
                                        role="trunk",
                                        net="V",
                                        pins=[[c.ref, "1"] for c in g.components],
                                    )
                                ]
                            ),
                        )
                    count = [0]

                    def interactions(*args, **kwargs):
                        count[0] += 1
                        return original_interactions(*args, **kwargs)

                    from functools import lru_cache

                    cache = lru_cache if caching else lambda **kwargs: lambda fn: fn
                    with patch("pnr.place.shove.lru_cache", cache), patch.object(
                        ChannelModel, "interactions", interactions
                    ), patch.dict(os.environ, {"PNR_POWER_CHANNELS": power_channels}):
                        records = repair(g, target, con, channels=model)
                    self.assertTrue(records)
                    self.assertFalse(any(hard_violations(g, con, 0.2).values()))
                    results.append((g.to_json(), records))
                    calls.append(count[0])
                self.assertEqual(results[0], results[1], (poured, power_channels))
                self.assertLess(calls[1], calls[0], (poured, power_channels, calls))

    def test_scalar_channel_cache_matches_vector_model_at_changed_poses(self):
        from itertools import product

        from pnr.constraints import compile_routing_rules
        from pnr.place.channels import ChannelModel
        from pnr.place.power_first import PourChannels
        from pnr.place.shove import _memoized_channels

        g = board()
        con = constraints(g)
        model = ChannelModel(g, compile_routing_rules(con, ["V", "G"]))
        cached = _memoized_channels(model, {c.ref: c for c in g.components})
        poured = PourChannels(
            model,
            dict(
                elements=[
                    dict(
                        kind="bbox",
                        role="trunk",
                        net="V",
                        pins=[[c.ref, "1"] for c in g.components],
                    )
                ]
            ),
        )
        poured_cached = _memoized_channels(poured, {c.ref: c for c in g.components})
        positive = []
        for angle in (0, 90, 180, 270):
            for side in ("top", "bottom"):
                for x, y in product((3.1, 4.0, 5.2, 6.25), (1.0, 5.1, 9.0)):
                    g.components[1].pos = (x, y)
                    g.components[1].rot = angle
                    g.components[1].side = side
                    for comp in g.components:
                        others = [c for c in g.components if c is not comp]
                        expected = float(model.penalty(comp, others, *comp.pos))
                        actual = float(cached.penalty(comp, others, *comp.pos))
                        self.assertEqual(actual.hex(), expected.hex())
                        self.assertEqual(
                            float(cached.penalty(comp, others, *comp.pos)).hex(), expected.hex()
                        )
                        positive.append(expected)
                        for power_channels in ("0", "1"):
                            with patch.dict(os.environ, PNR_POWER_CHANNELS=power_channels):
                                self.assertEqual(
                                    float(poured_cached.penalty(comp, others, *comp.pos)).hex(),
                                    float(poured.penalty(comp, others, *comp.pos)).hex(),
                                )
        self.assertGreater(max(positive), 0)

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

    def test_shove_switch_uses_compiled_constraint_adapter(self):
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

    def test_exact_alignment_finishes_before_shove_and_failed_snap_stays_illegal(self):
        from pnr.place.legalize import legalize, legalize_constraint_kwargs
        from pnr.place.regions import snap_aligns

        g = board()
        g.component("C2").courtyard = (2, 2.4)
        con = constraints(g, align=[dict(name="caps", refs=["C1", "C2"], axis="y", tol_mm=0)])
        before_snap = []

        def snap(placed, *args):
            before_snap.append(hard_violations(placed, con))
            return snap_aligns(placed, *args)

        with patch.dict(os.environ, {"PNR_LEGALIZE_SHOVE": "1"}):
            kwargs = legalize_constraint_kwargs(g, con, {})
            with patch("pnr.place.regions.snap_aligns", snap):
                placed = legalize(g, 14, 12, **kwargs)
            self.assertTrue(before_snap[0]["align_off"])
            self.assertFalse(any(hard_violations(placed, con, 0.2).values()))
            self.assertNotIn("skipped", placed.legal_motion["shove"])

            with patch("pnr.place.regions.snap_aligns", return_value=[]), patch(
                "pnr.place.shove.repair"
            ) as shove:
                failed = legalize(g, 14, 12, **kwargs)
            shove.assert_not_called()
            self.assertTrue(hard_violations(failed, con)["align_off"])
            self.assertEqual(failed.legal_motion["shove"]["skipped"], "illegal baseline")
            with self.assertRaisesRegex(ValueError, "requires a legal baseline"):
                repair(failed, g, con)

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
