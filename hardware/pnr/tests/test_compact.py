"""PNR_COMPACT / PNR_SHRINK (docs/design/compact-placement.md).

Flag-off identity on two ladder cases (goldens of the parent commit), offset courtyards
(pin headers, through-hole, rotated and bottom-side parts), the compact legalizer (courtyard
gap, copper margins, slots of off-centre bodies), the hard rung whose 1x8 header has its
origin on pin 1, the compactness tie-break, the global-placement loss against its cost
capture, the shrink search on a toy board, the runner's switches and the trace header.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "regression"))

import compact_fixture as fixture  # noqa: E402

from pnr.compact_flags import PARTS  # noqa: E402
from pnr.graph import BoardGraph, BoardOutline, Component, Pad  # noqa: E402

FLAGS = ("PNR_COMPACT", "PNR_SHRINK") + tuple("PNR_COMPACT_" + p for p in PARTS)
EPS = 1e-9


@contextlib.contextmanager
def flags(**values):
    """Every compact switch unset, then ``values`` set, for the block."""
    saved = {k: os.environ.get(k) for k in FLAGS}
    for k in FLAGS:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


ON = dict(PNR_COMPACT="1")


def part(ref, size=(2.0, 1.0), pos=(0.0, 0.0), rot=0.0, body=None, pads=None, footprint="t"):
    pads = pads or [
        Pad("1", "n" + ref, (-size[0] / 2 + 0.5, 0.0), (0.4, 0.4)),
        Pad("2", "m" + ref, (size[0] / 2 - 0.5, 0.0), (0.4, 0.4)),
    ]
    return Component(ref, footprint, pos, rot, "top", size, size, pads=pads, body=body)


def board(comps, w=20.0, h=10.0, nets=()):
    return BoardGraph("toy", comps, list(nets), BoardOutline(w, h))


def compiled(w=20.0, h=10.0, **board_extra):
    from pnr.constraints import compile_constraints

    return compile_constraints({"board": dict({"outline": {"w": w, "h": h}}, **board_extra)}, [])


class FlagsTest(unittest.TestCase):
    def test_parts_and_ablation(self):
        from pnr import compact_flags

        with flags():
            self.assertFalse(compact_flags.enabled())
            self.assertEqual(compact_flags.active(), {})
        with flags(PNR_COMPACT="1", PNR_COMPACT_GP="0", PNR_SHRINK="1"):
            self.assertTrue(compact_flags.enabled())
            self.assertFalse(compact_flags.enabled("GP"))
            self.assertTrue(compact_flags.enabled("COURTYARD"))
            self.assertEqual(
                compact_flags.active(),
                dict(RANK=True, LEGALIZE=True, COURTYARD=True, DROPS=True, SHRINK=True),
            )
            with self.assertRaises(ValueError):
                compact_flags.enabled("SPREAD")

    def test_part_lists_agree(self):
        """run.py --compact-off offers exactly the engine's parts (the ladder kind's list
        is checked by tests/unit/exp/test_kinds.py)."""
        import run

        self.assertEqual(run.COMPACT_PARTS, PARTS)


class IdentityTest(unittest.TestCase):
    """With the flags unset the engine reproduces the parent commit's outputs."""

    golden = json.loads((fixture.DATA / "identity.json").read_text())

    def test_flag_off_legalizer_and_starts_are_unchanged(self):
        with flags():
            for case in fixture.CASES:
                with self.subTest(case=case):
                    self.assertEqual(fixture.legal_digest(case), self.golden[case]["legal"])

    def test_flag_off_placement_is_unchanged(self):
        key = fixture.platform_key()
        cases = [c for c in fixture.CASES if key in self.golden[c]["place"]]
        if not cases:
            self.skipTest("no placement golden for " + key)
        with flags():
            for case in cases:
                with self.subTest(case=case):
                    self.assertEqual(fixture.place_digest(case), self.golden[case]["place"][key])


class FlagOffPathTest(unittest.TestCase):
    """Platform independent (CI runs no placement golden): with the flags unset the placer,
    the legalizer and the initial pool never call a compact-only function, and no part
    has an offset body, so they take their previous paths."""

    def test_flag_off_never_enters_the_compact_paths(self):
        from pnr.place import compact, geometry, model

        def refuse(name):
            def call(*args, **kwargs):
                raise AssertionError("compact-only %s called with the flags unset" % name)

            return call

        guarded = (
            "cluster_box",
            "box_coordinate",
            "occupied_box",
            "legalize_settings",
            "margins",
            "metrics",
            "rank_bucket",
            "scaled_constraints",
            "shrink_search",
        )
        real_body = geometry.compact_body

        def no_body(comp):
            out = real_body(comp)
            if out is not None:
                raise AssertionError("an offset body with the flags unset: " + comp.ref)
            return out

        with flags(), contextlib.ExitStack() as stack:
            for name in guarded:
                stack.enter_context(mock.patch.object(compact, name, refuse(name)))
            stack.enter_context(mock.patch.object(geometry, "compact_body", no_body))
            self.assertEqual(compact.spread(1.3), 1.3)
            self.assertEqual(compact.placement_clearance(compiled()), 0.2)
            for case in fixture.CASES:
                with self.subTest(case=case):
                    graph = fixture.load(case)[0]
                    self.assertIsNone(model.body_offsets(graph))
                    fixture.legal_digest(case)
                    fixture.place_digest(case)


class OffsetCourtyardTest(unittest.TestCase):
    def parts(self):
        """Off-centre parts of the fixtures: the 1x8 header (pin-1 origin, through-hole),
        the 1x3 header, a SOT-23-5 and an LED, plus a synthetic off-centre SMD part."""
        header = fixture.load("09-mcu-usb-31-header")[0]
        inverter = fixture.load("04-inverter-leds-8")[0]
        out = [header.component("J2"), inverter.component("J1"), inverter.component("U1")]
        out.append(inverter.component("D1"))
        pads = [Pad("1", "a", (0.0, 0.0), (0.6, 0.6)), Pad("2", "b", (2.0, 0.5), (0.6, 0.6))]
        out.append(part("X1", (6.0, 3.0), pos=(10.0, 5.0), body=(-1.0, -0.5, 3.0, 1.5), pads=pads))
        for c in out:
            c.pos = (20.0, 15.0)
        return out

    def test_courtyard_rect_is_the_turned_body_box(self):
        from types import SimpleNamespace

        from pnr.place.geometry import body_shift, courtyard_rect, set_component_side
        from pnr.place.regions import placed_boxes

        con = SimpleNamespace(params={})
        with flags(**ON):
            for comp in self.parts():
                for side in ("top", "bottom"):
                    c = copy.deepcopy(comp)
                    set_component_side(c, side)
                    for rot in (0.0, 90.0, 180.0, 270.0):
                        c.rot = rot
                        with self.subTest(ref=c.ref, side=side, rot=rot):
                            r = courtyard_rect(c)
                            x0, y0, x1, y1 = placed_boxes(c, con)[0]
                            self.assertAlmostEqual(r.left, x0, places=9)
                            self.assertAlmostEqual(r.right, x1, places=9)
                            self.assertAlmostEqual(r.bottom, y0, places=9)
                            self.assertAlmostEqual(r.top, y1, places=9)
                            sx, sy = body_shift(c)
                            self.assertAlmostEqual(r.cx - c.pos[0], sx, places=12)
                            self.assertAlmostEqual(r.cy - c.pos[1], sy, places=12)
                            with flags():
                                envelope = courtyard_rect(c)
                            # The body box lies inside the symmetric envelope.
                            self.assertGreaterEqual(r.left, envelope.left - EPS)
                            self.assertLessEqual(r.right, envelope.right + EPS)
                            self.assertGreaterEqual(r.bottom, envelope.bottom - EPS)
                            self.assertLessEqual(r.top, envelope.top + EPS)
                            # Every pad lies inside the body box.
                            from pnr.place.geometry import pad_rects

                            for _, _, pad in pad_rects(c):
                                self.assertGreaterEqual(pad.left, r.left - 1e-6)
                                self.assertLessEqual(pad.right, r.right + 1e-6)

    def test_centred_parts_and_flag_off_are_unchanged(self):
        from pnr.place.geometry import body_shift, courtyard_rect

        centred = part("C1", (3.0, 2.0), pos=(5.0, 4.0), rot=90.0)
        offset = part("X1", (4.0, 3.0), pos=(10.0, 5.0), body=(-1.0, -0.5, 3.0, 1.5))
        macro = part("MB00", (4.0, 3.0), body=(-1.0, -0.5, 3.0, 1.5), footprint="block:b")
        with flags():
            off = [courtyard_rect(c) for c in (centred, offset, macro)]
            self.assertIsNone(body_shift(offset))
        with flags(**ON):
            self.assertEqual(courtyard_rect(centred), off[0])
            self.assertIsNone(body_shift(centred))
            self.assertEqual(courtyard_rect(macro), off[2])  # macros keep their courtyard
            self.assertNotEqual(courtyard_rect(offset), off[1])
        with flags(PNR_COMPACT="1", PNR_COMPACT_COURTYARD="0"):
            self.assertEqual(courtyard_rect(offset), off[1])

    def test_edge_pose_and_keepout_follow_the_body(self):
        from pnr.constraints import compile_constraints
        from pnr.place.geometry import courtyard_rect, keepout_rects, resolve_fixed_poses

        header = part("J1", (3.63, 8.73), body=(-1.815, -4.365, 1.815, 1.815))
        g = board([header], 30.0, 20.0)
        doc = {
            "board": {"outline": {"w": 30, "h": 20}},
            "fixed": {"J1": {"edge": "south", "rot": 0}},
            "keepout": [{"name": "k", "ref": "J1", "extent": {"edge": "north", "depth_mm": 2.0}}],
        }
        cc = compile_constraints(doc, g.refs)
        with flags():
            pose = resolve_fixed_poses(g, cc)["J1"]
            self.assertAlmostEqual(pose[1], 8.73 / 2)
        with flags(**ON):
            pose = resolve_fixed_poses(g, cc)["J1"]
            header.pos = pose
            r = courtyard_rect(header)
            self.assertAlmostEqual(r.bottom, 0.0, places=9)  # flush with the south edge
            self.assertAlmostEqual(r.cx, 15.0, places=9)
            (k,) = keepout_rects(g, cc, {"J1": pose})
            self.assertAlmostEqual(k.bottom, r.top, places=9)
            self.assertAlmostEqual(k.h, 2.0, places=9)
            self.assertAlmostEqual(k.w, r.w, places=9)

    def test_pin1_origin_header_rung_places(self):
        """09-mcu-usb-31-header: the 1x8 header's symmetric envelope (39.19 mm, its origin
        is pin 1) cannot be placed on the 46 x 34 mm board at the ladder's spread; its body
        box can, with no hard violation."""
        from pnr.place.geometry import courtyard_rect
        from pnr.place.legalize import LegalizationError
        from pnr.place.metrics import hard_violations
        from pnr.place.placer import place

        g, cc, rules = fixture.load("09-mcu-usb-31-header")
        with flags():
            with self.assertRaises(LegalizationError):
                place(g, cc, seed=0, iters=60, spread=1.3, channel_rules=rules)
        with flags(**ON):
            placed, report = place(g, cc, seed=0, iters=60, spread=1.3, channel_rules=rules)
            self.assertTrue(report.legal, report.summary())
            self.assertFalse(any(hard_violations(placed, cc).values()))
            self.assertTrue(courtyard_rect(placed.component("J2")).inside(46.0, 34.0))


class LegalizerTest(unittest.TestCase):
    def test_settings_and_authored_gap(self):
        from pnr.constraints import BoardSpec, ConstraintError
        from pnr.place import compact

        g = board([part("A")])
        with flags():
            self.assertIsNone(compact.legalize_settings(g, compiled(), {}))
            self.assertEqual(compact.placement_clearance(compiled()), 0.2)
        with flags(**ON):
            tight = compact.legalize_settings(g, compiled(), {})
            self.assertEqual((tight.gap, tight.grid_mm), (compact.GAP_MM, compact.GRID_MM))
            self.assertEqual(
                compact.legalize_settings(g, compiled(courtyard_clearance_mm=0.05), {}).gap, 0.05
            )
            self.assertEqual(compact.placement_clearance(compiled()), compact.GAP_MM)
        with self.assertRaises(ConstraintError):
            compiled(courtyard_clearance_mm=-0.1)
        # Not a dataclass field: serialized constraints are unchanged.
        self.assertNotIn("courtyard_clearance_mm", dataclasses.asdict(BoardSpec()))

    def test_copper_margin_only_where_a_box_hugs_its_pads(self):
        from pnr.place import compact

        hugging = part("H", (2.0, 1.0), pads=[Pad("1", "a", (-0.75, 0.0), (0.5, 0.6))])
        library = part("L", (2.0, 1.0), pads=[Pad("1", "a", (-0.5, 0.0), (0.5, 0.5))])
        with flags(**ON):
            self.assertAlmostEqual(compact.pad_inset(hugging), 0.0)
            self.assertEqual(compact.margins(board([hugging, library]), 0.2), {"H": 0.1})

    def legalized(self, comps, w=20.0, h=10.0):
        from pnr.place import compact
        from pnr.place.legalize import legalize

        g = board(comps, w, h)
        cc = compiled(w, h, default_clearance_mm=0.4)
        tight = compact.legalize_settings(g, cc, {"fab": {"clearance_mm": 0.2}})
        kwargs = dict(clearance=0.4, grid_mm=0.25, spread=1.0)
        if tight is not None:
            kwargs = dict(
                clearance=tight.gap, grid_mm=tight.grid_mm, spread=1.0, margins=tight.margins
            )
        return legalize(g, w, h, fixed={}, keepouts=[], **kwargs)

    def test_compact_gap_packs_tighter_and_keeps_copper_clearance(self):
        from pnr.place.geometry import courtyard_rect, pad_rects

        def gap(a, b):
            return max(b.left - a.right, a.left - b.right, b.bottom - a.top, a.bottom - b.top)

        def comps(hug):
            # Pads on the box edge (hug: a copper margin applies) or 0.25 mm inside it.
            x = -0.75 if hug else -0.5
            return [
                part(
                    "P%d" % i,
                    (2.0, 1.0),
                    pos=(10.0, 5.0),
                    pads=[Pad("1", "a%d" % i, (x, 0.0), (0.5, 0.5))],
                )
                for i in range(2)
            ]

        for hug, least in ((False, 0.01), (True, 0.21)):
            with self.subTest(hug=hug):
                with flags():
                    loose = self.legalized(comps(hug))
                with flags(**ON):
                    tight = self.legalized(comps(hug))
                a, b = (courtyard_rect(c) for c in loose.components)
                self.assertGreaterEqual(gap(a, b), 0.4 - 1e-6)  # the routing clearance
                a, b = (courtyard_rect(c) for c in tight.components)
                self.assertGreaterEqual(gap(a, b), least - 1e-6)  # gap (+ copper margins)
                self.assertLess(gap(a, b), 0.4)
                pads = [r for c in tight.components for _, _, r in pad_rects(c)]
                self.assertGreaterEqual(gap(*pads), 0.2 - 1e-6)  # copper clearance kept

    def test_offset_bodies_take_their_own_slots(self):
        from pnr.place.geometry import body_shift, courtyard_rect
        from pnr.place.metrics import overlap_pairs

        headers = [
            part(
                "J%d" % i,
                (3.63, 8.73),
                pos=(6.0, 5.0),
                rot=90.0 * i,
                body=(-1.815, -4.365, 1.815, 1.815),
                pads=[
                    Pad("1", "a", (0.0, 0.0), (1.7, 1.7)),
                    Pad("2", "b", (0.0, -2.54), (1.7, 1.7)),
                ],
            )
            for i in range(4)
        ]
        with flags(**ON):
            placed = self.legalized(headers, 16.0, 12.0)
            self.assertEqual(overlap_pairs(placed, clearance=0.01 - 1e-6), [])
            for c in placed.components:
                r = courtyard_rect(c)
                self.assertTrue(r.inside(16.0, 12.0), c.ref)
                g = 0.125
                # The body box centre is on the slot lattice; pos is the origin behind it.
                sx, sy = body_shift(c)
                self.assertAlmostEqual(((c.pos[0] + sx) / (g / 2)) % 1.0, 0.0, places=6)
                self.assertAlmostEqual(((c.pos[1] + sy) / (g / 2)) % 1.0, 0.0, places=6)


class RankTest(unittest.TestCase):
    def test_route_rank_never_trades_completion(self):
        from pnr.place.initial_pool import route_rank

        complete = dict(objective=[0, 0, 9, 300.0], bucket=19)
        incomplete = dict(objective=[1, 1, 0, 10.0], bucket=0)
        dense = dict(objective=[0, 0, 12, 400.0], bucket=8)
        self.assertLess(route_rank(complete), route_rank(incomplete))
        self.assertLess(
            route_rank(dict(complete, length_unmatched=0)),
            route_rank(dict(complete, length_unmatched=1, bucket=0)),
        )
        # Equal completion: vias first (a fixed outline does not pay for the bbox), then
        # the bucket, then length.
        with flags(**ON):
            self.assertLess(route_rank(complete), route_rank(dense))
            self.assertEqual(route_rank(complete), (0, 0, 0, 9, 19, 300.0))
            same_vias = dict(objective=[0, 0, 9, 200.0], bucket=20)
            self.assertLess(route_rank(complete), route_rank(same_vias))
        # PNR_SHRINK (the outline follows the bbox): the bucket before the vias.
        with flags(PNR_COMPACT="1", PNR_SHRINK="1"):
            self.assertLess(route_rank(dense), route_rank(complete))
            self.assertEqual(route_rank(complete), (0, 0, 0, 19, 9, 300.0))
        # Without a bucket (the flag off) the key is the previous one.
        self.assertEqual(route_rank(dict(objective=[0, 0, 9, 300.0])), (0, 0, 0, 9, 300.0))

    def test_halving_native_key_puts_bbox_after_completion(self):
        from pnr.mc.halving import _rank_key

        a = dict(id="p001", objective=[0, 0, 0, 0, 0, 0], compactness=dict(bbox_mm2=900.0))
        b = dict(id="p002", objective=[0, 0, 0, 0, 0, 0], compactness=dict(bbox_mm2=400.0))
        c = dict(id="p003", objective=[1, 0, 0, 0, 0, 0], compactness=dict(bbox_mm2=100.0))
        with flags():
            key = _rank_key("native")
            self.assertEqual(
                [r["id"] for r in sorted([c, b, a], key=key)], ["p001", "p002", "p003"]
            )
        with flags(**ON):
            key = _rank_key("native")
            self.assertEqual(
                [r["id"] for r in sorted([c, b, a], key=key)], ["p002", "p001", "p003"]
            )

    def test_metrics_and_cluster_box(self):
        from pnr.constraints import compile_constraints
        from pnr.place import compact

        a = part("A", (2.0, 2.0), pos=(1.0, 1.0))
        b = part("B", (2.0, 2.0), pos=(5.0, 3.0), body=(-1.0, -1.0, 1.0, 1.0))
        m = compact.metrics(board([a, b], 10.0, 10.0), 10.0, 10.0)
        self.assertEqual(m["bbox_mm2"], 24.0)  # (0..6) x (0..4)
        self.assertEqual(m["area_mm2"], 8.0)
        self.assertEqual(m["utilization"], round(8 / 24, 3))
        self.assertEqual(m["occupancy"], 0.08)
        self.assertEqual(m["bucket"], 4)
        g = board([a, b], 40.0, 20.0)
        cc = compile_constraints(
            {"board": {"outline": {"w": 40, "h": 20}}, "fixed": {"A": {"at": [2, 18]}}}, g.refs
        )
        x0, y0, w, h = compact.cluster_box(g, cc, 40.0, 20.0)
        self.assertAlmostEqual(w * h, 2 * 8.0)
        self.assertAlmostEqual(w / h, 2.0)
        self.assertAlmostEqual(x0, 0.0)  # centred on A, clamped inside the outline
        self.assertAlmostEqual(y0 + h / 2, 18.0)


class GlobalLossTest(unittest.TestCase):
    def test_gp_loss_matches_cost_capture_with_offset_bodies(self):
        from pnr.place.model import global_place

        g, cc, _ = fixture.load("04-inverter-leds-8")
        with tempfile.TemporaryDirectory() as tmp, flags(**ON):
            with mock.patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": tmp}):
                global_place(g, cc, 26.0, 20.0, seed=0, iters=25, spread=1.0)
            files = sorted(Path(tmp).glob("*.json"))
            failed = [p.name for p in files if p.name.startswith("failed-")]
            self.assertEqual(failed, [])
            kinds = [json.loads(p.read_text())["kind"] for p in files]
            self.assertIn("global-objective", kinds)
            doc = next(json.loads(p.read_text()) for p in files)
            self.assertIn("effective_shift", doc)


class ShrinkTest(unittest.TestCase):
    def test_search_order_and_choice(self):
        from pnr.place import compact

        seen = []

        def probe(w, h):
            seen.append((w, h))
            return (w, h), w >= 30.0

        size, best, records = compact.shrink_search(probe, 40.0, 20.0, 0.5, first=0.9)
        self.assertEqual(seen, [(36.0, 18.0), (28.0, 14.0), (32.0, 16.0), (30.0, 15.0)])
        self.assertEqual(size, (30.0, 15.0))
        self.assertEqual(best, (30.0, 15.0))
        self.assertEqual([r["converged"] for r in records], [True, False, True, True])
        # Nothing converges: no choice (the envelope run stays).
        self.assertEqual(
            compact.shrink_search(lambda w, h: (None, False), 40.0, 20.0, 0.5)[:2], (None, None)
        )

    def test_scaled_constraints_and_lower_bound(self):
        from pnr.constraints import compile_constraints
        from pnr.place import compact

        g = board(
            [part("J1", (4.0, 2.0)), part("U1", (2.0, 2.0)), part("J2", (2.0, 4.0))], 40.0, 20.0
        )
        cc = compile_constraints(
            {
                "board": {"outline": {"w": 40, "h": 20}},
                "fixed": {"J1": {"at": [2, 10]}, "J2": {"at": [39, 12]}},
            },
            g.refs,
        )
        scaled = compact.scaled_constraints(cc, 40.0, 20.0, 30.0, 15.0)
        at = {c.refs[0]: c.params["at"] for c in scaled.constraints if c.kind == "fixed"}
        # A fixed at keeps its absolute position (mid-board y included); one on the far
        # (east) edge's band keeps its distance to that edge.
        self.assertEqual(at["J1"], [2.0, 10.0])
        self.assertEqual(at["J2"], [29.0, 12.0])
        self.assertEqual(
            compact.moved_fixed(cc, scaled), [dict(refs=["J2"], at=[39.0, 12.0], to=[29.0, 12.0])]
        )
        self.assertEqual((scaled.board.width, scaled.board.height), (30.0, 15.0))
        self.assertEqual(cc.board.width, 40)  # a copy
        lower = compact.shrink_lower_bound(g, cc, 40.0, 20.0)
        self.assertGreaterEqual(lower, math.sqrt(16.0 / (0.7 * 800.0)))
        # J2 (at y 12, 4 mm tall, not in the north band) must end below the scaled outline.
        self.assertGreaterEqual(lower, 14.0 / 20.0 - 1e-9)
        self.assertLess(lower, 1.0)
        self.assertEqual(compact.shrink_skip_reason(cc), None)
        self.assertEqual(compact.shrink_skip_reason(cc, auto_outline=True), "auto_outline")

    def test_route_and_place_keeps_the_smallest_converged_outline(self):
        from pnr.constraints import compile_constraints
        from pnr.place.placer import PlacementReport
        from pnr.route import feedback

        g = board(
            [part("A", (2.0, 2.0), pos=(3.0, 3.0)), part("B", (2.0, 2.0), pos=(9.0, 5.0))],
            40.0,
            20.0,
        )
        cc = compile_constraints({"board": {"outline": {"w": 40, "h": 20}}}, g.refs)
        calls = []

        def loop(graph, constraints, **kwargs):
            w, h = constraints.board.width, constraints.board.height
            calls.append((w, h))
            placed = BoardGraph.from_json(graph.to_json())
            placed.outline = BoardOutline(w, h)
            report = feedback.FeedbackReport(rounds=1, converged=w >= 20.0)
            report.placement = PlacementReport(w, h, 0.0, 0.0)
            return placed, report

        with flags(PNR_SHRINK="1"), mock.patch.object(feedback, "_place_route_loop", loop):
            placed, report = feedback.route_and_place(
                g, cc, detail_rules={"fab": {"edge_clearance_mm": 0.3}}
            )
        self.assertEqual(calls[0], (40.0, 20.0))  # the envelope first
        self.assertLessEqual(len(calls), 1 + 4)
        self.assertEqual(report.shrink["chosen"], [placed.outline.width, placed.outline.height])
        self.assertGreaterEqual(placed.outline.width, 20.0)
        self.assertLess(placed.outline.width * placed.outline.height, 800.0)
        self.assertEqual((cc.board.width, cc.board.height), report.outline)
        # Keep-outs are skipped and recorded so.
        cc2 = compile_constraints(
            {
                "board": {"outline": {"w": 40, "h": 20}},
                "keepout": [{"name": "k", "polygon": [[0, 0], [2, 0], [2, 2]]}],
            },
            g.refs,
        )
        with flags(PNR_SHRINK="1"), mock.patch.object(feedback, "_place_route_loop", loop):
            _, report = feedback.route_and_place(g, cc2)
        self.assertEqual(report.shrink["skipped"], "keep-outs")


class RunnerTest(unittest.TestCase):
    def test_switches_and_audit(self):
        import run

        self.assertEqual(run.compact_environment(False), {})
        self.assertEqual(
            run.compact_environment(True, ["GP"], True),
            dict(PNR_COMPACT="1", PNR_COMPACT_GP="0", PNR_SHRINK="1"),
        )
        with self.assertRaises(ValueError):
            run.compact_environment(False, ["GP"])
        args = run.parser().parse_args(
            ["--out", "x", "--compact", "--compact-off", "RANK", "--shrink"]
        )
        self.assertEqual((args.compact, args.compact_off, args.shrink), (True, ["RANK"], True))
        spec = dict(
            constraints=dict(
                board=dict(outline=dict(w=40, h=20)),
                edge_align={"J1": dict(edge="south", hard=True, tolerance_mm=0.5)},
            )
        )
        placed = dict(
            outline=dict(width=30.0, height=15.0),
            components=[
                dict(
                    ref="J1",
                    pos=[10.0, 4.4],
                    rot=0.0,
                    courtyard=[3.63, 8.73],
                    body=[-1.815, -4.365, 1.815, 1.815],
                )
            ],
        )
        _, findings = run.constraint_reasons(spec, placed, use_body=True)
        self.assertEqual(findings, [])  # the body's bottom is 0.035 mm from the edge
        # A north edge part is judged on the design's outline unless the run shrank it.
        north = dict(spec, constraints=dict(spec["constraints"], edge_align={}))
        north["constraints"]["edge_align"] = {"J1": dict(edge="north", hard=True, tolerance_mm=0.5)}
        top = dict(placed, components=[dict(placed["components"][0], pos=[10.0, 13.0])])
        self.assertEqual(len(run.constraint_reasons(north, top, use_body=True)[1]), 1)
        self.assertEqual(run.constraint_reasons(north, top, use_body=True, shrunk=True)[1], [])
        _, findings = run.constraint_reasons(spec, placed)
        self.assertEqual(len(findings), 0 if 4.4 - 8.73 / 2 <= 0.5 else 1)
        measured = run.compactness(placed)
        self.assertEqual(measured["outline_mm2"], 450.0)
        self.assertEqual(measured["bbox_mm"], [3.63, 6.18])


class MarginPathTest(unittest.TestCase):
    """A part whose box hugs its pads (a copper margin) keeps it after the legalizer too:
    the align snap and the feedback moves add both parts' margins to the gap."""

    def hugging(self, ref, pos):
        return part(ref, (2.0, 1.0), pos=pos, pads=[Pad("1", "h" + ref, (-0.75, 0.0), (0.5, 1.0))])

    def test_align_snap_keeps_the_margin(self):
        from pnr.constraints import compile_constraints
        from pnr.place import compact
        from pnr.place.regions import snap_aligns

        def boards():
            g = board(
                [part("A", (2.0, 1.0), pos=(4.0, 5.0)), part("B", (2.0, 1.0), pos=(10.0, 6.0))]
                + [self.hugging("H", (10.0, 3.9))]
            )
            cc = compile_constraints(
                {
                    "board": {"outline": {"w": 20, "h": 10}},
                    "fixed": {"A": {"at": [4, 5]}},
                    "align": [
                        dict(name="al", refs=["A", "B"], axis="y", anchor="origin", tol_mm=0.0)
                        | dict(hard=True)
                    ],
                },
                ["A", "B", "H"],
            )
            return g, cc

        with flags(**ON):
            g, cc = boards()
            margins = compact.margins(g, 0.2)
            self.assertEqual(margins, {"H": 0.1})
            # B down to y 5 leaves 0.1 mm to H: more than the gap, less than gap + margin.
            self.assertEqual(snap_aligns(g, cc, compact.GAP_MM, None, (20.0, 10.0)), ["B"])
            g, cc = boards()
            self.assertEqual(
                snap_aligns(g, cc, compact.GAP_MM, None, (20.0, 10.0), margins=margins), []
            )
            self.assertEqual(g.component("B").pos, (10.0, 6.0))

    def test_feedback_move_keeps_the_margin(self):
        from pnr.feedback.moves import MoveBoard
        from pnr.place import compact

        with flags(**ON):
            g = board([part("B", (2.0, 1.0), pos=(10.0, 6.0)), self.hugging("H", (10.0, 3.9))])
            cc = compiled()
            plain = MoveBoard(g, cc, {}, frozenset(), clearance=compact.GAP_MM)
            kept = MoveBoard(
                g, cc, {}, frozenset(), clearance=compact.GAP_MM, margins=compact.margins(g, 0.2)
            )
            g.component("B").pos = (10.0, 5.0)  # 0.1 mm from H
            self.assertIsNone(plain._local(["B"]))
            self.assertEqual(kept._local(["B"]), "overlap")


class DropsTest(unittest.TestCase):
    """PNR_COMPACT DROPS: a legacy plane_layer net's surface pads get their through-via
    drops from the detailed router (writeback dog-bones them otherwise)."""

    def route(self):
        from pnr.constraints import compile_constraints, compile_routing_rules
        from pnr.graph import Net
        from pnr.route.detail.router import route_board

        def resistor(ref, pos):
            return Component(
                ref,
                "R_0805",
                pos,
                0.0,
                "top",
                (3.2, 1.6),
                (3.2, 1.6),
                pads=[
                    Pad("1", "SIG", (-0.95, 0.0), (1.0, 1.4)),
                    Pad("2", "GND", (0.95, 0.0), (1.0, 1.4)),
                ],
            )

        comps = [resistor("R1", (6.0, 5.0)), resistor("R2", (14.0, 5.0))]
        nets = [Net("SIG", [("R1", "1"), ("R2", "1")]), Net("GND", [("R1", "2"), ("R2", "2")])]
        g = BoardGraph("toy", comps, nets, BoardOutline(20.0, 10.0))
        cc = compile_constraints(
            {
                "board": {"outline": {"w": 20, "h": 10}, "layers": 4},
                "net_class": {
                    "return": {"nets": ["GND"], "width_mm": 0.4, "plane_layer": "In1.Cu"}
                },
            },
            g.refs,
        )
        rules = compile_routing_rules(cc, ["SIG", "GND"])
        return route_board(g, cc, rules, pitch=0.25, max_iters=4)

    def test_legacy_plane_pads_get_router_drops(self):
        with flags():
            off = self.route()
        with flags(**ON):
            on = self.route()
        with flags(PNR_COMPACT="1", PNR_COMPACT_DROPS="0"):
            ablated = self.route()
        self.assertEqual([v for v in off.vias if v[0] == "GND"], [])
        self.assertEqual(off.vias, ablated.vias)
        self.assertEqual(off.result.unrouted, [])
        drops = [v for v in on.vias if v[0] == "GND"]
        self.assertEqual(len(drops), 2)  # one per GND pad, through In1.Cu's own region
        self.assertEqual(on.result.unrouted, [])
        stubs = [t for t in on.tracks if t[0] == "GND"]
        self.assertTrue(stubs and all(t[1] == "F.Cu" and t[4] >= 0.4 - 1e-9 for t in stubs))


class DriverTest(unittest.TestCase):
    def test_hier_utilisations_follow_gp(self):
        import hier_case

        spec = {}
        base = hier_case.budget_of(spec)["utilisations"]
        with flags(**ON):
            self.assertEqual(
                hier_case.budget_of(spec)["utilisations"],
                list(base) + [u for u in (0.5, 0.6) if u not in base],
            )
        with flags(PNR_COMPACT="1", PNR_COMPACT_GP="0"):
            self.assertEqual(hier_case.budget_of(spec)["utilisations"], base)

    def test_animation_fallback_arguments(self):
        from types import SimpleNamespace

        import animate_ladder

        same = SimpleNamespace(runner_arg=["--compact", "--gloss"], fallback_runner_arg=None)
        self.assertEqual(animate_ladder.fallback_arguments(same), ["--compact", "--gloss"])
        own = SimpleNamespace(runner_arg=["--compact", "--gloss"], fallback_runner_arg=["--gloss"])
        self.assertEqual(animate_ladder.fallback_arguments(own), ["--gloss"])
        none = SimpleNamespace(runner_arg=["--compact"], fallback_runner_arg=[""])
        self.assertEqual(animate_ladder.fallback_arguments(none), [])

    def test_animation_results_gloss(self):
        import animate_ladder

        self.assertIsNone(animate_ladder.gloss_result({}))
        summary = dict(
            accepted_transactions=7,
            proposed_transactions=7,
            metrics_before=dict(bends_total=39, length_mm=67.006),
            metrics_after=dict(bends_total=12, length_mm=63.033),
        )
        self.assertEqual(
            animate_ladder.gloss_result(dict(gloss=dict(kept=True, summary=summary))),
            dict(kept=True, accepted=7, proposed=7, bends=[39, 12], length_mm=[67.006, 63.033]),
        )


class TraceTest(unittest.TestCase):
    def test_header_body_and_renderer_rect(self):
        from pnr.animate.render import courtyard_rect as drawn
        from pnr.place.geometry import courtyard_rect, set_component_side
        from pnr.trace import board_header, um

        comp = part("J1", (3.63, 8.73), pos=(10.0, 8.0), body=(-1.815, -4.365, 1.815, 1.815))
        g = board([comp], 30.0, 20.0)
        with flags():
            self.assertNotIn("body", board_header(g)["components"][0])
        with flags(**ON):
            entry = board_header(g)["components"][0]
            self.assertEqual(entry["body"], [um(v) for v in comp.body])
            for side in ("top", "bottom"):
                for rot in (0.0, 90.0, 180.0, 270.0):
                    c = copy.deepcopy(comp)
                    set_component_side(c, side)
                    c.rot = rot
                    r = courtyard_rect(c)
                    x, y, w, h = drawn(entry, (um(10.0), um(8.0), rot, side))
                    self.assertEqual((x, y, w, h), (um(r.cx), um(r.cy), um(r.w), um(r.h)))

    def test_shrink_choice_sets_the_outline(self):
        from pnr.provenance import Trace

        header = dict(outline=dict(w=40000, h=20000, polygon=None), components=[], nets=[])
        events = [
            dict(
                seq=0,
                kind="scope_begin",
                scope="shrink-00",
                type="stage",
                parent="",
                meta=dict(outline=[40000, 20000]),
            ),
            dict(seq=1, kind="scope_end", scope="shrink-00", type="stage", status="ok", metrics={}),
            dict(
                seq=2,
                kind="scope_begin",
                scope="shrink-01",
                type="stage",
                parent="",
                meta=dict(outline=[30000, 15000]),
            ),
            dict(seq=3, kind="scope_end", scope="shrink-01", type="stage", status="ok", metrics={}),
            dict(
                seq=4,
                kind="select",
                scope="",
                id="shrink",
                among=["shrink-00", "shrink-01"],
                chosen="shrink-01",
                criterion="outline-area",
                scores={},
            ),
        ]
        trace = Trace(header=header, lanes={"engine": events})
        self.assertEqual(trace.shrink_choice(), "shrink-01")
        self.assertEqual(
            (trace.header["outline"]["w"], trace.header["outline"]["h"]), (30000, 15000)
        )


if __name__ == "__main__":
    unittest.main()
