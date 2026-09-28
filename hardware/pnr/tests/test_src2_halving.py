"""Dry-run checks for pnr.mc.halving: stage bookkeeping, ranking, controls, resume.

Workers are replaced by fakes and the process pools by thread pools, so no
placement, routing or native subprocess ever runs.
"""
import concurrent.futures as cf
import contextlib
import io
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pnr.mc import halving

N0, K1, K2, K3, CONTROL = 16, 8, 2, 1, 2


def _idx(rid):
    return int(rid[1:])


class FakeRun:
    """Deterministic fake workers; counts calls per stage."""

    def __init__(self, controls=()):
        self.calls = {'place': [], 'screen': [], 'native': [], 'deep': []}
        self.lock = threading.Lock()
        self.controls = list(controls)
        self.place_args = []
        self.assemble = []

    def starts(self, inputs, constraints_path, n, seed):
        return [dict(id='p%03d' % i, kind=('latin-global', 'stratified-global')[i % 2], seed=seed * 1000 + i)
                for i in range(n)]

    def place_one(self, args):
        assert len(args) == len(halving.PLACE_JOB), args
        inputs, constraints_path, start, cand_dir, iters, library, assemble_blocks = args
        with self.lock:
            self.calls['place'].append(start['id'])
            self.place_args.append(args)
        i = _idx(start['id'])
        rec = dict(id=start['id'], kind='hier-seed' if library else start['kind'], start_kind=start['kind'],
                   seed=start['seed'], stage='place', seconds=0.0)
        if library:
            rec['library_blocks'] = sorted(json.loads(Path(library).read_text()))
        if i % 5 == 4:
            rec.update(status='illegal')
        else:
            rec.update(status='legal', proxy_score=float((i * 7) % 16), cheap_score=float(i))
        return rec

    def screen_one(self, args):
        inputs, constraints_path, cand_dir, route_iters, live = args
        rid = Path(cand_dir).name
        with self.lock:
            self.calls['screen'].append(rid)
        missing = 0 if rid == (self.controls[:1] or [None])[0] else 99 if rid in self.controls else 1 + _idx(rid) % 7
        return dict(id=rid, stage='screen', status='ok', objective=[missing, missing, 0, 1.0],
                    missing_connections=missing, seconds=0.0)

    def native_one(self, inputs, constraints_path, cand, stage, seconds, workers, env, repo, assemble=False):
        with self.lock:
            self.calls[stage].append(cand.name)
            self.assemble.append(assemble)
        i = _idx(cand.name)
        # the good control: one DRC violation but few opens; everyone else: clean DRC, many opens
        obj = [1, 0, 0, 0, 0, 5 + i] if cand.name in self.controls[:1] else [0, 0, 0, 0, 0, 50 + i]
        return dict(id=cand.name, stage=stage, status='ok', objective=obj, opens=obj[5], violations=obj[0],
                    assembled=bool(assemble), seconds=0.0)


class HalvingDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.out = self.root / 'out'
        (self.root / 'inputs').mkdir()
        (self.root / 'c.yaml').write_text('{}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def argv(self, *extra, seed=0):
        return ['--out', str(self.out), '--inputs', str(self.root / 'inputs'), '--constraints',
                str(self.root / 'c.yaml'), '--repo', str(self.root), '--seed', str(seed), '--n0', str(N0),
                '--k1', str(K1), '--k2', str(K2), '--k3', str(K3), '--control', str(CONTROL), '--procs', '3',
                *extra]

    def run_main(self, fake, *extra, seed=0):
        with mock.patch.object(halving, '_starts', fake.starts), \
             mock.patch.object(halving, '_place_one', fake.place_one), \
             mock.patch.object(halving, '_screen_one', fake.screen_one), \
             mock.patch.object(halving, '_native_one', fake.native_one), \
             mock.patch.object(halving, '_process_pool', lambda n: cf.ThreadPoolExecutor(n)), \
             contextlib.redirect_stdout(io.StringIO()):
            halving.main(self.argv(*extra, seed=seed))
        return json.loads((self.out / 'status.json').read_text())

    def records(self, stage):
        lines = (self.out / 'dataset.jsonl').read_text().splitlines()
        return [r for r in map(json.loads, lines) if r['stage'] == stage]

    def expected_selection(self, seed=0):
        legal = [i for i in range(N0) if i % 5 != 4]
        ranked = sorted(legal, key=lambda i: ((i * 7) % 16, float(i)))
        survivors = ['p%03d' % i for i in ranked[:K1]]
        rest = ['p%03d' % i for i in ranked[K1:]]
        return survivors, rest, halving._sample(rest, CONTROL, seed, 'control')

    def test_screen_controls_ranking_and_resume(self):
        survivors, rest, controls = self.expected_selection()
        self.assertEqual(len(controls), CONTROL)
        self.assertTrue(set(controls) <= set(rest))
        self.assertEqual(controls, self.expected_selection()[2])     # seeded => repeatable
        fake = FakeRun(controls)
        status = self.run_main(fake)
        # stage 0: every start once, tuple arity == PLACE_JOB
        self.assertEqual(sorted(fake.calls['place']), ['p%03d' % i for i in range(N0)])
        self.assertTrue(all(args[5] is None and args[6] is False for args in fake.place_args))
        self.assertEqual(status['stages']['place'], dict(status['stages']['place'], n=N0, legal=13))
        # stage 1: top-K1 by proxy + controls, flagged
        screen = {r['id']: r for r in self.records('screen')}
        self.assertEqual(sorted(screen), sorted(survivors + controls))
        self.assertEqual(sorted(i for i, r in screen.items() if r['control']), sorted(controls))
        st = status['stages']['screen']
        self.assertEqual(st['controls'], controls)
        self.assertEqual((st['spearman_proxy_vs_missing_n'], st['spearman_proxy_vs_missing_all_n']),
                         (K1, K1 + CONTROL))
        self.assertIsInstance(st['spearman_proxy_vs_missing'], float)
        self.assertIsInstance(st['spearman_proxy_vs_missing_all'], float)
        self.assertEqual(st['missing']['control'], [0, 99])
        # stage 2: the good control competes on its screen result; the bad one never promoted
        best_survivor = min(survivors, key=lambda r: (1 + _idx(r) % 7, survivors.index(r)))
        native = {r['id']: r for r in self.records('native')}
        self.assertEqual(sorted(native), sorted([controls[0], best_survivor]))
        self.assertEqual(st['promoted_controls'], [controls[0]])
        self.assertTrue(native[controls[0]]['control'])
        self.assertFalse(native[best_survivor]['control'])
        self.assertTrue(all(r['promoted_from'] == 'screen' and r['sampled'] is False for r in native.values()))
        self.assertEqual(fake.assemble, [False] * len(fake.assemble))
        ns = status['stages']['native']
        self.assertEqual((ns['spearman_screen_vs_opens'], ns['spearman_screen_vs_opens_n']), (None, K2))
        self.assertEqual((ns['spearman_proxy_vs_opens'], ns['spearman_proxy_vs_opens_n']), (None, K2))
        # stage 3: DRC violations dominate unconnected in the native rank key (the old
        # unconnected-first key would have deepened the control with 7 opens + 1 violation)
        expect = best_survivor
        self.assertLess(native[controls[0]]['opens'], native[expect]['opens'])
        self.assertEqual([r['id'] for r in self.records('deep')], [expect])
        self.assertEqual(status['stages']['deep']['best'], expect)
        # full resume: nothing re-evaluated, bookkeeping identical
        counts = {k: len(v) for k, v in fake.calls.items()}
        status2 = self.run_main(fake)
        self.assertEqual({k: len(v) for k, v in fake.calls.items()}, counts)
        self.assertEqual(status2['stages']['screen']['controls'], controls)
        self.assertEqual(status2['stages']['deep']['best'], expect)
        # partial resume: drop one control's screen record and the deep stage; a stale control flag
        # on a survivor record must not leak into this run's bookkeeping
        lines = [json.loads(l) for l in (self.out / 'dataset.jsonl').read_text().splitlines()]
        kept = [r for r in lines if r['stage'] != 'deep' and not (r['stage'] == 'screen' and r['id'] == controls[1])]
        for r in kept:
            if r['stage'] == 'screen' and r['id'] == best_survivor:
                r['control'] = True
        (self.out / 'dataset.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in kept))
        status3 = self.run_main(fake)
        self.assertEqual(len(fake.calls['place']), counts['place'])
        self.assertEqual(fake.calls['screen'][counts['screen']:], [controls[1]])
        self.assertEqual(len(fake.calls['native']), counts['native'])
        self.assertEqual(fake.calls['deep'][counts['deep']:], [expect])
        self.assertTrue([r for r in self.records('screen') if r['id'] == controls[1]][-1]['control'])
        self.assertEqual(status3['stages']['screen']['promoted_controls'], [controls[0]])
        self.assertEqual(status3['stages']['deep']['best'], expect)

    def test_promote_from_place_samples_legal_without_screen(self):
        fake = FakeRun()
        status = self.run_main(fake, '--promote-from', 'place', '--assemble', seed=3)
        legal = ['p%03d' % i for i in range(N0) if i % 5 != 4]
        sampled = halving._sample(legal, K2, 3, 'promote-place')
        self.assertEqual(fake.calls['screen'], [])
        self.assertEqual(self.records('screen'), [])
        self.assertEqual(status['stages']['screen'], dict(skipped=True, promote_from='place', sampled=sampled))
        native = self.records('native')
        self.assertEqual(sorted(r['id'] for r in native), sorted(sampled))
        self.assertTrue(all(r['sampled'] and r['promoted_from'] == 'place' and not r['control'] for r in native))
        self.assertTrue(all(fake.assemble))                             # --assemble passed explicitly
        self.assertTrue(all(r['assembled'] for r in native))
        self.assertEqual(status['stages']['native']['spearman_screen_vs_opens_n'], 0)
        self.assertEqual(status['stages']['native']['promoted'], sampled)
        # stop-after screen with place promotion: nothing native
        other = FakeRun()
        self.out = self.root / 'out2'
        self.run_main(other, '--promote-from', 'place', '--stop-after', 'screen')
        self.assertEqual(other.calls['native'], [])

    def test_library_snapshot_is_frozen_for_workers_and_resume(self):
        import pnr.hier.top as top
        libdir = self.root / 'lib'
        libdir.mkdir()
        tier = [dict(width=4.0, height=5.0, layout={'@': (3.0, 1.875, 0.0, 'top')}, missing=0)]
        fake = FakeRun()
        with mock.patch.object(top, 'load_library', lambda root: {'blockA': tier, 'blockB': tier}):
            status = self.run_main(fake, '--library', str(libdir), '--stop-after', 'place')
        snap = self.out / halving.LIBRARY_SNAPSHOT
        data = json.loads(snap.read_text())
        self.assertEqual(data['blockA'][0]['layout']['@'], [3.0, 1.875, 0.0, 'top'])
        self.assertEqual(status['library']['path'], str(snap))
        self.assertFalse(status['library']['reused'])
        self.assertTrue(all(args[5] == str(snap) for args in fake.place_args))
        place = self.records('place')
        self.assertTrue(all(r['kind'] == 'hier-seed' and r['start_kind'] in ('latin-global', 'stratified-global')
                            for r in place))
        self.assertTrue(all(r['library_blocks'] == ['blockA', 'blockB'] for r in place))
        # library changes on disk: a resume of the same --out keeps the frozen snapshot
        with mock.patch.object(top, 'load_library', lambda root: {'changed': []}):
            status2 = self.run_main(FakeRun(), '--library', str(libdir), '--stop-after', 'place')
        self.assertTrue(status2['library']['reused'])
        self.assertEqual(status2['library']['sha256'], status['library']['sha256'])
        self.assertEqual(sorted(json.loads(snap.read_text())), ['blockA', 'blockB'])


class HalvingUnitTest(unittest.TestCase):
    def test_native_rank_key_order(self):
        key = halving._rank_key('native')
        # objective = [violations, blocked, reference, subwidth, unqualified_pairs, unconnected]
        recs = [dict(id='a', objective=[0, 0, 0, 0, 0, 1]),
                dict(id='b', objective=[1, 0, 0, 0, 0, 0]),
                dict(id='c', objective=[0, 0, 0, 0, 1, 0]),
                dict(id='d', objective=[0, 0, 1, 0, 0, 0]),
                dict(id='e', objective=[0, 1, 0, 0, 0, 0]),
                dict(id='f', objective=[0, 0, 0, 1, 0, 0]),
                dict(id='g', objective=[0, 0, 0, 0, 0, 0]),
                dict(id='h', objective=[0, 0, 0, 0, 0, 0]),
                dict(id='i', status='failed')]
        order = [r['id'] for r in sorted(reversed(recs), key=key)]
        self.assertEqual(order, ['g', 'h', 'f', 'e', 'd', 'c', 'a', 'b', 'i'])
        self.assertEqual(key(recs[1]), (1, 0, 0, 0, 0, 0, 'b'))
        self.assertEqual(halving._rank_key('deep')(recs[8]), (math.inf,) * 6 + ('i',))

    def test_agreement_needs_eight_pairs(self):
        self.assertEqual(halving._agreement([(i, i) for i in range(7)]), (None, 7))
        rho, n = halving._agreement([(i, i) for i in range(8)])
        self.assertEqual(n, 8)
        self.assertAlmostEqual(rho, 1.0)
        self.assertEqual(halving._agreement([]), (None, 0))

    def test_jsonable(self):
        value = {'a': (1, 2.5, Path('/x')), 'b': {3, 1}, 'c': [None, True, 'z']}
        self.assertEqual(halving._jsonable(value), {'a': [1, 2.5, '/x'], 'b': [1, 3], 'c': [None, True, 'z']})
        with self.assertRaises(TypeError):
            halving._jsonable({'f': object()})

    def test_place_one_records_hier_seed_and_snapshot_digest(self):
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / 'library.snapshot.json'
            snap.write_text(json.dumps({'blockA': []}))
            start = dict(id='p007', kind='latin-global', seed=7)
            missing = str(Path(d) / 'no-inputs')
            rec = halving._place_one((missing, missing, start, str(Path(d) / 'cand'), 1, str(snap), False))
            self.assertEqual((rec['kind'], rec['start_kind'], rec['status']), ('hier-seed', 'latin-global', 'failed'))
            self.assertEqual(len(rec['library_sha256']), 64)
            flat = halving._place_one((missing, missing, start, str(Path(d) / 'cand'), 1, None, False))
            self.assertEqual((flat['kind'], flat['start_kind']), ('latin-global', 'latin-global'))
            self.assertNotIn('library_sha256', flat)


class NativeOneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.tmp.name)
        self.inputs = root / 'inputs'
        self.inputs.mkdir()
        for name in ('source.kicad_pcb', 'source.kicad_pro', 'rules.json', 'fp-lib-table'):
            (self.inputs / name).write_text('x')
        self.cand = root / 'cand' / 'p001'
        self.cand.mkdir(parents=True)
        (self.cand / 'placed.json').write_text('{}')
        self.cmds = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, cmd, cwd, env, stdout, stderr):
        self.cmds.append(cmd)
        round_dir = Path(cmd[3])
        (round_dir / 'evaluation.json').write_text(json.dumps(dict(objective=[0, 1, 2, 3, 4, 5], qualified=False)))
        return mock.Mock(returncode=0)

    def call(self, assemble):
        with mock.patch.object(halving.subprocess, 'run', self.fake_run):
            return halving._native_one(self.inputs, self.root / 'c.yaml', self.cand, 'native', 10, 2, {},
                                       self.root, assemble=assemble)

    def test_assemble_missing_blocks_fails_without_running(self):
        rec = self.call(True)
        self.assertEqual((rec['status'], rec['assembled']), ('failed', False))
        self.assertIn('blocks.json is missing', rec['error'])
        self.assertEqual(self.cmds, [])
        self.assertIn('loadavg_start', rec)
        self.assertIn('loadavg_end', rec)
        (self.cand / 'blocks.json').write_text('[]')
        self.assertIn('lists no routed block boards', self.call(True)['error'])
        (self.cand / 'blocks.json').write_text(json.dumps([str(self.root / 'nope.kicad_pcb')]))
        self.assertIn('block board(s) are missing', self.call(True)['error'])
        self.assertEqual(self.cmds, [])

    def test_assemble_flag_passed_explicitly(self):
        board = self.root / 'block.kicad_pcb'
        board.write_text('x')
        (self.cand / 'blocks.json').write_text(json.dumps([str(board)]))
        rec = self.call(True)
        self.assertEqual(self.cmds[-1][-2:], ['--assemble', str(self.cand / 'blocks.json')])
        self.assertEqual((rec['status'], rec['assembled'], rec['assembled_blocks']), ('ok', True, 1))
        self.assertEqual((rec['violations'], rec['blocked_entries'], rec['reference_failures'], rec['subwidth'],
                          rec['unqualified_pairs'], rec['opens']), (0, 1, 2, 3, 4, 5))
        self.assertEqual(len(rec['loadavg_start']), 3)
        self.assertEqual(len(rec['loadavg_end']), 3)
        # blocks.json present but --assemble not set: never assembled implicitly
        rec = self.call(False)
        self.assertNotIn('--assemble', self.cmds[-1])
        self.assertFalse(rec['assembled'])


if __name__ == '__main__':
    unittest.main()
