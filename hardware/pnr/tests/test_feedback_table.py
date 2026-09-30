"""pnr.feedback.table: pooled failure rates, floor, lineage, router separation."""

import random
import unittest

from pnr.feedback.table import build


def conn(a, b, mode="signal"):
    ends = sorted([a, b])
    return dict(
        id="%s|%s" % tuple(ends),
        a=ends[0].rsplit(".", 1),
        b=ends[1].rsplit(".", 1),
        mode=mode,
        net="n",
    )


def node(i, conns, parent=None, router="plain"):
    return dict(id=i, parent=parent, fb=dict(router=router, conns=conns))


X, Y, Z = conn("ic.1", "r1.2"), conn("ic.3", "c1.1", "power"), conn("c2.1", "r2.1")


class TableTest(unittest.TestCase):
    def nodes(self):
        return [
            node("a", [X, Y]),
            node("b", [X]),
            node("c", [Y], parent="a"),
            node("d", [X, Z], parent="c"),
            dict(id="m", parent=None, fb=dict(missing=True)),
        ]

    def test_rates_and_determinism(self):
        t = build(self.nodes(), "tpl")
        self.assertEqual(t.n_evals, 4)  # the missing node does not count
        self.assertEqual(
            {k: e["fails"] for k, e in t.conns.items()}, {X["id"]: 3, Y["id"]: 2, Z["id"]: 1}
        )
        self.assertAlmostEqual(t.rate(X["id"]), 0.75)
        self.assertEqual(t.rate("nope"), 0.0)
        shuffled = self.nodes()
        random.Random(3).shuffle(shuffled)
        self.assertEqual(build(shuffled, "tpl").to_json(), t.to_json())
        self.assertEqual(build(shuffled, "tpl").sha(), t.sha())

    def test_lineage(self):
        t = build(self.nodes())
        self.assertEqual(t.lineage("d"), ["d", "c", "a"])
        self.assertEqual(t.lineage_fails(X["id"], "d"), 2)  # d and a (not c)
        self.assertEqual(t.lineage_fails(Y["id"], "d"), 2)  # c and a
        self.assertEqual(t.lineage_fails(Z["id"], "b"), 0)
        cyc = build([node("p", [X], parent="q"), node("q", [X], parent="p")])
        self.assertEqual(cyc.lineage_fails(X["id"], "p"), 2)  # cycle-safe

    def test_floor_needs_rate_and_evaluations(self):
        few = build([node(str(i), [X]) for i in range(5)])
        self.assertEqual(few.floor, [])  # 100% but only 5 evaluations
        many = build([node(str(i), [X] + ([Y] if i < 5 else [])) for i in range(10)])
        self.assertEqual(many.floor, [X["id"]])  # Y: 50%
        self.assertNotIn(tuple(X["a"] + X["b"]), many.pair_weights())
        self.assertTrue(many.is_floor(X["id"]))

    def test_pair_weights(self):
        t = build(self.nodes())
        w = t.pair_weights(weight=10, cap=20)
        self.assertEqual(
            w,
            {("ic", "1", "r1", "2"): 7.5, ("c1", "1", "ic", "3"): 5.0, ("c2", "1", "r2", "1"): 2.5},
        )
        self.assertEqual(t.pair_weights(weight=40, cap=20)[("ic", "1", "r1", "2")], 20)
        self.assertNotIn(("c2", "1", "r2", "1"), t.pair_weights(min_rate=0.3))

    def test_router_separation(self):
        nodes = self.nodes() + [node("s1", [Z], router="shove"), node("s2", [Z], router="shove")]
        plain, shove, both = (
            build(nodes, router="plain"),
            build(nodes, router="shove"),
            build(nodes),
        )
        self.assertEqual((plain.n_evals, shove.n_evals, both.n_evals), (4, 2, 6))
        self.assertEqual(shove.rate(Z["id"]), 1.0)
        self.assertEqual(plain.rate(Z["id"]), 0.25)
        self.assertNotEqual(plain.sha(), shove.sha())


if __name__ == "__main__":
    unittest.main()
