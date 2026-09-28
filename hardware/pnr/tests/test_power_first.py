"""Power-first placement (PNR_POWER_FIRST=1): derivation, staged objective, legalizer, ranking, routed metrics.

Run with the PnR runtime: ``PYTHONPATH=hardware/pnr python -m unittest tests/test_power_first.py``.
Real sub-board fixtures live in testdata/power_topology (regenerate with
tests/power_topology_golden.py, never by hand).
"""
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import torch

from pnr.constraints import BoardSpec, CompiledConstraints, Constraint, Enforcement, NetClass
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.power_topology import derive, minimum_cycle_basis

HERE = Path(__file__).resolve()
PNR = HERE.parents[1]
DATA = PNR / 'testdata' / 'power_topology'
sys.path.insert(0, str(HERE.parent))
from power_topology_golden import compile_fixture  # noqa: E402


def fixture(name):
    return compile_fixture(json.loads((DATA / f'{name}.json').read_text()))


def plain(x):
    return json.loads(json.dumps(x, sort_keys=True))


# ------------------------------------------------------------------ synthetic buck

def comp(ref, pads, at=(15, 15), size=(3, 3)):
    return Component(ref, 'fp', at, 0, 'top', size, size, address='board.blk.' + ref.lower(),
                     pads=[Pad(n, net, off, (.5, .5)) for n, net, off in pads])


def synthetic(terminals=(), extra=()):
    """VIN -> Q1 -> SW -> Q2 -> GND buck with L1/COUT, a controller U1 and a pull-up R1 on RAIL."""
    q1 = comp('Q1', [('1', 'SW', (-1, -1)), ('2', 'SW', (0, -1)), ('3', 'GH', (1, -1)),
                     ('4', 'VIN', (-1, 1)), ('5', 'VIN', (0, 1)), ('6', 'VIN', (1, 1))])
    q2 = comp('Q2', [('1', 'GND', (-1, -1)), ('2', 'GND', (0, -1)), ('3', 'GL', (1, -1)),
                     ('4', 'SW', (-1, 1)), ('5', 'SW', (0, 1)), ('6', 'SW', (1, 1))])
    l1 = comp('L1', [('1', 'SW', (-1, 0)), ('2', 'VOUT', (1, 0))])
    cin = comp('CIN', [('1', 'VIN', (-.5, 0)), ('2', 'GND', (.5, 0))])
    cout = comp('COUT', [('1', 'VOUT', (-.5, 0)), ('2', 'GND', (.5, 0))])
    u1 = comp('U1', [('1', 'GH', (-1, 1)), ('2', 'GL', (0, 1)), ('3', 'FB', (1, 1)), ('4', 'EN', (1, 0)),
                     ('5', 'VIN', (-1, 0)), ('6', 'SW', (-1, -1)), ('7', 'GND', (0, -1)),
                     ('8', 'GND', (1, -1)), ('9', 'GND', (0, 0))])
    r1 = comp('R1', [('1', 'RAIL', (-.5, 0)), ('2', 'EN', (.5, 0))])
    r2 = comp('R2', [('1', 'FB', (-.5, 0)), ('2', 'VOUT', (.5, 0))])
    comps = [q1, q2, l1, cin, cout, u1, r1, r2] + list(extra)
    pins = {}
    for c in comps:
        for p in c.pads:
            pins.setdefault(p.net, []).append((c.ref, p.name))
    graph = BoardGraph('buck', comps, [Net(n, i + 1, v) for i, (n, v) in enumerate(sorted(pins.items()))],
                       BoardOutline(30, 30))
    rules = dict(fab=dict(track_width_mm=.2), block_ports=['VIN', 'VOUT', 'RAIL', 'GND'],
                 net_classes=[dict(name='gnd', nets=['GND'], plane_layer='In1.Cu')],
                 electrical_nets=dict(VIN=dict(rms_current_a=3, peak_current_a=3, outer_width_mm=1.0),
                                      SW=dict(rms_current_a=3, peak_current_a=9, outer_width_mm=1.0),
                                      VOUT=dict(rms_current_a=3, peak_current_a=3, outer_width_mm=1.0),
                                      RAIL=dict(rms_current_a=2, peak_current_a=2, outer_width_mm=.5)),
                 current_intents=[dict(scope='terminal', ref=r, net=n, pads=list(p), rms_current_a=a, peak_current_a=a)
                                  for r, n, p, a in terminals])
    cons = CompiledConstraints(board=BoardSpec(30, 30), net_classes=[NetClass('gnd', nets=('GND',), plane_layer='In1.Cu')])
    return graph, cons, rules


SENSE = (('U1', 'VIN', ('5',), .1), ('U1', 'SW', ('6',), .2), ('U1', 'GND', ('7', '8'), .1))


class Derivation(unittest.TestCase):
    def test_goldens(self):
        for name in ('converter', 'pd'):
            with self.subTest(name):
                g, c, r = fixture(name)
                self.assertEqual(plain(derive(g, c, r)), json.loads((DATA / f'{name}.golden.json').read_text()))

    def test_converter_and_pd_roles(self):
        g, c, r = fixture('converter')
        roles = derive(g, c, r)
        self.assertEqual(sorted(roles['tier1']), sorted(['Q1', 'Q2', 'L2', 'U5', 'R13'] + ['C%d' % i for i in range(13, 25)]))
        self.assertEqual(roles['mixed'], ['U5'])
        self.assertFalse([ref for ref, t in roles['tier'].items() if t == 2])
        hot = {tuple(sorted(l['labels'])): round(l['weight'], 3) for l in roles['loops'] if l['hot']}
        self.assertEqual(sorted(hot.values()), [2.344, 7.5])
        self.assertTrue(any(d['member'] == 'C15' and d['radius_mm'] == 5.0 and d['anchor_current_a']['negotiated-hv'] == .1
                            for d in roles['hard_group_diagnostic']))
        g, c, r = fixture('pd')
        roles = derive(g, c, r)
        self.assertEqual(sorted(roles['tier1']), sorted(['U19', 'D1'] + ['C%d' % i for i in (53, 54, 55, 56, 57, 60, 61, 62, 63, 64)]))
        self.assertEqual(sorted(roles['controllers']), ['U18', 'U19'])

    def test_pullup_on_rail_is_not_promoted(self):
        roles = derive(*synthetic(SENSE))
        self.assertEqual(roles['tier']['R1'], 3)
        self.assertNotIn('R1', roles['carrying'])
        self.assertIn('RAIL', roles['power_nets'])

    def test_small_terminal_budget_does_not_carry(self):
        roles = derive(*synthetic(SENSE))
        self.assertNotIn('VIN', roles['carrying'].get('U1', {}))
        self.assertNotIn('SW', roles['carrying'].get('U1', {}))
        taps = {e['name']: e['stage'] for e in roles['elements'] if e['kind'] == 'tap'}
        self.assertEqual(taps['tap:U1.5'], 2)

    def test_plane_pads_with_and_without_intents(self):
        roles = derive(*synthetic(SENSE))
        self.assertEqual(roles['carrying']['U1'], {'GND': ['9']})
        roles = derive(*synthetic(SENSE[:2]))
        self.assertEqual(roles['carrying']['U1'], {'GND': ['7', '8', '9']})

    def test_controller_with_drive_and_sense_only_is_tier_two(self):
        roles = derive(*synthetic(SENSE))
        self.assertEqual(roles['tier']['U1'], 2)
        self.assertEqual(roles['controllers'], ['U1'])
        self.assertEqual({roles['tier'][r] for r in ('Q1', 'Q2', 'L1', 'CIN', 'COUT')}, {1})
        buck = [l for l in roles['loops'] if sorted(l['labels']) == ['CIN', 'Q1', 'Q2']]
        self.assertTrue(buck and buck[0]['hot'] and buck[0]['weight'] == roles['W'])

    def test_top_level_loops_stay_inside_blocks(self):
        g, c, r = synthetic(SENSE)
        base = derive(g, c, r)
        same = derive(g, c, r, block_of={x.ref: 'blk' for x in g.components})
        self.assertEqual(plain(same['loops']), plain(base['loops']))
        split = derive(g, c, r, block_of={x.ref: ('in' if x.ref == 'CIN' else 'stage') for x in g.components})
        hot = [l for l in split['loops'] if l['hot']]
        self.assertTrue(hot)
        self.assertFalse(any('CIN' in l['labels'] for l in split['loops']))

    def test_minimum_cycle_basis(self):
        # Two 4-cycles sharing an edge plus a pendant: basis = the two short cycles.
        adj = {'a': 'bd', 'b': 'ace', 'c': 'bf', 'd': 'ae', 'e': 'bdf', 'f': 'ceg', 'g': 'f'}
        basis = minimum_cycle_basis({k: list(v) for k, v in adj.items()}, lambda v: v)
        self.assertEqual(sorted(sorted(c) for c in basis), [['a', 'b', 'd', 'e'], ['b', 'c', 'e', 'f']])

    def test_renaming_refs_changes_only_labels(self):
        g, c, r = fixture('converter')
        base = plain(derive(g, c, r))
        doc = json.loads((DATA / 'converter.json').read_text())
        refs = [x['ref'] for x in doc['graph']['components']]
        new = {ref: 'Zq%03dZ' % i for i, ref in enumerate(reversed(refs))}
        text = json.dumps(doc)
        for ref in sorted(refs, key=len, reverse=True):
            text = re.sub(r'(?<=")%s(?=")' % re.escape(ref), new[ref], text)
        renamed = plain(derive(*compile_fixture(json.loads(text))))
        back = json.dumps(renamed, sort_keys=True)
        for ref, token in new.items():
            back = back.replace(token, ref)
        self.assertEqual(json.loads(back), base)

    def test_hash_seed_independent(self):
        code = ('import json,sys;sys.path.insert(0,%r);from power_topology_golden import compile_fixture;'
                'from pnr.power_topology import derive;'
                'print(json.dumps(derive(*compile_fixture(json.load(open(%r)))),sort_keys=True))'
                % (str(HERE.parent), str(DATA / 'converter.json')))
        outs = [subprocess.run([sys.executable, '-c', code], env=dict(os.environ, PYTHONHASHSEED=seed),
                               capture_output=True, text=True, check=True).stdout for seed in ('1', '2')]
        self.assertEqual(outs[0], outs[1])

    def test_no_reference_designator_literals(self):
        ref_literal = re.compile(r"""['"](?:C|R|L|Q|U|D|J)\d{1,3}['"]""")
        for path in (PNR / 'pnr/power_topology.py', PNR / 'pnr/place/power_first.py', PNR / 'pnr/hier/power_quality.py'):
            self.assertEqual(ref_literal.findall(path.read_text()), [], path.name)

    def test_unavailable_without_currents(self):
        from pnr.power_topology import PowerTopologyUnavailable
        g, c, r = synthetic()
        with self.assertRaises(PowerTopologyUnavailable):
            derive(g, c, dict(r, electrical_nets={}))


# ------------------------------------------------------------------ objective

class Objective(unittest.TestCase):
    def test_gradcheck_gap_and_guard(self):
        from pnr.place.power_first import torch_gap, torch_guard
        torch.manual_seed(0)
        xy = torch.rand(2, 6, 2, dtype=torch.float64, requires_grad=True)
        a = torch.tensor([[0, 1, 2], [3, 0, 0]])
        am = torch.tensor([[True, True, True], [True, False, False]])
        b = torch.tensor([[3, 4, 0], [4, 5, 1]])
        bm = torch.tensor([[True, True, False], [True, True, True]])
        self.assertTrue(torch.autograd.gradcheck(lambda x: torch_gap(x, a, am, b, bm), (xy,)))
        j = torch.tensor([9.0, 10.4, 12.0], dtype=torch.float64, requires_grad=True)
        self.assertTrue(torch.autograd.gradcheck(lambda v: torch_guard(v, 10.0, .03), (j,)))
        # The guard is ~0 below the budget and grows at Omega per unit above it.
        g = torch_guard(torch.tensor([9.0, 10.3 + 5.0], dtype=torch.float64), 10.0, .03)
        self.assertLess(float(g[0]), 1e-5)
        self.assertAlmostEqual(float(g[1]), 10 * 5.0, delta=.05)

    def test_torch_and_numpy_costs_agree(self):
        from pnr.place.power_first import Compiled, _TorchCost
        g, c, r = fixture('converter')
        roles = derive(g, c, r)
        comp = Compiled(g, roles)
        pp = np.random.default_rng(3).uniform(0, 30, (2, comp.npads, 2))
        ref = comp.numpy_j(pp)
        cost = _TorchCost(comp, 1.0)
        for s in (1, 2, 3):
            got = cost(s, torch.tensor(pp, dtype=torch.float64)).numpy()
            np.testing.assert_allclose(got, ref[s - 1], rtol=1e-6)

    def test_cost_inspect_terms_are_appended(self):
        from pnr.place import cost_inspect
        self.assertEqual(cost_inspect.TERMS[:cost_inspect.BASE_TERMS],
                         ('wirelength', 'spread_overlap', 'outline', 'plane_area', 'plane_separation',
                          'edge_alignment', 'group_radius', 'keepout', 'capacitor_loop'))
        self.assertEqual(cost_inspect.TERMS[cost_inspect.BASE_TERMS:], ('power_trunk', 'power_loop', 'power_tap', 'lex_guard'))
        self.assertEqual(len(cost_inspect.TERMS), len(cost_inspect.LABELS))
        self.assertEqual(len(cost_inspect.TERMS), len(cost_inspect.UNITS))
        g, c, r = fixture('converter')
        with self.assertRaises(ValueError):
            cost_inspect.Objective(g, c, pf_state={'w_ov': 1.0})
        report = cost_inspect.Objective(g, c).report()
        self.assertEqual(len(next(iter(report['components'].values()))['terms']), cost_inspect.BASE_TERMS)
        report = cost_inspect.Objective(g, c, roles=derive(g, c, r)).report()
        self.assertEqual(len(next(iter(report['components'].values()))['terms']), len(cost_inspect.TERMS))
        self.assertAlmostEqual(report['board_total'], sum(v['total'] for v in report['components'].values()), places=6)


# ------------------------------------------------------------------ placement

class Placement(unittest.TestCase):
    def place(self, name, flag, **kw):
        from pnr.place import place
        g, c, r = fixture(name)
        env = {'PNR_POWER_FIRST': '1'} if flag else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not flag:
                os.environ.pop('PNR_POWER_FIRST', None)
            return place(g, c, **dict(dict(seed=0, iters=600, orient=True, channel_rules=r), **kw)), (g, c, r)

    def test_power_first_legal_and_compact(self):
        from pnr.place.power_first import placement_quality
        (placed, report), (g, c, r) = self.place('converter', True)
        self.assertTrue(report.legal, report.summary())
        roles = derive(g, c, r)
        q = placement_quality(placed, roles)
        (base, base_report), _ = self.place('converter', False)
        q0 = placement_quality(base, roles)
        self.assertLess(q['J1'], q0['J1'])
        self.assertLess(q['power_mst_mm'], q0['power_mst_mm'])
        self.assertIn('stars', placed.power_first)

    def test_fallback_without_channel_rules_is_default(self):
        def outcome(flag):
            try:
                (placed, _), _ = self.place('pd', flag, channel_rules=None, iters=80)
                return placed.to_json()
            except Exception as error:
                return repr(error)
        with self.assertLogs('pnr.place.power_first', 'WARNING'):
            a = outcome(True)
        self.assertEqual(a, outcome(False))

    def test_capture_replay_with_flag(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {'PNR_COST_CAPTURE_DIR': d}):
                (placed, report), _ = self.place('converter', True, iters=90)
            docs = [json.loads(p.read_text()) for p in Path(d).glob('*.json')]
            self.assertFalse(list(Path(d).glob('failed-*')))
            glob_ = [x for x in docs if x['kind'] == 'global-objective']
            self.assertTrue(glob_)
            for x in glob_:
                self.assertLess(abs(x['report']['replay_error']), .005)
                self.assertIn('roles', x)
                self.assertIn('power_first.py', x['runtime_sources'])
                keys = {t['key'] for comp in x['report']['components'].values() for t in comp['terms']}
                self.assertIn('lex_guard', keys)

    def test_staged_placement_hash_seed_independent(self):
        code = ('import json,os,sys;sys.path.insert(0,%r);os.environ["PNR_POWER_FIRST"]="1";'
                'from power_topology_golden import compile_fixture;from pnr.place import place;'
                'g,c,r=compile_fixture(json.load(open(%r)));p,_=place(g,c,seed=1,iters=60,channel_rules=r);'
                'print(p.to_json())' % (str(HERE.parent), str(DATA / 'pd.json')))
        outs = [subprocess.run([sys.executable, '-c', code], env=dict(os.environ, PYTHONHASHSEED=seed),
                               capture_output=True, text=True, check=True).stdout for seed in ('3', '4')]
        self.assertEqual(outs[0], outs[1])

    def test_retry_when_a_hot_loop_grows(self):
        # J1 barely moves when legalization opens one hot loop (the trunks dominate
        # it), so the per-loop ratio alone must trigger the runner-up retry.
        import pnr.place.power_first as pf
        with mock.patch.object(pf, 'loop_ratio', side_effect=[2.0, 1.0]):
            (placed, report), _ = self.place('pd', True, iters=60)
        info = placed.power_first
        self.assertTrue(info['retried'])
        self.assertIn('hot loop', info['retry_reason'])
        self.assertEqual(len(info['attempts']), 2)
        self.assertEqual(len({a['start'] for a in info['attempts']}), 2)
        self.assertTrue(all(len(v) == 2 for a in info['attempts'] for v in a['loops'].values()))
        with mock.patch.object(pf, 'loop_ratio', return_value=1.0), \
                mock.patch.object(pf, 'RETRY_RATIO', math.inf):
            (placed, _), _ = self.place('pd', True, iters=60)
        self.assertFalse(placed.power_first['retried'])
        self.assertIsNone(placed.power_first['retry_reason'])


class Legalization(unittest.TestCase):
    def test_pour_exempts_only_trunks_both_parts_carry(self):
        from pnr.place.channels import ChannelModel
        from pnr.place.power_first import PourChannels
        g, c, r = synthetic(SENSE)
        roles = derive(g, c, r)
        pour = PourChannels(ChannelModel(g, r), roles)
        self.assertEqual(pour.poured('Q1', 'CIN'), {'VIN'})
        self.assertEqual(pour.poured('Q1', 'Q2'), {'SW'})
        self.assertEqual(pour.poured('Q1', 'R1'), set())
        # CIN 0.4 mm below Q1's SW/SW/GH pad row: the switch node is not on CIN,
        # so its corridor stays; only VIN (on both) pours across the gap.
        q1, cin = g.component('Q1'), g.component('CIN')
        q1.pos, cin.pos = (15.0, 15.0), (15.0, 13.1)
        at = (np.array([cin.pos[0]]), np.array([cin.pos[1]]))
        got = float(pour.penalty(cin, [q1], *at)[0])
        base = pour.base
        expect, facing = 0.0, set()
        for _, gap, overlap, _, nets in base.interactions(cin, q1, *at):
            if bool(np.all((gap >= 0) & (overlap > 0))):
                facing |= {k for k, v in nets.items() if bool(np.all(v))}
            required = base.active_demand({k: v for k, v in nets.items() if k != 'VIN'})
            expect += float(np.where((gap >= 0) & (overlap > 0), np.maximum(required - gap, 0) ** 2, 0)[0])
        self.assertIn('SW', facing)
        self.assertGreater(expect, 0.0)
        self.assertAlmostEqual(got, expect, places=9)
        # The former rule (trunks of either part) also dropped SW and hid the blockage.
        with mock.patch.object(PourChannels, 'poured', lambda self, a, b: self.trunk.get(a, set()) | self.trunk.get(b, set())):
            self.assertLess(float(pour.penalty(cin, [q1], *at)[0]), got)

    def test_loop_ratio_floors_small_loops(self):
        from pnr.place.power_first import loop_ratio
        self.assertAlmostEqual(loop_ratio({'L': 8.0}, {'L': 16.0}, {'L': 3}, .45), 2.0)
        # A softmin gap can be <= 0 for a tight loop; the floor keeps the ratio finite.
        self.assertAlmostEqual(loop_ratio({'L': -1.0}, {'L': 1.35}, {'L': 3}, .45), 1.0)
        self.assertAlmostEqual(loop_ratio({'A': 4.0, 'B': 10.0}, {'A': 4.0, 'B': 13.0}, {'A': 2, 'B': 3}, .45), 1.3)
        self.assertEqual(loop_ratio({}, {}, {}, .45), 0.0)

    def test_better_attempt(self):
        from pnr.place.power_first import EPS, better_attempt
        at = lambda hot, j1, legal=True, placed=True: dict(placed=object() if placed else None, legal=legal,
                                                           hot_legal=hot, j_legal=j1)
        self.assertTrue(better_attempt(at(200, 900), at(100, 800, legal=False)))
        self.assertTrue(better_attempt(at(200, 900, legal=False), at(0, 0, placed=False)))
        self.assertFalse(better_attempt(at(0, 0, placed=False), at(0, 0, placed=False)))
        # Outside the EPS[0] band the hot loop decides, even against a lower J1 ...
        self.assertTrue(better_attempt(at(97, 906), at(165, 872)))
        # ... inside it the hot loops tie and J1 decides.
        self.assertTrue(better_attempt(at(170, 854), at(170 * (1 - EPS[0] / 2), 988)))
        self.assertFalse(better_attempt(at(100, 800), at(100, 800)))


# ------------------------------------------------------------------ ranking

class Ranking(unittest.TestCase):
    def rec(self, opens, hot, band, cross, area=100.):
        return dict(objective=[0, 0, 0, 1.0, 0, opens], hot_loops_open=hot, area=area, port_debt_mm=1.0,
                    power_quality=dict(q_place=band * 5.0, q_band=band, crossings=cross))

    def test_rank_key_default_unchanged(self):
        from pnr.hier.synth_native import rank_key
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PNR_POWER_FIRST', None)
            self.assertEqual(rank_key(self.rec(3, 2, 20, 1)), (3, 0, 0, 0, 1.0, 0, 1.0, 100.))

    def test_rank_key_power_first(self):
        from pnr.hier.synth_native import aggregate, open_band, rank_key, stratified_order
        with mock.patch.dict(os.environ, {'PNR_POWER_FIRST': '1'}):
            a, b, c = self.rec(3, 1, 22, 0), self.rec(3, 0, 25, 4), self.rec(3, None, 20, 0)
            self.assertEqual(sorted([a, b, c], key=rank_key), [b, a, c])
            self.assertEqual(rank_key(self.rec(7, 0, 20, 0), band=3)[0], 2)
            runs = [dict(a, repeat=0, status='ok', hot_loops_open=0, instances=[]),
                    dict(a, repeat=1, status='ok', hot_loops_open=2, instances=[])]
            self.assertEqual(aggregate(runs)['hot_loops_open'], 2)
            agg = [dict(runs=[dict(objective=[0, 0, 0, 0, 0, v]) for v in vals]) for vals in ((4, 6), (10, 10, 13))]
            self.assertEqual(open_band(agg), 2)
            recs = [dict(self.rec(0, 0, 0, 0, area=a_), utilisation=.35, aspect=1.0,
                         power_quality=dict(q_place=q)) for a_, q in ((90., 130.), (100., 100.), (110., 104.))]
            order = [r['power_quality']['q_place'] for _, r in stratified_order(recs)]
            self.assertEqual(order, [100., 104., 130.])


# ------------------------------------------------------------------ routed metrics

class Routed(unittest.TestCase):
    def dump(self):
        pad = lambda ref, name, net, x, y, smd=True: dict(ref=ref, name=name, net=net, x=x, y=y, smd=smd,
                                                         bbox=[x - .3, y - .3, x + .3, y + .3],
                                                         layers=['F.Cu'] if smd else ['F.Cu', 'In1.Cu', 'B.Cu'],
                                                         zone_hits=[])
        return dict(copper_layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'],
                    pads=[pad('A1', '1', 'P', 0, 0), pad('B1', '1', 'P', 10, 0, smd=False), pad('C1', '1', 'P', 20, 5),
                          pad('A1', '2', 'G', 0, 2), pad('B1', '2', 'G', 10, 2)],
                    tracks=[dict(net='P', layer='F.Cu', x1=0, y1=0, x2=5, y2=0, w=.5, length=5, arc=False, pads1=[['A1', '1']], pads2=[]),
                            dict(net='P', layer='B.Cu', x1=5, y1=0, x2=10, y2=0, w=.5, length=5, arc=False, pads1=[], pads2=[]),
                            dict(net='G', layer='F.Cu', x1=0, y1=2, x2=1, y2=2, w=.3, length=1, arc=False, pads1=[], pads2=[]),
                            dict(net='G', layer='F.Cu', x1=10, y1=2, x2=9, y2=2, w=.3, length=1, arc=False, pads1=[], pads2=[])],
                    vias=[dict(net='P', x=5, y=0, d=.6, top='F.Cu', bottom='B.Cu', pads=[], zone_hits=[]),
                          dict(net='G', x=1, y=2, d=.6, top='F.Cu', bottom='B.Cu', pads=[], zone_hits=[[0, 'In1.Cu']]),
                          dict(net='G', x=9, y=2, d=.6, top='F.Cu', bottom='B.Cu', pads=[], zone_hits=[[0, 'In1.Cu']])],
                    zones=[dict(index=0, net='G', layer='In1.Cu', filled_area_mm2=100.)])

    def test_copper_graph(self):
        from pnr.hier.power_quality import Copper, drc_unconnected
        d = self.dump()
        p = Copper(d, 'P', layer_factor={'B.Cu': 1.0}, via_cost=1.6)
        self.assertEqual(p.islands(), 2)                          # C1 is not reached
        length, cost, vias = p.path([('A1', '1')], [('B1', '1')])
        self.assertAlmostEqual(length, 10.0, places=6)
        self.assertEqual(vias, 1)
        self.assertAlmostEqual(cost, 11.6, places=6)
        self.assertIsNone(p.path([('A1', '1')], [('C1', '1')]))
        g = Copper(d, 'G')
        self.assertEqual(g.islands(), 1)                          # joined through the plane zone
        drc = dict(unconnected_items=[dict(items=[dict(description='Pad 1 [P] of B1 on F.Cu'),
                                                  dict(description='Pad 1 [P] of C1 on F.Cu')])])
        self.assertEqual(drc_unconnected(drc), {'P': 1})

    def test_self_check_mismatch_opens_every_hot_link(self):
        from pnr.hier.power_quality import analyse
        roles = dict(power_nets=['P'], return_nets=['G'], weight={'P': 5.0}, w_ret=1.0, series=None,
                     carrying={'A1': {'P': ['1'], 'G': ['2']}, 'B1': {'P': ['1'], 'G': ['2']}},
                     classes=[dict(members=['A1'], label='A1'), dict(members=['B1'], label='B1')],
                     loops=[dict(classes=[0, 1], labels=['A1', 'B1'], nets=['P', 'G'], hot=True)])
        ok = analyse(self.dump(), roles, {}, dict(unconnected_items=[dict(items=[dict(description='Pad 1 [P] of C1')])]))
        self.assertTrue(ok['valid'])
        self.assertEqual(ok['hot_loops_open'], 0)
        bad = analyse(self.dump(), roles, {}, dict(unconnected_items=[]))
        self.assertFalse(bad['valid'])
        self.assertEqual(bad['hot_loops_open'], bad['hot_loop_links'])


if __name__ == '__main__':
    unittest.main()
