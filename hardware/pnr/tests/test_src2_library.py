"""Hierarchical library / macro placement fixes (src2 'library' key).

Covers: two-sided reservation of assembled ``block:`` macros, straddle refusal
in :func:`pnr.hier.macro.collapse`, the synth_native outline cap / stratified
order / repeat aggregation / scheduler / atomic library, and the top-level
rank-geometric library draw. Real Mini inputs are used where available.
"""
import copy
import json
import math
import os
import random
import tempfile
import threading
import unittest
from pathlib import Path

from pnr.constraints import Constraint, Enforcement
from pnr.graph import BoardGraph, Component, Pad
from pnr.place.geometry import occupied_sides, placement_rects
from pnr.place.legalize import legalize
from pnr.place.metrics import overlap_pairs

HERE = Path(__file__).resolve()
INPUTS = HERE.parents[4] / 'inputs'
CONSTRAINTS = HERE.parents[2] / 'splanc_dev' / 'mini-constraints.yaml'
HAVE_MINI = (INPUTS / 'graph.json').exists() and CONSTRAINTS.exists()
BLOCK_LIBRARY = HERE.parents[4] / 'blocks' / 'nb1'


def part(ref, side, footprint='test', size=(4, 4), through=False):
    return Component(ref, footprint, (5, 5), 0, side, size, size,
                     pads=[Pad('1', 'GND', (0, 0), (1, 1), through)])


class MacroSidesTest(unittest.TestCase):
    def test_block_macro_occupies_both_sides(self):
        self.assertEqual(occupied_sides(part('MB00', 'top', 'block:board.x')), ('top', 'bottom'))
        self.assertEqual(occupied_sides(part('U1', 'top')), ('top',))
        self.assertEqual(sorted(s for s, _ in placement_rects(part('MB00', 'top', 'block:board.x'))),
                         ['bottom', 'top'])

    def test_bottom_part_cannot_share_macro_outline(self):
        for footprint, shared in (('test', True), ('block:board.x', False)):
            g = BoardGraph('t', [part('MB00', 'top', footprint, size=(6, 6)), part('TP1', 'bottom')])
            g.component('MB00').pos = g.component('TP1').pos = (10, 10)
            self.assertEqual(overlap_pairs(g), [] if shared else [('MB00', 'TP1')])
            result = legalize(g, 20, 20, fixed={'MB00': (10, 10)}, keepouts=[], grid_mm=.25)
            self.assertEqual(overlap_pairs(result), [])
            x, y = result.component('TP1').pos
            moved = max(abs(x - 10), abs(y - 10))
            self.assertLess(moved, .3) if shared else self.assertGreaterEqual(moved, 5 - 1e-6)


@unittest.skipUnless(HAVE_MINI, 'Mini inputs not present')
class MiniCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pnr.mc.halving import _load
        from pnr.hier.blocks import extract_blocks
        cls.graph, cls.constraints, cls.rules = _load(INPUTS, CONSTRAINTS)
        cls.blocks = {b.name: b for b in extract_blocks(cls.graph, cls.constraints)}

    def layouts(self, names):
        from pnr.hier.blocks import aspect_sizes, sub_board
        out = []
        for name in names:
            b = self.blocks[name]
            w, h = aspect_sizes(self.graph, b)[0][:2]
            sub = sub_board(self.graph, self.constraints, self.rules, b, w, h)[0]
            out.append((b, sub, w, h))
        return out

    def collapse(self, constraints, names=None):
        from pnr.hier.macro import collapse
        return collapse(self.graph, constraints, self.rules, self.layouts(names or sorted(self.blocks)))

    def with_constraint(self, con):
        c = copy.deepcopy(self.constraints)
        c.constraints.append(con)
        return c


class CollapseStraddleTest(MiniCase):
    def test_mini_blocks_collapse_cleanly(self):
        mgraph, mcon, _, plan = self.collapse(self.constraints)
        self.assertEqual(len(plan.macros), len(self.blocks))
        self.assertTrue(all(c.footprint.startswith('block:') for c in mgraph.components if c.ref in plan.macros))
        # Hard groups live inside blocks; only the members' axis locks survive, on their macros.
        self.assertFalse([c for c in mcon.constraints if c.kind == 'group' and c.enforcement == Enforcement.HARD])
        locked = {c.refs[0] for c in mcon.constraints if c.kind == 'orientation'}
        self.assertEqual(locked, {plan.member_of['U17'], plan.member_of['U4']})

    def member_and_outside(self):
        bmp = self.blocks['group:board.bmp']  # no orientation lock inside
        outside = next(c.ref for c in self.graph.components
                       if all(c.ref not in b.refs for b in self.blocks.values()))
        return bmp.refs[0], outside

    def test_hard_group_straddle_raises(self):
        member, outside = self.member_and_outside()
        for anchor, refs in ((member, (outside,)), (outside, (member,)), (member, (member, outside))):
            con = Constraint('group', Enforcement.HARD, refs, {'anchor': anchor, 'radius_mm': 5})
            with self.assertRaisesRegex(ValueError, 'hard group constraint straddles macro'):
                self.collapse(self.with_constraint(con))
        soft = Constraint('group', Enforcement.SOFT, (outside,), {'anchor': member, 'radius_mm': 5}, 1.0)
        self.collapse(self.with_constraint(soft))  # soft pull may approximate via the macro

    def test_hard_group_across_two_macros_raises(self):
        a, b = self.blocks['group:board.mpu'].refs[0], self.blocks['group:board.bmp'].refs[0]
        con = Constraint('group', Enforcement.HARD, (b,), {'anchor': a, 'radius_mm': 9})
        for names in (None, ['group:board.mpu', 'board.pd'], ['group:board.bmp']):  # bmp or mpu flat: still mixed
            with self.assertRaises(ValueError):
                self.collapse(self.with_constraint(con), names)
        self.collapse(self.with_constraint(con), ['board.pd'])  # both flat: an ordinary hard group

    def test_orientation_straddle_raises(self):
        member, outside = self.member_and_outside()
        con = Constraint('orientation', Enforcement.HARD, (outside, member), {'rot': 90})
        with self.assertRaisesRegex(ValueError, 'orientation constraint straddles'):
            self.collapse(self.with_constraint(con))
        self.collapse(self.with_constraint(Constraint('orientation', Enforcement.HARD, (outside,), {'rot': 90})))

    def test_member_orientation_locks_must_agree(self):
        # The test layouts keep source rotations, so U17 (locked 270) needs macro rotation 270 - rot.
        other = next(r for r in self.blocks['group:board.mpu'].refs if r != 'U17')
        need = (270 - self.graph.component('U17').rot) % 360
        rot = self.graph.component(other).rot
        bad = Constraint('orientation', Enforcement.HARD, (other,), {'rot': (rot + need + 90) % 360})
        with self.assertRaisesRegex(ValueError, 'orientation locks'):
            self.collapse(self.with_constraint(bad))
        good = Constraint('orientation', Enforcement.HARD, (other,), {'rot': (rot + need) % 360})
        _, mcon, _, plan = self.collapse(self.with_constraint(good))
        mine = [c for c in mcon.constraints if c.kind == 'orientation' and c.refs == (plan.member_of['U17'],)]
        self.assertEqual([c.params['rot'] for c in mine], [need])

    def test_ref_relative_keepout_straddle_raises(self):
        member, outside = self.member_and_outside()
        con = Constraint('keepout', Enforcement.HARD, (member, outside),
                         {'extent': {'edge': 'north', 'depth_mm': 2}, 'polygon': None}, name='ko')
        with self.assertRaisesRegex(ValueError, "keepout 'ko' constraint straddles"):
            self.collapse(self.with_constraint(con))
        poly = Constraint('keepout', Enforcement.HARD, (), {'extent': None, 'polygon': [[0, 0], [1, 0], [1, 1]]})
        self.collapse(self.with_constraint(poly))


class OutlineCapTest(MiniCase):
    def test_hard_group_bounds_module_outlines(self):
        from pnr.hier.synth_native import CAP_EDGE_MARGIN_MM, outline_cap, outline_sizes
        by_ref = {c.ref: c for c in self.graph.components}
        for name, radius in (('board.converter', 12), ('board.pd', 12)):
            b = self.blocks[name]
            cap = outline_cap(self.graph, self.constraints, b)
            biggest = max(max(by_ref[r].courtyard) for r in b.refs)
            self.assertIsNotNone(cap)
            self.assertLessEqual(cap, 2 * radius + biggest + CAP_EDGE_MARGIN_MM + .25)
            self.assertGreaterEqual(cap, biggest + CAP_EDGE_MARGIN_MM)   # never below aspect_sizes' span
            sizes, cap2 = outline_sizes(self.graph, self.constraints, b)
            self.assertEqual(cap, cap2)
            self.assertTrue(all(w <= cap and h <= cap for w, h, _, _ in sizes))
            self.assertEqual(len(sizes), len({(w, h) for w, h, _, _ in sizes}))
        # LED channels carry only a soft group: no mechanical bound.
        self.assertIsNone(outline_cap(self.graph, self.constraints, self.blocks['board.led0']))

    def test_pair_bound_is_exact_for_two_parts(self):
        from pnr.constraints import CompiledConstraints
        from pnr.hier.blocks import Block
        from pnr.hier.synth_native import outline_cap
        g = BoardGraph('t', [part('A', 'top', size=(3, 1)), part('B', 'top', size=(2, 2)),
                             part('C', 'top', size=(1, 1))])
        con = copy.deepcopy(self.constraints)
        con.constraints = [Constraint('group', Enforcement.HARD, ('B',), {'anchor': 'A', 'radius_mm': 4}),
                           Constraint('group', Enforcement.HARD, ('C',), {'anchor': 'B', 'radius_mm': 1})]
        self.assertEqual(outline_cap(g, con, Block('x', ['A', 'B'])), 4 + 2.5 + 1)
        self.assertEqual(outline_cap(g, con, Block('x', ['A', 'B', 'C'])), 5 + 2 + 1)  # A-C via B, + margin
        con.constraints.pop()
        self.assertIsNone(outline_cap(g, con, Block('x', ['A', 'B', 'C'])))

    def test_tight_bound_keeps_a_legal_outline(self):
        """A bound set by one big part, or by a pair forced apart to it, must still legalize."""
        from pnr.hier.blocks import Block
        from pnr.hier.synth_native import outline_sizes
        cases = [(('J1', (10, 3)), ('C1', (1, 1)), 2.5),    # cap from J1 alone
                 (('U1', (5, 5)), ('U2', (5, 5)), 5.25)]    # square pair: extent 10.x on one axis
        for (a, sa), (b, sb), radius in cases:
            g = BoardGraph('t', [part(a, 'top', size=sa), part(b, 'top', size=sb)])
            con = copy.deepcopy(self.constraints)
            con.constraints = [Constraint('group', Enforcement.HARD, (b,), {'anchor': a, 'radius_mm': radius})]
            blk = Block('x', [a, b])
            sizes, cap = outline_sizes(g, con, blk)
            span = max(max(sa), max(sb)) + 1.0
            self.assertGreaterEqual(cap, span)
            self.assertIn((cap, cap), [(w, h) for w, h, _, _ in sizes])
            w, h = cap, cap
            gg = copy.deepcopy(g)
            gg.component(a).pos, gg.component(b).pos = (w / 2 - 2, h / 2), (w / 2 + 2, h / 2)
            out = legalize(gg, w, h, fixed={}, keepouts=[], grid_mm=.25, group_edges=[(a, b, radius)])
            self.assertEqual(overlap_pairs(out), [])


def rec(u, a, area, debt, seed=0, **kw):
    return dict(utilisation=u, aspect=a, area=area, port_debt_mm=debt, seed=seed, width=1, height=1, **kw)


def run_result(r, objective, repeat=0, status='ok'):
    return dict(r, stage='native', repeat=repeat, status=status, objective=objective,
                instances=[dict(instance='b', dir='/x/r%d' % repeat)])


class SynthNativeTest(unittest.TestCase):
    def test_stratified_order_is_round_robin(self):
        from pnr.hier.synth_native import stratified_order
        recs = [rec(.25, 1, 40, 1), rec(.55, 1, 20, 3), rec(.55, 1, 20, 1), rec(.55, 1, 20, 2),
                rec(.45, 1.5, 25, 0), rec(.45, 1.5, 25, 5)]
        order = stratified_order(recs)
        self.assertEqual([(k, r['utilisation'], r['port_debt_mm']) for k, r in order],
                         [(0, .55, 1), (0, .45, 0), (0, .25, 1), (1, .55, 2), (1, .45, 5), (2, .55, 3)])

    def test_aggregate_is_conservative(self):
        from pnr.hier.synth_native import aggregate, is_complete, rank_key
        base = rec(.55, 1, 20, 1)
        a = run_result(base, [0, 1, 0, 8, 0, 0], 0)
        b = run_result(base, [2, 0, 1, 4, 1, 0], 1)
        agg = aggregate([a, b])
        self.assertEqual(agg['objective'], [2, 1, 1, 6.0, 1, 0])
        self.assertFalse(is_complete(agg))
        self.assertEqual(agg['representative_repeat'], 0)  # (0 unconnected, 0 violations) ranks first
        self.assertEqual(agg['instances'][0]['dir'], '/x/r0')
        self.assertEqual([x['dirs'] for x in agg['runs']], [['/x/r0'], ['/x/r1']])
        c = run_result(base, [0, 0, 0, 3, 0, 0], 1)
        self.assertTrue(is_complete(aggregate([run_result(base, [0, 0, 0, 5, 0, 0]), c])))
        self.assertFalse(is_complete(aggregate([run_result(base, [0, 0, 0, 5, 0, 1]), c])))
        failed = aggregate([run_result(base, None, 0, 'failed'), c])
        self.assertEqual((failed['status'], failed['objective']), ('failed', None))
        self.assertIs(aggregate([c]), c)  # R=1 keeps the single result unchanged
        # Full rank key: subwidth breaks ties after the guards, then pairs, debt, area.
        keys = sorted([run_result(base, [0, 0, 0, 5, 0, 0]), run_result(base, [0, 0, 0, 3, 1, 0]),
                       run_result(base, [0, 0, 0, 3, 0, 0]), run_result(base, [1, 0, 0, 0, 0, 0])], key=rank_key)
        self.assertEqual([k['objective'] for k in keys],
                         [[0, 0, 0, 3, 0, 0], [0, 0, 0, 3, 1, 0], [0, 0, 0, 5, 0, 0], [1, 0, 0, 0, 0, 0]])

    def test_schedule_repeats_and_round_stop(self):
        from pnr.hier.synth_native import schedule, stratified_order
        recs = [rec(u, 1, 10 / u, s, seed=s) for u in (.25, .45, .55) for s in range(3)]
        order = stratified_order(recs)
        calls, lock = [], threading.Lock()

        def run(r, k):
            with lock:
                calls.append((r['utilisation'], r['seed'], k))
            return run_result(r, [0, 0, 0, r['seed'], 0, 0], k)

        seen = []
        out = schedule(order, run, repeats=2, parallel=3, enough=1, report=lambda r, agg, ev: seen.append(agg))
        # The first complete layout arrives in round 0; round 0 (3 strata) is finished, round 1 never starts.
        self.assertEqual(sorted(calls), sorted((u, 0, k) for u in (.25, .45, .55) for k in (0, 1)))
        self.assertEqual(len(out), 3)
        self.assertEqual(sum(a is not None for a in seen), 3)
        self.assertTrue(all(a['repeats'] == 2 for a in out))
        calls.clear()
        self.assertEqual(len(schedule(order, run, repeats=1, parallel=2)), 9)
        self.assertEqual(len(calls), 9)
        self.assertEqual(schedule(order, run, enough=0), [])

    def test_schedule_turns_worker_crash_into_failed_layout(self):
        from pnr.hier.synth_native import schedule

        def run(r, k):
            raise RuntimeError('boom')
        out = schedule([(0, rec(.55, 1, 20, 0))], run, repeats=2)
        self.assertEqual(out[0]['status'], 'failed')

    def test_write_library_is_atomic_and_ranked(self):
        from pnr.hier.synth_native import write_library
        base = rec(.55, 1, 20, 1)
        evaluated = [run_result(base, [0, 0, 0, 9, 0, 0]), run_result(base, [2, 0, 0, 0, 0, 0]),
                     run_result(base, [0, 0, 0, 4, 0, 0]), run_result(base, None, status='failed')]
        with tempfile.TemporaryDirectory() as d:
            write_library(d, ['b'], 'tid', evaluated, repeats=2, outline_cap_mm=None)
            self.assertEqual(os.listdir(d), ['library.json'])
            doc = json.loads((Path(d) / 'library.json').read_text())
        self.assertEqual([r['objective'][3] for r in doc['ranked']], [4, 9, 0])
        self.assertEqual([r['missing'] for r in doc['ranked']], [0, 0, 2])
        self.assertEqual(doc['counts'], dict(evaluated=4, ok=3, complete=2))
        self.assertEqual(doc['repeats'], 2)


@unittest.skipUnless(HAVE_MINI, 'Mini inputs not present')
class NativeTagTest(MiniCase):
    def test_repeats_use_distinct_native_dirs(self):
        import pnr.hier.native_block as nb
        from pnr.hier.synth import local_key
        from pnr.hier.synth_native import _native
        from pnr.hier.blocks import sub_board
        b = self.blocks['group:board.status_led']
        sub = sub_board(self.graph, self.constraints, self.rules, b, 6, 6)[0]
        r = dict(seed=1, width=6, height=6,
                 layout={local_key(b, c.address): [3, 3, c.rot, c.side] for c in sub.components})
        dirs, original = [], nb.evaluate
        nb.evaluate = lambda round_dir, *a, **k: dirs.append(str(round_dir)) or dict(status='ok', objective=[0] * 6)
        try:
            with tempfile.TemporaryDirectory() as out:
                results = [_native(INPUTS, CONSTRAINTS, r, [b.name], out, 1, 1, out, k) for k in range(3)]
        finally:
            nb.evaluate = original
        self.assertEqual(len(set(dirs)), 3)
        self.assertTrue(dirs[0].endswith('-s1-6x6'))
        self.assertTrue(dirs[2].endswith('-s1-6x6-r2'))
        self.assertEqual([x['repeat'] for x in results], [0, 1, 2])


def library_doc(name, ranked):
    return dict(blocks=[name], template_id=name, ranked=ranked)


class LoadLibraryTest(unittest.TestCase):
    def test_tier_rank_order_and_snapshot(self):
        from pnr.hier.top import load_library, snapshot_library
        ranked = [dict(objective=o, missing=o[5] + o[0], port_debt_mm=1, area=1, tag=t)
                  for t, o in (('b', [0, 0, 0, 7, 0, 0]), ('a', [0, 0, 0, 2, 0, 0]),
                               ('c', [0, 0, 0, 1, 0, 1]), ('d', [0, 0, 0, 9, 0, 0]))]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'lib'
            (root / 't1').mkdir(parents=True)
            (root / 't1' / 'library.json').write_text(json.dumps(library_doc('blk', ranked)))
            (root / 't2').mkdir()
            (root / 't2' / 'library.json').write_text(json.dumps(library_doc('other', ranked[:1])))
            lib = load_library(root)
            self.assertEqual([r['tag'] for r in lib['blk']], ['a', 'b', 'd'])  # min-missing tier, rank order
            snap = snapshot_library(root, Path(d) / 'snap.json')
            self.assertEqual(load_library(snap), lib)
            single = Path(d) / 'one.json'
            single.write_text(json.dumps(library_doc('blk', ranked)))
            self.assertEqual(load_library(single), {'blk': lib['blk']})
            listed = Path(d) / 'list.json'
            listed.write_text(json.dumps([library_doc('blk', ranked)]))
            self.assertEqual(load_library(listed), {'blk': lib['blk']})
            bad = Path(d) / 'bad.json'
            bad.write_text(json.dumps({'x': 1}))
            with self.assertRaises(ValueError):
                load_library(bad)
            bad.write_text(json.dumps({'x': [{'layout': {}}]}))    # list values without layout recs
            with self.assertRaises(ValueError):
                load_library(bad)

    def test_halving_snapshot_is_the_one_snapshot_format(self):
        from pnr.hier.top import load_library, snapshot_library
        from pnr.mc.halving import _read_library, _snapshot_library
        ranked = [dict(objective=[0, 0, 0, s, 0, 0], missing=0, port_debt_mm=1, area=1, tag=t,
                       layout={'@': [1.0, 2.0, 90, 'top']}, width=4.0, height=5.0)
                  for t, s in (('b', 7), ('a', 2), ('d', 9))]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'lib'
            (root / 't1').mkdir(parents=True)
            doc = dict(blocks=['blkA', 'blkB'], template_id='t1', ranked=ranked)
            (root / 't1' / 'library.json').write_text(json.dumps(doc))
            lib = load_library(root)
            hsnap, tsnap = Path(d) / 'halving.json', Path(d) / 'top.json'
            info = _snapshot_library(root, hsnap)
            snapshot_library(root, tsnap)
            self.assertEqual(hsnap.read_bytes(), tsnap.read_bytes())
            self.assertEqual(load_library(hsnap), lib)
            self.assertEqual(load_library(hsnap), _read_library(hsnap)[0])
            self.assertEqual(info['blocks'], 2)
            self.assertEqual(snapshot_library(hsnap, Path(d) / 'again.json').read_bytes(), hsnap.read_bytes())
            # a snapshot is frozen: its order is kept verbatim (it is what halving workers draw from)
            frozen = {'blkA': list(reversed(lib['blkA'])), 'empty': []}
            fz = Path(d) / 'frozen.json'
            fz.write_text(json.dumps(frozen))
            self.assertEqual(load_library(fz), {'blkA': frozen['blkA']})
            self.assertEqual([r['tag'] for r in load_library(fz)['blkA']], ['d', 'b', 'a'])
            empty = Path(d) / 'empty.json'
            empty.write_text('{}')
            self.assertEqual(load_library(empty), {})

    @unittest.skipUnless(any(BLOCK_LIBRARY.glob('*/library.json')), 'no Mini block library')
    def test_real_mini_library_round_trips_through_halving_snapshot(self):
        from pnr.hier.top import load_library
        from pnr.mc.halving import _snapshot_library
        lib = load_library(BLOCK_LIBRARY)
        self.assertTrue(lib)
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / 'library.snapshot.json'
            _snapshot_library(BLOCK_LIBRARY, snap)
            self.assertEqual(load_library(snap), lib)

    def test_draw_is_rank_geometric_and_seeded(self):
        from pnr.hier.top import draw_layout
        tier = list('abcd')
        rng = random.Random(7)
        n = 40000
        counts = [0] * 4
        for _ in range(n):
            counts[draw_layout(tier, rng)[0]] += 1
        expected = [0.5 ** i / sum(0.5 ** j for j in range(4)) for i in range(4)]
        for c, p in zip(counts, expected):
            self.assertAlmostEqual(c / n, p, delta=0.01)
        self.assertEqual([draw_layout(tier, random.Random(s)) for s in range(20)],
                         [draw_layout(tier, random.Random(s)) for s in range(20)])
        self.assertEqual(draw_layout(['only'], random.Random(1)), (0, 'only'))
        counts = [0] * 4
        for _ in range(n):
            counts[draw_layout(tier, rng, ratio=1.0)[0]] += 1
        self.assertTrue(all(abs(c / n - .25) < .01 for c in counts))


@unittest.skipUnless(HAVE_MINI, 'Mini inputs not present')
class MacroPlacementTest(MiniCase):
    def test_macro_graph_places_with_bottom_parts_off_blocks(self):
        """Stage-A layouts for every Mini block, collapse, few-iteration macro placement."""
        from pnr.hier.macro import collapse
        from pnr.hier.synth import instance_board
        from pnr.hier.synth_native import _place, outline_sizes
        from pnr.place.initial_pool import preserve_source_locks, _prepared_source
        from pnr.place.placer import place
        constraints = preserve_source_locks(self.graph, self.constraints)
        source = _prepared_source(self.graph, constraints, self.rules)
        layouts = []
        for name in sorted(self.blocks):
            b = self.blocks[name]
            sizes = sorted(outline_sizes(self.graph, self.constraints, b)[0], key=lambda s: s[0] * s[1])
            got = None
            for size in sizes[:4]:
                got = _place((str(INPUTS), str(CONSTRAINTS), name, size, 0, 300))
                if got['legal']:
                    break
            self.assertTrue(got['legal'], name)
            sub = instance_board(source, constraints, self.rules, b, got['layout'], got['width'], got['height'])[0]
            layouts.append((b, sub, got['width'], got['height']))
        mgraph, mcon, mrules, plan = collapse(source, constraints, self.rules, layouts)
        placed, report = place(mgraph, mcon, seed=1, iters=50, orient=True, spread=1.0, channel_rules=mrules)
        self.assertTrue(report.legal, report.summary())
        self.assertEqual(overlap_pairs(placed), [])
        macros = [c for c in placed.components if c.ref in plan.macros]
        bottom = [c for c in placed.components if c.side == 'bottom']
        self.assertTrue(bottom)
        for c in bottom:
            for m in macros:
                self.assertFalse(any(s == 'bottom' and r.overlaps(q) for s, r in placement_rects(m)
                                     for _, q in placement_rects(c)), (c.ref, m.ref))
        flat = plan.expand(placed, source)
        self.assertEqual(sorted(c.ref for c in flat.components), sorted(c.ref for c in source.components))


if __name__ == '__main__':
    unittest.main()
