"""Route-then-compact inside a hierarchical block (``PNR_ROUTE_COMPACT`` part ``BLOCK``).

A block template's chosen layout is routed on its own small board. Its parts squeeze
together by the copper that routing actually used (:mod:`pnr.place.route_compact`, x then
y), the block outline shrinks to the parts plus the margin each side needs (the edge
clearance, or the copper the route ran outside the parts there), and every instance of the
template routes again on the smaller board (:func:`pnr.hier.synth.run_trial` with the
compacted layout). A step that routes worse is refused and backs off; the macro the top
level places is then smaller by what the block gave up.
"""

from __future__ import annotations

import math

from pnr.graph import BoardGraph


def _grid_up(v, g):
    return math.ceil(v / g - 1e-9) * g


def _bbox(graph):
    from pnr.place.geometry import placement_rects

    rects = [r for c in graph.components for _s, r in placement_rects(c)]
    return (
        min(r.left for r in rects),
        min(r.bottom for r in rects),
        max(r.right for r in rects),
        max(r.top for r in rects),
    )


def _copper_box(rec):
    xs, ys = [], []
    for routes in (rec.get("routes") or {}).values():
        for _n, _la, a, b, w in routes.get("tracks", []):
            for p in (a, b):
                xs += [p[0] - w / 2, p[0] + w / 2]
                ys += [p[1] - w / 2, p[1] + w / 2]
        for v in routes.get("vias", []):
            xs.append(v[1])
            ys.append(v[2])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def shrunk_frame(candidate, rec, edge_mm, grid=0.25):
    """(width, height, dx, dy): the smallest outline around ``candidate``'s parts that keeps,
    per side, the edge clearance plus a grid step, or the room the routed copper of ``rec``
    used outside its parts there, whichever is larger, and never more than that side had;
    ``dx, dy`` move the parts into it (grid multiples)."""
    w, h = float(rec["width"]), float(rec["height"])
    x0, y0, x1, y1 = _bbox(BoardGraph.from_json(rec["placed"]))
    copper = _copper_box(rec) or (x0, y0, x1, y1)
    floor = edge_mm + grid
    sides = dict(
        left=(x0, x0 - copper[0]),
        bottom=(y0, y0 - copper[1]),
        right=(w - x1, copper[2] - x1),
        top=(h - y1, copper[3] - y1),
    )
    margin = {
        k: min(had, max(floor, overhang + edge_mm if overhang > 0 else floor))
        for k, (had, overhang) in sides.items()
    }
    c0, c1, c2, c3 = _bbox(candidate)
    dx = _grid_up(margin["left"] - c0, grid)
    dy = _grid_up(margin["bottom"] - c1, grid)
    width = min(w, _grid_up(c2 + dx + margin["right"], grid))
    height = min(h, _grid_up(c3 + dy + margin["top"], grid))
    return width, height, dx, dy


def compact_block(graph, constraints, rules, blocks, names, rec, *, iters, route_iters, pitch):
    """``(record, report)``: ``rec`` (a :func:`pnr.hier.synth.run_trial` record of the
    template ``names``) compacted and routed again, or ``rec`` itself when no step held."""
    from pnr.hier.blocks import sub_board
    from pnr.hier.extent import copper_clearance
    from pnr.hier.synth import run_trial
    from pnr.place import route_compact as rc
    from pnr.place.metrics import hard_violations

    rep = blocks[names[0]]
    fab = rules.get("fab") or {}
    edge = float(fab.get("edge_clearance_mm", 0.2))
    via_d = float(fab.get("via_diameter_mm", 0.6))
    clearance = copper_clearance(rules)
    cache = {}

    def board_constraints(w, h):
        key = (round(w, 6), round(h, 6))
        if key not in cache:
            cache[key] = sub_board(graph, constraints, rules, rep, w, h)[1]
        return cache[key]

    def bad(placed, w, h):
        return {k: v for k, v in hard_violations(placed, board_constraints(w, h)).items() if v}

    state = dict(rec=rec)
    base_bad = bad(BoardGraph.from_json(rec["placed"]), rec["width"], rec["height"])

    def items_of(placed):
        r = state["rec"]
        return rc.graph_items(placed, board_constraints(r["width"], r["height"]))

    def copper_of(r):
        tracks, vias = [], []
        for routes in (r.get("routes") or {}).values():  # every instance: one shared frame
            tracks += routes.get("tracks", [])
            vias += routes.get("vias", [])
        return rc.Copper.from_routes(tracks, vias, via_d, clearance)

    def reroute(candidate, axis):
        r = state["rec"]
        width, height, dx, dy = shrunk_frame(candidate, r, edge)
        if width * height >= r["width"] * r["height"] - 1e-9:
            return "outline does not shrink", None
        moved = BoardGraph.from_json(candidate.to_json())
        for c in moved.components:
            c.pos = (c.pos[0] + dx, c.pos[1] + dy)
        if len(bad(moved, width, height)) > len(base_bad):
            return "hard violation in the shrunk outline", None
        size = (width, height, r.get("utilisation"), r.get("aspect"))
        out = run_trial(
            graph,
            constraints,
            rules,
            blocks,
            names,
            size,
            r["seed"],
            iters,
            route_iters,
            reuse=moved.to_json(),
            pitch=pitch,
        )
        if out.get("status") != "ok":
            return "reroute %s" % out.get("status"), None
        out["id"] = rec.get("id")
        out["compacted_from"] = [rec["width"], rec["height"]]
        return BoardGraph.from_json(out["placed"]), out

    def accepted(stage, placed, r, record):
        state["rec"] = r

    def metrics_of(r):
        return dict(
            missing=r.get("missing", 0),
            unresolved=0,
            unmatched=r.get("length_unmatched", 0),
            vias=r.get("n_vias", 0),
            copper_mm=round(float(r.get("copper_mm", 0.0)), 3),
        )

    placed, out, report = rc.guarded(
        "block:" + names[0],
        (None, rec),
        lambda: rc.compact_loop(
            BoardGraph.from_json(rec["placed"]),
            rec,
            constraints=board_constraints(rec["width"], rec["height"]),
            items_of=items_of,
            copper_of=copper_of,
            reroute=reroute,
            metrics_of=metrics_of,
            min_gap=float(
                board_constraints(rec["width"], rec["height"]).board.default_clearance_mm
            ),
            outline=lambda r: (r["width"], r["height"]),
            label="block:" + names[0],
            observe=accepted,
            check=lambda a, b: rc.new_violations(
                a, b, board_constraints(state["rec"]["width"], state["rec"]["height"])
            ),
        ),
    )
    report["block_mm"] = dict(
        before=[rec["width"], rec["height"]], after=[out["width"], out["height"]]
    )
    return out, report
