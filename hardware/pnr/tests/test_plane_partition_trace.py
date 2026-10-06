"""The optional ``trace`` hooks of ``pnr.plane_partition`` (pnr.plane_partition.PlaneTrace),
for ``pnr.animate.plane_partition`` and ``docs/plane-partition.md``: a traced run must return
exactly what an untraced run returns, and the trace itself must hold one frame per documented
stage, in order, with the right shapes."""

import json
import unittest

from pnr.plane_partition import _CACHE, PlaneFrame, PlaneTrace, Terminal, partition

ENTRY = dict(
    layer="In2.Cu",
    nets=["A", "B"],
    order="current",
    split_gap_mm=0.3,
    min_width_mm=1.0,
    fill="GND",
    core_no_vias=True,
    terminal_reach_mm=0.8,
    currents={},
    budgets_mohm={},
    sources={},
    h_mm=0.1,
)


def pad(name, at):
    return Terminal(name, "pad", at, 0.8)


def via(name, at):
    return Terminal(name, "via", at, 0.175)


TERMS = {
    "A": [pad("A1", (2.0, 2.0)), pad("A2", (18.0, 2.0)), via("A3", (10.0, 5.0))],
    "B": [pad("B1", (2.0, 10.0)), pad("B2", (18.0, 10.0)), via("B3", (10.0, 7.0))],
}
FOREIGN = [((x, 6.0), 0.3) for x in (6.0, 8.0, 12.0, 14.0)]
CURRENTS = {"A": 4.0, "B": 1.5}
STAGES = ("raster", "terminals", "trunk", "widen", "grow", "carve", "polygons", "necks")


def run(trace=None):
    _CACHE.clear()
    return partition(
        ENTRY,
        width=20.0,
        height=12.0,
        terminals=TERMS,
        blocked=FOREIGN,
        currents=CURRENTS,
        trace=trace,
    )


class IdentityTest(unittest.TestCase):
    """A traced and an untraced call return the same :class:`Partition`, byte for byte."""

    def test_rows_and_report_match(self):
        plain = run()
        traced = run(PlaneTrace(nets=list(ENTRY["nets"])))
        self.assertEqual(
            json.dumps(plain.rows(), sort_keys=True), json.dumps(traced.rows(), sort_keys=True)
        )
        self.assertEqual(
            json.dumps(plain.report, sort_keys=True), json.dumps(traced.report, sort_keys=True)
        )
        self.assertEqual(plain.connect, traced.connect)
        self.assertEqual(plain.outer, traced.outer)
        self.assertEqual(sorted(plain.cores), sorted(traced.cores))
        for net in plain.cores:
            pts_a, half_a = plain.cores[net]
            pts_b, half_b = traced.cores[net]
            self.assertEqual(half_a, half_b)
            self.assertEqual(sorted(pts_a), sorted(pts_b))

    def test_a_traced_call_does_not_pollute_or_read_the_cache(self):
        # Two traced calls on the same inputs each get their own, independently filled trace
        # (a traced call is never served from the cache, and never seeds it either).
        _CACHE.clear()
        t1 = PlaneTrace(nets=list(ENTRY["nets"]))
        t2 = PlaneTrace(nets=list(ENTRY["nets"]))
        partition(
            ENTRY,
            width=20.0,
            height=12.0,
            terminals=TERMS,
            blocked=FOREIGN,
            currents=CURRENTS,
            trace=t1,
        )
        self.assertEqual(len(_CACHE), 0)
        partition(
            ENTRY,
            width=20.0,
            height=12.0,
            terminals=TERMS,
            blocked=FOREIGN,
            currents=CURRENTS,
            trace=t2,
        )
        self.assertTrue(len(t1.frames) > 0)
        self.assertEqual(len(t1.frames), len(t2.frames))


class TraceContentTest(unittest.TestCase):
    """The trace itself: one frame per stage (two rails: one ``trunk``/``widen`` pair each,
    a handful of ``grow`` snapshots), the right shapes, and the neck rows it carries."""

    def setUp(self):
        self.trace = PlaneTrace(nets=list(ENTRY["nets"]))
        self.part = run(self.trace)

    def test_every_stage_appears_in_order(self):
        seen = [f.stage for f in self.trace.frames]
        self.assertEqual(sorted(set(seen), key=seen.index), list(STAGES))
        # trunk/widen: once per rail, in the connect order the report gives.
        order = self.part.report["connect_order"]
        self.assertEqual(
            [f.caption.split(":")[0] for f in self.trace.frames if f.stage == "trunk"],
            ["Rail %s" % n for n in order],
        )
        self.assertEqual(
            [f.caption.split(":")[0] for f in self.trace.frames if f.stage == "widen"],
            ["Rail %s" % n for n in order],
        )

    def test_raster_sets_the_shared_background_once(self):
        self.assertIsNotNone(self.trace.free)
        self.assertIsNotNone(self.trace.blocked)
        self.assertEqual(self.trace.free.dtype, bool)
        self.assertTrue(self.trace.free.any())
        self.assertTrue(self.trace.blocked.any())  # the foreign discs
        raster = self.trace.frames[0]
        self.assertEqual(raster.stage, "raster")
        self.assertIsNone(raster.label)

    def test_label_frames_share_the_grid_shape_and_are_independent_copies(self):
        shape = self.trace.free.shape
        labelled = [f for f in self.trace.frames if f.label is not None]
        self.assertTrue(labelled)
        for f in labelled:
            self.assertEqual(f.label.shape, shape)
        # Mutating one frame's array must not touch another's (each is its own copy).
        labelled[0].label[0, 0] = 99
        self.assertNotEqual(labelled[-1].label[0, 0], 99)

    def test_necks_frame_carries_a_row_per_reached_non_root_terminal(self):
        necks = [f for f in self.trace.frames if f.stage == "necks"][0]
        ways = necks.extra["ways"]
        self.assertTrue(ways)
        for row in ways:
            self.assertIn(row["net"], ENTRY["nets"])
            self.assertIn("name", row)
            self.assertIn("neck_at", row)

    def test_grow_has_more_than_one_snapshot_before_the_final_state(self):
        grows = [f for f in self.trace.frames if f.stage == "grow"]
        self.assertGreater(len(grows), 1)
        self.assertTrue((grows[-1].label >= 0).sum() >= (grows[0].label >= 0).sum())


class PlaneFrameDefaultsTest(unittest.TestCase):
    def test_a_bare_frame_has_no_label_and_no_extra(self):
        f = PlaneFrame("raster", "caption")
        self.assertIsNone(f.label)
        self.assertEqual(f.extra, {})


if __name__ == "__main__":
    unittest.main()
