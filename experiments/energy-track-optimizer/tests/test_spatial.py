import dataclasses
import random
import unittest

from prototype.energy_track import Config, Controller, GateResult, Obstacle, Track
from prototype.spatial import LayerGrid, overlaps
from shapely.geometry import LineString, Point, box


def config(**kwargs):
    return Config(spatial_broadphase=True, growth_mm=2, **kwargs)


def bent(id="t", x=0, y=0, layer="F.Cu", width=0.2, clearance=0.2):
    return Track(
        id, id, ((x, y), (x + 1, y + 1), (x + 4, y + 1), (x + 5, y)), width, clearance, layer
    )


class GridTests(unittest.TestCase):
    def test_invalid_geometry(self):
        for call in (
            lambda: LayerGrid(0),
            lambda: LayerGrid(max_cells=0),
            lambda: LayerGrid().upsert("x", "F.Cu", (0, 0, float("inf"), 1)),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_boundary_negative_and_layers(self):
        g = LayerGrid(cell_mm=1)
        g.upsert("a", "F.Cu", (-1, -1, 0, 0))
        g.upsert("b", "In1.Cu", (-1, -1, 0, 0))
        self.assertEqual(g.query((0, 0, 0, 0), ("F.Cu",)), ["a"])
        self.assertEqual(g.query((-1, -1, -1, -1)), ["a", "b"])
        self.assertEqual(g.query((2, 2, 3, 3)), [])

    def test_moved_geometry_no_stale_buckets(self):
        g = LayerGrid()
        g.upsert("a", "F.Cu", (0, 0, 1, 1))
        g.upsert("a", "In1.Cu", (20, 20, 21, 21))
        self.assertEqual(g.query((0, 0, 1, 1)), [])
        self.assertEqual(g.query((20, 20, 21, 21), ("In1.Cu",)), ["a"])
        self.assertEqual(g.stats["rebuilds"], 0)
        g.remove("a")
        self.assertEqual(g.query((20, 20, 21, 21)), [])

    def test_large_object_and_query_bounded_fallback(self):
        g = LayerGrid(cell_mm=1, max_cells=4)
        g.upsert("huge", "F.Cu", (-100, -100, 100, 100))
        g.upsert("tiny", "F.Cu", (0, 0, 0.1, 0.1))
        self.assertIn("huge", g.oversize["F.Cu"])
        self.assertEqual(g.query((0, 0, 0.1, 0.1), ("F.Cu",)), ["huge", "tiny"])
        self.assertEqual(g.query((-200, -200, 200, 200), ("F.Cu",)), ["huge", "tiny"])
        self.assertGreater(g.stats["fullscan_queries"], 0)
        self.assertLessEqual(sum(len(s) for s in g.buckets.values()), 4)

    def test_random_dynamic_queries_equal_brute_force(self):
        rng = random.Random(6017)
        g = LayerGrid(cell_mm=1.7, max_cells=80)
        oracle = {}
        for step in range(1000):
            id = "r" + str(rng.randrange(90))
            layer = rng.choice(("F.Cu", "In1.Cu", "B.Cu"))
            if rng.random() < 0.13:
                g.remove(id)
                oracle.pop(id, None)
            else:
                x, y = rng.uniform(-50, 50), rng.uniform(-50, 50)
                w, h = rng.uniform(0, 20), rng.uniform(0, 20)
                bounds = (x, y, x + w, y + h)
                g.upsert(id, layer, bounds)
                oracle[id] = (layer, bounds)
            x, y = rng.uniform(-55, 55), rng.uniform(-55, 55)
            q = (x, y, x + rng.uniform(0, 30), y + rng.uniform(0, 30))
            layers = None if step % 5 == 0 else (layer,)
            expected = sorted(
                i
                for i, (la, b) in oracle.items()
                if (layers is None or la in layers) and overlaps(q, b)
            )
            self.assertEqual(g.query(q, layers), expected)
        self.assertEqual(g.stats["rebuilds"], 0)


class ControllerSpatialTests(unittest.TestCase):
    def test_large_clearance_and_width_are_not_missed(self):
        t = bent()
        wide = Track("wide", "wide", ((0, 15), (5, 15)), 20, 8)
        c = Controller([t, wide], config=config())
        self.assertIn("wide", {o.id for o in c.environment("t")})
        self.assertFalse(c.transact({"t": ((0, 0), (5, 0))}).accepted)

    def test_far_obstacles_filtered_and_other_layers_ignored(self):
        t = bent()
        obs = [
            Obstacle("near", Point(2, 2)),
            Obstacle("far", Point(100, 100)),
            Obstacle("other", Point(2, 2), layer="B.Cu"),
        ]
        c = Controller([t, bent("distant", 100)], obs, config=config())
        self.assertEqual({o.id for o in c.environment("t")}, {"near"})

    def test_random_obstacle_lookup_contains_every_true_potential_blocker(self):
        rng = random.Random(829)
        tracks = []
        obstacles = []
        for i in range(180):
            x, y = rng.uniform(-80, 80), rng.uniform(-80, 80)
            layer = rng.choice(("F.Cu", "B.Cu"))
            tracks.append(
                bent(
                    "t" + str(i),
                    x,
                    y,
                    layer,
                    width=rng.uniform(0.05, 8),
                    clearance=rng.uniform(0.01, 6),
                )
            )
            obstacles.append(
                Obstacle(
                    "o" + str(i),
                    Point(x + rng.uniform(-2, 2), y + rng.uniform(-2, 2)),
                    rng.uniform(0, 4),
                    rng.uniform(0, 7),
                    layer,
                )
            )
        c = Controller(tracks, obstacles, config=config(spatial_cell_mm=3, spatial_max_cells=200))
        for t in tracks[::3]:
            selected = {o.id for o in c.environment(t.id)}
            witness = LineString(t.points)
            for o in obstacles:
                threshold = 2 + t.width_mm / 2 + o.radius_mm + max(t.clearance_mm, o.clearance_mm)
                if o.layer == t.layer and witness.distance(o.shape) <= threshold:
                    self.assertIn(o.id, selected)
            for other in tracks:
                threshold = (
                    2 + (t.width_mm + other.width_mm) / 2 + max(t.clearance_mm, other.clearance_mm)
                )
                if (
                    other.id != t.id
                    and other.layer == t.layer
                    and witness.distance(LineString(other.points)) <= threshold
                ):
                    self.assertIn(other.id, selected)

    def test_dirty_neighbors_match_bruteforce_with_refill_and_layers(self):
        rng = random.Random(114)
        tracks = [
            bent(
                "t" + str(i),
                rng.uniform(-30, 30),
                rng.uniform(-30, 30),
                rng.choice(("F.Cu", "B.Cu")),
                rng.uniform(0.1, 4),
                rng.uniform(0.1, 3),
            )
            for i in range(200)
        ]
        indexed = Controller(tracks, config=config())
        linear = Controller(tracks, config=dataclasses.replace(config(), spatial_broadphase=False))
        for k in range(100):
            x, y = rng.uniform(-25, 25), rng.uniform(-25, 25)
            changed = LineString(((x, y), (x + 1, y + 1)))
            verdict = GateResult(
                True,
                dirty_region=box(x + 15, y + 15, x + 17, y + 17) if k % 2 else None,
                dirty_layers=None if k % 3 else ("B.Cu",),
            )
            expected = linear.dirty_neighbors(
                changed, "F.Cu", verdict, exclude=("t0",), queued=("t1",)
            )
            self.assertEqual(
                indexed.dirty_neighbors(changed, "F.Cu", verdict, exclude=("t0",), queued=("t1",)),
                expected,
            )

    def test_validator_extra_layer_includes_moved_layer_too(self):
        c = Controller([bent("a"), bent("b", layer="B.Cu")], config=config())
        v = GateResult(True, dirty_region=box(-1, -1, 6, 2), dirty_layers=("B.Cu",))
        self.assertEqual(c.dirty_neighbors(LineString(((0, 0), (5, 0))), "F.Cu", v), ["a", "b"])

    def test_index_updates_after_accept_and_not_after_refusal(self):
        c = Controller([bent()], config=config())
        before = c._spatial.records["track:t"].bounds
        self.assertTrue(c.transact({"t": ((0, 0), (5, 0))}).accepted)
        after = c._spatial.records["track:t"].bounds
        self.assertNotEqual(before, after)
        for a, b in zip(after, (-0.3, -0.3, 5.3, 0.3)):
            self.assertAlmostEqual(a, b)
        self.assertEqual(c._spatial.stats["rebuilds"], 0)
        c = Controller([bent()], config=config(), gate=lambda *args: GateResult(False, "native"))
        before = c._spatial.records.copy()
        self.assertFalse(c.transact({"t": ((0, 0), (5, 0))}).accepted)
        self.assertEqual(c._spatial.records, before)

    def test_group_candidate_uses_updated_neighbor_shape(self):
        a = bent("a")
        b = bent("b", 100)
        c = Controller([a, b], config=config())
        moved = dataclasses.replace(b, points=((0, 0.1), (5, 0.1)))
        trial = dict(c.tracks, b=moved)
        self.assertIn("b", {o.id for o in c.environment("a", trial, changed_ids=("b",))})
        shape = next(o.shape for o in c.environment("a", trial, changed_ids=("b",)) if o.id == "b")
        self.assertEqual(shape.bounds, (0, 0.1, 5, 0.1))

    def test_refill_updates_obstacle_index_and_dirty_scope(self):
        plane = Obstacle("plane", box(100, 100, 110, 110), dependent_plane=True)
        revised = dataclasses.replace(plane, shape=box(2, 2, 3, 3))
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
            [plane],
            config=config(),
            gate=lambda *args: GateResult(True, checks=checks, updated_obstacles=(revised,)),
        )
        self.assertNotIn("plane", {o.id for o in c.environment("t")})
        verdict = c.transact({"t": ((0, 0), (5, 0))})
        self.assertTrue(verdict.accepted)
        self.assertIsNotNone(verdict.dirty_region)
        self.assertIn("plane", {o.id for o in c.environment("t")})
        self.assertEqual(c._spatial.stats["rebuilds"], 0)

    def test_protected_or_metadata_obstacle_update_rejected(self):
        o = Obstacle("wall", Point(20, 20))
        changed = dataclasses.replace(o, layer="B.Cu")
        c = Controller(
            [bent()],
            [o],
            config=config(),
            gate=lambda *args: GateResult(True, updated_obstacles=(changed,)),
        )
        before = c._spatial.records.copy()
        self.assertEqual(c.transact({"t": ((0, 0), (5, 0))}).reason, "obstacle_update_metadata")
        self.assertEqual(c._spatial.records, before)

    def test_same_final_dependency_routes_with_and_without_index(self):
        tracks = [
            Track("b_blocker", "A", ((3, -2), (3, 1), (7, 1), (7, -2))),
            Track("a_waiting", "B", ((0, 0), (1.4, 1.4), (8.6, 1.4), (10, 0))),
        ]
        a = Controller(
            tracks, config=Config(growth_mm=3.2, spatial_broadphase=False, max_nodes=100)
        ).run()
        b = Controller(
            tracks, config=Config(growth_mm=3.2, spatial_broadphase=True, max_nodes=100)
        ).run()
        self.assertEqual(a["tracks"], b["tracks"])
        self.assertEqual(a["events"], b["events"])
        self.assertEqual(b["spatial"]["rebuilds"], 0)


if __name__ == "__main__":
    unittest.main()
