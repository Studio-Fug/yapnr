"""PNR_LEGALIZE_HPWL: the legalizer's wirelength term and its four-turn search
(docs/design/compact-placement.md section 11, ``C1``).

The wirelength helper against metrics.hpwl, the README case (a series resistor between a
fixed connector pin and an LED takes the near-side slot with the term and the far one without),
a reversed two-pad part half-turned, ties keeping the global turn, hard rotations, offset
courtyards at the chosen turn, plane nets left out, determinism, and the line-chaser case: the
series resistors of a line group's LEDs end upright beside their LEDs, between the driver and
the row (flush), where displacement alone puts them on the far side of the row.
"""

from __future__ import annotations

import contextlib
import copy
import math
import os
import unittest
from unittest import mock

import numpy as np

from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place import legalize as legalize_mod
from pnr.place.geometry import courtyard_rect
from pnr.place.legalize import legalize, plane_nets, wire_cost, wire_turns
from pnr.place.metrics import hpwl, outside_outline, overlap_pairs

ENV = ("PNR_LEGALIZE_HPWL", "PNR_COMPACT", "PNR_COMPACT_COURTYARD")


@contextlib.contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in ENV}
    for k in ENV:
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


def two_pad(ref, a, b, pos, rot=0.0, size=(2.0, 1.0), pitch=1.0, body=None, offsets=None):
    """A two-pad part, pads ``a`` and ``b`` along x, ``pitch`` apart about the origin."""
    offsets = offsets or ((-pitch / 2, 0.0), (pitch / 2, 0.0))
    pads = [Pad("1", a, offsets[0], (0.4, 0.4)), Pad("2", b, offsets[1], (0.4, 0.4))]
    return Component(ref, "R_0603", pos, rot, "top", size, size, pads=pads, body=body)


def pin(ref, nets, pos, size=(2.0, 2.0)):
    """A fixed part with one pad per net, all at its origin."""
    pads = [Pad(str(i + 1), n, (0.0, 0.0), (0.4, 0.4)) for i, n in enumerate(nets)]
    return Component(ref, "conn", pos, 0.0, "top", size, size, pads=pads)


def graph(parts, w=20.0, h=12.0):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "wire",
        list(parts),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(w, h),
    )


def run(g, fixed, weight, **kwargs):
    kwargs.setdefault("allow_rotation", True)
    kwargs.setdefault("grid_mm", 0.25)
    return legalize(
        g,
        g.outline.width,
        g.outline.height,
        fixed={r: g.component(r).pos for r in fixed},
        keepouts=[],
        clearance=0.2,
        wire_weight=weight,
        **kwargs,
    )


def nets_hpwl(g, names):
    sub = copy.deepcopy(g)
    sub.nets = [n for n in sub.nets if n.name in names]
    return hpwl(sub)


class WireCostTest(unittest.TestCase):
    def test_matches_hpwl_of_the_parts_nets(self):
        r = two_pad("R1", "A", "B", (5.0, 5.0), rot=90.0)
        j = pin("J1", ["A"], (1.0, 2.0))
        d = pin("D1", ["B"], (9.0, 8.0))
        g = graph([r, j, d])
        xs = np.array([3.0, 5.0, 8.5])
        ys = np.array([1.0, 6.0, 9.0])
        got = wire_cost(r, [j], [d], xs, ys)
        for x, y, value in zip(xs, ys, got):
            r.pos = (float(x), float(y))
            self.assertAlmostEqual(value, hpwl(g), places=9)

    def test_skips_plane_nets_and_lone_pads(self):
        r = two_pad("R1", "A", "GND", (5.0, 5.0))
        j = pin("J1", ["A", "GND"], (1.0, 2.0))
        # A at (4.5, 5) and GND at (5.5, 5), J1's pins at (1, 2).
        self.assertAlmostEqual(float(wire_cost(r, [j], [], 5.0, 5.0)), (3.5 + 3.0) + (4.5 + 3.0))
        self.assertAlmostEqual(
            float(wire_cost(r, [j], [], 5.0, 5.0, frozenset({"GND"}))), 3.5 + 3.0
        )
        # A net with one pad of the part and no other pin adds nothing.
        self.assertEqual(float(wire_cost(r, [], [], 5.0, 5.0)), 0.0)

    def test_plane_nets_of_a_channel_model(self):
        from pnr.place.channels import ChannelModel

        g = graph([two_pad("R1", "A", "GND", (5.0, 5.0)), pin("J1", ["A", "GND"], (1.0, 2.0))])
        rules = dict(net_classes=[dict(nets=["GND"], plane_layer="In1.Cu")])
        self.assertEqual(plane_nets(ChannelModel(g, rules)), frozenset({"GND"}))
        self.assertEqual(plane_nets(None), frozenset())

    def test_turns(self):
        self.assertEqual(wire_turns(90.0, True), [90.0, 180.0, 270.0, 0.0])
        self.assertEqual(wire_turns(90.0, False), [90.0])


class SlotAndTurnTest(unittest.TestCase):
    def readme_case(self):
        """A series resistor R1 between the connector pin J1 (net A) and the LED pin D1
        (net B), both south of an obstacle X1 its global target overlaps, a little north of
        the obstacle's centre: the nearest free slot is north (far side), the short one south."""
        x1 = Component("X1", "block", (10.0, 6.0), 0.0, "top", (8.0, 2.0), (8.0, 2.0))
        parts = [
            x1,
            pin("J1", ["A"], (8.0, 1.5)),
            pin("D1", ["B"], (12.0, 1.5)),
            two_pad("R1", "A", "B", (10.0, 6.3)),
        ]
        return graph(parts), ("X1", "J1", "D1")

    def test_near_side_with_the_term_far_side_without(self):
        g, fixed = self.readme_case()
        far = run(g, fixed, 0.0).component("R1")
        near = run(g, fixed, 4.0).component("R1")
        self.assertGreater(far.pos[1], 7.0)  # north of X1, away from J1 and D1
        self.assertLess(near.pos[1], 5.0)  # between X1 and the pins
        before = nets_hpwl(run(g, fixed, 0.0), {"A", "B"})
        after = nets_hpwl(run(g, fixed, 4.0), {"A", "B"})
        self.assertLess(after, before - 5.0)

    def test_reversed_part_is_half_turned(self):
        # Square body: every turn has the same slot, only the pins differ.
        r = two_pad("R1", "A", "B", (10.0, 6.0), size=(2.0, 2.0))
        g = graph([pin("J1", ["A"], (16.0, 6.0)), pin("D1", ["B"], (4.0, 6.0)), r])
        self.assertEqual(run(g, ("J1", "D1"), 0.0).component("R1").rot, 0.0)
        self.assertEqual(run(g, ("J1", "D1"), 4.0).component("R1").rot, 180.0)
        # No rotation search, or a hard rotation: the global turn stays.
        no_turn = run(g, ("J1", "D1"), 4.0, allow_rotation=False)
        self.assertEqual(no_turn.component("R1").rot, 0.0)
        held = run(g, ("J1", "D1"), 4.0, rotations={"R1": 0.0})
        self.assertEqual(held.component("R1").rot, 0.0)

    def test_ties_keep_the_global_turn(self):
        # Both nets end at one far corner: every turn has the same wirelength everywhere.
        r = two_pad("R1", "A", "B", (10.0, 6.0), rot=90.0, size=(2.0, 2.0))
        g = graph([pin("J1", ["A", "B"], (19.0, 11.0)), r])
        out = run(g, ("J1",), 4.0).component("R1")
        self.assertEqual(out.rot, 90.0)
        self.assertNotEqual(out.pos, (10.0, 6.0))  # pulled toward the corner, still turned 90

    def test_offset_courtyard_takes_its_turns_shift(self):
        # Body off the origin toward +x (a pin-1-origin part); the pins want it reversed.
        r = two_pad(
            "R1",
            "A",
            "B",
            (10.0, 6.0),
            size=(6.0, 1.0),
            body=(-0.5, -0.5, 2.5, 0.5),
            offsets=((0.0, 0.0), (2.0, 0.0)),
        )
        g = graph([pin("J1", ["A"], (17.0, 6.0)), pin("D1", ["B"], (3.0, 6.0)), r])
        with env(PNR_COMPACT="1"):
            placed = run(g, ("J1", "D1"), 4.0, grid_mm=0.125)
            comp = placed.component("R1")
            self.assertEqual(comp.rot, 180.0)
            rect = courtyard_rect(comp)
            self.assertAlmostEqual(rect.w, 3.0)
            # The body box is centred in a slot of whole cells: the pose is its centre less
            # the shift at 180 degrees, not at the global turn.
            cells = math.ceil((rect.w + 0.2) / 0.125)
            left = rect.cx / 0.125 - cells / 2.0
            self.assertAlmostEqual(left, round(left), places=6)
            self.assertEqual(overlap_pairs(placed), [])
            self.assertEqual(outside_outline(placed, 20.0, 12.0), [])

    def test_deterministic(self):
        g, fixed = self.readme_case()
        self.assertEqual(run(g, fixed, 4.0).to_json(), run(g, fixed, 4.0).to_json())


class FlagTest(unittest.TestCase):
    def test_legalize_takes_the_weight_from_its_caller_only(self):
        """legalize() never reads the environment: the placer passes PNR_LEGALIZE_HPWL, so the
        pool's basin fallback and power-first placement keep the plain legalizer."""
        g, fixed = SlotAndTurnTest().readme_case()

        def refuse(*args, **kwargs):
            raise AssertionError("wirelength term without a weight")

        with env():
            reference = run(g, fixed, 0.0).to_json()
        with env(PNR_LEGALIZE_HPWL="4"), mock.patch.object(
            legalize_mod, "wire_cost", refuse
        ), mock.patch.object(legalize_mod, "wire_turns", refuse):
            self.assertEqual(run(g, fixed, None).to_json(), reference)

    def test_placer_passes_the_weight_and_the_matched_parts(self):
        from pnr.constraints import compile_constraints
        from pnr.place import placer

        r1 = two_pad("R1", "DP", "A", (5.0, 5.0))
        r2 = two_pad("R2", "DN", "B", (5.0, 7.0))
        r3 = two_pad("R3", "C", "A", (8.0, 5.0))
        g = graph([r1, r2, r3])
        con = compile_constraints({"diff_pair": [{"name": "USB", "p": "DP", "n": "DN"}]}, g.refs)
        with env():
            self.assertEqual(placer._wire_kwargs(g, con), {})
        with env(PNR_LEGALIZE_HPWL="4"):
            self.assertEqual(
                placer._wire_kwargs(g, con),
                dict(wire_weight=4.0, wire_exempt=frozenset({"R1", "R2"})),
            )
        with env(PNR_LEGALIZE_HPWL="-1"), self.assertRaises(ValueError):
            placer._wire_kwargs(g, con)


class ExemptTest(unittest.TestCase):
    def test_exempt_part_keeps_the_plain_search(self):
        """A matched-length part (``wire_exempt``) is legalized as without the term: its slot
        and turn are the weight-0 ones, while the other parts still get the term."""
        g, fixed = SlotAndTurnTest().readme_case()
        plain = run(g, fixed, 0.0)
        wired = run(g, fixed, 4.0)
        exempt = run(g, fixed, 4.0, wire_exempt=frozenset({"R1"}))
        self.assertNotEqual(
            (wired.component("R1").pos, wired.component("R1").rot),
            (plain.component("R1").pos, plain.component("R1").rot),
        )
        self.assertEqual(exempt.component("R1").pos, plain.component("R1").pos)
        self.assertEqual(exempt.component("R1").rot, plain.component("R1").rot)


class PruneTest(unittest.TestCase):
    def test_pruned_choice_is_the_full_one(self):
        """``_place_part(bound=...)`` returns the cell the full scoring returns, ties (the
        first cell, row-major) included."""
        from pnr.place.legalize import _place_part

        rng = np.random.default_rng(7)
        for trial in range(40):
            ny, nx = rng.integers(8, 40, size=2)
            occ = rng.random((ny, nx)) < 0.3
            target = (rng.uniform(0, nx * 0.25), rng.uniform(0, ny * 0.25))
            grain = 0.5 if trial % 2 else 1e-3  # coarse values force ties

            def rest(xs, ys):
                return np.round(np.abs(np.sin(xs * 3.1) * np.cos(ys * 1.7)) * 9 / grain) * grain

            def cheap(xs, ys):
                return np.round((np.abs(xs - 2.0) + np.abs(ys - 1.0)) * 4 / grain) * grain

            def full(xs, ys):
                return rest(xs, ys) + cheap(xs, ys)

            kwargs = dict(target=target, limits=(), forbidden=[(1, 1)])
            try:
                want = _place_part(occ, 0.25, 2, 1, candidate_cost=full, **kwargs)
            except Exception:  # noqa: BLE001  (no free slot: both must agree)
                with self.assertRaises(Exception):
                    _place_part(occ, 0.25, 2, 1, candidate_cost=full, bound=(rest, cheap), **kwargs)
                continue
            got = _place_part(occ, 0.25, 2, 1, candidate_cost=full, bound=(rest, cheap), **kwargs)
            self.assertEqual(got, want, trial)

    def test_same_board_with_and_without_the_prune(self):
        from pnr.place import legalize as module

        g, fixed = SlotAndTurnTest().readme_case()
        pruned = run(g, fixed, 4.0).to_json()
        real = module._cheapest

        def unpruned(dist2, free, cx, cy, rest, cheap):
            out = np.where(
                free,
                dist2
                + (
                    rest(np.broadcast_to(cx, free.shape), np.broadcast_to(cy, free.shape))
                    + cheap(np.broadcast_to(cx, free.shape), np.broadcast_to(cy, free.shape))
                ),
                np.inf,
            )
            r, c = np.unravel_index(np.argmin(out), free.shape)
            return int(r), int(c)

        with mock.patch.object(module, "_cheapest", unpruned):
            self.assertEqual(run(g, fixed, 4.0).to_json(), pruned)
        self.assertIs(module._cheapest, real)


class BanTest(unittest.TestCase):
    def test_half_turn_shares_the_ban(self):
        from pnr.place.legalize import banned_cells

        entries = [(0.0, 3, 4, "top"), (90.0, 5, 6, "top"), (180.0, 7, 8, "bottom")]
        self.assertEqual(banned_cells(entries, 0.0, "top"), [(3, 4)])
        self.assertEqual(banned_cells(entries, 180.0, "top"), [])
        self.assertEqual(banned_cells(entries, 180.0, "top", half_turns=True), [(3, 4)])
        self.assertEqual(banned_cells(entries, 270.0, "top", half_turns=True), [(5, 6)])
        self.assertEqual(banned_cells(entries, 0.0, "bottom", half_turns=True), [(7, 8)])


class LineChaserTest(unittest.TestCase):
    """The README's line-constrained chaser in miniature: three LEDs in a line group (turned
    90 degrees, 3 mm pitch) above a fixed driver U1, each LED's series resistor between the
    driver pin and the LED's anode, and the cathodes to a connector at the top. The resistors'
    global targets overlap the row, a little north of it."""

    def board(self):
        from pnr.constraints import compile_constraints, compile_routing_rules

        u1 = Component(
            "U1",
            "driver",
            (9.0, 2.0),
            0.0,
            "top",
            (10.0, 2.0),
            (10.0, 2.0),
            pads=[
                Pad(str(i + 1), "S%d" % (i + 1), (3.0 * i - 3.0, 0.5), (0.6, 0.6)) for i in range(3)
            ],
        )
        j1 = pin("J1", ["GND"], (9.0, 11.0), size=(3.0, 1.5))
        parts = [u1, j1]
        for i in range(3):
            x = 6.0 + 3.0 * i
            led = [Pad("1", "L%d" % (i + 1), (-0.75, 0.0), (0.8, 0.8))]
            led.append(Pad("2", "GND", (0.75, 0.0), (0.8, 0.8)))
            parts.append(
                Component(
                    "D%d" % (i + 1),
                    "LED_0805",
                    (x, 7.0),
                    90.0,
                    "top",
                    (2.0, 1.2),
                    (2.0, 1.2),
                    pads=led,
                )
            )
            parts.append(
                two_pad("R%d" % (i + 1), "S%d" % (i + 1), "L%d" % (i + 1), (x, 7.3), pitch=1.2)
            )
        g = graph(parts, w=18.0, h=12.0)
        spec = dict(
            schema="v0",
            board=dict(outline=dict(w=18, h=12), layers=2, default_clearance_mm=0.2),
            fixed={
                "U1": dict(at=[9, 2], rot=0, side="top"),
                "J1": dict(at=[9, 11], rot=0, side="top"),
            },
            line_group=[dict(name="leds", members=["D1", "D2", "D3"], pitch_mm=3.0, rot=90)],
        )
        cc = compile_constraints(spec, g.refs)
        rules = compile_routing_rules(cc, [n.name for n in g.nets])
        return g, cc, rules

    def legalized(self, weight):
        from pnr.place import line_group
        from pnr.place.channels import ChannelModel
        from pnr.place.geometry import resolve_fixed_poses
        from pnr.place.legalize import legalize_constraint_kwargs

        g, cc, rules = self.board()
        mgraph, mcon, mrules, plan = line_group.collapse(g, cc, rules)
        positions, rotations = line_group.map_starts(
            plan, {c.ref: c.pos for c in g.components}, {c.ref: c.rot for c in g.components}
        )
        for comp in mgraph.components:
            comp.pos, comp.rot = tuple(positions[comp.ref]), rotations[comp.ref]
        poses = resolve_fixed_poses(mgraph, mcon)
        placed = legalize(
            mgraph,
            18.0,
            12.0,
            allow_rotation=True,
            channel_model=ChannelModel(mgraph, mrules),
            clearance=0.2,
            grid_mm=0.25,
            wire_weight=weight,
            **legalize_constraint_kwargs(mgraph, mcon, poses),
        )
        flat = plan.expand(placed, g)
        return flat, cc

    def flush(self, board):
        """Refs of the resistors that sit upright directly below their LED (within half a
        slot cell of 0.25 mm plus the odd slot's 0.125 mm offset), between U1 and the row, the
        pad on the LED's net facing the LED's anode."""
        out = []
        for i in range(3):
            r, d = board.component("R%d" % (i + 1)), board.component("D%d" % (i + 1))
            upright = int(round(r.rot)) % 360 == 90
            below = 3.0 < r.pos[1] < d.pos[1] and abs(r.pos[0] - d.pos[0]) <= 0.5 + 1e-9
            if upright and below:
                out.append(r.ref)
        return out

    def test_series_resistors_end_flush_with_the_row(self):
        from pnr.place import line_group
        from pnr.place.metrics import hard_violations

        flat, cc = self.legalized(4.0)
        self.assertEqual(line_group.violations(flat, cc), [])
        self.assertFalse(any(hard_violations(flat, cc).values()))
        self.assertEqual(self.flush(flat), ["R1", "R2", "R3"])
        # The row keeps its order and turn: the macro turns as one body (here not at all).
        xs = [flat.component("D%d" % (i + 1)).pos[0] for i in range(3)]
        self.assertEqual(xs, sorted(xs))
        self.assertTrue(all(flat.component("D%d" % (i + 1)).rot == 90.0 for i in range(3)))

    def test_without_the_term_they_land_on_the_far_side(self):
        flat, _ = self.legalized(0.0)
        self.assertEqual(self.flush(flat), [])
        series = {"S1", "S2", "S3", "L1", "L2", "L3"}
        self.assertGreater(nets_hpwl(flat, series), nets_hpwl(self.legalized(4.0)[0], series) + 5.0)


if __name__ == "__main__":
    unittest.main()
