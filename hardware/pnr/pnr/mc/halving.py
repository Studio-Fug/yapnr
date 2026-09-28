"""Successive halving over complete placements.

Every stage ranks the survivors of the previous stage by that stage's own
measured objective and promotes a fixed fraction to the next, more expensive
stage. No candidate is chosen by hand; the only inputs are the stage budgets.

Stages
  0 place   N independent global starts -> legalize -> cheap/capacity proxy
  1 screen  K1 best proxies + a seeded uniform control sample of the other legal
            placements (``control=True``) -> Python detail route (signal screening)
  2 native  K2 best screens (controls compete on their screen result only), or
            with ``--promote-from place`` a seeded uniform sample of K2 legal
            placements (``sampled=True``, no screen) -> native pipeline, short budget
  3 deep    K3 best natives -> full native electrical pipeline, long budget

Each stage appends one JSON record per candidate to ``dataset.jsonl`` (pose,
stage metrics, timings) so later stages' outcomes can be regressed on earlier
features. Rank agreement between consecutive stages is reported (Spearman, only
from SPEARMAN_MIN_N pairs up; the pair count is always recorded) so the value of
each cheap stage as a predictor is measured rather than assumed.

With ``--library`` the block library is frozen once into
``out/library.snapshot.json``; every worker (and any resume of the same --out)
reads that snapshot, never the live library directory.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
PNR_ROOT = HERE.parents[2]          # .../hardware/pnr
SRC_ROOT = HERE.parents[4]          # repo-shaped source root (contains hardware/)
KI = '/Applications/KiCad/KiCad.app/Contents'
SPEARMAN_MIN_N = 8                  # below this a rank correlation is noise: record null + n
LIBRARY_SNAPSHOT = 'library.snapshot.json'


# ---------------------------------------------------------------- utilities

def _append(path: Path, record: dict) -> None:
    with path.open('a') as fh:
        fh.write(json.dumps(record, sort_keys=True) + '\n')


def _process_pool(n):
    """Stage 0/1 executor (a seam so tests can run the driver in-process)."""
    return cf.ProcessPoolExecutor(n)


def _loadavg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _jsonable(value):
    """Plain JSON types only: tuples/sets -> lists, paths -> str, numpy -> python."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, 'tolist'):    # numpy scalar / array
        return _jsonable(value.tolist())
    raise TypeError(f'library value of type {type(value).__name__} is not JSON-serializable')


def _snapshot_library(root: Path, path: Path) -> dict:
    """Freeze ``load_library(root)`` into ``path`` once; a resume reuses the frozen copy."""
    reused = path.exists()
    if not reused:
        from pnr.hier.top import load_library
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(_jsonable(load_library(Path(root))), sort_keys=True, indent=1))
        tmp.replace(path)
    data = path.read_bytes()
    return dict(path=str(path), source=str(root), reused=reused, blocks=len(json.loads(data)),
                sha256=hashlib.sha256(data).hexdigest())


def _read_library(library):
    """(library, sha256 or None) from a snapshot file, or from a library root (legacy callers)."""
    path = Path(library)
    if path.is_dir():
        from pnr.hier.top import load_library
        return load_library(path), None
    data = path.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def _spearman(a, b):
    """Rank correlation of two equal-length sequences (ties averaged)."""
    n = len(a)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else None


def _agreement(pairs):
    """(rho, n) over (x, y) pairs; rho is None below SPEARMAN_MIN_N pairs."""
    pairs = list(pairs)
    n = len(pairs)
    rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs]) if n >= SPEARMAN_MIN_N else None
    return rho, n


def _load(inputs: Path, constraints_path: Path):
    import yaml
    from pnr.graph import BoardGraph
    from pnr.constraints import compile_constraints
    graph = BoardGraph.from_json((inputs / 'graph.json').read_text())
    constraints = compile_constraints(
        yaml.safe_load(constraints_path.read_text()), graph.refs,
        {c.address: c.ref for c in graph.components if c.address},
        {f"{c.address}:{p.name}": p.net for c in graph.components if c.address for p in c.pads if p.name})
    from pnr.fab_profile import apply_rules
    rules = apply_rules(json.loads((inputs / 'rules.json').read_text()))
    return graph, constraints, rules


# ---------------------------------------------------------------- stage 0

PLACE_JOB = ('inputs', 'constraints_path', 'start', 'cand_dir', 'iters', 'library', 'assemble_blocks')


def _place_one(args):
    """Worker: one global start -> legal placement + proxies. Never raises.

    ``args`` follows PLACE_JOB; ``library`` is the run's frozen snapshot file (a
    library root directory is still accepted). A hierarchical start only uses the
    start's seed, so it is recorded as kind='hier-seed' with the pool's start_kind.
    """
    inputs, constraints_path, start, cand_dir, iters, library, assemble_blocks = args
    t = time.monotonic()
    record = dict(id=start['id'], kind='hier-seed' if library else start['kind'], start_kind=start['kind'],
                  seed=start['seed'], stage='place')
    try:
        if library:
            lib, digest = _read_library(library)
            if digest:
                record['library_sha256'] = digest
        from pnr.place.initial_pool import preserve_source_locks, _prepared_source, _hard_and_source_errors
        from pnr.place.placer import place
        from pnr.place.metrics import hpwl
        from pnr.place.capacity_proxy import cheap_score, score
        graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
        constraints = preserve_source_locks(graph, constraints)
        source = _prepared_source(graph, constraints, rules)
        if library:
            from pnr.hier.top import hierarchical_place
            placed, report, choice = hierarchical_place(graph, constraints, rules, lib, start['seed'], iters)
            record['hier'] = choice
            if assemble_blocks:
                boards = [v['native_dir'] + '/electrical/board.kicad_pcb' for v in choice['blocks'].values()
                          if v.get('native_dir')]
                Path(cand_dir).mkdir(parents=True, exist_ok=True)
                (Path(cand_dir) / 'blocks.json').write_text(json.dumps(boards, indent=1))
        else:
            placed, report = place(source, constraints, seed=start['seed'], iters=iters, orient=True,
                                   spread=1.0, channel_rules=rules,
                                   initial_positions=start.get('positions'),
                                   initial_rotations=start.get('rotations'))
        errors = _hard_and_source_errors(placed, source, constraints)
        from pnr.place.metrics import hard_violations
        flat_violations = {k: v for k, v in hard_violations(placed, constraints).items() if v}
        if errors or not report.legal or flat_violations:
            record['flat_violations'] = str(flat_violations)[:500]
            record.update(status='illegal', errors=str(errors)[:500])
        else:
            cand = Path(cand_dir)
            cand.mkdir(parents=True, exist_ok=True)
            (cand / 'placed.json').write_text(placed.to_json())
            record.update(status='legal', hpwl_mm=hpwl(placed), cheap_score=cheap_score(placed, rules))
            try:
                proxy = score(placed, rules, pitch=2.0, passes=2)
                record['proxy_score'] = proxy['score']
                record['proxy'] = {k: v for k, v in proxy.items() if k not in ('heatmap', 'rounds')}
            except ValueError as error:
                record.update(proxy_score=float('inf'), proxy_error=str(error))
            record['poses'] = {c.ref: [c.pos[0], c.pos[1], c.rot, c.side] for c in placed.components}
    except Exception as error:  # a failed start is data, not a crash
        record.update(status='failed', error=repr(error), traceback=traceback.format_exc()[-2000:])
    record['seconds'] = time.monotonic() - t
    return record


def _starts(inputs, constraints_path, n, seed):
    from pnr.place.initial_pool import InitialPoolConfig, initial_starts, preserve_source_locks, _prepared_source
    graph, constraints, rules = _load(inputs, constraints_path)
    constraints = preserve_source_locks(graph, constraints)
    source = _prepared_source(graph, constraints, rules)
    cfg = InitialPoolConfig(starts=n, route_finalists=1, proxy_budget=n)
    starts = initial_starts(source, constraints, cfg, seed=seed, orient=True)
    for s in starts:
        s['id'] = 'p%03d' % int(s['id'].split('-')[1])
    return starts


# ---------------------------------------------------------------- stage 1

def _screen_one(args):
    inputs, constraints_path, cand_dir, route_iters, live = args
    t = time.monotonic()
    cand = Path(cand_dir)
    record = dict(id=cand.name, stage='screen')
    try:
        if live:
            os.environ['PNR_LIVE_CANDIDATE'] = live
        from pnr.graph import BoardGraph
        from pnr.route.detail.router import route_board
        from pnr.place.initial_pool import _route_metrics
        graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
        placed = BoardGraph.from_json((cand / 'placed.json').read_text())
        route = route_board(placed, constraints, rules, pitch=None, max_iters=route_iters)
        m = _route_metrics(route)
        record.update(status='ok', **{k: v for k, v in m.items() if k != 'unresolved_nets'},
                      unresolved=len(m['unresolved_nets']))
        (cand / 'screen.json').write_text(json.dumps(m, indent=2))
    except Exception as error:
        record.update(status='failed', error=repr(error), traceback=traceback.format_exc()[-2000:])
    record['seconds'] = time.monotonic() - t
    return record


# ---------------------------------------------------------------- stage 2/3

def _assemble_boards(blocks: Path, repo: Path):
    """(boards, None) for cand/blocks.json, or (None, why it cannot be assembled).
    Relative board paths resolve against ``repo`` (the native subprocess cwd)."""
    if not blocks.exists():
        return None, f'--assemble set but {blocks} is missing (candidate not placed with --library --assemble)'
    try:
        boards = json.loads(blocks.read_text())
    except ValueError as error:
        return None, f'--assemble set but {blocks} is unreadable: {error}'
    if not boards:
        return None, f'--assemble set but {blocks} lists no routed block boards (library has no native_dir)'
    missing = [b for b in boards if not (Path(repo) / b).exists()]
    if missing:
        return None, f'--assemble set but {len(missing)} block board(s) are missing: {missing[:4]}'
    return boards, None


def _native_one(inputs: Path, constraints_path: Path, cand: Path, stage: str, seconds: int,
                workers: int, env: dict, repo: Path, assemble: bool = False) -> dict:
    """Full native evaluation of one candidate. With ``assemble`` the routed block
    copper in cand/blocks.json is copied in; if that is impossible the record is
    status='failed' with the reason and nothing is run (never a silent flat run)."""
    t = time.monotonic()
    round_dir = cand / stage
    blocks = cand / 'blocks.json'
    record = dict(id=cand.name, stage=stage, budget_seconds=seconds, workers=workers,
                  assembled=False, loadavg_start=_loadavg())
    if assemble:
        boards, error = _assemble_boards(blocks, repo)
        if error:
            record.update(status='failed', error=error, loadavg_end=_loadavg(), seconds=time.monotonic() - t)
            return record
        record.update(assembled=True, assembled_blocks=len(boards))
    if round_dir.exists():
        shutil.rmtree(round_dir)
    round_dir.mkdir(parents=True)
    for name in ('source.kicad_pcb', 'source.kicad_pro', 'rules.json', 'fp-lib-table'):
        shutil.copy2(inputs / name, round_dir / name)
    shutil.copy2(cand / 'placed.json', round_dir / 'placed.json')
    dev = SRC_ROOT / 'hardware/splanc_dev'
    cmd = [sys.executable, '-m', 'pnr.full_iteration', str(round_dir),
           '--constraints', str(constraints_path),
           '--electrical-fab', str(dev / 'mini-routing-electrical-fab.json'),
           '--plane-fab', str(dev / 'mini-plane-access-fab.json'),
           '--annotation-source', str(dev / 'elec/src/splanc_mini.ato'),
           '--seconds', str(seconds), *(['--assemble', str(blocks)] if assemble else [])]
    run_env = dict(env, PNR_SINGLE_TRACK_WORKERS=str(workers),
                   PNR_LIVE_CANDIDATE=f"{env.get('PNR_LIVE_CANDIDATE', 'hier')}/{cand.name}/{stage}")
    with (round_dir / 'run.log').open('w') as log:
        code = subprocess.run(cmd, cwd=repo, env=run_env, stdout=log, stderr=subprocess.STDOUT).returncode
    record.update(exit_code=code, loadavg_end=_loadavg())
    evaluation = round_dir / 'evaluation.json'
    if evaluation.exists():
        e = json.loads(evaluation.read_text())
        obj = e['objective']    # [violations, blocked, reference, subwidth, unqualified_pairs, unconnected]
        record.update(status='ok', objective=obj, opens=obj[5], violations=obj[0],
                      subwidth=obj[3], blocked_entries=obj[1], reference_failures=obj[2],
                      unqualified_pairs=obj[4], qualified=e['qualified'])
        prog = round_dir / 'electrical/native-loop/progress.json'
        if prog.exists():
            p = json.loads(prog.read_text())
            record['trajectory'] = [r.get('after') for r in p.get('rounds', [])]
            record['initial_native_opens'] = p.get('initial_opens')
            record['native_termination'] = p.get('termination')
    else:
        record['status'] = 'failed'
    record['seconds'] = time.monotonic() - t
    return record


# ---------------------------------------------------------------- driver

def _rank_key(stage):
    if stage == 'place':
        return lambda r: (r.get('proxy_score', math.inf), r.get('cheap_score', math.inf))
    if stage == 'screen':
        return lambda r: tuple(r.get('objective') or [math.inf])
    # native/deep. objective = [violations, blocked, reference, subwidth, unqualified_pairs, unconnected]
    # key: violations, unconnected, unqualified pairs, reference failures, blocked entries, width debt, id
    def key(r):
        o = r.get('objective')
        return (o[0], o[5], o[4], o[2], o[1], o[3], r['id']) if o else (math.inf,) * 6 + (r['id'],)
    return key


def _sample(ids, k, seed, purpose):
    """Seeded uniform sample of ``k`` ids, independent of record order and of other draws."""
    ids = sorted(ids)
    return random.Random(f'halving:{seed}:{purpose}').sample(ids, min(max(k, 0), len(ids)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--inputs', type=Path, required=True)
    ap.add_argument('--constraints', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True, help='cwd for native subprocesses')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--n0', type=int, default=64)
    ap.add_argument('--k1', type=int, default=12)
    ap.add_argument('--k2', type=int, default=4)
    ap.add_argument('--k3', type=int, default=1)
    ap.add_argument('--control', type=int, default=2,
                    help='also screen a seeded uniform sample of N non-survivor legal placements (control=True)')
    ap.add_argument('--promote-from', choices=['screen', 'place'], default='screen',
                    help="stage-2 source: K2 best screens, or ('place') a seeded uniform sample of K2 legal "
                         'placements with no screen stage')
    ap.add_argument('--iters', type=int, default=600)
    ap.add_argument('--route-iters', type=int, default=10)
    ap.add_argument('--s2', type=int, default=900, help='native budget per stage-2 candidate')
    ap.add_argument('--s3', type=int, default=5400, help='native budget per stage-3 candidate')
    ap.add_argument('--procs', type=int, default=8)
    ap.add_argument('--native-parallel', type=int, default=2)
    ap.add_argument('--native-workers', type=int, default=4)
    ap.add_argument('--library', type=Path, help='block layout library: hierarchical placement '
                    f'(frozen once into OUT/{LIBRARY_SNAPSHOT}; a resume reuses that snapshot)')
    ap.add_argument('--assemble', action='store_true', help='copy routed block copper before native routing')
    ap.add_argument('--rung1-stop', help='native rung 1: stop each evaluation after this native_loop phase '
                    '(e.g. 06-signals) and promote the best --k2 of --k1r sampled placements')
    ap.add_argument('--k1r', type=int, default=12, help='placements sampled into rung 1')
    ap.add_argument('--stop-after', choices=['place', 'screen', 'native', 'deep'], default='deep')
    a = ap.parse_args(argv)
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    inputs = a.inputs.resolve()
    constraints_path = a.constraints.resolve()
    dataset = out / 'dataset.jsonl'
    status = dict(started=time.time(), args={k: str(v) for k, v in vars(a).items()}, stages={})

    def save_status():
        tmp = out / 'status.json.tmp'
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(out / 'status.json')

    def done_records(stage):
        if not dataset.exists():
            return {}
        recs = [json.loads(l) for l in dataset.read_text().splitlines() if l.strip()]
        return {r['id']: r for r in recs if r.get('stage') == stage}

    library = None
    if a.library:
        status['library'] = snap = _snapshot_library(a.library.resolve(), out / LIBRARY_SNAPSHOT)
        library = snap['path']
        print('library', 'reused' if snap['reused'] else 'snapshot', snap['path'], snap['blocks'], 'blocks',
              snap['sha256'][:12], flush=True)
        if not snap['blocks']:
            print('warning: library snapshot has no ranked blocks', flush=True)

    # ---- stage 0
    placed = done_records('place')
    starts = [s for s in _starts(inputs, constraints_path, a.n0, a.seed) if s['id'] not in placed]
    t0 = time.monotonic()
    with _process_pool(a.procs) as pool:
        jobs = [(str(inputs), str(constraints_path), s, str(out / 'cand' / s['id']), a.iters, library, a.assemble)
                for s in starts]     # PLACE_JOB order
        for rec in pool.map(_place_one, jobs):
            _append(dataset, rec)
            placed[rec['id']] = rec
            print('place', rec['id'], rec['status'], rec.get('proxy_score'), '%.1fs' % rec['seconds'], flush=True)
    legal = [r for r in placed.values() if r['status'] == 'legal']
    status['stages']['place'] = dict(n=len(placed), legal=len(legal), seconds=time.monotonic() - t0)
    save_status()
    if not legal:
        raise SystemExit('no legal placement')
    if a.stop_after == 'place':
        return
    screened = {}

    if a.promote_from == 'place':
        # ---- stage 1 skipped: uniform sample of legal placements goes straight to native
        count = a.k1r if a.rung1_stop else a.k2
        survivors = [placed[i] for i in _sample([r['id'] for r in legal], count, a.seed, 'promote-place')]
        flags = {r['id']: dict(promoted_from='place', sampled=True, control=False) for r in survivors}
        status['stages']['screen'] = dict(skipped=True, promote_from='place', sampled=[r['id'] for r in survivors])
        save_status()
        if a.stop_after == 'screen':
            return
    else:
        # ---- stage 1: top-K1 by proxy plus a uniform control sample of the rest
        survivors = sorted(legal, key=_rank_key('place'))[:a.k1]
        keep = {r['id'] for r in survivors}
        controls = _sample([r['id'] for r in legal if r['id'] not in keep], a.control, a.seed, 'control')
        control_ids = set(controls)
        cands = survivors + [placed[i] for i in controls]
        screened = done_records('screen')
        t1 = time.monotonic()
        todo = [r for r in cands if r['id'] not in screened]
        base_lane = os.environ.get('PNR_LIVE_CANDIDATE', 'hier')
        with _process_pool(min(a.procs, max(1, len(todo)))) as pool:
            jobs = [(str(inputs), str(constraints_path), str(out / 'cand' / r['id']), a.route_iters,
                     f"{base_lane}/{r['id']}/screen") for r in todo]
            for rec in pool.map(_screen_one, jobs):
                rec['control'] = rec['id'] in control_ids
                _append(dataset, rec)
                screened[rec['id']] = rec
                print('screen', rec['id'], rec['status'], rec.get('objective'), 'control' * rec['control'],
                      '%.1fs' % rec['seconds'], flush=True)
        # membership is this run's draw, not whatever an earlier (resumed) record said
        s1 = [dict(screened[r['id']], control=r['id'] in control_ids) for r in cands
              if screened.get(r['id'], {}).get('status') == 'ok']
        pairs = lambda rs: [(placed[r['id']]['proxy_score'], r['objective'][0]) for r in rs]
        rho, n = _agreement(pairs(r for r in s1 if not r['control']))
        rho_all, n_all = _agreement(pairs(s1))
        status['stages']['screen'] = dict(n=len(s1), seconds=time.monotonic() - t1, controls=controls,
            spearman_proxy_vs_missing=rho, spearman_proxy_vs_missing_n=n,
            spearman_proxy_vs_missing_all=rho_all, spearman_proxy_vs_missing_all_n=n_all,
            missing=dict(survivor=sorted(r['objective'][0] for r in s1 if not r['control']),
                         control=sorted(r['objective'][0] for r in s1 if r['control'])))
        save_status()
        if a.stop_after == 'screen' or not s1:
            return
        # controls compete on their screen result only; ties keep proxy-ranked survivors first
        survivors = sorted(s1, key=_rank_key('screen'))[:a.k2]
        flags = {r['id']: dict(promoted_from='screen', sampled=False, control=r['id'] in control_ids)
                 for r in survivors}
        status['stages']['screen']['promoted_controls'] = [r['id'] for r in survivors if r['control']]
        save_status()

    # ---- stage 2 / 3 (native)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(PNR_ROOT)

    def native_stage(name, cands, seconds, stop_phase=None):
        done = done_records(name)
        todo = [r for r in cands if r['id'] not in done]
        stage_env = dict(env, PNR_STOP_AFTER_PHASE=stop_phase) if stop_phase else env
        with cf.ThreadPoolExecutor(a.native_parallel) as pool:
            futs = {pool.submit(_native_one, inputs, constraints_path, out / 'cand' / r['id'], name,
                                seconds, a.native_workers, stage_env, a.repo.resolve(), assemble=a.assemble): r
                    for r in todo}
            for f in cf.as_completed(futs):
                rec = f.result()
                rec.update(flags.get(rec['id'], {}))
                _append(dataset, rec)
                done[rec['id']] = rec
                print(name, rec['id'], rec.get('status'), rec.get('objective'), rec.get('error', ''),
                      '%.0fs' % rec['seconds'], flush=True)
        return [done[r['id']] for r in cands if done.get(r['id'], {}).get('status') == 'ok']

    env.pop('PNR_STOP_AFTER_PHASE', None)
    if a.rung1_stop:
        # ---- native rung 1: every sampled placement through phases up to rung1_stop
        tr = time.monotonic()
        r1 = native_stage('rung1', survivors, a.s2, stop_phase=a.rung1_stop)
        status['stages']['rung1'] = dict(n=len(r1), seconds=time.monotonic() - tr, stop=a.rung1_stop,
            opens=sorted((r['opens'], r['id']) for r in r1))
        save_status()
        survivors = [dict(x) for x in sorted(r1, key=_rank_key('native'))[:a.k2]]
        for r in survivors:
            flags[r['id']] = dict(flags.get(r['id'], {}), promoted_from='rung1')
    t2 = time.monotonic()
    s2 = native_stage('native', survivors, a.s2)
    rho_s, n_s = _agreement((screened[r['id']]['objective'][0], r['opens']) for r in s2
                            if screened.get(r['id'], {}).get('status') == 'ok')
    rho_p, n_p = _agreement((placed[r['id']]['proxy_score'], r['opens']) for r in s2)
    status['stages']['native'] = dict(n=len(s2), seconds=time.monotonic() - t2, promote_from=a.promote_from,
        promoted=[r['id'] for r in survivors], assemble=a.assemble,
        spearman_screen_vs_opens=rho_s, spearman_screen_vs_opens_n=n_s,
        spearman_proxy_vs_opens=rho_p, spearman_proxy_vs_opens_n=n_p)
    save_status()
    if a.stop_after == 'native' or not s2:
        return
    survivors = sorted(s2, key=_rank_key('native'))[:a.k3]
    t3 = time.monotonic()
    s3 = native_stage('deep', survivors, a.s3)
    best = min(s3, key=_rank_key('deep')) if s3 else None
    status['stages']['deep'] = dict(n=len(s3), seconds=time.monotonic() - t3,
                                    best=best['id'] if best else None,
                                    best_objective=best['objective'] if best else None)
    status['finished'] = time.time()
    save_status()


if __name__ == '__main__':
    main()
