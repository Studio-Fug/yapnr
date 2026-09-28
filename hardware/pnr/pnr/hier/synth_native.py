"""Block layout synthesis judged by the native electrical pipeline.

Stage A: sample (outline, seed) local placements of a block template (cheap).
Outlines come from the (utilisation, aspect) grid of ``aspect_sizes``; each
dimension is capped at the block's hard-group extent bound plus edge margin
(:func:`outline_cap`) when its members are all tied together by HARD group radii.
Stage B: evaluate distinct legal placements with pnr.full_iteration on the
block's own sub-board, for every instance of the template, ``--repeats`` times
each (distinct native dirs). Layouts are issued round-robin across the
(utilisation, aspect) strata (:func:`stratified_order`); ``--enough`` stops a
template at the end of the round in which that many layouts are complete.

Ranking is lexicographic on measured native outcomes summed over instances
(the full objective vector): unconnected, violations, blocked entries, reference
failures, subwidth, unqualified pairs, then port-escape debt and area. Over
repeats a layout keeps the worst (max) guard terms and the mean subwidth, so
'complete' (no unconnected, no violations) needs every repeat complete. The
library keeps the ranked layouts; ``library.json`` is replaced atomically.

With ``PNR_POWER_FIRST=1`` placements use the staged power-first objective
(:mod:`pnr.place.power_first`); each trial records ``power_quality`` (placement:
q_place = rigid J1, loops, crossings, series gaps, stage wiring; native: routed
hot loops, vias, F.Cu share, E_pow). Strata are ordered by q_band first and the
rank key inserts open hot-loop links, q_band and power crossings after
unconnected/violations (see :func:`rank_key`).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import time
import traceback
from pathlib import Path

import yaml


def _place(args):
    inputs, constraints_path, name, size, seed, iters = args
    from pnr.mc.halving import _load
    from pnr.hier.blocks import extract_blocks, sub_board
    from pnr.hier.synth import local_key, _port_debt
    from pnr.place.placer import place
    w, h, u, a = size
    rec = dict(block=name, width=w, height=h, utilisation=u, aspect=a, seed=seed, area=w * h)
    t = time.monotonic()
    try:
        graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
        blk = {b.name: b for b in extract_blocks(graph, constraints)}[name]
        sg, sc, sr = sub_board(graph, constraints, rules, blk, w, h)
        placed, report = place(sg, sc, seed=seed, iters=iters, orient=True, channel_rules=sr)
        rec['legal'] = bool(report.legal)
        if report.legal:
            rec['layout'] = {local_key(blk, c.address): [c.pos[0], c.pos[1], c.rot, c.side]
                             for c in placed.components}
            rec['port_debt_mm'] = _port_debt(placed, blk.external_nets, w, h)
            if _power_first():
                pq = _placement_power_quality(sg, sc, sr, placed)
                if pq is not None:
                    rec['power_quality'] = pq
    except Exception as error:
        rec.update(legal=False, error=repr(error))
    rec['place_seconds'] = time.monotonic() - t
    return rec


Q_BAND = 0.05   # q_place band width, as a fraction of the template's best legal q_place


def _power_first():
    return os.environ.get('PNR_POWER_FIRST') == '1'


def _num(value):
    return math.inf if value is None else value


def _placement_power_quality(sg, sc, sr, placed):
    """Placement-level power metrics of a legal block layout (pnr.place.power_first)."""
    from pnr.power_topology import PowerTopologyUnavailable, derive
    from pnr.place.power_first import placement_quality
    try:
        roles = derive(sg, sc, sr)
    except PowerTopologyUnavailable:
        return None
    q = placement_quality(placed, roles)
    info = getattr(placed, 'power_first', None)
    if info:
        q['placer'] = {k: v for k, v in info.items() if k != 'continuous'}
    return q


def power_bands(recs):
    """Stamp q_band = floor(q_place / (Q_BAND * q_ref)) on each record; q_ref = best legal q_place."""
    qs = [r['power_quality']['q_place'] for r in recs if (r.get('power_quality') or {}).get('q_place') is not None]
    if not qs:
        return None
    q_ref = min(qs)
    for r in recs:
        pq = r.get('power_quality')
        if pq and pq.get('q_place') is not None:
            pq['q_ref'] = q_ref
            pq['q_band'] = math.floor(pq['q_place'] / (Q_BAND * q_ref)) if q_ref > 0 else 0
    return q_ref


def open_band(evaluated):
    """B = max(1, round(pooled within-layout SD of opens)) over layouts with >= 2 completed repeats."""
    ss, dof = 0.0, 0
    for x in evaluated:
        vals = [r['objective'][5] for r in x.get('runs') or [] if r.get('objective')]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            ss += sum((v - mean) ** 2 for v in vals)
            dof += len(vals) - 1
    return max(1, round(math.sqrt(ss / dof))) if dof else 1


def _native(inputs, constraints_path, rec, names, out, seconds, workers, repo, repeat=0):
    from pnr.mc.halving import _load
    from pnr.hier.blocks import extract_blocks, block_constraints_doc
    from pnr.hier.synth import instance_board
    from pnr.hier.native_block import evaluate
    graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
    blocks = {b.name: b for b in extract_blocks(graph, constraints)}
    doc0 = yaml.safe_load(Path(constraints_path).read_text())
    results = []
    for name in names:
        blk = blocks[name]
        g2, c2, r2 = instance_board(graph, constraints, rules, blk, rec['layout'], rec['width'], rec['height'])
        doc = block_constraints_doc(doc0, [c.address for c in g2.components], rec['width'], rec['height'])
        tag = '%s-s%d-%gx%g' % (name.replace(':', '_').replace('.', '_'), rec['seed'], rec['width'], rec['height'])
        tag += '-r%d' % repeat if repeat else ''
        try:
            r = evaluate(Path(out) / 'native' / tag, Path(inputs), doc, g2, r2, seconds, workers,
                         Path(repo), live_lane='blocks/' + tag)
        except Exception as error:
            r = dict(status='failed', error=repr(error), traceback=traceback.format_exc()[-1500:])
        r['instance'] = name
        r['dir'] = str(Path(out) / 'native' / tag)
        results.append(r)
    ok = all(r.get('status') == 'ok' for r in results)
    total = [sum(r['objective'][i] for r in results) for i in range(6)] if ok else None
    out = dict(rec, stage='native', repeat=repeat, instances=results, status='ok' if ok else 'failed',
               objective=total)
    if _power_first():
        hot = [r.get('hot_loops_open') for r in results]
        out['hot_loops_open'] = sum(hot) if ok and all(h is not None for h in hot) else None
    return out


def rank_key(r, band=None):
    """Full objective: unconnected, violations, blocked, ref failures, subwidth, pairs; then debt, area.

    With PNR_POWER_FIRST=1: unconnected, violations, open hot-loop links (routed),
    q_band and power crossings (placement), then the remaining terms; ``band``
    (PNR_RANK_OPEN_BAND=auto) coarsens unconnected to floor(unconnected / band).
    A layout without a routed power analysis ranks as if every hot loop were open.
    """
    o = r.get('objective')
    if not o:
        return (math.inf,)
    if _power_first():
        pq = r.get('power_quality') or {}
        return (o[5] // band if band else o[5], o[0], _num(r.get('hot_loops_open')), _num(pq.get('q_band')),
                _num(pq.get('crossings')), o[1], o[2], o[3], o[4], r.get('port_debt_mm', math.inf),
                r.get('area', math.inf))
    return (o[5], o[0], o[1], o[2], o[3], o[4], r.get('port_debt_mm', math.inf), r.get('area', math.inf))


def is_complete(r):
    o = r.get('objective')
    return bool(r.get('status') == 'ok' and o and o[5] == 0 and o[0] == 0)


def aggregate(runs):
    """One layout's result over its native repeats (a single run is returned as is).

    Conservative: every guard term (unconnected, violations, blocked, reference
    failures, unqualified pairs) is the max over repeats, subwidth (index 3) the
    mean; any failed repeat fails the layout. The best repeat by :func:`rank_key`
    is the representative whose instance dirs (routed boards) are kept.
    """
    if len(runs) == 1:
        return runs[0]
    best = min(runs, key=rank_key)
    ok = all(r.get('status') == 'ok' and r.get('objective') for r in runs)
    objective = None
    if ok:
        objective = [max(r['objective'][i] for r in runs) for i in range(len(best['objective']))]
        objective[3] = sum(r['objective'][3] for r in runs) / len(runs)
    out = {k: v for k, v in best.items() if k != 'repeat'}
    out.update(stage='native', status='ok' if ok else 'failed', objective=objective, repeats=len(runs),
               representative_repeat=best.get('repeat'),
               runs=[dict(repeat=r.get('repeat'), status=r.get('status'), objective=r.get('objective'),
                          dirs=[i.get('dir') for i in r.get('instances') or []]) for r in runs])
    if _power_first():
        # Placement terms are shared by every repeat (copied from best); routed hot
        # loops keep the worst repeat, like the other guard terms.
        hot = [r.get('hot_loops_open') for r in runs]
        out['hot_loops_open'] = max(hot) if all(h is not None for h in hot) else None
        for summary, r in zip(out['runs'], runs):
            summary['hot_loops_open'] = r.get('hot_loops_open')
    return out


CAP_EDGE_MARGIN_MM = 1.0   # the edge room aspect_sizes adds to the largest part (its span)


def outline_cap(graph, constraints, block):
    """Largest useful outline dimension implied by the block's HARD groups, or None.

    A hard group radius bounds anchor-member centre distance; shortest paths over
    those radii (edges inside the block) bound every member pair by the triangle
    inequality. When all pairs are bounded no legal layout's courtyards extend
    further than ``E = max_ij d_ij + (s_i + s_j) / 2`` on either axis (``s``:
    larger courtyard side, so any rotation is covered), e.g. radius 12 around
    one IC gives at most 2*12 + the largest part. The cap is ``E`` plus the same
    edge margin ``aspect_sizes`` gives its single-part span, so it never drops
    below that span and a tight bound (one big part, or a pair forced to ``E``)
    still leaves edge room to legalize. Rounded up to the 0.25 mm outline grid.
    """
    from pnr.place.geometry import hard_group_edges
    refs = list(block.refs)
    inside = set(refs)
    d = {(i, j): 0.0 if i == j else math.inf for i in refs for j in refs}
    for a, b, radius in hard_group_edges(constraints):
        if a in inside and b in inside:
            d[a, b] = d[b, a] = min(d[a, b], float(radius))
    for k in refs:
        for i in refs:
            for j in refs:
                if d[i, k] + d[k, j] < d[i, j]:
                    d[i, j] = d[i, k] + d[k, j]
    by_ref = {c.ref: c for c in graph.components}
    size = {r: max(by_ref[r].courtyard) for r in refs}
    cap = max(d[i, j] + (size[i] + size[j]) / 2 for i in refs for j in refs) + CAP_EDGE_MARGIN_MM
    return None if math.isinf(cap) else math.ceil(cap * 4 - 1e-9) / 4


def outline_sizes(graph, constraints, block):
    """(sizes, cap): ``aspect_sizes`` outlines with dimensions capped, duplicates dropped."""
    from pnr.hier.blocks import aspect_sizes
    cap = outline_cap(graph, constraints, block)
    sizes, seen = [], set()
    for w, h, u, a in aspect_sizes(graph, block):
        if cap:
            w, h = min(w, cap), min(h, cap)
        if (w, h) not in seen:
            seen.add((w, h))
            sizes.append((w, h, u, a))
    return sizes, cap


def stratified_order(recs):
    """[(round, rec)] visiting (utilisation, aspect) strata round-robin.

    Within a stratum layouts go cheapest first (area, then port debt); strata are
    visited smallest-footprint first. Round k holds the k-th layout of every
    stratum that still has one, so a capped or early-stopped budget covers all
    outline shapes evenly instead of only the most compact ones.
    """
    strata = {}
    for r in sorted(recs, key=lambda r: (r['area'], r.get('port_debt_mm', math.inf))):
        strata.setdefault((r['utilisation'], r['aspect']), []).append(r)
    if _power_first():
        # Power-first: within a stratum the best power-stage placement band goes first.
        power_bands(recs)
        for v in strata.values():
            v.sort(key=lambda r: (_num((r.get('power_quality') or {}).get('q_band')), r['area'],
                                  r.get('port_debt_mm', math.inf)))
    order = []
    for k in range(max(map(len, strata.values()), default=0)):
        order += [(k, v[k]) for v in strata.values() if k < len(v)]
    return order


def schedule(order, run, repeats=1, parallel=1, enough=math.inf, report=None):
    """Evaluate ``order`` ([(round, rec)]) with ``run(rec, repeat)``, ``repeats`` times each.

    At most ``parallel`` runs at once; the repeats of one layout are issued
    together. Stop rule: once ``enough`` layouts are complete no layout of a later
    round starts (the running round is finished, keeping strata balanced).
    ``report(result, aggregate_or_None, evaluated)`` sees every finished run.
    Returns the aggregated layout results in completion order.
    """
    jobs = [(i, rnd, k) for i, (rnd, _) in enumerate(order) for k in range(repeats)]
    runs, evaluated, complete, last_round, slots = {}, [], 0, -1, max(1, parallel)
    with cf.ThreadPoolExecutor(slots) as pool:
        running = {}
        while jobs or running:
            while jobs and len(running) < slots:
                i, rnd, k = jobs[0]
                if k == 0 and rnd > last_round and complete >= enough:
                    jobs = []
                    break
                jobs.pop(0)
                last_round = rnd
                running[pool.submit(run, order[i][1], k)] = (i, k)
            if not running:
                break
            done, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
            for f in sorted(done, key=running.get):
                i, k = running.pop(f)
                try:
                    r = f.result()
                except Exception as error:
                    r = dict(order[i][1], stage='native', repeat=k, status='failed', objective=None,
                             error=repr(error))
                slot = runs.setdefault(i, [None] * repeats)
                slot[k] = r
                agg = aggregate(slot) if all(x is not None for x in slot) else None
                if agg is not None:
                    evaluated.append(agg)
                    complete += is_complete(agg)
                if report:
                    report(r, agg, evaluated)
    return evaluated


def write_library(d, names, tid, evaluated, **extra):
    """Rank ``evaluated`` and replace ``d/library.json`` atomically (tmp + os.replace)."""
    band = None
    if _power_first() and os.environ.get('PNR_RANK_OPEN_BAND') == 'auto' and extra.get('repeats', 1) >= 2:
        band = open_band(evaluated)
        extra = dict(extra, open_band=band)
    ranked = sorted((x for x in evaluated if x['status'] == 'ok'), key=lambda x: rank_key(x, band))
    doc = dict(blocks=names, template_id=tid,
               ranked=[dict(x, missing=x['objective'][5] + x['objective'][0]) for x in ranked],
               counts=dict(evaluated=len(evaluated), ok=len(ranked), complete=sum(map(is_complete, ranked))),
               **extra)
    tmp = Path(d) / ('.library.json.%d.tmp' % os.getpid())
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, Path(d) / 'library.json')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--inputs', type=Path, required=True)
    ap.add_argument('--constraints', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--block', action='append', default=[])
    ap.add_argument('--seeds', type=int, default=3)
    ap.add_argument('--iters', type=int, default=600)
    ap.add_argument('--seconds', type=int, default=300, help='native budget per block evaluation')
    ap.add_argument('--procs', type=int, default=6, help='parallel placements')
    ap.add_argument('--parallel', type=int, default=4, help='parallel native evaluations')
    ap.add_argument('--workers', type=int, default=1, help='route workers per native evaluation')
    ap.add_argument('--enough', type=int, default=4,
                    help='stop a template at the end of the stratum round reaching this many complete layouts')
    ap.add_argument('--max-native', type=int, default=0,
                    help='cap layouts per template, taken in stratum-balanced order (0 = all legal)')
    ap.add_argument('--repeats', type=int, default=1,
                    help='native evaluations per layout (distinct dirs); worst guard terms, mean subwidth')
    a = ap.parse_args(argv)
    if a.repeats < 1:
        ap.error('--repeats must be >= 1')
    from pnr.mc.halving import _load
    from pnr.hier.blocks import extract_blocks
    from pnr.hier.synth import _template_id
    graph, constraints, rules = _load(a.inputs, a.constraints)
    blocks = extract_blocks(graph, constraints)
    templates = {}
    for b in blocks:
        templates.setdefault(b.template, []).append(b)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'blocks.json').write_text(json.dumps([b.to_dict() for b in blocks], indent=2))
    selected = [(tid, [b.name for b in m], m[0]) for tid, m in
                ((_template_id(t), m) for t, m in templates.items())
                if not a.block or {b.name for b in m} & set(a.block)]
    # Smallest blocks first: their libraries become usable early.
    selected.sort(key=lambda x: len(x[2].refs))
    for tid, names, rep in selected:
        d = a.out / tid
        d.mkdir(exist_ok=True)
        trials = d / 'trials.jsonl'
        sizes, cap = outline_sizes(graph, constraints, rep)
        jobs = [(str(a.inputs), str(a.constraints), names[0], size, seed, a.iters)
                for size in sizes for seed in range(a.seeds)]
        with cf.ProcessPoolExecutor(a.procs) as pool:
            placed = list(pool.map(_place, jobs))
        seen, legal = set(), []
        for rec in placed:
            with trials.open('a') as fh:
                fh.write(json.dumps(dict(rec, stage='place')) + '\n')
            if not rec.get('legal'):
                continue
            key = json.dumps([rec['width'], rec['height'],
                              sorted((k, round(v[0], 2), round(v[1], 2), v[2]) for k, v in rec['layout'].items())])
            if key in seen:
                continue
            seen.add(key)
            legal.append(rec)
        # Round-robin over (utilisation, aspect) strata so a capped budget covers every shape.
        order = stratified_order(legal)
        if a.max_native:
            order = order[:a.max_native]
        print(tid, names[0], 'placed', len(placed), 'legal', len(legal), 'order', len(order),
              'cap', cap, flush=True)

        def run(rec, k):
            return _native(a.inputs, a.constraints, rec, names, d, a.seconds, a.workers, a.repo, k)

        library_extra = {}
        if _power_first():
            from pnr.hier.blocks import sub_board
            from pnr.power_topology import PowerTopologyUnavailable, derive, summary
            try:
                library_extra['power_topology'] = summary(derive(*sub_board(graph, constraints, rules, rep,
                                                                            *sizes[0][:2])))
            except PowerTopologyUnavailable as error:
                library_extra['power_topology'] = dict(unavailable=str(error))

        def report(r, agg, evaluated):
            with trials.open('a') as fh:
                fh.write(json.dumps(r if a.repeats == 1 else dict(r, stage='native-repeat')) + '\n')
                if agg is not None and a.repeats > 1:
                    fh.write(json.dumps(agg) + '\n')
            print(tid, names[0], '%.1fx%.1f' % (r['width'], r['height']), 'seed', r['seed'],
                  *(('repeat', r.get('repeat')) if a.repeats > 1 else ()), r['status'], r.get('objective'),
                  *(('layout', agg['status'], agg.get('objective')) if agg is not None and a.repeats > 1 else ()),
                  flush=True)
            if agg is not None:
                write_library(d, names, tid, evaluated, repeats=a.repeats, outline_cap_mm=cap, **library_extra)

        schedule(order, run, a.repeats, a.parallel, a.enough, report)


if __name__ == '__main__':
    main()
