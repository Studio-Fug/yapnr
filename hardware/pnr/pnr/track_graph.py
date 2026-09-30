"""Conservative same-layer cycle reduction between copper terminals.

Split centerlines at intersections and terminal projections, contract unanchored
degree-two vertices, and remove redundant chains with an equal-or-wider, no-longer alternative.
Partial edits reconstruct untouched continuations on their original centerlines
and at their original widths. Native
connectivity, pad-entry and DRC validation remains mandatory for acceptance.

Partial-track edits (Electrical221) are opt-in: PNR_PARTIAL_CYCLE_CLEANUP=1 (see
:func:`partial_cycle_cleanup_enabled`). Unset, only complete original-track chains
are proposed, as in the src15 engine.
"""

import heapq
import math
import os
from collections import defaultdict


def partial_cycle_cleanup_enabled():
    """PNR_PARTIAL_CYCLE_CLEANUP=1: Electrical221's partial-track cycle edits.

    When no whole-track cycle is left on a net and layer, a cycle may remove part of
    an original track, with the continuations outside the chain rebuilt at their
    original width; via_coalesce's cycle trial then also matches the replacement
    segments and endpoints. Default off (src15 behaviour): Electrical221 has no
    A/B result on the hierarchical engine.
    """
    return os.environ.get("PNR_PARTIAL_CYCLE_CLEANUP") == "1"


def point(p):
    return (round(p[0]), round(p[1]))


def projection(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2)) if length2 else 0
    return point((a[0] + t * dx, a[1] + t * dy))


def on_segment(p, a, b):
    return math.dist(p, projection(p, a, b)) <= 1


def intersection(a, b, c, d):
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    det = rx * sy - ry * sx
    if not det:
        return None  # Collinear overlaps are split at their endpoints below.
    qx, qy = c[0] - a[0], c[1] - a[1]
    t, u = (qx * sy - qy * sx) / det, (qx * ry - qy * rx) / det
    if 0 <= t <= 1 and 0 <= u <= 1:
        return point((a[0] + t * rx, a[1] + t * ry))
    return None


def redundant_chains(segments, terminals=(), max_length=3_000_000, allow_partial=False):
    """Return deterministic cycle-removal proposals (units: nm).

    A pad/via/branch in the middle of a track is a real graph vertex. By default, only whole-track chains are proposed. With allow_partial,
    replacement_segments explicitly preserve all continuations outside a chain.
    """
    nodes = {tuple(p) for s in segments for p in (s["a"], s["b"])}
    anchors = {tuple(p) for p in terminals}
    nodes |= anchors
    byid = {s["id"]: s for s in segments}
    for i, s in enumerate(segments):
        for t in segments[i + 1 :]:
            p = intersection(s["a"], s["b"], t["a"], t["b"])
            if p is not None:
                nodes.add(p)
    edges, adjacency, original_edges = [], defaultdict(list), defaultdict(set)
    for s in segments:
        ps = sorted(
            (p for p in nodes if on_segment(p, s["a"], s["b"])), key=lambda p: math.dist(p, s["a"])
        )
        for a, b in zip(ps, ps[1:]):
            if a == b:
                continue
            e = len(edges)
            edges.append((a, b, s["width"], math.dist(a, b), s["id"]))
            adjacency[a].append(e)
            adjacency[b].append(e)
            original_edges[s["id"]].add(e)
    stops = anchors | {p for p, es in adjacency.items() if len(es) != 2}
    # A locked edge may be retained as an alternative but never removed.
    seen, proposals = set(), []
    for start in sorted(stops):
        for first in adjacency[start]:
            path, at, e = [], start, first
            while e not in path:
                path.append(e)
                a, b, _, _, _ = edges[e]
                at = b if at == a else a
                if at in stops:
                    break
                e = next(x for x in adjacency[at] if x != e)
            chain = frozenset(path)
            if chain in seen:
                continue
            seen.add(chain)
            ids = sorted({edges[e][4] for e in chain})
            if any(
                byid[i].get("locked") or (not allow_partial and not original_edges[i] <= chain)
                for i in ids
            ):
                continue
            length = sum(edges[e][3] for e in chain)
            if length <= 0 or length > max_length:
                continue
            width = max(edges[e][2] for e in chain)
            # Dijkstra on the retained graph; forbid a narrower or longer detour.
            heap, best, alternate = [(0.0, start)], {start: 0.0}, None
            while heap:
                distance, p = heapq.heappop(heap)
                if distance != best[p] or distance > length + 1:
                    continue
                if p == at:
                    alternate = distance
                    break
                for j in adjacency[p]:
                    if j in chain:
                        continue
                    a, b, w, size, _ = edges[j]
                    if w < width:
                        continue
                    q = b if p == a else a
                    new = distance + size
                    if new < best.get(q, math.inf):
                        best[q] = new
                        heapq.heappush(heap, (new, q))
            if alternate is not None:
                proposal = dict(
                    remove_tracks=ids,
                    length_mm=length / 1e6,
                    retained_path_mm=alternate / 1e6,
                    width_mm=width / 1e6,
                    endpoints=[start, at],
                )
                replacements = []
                for identity in ids:
                    for retained in sorted(original_edges[identity] - chain):
                        a, b, w, _, _ = edges[retained]
                        replacements.append(dict(track=identity, a=a, b=b, width=w))
                if replacements:
                    proposal["replacement_segments"] = replacements
                proposals.append(proposal)
    return sorted(proposals, key=lambda p: (-p["length_mm"], p["remove_tracks"]))


def cycle_candidates(board, rules, sources, max_length_mm=3):
    from pnr.plane_access import uid
    from pnr.via_coalesce import protected, touch

    excluded, _ = protected(board, rules, sources)
    groups = defaultdict(list)
    items = list(board.GetTracks())
    pads = [p for f in board.GetFootprints() for p in f.Pads()]
    unsupported = {
        (t.GetNetname(), t.GetLayer())
        for t in items
        if t.GetClass() not in {"PCB_TRACK", "PCB_VIA"}
    }
    for t in items:
        if t.GetClass() == "PCB_TRACK" and t.GetNetCode() and t.GetNetname() not in excluded:
            groups[t.GetNetname(), t.GetLayer()].append(t)
    result = []
    for (net, la), tracks in sorted(groups.items()):
        if (net, la) in unsupported:
            continue
        segments, anchors = [], set()
        terminals = [
            t
            for t in pads + items
            if t.GetNetname() == net and t.IsOnLayer(la) and t.GetClass() in {"PAD", "PCB_VIA"}
        ]
        for t in tracks:
            a, b = ((p.x, p.y) for p in (t.GetStart(), t.GetEnd()))
            segments.append(dict(id=uid(t), a=a, b=b, width=t.GetWidth(), locked=t.IsLocked()))
            for terminal in terminals:
                if touch(t, terminal, la):
                    p = terminal.GetPosition()
                    anchors.add(projection((p.x, p.y), a, b))
        choices = redundant_chains(segments, anchors, round(max_length_mm * 1e6))
        if not choices and partial_cycle_cleanup_enabled():
            # Only fall back to partial-track edits after whole-track cleanup.
            # Unselected portions are explicitly reconstructed at original width.
            choices = redundant_chains(
                segments, anchors, round(max_length_mm * 1e6), allow_partial=True
            )
        for proposal in choices:
            result.append(dict(proposal, net=net, layer=board.GetLayerName(la)))
    return result


def apply_cycle(board, proposal):
    """Apply a cycle proposal; return the uuids of the deleted tracks.

    The removed tracks are deleted (board.Delete), so no wrapper of them may be
    used afterwards; callers get their uuids.
    """
    from pnr.plane_access import uid

    items = {uid(t): t for t in board.GetTracks()}
    for identity in proposal["remove_tracks"]:
        t = items[identity]
        if t.IsLocked() or t.GetClass() != "PCB_TRACK" or t.GetNetname() != proposal["net"]:
            raise ValueError("stale or protected cycle proposal")
    import pcbnew

    replacements = []
    for piece in proposal.get("replacement_segments", []):
        source = items[piece["track"]]
        a, b = tuple(piece["a"]), tuple(piece["b"])
        ends = [(v.x, v.y) for v in (source.GetStart(), source.GetEnd())]
        if (
            piece["track"] not in proposal["remove_tracks"]
            or source.IsLocked()
            or piece["width"] != source.GetWidth()
            or a == b
            or not all(on_segment(p, *ends) for p in (a, b))
        ):
            raise ValueError("invalid retained original-track segment")
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(*a))
        t.SetEnd(pcbnew.VECTOR2I(*b))
        t.SetLayer(source.GetLayer())
        t.SetWidth(source.GetWidth())
        t.SetNetCode(source.GetNetCode())
        replacements.append(t)
    removed = [items[identity] for identity in proposal["remove_tracks"]]
    for t in replacements:
        board.Add(t)
        t.thisown = False
    for t in removed:
        board.Delete(t)  # discarded; a Removed track would outlive its board
    board.BuildConnectivity()
    return list(proposal["remove_tracks"])
