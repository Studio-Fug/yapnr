"""The storyboard (``pnr-storyboard-v1``): the critical path as an ordered list of scenes.

Scenes, in path order:

``title``       case title, description, parts, nets, layers
``source``      the unplaced board (the generator's row), full ratsnest, 0 %
``montage``     a selection's candidates as tiles (up to 7 rivals and the winner), placed
                before the winner's own replay; consecutive selections in stage order
``attempts``    placement attempts that failed legalization (at most 3)
``congestion``  the previous round's routing pressure before a new placement
``placement``   a global placement (snapshots) and its legalization (accepted order)
``move``        a round's placement that no global placement produced (local moves)
``route``       one detailed route: negotiation, commits, rip-ups, the final copper
``native``      a saved KiCad board after writeback, planes or refill, with its DRC
``end``         KiCad's verdict and the metrics

A storyboard names scopes and event sequence numbers of its trace, never files.
"""

from __future__ import annotations

import math

from pnr.provenance import critical_path, from_trace

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


def build(trace, title=None, subtitle=None):
    """The storyboard of a loaded :class:`pnr.provenance.Trace`."""
    dag = from_trace(trace)
    order, competitors, entry = critical_path(dag, "final")
    index = {n.id: i for i, n in enumerate(order)}
    # A selection's rivals appear where the winner's own ancestry begins.
    inserts = {}
    for node in order:
        if node.kind != "selection" or not node.chosen or not competitors.get(node.id):
            continue
        if node.criterion in ("missing-connections",):
            continue  # later rounds: named on the end card, not replayed
        inserts.setdefault(entry.get(node.chosen, index[node.id]), []).append(node)
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
        for selection in sorted(inserts.get(i, []), key=lambda s: index[s.id]):
            if selection.criterion == "first-legal":
                failed = [c for c in selection.candidates if dag.nodes[c].status != "ok"]
                scenes.append(dict(type="attempts", scopes=failed[:MAX_ATTEMPTS]))
            else:
                scenes.append(_montage(dag, selection, competitors[selection.id], trace))
        scenes.extend(_scenes_for(trace, dag, node))
        last_round = this_round or last_round
    rejected = {}
    for node in order:
        if node.kind == "selection" and competitors.get(node.id):
            if node.criterion != "first-legal":  # failed attempts are not rivals
                rejected[node.criterion or node.label] = len(competitors[node.id])
    result = trace.results[-1] if trace.results else {}
    scenes.append(
        dict(
            type="end",
            rejected=rejected,
            result={
                k: result.get(k)
                for k in ("passed", "opens", "violations", "rules", "vias", "copper_length_mm")
            },
        )
    )
    return dict(
        schema=SCHEMA,
        subject=subject(trace, title, subtitle),
        coarse=bool(trace.coarse),
        path=[n.id for n in order],
        scenes=scenes,
    )


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
                node=cid,
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
    if node.stage == "native":
        return [dict(type="native", stage=node.label, seq=node.meta.get("event"))]
    out = []
    if node.stage == "place":
        if node.id.endswith("/placement"):
            out.append(dict(type="move", scope=node.scope))
        elif trace.kind(node.scope, "poses") or trace.kind(node.scope, "legal"):
            out.append(dict(type="placement", scope=node.scope, label=node.label))
        return out
    if node.stage == "route":
        events = [e for e in trace.scopes[node.scope].events if e["kind"] in ("net", "route_end")]
        if events:
            out.append(dict(type="route", scope=node.scope, label=node.label))
    return out
