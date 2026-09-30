"""Routed power-path quality of a native block run (recorded behind ``PNR_POWER_FIRST=1``).

One KiCad-python read of the routed board (``python3 -m pnr.hier.power_quality
dump <board> <out.json>``, :func:`dump_main`) and a pure-Python per-net copper
graph: tracks with their width, vias, pads joined by KiCad hit tests or copper
overlap, overlapping pads of one footprint, and plane zones (vias and pads that
KiCad reports inside a zone's fill). From it:

* per-net unconnected counts, which must equal the final DRC counts. On a
  mismatch the analysis is marked invalid and every hot-loop link counts as
  open, so a broken analysis never helps a layout;
* ``hot_loops_open``: hot-loop links (best member choice) without a copper path;
* per hot loop: routed link length, vias, completeness;
* power-net vias, F.Cu share of power-net track length, routed/MST on fully
  connected power nets;
* ``E_pow``: over hot-loop and series-path links, w_link x (outer length + inner
  length x t_outer/t_inner + vias x board thickness); an open link counts 3x its
  straight pad-group gap. Plane legs count their straight length.

Only ``hot_loops_open`` is a ranking key (:func:`pnr.hier.synth_native.rank_key`);
the rest is recorded until routed outcomes show it predicts something.
Module level is stdlib only: the dump runs under KiCad's interpreter.
"""
from __future__ import annotations

import heapq
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# PNR_KICAD_PYTHON overrides the KiCad python (headless bundle); unset keeps the old default.
KI_PY = os.environ.get('PNR_KICAD_PYTHON', '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3')
OPEN_FACTOR = 3.0
TOL = 1e-3


# ------------------------------------------------------------------ KiCad dump

def dump_main(src, dst):
    """Dump pads, tracks, vias and zones of a routed board to JSON (KiCad python only)."""
    import pcbnew

    def mm(v):
        return pcbnew.ToMM(v)
    b = pcbnew.LoadBoard(str(src))
    zones = [z for z in b.Zones() if not z.GetIsRuleArea()]
    out = dict(board=str(src), footprints=[], pads=[], tracks=[], vias=[], zones=[],
               copper_layers=[b.GetLayerName(l) for l in b.GetEnabledLayers().CuStack()])

    def zone_hits(net, pt, layers=None):
        hits = []
        for zi, z in enumerate(zones):
            if z.GetNetname() != net:
                continue
            for lid in z.GetLayerSet().CuStack():
                if layers is not None and lid not in layers:
                    continue
                try:
                    if z.HitTestFilledArea(lid, pt, 0):
                        hits.append([zi, b.GetLayerName(lid)])
                except Exception:
                    pass
        return hits
    pads = []
    for fp in b.GetFootprints():
        p = fp.GetPosition()
        out['footprints'].append(dict(ref=fp.GetReference(), x=mm(p.x), y=mm(p.y), rot=fp.GetOrientationDegrees(),
                                      side='bottom' if fp.IsFlipped() else 'top'))
        for pad in fp.Pads():
            q = pad.GetPosition()
            bb = pad.GetBoundingBox()
            smd = pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
            lids = list(pad.GetLayerSet().CuStack())
            pads.append((fp.GetReference(), pad.GetNumber(), pad.GetNetname(), pad))
            out['pads'].append(dict(ref=fp.GetReference(), name=pad.GetNumber(), net=pad.GetNetname(),
                                    x=mm(q.x), y=mm(q.y),
                                    bbox=[mm(bb.GetX()), mm(bb.GetY()), mm(bb.GetRight()), mm(bb.GetBottom())],
                                    smd=smd, layers=[b.GetLayerName(l) for l in lids],
                                    zone_hits=zone_hits(pad.GetNetname(), q, None if not smd else lids)))

    def pad_hits(net, pt, layer=None):
        return [[ref, name] for ref, name, pnet, pad in pads
                if pnet == net and (layer is None or pad.IsOnLayer(layer)) and pad.HitTest(pt, 0)]
    for t in b.GetTracks():
        net = t.GetNetname()
        if t.GetClass() == 'PCB_VIA':
            q = t.GetPosition()
            try:
                d = mm(t.GetWidth(pcbnew.F_Cu))
            except Exception:
                d = mm(t.GetWidth())
            out['vias'].append(dict(net=net, x=mm(q.x), y=mm(q.y), d=d, top=b.GetLayerName(t.TopLayer()),
                                    bottom=b.GetLayerName(t.BottomLayer()), pads=pad_hits(net, q),
                                    zone_hits=zone_hits(net, q)))
            continue
        s, e = t.GetStart(), t.GetEnd()
        out['tracks'].append(dict(net=net, layer=b.GetLayerName(t.GetLayer()), x1=mm(s.x), y1=mm(s.y),
                                  x2=mm(e.x), y2=mm(e.y), w=mm(t.GetWidth()), length=mm(t.GetLength()),
                                  arc=t.GetClass() == 'PCB_ARC', pads1=pad_hits(net, s, t.GetLayer()),
                                  pads2=pad_hits(net, e, t.GetLayer())))
    for zi, z in enumerate(zones):
        for lid in z.GetLayerSet().CuStack():
            try:
                poly = z.GetFilledPolysList(lid)
                area = poly.Area() / 1e12 if poly is not None else 0.0
            except Exception:
                area = None
            out['zones'].append(dict(index=zi, net=z.GetNetname(), layer=b.GetLayerName(lid), filled_area_mm2=area))
    Path(dst).write_text(json.dumps(out))


def read_board(board, *, timeout=180, ki_py=None):
    """One KiCad-python read of ``board``; returns the dump dict."""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'board.json'
        subprocess.run([ki_py or KI_PY, '-m', 'pnr.hier.power_quality', 'dump', str(board), str(out)],
                       env=env, check=True, capture_output=True, text=True, timeout=timeout)
        return json.loads(out.read_text())


# ------------------------------------------------------------------ copper graph

def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _project(a, b, q):
    vx, vy = b[0] - a[0], b[1] - a[1]
    l2 = vx * vx + vy * vy
    u = 0.0 if l2 == 0 else max(0.0, min(1.0, ((q[0] - a[0]) * vx + (q[1] - a[1]) * vy) / l2))
    p = (a[0] + u * vx, a[1] + u * vy)
    return u, p, _dist(p, q)


def _seg_rect(a, b, r):
    """Distance between segment ab and rectangle r = (x0, y0, x1, y1)."""
    def inside(p):
        return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]
    if inside(a) or inside(b):
        return 0.0
    corners = [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]
    for c, d in zip(corners, corners[1:] + corners[:1]):
        o = lambda p, q, s: (q[0] - p[0]) * (s[1] - p[1]) - (q[1] - p[1]) * (s[0] - p[0])
        if o(a, b, c) * o(a, b, d) < 0 and o(c, d, a) * o(c, d, b) < 0:
            return 0.0
    dx = lambda p: math.hypot(max(r[0] - p[0], 0, p[0] - r[2]), max(r[1] - p[1], 0, p[1] - r[3]))
    return min([dx(a), dx(b)] + [_project(a, b, c)[2] for c in corners])


class Copper:
    """Copper connectivity of one net; edges carry (length, cost, via hop)."""

    def __init__(self, dump, net, *, layer_factor=None, via_cost=0.0):
        layers = dump.get('copper_layers') or ['F.Cu', 'B.Cu']
        order = {l: i for i, l in enumerate(layers)}
        factor = layer_factor or {}
        self.net = net
        self.xy, self.kind, self.adj = [], [], []
        self.pads = {}
        pads = [p for p in dump['pads'] if p['net'] == net]
        tracks = [t for t in dump['tracks'] if t['net'] == net]
        vias = [v for v in dump['vias'] if v['net'] == net]
        self.tracks, self.vias = tracks, vias

        def node(xy, kind):
            self.xy.append(tuple(xy))
            self.kind.append(kind)
            self.adj.append([])
            return len(self.xy) - 1

        def edge(u, v, length, cost=None):
            cost = length if cost is None else cost
            self.adj[u].append((v, length, cost))
            self.adj[v].append((u, length, cost))
        pnode = []
        for p in pads:
            k = node((p['x'], p['y']), 'pad')
            pnode.append(k)
            self.pads.setdefault((p['ref'], p['name']), []).append(k)
        for i, p in enumerate(pads):
            for j in range(i + 1, len(pads)):
                q = pads[j]
                if p['ref'] != q['ref'] or not set(p['layers']) & set(q['layers']):
                    continue
                a, b = p['bbox'], q['bbox']
                if a[0] <= b[2] + TOL and b[0] <= a[2] + TOL and a[1] <= b[3] + TOL and b[1] <= a[3] + TOL:
                    edge(pnode[i], pnode[j], _dist(self.xy[pnode[i]], self.xy[pnode[j]]))
        vnode = [node((v['x'], v['y']), 'via') for v in vias]
        half_via = via_cost / 2.0

        def spans(v, layer):
            lo, hi = sorted((order.get(v.get('top'), 0), order.get(v.get('bottom'), len(layers) - 1)))
            return lo <= order.get(layer, lo) <= hi
        for k, v in zip(vnode, vias):
            for ref, name in v.get('pads', []):
                for pn in self.pads.get((ref, name), []):
                    edge(pn, k, _dist(self.xy[pn], self.xy[k]), _dist(self.xy[pn], self.xy[k]) + half_via)
        ends = []
        for t in tracks:
            a, b = (t['x1'], t['y1']), (t['x2'], t['y2'])
            ends.append((node(a, 'pt'), node(b, 'pt')))
        for ti, t in enumerate(tracks):
            a, b = (t['x1'], t['y1']), (t['x2'], t['y2'])
            hw = t['w'] / 2
            f = factor.get(t['layer'], 1.0)
            points = [(0.0, a, ends[ti][0], 0.0, 0.0), (1.0, b, ends[ti][1], 0.0, 0.0)]
            for side, hits in ((0.0, t.get('pads1', [])), (1.0, t.get('pads2', []))):
                for ref, name in hits:
                    for pn in self.pads.get((ref, name), []):
                        points.append((side, (a, b)[int(side)], pn, _dist((a, b)[int(side)], self.xy[pn]), 0.0))
            for pi, p in enumerate(pads):
                if t['layer'] not in p['layers'] or _seg_rect(a, b, p['bbox']) > hw + TOL:
                    continue
                u, xy, _ = _project(a, b, (p['x'], p['y']))
                points.append((u, xy, pnode[pi], _dist(xy, (p['x'], p['y'])), 0.0))
            for k, v in zip(vnode, vias):
                if not spans(v, t['layer']):
                    continue
                u, xy, d = _project(a, b, (v['x'], v['y']))
                if d <= hw + (v.get('d') or 0.6) / 2 + TOL:
                    points.append((u, xy, k, d, half_via))
            for tj, s in enumerate(tracks):
                if tj == ti or s['layer'] != t['layer']:
                    continue
                for e, q in ((0, (s['x1'], s['y1'])), (1, (s['x2'], s['y2']))):
                    u, xy, d = _project(a, b, q)
                    if d <= max(t['w'], s['w']) / 2 + TOL:
                        points.append((u, xy, ends[tj][e], d, 0.0))
            points.sort(key=lambda r: r[0])
            length = t['length'] if t.get('arc') else _dist(a, b)
            prev = None
            for u, xy, other, extra, via_extra in points:
                sn = node(xy, 'pt')
                edge(sn, other, extra, extra + via_extra)
                if prev is not None:
                    seg = (u - prev[0]) * length
                    edge(prev[1], sn, seg, seg * f)
                prev = (u, sn)
        hubs = {}
        for k, v in zip(vnode, vias):
            for hit in v.get('zone_hits', []):
                hubs.setdefault(self._zone_key(net, hit), []).append(k)
        zone_layers = {}
        for z in dump.get('zones', []):
            if z['net'] == net and (z.get('filled_area_mm2') or 0) > 0:
                zone_layers.setdefault(z['layer'], []).append(self._zone_key(net, [z['index'], z['layer']]
                                                                           if 'index' in z else z['layer']))
        for pi, p in enumerate(pads):
            keys = [self._zone_key(net, hit) for hit in p.get('zone_hits', [])]
            if not p.get('smd'):
                keys += [k for l in p['layers'] for k in zone_layers.get(l, [])]
            for key in dict.fromkeys(keys):
                hubs.setdefault(key, []).append(pnode[pi])
        for members in hubs.values():
            for u, v in itertools.combinations(dict.fromkeys(members), 2):
                edge(u, v, _dist(self.xy[u], self.xy[v]))

    @staticmethod
    def _zone_key(net, hit):
        return ('zone', net, hit) if isinstance(hit, str) else ('zone', net, hit[0], hit[1])

    def islands(self):
        """Connected copper groups that contain at least one pad."""
        seen, groups = set(), 0
        for start in (k for ks in self.pads.values() for k in ks):
            if start in seen:
                continue
            groups += 1
            todo = [start]
            seen.add(start)
            while todo:
                u = todo.pop()
                for v, _, _ in self.adj[u]:
                    if v not in seen:
                        seen.add(v)
                        todo.append(v)
        return groups

    def path(self, src, dst):
        """Cheapest copper path between pad sets: (length, cost, vias) or None."""
        S = [k for rp in src for k in self.pads.get(tuple(rp), [])]
        T = {k for rp in dst for k in self.pads.get(tuple(rp), [])}
        if not S or not T:
            return None
        best = {s: 0.0 for s in S}
        info = {s: (0.0, 0) for s in S}
        heap = [(0.0, i, s) for i, s in enumerate(S)]
        heapq.heapify(heap)
        count, done = len(heap), set()
        while heap:
            c, _, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)
            if u in T:
                return info[u][0], c, info[u][1]
            for v, length, cost in self.adj[u]:
                nc = c + cost
                if nc < best.get(v, math.inf) - 1e-12:
                    best[v] = nc
                    info[v] = (info[u][0] + length, info[u][1] + (self.kind[v] == 'via'))
                    count += 1
                    heapq.heappush(heap, (nc, count, v))
        return None


def drc_unconnected(drc):
    """Per-net unconnected-item counts of a KiCad DRC JSON report."""
    out = {}
    for item in drc.get('unconnected_items', []):
        m = re.search(r'\[(.*?)\]', (item.get('items') or [{}])[0].get('description', ''))
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + 1
    return out


# ------------------------------------------------------------------ metrics

def analyse(dump, roles, rules, drc=None):
    """Routed power-path quality of ``dump`` under power-first ``roles``."""
    fab = rules.get('electrical_fab') or {}
    layers = dump.get('copper_layers') or ['F.Cu', 'B.Cu']
    outer = {layers[0], layers[-1]}
    t_out = float(fab.get('outer_copper_um') or 35.0)
    t_in = float(fab.get('inner_copper_um') or t_out)
    factor = {l: (1.0 if l in outer else t_out / t_in) for l in layers}
    via_cost = float(fab.get('board_thickness_mm') or 1.6)
    P, R = set(roles['power_nets']), set(roles['return_nets'])
    nets = sorted({p['net'] for p in dump['pads'] if p['net']})
    copper = {n: Copper(dump, n, layer_factor=factor, via_cost=via_cost) for n in nets}
    unconnected = {n: max(0, c.islands() - 1) for n, c in copper.items()}
    unconnected = {n: k for n, k in unconnected.items() if k}
    check = None
    valid = True
    if drc is not None:
        reported = drc_unconnected(drc)
        mismatch = {n: [unconnected.get(n, 0), reported.get(n, 0)] for n in set(unconnected) | set(reported)
                    if unconnected.get(n, 0) != reported.get(n, 0)}
        check = dict(unconnected=unconnected, drc=reported, mismatch=mismatch)
        valid = not mismatch
    xy = {}
    for p in dump['pads']:
        xy.setdefault((p['ref'], p['name']), []).append((p['x'], p['y']))
    carrying = roles['carrying']
    classes = roles['classes']
    weight = dict(roles['weight'])

    def pins(ref, net):
        return [(ref, name) for name in carrying.get(ref, {}).get(net, [])]

    def straight(a, b):
        pa = [q for rp in a for q in xy.get(rp, [])]
        pb = [q for rp in b for q in xy.get(rp, [])]
        return min(_dist(p, q) for p in pa for q in pb) if pa and pb else 0.0

    def link(net, a, b):
        r = copper[net].path(a, b) if net in copper else None
        s = straight(a, b)
        if r is None:
            return dict(net=net, open=True, straight_mm=s, length_mm=None, vias=0, cost=OPEN_FACTOR * s)
        return dict(net=net, open=False, straight_mm=s, length_mm=r[0], vias=r[2], cost=r[1])
    loops, e_pow, used, hot_open, hot_links = [], 0.0, {}, 0, 0
    for l in roles['loops']:
        if not l['hot']:
            continue
        members = [classes[i]['members'] for i in l['classes']]
        k = len(members)
        combos = (list(itertools.product(*members)) if math.prod(map(len, members)) <= 64 else [None])
        best = None
        for pick in combos:
            ls = []
            for j in range(k):
                net = l['nets'][j]
                ra = [pick[j]] if pick else members[j]
                rb = [pick[(j + 1) % k]] if pick else members[(j + 1) % k]
                x = link(net, [p for r in ra for p in pins(r, net)], [p for r in rb for p in pins(r, net)])
                x.update(a=ra, b=rb)
                ls.append(x)
            score = (sum(x['open'] for x in ls), sum(x['cost'] for x in ls))
            if best is None or score < best[0]:
                best = (score, ls, pick)
        (opens, _), ls, pick = best
        hot_open += opens
        hot_links += len(ls)
        for x in ls:
            used[(x['net'], tuple(sorted(x['a'] + x['b'])))] = x
        loops.append(dict(labels=l['labels'], nets=l['nets'], parts=list(pick) if pick else None, links=ls,
                          open_links=opens, complete=opens == 0,
                          routed_mm=None if opens else sum(x['length_mm'] for x in ls),
                          vias=sum(x['vias'] for x in ls)))
    series = None
    if roles.get('series'):
        s = roles['series']
        chosen, links = [], []
        for i, ci in enumerate(s['classes']):
            cand = classes[ci]['members']
            if not chosen:
                chosen.append(cand[0])
                continue
            net = s['nets'][i]
            options = [link(net, pins(chosen[-1], net), pins(r, net)) for r in cand]
            j = min(range(len(cand)), key=lambda q: (options[q]['open'], options[q]['cost'], q))
            options[j].update(a=[chosen[-1]], b=[cand[j]])
            links.append(options[j])
            chosen.append(cand[j])
            used.setdefault((net, tuple(sorted([chosen[-2], cand[j]]))), options[j])
        series = dict(parts=chosen, links=links, open_links=sum(x['open'] for x in links),
                      routed_mm=sum(x['length_mm'] or 0 for x in links))
    for (net, _), x in used.items():
        e_pow += (weight.get(net, float(roles.get('w_ret', 1.0)))) * x['cost']
    power_tracks = [t for t in dump['tracks'] if t['net'] in P]
    total = sum(t['length'] for t in power_tracks)
    fcu = sum(t['length'] for t in power_tracks if t['layer'] == layers[0])
    per_net = {}
    for n in sorted(P):
        if n not in copper:
            continue
        groups = {}
        for p in dump['pads']:
            if p['net'] == n:
                groups.setdefault(p['ref'], []).append((p['x'], p['y']))
        mst = _group_mst(list(groups.values()))
        routed = sum(t['length'] for t in dump['tracks'] if t['net'] == n)
        per_net[n] = dict(routed_mm=routed, mst_mm=mst, vias=sum(1 for v in dump['vias'] if v['net'] == n),
                          unconnected=unconnected.get(n, 0),
                          ratio=routed / mst if mst > 0 and not unconnected.get(n) else None,
                          layers=sorted({t['layer'] for t in dump['tracks'] if t['net'] == n}))
    connected = [v for v in per_net.values() if v['ratio'] is not None]
    if not valid:
        hot_open = hot_links
    return dict(schema='pnr-power-quality-routed-v1', valid=valid, self_check=check,
                hot_loops_open=hot_open, hot_loop_links=hot_links, loops=loops, series=series,
                power_vias=sum(1 for v in dump['vias'] if v['net'] in P), power_track_mm=total,
                fcu_fraction=fcu / total if total > 0 else None,
                routed_over_mst=(sum(v['routed_mm'] for v in connected) / sum(v['mst_mm'] for v in connected)
                                 if connected else None),
                per_net=per_net, E_pow=e_pow,
                model=dict(open_factor=OPEN_FACTOR, via_cost_mm=via_cost, layer_factor=factor))


def _group_mst(groups):
    n = len(groups)
    if n < 2:
        return 0.0
    d = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d[i][j] = d[j][i] = min(_dist(p, q) for p in groups[i] for q in groups[j])
    inside, best, total = [False] * n, [math.inf] * n, 0.0
    best[0] = 0.0
    for _ in range(n):
        i = min((k for k in range(n) if not inside[k]), key=lambda k: best[k])
        inside[i] = True
        total += best[i]
        for k in range(n):
            if not inside[k]:
                best[k] = min(best[k], d[i][k])
    return total


def evaluate(round_dir, graph, rules, *, dump=None):
    """Routed power quality of a native block run dir (after the final audit)."""
    from pnr.power_topology import derive
    round_dir = Path(round_dir)
    board = round_dir / 'electrical' / 'board.kicad_pcb'
    drc_path = next((p for p in (round_dir / 'electrical' / 'board.drc.json',
                                 round_dir / 'phases' / '09-final-audit' / 'diagnostic.drc.json') if p.exists()), None)
    roles = derive(graph, None, rules)
    dump = dump if dump is not None else read_board(board)
    drc = json.loads(drc_path.read_text()) if drc_path else None
    out = analyse(dump, roles, rules, drc)
    if drc is None:
        out.update(valid=False, hot_loops_open=out['hot_loop_links'], self_check='no DRC report found')
    out['board'] = str(board)
    return out


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'dump':
        dump_main(sys.argv[2], sys.argv[3])
    else:
        sys.exit('usage: python3 -m pnr.hier.power_quality dump <board.kicad_pcb> <out.json>')
