"""Plane or trace, and who takes the leftover: the allocation of a dedicated plane
layer among its candidate rails, decided by place-and-route feedback.

A ``plane_partition`` entry without a ``region`` lists **candidate** rails of a
dedicated plane layer (:mod:`pnr.power_spec`). Which of them get a territory, and
which net takes what is left of the layer, is a design decision with real
consequences: a milliamp rail given a third of the layer walls the main rail in and
slots the reference plane under the adjacent signal layer; a rail traced that needed
copper misses its IR budget. :func:`route` makes the decision the way the rest of the
engine makes decisions -- by enumerating, screening and routing:

1. **Alternatives.** Per layer, every split of the candidates into plane rails (at
   least one) and traced rails, times every leftover option: none (the plane rails
   compete for it), one of the plane rails (it alone grows; the others keep their trunk
   and lands), or a ``fill_candidates`` net. A spec may force a rail
   (``must_plane`` / ``must_trace``) or the leftover (``fill``); everything else is
   left to the engine.
2. **Hard constraints only** prune: a traced rail must carry its current at its class
   width (IPC-2221, external, 10 C) and meet its IR budget along the estimated trace
   (:func:`trace_estimate`).
3. **Screen.** Each surviving alternative is partitioned on the inputs of one probe
   route (the router's own grid and fanouts, every candidate a plane) at a coarser
   raster, and scored in millimetres of track: the partition's quality penalty
   (:func:`pnr.plane_quality.penalty`: unreachable terminals, dead area, detours,
   fragmentation, split length, a redundant fill), each traced rail's estimated cost
   (its terminals' spanning tree, longer through crowded pads, plus its vias), each
   plane rail's drop vias, and the static rule of thumb (:func:`pnr.plane_partition.
   _rail_decision`: a declared budget, 10 mA, 6 terminals) as a small prior only.
4. **Route the finalists.** The best ``FINALISTS`` (3; ``PNR_RAIL_FINALISTS``)
   alternatives, each plane / trace split's best first, are each routed by the real
   router on the same placement, and the one
   with the fewest missing connections and unresolved nets, then the lowest copper
   length plus vias plus partition penalty, wins (ties: the alternative's name).

The decision is made on the first route of a board in a process (the initial pool's
first finalist, or the first P/R round) and held for its later routes (other
placements of the same design), so the placement search compares like with like and
pays for the comparison once. ``PNR_RAIL_ALLOC=static`` restores the rule of thumb as
the decision (the A/B arm). The report (``escape_diagnostics.rail_allocation``) names
the chosen allocation and, per rail, the numbers that decided it.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

FINALISTS = 3  # alternatives routed per decision
SCREEN_H_MM = 0.25  # the screen's raster (the real partition keeps the entry's h_mm)
VIA_MM = 1.0  # a via costs this much track (the routed objective and the screen)
PRIOR_MM = 5.0  # per rail the static rule of thumb would decide otherwise
TRACE_DETOUR = 1.2  # a trace's length over its terminals' spanning tree
TRACE_DT_C = 10.0  # IPC-2221 temperature rise a traced rail's width must carry
_MEMO: Dict[str, dict] = {}


@dataclass(frozen=True)
class Choice:
    """One layer's allocation."""

    layer: str
    plane: Tuple[str, ...]
    trace: Tuple[str, ...]
    fill: Optional[str]

    def key(self) -> str:
        return "%s: plane %s | trace %s | leftover %s" % (
            self.layer,
            "+".join(self.plane) or "-",
            "+".join(self.trace) or "-",
            self.fill or "shared",
        )


def mode() -> str:
    value = os.environ.get("PNR_RAIL_ALLOC", "search")
    if value not in ("search", "static"):
        raise ValueError("PNR_RAIL_ALLOC is search or static, not %r" % value)
    return value


def finalists() -> int:
    k = int(os.environ.get("PNR_RAIL_FINALISTS", str(FINALISTS)))
    if not 1 <= k <= 6:
        raise ValueError("PNR_RAIL_FINALISTS is 1 to 6")
    return k


def plane_entries(rules) -> List[dict]:
    """The dedicated-plane entries of ``rules['plane_partition']`` (no ``region``)."""
    return [e for e in (rules or {}).get("plane_partition") or () if not e.get("region")]


def candidates(entry, stack) -> List[str]:
    """``entry``'s candidates that are plane nets of its layer (as for_route reads
    them)."""
    if stack is None or entry["layer"] not in stack.names:
        return []
    lay = stack.layer(entry["layer"])
    return [n for n in entry["nets"] if n in lay.nets]


def choices(entry, cands: Sequence[str]) -> List[Choice]:
    """Every allocation of ``entry``'s layer the spec leaves open (see the module
    doc), canonical (one plane rail and no leftover owner: that rail owns it) and
    without duplicates, in a fixed order."""
    layer = entry["layer"]
    cands = list(cands)
    forced_fill = entry.get("fill")
    must_plane = set(entry.get("must_plane") or ()) & set(cands)
    must_trace = set(entry.get("must_trace") or ()) & set(cands)
    if forced_fill in cands:
        must_plane.add(forced_fill)
    if must_plane & must_trace:
        raise ValueError(
            "plane_partition %s: %s both must_plane and must_trace"
            % (layer, ", ".join(sorted(must_plane & must_trace)))
        )
    open_ = [n for n in cands if n not in must_plane | must_trace]
    extra = [n for n in entry.get("fill_candidates") or () if n not in cands]
    out, seen = [], set()
    for mask in range(1 << len(open_)):
        chosen = {n for k, n in enumerate(open_) if mask >> k & 1}
        plane = tuple(n for n in cands if n in must_plane or n in chosen)
        if not plane:
            continue
        trace = tuple(n for n in cands if n not in plane)
        fills = [forced_fill] if forced_fill else [None] + list(plane) + extra
        for fill in fills:
            if fill is None and len(plane) == 1:
                fill = plane[0]
            c = Choice(layer, plane, trace, fill)
            if c not in seen:
                seen.add(c)
                out.append(c)
    return sorted(out, key=lambda c: c.key())


# ---------------------------------------------------------------- estimates


def _pads(graph, net):
    """``[(ref, pad, (x, y), side, through_hole)]`` of ``net`` (absolute centres)."""
    from pnr.place.geometry import pad_rects

    out = []
    for comp in graph.components:
        for (name, n, r), pad in zip(pad_rects(comp), comp.pads):
            if n == net:
                out.append((comp.ref, name, (r.cx, r.cy), comp.side, bool(pad.through_hole)))
    return out


def _budget_mohm(rules, entry, net, current):
    budget = (entry.get("budgets_mohm") or {}).get(net)
    if budget:
        return float(budget)
    for ir in rules.get("ir_drop") or ():
        if ir.get("net") != net:
            continue
        if ir.get("budget_mohm"):
            return float(ir["budget_mohm"])
        amps = current or ir.get("current_a")
        if ir.get("budget_mv") and amps:
            return float(ir["budget_mv"]) / float(amps)
    return None


def _source_point(rules, entry, net, pads):
    """Where a rail's current enters: its ``ir_drop`` / ``sources`` pad, else a
    through-hole pad (a connector), else the pads' centroid."""
    names = []
    for ir in rules.get("ir_drop") or ():
        if ir.get("net") == net:
            src = ir.get("sources")
            if isinstance(src, dict):
                names += ["%s:%s" % (ref, p) for ref, ps in src.items() for p in ps]
            elif src:
                names += list(src)
    if (entry.get("sources") or {}).get(net):
        names.append(entry["sources"][net])
    for text in names:
        ref, _, pad = str(text).partition(":")
        for r, p, at, _s, _th in pads:
            if r == ref and p == pad:
                return at
    for _r, _p, at, _s, th in pads:
        if th:
            return at
    xs = [p[2][0] for p in pads] or [0.0]
    ys = [p[2][1] for p in pads] or [0.0]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def trace_estimate(graph, rules, entry, net, fanout_refs=frozenset()):
    """A traced rail's estimated cost: its pads' Euclidean spanning tree (an upper bound
    on their Steiner tree), longer by the crowding of other nets' pads along it (signal
    congestion), its vias (a ball under a declared fanout escapes by one; pads on both
    sides add one), and its DC drop over the root's path at the class width."""
    from pnr.electrical import current_width
    from pnr.ir_drop import barrel_ohm, resistivity
    from pnr.plane_quality import emst
    from pnr.power_spec import rail_current

    pads = _pads(graph, net)
    pts = [p[2] for p in pads]
    tree = emst(pts)
    # Congestion: foreign pads within 0.6 mm of the tree's straight segments, per mm.
    segs = []
    if len(pts) > 1:
        used = [0]
        rest = list(range(1, len(pts)))
        while rest:
            a, b = min(
                ((u, v) for u in used for v in rest),
                key=lambda e: (math.dist(pts[e[0]], pts[e[1]]), e),
            )
            segs.append((pts[a], pts[b]))
            used.append(b)
            rest.remove(b)
    crowd = 0
    if segs:
        from pnr.place.geometry import pad_rects

        for comp in graph.components:
            for _name, n, r in pad_rects(comp):
                if n == net:
                    continue
                x, y = r.cx, r.cy
                for (ax, ay), (bx, by) in segs:
                    dx, dy = bx - ax, by - ay
                    den = dx * dx + dy * dy
                    t = (
                        0.0
                        if den == 0
                        else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / den))
                    )
                    if math.hypot(x - ax - t * dx, y - ay - t * dy) <= 0.6:
                        crowd += 1
                        break
    density = crowd / max(tree, 1.0)
    congestion = 1.0 + min(2.0, 0.5 * density)
    vias = sum(1 for r, _p, _a, _s, th in pads if r in fanout_refs and not th)
    if len({s for _r, _p, _a, s, th in pads if not th}) > 1:
        vias += 1
    length = TRACE_DETOUR * tree
    fab = rules.get("fab") or {}
    width = float(fab.get("track_width_mm", 0.25))
    for nc in rules.get("net_classes") or ():
        if net in nc.get("nets", ()) and nc.get("width_mm"):
            width = max(width, float(nc["width_mm"]))
    current = rail_current(rules, net, (entry.get("currents") or {}).get(net))
    rho = resistivity(25.0)
    copper = 0.035
    root = _source_point(rules, entry, net, pads)
    path = TRACE_DETOUR * max((abs(p[0] - root[0]) + abs(p[1] - root[1]) for p in pts), default=0.0)
    drill = float(fab.get("via_drill_mm", 0.3))
    r_mohm = 1e3 * (rho / copper * path / width + min(vias, 2) * barrel_ohm(1.6, drill, rho=rho))
    out = dict(
        terminals=len(pads),
        tree_mm=round(tree, 3),
        length_mm=round(length, 3),
        congestion=round(congestion, 3),
        vias=vias,
        width_mm=width,
        cost_mm=round(length * congestion + VIA_MM * vias, 3),
        path_mm=round(path, 3),
        r_mohm=round(r_mohm, 3),
        current_a=current,
    )
    if current:
        out["drop_mv"] = round(r_mohm * current, 3)
        out["ipc_width_mm"] = round(current_width(current, 1.0, TRACE_DT_C, external=True), 4)
    budget = _budget_mohm(rules, entry, net, current)
    if budget:
        out["budget_mohm"] = round(budget, 3)
    return out


def hard_reason(choice, estimates) -> Optional[str]:
    """Why ``choice`` is infeasible on hard constraints alone (a traced rail's current
    capacity or IR budget), else None."""
    for net in choice.trace:
        e = estimates[net]
        if e.get("ipc_width_mm") and e["ipc_width_mm"] > e["width_mm"] + 1e-9:
            return "%s traced: %.3g A needs %.2f mm, its class width is %.2f mm" % (
                net,
                e["current_a"],
                e["ipc_width_mm"],
                e["width_mm"],
            )
        if e.get("budget_mohm") and e["r_mohm"] > e["budget_mohm"]:
            return "%s traced: an estimated %.0f mOhm over its %.0f mOhm IR budget" % (
                net,
                e["r_mohm"],
                e["budget_mohm"],
            )
    return None


# -------------------------------------------------------------------- screen


def screen(choice, captured, estimates, prior) -> dict:
    """``choice`` scored on the probe's inputs (see the module doc): its partition at
    :data:`SCREEN_H_MM`, its quality penalty, its traced rails' estimated cost, its
    plane rails' drop vias and the prior. Lower is better."""
    from pnr.plane_partition import finish_quality, partition

    cap = captured[choice.layer]
    entry = dict(
        cap["entry"],
        nets=[n for n in cap["entry"]["nets"] if n in choice.plane],
        fill=choice.fill,
        h_mm=max(float(cap["entry"].get("h_mm", 0.1)), SCREEN_H_MM),
    )
    blocked = list(cap["blocked"])
    for n in choice.trace:
        for t in cap["terminals"].get(n, ()):
            blocked.append((t.at, t.radius + cap["gaps"][n]))
    part = partition(
        entry,
        width=cap["width"],
        height=cap["height"],
        terminals={n: cap["terminals"][n] for n in choice.plane},
        blocked=blocked,
        blocked_polygons=cap["blocked_polygons"],
        currents={n: c for n, c in cap["currents"].items() if n in choice.plane},
        budgets_mohm={n: b for n, b in cap["budgets_mohm"].items() if n in choice.plane},
        sources={n: s for n, s in cap["sources"].items() if n in choice.plane},
        copper_mm=cap["copper_mm"],
        edge_mm=cap["edge_mm"],
        via_drill_mm=cap["via_drill_mm"],
        fill_min_mm=cap["fill_min_mm"],
    )
    report = dict(quality=copy.deepcopy(part.report["quality"]))
    planes = cap["planes_elsewhere"]
    finish_quality(
        report,
        choice.layer,
        cap["via_blocked"],
        {choice.fill: planes.get(choice.fill) or []} if choice.fill else None,
    )
    quality = report["quality"]
    traced = {n: estimates[n]["cost_mm"] for n in choice.trace}
    drops = sum(1 for n in choice.plane for t in cap["terminals"].get(n, ()) if t.kind == "pad")
    against = sorted(
        n
        for n in choice.plane + choice.trace
        if prior.get(n) != ("plane" if n in choice.plane else "trace")
    )
    parts = dict(
        partition_mm=quality["penalty_mm"],
        trace_mm=round(sum(traced.values()), 3),
        drops_mm=round(VIA_MM * drops, 3),
        prior_mm=round(PRIOR_MM * len(against), 3),
    )
    return dict(
        choice=choice.key(),
        score_mm=round(sum(parts.values()), 3),
        parts=parts,
        penalty=quality["penalty"],
        traced=traced,
        against_prior=against,
        rails={
            n: {
                k: row[k]
                for k in ("area_mm2", "need_mm2", "area_ratio", "detour", "dead_mm2", "split_mm")
            }
            for n, row in quality["nets"].items()
        },
    )


# --------------------------------------------------------------------- route


def static_allocation(graph, rules, fixed_copper=None) -> dict:
    """The rule of thumb as the decision (``PNR_RAIL_ALLOC=static``; the A/B arm)."""
    from pnr.plane_partition import decide_rails

    return dict(decisions=decide_rails(rules, graph, fixed_copper), fills={}, mode="static")


def allocation_of(combo: Sequence[Choice], note="") -> dict:
    decisions = {}
    for c in combo:
        for n in c.plane:
            decisions[n] = ("plane", "allocation search: a territory on %s%s" % (c.layer, note))
        for n in c.trace:
            decisions[n] = ("trace", "allocation search: traced%s" % note)
    return dict(decisions=decisions, fills={c.layer: c.fill for c in combo}, mode="search")


def with_fills(rules, allocation):
    """``rules`` with each dedicated-plane entry's ``fill`` set to the allocation's
    leftover (a shallow copy; ``rules`` itself when nothing changes)."""
    fills = (allocation or {}).get("fills") or {}
    entries = (rules or {}).get("plane_partition") or ()
    if not fills or not any(
        e["layer"] in fills and e.get("fill") != fills[e["layer"]]
        for e in entries
        if not e.get("region")
    ):
        return rules
    out = dict(rules)
    out["plane_partition"] = [
        dict(e, fill=fills[e["layer"]]) if not e.get("region") and e["layer"] in fills else e
        for e in entries
    ]
    return out


def routed_rules(rules, traced):
    """``rules`` as the routed board's later stages read them (writeback, the planes
    stage, the checks): every ``plane_partition`` candidate the allocation traced
    leaves its ``plane_layer`` net class (an ordinary net with the class's other
    settings), and ``traced_rails`` names them. ``rules`` itself when none was."""
    traced = sorted(set(traced or ()))
    if not traced:
        return rules
    classes = []
    for nc in rules.get("net_classes") or ():
        moved = [n for n in nc.get("nets") or () if n in traced]
        if not nc.get("plane_layer") or not moved:
            classes.append(nc)
            continue
        keep = [n for n in nc["nets"] if n not in traced]
        plain = {k: v for k, v in nc.items() if k != "plane_layer"}
        if keep:
            classes.append(dict(nc, nets=keep))
            plain["name"] = "%s_traced" % nc.get("name", "rail")
        classes.append(dict(plain, nets=moved))
    out = dict(rules, net_classes=classes)
    out["traced_rails"] = traced
    return out


def _metrics(board, layers):
    from pnr.place.initial_pool import _route_metrics

    m = _route_metrics(board)
    penalty = 0.0
    quality = {}
    for rep in (getattr(board, "escape_diagnostics", None) or {}).get("plane_partition") or ():
        if rep.get("layer") in layers and rep.get("quality"):
            penalty += rep["quality"]["penalty_mm"]
            quality[rep["layer"]] = rep["quality"]
    total = m["copper_length_mm"] + VIA_MM * m["vias"] + penalty
    return (
        dict(
            missing_connections=m["missing_connections"],
            unresolved=len(m["unresolved_nets"]),
            length_unmatched=m.get("length_unmatched", 0),
            vias=m["vias"],
            copper_mm=round(m["copper_length_mm"], 3),
            partition_mm=round(penalty, 3),
            total_mm=round(total, 3),
        ),
        quality,
    )


def _rank(row):
    m = row["routed"]
    return (
        m["missing_connections"],
        m["unresolved"],
        m["length_unmatched"],
        m["total_mm"],
        row["choice"],
    )


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def route(graph, constraints, rules, kw, route_once):
    """``route_once(graph, constraints, rules, **kw, allocation=..., probe=...)`` under
    the allocation this module decides (see the module doc). Returns its BoardRoute,
    the decision's report in ``escape_diagnostics['rail_allocation']``."""
    entries = plane_entries(rules)
    if not entries:
        return route_once(graph, constraints, rules, **kw)
    from pnr.route.detail.router import layer_plan

    fixed_copper = kw.get("fixed_copper") or (rules or {}).get("fixed_copper")
    _layers, _planes, stack = layer_plan(graph, rules)
    per_layer = {e["layer"]: (e, candidates(e, stack)) for e in entries}
    options = {layer: choices(e, c) for layer, (e, c) in per_layer.items() if c}
    if mode() == "static":
        alloc = static_allocation(graph, rules, fixed_copper)
        board = route_once(graph, constraints, rules, **kw, allocation=alloc)
        _attach(board, dict(mode="static", decisions=_plain(alloc)))
        return board
    if not options or all(len(v) == 1 for v in options.values()):
        combo = [v[0] for v in options.values()]
        alloc = allocation_of(combo, ": the spec leaves no alternative")
        board = route_once(graph, constraints, rules, **kw, allocation=alloc)
        _attach(board, dict(mode="search", chosen=[c.key() for c in combo], alternatives=1))
        return board
    key = _digest(
        dict(
            entries=entries,
            candidates={k: v[1] for k, v in per_layer.items()},
            nets=sorted(n.name for n in graph.nets),
            finalists=finalists(),
        )
    )
    held = _MEMO.get(key)
    if held is not None:
        combo = held["combo"]
        alloc = allocation_of(combo, ": held from this design's first routed comparison")
        board = route_once(graph, constraints, rules, **kw, allocation=alloc)
        _attach(board, dict(held["report"], held=True))
        return board
    report = decide(graph, constraints, rules, kw, route_once, stack, per_layer, options)
    combo, board = report.pop("_combo"), report.pop("_board")
    _MEMO[key] = dict(combo=combo, report=report)
    _attach(board, report)
    return board


def _plain(alloc):
    return {n: dict(decision=d, reason=r) for n, (d, r) in sorted(alloc["decisions"].items())}


def _attach(board, report):
    if board is not None:
        board.escape_diagnostics["rail_allocation"] = report


def decide(graph, constraints, rules, kw, route_once, stack, per_layer, options):
    """Enumerate, prune, screen and route the finalists (the module doc's steps 1-4)."""
    from pnr.plane_partition import decide_rails

    fixed_copper = kw.get("fixed_copper") or (rules or {}).get("fixed_copper")
    fanout_refs = frozenset(
        f.get("ref") for f in (rules or {}).get("fanouts") or () if f.get("ref")
    )
    prior = {n: d for n, (d, _r) in decide_rails(rules, graph, fixed_copper).items()}
    estimates = {
        n: trace_estimate(graph, rules, e, n, fanout_refs)
        for e, cands in per_layer.values()
        for n in cands
    }
    probe_alloc = dict(
        decisions={n: ("plane", "probe") for _e, c in per_layer.values() for n in c},
        fills={layer: per_layer[layer][0].get("fill") for layer in options},
    )
    captured: dict = {}
    route_once(graph, constraints, rules, **kw, allocation=probe_alloc, probe=captured)
    screened = {}
    pruned = []
    for layer, alts in sorted(options.items()):
        rows = []
        for c in alts:
            why = hard_reason(c, estimates)
            if why:
                pruned.append(dict(choice=c.key(), reason=why))
                continue
            if layer not in captured:
                rows.append((0.0, c, dict(choice=c.key(), score_mm=0.0, note="no probe")))
                continue
            row = screen(c, captured, estimates, prior)
            rows.append((row["score_mm"], c, row))
        if not rows:
            raise ValueError(
                "plane_partition %s: every allocation breaks a hard constraint (%s)"
                % (layer, "; ".join(p["reason"] for p in pruned))
            )
        rows.sort(key=lambda r: (r[0], r[1].key()))
        screened[layer] = rows
    k = finalists()
    combos = []
    for picks in itertools.product(*(rows[:k] for _l, rows in sorted(screened.items()))):
        combos.append((sum(p[0] for p in picks), tuple(p[1] for p in picks)))
    combos.sort(key=lambda x: (x[0], tuple(c.key() for c in x[1])))
    finals = []
    tried = []
    best = None
    # The routed finalists: the best screened alternative of each distinct plane /
    # trace split first (a leftover variant of a split the router already tries
    # routes the same signals), then the next best.
    picked, splits = [], set()
    for score, combo in combos:
        split = tuple((c.layer, c.plane, c.trace) for c in combo)
        if split not in splits and len(picked) < k:
            splits.add(split)
            picked.append((score, combo))
    for item in combos:
        if len(picked) >= k:
            break
        if item not in picked:
            picked.append(item)
    picked.sort(key=lambda x: (x[0], tuple(c.key() for c in x[1])))
    for score, combo in picked:
        alloc = allocation_of(combo, " (routed finalist)")
        board = route_once(graph, constraints, rules, **kw, allocation=alloc)
        layers = {c.layer for c in combo}
        routed, quality = _metrics(board, layers)
        row = dict(
            choice=" || ".join(c.key() for c in combo),
            screen_mm=round(score, 3),
            routed=routed,
            warnings=[w["code"] + " " + w["net"] for q in quality.values() for w in q["warnings"]],
        )
        finals.append(row)
        tried.append((row, combo))
        if best is None or _rank(row) < _rank(best[0]):
            best = (row, combo, board)
    row, combo, board = best
    rails = {}
    for c in combo:
        for n in c.plane + c.trace:
            rails[n] = _why(n, c, tried, screened[c.layer], estimates)
    for rep in board.escape_diagnostics.get("plane_partition") or ():
        for n, cand in (rep.get("candidates") or {}).items():
            if n in rails:
                cand["reason"] = rails[n]["summary"]
    return dict(
        mode="search",
        chosen=[c.key() for c in combo],
        rails=rails,
        finalists=finals,
        screened=[r[2] for rows in screened.values() for r in rows],
        pruned=pruned,
        prior={n: prior.get(n) for n in sorted(estimates)},
        estimates=estimates,
        _combo=list(combo),
        _board=board,
    )


def _why(net, choice, tried, rows, estimates):
    """The numbers that decided ``net``: the best routed finalist with it a plane and
    with it traced (or, for a side no finalist tried, the best screened score)."""
    side = "plane" if net in choice.plane else "trace"
    best = {}
    for f, combo in tried:
        s = "plane" if any(net in c.plane for c in combo) else "trace"
        if s not in best or _rank(f) < _rank(best[s]):
            best[s] = f
    out = dict(decision=side, routed={}, screened={})
    for s, f in best.items():
        out["routed"][s] = dict(choice=f["choice"], **f["routed"])
    for score, c, _row in rows:
        s = "plane" if net in c.plane else "trace"
        if s not in out["screened"]:
            out["screened"][s] = dict(choice=c.key(), score_mm=score)
    other = "trace" if side == "plane" else "plane"
    if other in out["routed"]:
        a, b = out["routed"][side], out["routed"][other]
        text = "%s: routed %.1f mm-eq (copper %.1f, %d vias, partition %.1f) vs %s %.1f" % (
            side,
            a["total_mm"],
            a["copper_mm"],
            a["vias"],
            a["partition_mm"],
            other,
            b["total_mm"],
        )
        if (a["missing_connections"], a["unresolved"]) != (
            b["missing_connections"],
            b["unresolved"],
        ):
            text += " (missing %d vs %d)" % (a["missing_connections"], b["missing_connections"])
    elif other in out["screened"]:
        text = "%s: screened %.1f mm-eq vs %s %.1f (not routed)" % (
            side,
            out["screened"].get(side, {}).get("score_mm", float("nan")),
            other,
            out["screened"][other]["score_mm"],
        )
    else:
        text = "%s: the only feasible side" % side
    if side == "trace":
        e = estimates[net]
        text += "; trace est. %.1f mm, %d via(s)" % (e["length_mm"], e["vias"])
    out["summary"] = "allocation search, " + text
    return out
