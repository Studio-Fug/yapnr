"""PNR_GP_POLISH / PNR_GP_CHANNELS / PNR_POOL_SOURCE_CLAMP: global placement and the legalizer
agree on spacing (pnr.place.gp_polish; docs/design/compact-placement.md section 11, ``A``).

On a toy packing pulled tight by its nets the polished global result has no slot overlap, and
the legalizer then only snaps it to the grid (no part moves more than half a cell diagonal),
where the unpolished one is pushed apart; the channel term opens a facing pair whose nets
escape and ignores a pair-local net; zero polish steps change nothing; the polish is
deterministic; the source start is clamped into the outline or the cluster box.
"""

from __future__ import annotations

import contextlib
import math
import os
import unittest
from unittest import mock

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.gp_polish import Polish
from pnr.place.legalize import legalize
from pnr.place.model import global_place

W, H = 24.0, 16.0
G = 0.25
CLEARANCE = 0.2
FLAGS = ("PNR_GP_POLISH", "PNR_GP_CHANNELS", "PNR_POOL_SOURCE_CLAMP", "PNR_COMPACT")


@contextlib.contextmanager
def env(**values):
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


def two_pad(ref, a, b, pos, size=(2.0, 1.2)):
    pads = [Pad("1", a, (-0.6, 0.0), (0.5, 0.6)), Pad("2", b, (0.6, 0.0), (0.5, 0.6))]
    return Component(ref, "R_0603", pos, 0.0, "top", size, size, pads=pads)


def board(parts):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "polish",
        list(parts),
        [Net(n, i + 1, p) for i, (n, p) in enumerate(sorted(nets.items()))],
        BoardOutline(W, H),
    )


def compiled(g, **extra):
    doc = dict(schema="v0", board=dict(outline=dict(w=W, h=H), layers=2, default_clearance_mm=0.2))
    doc.update(extra)
    return compile_constraints(doc, g.refs)


def toy():
    """Six parts on one ring of nets and a hub net: the wirelength packs them shoulder to
    shoulder in the board's middle."""
    parts = []
    for i in range(6):
        parts.append(two_pad("R%d" % i, "N%d" % i, "N%d" % ((i + 1) % 6), (4.0 + 3.0 * i, 8.0)))
    hub = [Pad(str(k + 1), "N%d" % k, (0.0, 0.0), (0.4, 0.4)) for k in range(6)]
    parts.append(Component("U1", "hub", (12.0, 8.0), 0.0, "top", (1.4, 1.4), (1.4, 1.4), pads=hub))
    return board(parts)


def polish(**kwargs):
    kwargs.setdefault("clearance", CLEARANCE)
    kwargs.setdefault("grid_mm", G)
    return Polish(**kwargs)


def place(g, cc, polish=None, iters=150, seed=0):
    positions, rotations = global_place(
        g, cc, W, H, seed=seed, iters=iters, **({} if polish is None else dict(polish=polish))
    )
    cont = BoardGraph.from_json(g.to_json())
    for comp in cont.components:
        comp.pos, comp.rot = positions[comp.ref], rotations[comp.ref]
    return cont


def slot_overlaps(cont, spec):
    """Pairs whose legalizer slots (spec's sizes, the courtyard centred) overlap by > 1 um."""
    from pnr.place.geometry import courtyard_rect

    rects = []
    for comp in cont.components:
        r = courtyard_rect(comp)
        bw, bh = spec.slot_cells(comp, r)
        rects.append((comp.ref, r.cx, r.cy, bw * G / 2, bh * G / 2))
    out = []
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            if a[3] + b[3] - abs(a[1] - b[1]) > 1e-3 and a[4] + b[4] - abs(a[2] - b[2]) > 1e-3:
                out.append((a[0], b[0]))
    return out


def displacement(cont, cc):
    out = legalize(
        cont,
        W,
        H,
        fixed={},
        keepouts=[],
        clearance=CLEARANCE,
        grid_mm=G,
        allow_rotation=True,
        wire_weight=0.0,
    )
    return {c.ref: math.dist(c.pos, cont.component(c.ref).pos) for c in out.components}


class SlotTest(unittest.TestCase):
    def test_polished_layout_only_snaps(self):
        g = toy()
        cc = compiled(g)
        spec = polish()
        tight = place(g, cc)
        self.assertTrue(slot_overlaps(tight, spec))  # the toy really packs too close
        moved = displacement(tight, cc)
        self.assertGreater(max(moved.values()), G / math.sqrt(2) + 1e-9)
        polished = place(g, cc, spec)
        self.assertEqual(slot_overlaps(polished, spec), [])
        moved = displacement(polished, cc)
        self.assertLessEqual(max(moved.values()), G / math.sqrt(2) + 1e-6, moved)
        # The turns are the global ones: the polish never turns a part.
        self.assertEqual(
            {c.ref: c.rot for c in polished.components}, {c.ref: c.rot for c in tight.components}
        )

    def test_zero_steps_and_none_are_the_unpolished_path(self):
        g = toy()
        cc = compiled(g)
        base = place(g, cc).to_json()
        self.assertEqual(place(g, cc, polish(steps=0)).to_json(), base)

    def test_deterministic(self):
        g = toy()
        cc = compiled(g)
        spec = polish(channel_weight=1.0, rules=dict())
        self.assertEqual(place(g, cc, spec).to_json(), place(g, cc, spec).to_json())


class ChannelTest(unittest.TestCase):
    def pair(self, escaping=True):
        """A and B face each other east-west; their facing pads carry nets that reach a far
        pin each (escaping) or only each other (pair-local)."""
        a = Component(
            "A",
            "ic",
            (11.0, 8.0),
            0.0,
            "top",
            (2.0, 3.0),
            (2.0, 3.0),
            pads=[
                Pad(str(k + 1), "E%d" % k if escaping else "L%d" % k, (0.7, 1.0 - k), (0.5, 0.4))
                for k in range(3)
            ],
        )
        b = Component(
            "B",
            "ic",
            (13.0, 8.0),
            0.0,
            "top",
            (2.0, 3.0),
            (2.0, 3.0),
            pads=[
                Pad(str(k + 1), "F%d" % k if escaping else "L%d" % k, (-0.7, 1.0 - k), (0.5, 0.4))
                for k in range(3)
            ],
        )
        parts = [a, b]
        if escaping:
            pins = [Pad(str(k + 1), "E%d" % k, (0.0, 0.0), (0.4, 0.4)) for k in range(3)]
            pins += [Pad(str(k + 4), "F%d" % k, (0.0, 0.0), (0.4, 0.4)) for k in range(3)]
            parts.append(
                Component("J1", "conn", (12.0, 14.5), 0.0, "top", (1.0, 1.0), (1.0, 1.0), pads=pins)
            )
        # A strong net pulls A and B together.
        a.pads.append(Pad("9", "S", (0.0, 0.0), (0.2, 0.2)))
        b.pads.append(Pad("9", "S", (0.0, 0.0), (0.2, 0.2)))
        g = board(parts)
        fixed = {"J1": dict(at=[12.0, 14.5], rot=0, side="top")} if escaping else {}
        return g, compiled(g, fixed=fixed, orientation={"A": 0, "B": 0})

    def rules(self):
        return dict(fab=dict(track_width_mm=0.2), default_clearance_mm=0.2)

    def shortage(self, cont):
        from pnr.place.channels import ChannelModel

        return ChannelModel(cont, self.rules()).report(cont)["shortage_score"]

    def test_escaping_rows_open_up(self):
        g, cc = self.pair()
        plain = place(g, cc, polish(), iters=200)
        channel = place(g, cc, polish(channel_weight=1.0, rules=self.rules()), iters=200)
        self.assertGreater(self.shortage(plain), 0.0)
        self.assertLess(self.shortage(channel), self.shortage(plain))
        gap = lambda c: c.component("B").pos[0] - c.component("A").pos[0]  # noqa: E731
        self.assertGreater(gap(channel), gap(plain))

    def test_pair_local_nets_reserve_nothing(self):
        g, _ = self.pair(escaping=False)
        tensors = polish(channel_weight=1.0, rules=self.rules()).prepare(
            g, [0.0, 0.0], ["top", "top"], W, H
        )
        self.assertEqual(float(tensors["need"].abs().sum()), 0.0)
        g, _ = self.pair()
        tensors = polish(channel_weight=1.0, rules=self.rules()).prepare(
            g, [0.0, 0.0, 0.0], ["top"] * 3, W, H
        )
        self.assertGreater(float(tensors["need"][0, 1, 1]), 0.0)  # B east of A


class FlagTest(unittest.TestCase):
    def test_placer_builds_the_polish_only_with_a_flag(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import compact_fixture as fixture

        from pnr.place import gp_polish, placer

        graph, constraints, rules = fixture.load("04-inverter-leds-8")
        with env(), mock.patch.object(gp_polish, "Polish", side_effect=AssertionError("built")):
            placer.place(graph, constraints, seed=0, iters=40, spread=1.3, channel_rules=rules)
        seen = []
        real = gp_polish.Polish

        def spy(**kwargs):
            seen.append(kwargs)
            return real(**kwargs)

        with env(PNR_GP_CHANNELS="0.5"), mock.patch.object(gp_polish, "Polish", spy):
            placed, report = placer.place(
                graph, constraints, seed=0, iters=40, spread=1.3, channel_rules=rules
            )
        self.assertTrue(report.legal)
        (kwargs,) = seen
        self.assertEqual(kwargs["channel_weight"], 0.5)
        self.assertEqual(kwargs["spread"], 1.3)
        self.assertEqual(kwargs["grid_mm"], 0.25)
        self.assertEqual(kwargs["clearance"], float(constraints.board.default_clearance_mm))


class SourceClampTest(unittest.TestCase):
    def staging(self):
        """The toy with its source rows far off the board (a generated staging layout)."""
        g = toy()
        for i, comp in enumerate(g.components):
            comp.pos = (60.0 + 3.0 * i, -40.0)
        return g

    def test_source_start_is_clamped_into_the_outline(self):
        from pnr.place.initial_pool import InitialPoolConfig, initial_starts

        g = self.staging()
        cc = compiled(g)
        with env():
            plain = initial_starts(g, cc, InitialPoolConfig(), seed=0)
        self.assertTrue(any(x > W for x, _ in plain[1]["positions"].values()))
        with env(PNR_POOL_SOURCE_CLAMP="1"):
            clamped = initial_starts(g, cc, InitialPoolConfig(), seed=0)
        for ref, (x, y) in clamped[1]["positions"].items():
            hx, hy = (v / 2 for v in g.component(ref).courtyard)
            self.assertTrue(hx - 1e-9 <= x <= W - hx + 1e-9 and hy - 1e-9 <= y <= H - hy + 1e-9)
        # Every other start is unchanged.
        self.assertEqual(clamped[2:], plain[2:])
        self.assertEqual(clamped[0], plain[0])

    def test_compact_clamps_into_the_cluster_box(self):
        from pnr.place import compact
        from pnr.place.initial_pool import InitialPoolConfig, initial_starts

        g = self.staging()
        cc = compiled(g)
        with env(PNR_COMPACT="1", PNR_POOL_SOURCE_CLAMP="1"):
            x0, y0, bw, bh = compact.cluster_box(g, cc, W, H)
            starts = initial_starts(g, cc, InitialPoolConfig(), seed=0)
        for x, y in starts[1]["positions"].values():
            self.assertTrue(x0 - 1e-9 <= x <= x0 + bw + 1e-9 and y0 - 1e-9 <= y <= y0 + bh + 1e-9)


if __name__ == "__main__":
    unittest.main()
