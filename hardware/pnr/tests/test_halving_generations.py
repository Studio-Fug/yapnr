"""pnr.mc.halving --generations with fake native / prior workers on a real whole-board placement.

The seed run is synthetic but real-shaped: two legal stage-0 placements of the
Mini (hier/runs/h4 p000, p001, copied to testdata/feedback/board-placed.json and
reused shifted) with the 38 failed targets of h3 p010's native round
(testdata/feedback/board-targets.json). PULL/RAND moves, legality checks and
the driver's bookkeeping are real; only native routing and the prior's global
placement are faked.
"""
import concurrent.futures as cf
import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pnr.mc import halving

HERE = Path(__file__).resolve()
DATA = HERE.parents[1] / 'testdata' / 'feedback'
INPUTS = HERE.parents[4] / 'inputs10'
CONSTRAINTS = HERE.parents[2] / 'splanc_dev' / 'mini-constraints.yaml'
HAVE_MINI = (INPUTS / 'graph.json').exists() and CONSTRAINTS.exists()
ENV = {'PNR_FEEDBACK': '1', 'PNR_POWER_FIRST': '0', 'PNR_SHOVE': '0'}


def write_round(d, targets, opens, placed, router='plain', tree=None):
    d = Path(d)
    (d / 'electrical' / 'native-loop').mkdir(parents=True, exist_ok=True)
    if tree is not None:
        origin = d / 'electrical' / 'native-loop' / 'source-inputs'
        origin.mkdir(parents=True, exist_ok=True)
        (origin / 'origins.json').write_text(json.dumps([dict(
            original=str(Path(tree) / 'hardware/splanc_dev/elec/src/splanc_mini.ato'), snapshot='x', sha256='y')]))
    fb = dict(targets=targets, native_opens=opens, component_scores={}, routing_failure_scores={})
    if router == 'shove':
        fb['shove'] = dict(events=[])
    (d / 'feedback.json').write_text(json.dumps(fb))
    comps = json.loads(Path(placed).read_text())['components']
    (d / 'evaluated-placed.json').write_text(json.dumps(dict(components=[
        {k: c[k] for k in ('ref', 'address', 'pos', 'rot', 'side')} for c in comps])))
    (d / 'evaluation.json').write_text(json.dumps(dict(objective=[0, 0, 0, 150, 0, opens])))
    (d / 'electrical' / 'native-loop' / 'progress.json').write_text(json.dumps(dict(budgets=dict(seconds=900.0))))
    (d / 'rules.json').write_text(json.dumps(dict(fab_profile='jlc-pofv')))


class Fakes:
    """Native: a PULL child fixes its target (parent opens - 1, feedback without it), RAND and repeat keep
    the parent's opens + 1, a prior 40; rung 1 adds 70. Prior: copies the board placement."""

    def __init__(self, out, root):
        self.out, self.root = Path(out), Path(root)
        self.calls = {'rung1': [], 'native': [], 'deep': []}
        self.priors = []
        self.lock = threading.Lock()

    def records(self, stage):
        ds = self.out / 'dataset.jsonl'
        return {r['id']: r for r in map(json.loads, ds.read_text().splitlines()) if r.get('stage') == stage}

    def parent_round(self, pid):
        rec = self.records('native').get(pid) or {}
        return Path(rec.get('native_dir') or self.out / 'cand' / pid / 'native')

    def native_one(self, inputs, constraints_path, cand, stage, seconds, workers, env, repo, assemble=False):
        with self.lock:
            self.calls[stage].append(cand.name)
        gp = self.records('gen-place').get(cand.name, {})
        opens, targets = 40, []
        if gp.get('parent'):
            fb = json.loads((self.parent_round(gp['parent']) / 'feedback.json').read_text())
            opens, targets = fb['native_opens'], fb['targets']
            if gp['arm'] == 'pull':
                conn = gp['op_detail']['conn']
                targets = [t for t in targets if '|'.join(sorted([t['source'], t['target']])) != conn]
                opens -= 1
            else:
                opens += 1
        if stage == 'rung1':
            opens += 70
        write_round(cand / stage, targets, opens, cand / 'placed.json')
        o = [0, 0, 0, 150, 0, opens]
        return dict(id=cand.name, stage=stage, status='ok', objective=o, opens=opens, violations=0, seconds=0.0)

    def prior_one(self, args):
        *job, pairs = args
        start, cand_dir = job[2], Path(job[3])
        with self.lock:
            self.priors.append((start['id'], start.get('source_start'), len(pairs)))
        cand_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DATA / 'board-placed.json', cand_dir / 'placed.json')
        return dict(id=start['id'], kind=start.get('kind'), start_kind=start.get('kind'), seed=start['seed'],
                    stage='place', status='legal', seconds=0.0)


@unittest.skipUnless(HAVE_MINI, 'Mini inputs10 not present')
class HalvingGenerationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.seed_run = self.root / 'h9'
        self.write_seed()

    def write_seed(self, code='stamped', tree=None, ready=True):
        """Seed run h9: p000 (38 opens), p001 (45), p002 failed.

        ``code``: 'stamped' records carry this tree's code stamp (records made by this
        code do), None leaves it unknown; ``tree`` writes the native rounds'
        annotation-source origin (the tree that evaluated them). ``ready``: status.json
        says the native stage finished."""
        from pnr.feedback.signals import code_stamp
        if self.seed_run.exists():
            shutil.rmtree(self.seed_run)
        targets = json.loads((DATA / 'board-targets.json').read_text())['targets']
        recs = []
        for rid, tg, opens in (('p000', targets, 38), ('p001', targets[:20], 45)):
            cand = self.seed_run / 'cand' / rid
            cand.mkdir(parents=True)
            shutil.copy2(DATA / 'board-placed.json', cand / 'placed.json')
            write_round(cand / 'native', tg, opens, cand / 'placed.json', tree=tree)
            recs.append(dict(id=rid, stage='native', status='ok', objective=[0, 0, 0, 150, 0, opens], opens=opens))
            if code == 'stamped':
                recs[-1].update(code_stamp(router='plain'))
        recs.append(dict(id='p002', stage='native', status='failed'))
        (self.seed_run / 'dataset.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in recs))
        stages = dict(place={}, native={}) if ready else dict(place={}, screen={})    # still in its rung (like h4)
        (self.seed_run / 'status.json').write_text(json.dumps(dict(stages=stages)))

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, out, *extra, env=ENV):
        fakes = Fakes(out, self.root)
        argv = ['--out', str(out), '--inputs', str(INPUTS), '--constraints', str(CONSTRAINTS), '--repo', str(self.root),
                '--n0', '0', '--seed-from', str(self.seed_run), '--k3', '1', '--s2', '900', '--s3', '100',
                '--native-parallel', '2', '--procs', '2', *extra]
        with mock.patch.dict(os.environ, env), mock.patch.object(halving, '_native_one', fakes.native_one), \
             mock.patch.object(halving, '_gen_prior_one', fakes.prior_one), \
             mock.patch.object(halving, '_process_pool', lambda n: cf.ThreadPoolExecutor(n)), \
             contextlib.redirect_stdout(io.StringIO()):
            halving.main(argv)
        return fakes, json.loads((Path(out) / 'status.json').read_text())

    def test_generations_ids_pool_deep_and_resume(self):
        from pnr.graph import BoardGraph
        from pnr.feedback.toplevel import prepared, source_errors
        from pnr.mc.halving import _load
        out = self.root / 'out'
        fakes, status = self.run_main(out, '--generations', '2')
        native = fakes.records('native')
        self.assertEqual(status['seed_from']['native'], ['h9-p000', 'h9-p001'])       # failed p002 not imported
        self.assertTrue(native['h9-p000']['imported_from'].endswith('h9'))
        self.assertTrue((out / 'cand' / 'h9-p000' / 'placed.json').exists())
        self.assertNotIn('h9-p000', fakes.calls['native'])                             # imports never re-run
        g1, g2 = status['stages']['gen1'], status['stages']['gen2']
        self.assertEqual(g1['parents'], ['h9-p000', 'h9-p001'])                        # ceil(3/1), only 2 exist
        self.assertEqual(len(g2['parents']), 2)                                        # ceil(3/2)
        self.assertEqual(g1['children'], dict(pull=4, rand=1, repeat=1, prior=1))
        gen1 = sorted(i for i in fakes.calls['native'] if i.startswith('g1'))
        self.assertEqual(gen1, ['g1c00', 'g1c01', 'g1c02', 'g1c03', 'g1f0', 'g1p0', 'g1r0'])
        self.assertEqual(fakes.calls['rung1'], [])                                     # entry native by default
        self.assertEqual(g1['repeat_delta'], [1])
        self.assertEqual(g1['arms']['pull']['delta'], [-1, -1, -1, -1])
        self.assertEqual(fakes.priors[0][1], None)                                     # --n0 0: a prior seed
        self.assertGreater(fakes.priors[0][2], 0)                                      # pair weights passed
        for rid, rec in native.items():
            if rid.startswith('g'):
                self.assertIn(rec['arm'], ('pull', 'rand', 'prior', 'repeat'))
                self.assertEqual(rec['gen'], int(rid[1]))
        # deep: the best of the whole native pool (a generation-2 PULL child)
        pool = [r for r in native.values() if r.get('status') == 'ok']
        best = min(pool, key=halving._rank_key('native'))
        self.assertEqual(fakes.calls['deep'], [best['id']])
        self.assertTrue(best['id'].startswith('g2c'))
        # children: legal, and only the mover differs from the parent's evaluated pose
        g, c, r = _load(INPUTS, CONSTRAINTS)
        con, src = prepared(g, c, r)
        gp = fakes.records('gen-place')
        parent = {x.ref: x for x in BoardGraph.from_json((DATA / 'board-placed.json').read_text()).components}
        for cid, rec in gp.items():
            child = BoardGraph.from_json((out / 'cand' / cid / 'placed.json').read_text())
            self.assertEqual(source_errors(child, src, con), {}, cid)
            if rec['arm'] in ('pull', 'rand') and rec['parent'].startswith('h9'):
                moved = [x.ref for x in child.components
                         if tuple(x.pos) != tuple(parent[x.ref].pos) or x.rot % 360 != parent[x.ref].rot % 360]
                self.assertEqual(moved, rec['op_detail']['mover_refs'], cid)
        self.assertTrue((out / 'feedback-table.g2.json').exists())
        # resume: nothing re-run, no duplicate records
        lines = (out / 'dataset.jsonl').read_text()
        fakes2, status2 = self.run_main(out, '--generations', '2')
        self.assertEqual(fakes2.calls, {'rung1': [], 'native': [], 'deep': []})
        self.assertEqual((out / 'dataset.jsonl').read_text(), lines)
        self.assertEqual(status2['stages']['gen2']['parents'], g2['parents'])

    def test_rung1_entry_promotes_half_and_repeat_goes_direct(self):
        fakes, status = self.run_main(self.root / 'out', '--generations', '1', '--rung1-stop', '06-signals',
                                      '--stop-after', 'native')
        g1 = status['stages']['gen1']
        self.assertEqual(g1['entry'], 'rung1')
        self.assertEqual(sorted(fakes.calls['rung1']), ['g1c00', 'g1c01', 'g1c02', 'g1c03', 'g1f0', 'g1r0'])
        self.assertEqual(len(g1['promoted']), 3)
        self.assertEqual(sorted(fakes.calls['native']), sorted(['g1p0'] + g1['promoted']))
        self.assertEqual(fakes.calls['deep'], [])                                      # --stop-after native
        for i in g1['promoted']:
            self.assertEqual(fakes.records('native')[i]['promoted_from'], 'gen-rung1')

    def test_library_units_move_rigidly(self):
        lib = self.root / 'lib.json'
        tier = [dict(width=30.0, height=30.0, layout={}, missing=0, seed=0, utilisation=.3, aspect=1.0)]
        lib.write_text(json.dumps({'board.converter': tier, 'board.pd': tier}))
        from pnr.hier.blocks import extract_blocks
        from pnr.mc.halving import _load
        g, c, _ = _load(INPUTS, CONSTRAINTS)
        blocks = {b.name: set(b.refs) for b in extract_blocks(g, c)}
        fakes, status = self.run_main(self.root / 'out', '--generations', '1', '--library', str(lib),
                                      '--stop-after', 'native')
        self.assertEqual(status['feedback']['library_blocks'], ['board.converter', 'board.pd'])
        for cid, rec in fakes.records('gen-place').items():
            refs = set(rec.get('op_detail', {}).get('mover_refs') or [])
            for name in ('board.converter', 'board.pd'):
                self.assertIn(len(refs & blocks[name]), (0, len(blocks[name])), (cid, name))

    def test_rebase_stale_seed_records(self):
        self.write_seed(code=None)                               # code unknown: fixture rounds name no tree
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(self.root / 'o0', '--generations', '1')
        self.assertIn('code unknown', err.getvalue())
        out = self.root / 'out'
        fakes, status = self.run_main(out, '--generations', '1', '--import-code-mismatch', 'rebase')
        native = fakes.records('native')
        self.assertEqual(status['seed_from']['stale_code'], ['h9-p000', 'h9-p001'])
        self.assertEqual(sorted(fakes.calls['native'])[:2], ['g1c00', 'g1c01'])
        self.assertEqual(sorted(i for i in fakes.calls['native'] if i.startswith('rb-')), ['rb-h9-p000', 'rb-h9-p001'])
        self.assertNotIn('h9-p000', fakes.calls['native'])
        rb = status['stages']['rebase']
        self.assertEqual(rb['pairs'], [['h9-p000', 38, 39], ['h9-p001', 45, 46]])     # fake: a re-run adds 1 open
        self.assertEqual(native['rb-h9-p000']['arm'], 'rebase')
        self.assertEqual(native['rb-h9-p000']['parent'], 'h9-p000')
        # generation 1 ranks only same-code records: the rebases are the parents, never the stale imports
        g1 = status['stages']['gen1']
        self.assertEqual(g1['parents'], ['rb-h9-p000', 'rb-h9-p001'])
        self.assertEqual(g1['pool'], 2)
        for rid, rec in native.items():
            if rid.startswith('g1') and rec['arm'] != 'prior':
                self.assertTrue(rec['parent'].startswith('rb-'), rid)
        # deep: the best same-code record, never a stale import
        self.assertEqual(len(fakes.calls['deep']), 1)
        self.assertFalse(fakes.calls['deep'][0].startswith('h9-'))
        # the rebase evaluated the seed placement unchanged
        self.assertEqual((out / 'cand' / 'rb-h9-p000' / 'placed.json').read_text(),
                         (self.seed_run / 'cand' / 'p000' / 'placed.json').read_text())
        # resume: nothing re-run
        lines = (out / 'dataset.jsonl').read_text()
        fakes2, _ = self.run_main(out, '--generations', '1', '--import-code-mismatch', 'rebase')
        self.assertEqual(fakes2.calls, {'rung1': [], 'native': [], 'deep': []})
        self.assertEqual((out / 'dataset.jsonl').read_text(), lines)
        # the code policy is frozen with the import set
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(out, '--generations', '1', '--import-code-mismatch', 'warn')
        self.assertIn('differs from the first start', err.getvalue())

    def test_code_from_the_evaluation_tree(self):
        # rounds naming this tree (as native_loop records it) are same-code: accepted under the default policy
        from pnr.feedback.signals import PNR_ROOT
        self.write_seed(code=None, tree=PNR_ROOT.parents[1])
        fakes, status = self.run_main(self.root / 'out', '--generations', '1', '--stop-after', 'native',
                                      '--gen-repeat', '0', '--gen-fresh', '0')
        self.assertEqual(status['seed_from']['stale_code'], [])
        self.assertEqual(status['stages']['gen1']['parents'], ['h9-p000', 'h9-p001'])

    def test_seed_gate_torn_lines_and_frozen_imports(self):
        self.write_seed(ready=False)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(self.root / 'o1', '--generations', '1')
        self.assertIn('native stage not finished', err.getvalue())
        # a running seed run may end in a half-written line: skipped with a warning, not a crash
        with (self.seed_run / 'dataset.jsonl').open('a') as fh:
            fh.write('{"id": "p003", "stage": "nat')
        out = self.root / 'out'
        fakes, status = self.run_main(out, '--generations', '1', '--seed-allow-running', '--stop-after', 'native',
                                      '--gen-fresh', '0')
        self.assertEqual(status['seed_from']['native'], ['h9-p000', 'h9-p001'])
        self.assertTrue(any('unreadable dataset line' in w for w in status['seed_from']['warnings']))
        # the seed run goes on and finishes a better candidate: a resume keeps the frozen import set
        cand = self.seed_run / 'cand' / 'p004'
        cand.mkdir(parents=True)
        shutil.copy2(DATA / 'board-placed.json', cand / 'placed.json')
        write_round(cand / 'native', [], 1, cand / 'placed.json')
        with (self.seed_run / 'dataset.jsonl').open('a') as fh:
            fh.write('\n' + json.dumps(dict(id='p004', stage='native', status='ok', objective=[0, 0, 0, 150, 0, 1],
                                            opens=1)) + '\n')
        lines = (out / 'dataset.jsonl').read_text()
        fakes2, status2 = self.run_main(out, '--generations', '1', '--seed-allow-running', '--stop-after', 'native',
                                        '--gen-fresh', '0')
        self.assertEqual(status2['seed_from']['native'], ['h9-p000', 'h9-p001'])
        self.assertTrue(status2['seed_from']['frozen'])
        self.assertEqual(fakes2.calls, {'rung1': [], 'native': [], 'deep': []})
        self.assertEqual((out / 'dataset.jsonl').read_text(), lines)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(out, '--generations', '1', '--seed-allow-running', '--seed-from', str(self.root / 'h8'))
        self.assertIn('frozen at the first start', err.getvalue())

    def test_resume_refuses_a_changed_plan(self):
        out = self.root / 'out'
        self.run_main(out, '--generations', '1', '--stop-after', 'native')
        # tamper: g1c00 was stored with other poses (e.g. planned from a different parent)
        lines = (out / 'dataset.jsonl').read_text().splitlines()
        edited = []
        for line in lines:
            r = json.loads(line)
            if r.get('stage') == 'gen-place' and r['id'] == 'g1c00':
                r['poses_sha'] = 'deadbeef00'
            edited.append(json.dumps(r, sort_keys=True))
        (out / 'dataset.jsonl').write_text('\n'.join(edited) + '\n')
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            self.run_main(out, '--generations', '1', '--stop-after', 'native')
        self.assertIn('resume plan changed for g1c00', str(cm.exception))

    def test_prior_needs_pair_weights_and_rand_is_matched(self):
        from pnr.feedback.table import Table
        with mock.patch.object(Table, 'pair_weights', return_value={}):
            fakes, status = self.run_main(self.root / 'out', '--generations', '1', '--stop-after', 'native')
        g1 = status['stages']['gen1']
        self.assertEqual(g1['prior_skipped'], 1)
        self.assertNotIn('prior', g1['children'])
        self.assertEqual(fakes.priors, [])
        gp = fakes.records('gen-place')
        self.assertEqual(gp['g1r0']['matched'], 'g1c00')
        self.assertEqual((gp['g1c00']['k'], gp['g1c01']['k']), (0, 1))
        self.assertEqual(gp['g1c00']['parent'], gp['g1r0']['parent'])
        self.assertEqual(fakes.records('native')['g1r0']['matched'], 'g1c00')

    def test_flags_and_router_mismatch(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main(self.root / 'o1', '--generations', '1', env=dict(ENV, PNR_FEEDBACK='0'))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_main(self.root / 'o2', '--generations', '1', env=dict(ENV, PNR_SHOVE='1'))
        self.assertIn("router 'plain' != this run 'shove'", err.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main(self.root / 'o3', '--generations', '1', '--gen-entry', 'rung1')   # needs --rung1-stop


if __name__ == '__main__':
    unittest.main()
