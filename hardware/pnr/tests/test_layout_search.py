"""Contextual block variants: real parent placement and independent legality checks."""

import copy
import json
import os
import unittest
from unittest.mock import patch

from layout_choice_cases import bank_shapes, opposed_ports, record

from pnr.hier.layout_search import LayoutSearch, eligible, portfolio, record_id
from pnr.hier.top import _hierarchical_place, hierarchical_place
from pnr.place.metrics import hard_violations


class LayoutSearchTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith("PNR_")},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_case(self, case, method, **kw):
        g, c, r, lib = case()
        return hierarchical_place(
            g,
            c,
            r,
            lib,
            0,
            iters=80,
            layout_search=LayoutSearch(method=method, rounds=2, relax_iters=25, **kw),
        )

    def test_disabled_is_byte_identical_to_existing_path(self):
        args = opposed_ports()
        a = hierarchical_place(*args, 0, iters=30)
        b = _hierarchical_place(*args, 0, iters=30)
        self.assertEqual(a[0].to_json(), b[0].to_json())
        self.assertEqual(a[1], b[1])
        self.assertEqual(a[2], b[2])
        self.assertNotIn("layout_search", a[2])

    def test_twins_select_different_port_order_and_reduce_parent_cost(self):
        baseline = self.run_case(opposed_ports, "off")
        result = self.run_case(opposed_ports, "frozen")
        self.assertEqual(len({r["tier_rank"] for r in baseline[2]["blocks"].values()}), 1)
        self.assertEqual(len({r["tier_rank"] for r in result[2]["blocks"].values()}), 2)
        self.assertLess(result[1].hpwl_placed, baseline[1].hpwl_placed - 5)
        g, c, _, _ = opposed_ports()
        self.assertFalse(any(hard_violations(result[0], c, 0.2).values()))
        for comp in result[0].components:
            if comp.ref.startswith("J"):
                self.assertEqual(comp.pos, g.component(comp.ref).pos)

    def test_bank_shape_benefits_from_parent_relaxation(self):
        frozen = self.run_case(bank_shapes, "frozen")
        relaxed = self.run_case(bank_shapes, "alternating")
        self.assertLess(relaxed[1].hpwl_placed, frozen[1].hpwl_placed - 2)
        self.assertFalse(any(hard_violations(relaxed[0], bank_shapes()[1], 0.2).values()))
        events = relaxed[2]["layout_search"]["accepted"]
        self.assertTrue(any(e["stage"] == "swap" for e in events))
        self.assertTrue(all(b["cost"] < a["cost"] for a, b in zip(events, events[1:])))

    def test_repeatable_and_read_only_inputs(self):
        args = opposed_ports()
        old_graph, old_lib = args[0].to_json(), copy.deepcopy(args[3])
        conf = LayoutSearch(method="paired", rounds=1, relax_iters=10)
        a = hierarchical_place(*args, 2, iters=35, layout_search=conf)
        b = hierarchical_place(*args, 2, iters=35, layout_search=conf)
        self.assertEqual(a[0].to_json(), b[0].to_json())
        self.assertEqual(a[2]["layout_search"], b[2]["layout_search"])
        self.assertEqual(args[0].to_json(), old_graph)
        self.assertEqual(args[3], old_lib)
        self.assertTrue(a[2]["layout_search"]["parent_routing_required"])
        self.assertTrue(any(t["stage"] == "paired-swap" for t in a[2]["layout_search"]["trials"]))

    def test_budget_and_failed_proposals_do_not_replace_incumbent(self):
        g, c, r, lib = opposed_ports()
        # A nominally complete record with overlapping internals is still
        # rejected by the independent geometric oracle after expansion.
        bad = copy.deepcopy(lib["parent.a"][0])
        bad["layout"]["r1"] = list(bad["layout"]["r0"])
        lib = {n: records + [bad] for n, records in lib.items()}
        out = hierarchical_place(
            g,
            c,
            r,
            lib,
            0,
            iters=40,
            layout_search=LayoutSearch(method="paired", max_evaluations=8, relax_iters=10),
        )
        meta = out[2]["layout_search"]
        self.assertLessEqual(meta["evaluations"], 8)
        self.assertTrue(any(not t["legal"] for t in meta["trials"]))
        self.assertFalse(any(hard_violations(out[0], c, 0.2).values()))
        self.assertNotIn(record_id(bad), meta["selected"].values())
        json.dumps(meta, allow_nan=False)

    def test_diversity_retains_different_geometry_under_small_budget(self):
        a = record({"c": [2, 2, 0, "top"]}, 4, 4)
        near = record({"c": [2.01, 2, 0, "top"]}, 4, 4, 1)
        wide = record({"c": [2, 2, 0, "top"]}, 12, 4, 2)
        self.assertEqual(portfolio([a, near, wide], 2), [0, 1])
        self.assertEqual(portfolio([a, near, wide], 2, True), [0, 2])
        self.assertEqual(portfolio([dict(a, missing=1), wide], 2, True), [1])
        for index in range(6):
            counters = [0] * 6
            counters[index] = 1
            self.assertFalse(eligible(dict(a, objective=counters)))
        self.assertFalse(eligible(dict(a, smps_layout_unqualified=None)))
        self.assertFalse(eligible(dict(a, objective=[0, 0, None, 0, 0, 0])))

    def test_validation(self):
        for kwargs in (
            dict(method="unknown"),
            dict(max_evaluations=0),
            dict(rounds=True),
            dict(diverse="yes"),
            dict(score="bogus"),
        ):
            with self.assertRaises(ValueError):
                LayoutSearch(**kwargs)


if __name__ == "__main__":
    unittest.main()
