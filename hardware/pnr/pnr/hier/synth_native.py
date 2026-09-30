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
    """Stage-A worker. ``args``: (inputs, constraints, block, size, seed, iters[, pairs]);
    ``pairs`` ([[key_a, pad_a, key_b, pad_b, w]], block-local keys) is the
    pnr.feedback 'prior' attraction passed to place() as ``pair_weights``."""
    inputs, constraints_path, name, size, seed, iters, *extra = args
    pairs = extra[0] if extra else None
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
        weights = None
        if pairs:
            ref_of = {local_key(blk, c.address): c.ref for c in sg.components}
            weights = {(ref_of[ka], pa, ref_of[kb], pb): float(wt) for ka, pa, kb, pb, wt in pairs
                       if ka in ref_of and kb in ref_of}
            rec['pair_weights_n'] = len(weights)
        placed, report = place(sg, sc, seed=seed, iters=iters, orient=True, channel_rules=sr,
                               pair_weights=weights or None)
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


def _feedback():
    return os.environ.get('PNR_FEEDBACK') == '1'


def _loadavg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _num(value):
    return math.inf if value is None else value


def _rank_used():
    """PNR_LIBRARY_RANK_USED=1 (N-0001): rank by the used-extent area band."""
    return os.environ.get('PNR_LIBRARY_RANK_USED') == '1'


def _used_area(r):
    return _num((r.get('used') or {}).get('area_mm2'))


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
    stamp = {}
    if _feedback():
        # The code that runs this evaluation (hashed now, at launch), the layout it
        # was given (before nudges are merged) and the machine load: an import of
        # this record needs no tree lookup, dedup sees the input layout.
        from pnr.feedback.blocks import layout_key
        from pnr.feedback.signals import code_key
        stamp = dict(code=code_key(), input_layout_key=layout_key(rec), loadavg_start=_loadavg())
    results = []
    for name in names:
        blk = blocks[name]
        g2, c2, r2 = instance_board(graph, constraints, rules, blk, rec['layout'], rec['width'], rec['height'])
        doc = block_constraints_doc(doc0, [c.address for c in g2.components], rec['width'], rec['height'])
        tag = '%s-s%d-%gx%g' % (name.replace(':', '_').replace('.', '_'), rec['seed'], rec['width'], rec['height'])
        # Feedback children share their parent's seed and outline: the suffix keeps their dirs apart.
        tag += rec.get('tag_suffix', '')
        tag += '-r%d' % repeat if repeat else ''
        try:
            r = evaluate(Path(out) / 'native' / tag, Path(inputs), doc, g2, r2, seconds, workers,
                         Path(repo), live_lane=Path(out).parent.name + '/' + tag)
        except Exception as error:
            r = dict(status='failed', error=repr(error), traceback=traceback.format_exc()[-1500:])
        r['instance'] = name
        r['dir'] = str(Path(out) / 'native' / tag)
        if os.environ.get('PNR_SHOVE') == '1' and r.get('status') == 'ok':
            r['nudged'] = _nudged_layout(Path(r['dir']), blk, r.get('apron_mm', 0.0))
        if _feedback() and r.get('status') == 'ok':
            from pnr.feedback.blocks import block_key
            from pnr.feedback.signals import read_round
            r['fb'] = read_round(Path(r['dir']), key=block_key(blk))
        results.append(r)
    ok = all(r.get('status') == 'ok' for r in results)
    total = [sum(r['objective'][i] for r in results) for i in range(6)] if ok else None
    out = dict(rec, stage='native', repeat=repeat, instances=results, status='ok' if ok else 'failed',
               objective=total)
    if stamp:
        out.update(stamp, loadavg_end=_loadavg())
    if os.environ.get('PNR_SHOVE') == '1' and ok:
        # Accepted make-room nudges moved parts inside the routed block board; the
        # layout must carry them or hier.assemble finds the block non-rigid.
        merged, conflict = merge_nudges(rec['layout'], results)
        if merged:
            out['nudged'] = merged
            out['nudged_conflict'] = conflict
            if conflict:
                # Every instance of a template is posed from ONE layout: instances
                # that disagree (one nudged a part, another did not, or nudged it
                # elsewhere) cannot all be rigid with it, so the record is unusable.
                out.update(status='failed', error='nudged_conflict: ' + conflict)
            else:
                layout = dict(rec['layout'], **merged)
                bad = {}
                for name in names:
                    for kind, values in _layout_violations(graph, constraints, rules, blocks[name], rec['layout'],
                                                           layout, rec['width'], rec['height']).items():
                        bad[kind] = sorted(set(bad.get(kind, [])) | set(values))
                if bad:
                    out.update(status='failed', error='nudged layout breaks hard placement constraints: '
                               + json.dumps(bad, sort_keys=True))
                    out['nudged_violations'] = bad
                else:
                    out['layout'] = layout
                    if stamp:
                        out['input_layout'] = rec['layout']     # what the router was given (pre-nudge)
    if _power_first():
        hot = [r.get('hot_loops_open') for r in results]
        out['hot_loops_open'] = sum(hot) if ok and all(h is not None for h in hot) else None
    return out


def merge_nudges(layout, results):
    """``(merged, conflict)`` of the instances' nudged poses (PNR_SHOVE=1).

    Each instance's effective pose of a key is its nudged pose, else the shared
    layout pose. Any disagreement between instances, including one instance not
    nudging a part another nudged, or an instance whose nudges are unknown, is a
    conflict (a non-empty description); ``merged`` holds the agreed poses."""
    keys = sorted({key for r in results for key in (r.get('nudged') or {})})
    if not keys:
        return {}, ''
    merged, problems = {}, []
    for key in keys:
        poses = []
        for r in results:
            nudged = r.get('nudged')
            if nudged is None:
                problems.append('%s: nudges of %s unknown' % (key, r.get('instance')))
                continue
            poses.append(nudged.get(key, layout.get(key)))
        same = lambda p, q: (p is not None and q is not None and math.dist(p[:2], q[:2]) <= 1e-6
                             and list(p[2:]) == list(q[2:]))
        if any(not same(pose, poses[0]) for pose in poses[1:]):
            problems.append('%s: instances disagree' % key)
        elif poses:
            merged[key] = poses[0]
    return merged, '; '.join(problems)


def _layout_violations(graph, constraints, rules, block, before, after, w, h):
    """Hard placement violations the nudged ``after`` layout adds to ``before``,
    in the block frame hierarchical placement uses (block rectangle, groups,
    rows, overlaps): {} when legal."""
    from pnr.hier.synth import instance_board
    from pnr.place.metrics import hard_violations

    def violations(layout):
        g2, c2, _ = instance_board(graph, constraints, rules, block, layout, w, h)
        return {kind: {json.dumps(v, sort_keys=True) for v in values}
                for kind, values in hard_violations(g2, c2).items()}
    old, new = violations(before), violations(after)
    return {kind: sorted(values - old.get(kind, set())) for kind, values in new.items() if values - old.get(kind, set())}


def _nudged_layout(round_dir, block, apron):
    """Layout poses (block-local, apron removed) of parts whose evaluated pose
    differs from the placed pose (PNR_SHOVE=1 make-room nudges); None when the
    placements cannot be read (the instance's nudges are unknown)."""
    import json
    from pnr.hier.synth import local_key
    try:
        placed = {c['ref']: c for c in json.loads((round_dir / 'placed.json').read_text())['components']}
        final = {c['ref']: c for c in json.loads((round_dir / 'evaluated-placed.json').read_text())['components']}
    except (OSError, ValueError, KeyError):
        return None
    out = {}
    for ref, c in final.items():
        p = placed.get(ref)
        if p is None or math.dist(p['pos'], c['pos']) <= 1e-6:
            continue
        out[local_key(block, c['address'])] = [c['pos'][0] - apron, c['pos'][1] - apron, c['rot'], c['side']]
    return out


def rank_key(r, band=None):
    """Full objective: unconnected, violations, blocked, ref failures, subwidth, pairs; then debt, area.

    With PNR_POWER_FIRST=1: unconnected, violations, open hot-loop links (routed),
    q_band and power crossings (placement), then the remaining terms; ``band``
    (PNR_RANK_OPEN_BAND=auto) coarsens unconnected to floor(unconnected / band).
    A layout without a routed power analysis ranks as if every hot loop were open.

    With PNR_LIBRARY_RANK_USED=1 the used-extent area band ``used_band`` (stamped
    by :func:`pnr.hier.extent.stamp_used` in ``load_library``; inf when absent) is
    inserted right before subwidth, and the used area breaks ties before w*h.
    """
    o = r.get('objective')
    if not o:
        return (math.inf,)
    if _rank_used():
        band_used = _num(r.get('used_band'))
        if _power_first():
            pq = r.get('power_quality') or {}
            return (o[5] // band if band else o[5], o[0], _num(r.get('hot_loops_open')), _num(pq.get('q_band')),
                    _num(pq.get('crossings')), o[1], o[2], band_used, o[3], o[4], r.get('port_debt_mm', math.inf),
                    _used_area(r), r.get('area', math.inf))
        return (o[5], o[0], o[1], o[2], band_used, o[3], o[4], r.get('port_debt_mm', math.inf), _used_area(r),
                r.get('area', math.inf))
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
    fb = ap.add_argument_group('routing feedback rounds (PNR_FEEDBACK=1; see pnr.feedback)')
    fb.add_argument('--rounds', '--generations', dest='generations', type=int, default=0,
                    help='feedback rounds after stage B: children of ranked parents (PULL, RAND control) plus '
                         "fresh samples ('prior' with failure-rate attraction, 'fresh'), ranked in one pool")
    fb.add_argument('--gen-parents', type=int, default=4, help='parents in round 1; round g takes ceil(P / 2^(g-1))')
    fb.add_argument('--gen-children', type=int, default=2, help='PULL children per parent (distinct movers)')
    fb.add_argument('--gen-rand', type=int, default=1, help='top parents that also get one RAND control child')
    fb.add_argument('--gen-fresh', type=int, default=2, help="fresh slots per round: prior, fresh, prior, ...")
    fb.add_argument('--gen-plateau', type=int, default=2, help='stop when the pool best is unchanged this many rounds')
    fb.add_argument('--round-budget', type=int, default=0, help='cap on evaluations per template per round (0 = none)')
    fb.add_argument('--max-move', type=float, default=3.0, help='PULL lattice radius (mm)')
    fb.add_argument('--lineage-cap', type=float, default=4.0, help='max distance of a mover from its round-0 pose (mm)')
    fb.add_argument('--fb-pair-weight', type=float, default=10.0,
                    help="'prior' attraction weight x failure rate (capped at 2x)")
    fb.add_argument('--gen-seed', type=int, default=0)
    fb.add_argument('--import-trials', action='append', default=[], type=Path,
                    help='round 0 from an earlier run: its native records (not re-evaluated) and stage-A layouts; '
                         'only templates with imported native records are selected (--block naming another is an '
                         'error)')
    fb.add_argument('--import-budget-mismatch', choices=['error', 'warn'], default='error')
    fb.add_argument('--import-code-mismatch', choices=['error', 'warn', 'rebase'], default='error',
                    help='imports evaluated by different (or unknown) evaluation code: refuse; import as is with a '
                         'warning; or rebase: re-evaluate them unchanged under this code as round 0 (arm rebase) and '
                         'rank only same-code evaluations')
    fb.add_argument('--import-rebase', type=int, default=0,
                    help='with --import-code-mismatch rebase: re-evaluate the best N stale imports per template by '
                         'their recorded rank (0 = all)')
    a = ap.parse_args(argv)
    if a.repeats < 1:
        ap.error('--repeats must be >= 1')
    if a.generations < 0:
        ap.error('--rounds must be >= 0')
    if a.generations and not _feedback():
        ap.error('--rounds/--generations > 0 needs PNR_FEEDBACK=1')
    if a.import_trials and not a.generations:
        ap.error('--import-trials needs --rounds > 0')
    if a.import_rebase < 0:
        ap.error('--import-rebase must be >= 0')
    from pnr.mc.halving import _load
    from pnr.hier.blocks import extract_blocks
    from pnr.hier.synth import _template_id
    graph, constraints, rules = _load(a.inputs, a.constraints)
    blocks = extract_blocks(graph, constraints)
    templates = {}
    for b in blocks:
        templates.setdefault(b.template, []).append(b)
    everything = [(tid, [b.name for b in m], m[0]) for tid, m in ((_template_id(t), m) for t, m in templates.items())]
    selected = [x for x in everything if not a.block or set(x[1]) & set(a.block)]
    imports = {}
    if a.generations and a.import_trials:
        from pnr.feedback.signals import current_key
        imports = _load_imports(a.import_trials, {tid: names for tid, names, _ in everything},
                                current_key('block', a.seconds, a.inputs), a.import_budget_mismatch,
                                {b.name: b for b in blocks}, a.import_code_mismatch)
        # An import run names the templates of the experiment: a selected template
        # without imported layouts would silently run a whole new stage A/B first.
        have = {tid for tid, slot in imports.items() if slot['native']}
        if a.block:
            bare = [(tid, names[0]) for tid, names, _ in selected if tid not in have]
            if bare:
                ap.error('--import-trials: selected templates without imported native records: %s (name only '
                         'imported templates with --block)' % ', '.join('%s %s' % x for x in bare))
        else:
            skipped = [names[0] for tid, names, _ in selected if tid not in have]
            selected = [x for x in selected if x[0] in have]
            if skipped:
                print('import mode: templates without imported native records are not run:', ', '.join(skipped),
                      flush=True)
        if not selected:
            ap.error('--import-trials: no selected template has imported native records')
        errors = [e for tid, _, _ in selected for e in imports[tid]['errors']]
        if errors:
            ap.error('imports do not match this run:\n  ' + '\n  '.join(errors[:20]))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'blocks.json').write_text(json.dumps([b.to_dict() for b in blocks], indent=2))
    # Smallest blocks first: their libraries become usable early.
    selected.sort(key=lambda x: len(x[2].refs))
    for tid, names, rep in selected:
        if a.generations:
            _feedback_template(a, graph, constraints, rules, blocks, tid, names, rep, imports.get(tid))
            continue
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


# ------------------------------------------------------------------ feedback rounds

ARM_LETTER = dict(pull='c', rand='r', prior='p', fresh='f', rebase='b')


def _read_jsonl(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _import_code(r, router, cache):
    """observed_code of an imported layout record: its stamped code, else the one tree
    all its instance dirs name (disagreeing or unknown instances make it unknown)."""
    from pnr.feedback.signals import observed_code
    if r.get('code'):
        return observed_code(None, router, stamped=r['code'])
    obs = [observed_code(i['dir'], router, cache=cache) for i in r.get('instances') or [] if i.get('dir')]
    if not obs:
        return dict(code=None, tree=None, files=None, reason='no instance dirs')
    codes = {o['code'] for o in obs}
    if len(codes) == 1:
        return obs[0]
    unknown = [o for o in obs if o['code'] is None]
    return unknown[0] if unknown else dict(code=None, tree=None, files=None, reason='instances disagree')


def _load_imports(paths, templates, current, policy, blocks, code_policy='error'):
    """{tid: dict(place, native, sources, warnings, errors)} of ``--import-trials``.

    Native layout records (any status) become round 0; their feedback is read
    from the instance dirs when the run predates PNR_FEEDBACK. Router,
    power-first, fab profile (and budget unless 'warn') must match this run.
    The evaluation code must match too unless ``code_policy`` is 'warn', or
    'rebase': then the record is marked ``stale_code`` (re-evaluated under this
    code before round 1 and never ranked itself)."""
    from pnr.feedback.blocks import layout_fb, tag_of
    from pnr.feedback.signals import PNR_ROOT, check_code, check_import, eval_code_files, observed_key
    by_block = {n: tid for tid, names in templates.items() for n in names}
    out, cache = {}, {}
    current_files = eval_code_files(PNR_ROOT, current['router'])
    for path in paths:
        for r in _read_jsonl(path):
            tid = by_block.get(r.get('block'))
            if tid is None:
                continue
            slot = out.setdefault(tid, dict(place=[], native=[], sources=[], warnings=[], errors=[]))
            if str(path) not in slot['sources']:
                slot['sources'].append(str(path))
            if r.get('stage') == 'place':
                slot['place'].append(r)
            elif r.get('stage') == 'native':
                # every import is round 0 of this run (a child of an imported feedback run keeps its
                # tag and lineage, but its round belongs to the source run)
                r = dict(r, imported=str(path), gen=0, source_gen=r.get('gen', 0))
                fb = layout_fb(r, blocks)
                if fb.get('missing'):
                    slot['warnings'].append('%s: feedback missing' % tag_of(r))
                else:
                    errs, warns = check_import(observed_key(fb, stage='block', power_first='power_quality' in r),
                                               current, policy)
                    slot['errors'] += ['%s %s: %s' % (path, tag_of(r), e) for e in errs]
                    slot['warnings'] += ['%s: %s' % (tag_of(r), w) for w in warns]
                if r.get('status') == 'ok':
                    # Failed imports are never ranked (they only mark layouts as tried).
                    obs = _import_code(r, current['router'], cache)
                    errs, warns, stale = check_code(obs, current.get('code'), current_files, code_policy)
                    slot['errors'] += ['%s %s: %s' % (path, tag_of(r), e) for e in errs]
                    slot['warnings'] += ['%s: %s' % (tag_of(r), w) for w in warns]
                    r['import_code'] = dict(code=obs.get('code'), tree=obs.get('tree'), reason=obs.get('reason'))
                    if stale:
                        r['stale_code'] = True
                slot['native'].append(r)
    return out


def _template_q_ref(recs):
    refs = [(r.get('power_quality') or {}).get('q_ref') for r in recs]
    refs = [q for q in refs if q]
    return min(refs) if refs else None


def _child_metrics(rec, graph, constraints, rules, block, q_ref):
    """Port debt and (PNR_POWER_FIRST=1) placement power quality of a child layout,
    banded against the template's q_ref like stage A."""
    from pnr.hier.synth import instance_board, _port_debt
    g2, c2, r2 = instance_board(graph, constraints, rules, block, rec['layout'], rec['width'], rec['height'])
    rec['port_debt_mm'] = _port_debt(g2, block.external_nets, rec['width'], rec['height'])
    if _power_first():
        pq = _placement_power_quality(g2, c2, r2, g2)
        if pq is not None:
            _stamp_band(pq, q_ref)
            rec['power_quality'] = pq


def _stamp_band(pq, q_ref):
    if pq and pq.get('q_place') is not None:
        ref = q_ref or pq['q_place']
        pq['q_ref'] = ref
        pq['q_band'] = math.floor(pq['q_place'] / (Q_BAND * ref)) if ref > 0 else 0


def _live(recs):
    """Records evaluated by this run's code (stale-code imports only feed rebase and dedup)."""
    return [x for x in recs if not x.get('stale_code')]


def _pool_order(band):
    """Pool ranking of the feedback rounds: rank_key, ties by tag (resume-stable)."""
    from pnr.feedback.blocks import tag_of
    return lambda x: (rank_key(x, band), tag_of(x))


def _input_keys(recs, place_by_tag):
    """Layout keys of what was actually handed to the router: stamped input keys,
    the stage-A layout of un-suffixed (stage B / imported) records, and the
    evaluated (nudged) layout itself."""
    from pnr.feedback.blocks import base_tag, layout_key
    keys = set()
    for x in recs:
        if x.get('layout'):
            keys.add(layout_key(x))
        if x.get('input_layout_key'):
            keys.add(x['input_layout_key'])
        src = place_by_tag.get(base_tag(x)) if not x.get('tag_suffix') and x.get('block') else None
        if src is not None:
            keys.add(layout_key(src))
    return keys


def _rebase_plan(a, stale, place_by_tag, rkey_s, graph, constraints, rules, rep, q_ref):
    """Layout records re-evaluating stale-code imports unchanged under this code.

    The input is what the import's router was given (``input_layout``, else the
    stage-A layout of its seed/outline, else its evaluated layout), so the rebase
    differs from the import only in the evaluation code (and run-to-run noise)."""
    from pnr.feedback import blocks as fbb
    pick = sorted(stale, key=lambda x: (rank_key(x), fbb.tag_of(x)))
    if a.import_rebase:
        pick = pick[:a.import_rebase]
    out, seen = [], set()
    for x in pick:
        src = place_by_tag.get(fbb.base_tag(x)) if not x.get('tag_suffix') else None
        if x.get('input_layout'):
            layout, source = x['input_layout'], 'input'
        elif src is not None:
            layout, source = src['layout'], 'stage-A'
        else:
            layout, source = x['layout'], 'evaluated'
        rec = {k: x[k] for k in ('block', 'width', 'height', 'utilisation', 'aspect', 'seed', 'area')}
        suffix = '-g0%s-%s' % (ARM_LETTER['rebase'], fbb.layout_sha(layout))
        rec.update(legal=True, layout={k: list(v) for k, v in layout.items()}, gen=0, arm='rebase',
                   parent=dict(tag=fbb.tag_of(x), layout_sha=fbb.layout_sha(x['layout'])), lineage_depth=0,
                   router_key=rkey_s, tag_suffix=suffix,
                   op_detail=dict(arm='rebase', layout_source=source, import_objective=x.get('objective'),
                                  import_code=(x.get('import_code') or {}).get('code')))
        rec['tag'] = fbb.base_tag(rec) + suffix
        if rec['tag'] in seen:
            continue
        seen.add(rec['tag'])
        _child_metrics(rec, graph, constraints, rules, rep, q_ref)
        out.append(rec)
    return out


def _feedback_template(a, graph, constraints, rules, blocks, tid, names, rep, imported):
    """Round 0 (stage A/B, or imports [+ rebase]), then ``a.generations`` feedback rounds.

    Every round ranks the pool of all same-code evaluated layouts of earlier
    rounds with :func:`rank_key` (ties by tag); parents are the top
    ceil(P / 2^(g-1)) with a PULL child; children, RAND controls (each matched to
    its parent's first PULL child) and fresh samples are evaluated like stage B
    and join the pool (parents are never removed). Imports evaluated by other
    code (``stale_code``, --import-code-mismatch rebase) are re-evaluated
    unchanged first and never ranked themselves. Children are a pure function of
    the records, the CLI and sha256 seeds, and tags already in trials.jsonl are
    not re-run, so a resume replays the same plan.
    """
    from pnr.feedback import blocks as fbb
    from pnr.feedback.moves import pull_children, rand_child, seed_for
    from pnr.feedback.signals import current_key, key_string
    from pnr.feedback.table import build
    d = a.out / tid
    d.mkdir(exist_ok=True)
    trials = d / 'trials.jsonl'
    existing = _read_jsonl(trials)
    rkey = current_key('block', a.seconds, a.inputs)
    rkey_s = key_string(rkey)
    by_name = {b.name: b for b in blocks}
    sizes, cap = outline_sizes(graph, constraints, rep)

    def append(rec):
        with trials.open('a') as fh:
            fh.write(json.dumps(rec) + '\n')

    library_extra = dict(router_key=rkey_s)
    if _power_first():
        from pnr.hier.blocks import sub_board
        from pnr.power_topology import PowerTopologyUnavailable, derive, summary
        try:
            library_extra['power_topology'] = summary(derive(*sub_board(graph, constraints, rules, rep,
                                                                        *sizes[0][:2])))
        except PowerTopologyUnavailable as error:
            library_extra['power_topology'] = dict(unavailable=str(error))
    if imported:
        library_extra['imports'] = dict(sources=imported['sources'], native=len(imported['native']),
                                        stale_code=sum(bool(x.get('stale_code')) for x in imported['native']),
                                        code_policy=a.import_code_mismatch,
                                        warnings=sorted(set(imported['warnings']))[:20])
        for w in sorted(set(imported['warnings']))[:10]:
            print(tid, names[0], 'import warning:', w, flush=True)

    # ---- round 0: stage A (or imported layouts)
    if imported:
        stage_a = imported['place']
    else:
        stage_a = [r for r in existing if r.get('stage') == 'place']
        if not stage_a:
            jobs = [(str(a.inputs), str(a.constraints), names[0], size, seed, a.iters)
                    for size in sizes for seed in range(a.seeds)]
            with cf.ProcessPoolExecutor(a.procs) as pool:
                stage_a = list(pool.map(_place, jobs))
            for rec in stage_a:
                append(dict(rec, stage='place'))
    seen, legal = set(), []
    for rec in stage_a:
        if rec.get('legal') and rec.get('layout'):
            key = fbb.layout_key(rec)
            if key not in seen:
                seen.add(key)
                legal.append(rec)
    place_by_tag = {}
    for rec in legal:
        place_by_tag.setdefault(fbb.base_tag(rec), rec)
    order = stratified_order(legal)
    done = {fbb.tag_of(r): r for r in existing if r.get('stage') == 'native'}
    evaluated, gen_summary = [], []

    def write():
        live = _live(evaluated)
        write_library(d, names, tid, live, repeats=a.repeats, outline_cap_mm=cap, generations=gen_summary,
                      stale_imports=len(evaluated) - len(live), **library_extra)

    def run(rec, k):
        return _native(a.inputs, a.constraints, rec, names, d, a.seconds, a.workers, a.repo, k)

    def report(r, agg, _):
        append(r if a.repeats == 1 else dict(r, stage='native-repeat'))
        if agg is not None and a.repeats > 1:
            append(agg)
        print(tid, names[0], '%.1fx%.1f' % (r['width'], r['height']), 'seed', r['seed'],
              *(('gen', r['gen'], r['arm']) if r.get('arm') else ()), r['status'], r.get('objective'), flush=True)
        if agg is not None:
            evaluated.append(agg)
            write()

    if imported:
        evaluated.extend(imported['native'])
        print(tid, names[0], 'imported', len(imported['native']), 'layouts from', imported['sources'], flush=True)
        write()
        stale = [x for x in imported['native'] if x.get('stale_code') and x.get('status') == 'ok'
                 and x.get('objective')]
        if stale:
            # ---- round 0 rebase: the imports' own inputs under this code
            q_ref0 = _template_q_ref(list(imported['native']) + legal)
            planned = _rebase_plan(a, stale, place_by_tag, rkey_s, graph, constraints, rules, rep, q_ref0)
            todo = []
            for rec in planned:
                if rec['tag'] in done:
                    evaluated.append(done[rec['tag']])
                else:
                    todo.append((0, rec))
            print(tid, names[0], 'rebase', len(planned), 'of', len(stale), 'stale-code imports', 'resumed',
                  len(planned) - len(todo), flush=True)
            schedule(todo, run, a.repeats, a.parallel, math.inf, report)
            by_tag = {fbb.tag_of(x): x for x in imported['native']}
            pairs_ = []
            for x in evaluated:
                if x.get('arm') == 'rebase' and x.get('status') == 'ok' and x.get('objective'):
                    old = by_tag.get((x.get('parent') or {}).get('tag'))
                    if old is not None:
                        pairs_.append([fbb.tag_of(old), fbb.missing(old), fbb.missing(x)])
            pairs_.sort()
            library_extra['rebase'] = dict(
                planned=len(planned), evaluated=len(pairs_), pairs=pairs_,
                delta=[p[2] - p[1] for p in pairs_],
                complete_import=sum(p[1] == 0 for p in pairs_), complete_rebase=sum(p[2] == 0 for p in pairs_))
            write()
    else:
        stage_b = order[:a.max_native] if a.max_native else order
        todo = []
        for rnd, rec in stage_b:
            if fbb.tag_of(rec) in done:
                evaluated.append(done[fbb.tag_of(rec)])
            else:
                todo.append((rnd, rec))
        print(tid, names[0], 'placed', len(stage_a), 'legal', len(legal), 'order', len(stage_b),
              'resumed', len(stage_b) - len(todo), 'cap', cap, flush=True)
        schedule(todo, run, a.repeats, a.parallel, a.enough - sum(map(is_complete, evaluated)), report)
        write()

    # ---- feedback rounds
    band = None
    if _power_first() and os.environ.get('PNR_RANK_OPEN_BAND') == 'auto' and a.repeats >= 2:
        band = open_band(_live(evaluated))
    history = []
    for g in range(1, a.generations + 1):
        pool = [x for x in _live(evaluated) if x.get('gen', 0) < g]
        ranked = sorted((x for x in pool if x.get('status') == 'ok' and x.get('objective')), key=_pool_order(band))
        tags = {fbb.tag_of(x): x for x in pool}
        complete = sum(map(is_complete, ranked))
        history.append(fbb.tag_of(ranked[0]) if ranked else None)
        summary = dict(gen=g, pool=len(pool), complete_before=complete, best_before=history[-1],
                       best_objective_before=ranked[0]['objective'] if ranked else None)
        if complete >= a.enough:
            gen_summary.append(dict(summary, stop='enough'))
            break
        if a.gen_plateau and len(history) > a.gen_plateau and len(set(history[-a.gen_plateau - 1:])) == 1:
            gen_summary.append(dict(summary, stop='plateau'))
            break
        nodes = [dict(id=t, parent=(x.get('parent') or {}).get('tag'), fb=fbb.layout_fb(x, by_name))
                 for t, x in tags.items()]
        table = build(nodes, scope=tid, router=rkey['router'])
        (d / ('feedback-table.g%d.json' % g)).write_text(json.dumps(
            dict(table.to_json(), router_key=rkey_s, sha=table.sha()), indent=1))
        q_ref = _template_q_ref(list(pool) + legal)
        # every layout already handed to a router (any code, any round), before and after nudges
        known = _input_keys(evaluated, place_by_tag)
        planned_keys = set()
        children, parents = [], []

        def root_of(x):
            seen_ = set()
            while (x.get('parent') or {}).get('tag') in tags and fbb.tag_of(x) not in seen_:
                seen_.add(fbb.tag_of(x))
                x = tags[x['parent']['tag']]
            return x

        def child(parent, board, kid, arm, j, **extra):
            layout = fbb.child_layout(parent['layout'], board, kid['poses'])
            rec = {k: parent[k] for k in ('block', 'width', 'height', 'utilisation', 'aspect', 'seed', 'area')}
            suffix = '-g%d%s%d-%s' % (g, ARM_LETTER[arm], j, fbb.layout_sha(parent['layout']))
            rec.update(legal=True, layout=layout, gen=g, arm=arm,
                       parent=dict(tag=fbb.tag_of(parent), layout_sha=fbb.layout_sha(parent['layout'])),
                       root=fbb.tag_of(root_of(parent)), lineage_depth=parent.get('lineage_depth', 0) + 1,
                       op_detail=kid['detail'], moved_mm=fbb.displacement(parent['layout'], layout),
                       router_key=rkey_s, tag_suffix=suffix, **extra)
            rec['tag'] = fbb.base_tag(rec) + suffix
            _child_metrics(rec, graph, constraints, rules, rep, q_ref)
            return rec

        want = math.ceil(a.gen_parents / 2 ** (g - 1))
        for x in ranked:
            if len(parents) >= want:
                break
            fb = fbb.layout_fb(x, by_name)
            if fb.get('missing') or fb.get('router') != rkey['router'] or not fb.get('conns'):
                continue
            board = fbb.block_board(graph, constraints, rules, by_name, names, x['layout'], x['width'],
                                    x['height'], origin_layout=root_of(x)['layout'],
                                    q_ref=(x.get('power_quality') or {}).get('q_ref') or q_ref,
                                    power_first=_power_first())
            def seen_(poses, x=x, board=board):
                key = fbb.layout_key(dict(x, layout=fbb.child_layout(x['layout'], board, poses)))
                return key in known or key in planned_keys
            kids = pull_children(board, fb, table, fbb.tag_of(x), n=a.gen_children, max_move=a.max_move,
                                 lineage_cap=a.lineage_cap, skip=seen_)
            if not kids:
                continue
            parents.append(fbb.tag_of(x))
            pulls = [child(x, board, kid, 'pull', j, k=j) for j, kid in enumerate(kids)]
            children += pulls
            planned_keys.update(fbb.layout_key(c) for c in children)
            if len(parents) <= a.gen_rand:
                # the control is matched to the FIRST PULL child (k=0): same parent, same displacement
                first = kids[0]['detail']
                kid = rand_child(board, seed_for(a.gen_seed, tid, g, fbb.tag_of(x), 'rand'), first['move_mm'],
                                 rotate=first['rot'] is not None, lineage_cap=a.lineage_cap, skip=seen_)
                if kid is not None:
                    children.append(child(x, board, kid, 'rand', 0, matched=pulls[0]['tag']))
                    planned_keys.add(fbb.layout_key(children[-1]))
        # fresh slots: 'prior' re-places a stage-A (outline, seed) with the pooled failure-rate
        # attraction, 'fresh' is that same stage-A layout as sampled (or a new seed when none is left).
        # A stage-A (seed, outline) already handed to a router in any form is not spare.
        used = {fbb.base_tag(x) for x in evaluated}
        spare = [rec for _, rec in order if fbb.base_tag(rec) not in used and fbb.layout_key(rec) not in known]
        pairs = [[ka, pa, kb, pb, w] for (ka, pa, kb, pb), w in
                 sorted(table.pair_weights(weight=a.fb_pair_weight, cap=2 * a.fb_pair_weight).items())]
        jobs, fresh, prior_skipped = [], [], 0
        for j in range(a.gen_fresh):
            arm, src_i = ('prior', 'fresh')[j % 2], j // 2
            if arm == 'prior' and not pairs:
                # Without pair weights the placer reproduces the unweighted sample: not a prior.
                prior_skipped += 1
                continue
            src = spare[src_i] if src_i < len(spare) else None
            if src is not None:
                size, seed = (src['width'], src['height'], src['utilisation'], src['aspect']), src['seed']
            else:
                size = sizes[seed_for(a.gen_seed, tid, g, 'size', src_i) % len(sizes)]
                seed = 1000 + seed_for(a.gen_seed, tid, g, 'seed', src_i) % 100000
            if arm == 'fresh' and src is not None:
                fresh.append((arm, j, dict(src)))
            else:
                jobs.append((arm, j, (str(a.inputs), str(a.constraints), names[0], size, seed, a.iters,
                                      pairs if arm == 'prior' else None)))
        placed_prev = {(r.get('gen'), r.get('job')): r for r in existing if r.get('stage') == 'gen-place'}
        todo_jobs = [(arm, j, job) for arm, j, job in jobs if (g, '%s%d' % (arm, j)) not in placed_prev]
        if todo_jobs:
            with cf.ProcessPoolExecutor(max(1, min(a.procs, len(todo_jobs)))) as pool_:
                for (arm, j, _), rec in zip(todo_jobs, pool_.map(_place, [job for _, _, job in todo_jobs])):
                    rec = dict(rec, stage='gen-place', gen=g, job='%s%d' % (arm, j), arm=arm)
                    append(rec)
                    placed_prev[(g, rec['job'])] = rec
        for arm, j, _ in jobs:
            rec = placed_prev.get((g, '%s%d' % (arm, j)))
            if rec and rec.get('legal') and rec.get('layout'):
                fresh.append((arm, j, rec))
        for arm, j, src in sorted(fresh, key=lambda f: f[1]):
            rec = {k: v for k, v in src.items() if k not in ('stage', 'job', 'place_seconds')}
            suffix = '-g%d%s%d-%s' % (g, ARM_LETTER[arm], j, fbb.layout_sha(rec['layout']))
            _stamp_band(rec.get('power_quality'), q_ref)
            rec.update(gen=g, arm=arm, parent=None, lineage_depth=0, router_key=rkey_s, tag_suffix=suffix,
                       source=fbb.base_tag(rec),
                       op_detail=dict(arm=arm, pair_weights_n=len(pairs) if arm == 'prior' else 0))
            rec['tag'] = fbb.base_tag(rec) + suffix
            children.append(rec)
        planned, dropped = [], 0
        for c in children:
            key = fbb.layout_key(c)
            if key in known:
                dropped += 1
                continue
            known.add(key)
            planned.append(c)
        if a.round_budget:
            planned = planned[:a.round_budget]
        todo = []
        for c in planned:
            if c['tag'] in done:
                evaluated.append(done[c['tag']])
            else:
                todo.append((0, c))
        arms = {}
        for c in planned:
            arms[c['arm']] = arms.get(c['arm'], 0) + 1
        print(tid, names[0], 'round', g, 'parents', parents, 'children', arms, 'duplicates', dropped,
              'prior skipped (no pair weights)' * bool(prior_skipped), 'resumed', len(planned) - len(todo),
              'table', table.sha(), flush=True)
        schedule(todo, run, a.repeats, a.parallel, math.inf, report)
        mine = [x for x in evaluated if x.get('gen') == g]
        deltas = {}
        for x in mine:
            parent = tags.get((x.get('parent') or {}).get('tag'))
            if x.get('status') != 'ok' or not x.get('objective'):
                continue
            entry = deltas.setdefault(x['arm'], dict(n=0, missing=[], delta=[]))
            entry['n'] += 1
            entry['missing'].append(x['objective'][5] + x['objective'][0])
            if parent is not None and parent.get('objective'):
                entry['delta'].append(x['objective'][5] + x['objective'][0]
                                      - parent['objective'][5] - parent['objective'][0])
        after = sorted((x for x in _live(evaluated) if x.get('status') == 'ok' and x.get('objective')),
                       key=_pool_order(band))
        gen_summary.append(dict(summary, parents=parents, children=arms, duplicates=dropped,
                                prior_skipped=prior_skipped,
                                evaluated=len(mine), table_sha=table.sha(), table_evals=table.n_evals,
                                floor=table.floor, pair_weights=len(pairs), arms=deltas,
                                best_after=fbb.tag_of(after[0]) if after else None,
                                best_objective_after=after[0]['objective'] if after else None,
                                complete_after=sum(map(is_complete, after))))
        write()
    write()


if __name__ == '__main__':
    main()
