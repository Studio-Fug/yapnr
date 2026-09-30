"""N-0001: block macro shrink (PNR_MACRO_SHRINK), per-side hull (PNR_MACRO_HULL)
and used-area library ranking (PNR_LIBRARY_RANK_USED); review fixes: the hull's
inner plane (drilled parts vs block inner copper), holes in the shrunk extent
(fab hole-to-edge), pad/drill edge clearance in the legalizer
(PNR_PAD_EDGE_CLEARANCE), generations refused with hulls, PNR_KICAD_PYTHON.

Synthetic cases pin the mask legalization and overlap semantics; the real
nb6-fb converter / pd layouts (routed boards read without KiCad) pin the
geometry, the rigid expand and where assembled block copper lands. The
flags-off identity against the pristine ``src12n.base`` tree runs in a
subprocess; the slow hierarchical placements run with PNR_SLOW_TESTS=1.
"""

import hashlib
import json
import math
import os
import random
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.hier import extent as E
from pnr.place import hull as H
from pnr.place.geometry import Rect, courtyard_rect, pad_rects, placement_rects, pin_positions
from pnr.place.legalize import legalize
from pnr.place.metrics import hard_violations, overlap_pairs

HERE = Path(__file__).resolve()
HIER = HERE.parents[4]
NB6 = HIER / "blocks" / "nb6-fb"
INPUTS = HIER / "inputs10b"
CONSTRAINTS = HERE.parents[2] / "splanc_dev" / "mini-constraints.yaml"
SNAPSHOT = HIER / "runs" / "h6-hier" / "library.snapshot.json"
BASE_TREE = HIER / "src12n.base" / "hardware" / "pnr"
HAVE_REAL = all(p.exists() for p in (NB6, INPUTS / "graph.json", CONSTRAINTS, SNAPSHOT))
SLOW = os.environ.get("PNR_SLOW_TESTS") == "1"
CL = 0.2  # Mini default placement clearance
G = 0.25  # placement grid


def flags(**kw):
    env = {k: v for k, v in kw.items()}
    return mock.patch.dict(os.environ, env)


def off():
    """Environment with every N-0001 flag removed."""
    patch = mock.patch.dict(os.environ, {})
    patch.start()
    for k in (
        "PNR_MACRO_SHRINK",
        "PNR_MACRO_HULL",
        "PNR_LIBRARY_RANK_USED",
        "PNR_LIBRARY_RANK_USED_Q",
        "PNR_PAD_EDGE_CLEARANCE",
        "PNR_KICAD_PYTHON",
        "PNR_POWER_FIRST",
    ):
        os.environ.pop(k, None)
    return patch


# ----------------------------------------------------------------------------- geometry helpers


def seg_rect_dist(ax, ay, bx, by, x0, y0, x1, y1):
    """Distance between a segment and a closed axis-aligned rectangle (0 when they meet)."""
    t0, t1, dx, dy, inter = 0.0, 1.0, bx - ax, by - ay, True
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                inter = False
                break
        else:
            r = q / p
            if p < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
    if inter and t0 <= t1:
        return 0.0

    def pt_rect(px, py):
        return math.hypot(max(0, x0 - px, px - x1), max(0, y0 - py, py - y1))

    def pt_seg(px, py):
        L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    return min(
        pt_rect(ax, ay), pt_rect(bx, by), *(pt_seg(cx, cy) for cx in (x0, x1) for cy in (y0, y1))
    )


def rect_gap(a: Rect, b: Rect):
    return math.hypot(
        max(0, a.left - b.right, b.left - a.right), max(0, a.bottom - b.top, b.bottom - a.top)
    )


def in_union(x, y, rects, eps=1e-9):
    return any(
        r.left - eps <= x <= r.right + eps and r.bottom - eps <= y <= r.top + eps for r in rects
    )


# ----------------------------------------------------------------------------- synthetic macro

TRACK_W = 0.2
C_CU = 0.35


def synthetic_geometry():
    """10 x 6 macro (block frame centred on 0): a member on the left third (top),
    a top track across y=1.5, one through via at (3, 2.4). Top pocket: x > -1,
    y < 1.5 - track; bottom: free except the via."""
    top = dict(
        rect=[[-5.0, -3.0, -1.0, 3.0]],
        cap=[[-4.6, 1.5, 4.6, 1.5, TRACK_W / 2 + C_CU], [3.0, 2.4, 3.0, 2.4, 0.225 + C_CU]],
    )
    bottom = dict(rect=[], cap=[[3.0, 2.4, 3.0, 2.4, 0.225 + C_CU]])
    return E.BlockGeometry(
        ok=True,
        extent=(-5.0, -3.0, 5.0, 3.0),
        shapes=dict(top=top, bottom=bottom),
        c_cu=C_CU,
        margin=0.3,
        track_width=TRACK_W,
    )


MACRO_POS = (15.125, 10.125)  # a legalizer slot centre for the 10 x 6 macro at every quarter turn


def synthetic_macro(ref="MB00", pos=MACRO_POS, rot=0.0):
    hull = E.build_hull(synthetic_geometry(), (0.0, 0.0), (10.0, 6.0), CL)
    return Component(
        ref,
        "block:test",
        pos,
        rot,
        "top",
        (10.0, 6.0),
        (10.0, 6.0),
        pads=[Pad("U1.1", "N1", (-3.0, 0.0), (1.0, 1.0))],
        address="block:test",
        hull=hull,
    )


def small(ref, pos, side="top", size=(1.0, 0.6), through=False):
    return Component(
        ref,
        "R0402",
        pos,
        0.0,
        side,
        size,
        size,
        pads=[
            Pad("1", "N1", (0.0, 0.0), (0.4, 0.4), through, (0.3, 0.3) if through else (0.0, 0.0))
        ],
    )


def world(macro, u, v):
    k = H.quarter(macro.rot)
    x, y = H._xf_point(u, v, k, False)
    return macro.pos[0] + x, macro.pos[1] + y


def physical_copper_gap(macro, part):
    """Least distance from block copper (track/via edges) on the part's sides to its courtyard."""
    k = H.quarter(macro.rot)
    cr = courtyard_rect(part)
    sides = ("top", "bottom") if any(p.through_hole for p in part.pads) else (part.side,)
    best = math.inf
    geo = synthetic_geometry()
    for side in sides:
        for ax, ay, bx, by, r in geo.shapes[side]["cap"]:
            (pax, pay), (pbx, pby) = (world(macro, ax, ay), world(macro, bx, by))
            best = min(
                best,
                seg_rect_dist(pax, pay, pbx, pby, cr.left, cr.bottom, cr.right, cr.top)
                - (r - C_CU),
            )
    return best


def member_rects(macro):
    k = H.quarter(macro.rot)
    out = []
    for x0, y0, x1, y1 in synthetic_geometry().shapes["top"]["rect"]:
        a, b = world(macro, x0, y0), world(macro, x1, y1)
        out.append(Rect((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, abs(a[0] - b[0]), abs(a[1] - b[1])))
    return out


class GraphHullJsonTest(unittest.TestCase):
    def test_default_json_has_no_hull_key(self):
        g = BoardGraph("t", [small("R1", (1, 1))])
        self.assertNotIn("hull", g.to_json())
        self.assertIsNone(BoardGraph.from_json(g.to_json()).components[0].hull)

    def test_hull_round_trips(self):
        g = BoardGraph("t", [synthetic_macro()])
        back = BoardGraph.from_json(g.to_json())
        self.assertEqual(back.components[0].hull, g.components[0].hull)
        self.assertEqual(back.to_json(), g.to_json())


class RasterTest(unittest.TestCase):
    def test_fft_correlation_matches_brute_force_all_parities(self):
        rng = np.random.default_rng(3)
        for ny, nx in ((40, 37), (33, 50)):
            occ = rng.random((ny, nx)) < 0.2
            for bh, bw in ((5, 6), (6, 5), (7, 7), (8, 8), (1, 1)):
                mask = rng.random((bh, bw)) < 0.4
                fft = H.correlate(occ, mask) < 0.5
                brute = H.correlate_brute(occ, mask) < 0.5
                np.testing.assert_array_equal(fft, brute)

    def test_free_map_is_mask_and_keepout_exact(self):
        rng = np.random.default_rng(5)
        occ = {s: rng.random((30, 31)) < 0.1 for s in ("top", "bottom")}
        keep = np.zeros((30, 31), bool)
        keep[10:12, 20:25] = True
        masks = {"top": rng.random((6, 7)) < 0.5, "bottom": np.zeros((6, 7), bool)}
        free = H.free_map(occ, keep, masks, 7, 6)
        for r in range(free.shape[0]):
            for c in range(free.shape[1]):
                ok = (
                    not (occ["top"][r : r + 6, c : c + 7] & masks["top"]).any()
                    and not keep[r : r + 6, c : c + 7].any()
                )
                self.assertEqual(bool(free[r, c]), ok, (r, c))

    def test_capsule_raster_is_exact_cell_cover(self):
        rng = random.Random(1)
        for _ in range(30):
            bw, bh = rng.randint(8, 21), rng.randint(8, 21)
            cap = [
                rng.uniform(-2, 2),
                rng.uniform(-2, 2),
                rng.uniform(-2, 2),
                rng.uniform(-2, 2),
                rng.uniform(0.05, 0.6),
            ]
            mask = H.raster(dict(cap=[cap]), 0.0, bw, bh, G)
            xe, ye = H.lattice(bw, G), H.lattice(bh, G)
            for i in range(bh):
                for j in range(bw):
                    d = seg_rect_dist(*cap[:4], xe[j], ye[i], xe[j + 1], ye[i + 1])
                    if abs(d - cap[4]) > 1e-7:
                        self.assertEqual(bool(mask[i, j]), d < cap[4], (cap, i, j, d))

    def test_rect_raster_matches_legalizer_block(self):
        # A courtyard grown by clearance/2 fills exactly the legalizer's ceil((w + cl)/g) block.
        for w, h in ((1.0, 0.6), (1.6, 0.8), (2.35, 1.15)):
            bw, bh = int(math.ceil((w + CL) / G)), int(math.ceil((h + CL) / G))
            mask = H.raster(dict(rect=[[-w / 2, -h / 2, w / 2, h / 2]]), CL / 2, bw, bh, G)
            self.assertTrue(mask[:, 1:-1].all() and mask[1:-1, :].all(), (w, h))

    def test_enclosed_pocket_is_filled_open_one_is_not(self):
        m = np.zeros((12, 12), bool)
        m[2, 2:10] = m[9, 2:10] = m[2:10, 2] = m[2:10, 9] = True
        filled, n = H.fill_pockets(m)
        self.assertEqual(n, 36)
        self.assertTrue(filled[3:9, 3:9].all())
        m[5, 9] = False  # a one-cell exit
        filled, n = H.fill_pockets(m)
        self.assertEqual(n, 0)
        filled, n = H.fill_pockets(m, erode_cells=1)  # but not wide enough for a 3-cell channel
        self.assertEqual(n, 37)  # the pocket and its one-cell exit


class SyntheticHullTest(unittest.TestCase):
    def setUp(self):
        self.env = off()
        os.environ["PNR_MACRO_HULL"] = "1"

    def tearDown(self):
        self.env.stop()

    def run_legalize(self, parts, macro_rot=0.0, width=30.0, height=20.0, fixed=None, keepouts=()):
        macro = synthetic_macro(rot=macro_rot)
        g = BoardGraph("t", [macro] + parts)
        return legalize(
            g,
            width,
            height,
            fixed=fixed or {},
            keepouts=list(keepouts),
            clearance=CL,
            grid_mm=G,
            rotations={"MB00": macro_rot},
        )

    def test_hull_lattice_and_stats(self):
        hull = synthetic_macro().hull
        self.assertEqual(hull["cells"], [int(math.ceil(10.2 / G)), int(math.ceil(6.2 / G))])
        self.assertAlmostEqual(hull["cap_relief"], CL)
        self.assertLess(hull["stats"]["top"]["fraction"], 0.7)
        self.assertLess(hull["stats"]["bottom"]["fraction"], 0.1)

    def test_top_part_nests_into_free_top_pocket(self):
        target = world(synthetic_macro(), 2.0, -1.0)
        out = self.run_legalize([small("R1", target)])
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual(m.pos, MACRO_POS)
        self.assertLess(math.dist(p.pos, target), 0.2)
        self.assertTrue(courtyard_rect(p).overlaps(courtyard_rect(m)))  # nested in the macro
        self.assertEqual(overlap_pairs(out), [])
        self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9)
        self.assertTrue(all(rect_gap(courtyard_rect(p), r) >= CL - 1e-9 for r in member_rects(m)))
        with flags(PNR_MACRO_HULL="0"):  # full rectangle again
            self.assertEqual(overlap_pairs(out), [("MB00", "R1")])

    def test_top_part_is_refused_over_top_copper(self):
        out = self.run_legalize([small("R1", world(synthetic_macro(), 2.0, 1.5))])  # on the track
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual(m.pos, MACRO_POS)
        self.assertEqual(overlap_pairs(out), [])
        self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9)
        self.assertGreater(math.dist(p.pos, world(m, 2.0, 1.5)), 0.5)

    def test_via_blocks_both_sides(self):
        out = self.run_legalize(
            [small("R1", world(synthetic_macro(), 3.0, 2.4), side="bottom", size=(0.6, 0.4))]
        )
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual(m.pos, MACRO_POS)
        self.assertEqual(overlap_pairs(out), [])
        self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9)

    def test_bottom_part_nests_under_top_copper(self):
        target = world(synthetic_macro(), -3.0, 1.5)  # under member and track
        out = self.run_legalize([small("R1", target, side="bottom")])
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual(m.pos, MACRO_POS)
        self.assertLess(math.dist(p.pos, target), 0.2)
        self.assertEqual(overlap_pairs(out), [])

    def test_through_hole_part_needs_both_sides_free(self):
        out = self.run_legalize([small("J1", world(synthetic_macro(), -3.0, 1.5), through=True)])
        m, p = out.component("MB00"), out.component("J1")
        self.assertEqual(m.pos, MACRO_POS)
        self.assertEqual(overlap_pairs(out), [])
        self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9)
        self.assertTrue(all(rect_gap(courtyard_rect(p), r) >= CL - 1e-9 for r in member_rects(m)))

    def test_copper_at_the_courtyard_edge_gets_no_hull(self):
        geo = synthetic_geometry()
        geo.shapes["top"]["cap"].append([-5.0, 0.0, -4.95, 0.0, TRACK_W / 2 + C_CU])
        with self.assertRaises(ValueError):
            E.build_hull(geo, (0.0, 0.0), (10.0, 6.0), CL)

    def test_keepout_blocks_the_whole_macro_courtyard(self):
        x, y = world(synthetic_macro(), 3.0, -2.0)  # inside the free pocket
        keep = Rect(x, y, 0.5, 0.5)
        out = self.run_legalize([], keepouts=[keep])
        self.assertNotEqual(out.component("MB00").pos, MACRO_POS)
        self.assertFalse(courtyard_rect(out.component("MB00")).overlaps(keep))

    def test_masks_rotate_with_the_macro(self):
        macro = synthetic_macro()
        bw, bh = macro.hull["cells"]
        m0 = H.slot_masks(macro, G, CL, bw, bh)
        for k in (1, 2, 3):
            macro.rot = 90.0 * k
            mk = H.slot_masks(macro, G, CL, *(bw, bh) if k % 2 == 0 else (bh, bw))
            for side in ("top", "bottom"):
                # exact lattice rotation: cell centres map onto cell centres
                np.testing.assert_array_equal(mk[side], _rotate_mask(m0[side], k))

    def test_free_hull_macro_tries_every_quarter_turn(self):
        # 10.5 x 6.5 board: only 0/180 fit; the fixed part sits in the rot-0 pocket, on the member at 180.
        from pnr.place.legalize import LegalizationError

        fixed = small("F", (7.25, 2.25), size=(0.6, 0.6))
        g = BoardGraph("t", [synthetic_macro(pos=(5.0, 3.0), rot=90.0), fixed])
        out = legalize(
            g,
            10.5,
            6.5,
            fixed={"F": (7.25, 2.25)},
            keepouts=[],
            clearance=CL,
            grid_mm=G,
            allow_rotation=True,
        )
        m = out.component("MB00")
        self.assertEqual(m.rot, 0.0)
        self.assertEqual(overlap_pairs(out), [])
        self.assertGreaterEqual(physical_copper_gap(m, out.component("F")), C_CU - 1e-9)
        with flags(PNR_MACRO_HULL="0"), self.assertRaises(LegalizationError):
            legalize(
                g,
                10.5,
                6.5,
                fixed={"F": (7.25, 2.25)},
                keepouts=[],
                clearance=CL,
                grid_mm=G,
                allow_rotation=True,
            )

    def test_rotated_macro_pocket_moves_with_it(self):
        target = world(synthetic_macro(rot=90.0), 2.0, -1.0)  # pocket, rotated frame
        out = self.run_legalize([small("R1", target)], macro_rot=90.0)
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual((m.pos, m.rot), (MACRO_POS, 90.0))
        self.assertLess(math.dist(p.pos, target), 0.2)
        self.assertEqual(overlap_pairs(out), [])
        # the unrotated pocket position now lies on the member: refused
        out = self.run_legalize([small("R1", world(synthetic_macro(), 2.5, -1.5))], macro_rot=90.0)
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual((m.pos, m.rot), (MACRO_POS, 90.0))
        self.assertEqual(overlap_pairs(out), [])
        self.assertTrue(all(rect_gap(courtyard_rect(p), r) >= CL - 1e-9 for r in member_rects(m)))
        self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9)

    def test_random_legalizations_never_overlap(self):
        rng = random.Random(11)
        nested = 0
        for trial in range(6):
            parts = []
            for i in range(14):
                side = rng.choice(("top", "top", "bottom"))
                parts.append(
                    small(
                        f"R{i}",
                        (rng.uniform(8, 22), rng.uniform(5, 15)),
                        side=side,
                        size=rng.choice(((1.0, 0.6), (1.6, 0.8), (0.6, 0.4))),
                        through=rng.random() < 0.1,
                    )
                )
            rot = rng.choice((0.0, 90.0, 180.0, 270.0))
            out = self.run_legalize(parts, macro_rot=rot)
            self.assertEqual(overlap_pairs(out), [], trial)
            m = out.component("MB00")
            for p in out.components[1:]:
                nested += courtyard_rect(p).overlaps(courtyard_rect(m))
                self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9, (trial, p.ref))
                if p.side == "top" or any(q.through_hole for q in p.pads):
                    self.assertTrue(
                        all(rect_gap(courtyard_rect(p), r) >= CL - 1e-9 for r in member_rects(m))
                    )
        self.assertGreater(nested, 10)  # parts do use the macro's free space

    def test_hard_violations_see_hull_rectangles(self):
        from pnr.constraints import compile_constraints

        m = synthetic_macro()
        ok = small("R1", world(m, 2.0, -1.0))
        bad = small("R2", world(m, 0.0, 1.5))
        con = compile_constraints({"board": {"outline": {"w": 30, "h": 20}}}, ["MB00", "R1", "R2"])
        v = hard_violations(BoardGraph("t", [m, ok]), con)
        self.assertEqual(v["overlaps"], [])
        v = hard_violations(BoardGraph("t", [m, ok, bad]), con)
        self.assertEqual(v["overlaps"], [("MB00", "R2")])

    def test_complementary_macros_interlock(self):
        def half_macro(ref, pos, lower):
            # split on a lattice line: 25 rows of 0.25 mm centred on 0 put the edges at +-0.125 + k/4
            y0, y1 = (-3.0, 0.125) if lower else (0.125, 3.0)
            geo = E.BlockGeometry(
                ok=True,
                extent=(-5, -3, 5, 3),
                c_cu=C_CU,
                margin=0.3,
                track_width=0.2,
                shapes=dict(
                    top=dict(rect=[[-5.0, y0, 5.0, y1]], cap=[]), bottom=dict(rect=[], cap=[])
                ),
            )
            return Component(
                ref,
                "block:half",
                pos,
                0.0,
                "top",
                (10.0, 6.0),
                (10.0, 6.0),
                pads=[Pad("X.1", "N", (0, 0), (1, 1))],
                hull=E.build_hull(geo, (0, 0), (10, 6), CL),
            )

        a, b = half_macro("MA", (15.0, 10.0), True), half_macro("MB", (15.0, 10.0), False)
        self.assertTrue(courtyard_rect(a).overlaps(courtyard_rect(b)))
        self.assertEqual(overlap_pairs(BoardGraph("t", [a, b])), [])
        b.pos = (15.0, 10.0 - 0.25)
        self.assertEqual(overlap_pairs(BoardGraph("t", [a, b])), [("MA", "MB")])
        # and the legalizer nests them: both target the same spot
        g = BoardGraph(
            "t", [half_macro("MA", (15.0, 10.0), True), half_macro("MB", (15.0, 10.0), False)]
        )
        out = legalize(
            g,
            30,
            20,
            fixed={},
            keepouts=[],
            clearance=CL,
            grid_mm=G,
            rotations={"MA": 0.0, "MB": 0.0},
        )
        ra, rb = courtyard_rect(out.component("MA")), courtyard_rect(out.component("MB"))
        self.assertEqual(overlap_pairs(out), [])
        self.assertTrue(ra.overlaps(rb))
        self.assertLess(abs(ra.cy - rb.cy), 6.0 - 1.0)

    def test_flag_off_ignores_hull(self):
        with flags(PNR_MACRO_HULL="0"):
            m = synthetic_macro()
            self.assertEqual([s for s, _ in placement_rects(m)], ["top", "bottom"])
            self.assertIsNone(H.gp_bodies([m, small("R1", (1, 1))]))


class BodiesTest(unittest.TestCase):
    def setUp(self):
        self.env = off()

    def tearDown(self):
        self.env.stop()

    def test_no_hull_no_bodies_and_same_global_placement(self):
        from pnr.constraints import compile_constraints
        from pnr.place.model import global_place

        parts = [small(f"R{i}", (i + 1, 2)) for i in range(4)]
        g = BoardGraph("t", parts, [Net("N1", 1, [(p.ref, "1") for p in parts])])
        con = compile_constraints({"board": {"outline": {"w": 10, "h": 10}}}, g.refs)
        a = global_place(g, con, 10, 10, iters=40, seed=2)
        with flags(PNR_MACRO_HULL="1"):
            self.assertIsNone(H.gp_bodies(g.components))
            b = global_place(g, con, 10, 10, iters=40, seed=2)
        self.assertEqual(a, b)

    def test_bodies_rotate_like_pins(self):
        hull = dict(grid_mm=G, cells=[40, 24], top=[[1.0, 0.0, 3.0, 2.0]], bottom=[], shapes={})
        comp = Component("MB00", "block:x", (0, 0), 0.0, "top", (10, 6), (10, 6), hull=hull)
        with flags(PNR_MACRO_HULL="1"):
            b = H.gp_bodies([comp, small("R1", (1, 1))])
        self.assertEqual(b["owner"].tolist(), [0, 1])
        np.testing.assert_allclose(
            b["off4"][0].numpy(), [[2, 1], [-1, 2], [-2, -1], [1, -2]], atol=1e-6
        )
        np.testing.assert_allclose(b["half4"][0].numpy(), [[1, 1]] * 4, atol=1e-6)
        self.assertEqual(b["pair"].tolist(), [[0.0, 1.0], [0.0, 0.0]])

    def test_hull_rects_rotate_and_mirror_like_pads(self):
        from pnr.place.geometry import set_component_side

        hull = dict(
            grid_mm=G,
            cells=[40, 24],
            top=[[1.5, 0.5, 2.5, 1.5]],
            bottom=[[-1.0, -1.0, 0.0, 0.0]],
            shapes={},
        )
        with flags(PNR_MACRO_HULL="1"):
            for side in ("top", "bottom"):
                for rot in (0.0, 90.0, 180.0, 270.0):
                    comp = Component(
                        "MB00",
                        "block:x",
                        (10, 10),
                        0.0,
                        "top",
                        (10, 6),
                        (10, 6),
                        pads=[Pad("a", "N", (2.0, 1.0)), Pad("b", "N", (-0.5, -0.5))],
                        hull=dict(hull),
                    )
                    set_component_side(comp, side)
                    comp.rot = rot
                    pins = dict(pin_positions(comp))
                    rects = placement_rects(comp)
                    self.assertEqual(len(rects), 2)
                    # pad 'a' marks the top rect's centre, 'b' the bottom one's; mirroring swaps the sides
                    for pad, home in (("a", "top"), ("b", "bottom")):
                        want = home if side == "top" else ("bottom" if home == "top" else "top")
                        hit = [
                            r
                            for s, r in rects
                            if s == want
                            and abs(r.cx - pins[pad][0]) < 1e-9
                            and abs(r.cy - pins[pad][1]) < 1e-9
                        ]
                        self.assertEqual(len(hit), 1, (side, rot, pad))

    def test_hull_bodies_let_parts_sit_in_pockets(self):
        import torch

        m = synthetic_macro()
        p = small("R1", (0, 0))
        with flags(PNR_MACRO_HULL="1"):
            b = H.gp_bodies([m, p])
        pos = torch.tensor([[15.0, 10.0], [17.0, 9.0]])
        prob = torch.tensor([[1.0, 0, 0, 0], [1.0, 0, 0, 0]])
        self.assertEqual(float(H.gp_overlap(b, pos, prob, 0.0)), 0.0)  # in the pocket
        pos = torch.tensor([[15.0, 10.0], [12.0, 9.0]])
        self.assertGreater(float(H.gp_overlap(b, pos, prob, 0.0)), 0.0)  # on the member


def _rotate_mask(mask, k):
    """Mask of the slot rotated CCW by k quarter turns (row 0 = bottom)."""
    out = mask
    for _ in range(k % 4):
        bh, bw = out.shape
        nxt = np.zeros((bw, bh), bool)
        # rot 90 CCW: new[i, j] = old[bh - 1 - j, i]
        for i in range(bw):
            for j in range(bh):
                nxt[i, j] = out[bh - 1 - j, i]
        out = nxt
    return out


# ----------------------------------------------------------------------------- real layouts


@unittest.skipUnless(HAVE_REAL, "Mini inputs / nb6-fb library / h6 snapshot not available")
class RealLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pnr.mc.halving import _load
        from pnr.hier.blocks import extract_blocks
        from pnr.hier.synth import instance_board
        from pnr.place.initial_pool import preserve_source_locks, _prepared_source

        cls.env = off()
        graph, constraints, rules = _load(INPUTS, CONSTRAINTS)
        cls.constraints = preserve_source_locks(graph, constraints)
        cls.rules = rules
        cls.source = _prepared_source(graph, cls.constraints, rules)
        cls.lib = json.loads(SNAPSHOT.read_text())
        cls.blocks = {b.name: b for b in extract_blocks(cls.source, cls.constraints)}
        cls.subs = {}
        for name, tier in cls.lib.items():
            for rank, rec in enumerate(tier):
                sub, _, _ = instance_board(
                    cls.source,
                    cls.constraints,
                    rules,
                    cls.blocks[name],
                    rec["layout"],
                    rec["width"],
                    rec["height"],
                )
                d = [i for i in rec["instances"] if i["instance"] == name][0]["dir"]
                cls.subs[(name, rank)] = (rec, sub, Path(d) / "electrical" / "board.kicad_pcb")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def converter(self, size=(27.25, 27.25)):
        for (name, rank), (rec, sub, board) in self.subs.items():
            if name == "board.converter" and (rec["width"], rec["height"]) == size:
                return rec, sub, board
        self.skipTest("converter layout %r not in the snapshot tier" % (size,))

    def test_frame_residual_on_every_tier_layout(self):
        for key, (rec, sub, board) in self.subs.items():
            geo = E.block_geometry(sub.components, board, self.rules)
            self.assertLessEqual(geo.residual_mm, 2e-6, key)
            self.assertEqual(geo.stats["footprints"], len(sub.components))

    def test_frame_rejects_a_perturbed_member(self):
        rec, sub, board = self.converter()
        comps = BoardGraph.from_json(sub.to_json()).components
        comps[3].pos = (comps[3].pos[0] + 0.01, comps[3].pos[1])
        with self.assertRaises(ValueError):
            E.block_geometry(comps, board, self.rules)
        comps = BoardGraph.from_json(sub.to_json()).components
        comps[3].side = "bottom"
        with self.assertRaises(ValueError):
            E.block_geometry(comps, board, self.rules)
        self.assertFalse(E.safe_geometry(comps, board, self.rules).ok)
        self.assertFalse(E.safe_geometry(comps, None, self.rules).ok)

    def test_extent_contains_everything_and_fits_the_old_rectangle(self):
        m = 0.3
        for key, (rec, sub, board) in self.subs.items():
            geo = E.block_geometry(sub.components, board, self.rules)
            x0, y0, x1, y1 = geo.extent
            eps = 1e-9
            for c in sub.components:
                cr = courtyard_rect(c)
                self.assertTrue(
                    x0 - eps <= cr.left
                    and cr.right <= x1 + eps
                    and y0 - eps <= cr.bottom
                    and cr.top <= y1 + eps,
                    (key, c.ref),
                )
                for _, _, r in pad_rects(c):
                    self.assertTrue(
                        x0 - eps <= r.left - m and r.right + m <= x1 + eps, (key, c.ref)
                    )
            data = E.read_board(board)
            tx, ty, _ = E.solve_frame(data["footprints"], sub.components)
            for ax, ay, bx, by, w, layer in data["segments"]:
                for x, y in ((ax - tx, ty - ay), (bx - tx, ty - by)):
                    self.assertTrue(x0 - eps <= x - w / 2 - m and x + w / 2 + m <= x1 + eps, key)
                    self.assertTrue(y0 - eps <= y - w / 2 - m and y + w / 2 + m <= y1 + eps, key)
            for x, y, size, _ in data["vias"]:
                x, y = x - tx, ty - y
                self.assertTrue(x0 - eps <= x - size / 2 - m and x + size / 2 + m <= x1 + eps, key)
            w, h = rec["width"], rec["height"]
            self.assertTrue(
                -m - eps <= x0 and x1 <= w + m + eps and -m - eps <= y0 and y1 <= h + m + eps, key
            )

    def test_measured_extents(self):
        # holes grown by the 0.5 hole-to-edge rule (review fix): via-lined edges grow by up to 0.125
        want = {
            ("board.converter", 27.75, 35.75): (26.60, 31.38),
            ("board.converter", 27.25, 27.25): (26.89, 26.86),
            ("board.converter", 22.25, 33.25): (22.71, 29.98),
            ("board.pd", 16.75, 25.0): (17.22, 25.26),
            ("board.pd", 30.5, 22.25): (26.14, 20.61),
            ("board.pd", 28.25, 19.0): (21.70, 18.78),
        }
        for (name, rank), (rec, sub, board) in self.subs.items():
            key = (name, rec["width"], rec["height"])
            if key in want:
                geo = E.block_geometry(sub.components, board, self.rules)
                self.assertAlmostEqual(geo.size[0], want[key][0], delta=0.01)
                self.assertAlmostEqual(geo.size[1], want[key][1], delta=0.01)

    def collapse_one(self, rec, sub, board, **kw):
        from pnr.hier.macro import collapse

        name = rec["block"]
        geo = {name: E.safe_geometry(sub.components, board, self.rules)}
        with flags(**kw):
            return collapse(
                self.source,
                self.constraints,
                self.rules,
                [(self.blocks[name], sub, rec["width"], rec["height"])],
                geometry=geo,
            )

    def test_expand_is_rigid_at_every_rotation(self):
        rec, sub, board = self.converter()
        for kw in (
            dict(PNR_MACRO_SHRINK="1"),
            dict(PNR_MACRO_HULL="1"),
            dict(PNR_MACRO_SHRINK="1", PNR_MACRO_HULL="1"),
        ):
            mgraph, _, _, plan = self.collapse_one(rec, sub, board, **kw)
            macro = mgraph.component("MB00")
            rng = random.Random(4)
            for rot in (0.0, 90.0, 180.0, 270.0):
                placed = BoardGraph.from_json(mgraph.to_json())
                mc = placed.component("MB00")
                mc.pos, mc.rot = (rng.uniform(20, 40), rng.uniform(20, 40)), rot
                flat = plan.expand(placed, self.source)
                macro_pins = dict(pin_positions(mc))
                for c in flat.components:
                    if c.ref not in plan.member_of:
                        continue
                    for name, xy in pin_positions(c):
                        mx, my = macro_pins["%s.%s" % (c.ref, name)]
                        self.assertAlmostEqual(xy[0], mx, delta=1e-9)
                        self.assertAlmostEqual(xy[1], my, delta=1e-9)
            self.assertIn("origin", plan.macros["MB00"])
            self.assertEqual(macro.courtyard, tuple(plan.macros["MB00"]["courtyard"]))

    def test_assembled_copper_lands_inside_courtyard_and_hull(self):
        """Pure-python copy of pnr.hier.assemble's transform: solved from the
        member footprints of the block board and of a full board written from
        the expanded flat graph (KiCad frame: y down, arbitrary origin)."""
        rec, sub, board = self.converter()
        data = E.read_board(board)
        mgraph, _, _, plan = self.collapse_one(
            rec, sub, board, PNR_MACRO_SHRINK="1", PNR_MACRO_HULL="1"
        )
        X0, Y0 = 100.0, 120.0
        with flags(PNR_MACRO_HULL="1"):
            for rot in (0.0, 90.0, 180.0, 270.0):
                placed = BoardGraph.from_json(mgraph.to_json())
                mc = placed.component("MB00")
                mc.pos, mc.rot = (31.3, 27.9), rot
                flat = {c.ref: c for c in plan.expand(placed, self.source).components}
                dest = {
                    ref: (X0 + c.pos[0], Y0 - c.pos[1], c.rot)
                    for ref, c in flat.items()
                    if ref in plan.member_of
                }
                refs = list(data["footprints"])
                p0, d0 = data["footprints"][refs[0]], dest[refs[0]]
                theta = math.radians((d0[2] - p0[2]) % 360)
                c_, s_ = math.cos(-theta), math.sin(-theta)

                def xf(x, y):
                    return (
                        d0[0] + (x - p0[0]) * c_ - (y - p0[1]) * s_,
                        d0[1] + (x - p0[0]) * s_ + (y - p0[1]) * c_,
                    )

                worst = max(
                    max(
                        abs(xf(*data["footprints"][r][:2])[0] - dest[r][0]),
                        abs(xf(*data["footprints"][r][:2])[1] - dest[r][1]),
                    )
                    for r in refs
                )
                self.assertLess(worst * 1e6, 2000)  # nm, assemble's tolerance
                court = courtyard_rect(mc)
                cover = {
                    s: [r for side, r in placement_rects(mc) if side == s]
                    for s in ("top", "bottom", "inner")
                }
                for ax, ay, bx, by, w, layer in data["segments"]:
                    pa, pb = xf(ax, ay), xf(bx, by)
                    pa, pb = (pa[0] - X0, Y0 - pa[1]), (pb[0] - X0, Y0 - pb[1])
                    for x, y in (pa, pb):
                        self.assertTrue(
                            court.left <= x - w / 2 and x + w / 2 <= court.right, (rot, layer)
                        )
                        self.assertTrue(
                            court.bottom <= y - w / 2 and y + w / 2 <= court.top, (rot, layer)
                        )
                    side = E.OUTER.get(layer, "inner")
                    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
                        x, y = pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1])
                        for ox, oy in ((0, 0), (w / 2, 0), (-w / 2, 0), (0, w / 2), (0, -w / 2)):
                            self.assertTrue(
                                in_union(x + ox, y + oy, cover[side]), (rot, layer, x, y)
                            )
                for x, y, size, _ in data["vias"]:
                    q = xf(x, y)
                    q = (q[0] - X0, Y0 - q[1])
                    for side in ("top", "bottom", "inner"):
                        for ox, oy in (
                            (0, 0),
                            (size / 2, 0),
                            (-size / 2, 0),
                            (0, size / 2),
                            (0, -size / 2),
                        ):
                            self.assertTrue(
                                in_union(q[0] + ox, q[1] + oy, cover[side]), (rot, side)
                            )

    def test_real_hull_masks_contain_cover_and_rotate(self):
        rec, sub, board = self.converter()
        mgraph, _, _, plan = self.collapse_one(
            rec, sub, board, PNR_MACRO_SHRINK="1", PNR_MACRO_HULL="1"
        )
        macro = mgraph.component("MB00")
        with flags(PNR_MACRO_HULL="1"):
            bw = int(math.ceil((macro.courtyard[0] + CL) / G))
            bh = int(math.ceil((macro.courtyard[1] + CL) / G))
            self.assertEqual(macro.hull["cells"], [bw, bh])
            m0 = H.slot_masks(macro, G, CL, bw, bh)
            cover0 = {
                s: H.raster(dict(cover=H.transformed(macro.hull, 0.0)[s]["cover"]), 0, bw, bh, G)
                for s in ("top", "bottom")
            }
            for s in ("top", "bottom"):
                self.assertFalse((cover0[s] & ~m0[s]).any())
            self.assertTrue(m0["inner"].any())
            for k in (1, 2, 3):
                macro.rot = 90.0 * k
                mk = H.slot_masks(macro, G, CL, *((bw, bh) if k % 2 == 0 else (bh, bw)))
                for s in ("top", "bottom", "inner"):
                    np.testing.assert_array_equal(mk[s], _rotate_mask(m0[s], k))
            top_boxes, cover = H.cover_boxes(macro.hull, "top")
            self.assertGreaterEqual(cover, 0.95)
            self.assertLessEqual(len(top_boxes), H.BODY_MAX_PER_SIDE)

    def test_extent_holds_every_hole_grown_by_hole_to_edge(self):
        he = float(self.rules["fab"]["hole_to_edge_mm"])
        self.assertEqual(he, 0.5)
        for key, (rec, sub, board) in self.subs.items():
            geo = E.block_geometry(sub.components, board, self.rules)
            x0, y0, x1, y1 = geo.extent
            data = E.read_board(board)
            self.assertEqual(len(data["via_drills"]), len(data["vias"]))
            self.assertTrue(all(d > 0 for d in data["via_drills"]), key)
            tx, ty, _ = E.solve_frame(data["footprints"], sub.components)
            for (x, y, _, _), drill in zip(data["vias"], data["via_drills"]):
                x, y = x - tx, ty - y
                r = drill / 2 + he
                self.assertTrue(
                    x0 - 1e-9 <= x - r
                    and x + r <= x1 + 1e-9
                    and y0 - 1e-9 <= y - r
                    and y + r <= y1 + 1e-9,
                    key,
                )
            for c in sub.components:
                for pad, (_, _, r) in zip(c.pads, pad_rects(c)):
                    if pad.through_hole:
                        d = max(pad.drill_size) / 2 + he
                        self.assertTrue(
                            x0 - 1e-9 <= r.cx - d and r.cx + d <= x1 + 1e-9, (key, c.ref)
                        )
                        self.assertTrue(
                            y0 - 1e-9 <= r.cy - d and r.cy + d <= y1 + 1e-9, (key, c.ref)
                        )
            self.assertEqual(geo.stats["via_drills"], len(data["vias"]))
            inner_caps = geo.shapes["inner"]["cap"]
            self.assertEqual(len(inner_caps), geo.stats["inner_segments"] + len(data["vias"]), key)
            self.assertGreater(geo.stats["inner_segments"], 0, key)

    def test_real_drilled_part_never_lands_on_inner_copper(self):
        """The review's repro: SW1 (two 0.9 mm NPTH pegs) legalized next to the
        converter 27.75 x 35.75 at the reviewer's target and on In2 track midpoints."""
        import copy

        rec, sub, board = self.converter((27.75, 35.75))
        mgraph, _, _, plan = self.collapse_one(
            rec, sub, board, PNR_MACRO_SHRINK="1", PNR_MACRO_HULL="1"
        )
        data = E.read_board(board)
        tx, ty, _ = E.solve_frame(data["footprints"], sub.components)
        ox, oy = plan.macros["MB00"]["origin"]
        inner = [
            (x0 - tx - ox, ty - y0 - oy, x1 - tx - ox, ty - y1 - oy, w)
            for x0, y0, x1, y1, w, la in data["segments"]
            if la not in E.OUTER
        ]
        sw = copy.deepcopy(self.source.component("SW1"))
        sw.side, sw.rot = "top", 0.0
        self.assertTrue(sw.smd_body and sum(p.through_hole for p in sw.pads) == 2)
        macro_target = (35.0, 27.5)
        targets = [(47.25, 39.0)] + [
            (macro_target[0] + (a + c) / 2, macro_target[1] + (b + d) / 2)
            for a, b, c, d, _ in inner[::4]
        ]
        worst, nested = math.inf, 0
        with flags(PNR_MACRO_HULL="1"):
            for target in targets:
                g = BoardGraph.from_json(mgraph.to_json())
                g.components = [c for c in g.components if c.ref == "MB00"]
                g.nets = []
                g.components[0].pos = macro_target
                part = copy.deepcopy(sw)
                part.pos = target
                g.components.append(part)
                out = legalize(
                    g,
                    70.0,
                    55.0,
                    fixed={},
                    keepouts=[],
                    clearance=CL,
                    grid_mm=G,
                    rotations={"MB00": 0.0, "SW1": 0.0},
                )
                m, p = out.component("MB00"), out.component("SW1")
                self.assertEqual(overlap_pairs(out), [])
                cr = courtyard_rect(p)
                nested += cr.overlaps(courtyard_rect(m))
                k = H.quarter(m.rot)
                for a, b, c, d, w in inner:
                    (ax, ay), (bx, by) = H._xf_point(a, b, k, False), H._xf_point(c, d, k, False)
                    gap = (
                        seg_rect_dist(
                            m.pos[0] + ax,
                            m.pos[1] + ay,
                            m.pos[0] + bx,
                            m.pos[1] + by,
                            cr.left,
                            cr.bottom,
                            cr.right,
                            cr.top,
                        )
                        - w / 2
                    )
                    worst = min(worst, gap)
        self.assertGreaterEqual(worst, C_CU - 1e-9)
        self.assertGreater(nested, 0)  # SW1 still nests where the block has no inner copper

    def test_hull_only_keeps_full_rectangle(self):
        rec, sub, board = self.converter()
        mgraph, _, _, plan = self.collapse_one(rec, sub, board, PNR_MACRO_HULL="1")
        macro = mgraph.component("MB00")
        self.assertEqual(macro.courtyard, (rec["width"] + 0.6, rec["height"] + 0.6))
        self.assertEqual(plan.macros["MB00"]["shape"], "rect")
        self.assertIsNotNone(macro.hull)
        mgraph, _, _, plan = self.collapse_one(rec, sub, board, PNR_MACRO_SHRINK="1")
        self.assertIsNone(mgraph.component("MB00").hull)
        self.assertEqual(plan.macros["MB00"]["shape"], "used")

    def test_unmeasurable_block_keeps_rectangle(self):
        from pnr.hier.macro import collapse

        rec, sub, board = self.converter()
        name = rec["block"]
        with flags(PNR_MACRO_SHRINK="1", PNR_MACRO_HULL="1"):
            mgraph, _, _, plan = collapse(
                self.source,
                self.constraints,
                self.rules,
                [(self.blocks[name], sub, rec["width"], rec["height"])],
                geometry={name: E.safe_geometry(sub.components, None, self.rules)},
            )
        self.assertEqual(
            mgraph.component("MB00").courtyard, (rec["width"] + 0.6, rec["height"] + 0.6)
        )
        self.assertIsNone(mgraph.component("MB00").hull)
        self.assertIn("no routed block board", plan.macros["MB00"]["reason"])

    def test_flags_off_collapse_ignores_geometry(self):
        from pnr.hier.macro import collapse

        rec, sub, board = self.converter()
        name = rec["block"]
        args = (
            self.source,
            self.constraints,
            self.rules,
            [(self.blocks[name], sub, rec["width"], rec["height"])],
        )
        a = collapse(*args)
        b = collapse(*args, geometry={name: E.safe_geometry(sub.components, board, self.rules)})
        self.assertEqual(a[0].to_json(), b[0].to_json())
        self.assertEqual(a[3].macros, b[3].macros)


@unittest.skipUnless(NB6.exists(), "nb6-fb library not available")
class RankUsedTest(unittest.TestCase):
    def setUp(self):
        self.env = off()

    def tearDown(self):
        self.env.stop()

    def order(self, **kw):
        from pnr.hier.top import load_library

        with flags(**kw):
            lib = load_library(NB6)
        return {
            n: [(r["width"], r["height"]) for r in lib[n]] for n in ("board.converter", "board.pd")
        }, lib

    def test_orders(self):
        conv_sub = [(27.25, 27.25), (22.25, 33.25), (27.75, 35.75)]
        pd_sub = [(16.75, 25.0), (28.25, 19.0), (30.5, 22.25)]
        conv_pf = [(27.75, 35.75), (27.25, 27.25), (22.25, 33.25)]
        pd_pf = [(16.75, 25.0), (30.5, 22.25), (28.25, 19.0)]
        self.assertEqual(self.order()[0], {"board.converter": conv_sub, "board.pd": pd_sub})
        self.assertEqual(
            self.order(PNR_POWER_FIRST="1")[0], {"board.converter": conv_pf, "board.pd": pd_pf}
        )
        # Q = 0.10: no reordering in either variant (design prediction)
        order, lib = self.order(PNR_LIBRARY_RANK_USED="1")
        self.assertEqual(order, {"board.converter": conv_sub, "board.pd": pd_sub})
        used = {
            (r["width"], r["height"]): (r["used"]["area_mm2"], r["used_band"])
            for r in lib["board.converter"]
        }
        self.assertAlmostEqual(used[(22.25, 33.25)][0], 680.60, delta=0.05)
        self.assertEqual([used[k][1] for k in conv_sub], [0, 0, 2])
        self.assertEqual(
            self.order(PNR_LIBRARY_RANK_USED="1", PNR_POWER_FIRST="1")[0],
            {"board.converter": conv_pf, "board.pd": pd_pf},
        )
        # Q = 0.05 reorders the subwidth-first ranking, not the power-first one (q_band decides first)
        self.assertEqual(
            self.order(PNR_LIBRARY_RANK_USED="1", PNR_LIBRARY_RANK_USED_Q="0.05")[0],
            {
                "board.converter": [(22.25, 33.25), (27.25, 27.25), (27.75, 35.75)],
                "board.pd": [(28.25, 19.0), (16.75, 25.0), (30.5, 22.25)],
            },
        )
        self.assertEqual(
            self.order(
                PNR_LIBRARY_RANK_USED="1", PNR_LIBRARY_RANK_USED_Q="0.05", PNR_POWER_FIRST="1"
            )[0],
            {"board.converter": conv_pf, "board.pd": pd_pf},
        )

    def test_unmeasurable_record_ranks_last_in_its_band(self):
        from pnr.hier.synth_native import rank_key

        _, lib = self.order(PNR_LIBRARY_RANK_USED="1")
        recs = [dict(r) for r in lib["board.converter"]]
        broken = dict(recs[0], instances=[dict(recs[0]["instances"][0], dir="/nonexistent")])
        stamped = E.stamp_used([broken] + recs)
        self.assertIsNone(stamped[0]["used_band"])
        self.assertIsNone(stamped[0]["used"]["area_mm2"])
        with flags(PNR_LIBRARY_RANK_USED="1"):
            keys = sorted(stamped, key=rank_key)
            self.assertIs(keys[-1], stamped[0])
            self.assertEqual(rank_key(stamped[0])[4], math.inf)

    def test_flag_off_rank_key_is_the_old_tuple(self):
        from pnr.hier.synth_native import rank_key

        r = dict(
            objective=[1, 2, 3, 40, 5, 6],
            port_debt_mm=7.0,
            area=8.0,
            used_band=0,
            used=dict(area_mm2=1.0),
        )
        self.assertEqual(rank_key(r), (6, 1, 2, 3, 40, 5, 7.0, 8.0))
        with flags(PNR_LIBRARY_RANK_USED="1"):
            self.assertEqual(rank_key(r), (6, 1, 2, 3, 0, 40, 5, 7.0, 1.0, 8.0))


# ----------------------------------------------------------------------------- review fixes

INNER_Y = -1.5  # the synthetic In2 track runs across the free pocket


def inner_geometry():
    """synthetic_geometry plus inner copper: an In2 track across the free pocket
    (x -0.5..4.6 at y=-1.5) and the through via's barrel."""
    geo = synthetic_geometry()
    geo.shapes["inner"] = dict(
        rect=[],
        cap=[[-0.5, INNER_Y, 4.6, INNER_Y, TRACK_W / 2 + C_CU], [3.0, 2.4, 3.0, 2.4, 0.225 + C_CU]],
    )
    return geo


def inner_macro(ref="MB00", pos=MACRO_POS, rot=0.0):
    hull = E.build_hull(inner_geometry(), (0.0, 0.0), (10.0, 6.0), CL)
    return Component(
        ref,
        "block:test",
        pos,
        rot,
        "top",
        (10.0, 6.0),
        (10.0, 6.0),
        pads=[Pad("U1.1", "N1", (-3.0, 0.0), (1.0, 1.0))],
        address="block:test",
        hull=hull,
    )


def inner_gap(macro, rect):
    """Distance from the synthetic In2 track's copper edge to ``rect``."""
    (ax, ay), (bx, by) = world(macro, -0.5, INNER_Y), world(macro, 4.6, INNER_Y)
    return seg_rect_dist(ax, ay, bx, by, rect.left, rect.bottom, rect.right, rect.top) - TRACK_W / 2


class InnerPlaneTest(unittest.TestCase):
    """Review issue 1: drilled parts (and other blocks) clear block inner-layer copper."""

    def setUp(self):
        self.env = off()
        os.environ["PNR_MACRO_HULL"] = "1"

    def tearDown(self):
        self.env.stop()

    def run_legalize(self, parts, macro_rot=0.0, fixed=None, macro=None):
        g = BoardGraph("t", [macro or inner_macro(rot=macro_rot)] + parts)
        return legalize(
            g,
            30.0,
            20.0,
            fixed=fixed or {},
            keepouts=[],
            clearance=CL,
            grid_mm=G,
            rotations={"MB00": macro_rot},
        )

    def test_hull_carries_an_inner_plane(self):
        hull = inner_macro().hull
        self.assertEqual(hull["v"], 2)
        self.assertTrue(hull["inner"])
        self.assertEqual(
            hull["stats"]["inner"]["pocket_cells"], 0
        )  # no pocket fill on the inner plane
        self.assertIn("inner", {s for s, _ in placement_rects(inner_macro())})
        self.assertEqual(synthetic_macro().hull["inner"], [])  # no inner copper, no inner cover

    def test_smd_part_still_nests_over_inner_copper(self):
        target = world(inner_macro(), 2.0, INNER_Y)
        out = self.run_legalize([small("R1", target)])
        m, p = out.component("MB00"), out.component("R1")
        self.assertEqual(m.pos, MACRO_POS)
        self.assertLess(math.dist(p.pos, target), 0.2)
        self.assertLess(inner_gap(m, courtyard_rect(p)), 0.0)  # right over the In2 track
        self.assertEqual(overlap_pairs(out), [])

    def test_drilled_part_is_refused_over_inner_copper(self):
        for rot in (0.0, 90.0, 180.0, 270.0):
            target = world(inner_macro(rot=rot), 2.0, INNER_Y)
            out = self.run_legalize([small("J1", target, through=True)], macro_rot=rot)
            m, p = out.component("MB00"), out.component("J1")
            self.assertEqual((m.pos, m.rot), (MACRO_POS, rot))
            self.assertEqual(overlap_pairs(out), [])
            self.assertGreaterEqual(inner_gap(m, courtyard_rect(p)), C_CU - 1e-9, rot)
            self.assertGreaterEqual(physical_copper_gap(m, p), C_CU - 1e-9, rot)
        # the flat check sees it too: the drilled pad over the inner cover is an overlap, an SMD part is not
        m = inner_macro()
        target = world(m, 2.0, INNER_Y)
        con_refs = ["MB00", "J1", "R1"]
        from pnr.constraints import compile_constraints

        con = compile_constraints({"board": {"outline": {"w": 30, "h": 20}}}, con_refs)
        self.assertEqual(
            hard_violations(BoardGraph("t", [m, small("R1", target)]), con)["overlaps"], []
        )
        v = hard_violations(BoardGraph("t", [m, small("J1", target, through=True)]), con)
        self.assertEqual(v["overlaps"], [("MB00", "J1")])

    def test_fixed_drilled_part_keeps_the_macros_inner_copper_away(self):
        spot = world(inner_macro(), 2.0, INNER_Y)
        j1 = small("J1", spot, through=True)
        out = self.run_legalize([j1], fixed={"J1": spot})
        m, p = out.component("MB00"), out.component("J1")
        self.assertEqual(p.pos, spot)
        self.assertNotEqual(m.pos, MACRO_POS)
        self.assertEqual(overlap_pairs(out), [])
        pad = pad_rects(p)[0][2]
        self.assertGreaterEqual(inner_gap(m, pad), C_CU - 1e-9)

    def test_drill_without_pad_size_reserves_the_inner_plane(self):
        peg = Component(
            "SW9",
            "SW",
            (3.0, 3.0),
            90.0,
            "top",
            (2.0, 1.0),
            (2.0, 1.0),
            pads=[Pad("", "", (0.5, 0.0), (0.0, 0.0), True, (0.9, 0.6), False)],
        )
        rects = [r for s, r in placement_rects(peg) if s == "inner"]
        self.assertEqual(len(rects), 1)
        self.assertAlmostEqual(rects[0].w, 0.6)  # drill rotated with the part
        self.assertAlmostEqual(rects[0].h, 0.9)
        self.assertAlmostEqual(rects[0].cx, 3.0)
        self.assertAlmostEqual(rects[0].cy, 3.5)

    def test_solid_block_reserves_the_inner_plane(self):
        solid = Component(
            "MB01",
            "block:solid",
            (0, 0),
            0.0,
            "top",
            (1.0, 0.6),
            (1.0, 0.6),
            pads=[Pad("X.1", "N", (0, 0), (0.4, 0.4))],
        )
        self.assertEqual([s for s, _ in placement_rects(solid)], ["top", "bottom", "inner"])
        with flags(PNR_MACRO_HULL="0"):
            self.assertEqual([s for s, _ in placement_rects(solid)], ["top", "bottom"])
            self.assertEqual(
                [s for s, _ in placement_rects(small("J1", (0, 0), through=True))],
                ["top", "bottom"],
            )

    def test_two_blocks_inner_copper_never_meets(self):
        def half(ref, lower):
            y0, y1 = (-3.0, 0.125) if lower else (0.125, 3.0)
            geo = E.BlockGeometry(
                ok=True,
                extent=(-5, -3, 5, 3),
                c_cu=C_CU,
                margin=0.3,
                track_width=0.2,
                shapes=dict(
                    top=dict(rect=[[-5.0, y0, 5.0, y1]], cap=[]),
                    bottom=dict(rect=[], cap=[]),
                    inner=dict(rect=[], cap=[[-4.4, -1.0, 4.4, -1.0, TRACK_W / 2 + C_CU]]),
                ),
            )
            return Component(
                ref,
                "block:half",
                (15.0, 10.0),
                0.0,
                "top",
                (10.0, 6.0),
                (10.0, 6.0),
                pads=[Pad("X.1", "N", (0, 0), (1, 1))],
                hull=E.build_hull(geo, (0, 0), (10, 6), CL),
            )

        a, b = half("MA", True), half("MB", False)
        self.assertEqual(
            overlap_pairs(BoardGraph("t", [a, b])), [("MA", "MB")]
        )  # outer sides interlock, In2 does not
        out = legalize(
            BoardGraph("t", [half("MA", True), half("MB", False)]),
            30,
            20,
            fixed={},
            keepouts=[],
            clearance=CL,
            grid_mm=G,
            rotations={"MA": 0.0, "MB": 0.0},
        )
        self.assertEqual(overlap_pairs(out), [])
        ma, mb = out.component("MA"), out.component("MB")
        self.assertTrue(courtyard_rect(ma).overlaps(courtyard_rect(mb)))  # still interlocked
        dx = max(0.0, abs(ma.pos[0] - mb.pos[0]) - 8.8)  # the In2 tracks are 8.8 long
        self.assertGreaterEqual(math.hypot(dx, ma.pos[1] - mb.pos[1]) - TRACK_W, C_CU - 1e-9)

    def test_inner_masks_rotate_with_the_macro(self):
        macro = inner_macro()
        bw, bh = macro.hull["cells"]
        m0 = H.slot_masks(macro, G, CL, bw, bh)
        self.assertTrue(m0["inner"].any())
        for k in (1, 2, 3):
            macro.rot = 90.0 * k
            mk = H.slot_masks(macro, G, CL, *((bw, bh) if k % 2 == 0 else (bh, bw)))
            np.testing.assert_array_equal(mk["inner"], _rotate_mask(m0["inner"], k))

    def test_random_drilled_parts_never_meet_inner_copper(self):
        rng = random.Random(5)
        for trial in range(4):
            rot = rng.choice((0.0, 90.0, 180.0, 270.0))
            parts = [
                small(
                    f"J{i}",
                    (rng.uniform(10, 20), rng.uniform(7, 13)),
                    through=True,
                    size=(0.8, 0.8),
                )
                for i in range(8)
            ]
            out = self.run_legalize(parts, macro_rot=rot)
            self.assertEqual(overlap_pairs(out), [], trial)
            m = out.component("MB00")
            for p in out.components[1:]:
                self.assertGreaterEqual(
                    inner_gap(m, courtyard_rect(p)), C_CU - 1e-9, (trial, p.ref)
                )


class PadEdgeTest(unittest.TestCase):
    """Review issue 3: PNR_PAD_EDGE_CLEARANCE keeps pads/drills off the board edge."""

    def setUp(self):
        self.env = off()

    def tearDown(self):
        self.env.stop()

    @staticmethod
    def flush_part(ref="U3", pos=(5.0, 0.0), drill=False):
        """Courtyard 2.0 x 1.2 whose pads reach its bottom and left edges (like the prepared SOT-23-5)."""
        pads = [Pad("1", "N1", (-0.7, -0.3), (0.6, 0.6)), Pad("2", "N2", (0.7, 0.3), (0.6, 0.6))]
        if drill:
            pads.append(Pad("MP", "", (0.0, -0.15), (0.9, 0.9), True, (0.9, 0.9), False))
        return Component(ref, "SOT", pos, 0.0, "top", (2.0, 1.2), (2.0, 1.2), pads=pads)

    @staticmethod
    def edge_gaps(comp, width, height):
        """(least pad copper gap, least drill gap) to the outline rectangle."""
        pad_gap = hole_gap = math.inf
        swap = int(round(comp.rot)) % 180 == 90
        for pad, (_, _, r) in zip(comp.pads, pad_rects(comp)):
            pad_gap = min(pad_gap, r.left, r.bottom, width - r.right, height - r.top)
            if pad.through_hole:
                dw, dh = (pad.drill_size[1], pad.drill_size[0]) if swap else pad.drill_size
                hole_gap = min(
                    hole_gap,
                    r.cx - dw / 2,
                    r.cy - dh / 2,
                    width - r.cx - dw / 2,
                    height - r.cy - dh / 2,
                )
        return pad_gap, hole_gap

    def test_rule_is_off_by_default_and_reads_the_fab(self):
        from pnr.place.legalize import pad_edge_rule

        self.assertIsNone(
            pad_edge_rule(rules={"fab": {"edge_clearance_mm": 0.3, "hole_to_edge_mm": 0.5}})
        )
        with flags(PNR_PAD_EDGE_CLEARANCE="1"):
            self.assertEqual(
                pad_edge_rule(rules={"fab": {"edge_clearance_mm": 0.3, "hole_to_edge_mm": 0.5}}),
                (0.3, 0.5),
            )
            self.assertEqual(pad_edge_rule(rules={"fab": {"edge_clearance_mm": 0.25}}), (0.25, 0.0))
            from pnr.constraints import compile_constraints

            con = compile_constraints({"board": {"outline": {"w": 10, "h": 8}}}, [])
            with flags(PNR_FAB_PROFILE="jlc-pofv"):
                self.assertEqual(pad_edge_rule(con), (0.3, 0.5))

    def test_legalizer_keeps_pads_and_drills_off_the_edge(self):
        for drill in (False, True):
            for target in ((5.0, 0.0), (0.0, 4.0), (10.0, 8.0), (0.0, 0.0)):
                for rot in (0.0, 90.0):
                    part = self.flush_part(pos=target, drill=drill)
                    part.rot = rot
                    g = BoardGraph("t", [part])
                    old = legalize(
                        g,
                        10.0,
                        8.0,
                        fixed={},
                        keepouts=[],
                        clearance=CL,
                        grid_mm=G,
                        rotations={"U3": rot},
                    )
                    new = legalize(
                        g,
                        10.0,
                        8.0,
                        fixed={},
                        keepouts=[],
                        clearance=CL,
                        grid_mm=G,
                        rotations={"U3": rot},
                        pad_edge=(0.3, 0.5),
                    )
                    pad_gap, hole_gap = self.edge_gaps(new.component("U3"), 10.0, 8.0)
                    self.assertGreaterEqual(pad_gap, 0.3 - 1e-9, (drill, target, rot))
                    self.assertGreaterEqual(hole_gap, 0.5 - 1e-9, (drill, target, rot))
                    from pnr.place.metrics import pad_edge_violations

                    self.assertEqual(pad_edge_violations(new, 10.0, 8.0, (0.3, 0.5)), [])
                    # courtyard-only legalization leaves the flush pad ~cl/2 from the edge
                    self.assertLess(
                        self.edge_gaps(old.component("U3"), 10.0, 8.0)[0], 0.3, (drill, target, rot)
                    )
                    self.assertEqual(pad_edge_violations(old, 10.0, 8.0, (0.3, 0.5)), ["U3"])

    def test_row_ends_keep_pads_off_the_perpendicular_edges(self):
        import copy
        from pnr.constraints import compile_constraints
        from pnr.graph import BoardOutline
        from pnr.place.rows import sample_constraints, violations

        parts = [self.flush_part(r, (5.0, 4.0), drill=True) for r in ("J0", "J1")]
        g = BoardGraph("t", parts, [], BoardOutline(8.0, 6.0))
        con = compile_constraints(
            dict(
                board=dict(outline=dict(w=8, h=6)),
                row=[dict(name="r", members=["J0", "J1"], gap_mm=0.2, edge="any", facing="south")],
            ),
            g.refs,
        )
        worst = {None: [math.inf, math.inf], (0.3, 0.5): [math.inf, math.inf]}
        for seed in range(40):
            for pe in worst:
                cc = sample_constraints(g, con, seed, **({} if pe is None else dict(pad_edge=pe)))
                trial = BoardGraph.from_json(g.to_json())
                for x in cc.constraints:
                    if x.kind == "fixed":
                        c = trial.component(x.refs[0])
                        c.pos, c.rot = tuple(x.params["at"]), x.params["rot"]
                self.assertEqual(violations(trial, cc), [])  # still a flush row on its facing edge
                for x in cc.constraints:
                    if x.kind != "fixed":
                        continue
                    c = trial.component(x.refs[0])
                    along_x = x.params["row_trial"]["edge"] in ("north", "south")
                    swap = int(round(c.rot)) % 180 == 90
                    for pad, (_, _, r) in zip(c.pads, pad_rects(c)):
                        lo, hi = (r.left, 8.0 - r.right) if along_x else (r.bottom, 6.0 - r.top)
                        worst[pe][0] = min(worst[pe][0], lo, hi)
                        if pad.through_hole:
                            dw, dh = (
                                (pad.drill_size[1], pad.drill_size[0]) if swap else pad.drill_size
                            )
                            d = dw if along_x else dh
                            c0 = r.cx if along_x else r.cy
                            worst[pe][1] = min(
                                worst[pe][1], c0 - d / 2, (8.0 if along_x else 6.0) - c0 - d / 2
                            )
        self.assertLess(worst[None][0], 0.3)  # courtyard-only rows reach the corner
        self.assertGreaterEqual(worst[(0.3, 0.5)][0], 0.3 - 1e-9)
        self.assertGreaterEqual(worst[(0.3, 0.5)][1], 0.5 - 1e-9)
        # the flag off keeps the sampler byte-identical
        a = sample_constraints(g, con, 7)
        with flags(PNR_PAD_EDGE_CLEARANCE="0"):
            b = sample_constraints(g, con, 7)
        self.assertEqual(repr(a.constraints), repr(b.constraints))

    def test_place_flags_and_prevents_pad_edge_violations(self):
        from pnr.constraints import compile_constraints
        from pnr.place.placer import place, _finish

        parts = [self.flush_part("U%d" % i, (0.5 + i, 0.5)) for i in range(4)]
        g = BoardGraph("t", parts, [Net("N1", 1, [(p.ref, "1") for p in parts])])
        con = compile_constraints({"board": {"outline": {"w": 10, "h": 8}}}, g.refs)
        rules = {"fab": {"edge_clearance_mm": 0.3, "hole_to_edge_mm": 0.5}}
        from pnr.place.metrics import pad_edge_violations

        with flags(PNR_PAD_EDGE_CLEARANCE="1"):
            placed, report = place(g, con, seed=1, iters=60, channel_rules=rules)
            self.assertTrue(report.legal, report.summary())
            self.assertEqual(pad_edge_violations(placed, 10, 8, (0.3, 0.5)), [])
            moved = BoardGraph.from_json(placed.to_json())
            c = moved.components[0]
            c.rot, c.pos = 0.0, (1.0 + 0.1, 4.0)  # courtyard 0.1 inside the edge: pads at 0.1
            _, bad = _finish(moved, g, con, 10, 8, 0.0, (0.3, 0.5))
            self.assertIn(c.ref, bad.outside_outline)
            self.assertFalse(bad.legal)
        _, ok = _finish(moved, g, con, 10, 8, 0.0)  # flag off: courtyards only, as before
        self.assertNotIn(c.ref, ok.outside_outline)


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.env = off()

    def tearDown(self):
        self.env.stop()

    def test_generations_with_hull_is_refused(self):
        from pnr.mc.halving import main

        argv = [
            "--out",
            "/nonexistent/out",
            "--inputs",
            "/nonexistent/in",
            "--constraints",
            "/nonexistent/c.yaml",
            "--repo",
            "/nonexistent",
            "--library",
            "/nonexistent/lib",
            "--generations",
            "1",
        ]
        with flags(PNR_MACRO_HULL="1", PNR_FEEDBACK="1"), mock.patch("sys.stderr") as err:
            with self.assertRaises(SystemExit) as cm:
                main(argv)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn(
            "does not support --generations",
            "".join(str(c.args[0]) for c in err.write.call_args_list),
        )

    def test_kicad_python_override(self):
        import argparse

        seen = {}
        real = argparse.ArgumentParser.parse_args

        class Stop(Exception):
            pass

        def spy(self, *a, **kw):
            seen["ns"] = real(self, *a, **kw)
            raise Stop

        for module, argv, attr in (
            ("pnr.full_iteration", ["x", "round", "--constraints", "c"], "python"),
            ("pnr.transaction_cleanup", ["x", "b", "--rules", "r"], "kicad_python"),
        ):
            mod = __import__(module, fromlist=["main"])
            for env, want in (
                (
                    {},
                    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3",
                ),
                ({"PNR_KICAD_PYTHON": "/headless/python3"}, "/headless/python3"),
            ):
                with flags(**env), mock.patch.object(
                    argparse.ArgumentParser, "parse_args", spy
                ), mock.patch("sys.argv", argv), self.assertRaises(Stop):
                    mod.main()
                self.assertEqual(getattr(seen["ns"], attr), want, (module, env))
        script = (
            "import json, pnr.hier.native_block as a, pnr.hier.power_quality as b; "
            "print(json.dumps([a.KI_PY, b.KI_PY]))"
        )
        env = {k: v for k, v in os.environ.items() if k != "PNR_KICAD_PYTHON"}
        for extra, want in (
            (
                {},
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3",
            ),
            ({"PNR_KICAD_PYTHON": "/headless/python3"}, "/headless/python3"),
        ):
            res = subprocess.run(
                [sys.executable, "-c", script],
                env=dict(env, PYTHONDONTWRITEBYTECODE="1", **extra),
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(res.returncode, 0, res.stderr[-2000:])
            self.assertEqual(json.loads(res.stdout.strip().splitlines()[-1]), [want, want])


# ----------------------------------------------------------------------------- identity vs base tree

IDENTITY_SCRIPT = textwrap.dedent(
    """
    import hashlib, json, os, sys
    from pathlib import Path
    for k in ('PNR_MACRO_SHRINK', 'PNR_MACRO_HULL', 'PNR_LIBRARY_RANK_USED'):
        os.environ.pop(k, None)
    H = Path(sys.argv[1])
    from pnr.mc.halving import _load
    from pnr.hier.top import load_library
    from pnr.hier.blocks import extract_blocks
    from pnr.hier.synth import instance_board
    from pnr.hier.macro import collapse
    from pnr.place.initial_pool import preserve_source_locks, _prepared_source
    graph, constraints, rules = _load(H / 'inputs10b', Path(sys.argv[2]))
    constraints = preserve_source_locks(graph, constraints)
    source = _prepared_source(graph, constraints, rules)
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
    out = dict(library=sha(json.dumps(load_library(H / 'blocks' / 'nb6-fb'), sort_keys=True)))
    lib = json.loads((H / 'runs/h6-hier/library.snapshot.json').read_text())
    blocks = {b.name: b for b in extract_blocks(source, constraints)}
    for rank in range(3):
        layouts = []
        for name in sorted(lib):
            rec = lib[name][min(rank, len(lib[name]) - 1)]
            sub, _, _ = instance_board(source, constraints, rules, blocks[name], rec['layout'], rec['width'], rec['height'])
            layouts.append((blocks[name], sub, rec['width'], rec['height']))
        g, c, r, plan = collapse(source, constraints, rules, layouts)
        out['collapse%d' % rank] = sha(g.to_json() + repr(c.constraints) + repr(c.copper_keepouts)
                                       + json.dumps(r, sort_keys=True, default=str)
                                       + json.dumps(plan.macros, sort_keys=True) + json.dumps(plan.member_of, sort_keys=True))
    print(json.dumps(out))
"""
)


def _run_tree(tree: Path, script: str, *args, env_extra=None, timeout=900):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PNR_MACRO")
        and not k.startswith("PNR_LIBRARY_RANK")
        and k not in ("PNR_PAD_EDGE_CLEARANCE", "PNR_KICAD_PYTHON", "PNR_POWER_FIRST")
    }
    env.update(PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1", **(env_extra or {}))
    res = subprocess.run(
        [sys.executable, "-c", script, *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if res.returncode:
        raise AssertionError(res.stderr[-3000:])
    return json.loads(res.stdout.strip().splitlines()[-1])


@unittest.skipUnless(
    HAVE_REAL and BASE_TREE.exists(), "pristine base tree / Mini inputs not available"
)
class FlagsOffIdentityTest(unittest.TestCase):
    def test_collapse_and_library_bytes_match_base(self):
        here = HERE.parents[1]
        a = _run_tree(here, IDENTITY_SCRIPT, HIER, CONSTRAINTS)
        b = _run_tree(BASE_TREE, IDENTITY_SCRIPT, HIER, CONSTRAINTS)
        self.assertEqual(a, b)


PLACE_SCRIPT = textwrap.dedent(
    """
    import hashlib, json, os, sys
    from pathlib import Path
    H = Path(sys.argv[1])
    from pnr.mc.halving import _load
    from pnr.hier.top import hierarchical_place, load_library
    graph, constraints, rules = _load(H / 'inputs10b', Path(sys.argv[2]))
    lib = load_library(H / 'runs/h6-hier/library.snapshot.json')
    out = {}
    for seed in map(int, sys.argv[3].split(',')):
        try:
            placed, report, choice = hierarchical_place(graph, constraints, rules, lib, seed, 600)
            out[seed] = dict(sha=hashlib.sha256(placed.to_json().encode()).hexdigest(),
                             choice=hashlib.sha256(json.dumps(choice, sort_keys=True).encode()).hexdigest(),
                             legal=report.legal)
        except Exception as e:
            out[seed] = dict(error=repr(e))
    print(json.dumps(out))
"""
)


@unittest.skipUnless(
    SLOW and HAVE_REAL and BASE_TREE.exists(), "set PNR_SLOW_TESTS=1 (hierarchical placements)"
)
class SlowHierarchicalTest(unittest.TestCase):
    SEEDS = "6,2827689,523651"  # h6 p000 (fails), p027, p005

    def test_flags_off_placements_match_base(self):
        here = HERE.parents[1]
        env = dict(PNR_POWER_FIRST="1")
        a = _run_tree(
            here, PLACE_SCRIPT, HIER, CONSTRAINTS, self.SEEDS, env_extra=env, timeout=3600
        )
        b = _run_tree(
            BASE_TREE, PLACE_SCRIPT, HIER, CONSTRAINTS, self.SEEDS, env_extra=env, timeout=3600
        )
        self.assertEqual(a, b)
        self.assertIn("error", a["6"])

    def test_shrink_hull_p027_is_legal_and_keeps_copper_clearance(self):
        from pnr.mc.halving import _load
        from pnr.hier.top import hierarchical_place, load_library

        graph, constraints, rules = _load(INPUTS, CONSTRAINTS)
        lib = load_library(SNAPSHOT)
        with flags(PNR_MACRO_SHRINK="1", PNR_MACRO_HULL="1", PNR_POWER_FIRST="1"):
            placed, report, choice = hierarchical_place(
                graph, constraints, rules, lib, 2827689, 600
            )
        self.assertTrue(report.legal, report.summary())
        self.assertEqual({k: v for k, v in hard_violations(placed, constraints).items() if v}, {})
        for m, v in choice["macros"].items():
            self.assertEqual(v["shape"], "used")
            self.assertIsNotNone(v["hull"])
