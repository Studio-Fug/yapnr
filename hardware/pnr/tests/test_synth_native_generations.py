"""synth_native feedback rounds (--rounds/--generations) with fake placement and native workers.

Round 0 is imported from a trials.jsonl built from the two real nb5-noshove
converter parents in testdata/feedback (their feedback is read from the fixture
rounds). Children are real PULL/RAND moves on the real converter sub-board;
only the native evaluation and stage-A placement are faked.
"""
import concurrent.futures as cf
import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
DATA = HERE.parents[1] / 'testdata' / 'feedback'
INPUTS = HERE.parents[4] / 'inputs10'
CONSTRAINTS = HERE.parents[2] / 'splanc_dev' / 'mini-constraints.yaml'
HAVE_MINI = (INPUTS / 'graph.json').exists() and CONSTRAINTS.exists()
ENV = {'PNR_FEEDBACK': '1', 'PNR_POWER_FIRST': '1'}


def parents(with_fb=False):
    from pnr.feedback.blocks import block_key
    from pnr.feedback.signals import read_round
    from pnr.hier.blocks import Block
    out = []
    for name in ('converter-parent.json', 'converter-parent-33x35.75.json'):
        rec = json.loads((DATA / name).read_text())
        for inst in rec['instances']:
            inst['dir'] = str(DATA / inst['dir'])
            if with_fb:
                inst['fb'] = read_round(inst['dir'], key=block_key(Block('board.converter', [], prefix='board.converter')))
        out.append(rec)
    return out


class FakeWorkers:
    """_native: a PULL child fixes its target (parent opens - ``improve``, feedback without it); a RAND
    child keeps its parent's result, anything else 5 opens. _place: the roomy parent's layout, shifted per seed."""

    def __init__(self, improve=1):
        self.native, self.place = [], []
        self.lock = threading.Lock()
        self.improve = improve
        self.by_tag = {}

    def fb_for(self, rec):
        from pnr.feedback.blocks import tag_of
        parent = self.by_tag.get((rec.get('parent') or {}).get('tag'))
        if parent is None:
            return None, 5
        fb = copy.deepcopy(parent['instances'][0]['fb'])
        opens = parent['objective'][5]
        if rec.get('arm') == 'pull' and self.improve:
            fb['conns'] = [c for c in fb['conns'] if c['id'] != rec['op_detail']['conn']]
            opens = max(0, opens - self.improve)
        return fb, opens

    def native_fn(self, inputs, constraints_path, rec, names, out, seconds, workers, repo, repeat=0):
        from pnr.feedback.blocks import tag_of
        with self.lock:
            self.native.append(tag_of(rec))
        fb, opens = self.fb_for(rec)
        o = [0, 0, 0, 30, 0, opens]
        inst = dict(instance=names[0], status='ok', objective=o, dir=str(Path(out) / 'native' / tag_of(rec)))
        if fb is not None:
            inst['fb'] = fb
        res = dict(rec, stage='native', repeat=repeat, status='ok', objective=o, instances=[inst], hot_loops_open=0)
        with self.lock:
            self.by_tag[tag_of(rec)] = res
        return res

    def place_fn(self, args):
        inputs, constraints_path, name, size, seed, iters, *extra = args
        with self.lock:
            self.place.append((size[:2], seed, bool(extra and extra[0])))
        base = parents()[1]
        layout = {k: [v[0] + 0.01 * (seed % 50), v[1], v[2], v[3]] for k, v in base['layout'].items()}
        rec = dict(block=name, width=size[0], height=size[1], utilisation=size[2], aspect=size[3], seed=seed,
                   area=size[0] * size[1], legal=True, layout=layout, port_debt_mm=100.0,
                   power_quality=dict(base['power_quality']))
        if extra and extra[0]:
            rec['pair_weights_n'] = len(extra[0])
        return rec


@unittest.skipUnless(HAVE_MINI, 'Mini inputs10 not present')
class SynthGenerationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / 'out'
        self.trials = self.root / 'import.jsonl'
        self.write_trials()

    def write_trials(self, code=True, nudge=False):
        """Import file: the two parents' stage-A and native records plus one unevaluated stage-A layout.

        ``code``: the native records carry this tree's code key (as records made by
        this code do); False leaves the code unknown (fixture rounds record no tree).
        ``nudge``: the first parent's native layout has one part moved 0.1 mm (an
        accepted shove nudge), so it differs from its stage-A layout."""
        from pnr.feedback.signals import code_key
        ps = parents()
        extra = dict(ps[1], stage='place', seed=9)
        extra['layout'] = {k: [v[0] + 0.25, v[1], v[2], v[3]] for k, v in ps[1]['layout'].items()}
        places = [dict(p, stage='place') for p in ps] + [extra]
        for rec in places:
            rec.pop('instances', None)
            rec.pop('objective', None)
        natives = [dict(p) for p in ps]
        for rec in natives:
            if code:
                rec['code'] = code_key(router='plain')
        if nudge:
            layout = {k: list(v) for k, v in natives[0]['layout'].items()}
            layout['inductor'][0] += 0.1
            natives[0]['layout'] = layout
        self.trials.write_text(''.join(json.dumps(r) + '\n' for r in places + natives))

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, fake, *extra, env=ENV):
        import pnr.hier.synth_native as sn
        from pnr.feedback.blocks import tag_of
        for p in parents(with_fb=True):
            fake.by_tag[tag_of(p)] = p
        argv = ['--out', str(self.out), '--inputs', str(INPUTS), '--constraints', str(CONSTRAINTS),
                '--repo', str(self.root), '--block', 'board.converter', '--seconds', '600', '--parallel', '2',
                '--import-trials', str(self.trials), *extra]
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(sn, '_native', fake.native_fn), mock.patch.object(sn, '_place', fake.place_fn), \
             mock.patch.object(cf, 'ProcessPoolExecutor', cf.ThreadPoolExecutor), \
             contextlib.redirect_stdout(io.StringIO()):
            sn.main(argv)
        d = self.out / 'd5be6b6d0e99'
        return json.loads((d / 'library.json').read_text()), [json.loads(l) for l in
                                                                (d / 'trials.jsonl').read_text().splitlines()]

    def test_rounds_schedule_tags_resume(self):
        from pnr.feedback.blocks import tag_of
        fake = FakeWorkers()
        lib, trials = self.run_main(fake, '--generations', '2', '--gen-parents', '2', '--gen-children', '2',
                                    '--gen-rand', '1', '--gen-fresh', '2', '--enough', '99')
        native = [r for r in trials if r['stage'] == 'native']
        imported = {tag_of(p) for p in parents()}
        self.assertFalse(imported & set(fake.native))                         # imports never re-run
        tags = [tag_of(r) for r in native]
        self.assertEqual(len(tags), len(set(tags)))                            # unique tags (dirs)
        self.assertEqual(sorted(tags), sorted(fake.native))
        g = lib['generations']
        self.assertEqual([x['gen'] for x in g], [1, 2])
        self.assertEqual(len(g[0]['parents']), 2)                               # ceil(2 / 1)
        self.assertEqual(len(g[1]['parents']), 1)                               # ceil(2 / 2)
        self.assertEqual(sorted(g[0]['children']), ['fresh', 'prior', 'pull', 'rand'])
        for r in native:
            self.assertIn(r['arm'], ('pull', 'rand', 'prior', 'fresh'))
            self.assertTrue(r['tag'].startswith('board_converter-s%d-' % r['seed']))
            self.assertIn('-g%d%s' % (r['gen'], dict(pull='c', rand='r', prior='p', fresh='f')[r['arm']]), r['tag'])
            self.assertIn('router_key', r)
            if r['arm'] in ('pull', 'rand'):
                self.assertEqual(len(r['moved_mm']), 1)
                self.assertNotIn('ic', r['moved_mm'])
                self.assertIn(r['parent']['tag'], imported | set(tags))
                self.assertEqual(r['lineage_depth'], 1 + (r['parent']['tag'] not in imported))
                self.assertIn('q_band', r['power_quality'])
        self.assertTrue(all(isinstance(r['op_detail']['conn'], str) for r in native if r['arm'] == 'pull'))
        prior = [p for p in fake.place if p[2]]
        self.assertEqual(len(prior), 2)                                          # one weighted slot per round
        self.assertTrue((self.out / 'd5be6b6d0e99' / 'feedback-table.g2.json').exists())
        ranked = {tag_of(r) for r in lib['ranked']}
        self.assertTrue(imported <= ranked and set(tags) <= ranked)
        self.assertEqual(lib['counts']['evaluated'], len(native) + 2)
        # resume: nothing re-evaluated or re-placed, no duplicate records
        calls, places, n = len(fake.native), len(fake.place), len(trials)
        lib2, trials2 = self.run_main(fake, '--generations', '2', '--gen-parents', '2', '--gen-children', '2',
                                      '--gen-rand', '1', '--gen-fresh', '2', '--enough', '99')
        self.assertEqual((len(fake.native), len(fake.place), len(trials2)), (calls, places, n))
        self.assertEqual(lib2['generations'], g)

    def test_enough_and_plateau_stop(self):
        # the 2-open parent's PULL child closes both opens: round 2 stops before planning anything
        lib, _ = self.run_main(FakeWorkers(improve=2), '--generations', '3', '--gen-parents', '1',
                               '--gen-fresh', '0', '--gen-rand', '0', '--enough', '1')
        self.assertEqual([(x['gen'], x.get('stop')) for x in lib['generations']], [(1, None), (2, 'enough')])
        self.assertEqual(lib['generations'][0]['complete_after'], 1)
        self.assertEqual(lib['counts']['complete'], 1)
        # every child is worse: the pool best of round 1 is still the best after it -> plateau
        self.out = self.root / 'out2'
        lib, _ = self.run_main(FakeWorkers(improve=-1), '--generations', '4', '--gen-parents', '2',
                               '--gen-fresh', '0', '--gen-rand', '0', '--gen-plateau', '1', '--enough', '99')
        self.assertEqual([x.get('stop') for x in lib['generations']], [None, 'plateau'])

    def test_feedback_flag_required_and_rounds_off_is_the_old_path(self):
        import pnr.hier.synth_native as sn
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.dict(os.environ, {'PNR_FEEDBACK': '0'}):
            sn.main(['--out', str(self.out), '--inputs', str(INPUTS), '--constraints', str(CONSTRAINTS),
                     '--repo', str(self.root), '--generations', '1'])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.dict(os.environ, ENV):
            sn.main(['--out', str(self.out), '--inputs', str(INPUTS), '--constraints', str(CONSTRAINTS),
                     '--repo', str(self.root), '--import-trials', str(self.trials)])
        fake = FakeWorkers()
        argv = ['--out', str(self.out), '--inputs', str(INPUTS), '--constraints', str(CONSTRAINTS),
                '--repo', str(self.root), '--block', 'board.converter', '--seeds', '1', '--max-native', '2']
        with mock.patch.dict(os.environ, ENV), mock.patch.object(sn, '_native', fake.native_fn), \
             mock.patch.object(sn, '_place', fake.place_fn), \
             mock.patch.object(sn, '_feedback_template', mock.Mock(side_effect=AssertionError('feedback path'))), \
             mock.patch.object(cf, 'ProcessPoolExecutor', cf.ThreadPoolExecutor), \
             contextlib.redirect_stdout(io.StringIO()):
            sn.main(argv)
        trials = [json.loads(l) for l in (self.out / 'd5be6b6d0e99' / 'trials.jsonl').read_text().splitlines()]
        self.assertEqual(len([r for r in trials if r['stage'] == 'native']), 2)
        self.assertFalse(any('gen' in r or 'tag_suffix' in r for r in trials))
        self.assertTrue(all(len(p) == 3 and not p[2] for p in fake.place))    # 6-tuple jobs, no weights

    def test_import_router_mismatch_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(FakeWorkers(), '--generations', '1', env=dict(ENV, PNR_SHOVE='1'))
        self.assertIn("router 'plain' != this run 'shove'", err.getvalue())

    def test_import_selects_only_imported_templates(self):
        import pnr.hier.synth_native as sn
        # without --block: only the imported template runs (no stage A/B of the other nine)
        fake = FakeWorkers()
        argv = ['--out', str(self.out), '--inputs', str(INPUTS), '--constraints', str(CONSTRAINTS),
                '--repo', str(self.root), '--seconds', '600', '--import-trials', str(self.trials),
                '--generations', '1', '--gen-parents', '1', '--gen-fresh', '0', '--gen-rand', '0']
        for p in parents(with_fb=True):
            from pnr.feedback.blocks import tag_of
            fake.by_tag[tag_of(p)] = p
        with mock.patch.dict(os.environ, ENV), mock.patch.object(sn, '_native', fake.native_fn), \
             mock.patch.object(sn, '_place', fake.place_fn), \
             mock.patch.object(cf, 'ProcessPoolExecutor', cf.ThreadPoolExecutor), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            sn.main(argv)
        self.assertEqual(sorted(p.name for p in self.out.iterdir() if p.is_dir()), ['d5be6b6d0e99'])
        self.assertEqual(fake.place, [])
        self.assertIn('templates without imported native records are not run', out.getvalue())
        # --block naming a template without imports is an error, before anything runs
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(FakeWorkers(), '--generations', '1', '--block', 'board.pd')
        self.assertIn('without imported native records', err.getvalue())

    def test_code_mismatch_refused_or_rebased(self):
        from pnr.feedback.blocks import layout_key, tag_of
        self.write_trials(code=False, nudge=True)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(FakeWorkers(), '--generations', '1')
        self.assertIn('code unknown', err.getvalue())
        imported = {tag_of(p) for p in parents()}
        fake = FakeWorkers()
        args = ('--generations', '1', '--gen-parents', '2', '--gen-children', '1', '--gen-rand', '1',
                '--gen-fresh', '0', '--enough', '99', '--import-code-mismatch', 'rebase')
        lib, trials = self.run_main(fake, *args)
        native = {tag_of(r): r for r in trials if r['stage'] == 'native'}
        rebase = {t: r for t, r in native.items() if r['arm'] == 'rebase'}
        self.assertEqual(len(rebase), 2)
        self.assertEqual({r['parent']['tag'] for r in rebase.values()}, imported)
        self.assertTrue(all('-g0b-' in t and r['gen'] == 0 for t, r in rebase.items()))
        self.assertFalse(imported & set(fake.native))                           # imports themselves never re-run
        # the rebase re-runs what the import's router was given: the stage-A layout, not the nudged one
        first = next(r for r in rebase.values() if r['parent']['tag'] == tag_of(parents()[0]))
        self.assertEqual(layout_key(first), layout_key(dict(parents()[0], layout=parents()[0]['layout'])))
        self.assertEqual(first['op_detail']['layout_source'], 'stage-A')
        # stale imports are never ranked; the rounds build on the same-code rebases
        ranked = {tag_of(r) for r in lib['ranked']}
        self.assertFalse(ranked & imported)
        self.assertTrue(set(rebase) <= ranked)
        self.assertEqual(lib['stale_imports'], 2)
        self.assertEqual(lib['imports']['stale_code'], 2)
        self.assertEqual(lib['rebase']['evaluated'], 2)
        self.assertEqual(sorted(lib['generations'][0]['parents']), sorted(rebase))
        for r in native.values():
            if r['arm'] in ('pull', 'rand'):
                self.assertIn(r['parent']['tag'], rebase)
        rand = [r for r in native.values() if r['arm'] == 'rand']
        self.assertEqual(len(rand), 1)
        self.assertEqual(native[rand[0]['matched']]['arm'], 'pull')              # matched to the k=0 sibling
        self.assertEqual(native[rand[0]['matched']]['k'], 0)
        self.assertEqual(native[rand[0]['matched']]['parent'], rand[0]['parent'])
        # resume: nothing re-run
        calls = len(fake.native)
        self.run_main(fake, *args)
        self.assertEqual(len(fake.native), calls)
        # --import-rebase 1: only the best stale import (2 opens) is re-evaluated
        self.out = self.root / 'out-one'
        fake = FakeWorkers()
        lib, trials = self.run_main(fake, '--generations', '1', '--gen-parents', '0', '--gen-fresh', '0',
                                    '--import-code-mismatch', 'rebase', '--import-rebase', '1')
        self.assertEqual(len(fake.native), 1)
        self.assertEqual(lib['rebase']['pairs'][0][0], tag_of(parents()[0]))

    def test_fresh_skips_evaluated_stage_a_and_prior_needs_weights(self):
        from pnr.feedback.blocks import tag_of
        from pnr.feedback.table import Table
        self.write_trials(nudge=True)            # parent 1's native layout differs from its stage-A layout
        fake = FakeWorkers()
        with mock.patch.object(Table, 'pair_weights', return_value={}):
            lib, trials = self.run_main(fake, '--generations', '1', '--gen-parents', '0', '--gen-fresh', '2',
                                        '--enough', '99')
        g = lib['generations'][0]
        self.assertEqual(g['children'], {'fresh': 1})
        self.assertEqual(g['prior_skipped'], 1)
        self.assertEqual(g['duplicates'], 0)
        fresh = [r for r in trials if r['stage'] == 'native' and r.get('arm') == 'fresh']
        self.assertEqual([r['seed'] for r in fresh], [9])                       # the unevaluated stage-A layout
        self.assertNotIn(tag_of(parents()[0]).split('-g')[0], {r['source'] for r in fresh})
        self.assertFalse(any(p[2] for p in fake.place))                          # no weighted placement


if __name__ == '__main__':
    unittest.main()
