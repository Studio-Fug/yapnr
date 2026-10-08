"""The storyboard (``pnr-storyboard-v1``): the critical path as an ordered list of scenes.

Scenes, in path order:

``title``       case title, description, parts, nets, layers
``source``      the unplaced board (the generator's row), full ratsnest, 0 %
``montage``     a selection's candidates as tiles (up to 7 rivals and the winner), placed
                right after the winner's own replay reached the state the tiles show
``attempts``    placement attempts that failed legalization (at most 3)
``congestion``  the previous round's routing pressure before a new placement
``placement``   a global placement (snapshots) and its legalization (accepted order)
``move``        a round's placement that no global placement produced (local moves)
``route``       one detailed route: negotiation, commits, rip-ups, the final copper
``native``      a saved KiCad board after writeback, planes, gloss or refill, with its DRC
``end``         KiCad's verdict and the metrics

A storyboard names scopes and event sequence numbers of its trace, never files. A trace with a
``blocks`` event (a hierarchical case) gets the chapters of :mod:`pnr.animate.hier` instead.
"""

from __future__ import annotations

import math

from pnr.provenance import critical_path, from_trace, hier_blocks

SCHEMA = "pnr-storyboard-v1"
MAX_TILES = 8
MAX_ATTEMPTS = 3


def score_key(score):
    """Sort key of a selection score (a number, a list compared in order, or None)."""
    if score is None:
        return (1, ())
    values = score if isinstance(score, list) else [score]
    out = []
    for v in values:
        if isinstance(v, (int, float)) and math.isfinite(v):
            out.append(float(v))
        else:
            out.append(float("inf"))
    return (0, tuple(out))


def subject(trace, title=None, subtitle=None):
    """What the overlay says about the board."""
    info = trace.run.get("subject", {})
    header = trace.header
    nets = [n for n in header["nets"] if len(n["pins"]) >= 2]
    return dict(
        title=title or info.get("case") or "Place and route",
        description=subtitle if subtitle is not None else info.get("description", ""),
        case=info.get("case"),
        seed=info.get("seed"),
        parts=len(header["components"]),
        nets=len(nets),
        layers=len(header["copper_layers"]),
        connections=header.get("connections_total", 0),
        config=trace.run.get("config", {}),
    )


def legal_motion(trace, scopes):
    """The legalizer's motion over the last ``legal`` event of each scope in ``scopes`` that
    records one (``motion``, :func:`pnr.place.motion.summary`), combined (:func:`combine`); None
    when none does."""
    records = []
    for scope in scopes:
        events = [e for e in trace.kind(scope, "legal") if isinstance(e.get("motion"), dict)]
        if events:
            records.append(events[-1]["motion"])
    return combine(records) if records else None


def detail_motion(trace, scopes):
    """The detailed-placement pass's record over the last ``detail`` event of each scope in
    ``scopes`` that records one (``motion``, :func:`pnr.place.detail.summary`), combined
    (:func:`combine_detail`); None when none does."""
    records = []
    for scope in scopes:
        events = [e for e in trace.kind(scope, "detail") if isinstance(e.get("motion"), dict)]
        if events:
            records.append(events[-1]["motion"])
    return combine_detail(records) if records else None


DETAIL_KINDS = ("turn", "slide", "swap", "wrong_side", "compact")


def combine_detail(records):
    """``moves`` per kind, ``moved`` and ``wire_saved`` added over ``records`` (what the end card
    shows; the renderer does not import the placer)."""

    def saved(r):
        # The whole pass's estimated wiring saving (compaction included), from the record's
        # before/after totals where it has them.
        keys = ("hpwl_before", "crossing_before", "hpwl_after", "crossing_after")
        if all(isinstance(r.get(k), (int, float)) for k in keys):
            return (r["hpwl_before"] + r["crossing_before"]) - (
                r["hpwl_after"] + r["crossing_after"]
            )
        return float(r.get("wire_saved") or 0.0)

    return dict(
        moves={
            k: sum(int((r.get("moves") or {}).get(k) or 0) for r in records) for k in DETAIL_KINDS
        },
        moved=sum(int(r.get("moved") or 0) for r in records),
        wire_saved=round(sum(saved(r) for r in records), 3),
    )


def combine(records):
    """``moved``, ``count`` and ``sum_mm`` added and ``max_mm`` the largest over ``records``
    (what the end card shows; :func:`pnr.place.motion.combine` keeps the rest, but the renderer
    does not import the placer)."""
    return dict(
        moved=sum(int(r.get("moved") or 0) for r in records),
        count=sum(int(r.get("count") or 0) for r in records),
        sum_mm=round(sum(float(r.get("sum_mm") or 0.0) for r in records), 3),
        max_mm=round(max(float(r.get("max_mm") or 0.0) for r in records), 3),
    )


def build(trace, title=None, subtitle=None):
    """The storyboard of a loaded :class:`pnr.provenance.Trace`."""
    if trace.root is not None and hier_blocks(trace):
        from pnr.animate import hier

        return hier.build(trace, title=title, subtitle=subtitle)
    dag = trace.dag if getattr(trace, "dag", None) is not None else from_trace(trace)
    order, competitors, entry = critical_path(dag, "final")
    index = {n.id: i for i, n in enumerate(order)}
    # Failed placement attempts flash where the winner's own ancestry begins (they came
    # first). A selection's rivals appear once the winner's own replay has reached the state
    # their tiles show (its legal placement, its routed board), so the montage zooms into the
    # frame on screen and the timeline never jumps ahead of itself or back.
    before, after = {}, {}
    for node in order:
        if node.kind != "selection" or not node.chosen or not competitors.get(node.id):
            continue
        if node.criterion in ("missing-connections",):
            continue  # later rounds: named on the end card, not replayed
        if node.criterion == "first-legal":
            before.setdefault(entry.get(node.chosen, index[node.id]), []).append(node)
        else:
            after.setdefault(index.get(node.chosen, index[node.id]), []).append(node)
    scenes = [dict(type="title")]
    last_round = None
    for i, node in enumerate(order):
        this_round = None
        if node.scope and node.stage in ("place", "route"):
            this_round = _round(trace, node.scope)
        if (
            node.stage == "place"
            and this_round
            and last_round
            and this_round != last_round
            and trace.kind(last_round, "congestion")
        ):
            scenes.append(dict(type="congestion", scope=last_round))
        for selection in sorted(before.get(i, []), key=lambda s: index[s.id]):
            failed = [c for c in selection.candidates if dag.nodes[c].status != "ok"]
            if failed:
                scenes.append(dict(type="attempts", scopes=failed[:MAX_ATTEMPTS]))
        scenes.extend(_scenes_for(trace, dag, node))
        for selection in sorted(after.get(i, []), key=lambda s: index[s.id]):
            scenes.append(_montage(dag, selection, competitors[selection.id], trace))
        last_round = this_round or last_round
    rejected = set_aside(order, index)
    result = trace.results[-1] if trace.results else {}
    end = {
        k: result.get(k)
        for k in ("passed", "opens", "violations", "rules", "vias", "copper_length_mm")
    }
    placed = [n.scope for n in order if n.stage == "place" and n.scope]
    motion = legal_motion(trace, placed)
    if motion is not None:
        end["legal_motion"] = motion
    motion = detail_motion(trace, placed)
    if motion is not None:
        end["detail_motion"] = motion
    scenes.append(dict(type="end", rejected=rejected, result=end))
    return dict(
        schema=SCHEMA,
        subject=subject(trace, title, subtitle),
        coarse=bool(trace.coarse),
        path=[n.id for n in order],
        scenes=scenes,
    )


def from_dag(dag, kind):
    """The critical path of a DAG without a board to draw (a synthesis library): the path, the
    competitors of each selection on it and its montages as data."""
    order, competitors, _entry = critical_path(dag, "final")
    index = {n.id: i for i, n in enumerate(order)}
    montages = [
        _montage(dag, n, competitors[n.id], None)
        for n in order
        if n.kind == "selection" and n.chosen and competitors.get(n.id)
    ]
    return dict(
        schema=SCHEMA,
        kind=kind,
        path=[n.id for n in order],
        competitors={k: v for k, v in sorted(competitors.items()) if v},
        scenes=montages,
        rejected=set_aside(order, index),
    )


def set_aside(order, on_path):
    """Per criterion, how many candidates a selection on the path turned down.

    A shortlist is one selection event recorded as one node per member (``id[member]``);
    it turned down the candidates outside its selected members, counted once per event.
    Candidates on the path (earlier feedback rounds) and failed placement attempts are not
    rivals."""
    events = {}
    for node in order:
        if node.kind != "selection" or node.criterion == "first-legal":
            continue
        base = node.id.split("[", 1)[0]
        kept = set(node.selected) | ({node.chosen} if node.chosen else set())
        lost = {c for c in node.candidates if c not in kept and c not in on_path}
        key = node.criterion or node.label
        events.setdefault((key, base), set()).update(lost)
    rejected = {}
    for (key, _base), lost in sorted(events.items()):
        if lost:
            rejected[key] = rejected.get(key, 0) + len(lost)
    return rejected


def _round(trace, scope_id):
    while scope_id:
        scope = trace.scopes.get(scope_id)
        if scope is None:
            return None
        if scope.type == "round":
            return scope_id
        scope_id = scope.parent
    return None


def _montage(dag, selection, rivals, trace):
    scores = selection.scores
    ranked = sorted(rivals, key=lambda c: (score_key(scores.get(c)), dag.nodes[c].order, c))
    shown = ranked[: MAX_TILES - 1]
    tiles = []
    for cid in sorted(shown + [selection.chosen], key=lambda c: (dag.nodes[c].order, c)):
        node = dag.nodes[cid]
        tiles.append(
            dict(
                node=node.scope or cid,
                label=node.label,
                stage=node.stage,
                score=scores.get(cid),
                lit=cid in selection.selected or cid == selection.chosen,
                chosen=cid == selection.chosen,
            )
        )
    return dict(
        type="montage",
        selection=selection.id,
        criterion=selection.criterion,
        among=len(selection.candidates),
        selected=len(selection.selected) or 1,
        tiles=tiles,
        more=len(ranked) - len(shown),
    )


def _scenes_for(trace, dag, node):
    if node.id == "source":
        return [dict(type="source")]
    if node.kind == "selection" or node.id == "final":
        return []
    if node.id.startswith("native:"):
        return [dict(type="native", stage=node.label, seq=node.meta.get("event"))]
    if node.meta.get("replay") == "native":
        # A coarse run's winning rung: its saved native phases, in order.
        return [
            dict(type="native", stage=e["stage"], seq=e["seq"])
            for e in trace.events("native")
            if e["kind"] == "board"
        ]
    out = []
    if node.stage == "place" and node.scope:
        global_poses = [e for e in trace.kind(node.scope, "poses") if e.get("stage") == "global"]
        if node.id.endswith("/placement"):
            out.append(dict(type="move", scope=node.scope))
        elif global_poses or trace.kind(node.scope, "legal"):
            out.append(dict(type="placement", scope=node.scope, label=node.label))
        elif trace.kind(node.scope, "poses"):
            out.append(dict(type="move", scope=node.scope, label=node.label))
        return out
    if node.stage == "route":
        events = [e for e in trace.scopes[node.scope].events if e["kind"] in ("net", "route_end")]
        if events:
            out.append(dict(type="route", scope=node.scope, label=node.label))
    return out
