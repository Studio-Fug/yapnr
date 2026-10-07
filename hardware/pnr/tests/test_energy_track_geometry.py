import dataclasses
import math
import unittest

from shapely.geometry import LineString, Point, box

from pnr.energy_track_geometry import (
    Config,
    Controller,
    Energy,
    GateResult,
    Obstacle,
    Planner,
    Track,
    length,
    normalize,
)


def bent(id="a", y=0):
    return Track(id, id, ((0, y), (1, y + 1), (4, y + 1), (5, y)))


class GeometryTests(unittest.TestCase):
    def test_normalization_and_units(self):
        p = ((0, 0), (1, 0), (1, 0), (2, 0), (2, 1))
        self.assertEqual(normalize(p), ((0, 0), (2, 0), (2, 1)))
        self.assertAlmostEqual(Energy().value(p), 3.15)
        self.assertAlmostEqual(
            Energy(curvature_mm_per_rad2=0.2).value(p), 3.15 + 0.2 * (math.pi / 2) ** 2
        )

    def test_reject_invalid_inputs(self):
        for f in (
            lambda: Energy(bend_mm=-1),
            lambda: Config(seconds=math.inf),
            lambda: Config(max_states=0),
            lambda: Config(growth_levels=(1, 0.5)),
            lambda: Track("a", "a", ((0, 0), (math.nan, 1))),
            lambda: Obstacle("x", Point(), 0, 0.2),
        ):
            with self.assertRaises(ValueError):
                f()

    def test_free_path_simplifies(self):
        p = Planner(Config(growth_mm=2)).plan(bent(), [])
        self.assertEqual(p.after, ((0, 0), (5, 0)))
        self.assertLess(p.energy_after, p.energy_before)
        self.assertTrue(p.diagnostics["reverse_field"])

    def test_feasibility_witness_preserved_when_blocked(self):
        t = bent()
        o = Obstacle("wall", box(2, -2, 3, 0.5), clearance_mm=0.2)
        p = Planner(Config(growth_mm=2)).plan(t, [o])
        self.assertNotEqual(p.after, ((0, 0), (5, 0)))
        self.assertGreaterEqual(LineString(p.after).distance(o.shape) + 1e-8, 0.3)

    def test_multiple_obstacles_all_checked(self):
        t = Track("a", "a", ((0, 0), (2, 2), (8, 2), (10, 0)))
        obs = [Obstacle("left", box(2, -1, 3, 0.8)), Obstacle("right", box(7, -1, 8, 0.8))]
        p = Planner(Config(growth_mm=2, seconds=20, max_nodes=100)).plan(t, obs)
        for o in obs:
            self.assertGreaterEqual(LineString(p.after).distance(o.shape) + 1e-8, 0.3)
        self.assertNotEqual(p.after, ((0, 0), (10, 0)))
        self.assertEqual((p.after[0], p.after[-1]), (t.points[0], t.points[-1]))

    def test_narrow_clearance_not_rounded_away(self):
        t = Track("a", "a", ((0, 0), (3, 0)))
        p = Planner().plan(t, [Obstacle("near", Point(1, 0.299), 0, 0.2)])
        self.assertEqual(p.reason, "witness_not_legal")

    def test_capsule_radius_in_clearance(self):
        t = Track("a", "a", ((0, 0), (3, 0)))
        self.assertEqual(
            Planner()
            .plan(t, [Obstacle("wire", LineString(((0, 0.45), (3, 0.45))), 0.2, 0.2)])
            .reason,
            "witness_not_legal",
        )

    def test_different_layers_do_not_block(self):
        t = bent()
        o = Obstacle("other", box(1, -1, 4, 2), layer="In1.Cu")
        self.assertEqual(Planner(Config(growth_mm=2)).plan(t, [o]).after, ((0, 0), (5, 0)))

    def test_locked_and_pairs_fail_closed(self):
        for t in (
            dataclasses.replace(bent(), protected=True),
            dataclasses.replace(bent(), pair="LVDS"),
        ):
            p = Planner().plan(t, [])
            self.assertEqual(p.after, normalize(t.points))
            self.assertIn("requires_group", p.reason)

    def test_length_target_does_not_disappear(self):
        t = dataclasses.replace(
            bent(), min_length_mm=length(bent().points), max_length_mm=length(bent().points)
        )
        p = Planner(Config(growth_mm=2)).plan(t, [])
        self.assertAlmostEqual(length(p.after), length(t.points))

    def test_invalid_witness_has_no_assumed_feasibility(self):
        t = Track("a", "a", ((0, 0), (2, 2), (0, 2), (2, 0)))
        self.assertEqual(Planner().plan(t, []).reason, "invalid_witness")

    def test_budgets_return_legal_witness(self):
        p = Planner(Config(max_edges_checked=1)).plan(bent(), [])
        self.assertIn("budget", p.reason)
        self.assertEqual(p.after, normalize(bent().points))

    def test_native_segment_refusal_is_hard(self):
        t = bent()
        old_edges = {frozenset((a, b)) for a, b in zip(t.points, t.points[1:])}
        p = Planner(Config(growth_mm=2)).plan(
            t, [], segment_gate=lambda a, b: frozenset((a, b)) in old_edges
        )
        self.assertEqual(p.after, normalize(t.points))

    def test_path_guard_refusal_is_hard(self):
        p = Planner(Config(growth_mm=2)).plan(bent(), [], path_gate=lambda p: False)
        self.assertEqual(p.after, normalize(bent().points))

    def test_obstacle_order_deterministic(self):
        obs = [Obstacle("x", Point(2, -3)), Obstacle("y", Point(3, -3))]
        planner = Planner(Config(growth_mm=2, max_nodes=70))
        self.assertEqual(
            planner.plan(bent(), obs).after, planner.plan(bent(), list(reversed(obs))).after
        )

    def test_duplicate_obstacle_ids_rejected(self):
        with self.assertRaises(ValueError):
            Planner().plan(bent(), [Obstacle("x", Point(0, -3)), Obstacle("x", Point(1, -3))])

    def test_plane_needs_explicit_transaction_mode(self):
        p = Planner().plan(bent(), [Obstacle("plane", box(-1, -1, 6, 2), dependent_plane=True)])
        self.assertEqual(p.reason, "dependent_plane_requires_atomic_gate")


class TransactionTests(unittest.TestCase):
    def test_requeues_blocked_neighbor_after_space_freed(self):
        a = Track("b_blocker", "A", ((3, -2), (3, 1), (7, 1), (7, -2)))
        b = Track("a_waiting", "B", ((0, 0), (1.4, 1.4), (8.6, 1.4), (10, 0)))
        c = Controller([a, b], config=Config(growth_mm=3.2, seconds=25, max_nodes=100))
        r = c.run()
        self.assertEqual(r["stop"], "converged")
        self.assertEqual(r["transactions"], 2)
        self.assertEqual(r["events"][0]["reason"], "no_improvement")
        self.assertTrue(
            any(e["kind"] == "requeue" and e["ids"] == ["a_waiting"] for e in r["events"])
        )
        self.assertAlmostEqual(length(c.tracks["b_blocker"].points), 4)
        self.assertAlmostEqual(length(c.tracks["a_waiting"].points), 10)
        self.assertGreaterEqual(
            LineString(c.tracks["b_blocker"].points).distance(
                LineString(c.tracks["a_waiting"].points)
            ),
            0.4,
        )

    def test_native_gate_rejection_rolls_back(self):
        c = Controller(
            [bent()], config=Config(growth_mm=2), gate=lambda *args: GateResult(False, "native_drc")
        )
        original = c.fingerprint(c.tracks)
        verdict = c.transact({"a": ((0, 0), (5, 0))})
        self.assertFalse(verdict.accepted)
        self.assertEqual(c.fingerprint(c.tracks), original)

    def test_gate_exception_rolls_back(self):
        def broken(*args):
            raise TimeoutError()

        c = Controller([bent()], config=Config(growth_mm=2), gate=broken)
        self.assertEqual(c.transact({"a": ((0, 0), (5, 0))}).reason, "gate_exception:TimeoutError")
        self.assertEqual(c.tracks["a"].points, normalize(bent().points))

    def test_plane_gate_requires_all_checks(self):
        o = Obstacle("plane", box(-1, -1, 6, 2), dependent_plane=True)
        for gate in (None, lambda *args: GateResult(True)):
            c = Controller([bent()], [o], config=Config(growth_mm=2), gate=gate)
            self.assertFalse(c.transact({"a": ((0, 0), (5, 0))}).accepted)

    def test_plane_full_gate_and_dirty_region(self):
        o = Obstacle("plane", box(-1, -1, 6, 2), dependent_plane=True)
        checks = {
            k: True
            for k in (
                "refill",
                "native_drc",
                "connectivity",
                "pair_skew",
                "length",
                "power_ir",
                "reference",
                "protected_macros",
            )
        }
        c = Controller(
            [bent()],
            [o],
            config=Config(growth_mm=2),
            gate=lambda *args: GateResult(True, dirty_region=box(-1, -1, 7, 3), checks=checks),
        )
        self.assertTrue(c.transact({"a": ((0, 0), (5, 0))}).accepted)

    def test_protected_plane_never_moves(self):
        o = Obstacle("plane", box(-1, -1, 6, 2), dependent_plane=True, protected=True)
        c = Controller(
            [bent()], [o], config=Config(growth_mm=2), gate=lambda *args: GateResult(True)
        )
        self.assertEqual(c.transact({"a": ((0, 0), (5, 0))}).reason, "protected_plane")

    def test_anchor_and_length_invariants(self):
        c = Controller([bent()], config=Config(growth_mm=3))
        self.assertEqual(c.transact({"a": ((0, 0), (5, 1))}).reason, "anchors")
        t = dataclasses.replace(bent(), min_length_mm=5.5)
        self.assertEqual(
            Controller([t], config=Config(growth_mm=3)).transact({"a": ((0, 0), (5, 0))}).reason,
            "length_contract",
        )

    def test_hysteresis_and_cycle(self):
        c = Controller([bent()], config=Config(growth_mm=2, hysteresis_mm=2))
        self.assertEqual(c.transact({"a": ((0, 0), (5, 0))}).reason, "objective_hysteresis")
        c = Controller([bent()], config=Config(growth_mm=2))
        target = {"a": dataclasses.replace(c.tracks["a"], points=((0, 0), (5, 0)))}
        c.seen.add(c.fingerprint(target))
        self.assertEqual(c.transact({"a": ((0, 0), (5, 0))}).reason, "cycle")

    def test_group_is_atomic_and_bounded(self):
        c = Controller(
            [bent("a"), bent("b", 5)],
            config=Config(growth_mm=2),
            gate=lambda *args: GateResult(False, "power_ir"),
        )
        fingerprint = c.fingerprint(c.tracks)
        self.assertFalse(c.transact({"a": ((0, 0), (5, 0)), "b": ((0, 5), (5, 5))}).accepted)
        self.assertEqual(c.fingerprint(c.tracks), fingerprint)
        self.assertEqual(c.transact({}).reason, "group_budget")

    def test_group_final_intertrack_clearance(self):
        a = bent("a")
        b = bent("b", 0.1)
        c = Controller([a, b], config=Config(growth_mm=2), gate=lambda *args: GateResult(True))
        self.assertTrue(
            c.transact({"a": ((0, 0), (5, 0)), "b": ((0, 0.1), (5, 0.1))}).reason.startswith(
                "clearance:"
            )
        )


if __name__ == "__main__":
    unittest.main()
