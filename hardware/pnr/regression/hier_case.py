"""Hierarchical place and route of one ladder case (``design["driver"] == "hier"``).

Called like ``route_case.py``: ``hier_case.py CASE_DIR SEED ROUNDS`` (ROUNDS is not used:
the hierarchical flow has no place-route rounds). Pure Python on existing engine parts:

1. **Blocks.** Every block template (:func:`pnr.hier.blocks.extract_blocks` over the
   parts' atopile addresses) is placed and routed on its own small board by
   :func:`pnr.hier.synth.run_trial` for each candidate outline and trial seed; the
   trial with the fewest missing connections, then by :func:`pnr.hier.synth.rank_key`,
   is the template's layout, reused for every instance of the template.
2. **Top level.** For each top seed :func:`pnr.hier.top.hierarchical_place` places the
   blocks as rigid macros among the top-level parts and expands them.
3. **Knitting.** Each instance's routed copper moves onto the board with the macro's
   rigid transform. The top-level routing graph keeps one representative pad per block
   and external net (the pad nearest the block outline); the block's other pads on that
   net are renamed ``NET@block`` and leave the net, and block-internal nets are dropped,
   since the block copper already joins them. :func:`pnr.route.detail.router.route_board`
   then routes the nets between blocks around the block copper
   (``fixed_copper_own_net=True``).
4. **Selection.** The seed with the smallest (missing connections, unresolved nets,
   vias, copper length) wins; missing connections come from :func:`pin_groups` over
   pads, block copper and top-level copper, the driver's own completeness check
   (KiCad's DRC judges afterwards).

Outputs in the shape ``route_case.py`` writes: ``placed.json``, ``routes.json`` (block
copper plus top-level copper), ``rules.json`` and ``pnr-report.json`` (with ``hier``: the
chosen layouts, macros, representatives and seed scores).

Tracing (``PNR_TRACE_DIR``, set by the ladder runner): each template's trials go to
``blocks/<template-id>/`` (a ``pnr-trace-v1`` directory of its own: ``start-NN`` trial
scopes, ``start-NN-<block>`` route scopes, a ``block-rank`` selection); the case trace
holds a ``hier-blocks`` scope with one ``blocks`` event, a ``top-NN`` start scope per
seed (the macro placement, members expanded), a ``top-NN-route`` route scope per legal
seed that opens with a ``fixed`` event (the block copper and the pins it already joins)
and a ``top-seed`` selection.

With ``PNR_COMPACT=1`` (pnr.place.compact, default off) the block trials also try the
utilisations :data:`pnr.place.compact.UTILISATIONS` (``GP``; the budget is this driver's,
so a caller of the engine's hierarchical API sets its own) and (``RANK``) the top seed
ranks by the compactness bucket after the completion keys and vias (route_rank).
``PNR_SHRINK`` does not apply to this driver; ``pnr-report.json`` records it as skipped.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import time
from pathlib import Path

from pnr import trace
from pnr.graph import BoardGraph, Net
from pnr.hier.blocks import aspect_sizes, extract_blocks, sub_board
from pnr.hier.macro import _rot
from pnr.hier.synth import _template_id, instance_board, rank_key, run_trial
from pnr.place.geometry import pad_rects
from pnr.route.detail.exact_route import exact_mode
from pnr.route.detail.native_maze import status as maze_status
from pnr.writeback import _segment_distance_sq

# Budgets; a design's "hier" mapping overrides them (and the report records them).
DEFAULT_BUDGET = dict(
    utilisations=[0.3, 0.4],
    aspects=[1.5, 1 / 1.5],
    trial_seeds=[0, 1],
    block_iters=350,
    route_pitch_mm=0.25,
    route_iters=8,
    top_seeds=4,
    top_iters=350,
    representative_retries=3,
)
EPS = 1e-6


def load(root):
    """(spec, graph with the parts' addresses, constraints, rules) of a case directory."""
    from pnr.constraints import compile_constraints, compile_routing_rules
    from pnr.fab_profile import apply_rules

    spec = json.loads((root / "design.json").read_text())
    graph = BoardGraph.from_json((root / "source-graph.json").read_text())
    addresses = {p["ref"]: p["address"] for p in spec["parts"] if p.get("address")}
    for comp in graph.components:
        comp.address = addresses.get(comp.ref, comp.address)
    # The rung's side policy (``sides: double``) as the engine's ``board.sides``, as
    # route_case.py maps it (hierarchical blocks and their macros stay on their side).
    from pnr.place.sides import with_policy

    constraints = compile_constraints(
        with_policy(spec["constraints"], spec.get("sides")), graph.refs
    )
    # Route under the fab profile writeback stamps and KiCad judges, as route_case.py does.
    rules = apply_rules(compile_routing_rules(constraints, [n.name for n in graph.nets]))
    declared = set((spec.get("via_policy") or {}).get("allowed") or []) - {"through"}
    if declared:
        # Blind, buried and micro vias reach the flat drivers only (pnr.via_policy).
        sys.stderr.write(
            "hier_case: via policy %s not applied: hierarchical blocks route through vias "
            "only\n" % ", ".join(sorted(declared))
        )
    board = root / "source.kicad_pcb"
    if board.exists():
        from pnr.length_model import attach_board

        # Pairs and groups are tuned against the board's own stackup and pad lands.
        attach_board(rules, board.read_text())
    return spec, graph, constraints, rules


def budget_of(spec):
    budget = dict(DEFAULT_BUDGET)
    budget.update(spec.get("hier") or {})
    from pnr.place import compact

    if compact.enabled("GP"):
        # PNR_COMPACT GP: denser block outlines join the trials (rank_key prefers less area).
        extra = [u for u in compact.UTILISATIONS if u not in budget["utilisations"]]
        budget["utilisations"] = list(budget["utilisations"]) + extra
    return budget


@contextlib.contextmanager
def trace_directory(path):
    """Point ``PNR_TRACE_DIR`` at ``path`` while open (only when tracing is on); yields
    that directory's recorder and closes it on the way out."""
    previous = os.environ.get(trace.ENV_DIR)
    if not previous or path is None:
        yield None
        return
    os.environ[trace.ENV_DIR] = str(path)
    try:
        yield trace.current()
    finally:
        recorder = trace.current()
        if recorder is not None:
            recorder.close()
        os.environ[trace.ENV_DIR] = previous


# ------------------------------------------------------------------------ blocks


def templates_of(blocks):
    """[(template key, [blocks])] in the order hierarchical_place draws layouts."""
    by_template = {}
    for b in blocks:
        by_template.setdefault(b.template, []).append(b)
    return sorted(by_template.items(), key=lambda kv: kv[1][0].name)


def macro_refs(blocks):
    """{block name: macro ref} as :func:`pnr.hier.macro.collapse` names them there."""
    order = [b.name for _, members in templates_of(blocks) for b in members]
    return {name: "MB%02d" % i for i, name in enumerate(order)}


def synthesize(source, constraints, rules, budget, seed, trace_root=None):
    """Blocks and, per template, its trials and chosen layout (``trials``, ``chosen``)."""
    blocks = extract_blocks(source, constraints)
    by_name = {b.name: b for b in blocks}
    out = []
    for template, members in templates_of(blocks):
        names = [b.name for b in members]
        tid = _template_id(template)
        sizes = aspect_sizes(
            source,
            members[0],
            utilisations=tuple(budget["utilisations"]),
            aspects=tuple(budget["aspects"]),
        )
        jobs = [(size, seed + s) for size in sizes for s in budget["trial_seeds"]]
        folder = None if trace_root is None else Path(trace_root) / "blocks" / tid
        with trace_directory(folder) as recorder:
            if recorder is not None:
                sub, sub_constraints, sub_rules = sub_board(
                    source, constraints, rules, members[0], sizes[0][0], sizes[0][1]
                )
                recorder.begin_board(sub, sub_constraints, sub_rules)
            trials = []
            for k, (size, trial_seed) in enumerate(jobs):
                name = "start-%02d" % k
                rec = run_trial(
                    source,
                    constraints,
                    rules,
                    by_name,
                    names,
                    size,
                    trial_seed,
                    budget["block_iters"],
                    budget["route_iters"],
                    pitch=budget["route_pitch_mm"],
                    trace_id=name,
                )
                rec["id"] = name
                trials.append(rec)
                if rec.get("status") == "failed":
                    print(rec.get("traceback", rec.get("error")), flush=True)
                print(
                    "Block %s trial %s: %s, %s missing, %.2f x %.2f mm, seed %d, %.1fs"
                    % (
                        names[0],
                        name,
                        rec.get("status"),
                        rec.get("missing"),
                        rec["width"],
                        rec["height"],
                        trial_seed,
                        rec["seconds"],
                    ),
                    flush=True,
                )
            ok = [r for r in trials if r.get("status") == "ok"]
            if not ok:
                raise RuntimeError("block template %s: no trial placed and routed" % names[0])
            fewest = min(r["missing"] for r in ok)
            tier = sorted((r for r in ok if r["missing"] == fewest), key=lambda r: rank_key(r))
            chosen = tier[0]
            trace.select(
                "block-rank",
                [r["id"] for r in trials],
                chosen["id"],
                "block-rank",
                {r["id"]: list(rank_key(r)) for r in trials},
            )
        compacted = None
        from pnr.place import route_compact

        if route_compact.enabled("BLOCK"):
            # PNR_ROUTE_COMPACT BLOCK: the chosen layout squeezed by its own copper.
            from pnr.hier.compact_block import compact_block

            with trace.suspended():  # the block traces keep their trials only
                chosen, compacted = compact_block(
                    source,
                    constraints,
                    rules,
                    by_name,
                    names,
                    chosen,
                    iters=budget["block_iters"],
                    route_iters=budget["route_iters"],
                    pitch=budget["route_pitch_mm"],
                )
            print(
                "Block %s route-compact: %s -> %s mm, %d step(s) accepted%s"
                % (
                    names[0],
                    compacted.get("block_mm", {}).get("before"),
                    compacted.get("block_mm", {}).get("after"),
                    compacted["accepted"],
                    (", " + compacted["error"]) if "error" in compacted else "",
                ),
                flush=True,
            )
        out.append(
            dict(template=tid, blocks=names, trials=trials, chosen=chosen, compacted=compacted)
        )
    return blocks, out


def frames_of(source, constraints, rules, blocks, synth):
    """{block name: frame}: the instance's chosen layout in its block frame."""
    by_name = {b.name: b for b in blocks}
    frames = {}
    for t in synth:
        rec = t["chosen"]
        for name in t["blocks"]:
            block = by_name[name]
            sub, _, _ = instance_board(
                source, constraints, rules, block, rec["layout"], rec["width"], rec["height"]
            )
            frames[name] = dict(
                block=block,
                template=t["template"],
                trial=rec["id"],
                rec=rec,
                sub=sub,
                width=rec["width"],
                height=rec["height"],
            )
    return frames


def macro_pose(frame, placed):
    """(centre, turn) of an instance's macro, recovered from one member: the expansion is
    rigid (:meth:`pnr.hier.macro.MacroPlan.expand`)."""
    local = min(frame["sub"].components, key=lambda c: c.ref)
    comp = placed.component(local.ref)
    turn = float(round((comp.rot - local.rot) / 90.0) * 90 % 360)
    dx, dy = _rot(local.pos[0] - frame["width"] / 2, local.pos[1] - frame["height"] / 2, turn)
    return (comp.pos[0] - dx, comp.pos[1] - dy), turn


def to_board(point, frame, centre, turn):
    dx, dy = _rot(point[0] - frame["width"] / 2, point[1] - frame["height"] / 2, turn)
    return [centre[0] + dx, centre[1] + dy]


def block_copper(frame, centre=None, turn=0.0):
    """(tracks, vias) of an instance in the routes.json shape; in the board frame when
    ``centre`` is given, else in the block frame."""
    routes = frame["rec"]["routes"][frame["block"].name]

    def move(p):
        return list(p) if centre is None else to_board(p, frame, centre, turn)

    tracks = [[n, la, move(a), move(b), w] for n, la, a, b, w in routes["tracks"]]
    vias = [[n, *move((x, y))] for n, x, y in routes["vias"]]
    return tracks, vias


def copper_blob(tracks, vias, layers, fab):
    """``tracks``/``vias`` (routes.json shape) as a pnr-trace-v1 copper blob."""
    um = trace.um
    rows = sorted(
        [layers.index(la) if la in layers else 0, um(a[0]), um(a[1]), um(b[0]), um(b[1]), um(w)]
        for _n, la, a, b, w in tracks
    )
    points = sorted({(um(x), um(y)) for _n, x, y in vias})
    size = [um(fab["via_diameter_mm"]), um(fab["via_drill_mm"])]
    return dict(tracks=rows, vias=[[x, y] + size for x, y in points], zones=[])


# ------------------------------------------------------------------------ knitting


def representatives(frames):
    """{(block, net): [(ref, pad), ...]}: each block's pads on each external net, nearest
    the block outline first (ties by ref and pad name)."""
    out = {}
    for name, frame in sorted(frames.items()):
        w, h = frame["width"], frame["height"]
        for net in frame["block"].external_nets:
            candidates = []
            for comp in frame["sub"].components:
                for pad, pnet, r in pad_rects(comp):
                    if pnet == net:
                        d = min(r.cx, w - r.cx, r.cy, h - r.cy)
                        candidates.append((round(d, 9), comp.ref, pad))
            out[(name, net)] = [(ref, pad) for _d, ref, pad in sorted(candidates)]
    return out


def top_graph(placed, frames, reps, choice=None):
    """The top-level routing graph: block-internal nets dropped; per block and external
    net only the representative pad stays on the net, the others become ``NET@block``."""
    choice = choice or {}
    top = BoardGraph.from_json(placed.to_json())
    internal = {n for f in frames.values() for n in f["block"].internal_nets}
    rename = {}
    for (name, net), candidates in reps.items():
        keep = candidates[choice.get((name, net), 0) % len(candidates)]
        for pin in candidates:
            if pin != keep:
                rename[pin] = "%s@%s" % (net, name)
    for comp in top.components:
        for pad in comp.pads:
            if (comp.ref, pad.name) in rename:
                pad.net = rename[(comp.ref, pad.name)]
    top.nets = [
        Net(n.name, n.code, [tuple(p) for p in n.pins if tuple(p) not in rename])
        for n in top.nets
        if n.name not in internal
    ]
    return top


def fixed_copper(tracks, vias, fab):
    """Block copper in :mod:`pnr.route.detail.fixed`'s shape."""
    return dict(
        frame="engine-mm-y-up",
        tracks=[[n, la, list(a), list(b), float(w)] for n, la, a, b, w in tracks],
        vias=[
            dict(
                net=n,
                xy=[x, y],
                diameter_mm=float(fab["via_diameter_mm"]),
                drill_mm=float(fab["via_drill_mm"]),
                type="through",
            )
            for n, x, y in vias
        ],
    )


def _point_rect_d2(p, r):
    dx = max(r.left - p[0], 0.0, p[0] - r.right)
    dy = max(r.bottom - p[1], 0.0, p[1] - r.top)
    return dx * dx + dy * dy


def _segment_rect_d2(a, b, r):
    if _point_rect_d2(a, r) == 0.0 or _point_rect_d2(b, r) == 0.0:
        return 0.0
    corners = [(r.left, r.bottom), (r.right, r.bottom), (r.right, r.top), (r.left, r.top)]
    return min(_segment_distance_sq(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _touch(u, v):
    """True when two copper items of one net overlap on a shared layer."""
    if not (u[1] & v[1]):
        return False
    ku, gu, kv, gv = u[0], u[2], v[0], v[2]
    if ku == "track" and kv != "track":
        return _touch(v, u)
    if ku == "pad":
        if kv == "pad":
            return gu.overlaps(gv)
        if kv == "track":
            a, b, w = gv
            return _segment_rect_d2(a, b, gu) <= (w / 2) ** 2 + EPS
        return _point_rect_d2(gv[0], gu) <= gv[1] ** 2 + EPS
    if ku == "via":
        if kv == "via":
            return math.dist(gu[0], gv[0]) <= gu[1] + gv[1] + EPS
        a, b, w = gv
        return _segment_distance_sq(a, b, gu[0], gu[0]) <= (gu[1] + w / 2) ** 2 + EPS
    a, b, w = gu
    c, d, x = gv
    return _segment_distance_sq(a, b, c, d) <= ((w + x) / 2) ** 2 + EPS


def pin_groups(graph, tracks, vias, via_diameter):
    """{net: [[pin indices], ...]} for every net of ``graph``: its pins (indices into the
    net's ``pins``) joined by the given copper, where pads, tracks and vias of the net
    count as joined when they overlap on a shared layer. A geometric check, not DRC."""
    layers = {"F.Cu", "B.Cu"} | {t[1] for t in tracks}
    items = {}
    for comp in graph.components:
        side = "B.Cu" if comp.side == "bottom" else "F.Cu"
        for (name, net, r), pad in zip(pad_rects(comp), comp.pads):
            if net:
                on = set(layers) if pad.through_hole else {side}
                items.setdefault(net, []).append(("pad", on, r, (comp.ref, name)))
    for net, layer, a, b, w in tracks:
        items.setdefault(net, []).append(("track", {layer}, (tuple(a), tuple(b), float(w)), None))
    for net, x, y in vias:
        items.setdefault(net, []).append(("via", set(layers), ((x, y), via_diameter / 2), None))
    out = {}
    for n in graph.nets:
        found = items.get(n.name, [])
        parent = list(range(len(found)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(found)):
            for j in range(i + 1, len(found)):
                if find(i) != find(j) and _touch(found[i], found[j]):
                    parent[find(i)] = find(j)
        at = {it[3]: k for k, it in enumerate(found) if it[0] == "pad"}
        groups = {}
        for index, pin in enumerate(n.pins):
            k = at.get(tuple(pin))
            groups.setdefault(("pin", index) if k is None else find(k), []).append(index)
        out[n.name] = sorted(sorted(g) for g in groups.values())
    return out


def missing(groups):
    return sum(len(g) - 1 for g in groups.values())


def route_metrics(route):
    from pnr.place.initial_pool import _route_metrics

    return _route_metrics(route)


def route_rank(record):
    """A knit's sort key (:func:`pnr.place.initial_pool.route_rank`)."""
    from pnr.place.initial_pool import route_rank as rank

    return rank(record)


def knit(case, k, flat, reps, choice=None, attempt=0, label=None, start=None):
    """Route the nets between blocks for one placed seed; returns its record.

    ``attempt`` numbers the representative-pad retries of a seed: each attempt routes in
    its own trace scope (``top-NN-route``, then ``top-NN-route-r1``, ...), and the record's
    ``id`` is the scope id the recorder assigned, so a selection names exactly the route
    whose copper the record carries. ``label`` names the scope instead (the route-then-
    compact reroutes, ``compact-...-route``)."""
    from pnr.route.detail.router import route_board

    frames, fab, budget = case["frames"], case["rules"]["fab"], case["budget"]
    tracks, vias, poses = [], [], {}
    for name, frame in sorted(frames.items()):
        centre, turn = macro_pose(frame, flat)
        poses[name] = [centre[0], centre[1], turn]
        t, v = block_copper(frame, centre, turn)
        tracks += t
        vias += v
    top = top_graph(flat, frames, reps, choice)
    fixed = fixed_copper(tracks, vias, fab)
    groups = pin_groups(flat, tracks, vias, float(fab["via_diameter_mm"]))
    label = label or "top-%02d-route" % k + ("-r%d" % attempt if attempt else "")
    retry = dict(attempt=attempt) if attempt else {}
    with trace.scope(label, "route", start=start or "top-%02d" % k, **retry):
        recorder = trace.current()
        if recorder is not None:
            label = recorder.scope_id
            joined = {n: g for n, g in groups.items() if len(g) < len(flat.net(n).pins)}
            recorder.fixed(copper_blob(tracks, vias, case["layers"], fab), groups=joined)
        route = route_board(
            top,
            case["constraints"],
            case["rules"],
            pitch=budget["route_pitch_mm"],
            max_iters=budget["route_iters"],
            fixed_copper=fixed,
            fixed_copper_own_net=True,
        )
    top_tracks = [[n, la, list(a), list(b), w] for n, la, a, b, w in route.tracks]
    top_vias = [[n, x, y] for n, x, y in route.vias]
    final = pin_groups(flat, tracks + top_tracks, vias + top_vias, float(fab["via_diameter_mm"]))
    metrics = route_metrics(route)
    split = {n: g for n, g in final.items() if len(g) > 1}
    return dict(
        id=label,
        placed=flat,
        route=route,
        macro_poses=poses,
        block_tracks=tracks,
        block_vias=vias,
        top_tracks=top_tracks,
        top_vias=top_vias,
        missing=missing(final),
        choice=dict(choice or {}),
        seed_index=k,
        split_nets=sorted(split),
        unresolved=metrics["unresolved_nets"],
        deferred_nets=metrics["deferred_nets"],
        objective=[
            missing(final),
            len(metrics["unresolved_nets"]),
            len(top_vias),
            round(metrics["copper_length_mm"], 6),
        ],
        **(
            {"length_unmatched": metrics["length_unmatched"]}
            if "length_unmatched" in metrics
            else {}
        ),
        representatives={
            "%s/%s" % key: "%s.%s" % reps[key][(choice or {}).get(key, 0) % len(reps[key])]
            for key in sorted(reps)
        },
    )


# ------------------------------------------------------------------------ route-then-compact


def routed_geometry(rules):
    """``geometry_of`` for :func:`pnr.hier.top.hierarchical_place`: an instance's measured
    extent and shapes (PNR_MACRO_SHRINK / PNR_MACRO_HULL) from its trial's in-memory
    routes, as this driver keeps no native board per block."""
    from pnr.hier.extent import BlockGeometry, routes_geometry

    def measure(block, sub, rec):
        routes = (rec.get("routes") or {}).get(block.name)
        if routes is None:
            return BlockGeometry(ok=False, reason="no routes for %s" % block.name)
        try:
            return routes_geometry(sub.components, routes["tracks"], routes["vias"], rules)
        except Exception as error:  # the macro keeps its rectangle, with the reason
            return BlockGeometry(ok=False, reason="%s: %s" % (type(error).__name__, error))

    return measure


def macro_shapes(frame, centre, turn, margin, hull=None):
    """A placed macro's compaction shapes (board frame): its hull cover rectangles per plane
    (``hull`` = (hull, origin in the block frame), PNR_MACRO_HULL), else its outline grown
    by ``margin`` on every plane (a block reserves both sides)."""
    from pnr.graph import Component
    from pnr.place import route_compact as rc
    from pnr.place.hull import hull_placement_rects

    w, h = frame["width"], frame["height"]
    if hull is not None:
        shape, origin = hull
        dx, dy = _rot(origin[0] - w / 2, origin[1] - h / 2, turn)
        body = Component(
            ref="hull",
            footprint="block:hull",
            pos=(centre[0] + dx, centre[1] + dy),
            rot=turn,
            side="top",
            courtyard=tuple(shape["courtyard"]),
            bbox=tuple(shape["courtyard"]),
            hull=shape,
        )
        return [
            (plane, r.left, r.bottom, r.right, r.top) for plane, r in hull_placement_rects(body)
        ]
    cw, ch = (w, h) if int(round(turn)) % 180 == 0 else (h, w)
    return [
        (
            rc.ALL,
            centre[0] - cw / 2 - margin,
            centre[1] - ch / 2 - margin,
            centre[0] + cw / 2 + margin,
            centre[1] + ch / 2 + margin,
        )
    ]


def top_compact(case, best, graph, constraints, reps):
    """PNR_ROUTE_COMPACT TOP: the gutters between the placed macros and top-level parts
    shrink to the knitted copper they hold (x, then y), the nets between the blocks are
    ripped up and knitted again, a worse knit backs off (:mod:`pnr.place.route_compact`).
    Returns ``(record, report)``: the knit record to keep and the compaction report."""
    from pnr.hier.extent import copper_clearance
    from pnr.live import emit
    from pnr.place import route_compact as rc
    from pnr.place.geometry import outline_size

    frames, rules = case["frames"], case["rules"]
    fab = rules["fab"]
    margin = float(fab.get("edge_clearance_mm", 0.2))
    macros = case["macros"]
    hulls = case.get("hulls") or {}
    via_d = float(fab["via_diameter_mm"])
    clearance = copper_clearance(rules)
    k = int(best["seed_index"])
    steps, routed, kept = [], [], []

    def items_of(flat):
        rigid, shapes = [], {}
        for name, frame in sorted(frames.items()):
            centre, turn = macro_pose(frame, flat)
            rigid.append((macros[name], [c.ref for c in frame["sub"].components]))
            shapes[macros[name]] = macro_shapes(frame, centre, turn, margin, hulls.get(name))
        return rc.graph_items(flat, constraints, rigid=rigid, shapes=shapes)

    def copper_of(rec):
        return rc.Copper.from_routes(rec["top_tracks"], rec["top_vias"], via_d, clearance)

    def announce(candidate, axis, record):
        steps.append(axis)
        label = "compact-%02d-%s" % (len(steps), "xy"[axis])
        stats = {
            key: record.get(key)
            for key in (
                "moved",
                "gutter_mean_mm",
                "gutter_need_mean_mm",
                "gutter_after_mean_mm",
                "span_before_mm",
                "span_after_mm",
                "anchor",
            )
        }
        with trace.scope(label, "stage", kind="route-compact", axis="xy"[axis], **stats):
            recorder = trace.current()
            if recorder is not None:
                recorder.poses("compaction", candidate, phase="placement")
        emit(
            "route_compact",
            layout=json.loads(candidate.to_json()),
            data=dict(phase="route-compact %s (top level)" % "xy"[axis], step=len(steps), **stats),
        )

    def reroute(candidate, axis):
        label = "compact-%02d-%s" % (len(steps), "xy"[axis])
        rec = knit(
            case, k, candidate, reps, best.get("choice"), label=label + "-route", start=label
        )
        routed.append(rec)
        return candidate, rec

    import math as _math

    block_mm = sum(_math.dist(a, b) for _n, _la, a, b, _w in best["block_tracks"])
    # The flat board's pair and plane guards apply at the top level too: the knit may
    # leave a coupled pair's legs uncoupled or starve a legacy plane pad of its
    # dog-bone site, the same KiCad failures a flat board's reroute guards against.
    # ``rec["placed"]`` is the fully flattened graph (every block's own parts at their
    # placed pose), so the whole board's pads and copper are available here, not just
    # the inter-block nets ``copper_of`` measures gutters from.
    legacy_planes = rc.legacy_plane_path(rules, best["placed"])
    pair_nets = rc.pair_nets_of(rules)

    def pair_copper(rec):
        return sorted(
            (t[0], t[1], round(t[2][0], 4), round(t[2][1], 4), round(t[3][0], 4), round(t[3][1], 4))
            for t in rec["block_tracks"] + rec["top_tracks"]
            if t[0] in pair_nets
        )

    base_pairs = pair_copper(best) if pair_nets else None

    def metrics_of(rec):
        out = dict(
            missing=rec["missing"],
            unresolved=len(rec["unresolved"]),
            deferred=len(rec.get("deferred_nets") or []),
            unmatched=rec.get("length_unmatched", 0),
            vias=len(rec["top_vias"]),
            copper_mm=round(float(rec["objective"][3]), 3),
            # the copper tolerance is a share of the whole board's copper (blocks included)
            copper_ref_mm=round(float(rec["objective"][3]) + block_mm, 3),
        )
        if legacy_planes:
            out["plane_blocked"] = rc.plane_blocked(
                rec["placed"],
                rec["block_tracks"] + rec["top_tracks"],
                rec["block_vias"] + rec["top_vias"],
                rules,
            )
        if base_pairs is not None:
            out["pairs_moved"] = int(pair_copper(rec) != base_pairs)
        return out

    placed, rec, report = rc.guarded(
        "top",
        (best["placed"], best),
        lambda: rc.compact_loop(
            best["placed"],
            best,
            constraints=constraints,
            items_of=items_of,
            copper_of=copper_of,
            reroute=reroute,
            metrics_of=metrics_of,
            min_gap=float(constraints.board.default_clearance_mm),
            outline=outline_size(graph, constraints),
            label="top",
            announce=announce,
            observe=lambda stage, placed, route, record: kept.append(route["id"]),
        ),
    )
    # The knits kept, in order (the hierarchical animation replays them as chapter 4).
    trace.select(
        "route-compact",
        [best["id"]] + [r["id"] for r in routed],
        kept or [best["id"]],
        "not-worse-knit",
        {r["id"]: r["objective"] for r in [best] + routed},
    )
    report["routes"] = len(routed)
    return rec, report


# ------------------------------------------------------------------------ the run


def run(root, seed):
    """The whole hierarchical flow for one case directory: ``(pnr report, case)``, where
    ``case`` holds the block frames, synthesis records and representatives."""
    from pnr.hier.top import hierarchical_place
    from pnr.place.initial_pool import _prepared_source, preserve_source_locks
    from pnr.place.legalize import LegalizationError
    from pnr.place.metrics import hard_violations, hpwl

    started = time.monotonic()
    spec, graph, constraints, rules = load(root)
    (root / "rules.json").write_text(json.dumps(rules, indent=2))
    budget = budget_of(spec)
    trace_root = os.environ.get(trace.ENV_DIR) or None
    locked = preserve_source_locks(graph, constraints)
    source = _prepared_source(graph, locked, rules)

    # 1. Blocks, each template on its own board (and its own trace directory).
    blocks, synth = synthesize(source, locked, rules, budget, seed, trace_root)
    frames = frames_of(source, locked, rules, blocks, synth)
    macros = macro_refs(blocks)
    library = {name: [t["chosen"]] for t in synth for name in t["blocks"]}
    layers = trace.copper_layer_names(int(constraints.board.layers))
    case = dict(
        frames=frames,
        rules=rules,
        budget=budget,
        constraints=constraints,
        layers=layers,
        synth=synth,
        macros=macros,
    )

    # The case trace starts once every block recorder is closed (one recorder per directory).
    recorder = trace.current()
    if recorder is not None:
        recorder.begin_board(graph, constraints, rules)
        with trace.scope("hier-blocks", "block", blocks=len(frames), templates=len(synth)):
            entries = []
            for name, frame in sorted(frames.items()):
                t, v = block_copper(frame)
                entries.append(
                    dict(
                        block=name,
                        template=frame["template"],
                        trace="blocks/" + frame["template"],
                        trial=frame["trial"],
                        macro=macros[name],
                        size_um=[trace.um(frame["width"]), trace.um(frame["height"])],
                        members={
                            c.ref: [
                                trace.um(c.pos[0]),
                                trace.um(c.pos[1]),
                                trace.angle(c.rot),
                                c.side,
                            ]
                            for c in sorted(frame["sub"].components, key=lambda c: c.ref)
                        },
                        copper=recorder.blob(copper_blob(t, v, layers, rules["fab"])),
                    )
                )
            recorder.event("blocks", blocks=entries, phase="placement")

    # 2 and 3. Top level: place the macros, then knit, for every seed.
    reps = case["representatives"] = representatives(frames)
    seeds, routed = [], []
    for k in range(int(budget["top_seeds"])):
        top_seed = seed + k
        record = dict(id="top-%02d" % k, seed=top_seed)
        seeds.append(record)
        with trace.scope(record["id"], "start", kind="hier-top", seed=top_seed):
            try:
                flat, report, placement = hierarchical_place(
                    graph,
                    constraints,
                    rules,
                    library,
                    top_seed,
                    iters=budget["top_iters"],
                    geometry_of=routed_geometry(rules),
                )
            except LegalizationError as error:
                record.update(legal=False, error=str(error))
                trace.note(status="illegal")
                continue
            if placement.get("hulls"):  # PNR_MACRO_HULL: the shapes route-compact slides
                case["hulls"] = placement["hulls"]
            got = {v["block"]: m for m, v in placement["macros"].items()}
            if got != macros:
                raise RuntimeError("macro naming differs from the block event: %r" % got)
            bad = {k2: v for k2, v in hard_violations(flat, constraints).items() if v}
            record.update(legal=bool(report.legal and not bad), hpwl_mm=round(hpwl(flat), 3))
            if bad:
                record["violations"] = bad
            trace.note(status="ok" if record["legal"] else "illegal", hpwl_mm=record["hpwl_mm"])
        if not record["legal"]:
            continue
        result = knit(case, k, flat, reps)
        tries = 0
        while result["split_nets"] and tries < int(budget["representative_retries"]):
            # Fallback: the next representative pads for the blocks of the failing nets.
            tries += 1
            choice = {
                key: tries
                for key in reps
                if key[1] in set(result["split_nets"]) and len(reps[key]) > 1
            }
            if not choice:
                break
            retry = knit(case, k, flat, reps, choice, attempt=tries)
            if route_rank(retry) < route_rank(result):
                result = retry
        record.update(
            objective=result["objective"],
            missing=result["missing"],
            split_nets=result["split_nets"],
            representative_retries=tries,
        )
        from pnr.place import compact

        if compact.enabled("RANK"):
            # PNR_COMPACT RANK: the compactness bucket after the completion keys (route_rank).
            from pnr.place.geometry import outline_size

            measured = compact.metrics(result["placed"], *outline_size(graph, constraints))
            result["bucket"] = measured["bucket"]
            record["compactness"] = measured
        routed.append(result)
        print(
            "Hierarchical top seed %d: HPWL %.0f mm, missing %d, objective %s"
            % (top_seed, record["hpwl_mm"], result["missing"], result["objective"]),
            flush=True,
        )
    if not routed:
        raise RuntimeError("no legal top-level placement among %d seeds" % len(seeds))
    best = min(routed, key=lambda r: (route_rank(r), r["id"]))
    trace.select(
        "top-seed",
        [r["id"] for r in routed],
        best["id"],
        "route-objective",
        {r["id"]: r["objective"] for r in routed},
    )
    from pnr.place import route_compact

    compaction = {}
    if route_compact.enabled("TOP"):
        # PNR_ROUTE_COMPACT TOP: gutters shrink to the knitted copper, then knit again.
        best, compaction["top"] = top_compact(case, best, graph, constraints, reps)
        top = compaction["top"]
        if "error" in top:
            print("Hierarchical route-compact failed (kept the knit): %s" % top["error"])
        else:
            print(
                "Hierarchical route-compact: bbox %.0f -> %.0f mm2, %d step(s) accepted, %.1fs"
                % (
                    top["before"]["bbox_mm2"],
                    top["after"]["bbox_mm2"],
                    top["accepted"],
                    top["seconds"]["total"],
                ),
                flush=True,
            )
    blocks_compacted = {t["template"]: t["compacted"] for t in synth if t.get("compacted")}
    if blocks_compacted:
        compaction["blocks"] = blocks_compacted
    placed = best["placed"]
    (root / "placed.json").write_text(placed.to_json())
    unrouted = sorted(set(best["unresolved"]) | set(best["split_nets"]))
    (root / "routes.json").write_text(
        json.dumps(
            dict(
                tracks=best["block_tracks"] + best["top_tracks"],
                vias=best["block_vias"] + best["top_vias"],
                unrouted=unrouted,
            ),
            indent=2,
        )
    )
    legal = not any(hard_violations(placed, constraints).values())
    converged = best["missing"] == 0 and not unrouted
    templates = [
        dict(
            template=t["template"],
            blocks=t["blocks"],
            chosen=t["chosen"]["id"],
            trials=[
                {
                    key: r.get(key)
                    for key in (
                        "id",
                        "status",
                        "seed",
                        "width",
                        "height",
                        "utilisation",
                        "aspect",
                        "missing",
                        "port_debt_mm",
                        "n_vias",
                        "copper_mm",
                        "seconds",
                    )
                }
                for r in t["trials"]
            ],
        )
        for t in synth
    ]
    report = dict(
        converged=converged,
        legal=legal,
        rounds=1,
        best_round=1,
        termination="hierarchical_knit_complete" if converged else "hierarchical_knit_incomplete",
        connection_history=[best["missing"]],
        unrouted=unrouted,
        deferred=sorted(best["route"].deferred_nets),
        initial_pool={},
        escape_diagnostics={},
        maze_kernel=maze_status(),
        exact_separation=exact_mode(),
        elapsed_seconds=time.monotonic() - started,
        summary="hierarchical: %d blocks (%d templates), seed %s of %d, %d missing connections"
        % (len(frames), len(synth), best["id"], len(seeds), best["missing"]),
        hier=dict(
            budget=budget,
            templates=templates,
            macros={
                macros[name]: dict(
                    block=name,
                    template=frames[name]["template"],
                    trial=frames[name]["trial"],
                    size_mm=[frames[name]["width"], frames[name]["height"]],
                    pose=best["macro_poses"][name],
                )
                for name in sorted(frames)
            },
            representatives=best["representatives"],
            seeds=seeds,
            selected=best["id"],
            block_copper=dict(tracks=len(best["block_tracks"]), vias=len(best["block_vias"])),
            **(
                {"hulls": {b: h[0].get("stats") for b, h in sorted(case["hulls"].items())}}
                if case.get("hulls")
                else {}
            ),
            top_copper=dict(tracks=len(best["top_tracks"]), vias=len(best["top_vias"])),
        ),
    )
    from pnr.place import compact

    if compact.shrink_enabled():  # PNR_SHRINK is the flat driver's: recorded as skipped
        report["shrink"] = dict(skipped="hier driver")
    if compaction:
        report["route_compact"] = compaction
    (root / "pnr-report.json").write_text(json.dumps(report, indent=2))
    return report, case


def main(argv=None):
    from pnr.place.route_compact import arm_deadline

    arm_deadline()  # PNR_ROUTE_COMPACT keeps to the runner's stage budget from here
    argv = sys.argv[1:] if argv is None else argv
    root, seed = Path(argv[0]), int(argv[1])
    report, _case = run(root, seed)
    print(report["summary"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
