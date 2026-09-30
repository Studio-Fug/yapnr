"""Diff-pair via landing reserves for placement (PNR_PAIR_LANDING_RESERVE=1).

Owner decision 2026-09-29: parts on the side opposite a differential-pair
terminal part must keep that part's pair via landings clear (a bottom test-point
array legalized over the MCU's USB pins left no room for the B.Cu via pair the
pair router needs). Parts stay free to move; only legality changes.

Everything is derived from rules.json: each diff_pairs terminal_chain node and
each auxiliary_pairs source/target names the (p, n) pads of one part. For each
such pad pair, in the part's own frame:

* axis = n pad -> p pad, tangent perpendicular to it (pad row direction);
* via diameter/drill from the fab rules the router will use (as
  pnr.native_electrical.pair_via_geometry on the profile-applied rules: the
  placement rules.json predates the fab profile, e.g. 0.6 mm vias where the
  jlc-pofv router uses 0.45 mm; :func:`router_rules`), via pair spacing
  max(diameter+clearance, drill+hole-to-hole) (+2 um, as the router's
  pair_bridge_ports), also the pad pitch when that is wider;
* run_mm: the minimum legal run along the tangent (each direction separately)
  at which both vias clear every pad of the part by max(pair net clearance,
  via_to_smd_pad) - the geometric counterpart of the router's exact minimum run;
* runs are searched up to max_uncoupled_mm - 0.15 (the router's bound on a
  surface fanout, pair_bridge_ports); a direction without a legal run there has
  no landing and gets no reserve;
* the landing zone per direction spans the tangent from the pad-pair midline to
  the far clearance edge of the via pair (run + via radius + clearance) and, across
  the axis, both vias (widest spacing) plus clearance. Together the two zones
  are one corridor under the pad pair joining the inward and the outward via
  pair, so a B.Cu lead can leave either landing in both tangent directions (the
  router's directional pair search tries both headings). The B.Cu lead beyond
  the far via edge is not reserved: the routed controls (e.g. h4 p007) land
  there with an opposite-side courtyard 0.6 mm past the via clearance.

Both directions (inward and outward) are reserved, so at least one inward and
one outward landing pair survive. (Evidence, src13 review: routed USB routes
land their U6 via pair on the inward side on h4 p007, h3 p010 and h3 p035 (TP1
moved) and on the outward side on h5 g1f0 (TP1 moved); D2 via pairs have used
both sides; so no single fixed direction serves every layout.) A block macro
counts as mounted on the sides its members are mounted on (recorded by
pnr.hier.macro.collapse, see :func:`macro_mount`), like the parts it contains;
routed copper (a macro's own routing, early vias) is not a body and is not
kept out by this reservation.

The reserves live in Component.reserves as
recipes (pad names + distances), so rotation, side mirroring (pad offsets are
mirrored by set_component_side), block-macro collapse (pads renamed
'<ref>.<pad>') and the JSON round-trip carry them without a second geometry.
:func:`pnr.place.geometry.placement_rects` turns them into ReserveRects on the
side opposite the owner; see ReserveRect for what they exclude. Stdlib only
(plus pnr.fab_profile, itself stdlib-only).
"""

from __future__ import annotations

import math
import os

KIND = "pair_landing"


def enabled():
    """PNR_PAIR_LANDING_RESERVE=1 turns the reservation on (placement + checks)."""
    return os.environ.get("PNR_PAIR_LANDING_RESERVE") == "1"


def router_rules(rules):
    """``rules`` with the fab block the pair router will see.

    Placement reads the design's rules.json before the fab profile is applied
    (native_loop's prepare worker applies it for routing); rules that already
    carry a profile marker are used as they are.
    """
    from pnr import fab_profile

    if rules.get(fab_profile.MARKER) is not None or fab_profile.is_legacy():
        return rules
    out = dict(rules)
    out["fab"] = fab_profile.apply_fab(rules.get("fab"))
    return out


def via_geometry(rules):
    """(diameter, drill) exactly as pnr.native_electrical.pair_via_geometry."""
    fab = rules.get("fab", {})
    electrical = rules.get("electrical_fab", {})
    diameter = float(fab.get("via_diameter_mm", electrical.get("via_diameter_mm", 0.6)))
    drill = float(fab.get("via_drill_mm", electrical.get("via_drill_mm", 0.3)))
    if not 0 < drill < diameter:
        raise ValueError("invalid pair via dimensions")
    return diameter, drill


def terminal_groups(rules):
    """{ref: [(pair name, p pad, n pad)]} for every pair terminal of one part."""
    out = {}
    for pair in rules.get("diff_pairs", []):
        nodes = list(pair.get("terminal_chain", []))
        for group in pair.get("auxiliary_pairs", []):
            nodes += [group[side] for side in ("source", "target") if side in group]
        for node in nodes:
            try:
                pref, ppad = node["p"].rsplit(".", 1)
                nref, npad = node["n"].rsplit(".", 1)
            except (KeyError, ValueError):
                continue
            if pref != nref:
                continue
            entry = (pair.get("name", ""), ppad, npad)
            if entry not in out.setdefault(pref, []):
                out[pref].append(entry)
    return out


def _rect_distance(point, center, size):
    dx = max(0.0, abs(point[0] - center[0]) - size[0] / 2)
    dy = max(0.0, abs(point[1] - center[1]) - size[1] / 2)
    return math.hypot(dx, dy)


def recipe(comp, pair, ppad, npad, rules):
    """Landing recipe for one pad pair of ``comp`` (None if the pads are missing)."""
    from pnr.electrical import net_policy

    rules = router_rules(rules)
    pads = {p.name: p for p in comp.pads}
    if ppad not in pads or npad not in pads:
        return None
    P, N = pads[ppad].offset, pads[npad].offset
    distance = math.dist(P, N)
    if distance < 1e-9:
        return None
    diameter, drill = via_geometry(rules)
    fab = rules.get("fab", {})
    nets = [pads[ppad].net, pads[npad].net]
    net_clearance = max(
        [net_policy(net, rules)["clearance_mm"] for net in nets if net]
        or [fab.get("clearance_mm", 0.15)]
    )
    pad_clearance = max(net_clearance, float(fab.get("via_to_smd_pad_mm", 0) or 0))
    hole_gap = fab.get("hole_to_hole_mm", fab.get("hole_clearance_mm", 0.2))
    spacings = sorted(
        {
            max(diameter + net_clearance + 0.002, drill + hole_gap + 0.002),
            max(diameter + net_clearance + 0.002, drill + hole_gap + 0.002, distance),
        }
    )
    cap = float(pair.get("max_uncoupled_mm", 2))
    envelope = 2 * float(pair.get("width_mm", 0.2)) + float(pair.get("gap_mm", 0.15))
    axis = ((P[0] - N[0]) / distance, (P[1] - N[1]) / distance)
    tangent = (-axis[1], axis[0])
    mid = ((P[0] + N[0]) / 2, (P[1] + N[1]) / 2)
    radius = diameter / 2 + pad_clearance
    others = [(p.offset, p.size) for p in comp.pads]

    def legal(run, sign, spacing):
        for side in (1, -1):
            v = tuple(
                mid[i] + sign * tangent[i] * run + side * axis[i] * spacing / 2 for i in (0, 1)
            )
            if any(_rect_distance(v, c, s) < radius - 1e-9 for c, s in others):
                return False
        return True

    runs = {}
    limit = cap - 0.15
    for sign in (-1, 1):
        best = None
        for spacing in spacings:
            steps = int(math.ceil(limit / 0.01))
            first = next((i for i in range(steps + 1) if legal(i * 0.01, sign, spacing)), None)
            if first is None:
                continue
            lo, hi = max(0.0, (first - 1) * 0.01), first * 0.01
            if first:
                while hi - lo > 1e-5:
                    middle = (lo + hi) / 2
                    lo, hi = (lo, middle) if legal(middle, sign, spacing) else (middle, hi)
            best = hi if best is None else min(best, hi)
        if best is not None:
            runs[sign] = best
    if not runs:
        return None
    half_width = max(max(spacings) / 2 + radius, envelope / 2 + pad_clearance)
    return dict(
        kind=KIND,
        pair=pair.get("name", ""),
        pads=[ppad, npad],
        side="opposite",
        runs_mm={str(sign): round(run, 6) for sign, run in sorted(runs.items())},
        via_radius_mm=round(radius, 6),
        half_width_mm=round(half_width, 6),
    )


def attach(graph, rules):
    """Set Component.reserves (pair_landing recipes) from rules; idempotent.

    Components without pair terminals keep their reserves untouched; existing
    pair_landing recipes of a terminal part are replaced. Returns {ref: count}.
    """
    groups = terminal_groups(rules)
    pairs = {p.get("name", ""): p for p in rules.get("diff_pairs", [])}
    counts = {}
    for comp in graph.components:
        if comp.ref not in groups:
            continue
        kept = [r for r in comp.reserves if r.get("kind") != KIND]
        for name, ppad, npad in groups[comp.ref]:
            made = recipe(comp, pairs[name], ppad, npad, rules)
            if made is not None:
                kept.append(made)
        comp.reserves = kept
        counts[comp.ref] = sum(r.get("kind") == KIND for r in kept)
    return counts


def relax_violated(graph, violations):
    """Drop only the landing recipes that an existing placement already violates.

    For a board placed without the reserve (legacy layouts, e.g. a bottom test
    pad array over the MCU's USB pins) whose incremental moves need a legal
    baseline: while ``violations()`` (hard_violations of ``graph``) reports
    overlaps, the pair_landing recipes of the parts in those overlaps are
    removed; every other part keeps its reserve. Returns the refs relaxed, in
    order; the caller rechecks legality (a violation not caused by a reserve
    remains and is the caller's to report).
    """
    relaxed = []
    by_ref = {c.ref: c for c in graph.components}
    while True:
        bad = violations()
        refs = sorted(
            {
                ref
                for pair in bad.get("overlaps", [])
                for ref in pair
                if ref not in relaxed and any(r.get("kind") == KIND for r in by_ref[ref].reserves)
            }
        )
        if not refs:
            return relaxed
        for ref in refs:
            by_ref[ref].reserves = [r for r in by_ref[ref].reserves if r.get("kind") != KIND]
            relaxed.append(ref)


MOUNT = "macro_mount"


def macro_mount_record(members):
    """Reserve-list record of the sides a block macro's members are mounted on."""
    return dict(kind=MOUNT, sides=sorted({m.side for m in members}))


def macro_mount(comp):
    """'top', 'bottom' or 'both': where a block macro counts as mounted for the
    landing reservation (from :func:`macro_mount_record`; 'both' when unknown)."""
    for r in comp.reserves:
        if r.get("kind") == MOUNT:
            sides = set(r.get("sides") or ())
            return sides.pop() if len(sides) == 1 else "both"
    return "both"


def macro_reserves(member, macro_side="top"):
    """A block member's recipes re-expressed on its macro (pads '<ref>.<pad>')."""
    out = []
    for r in member.reserves:
        absolute = (
            member.side
            if r.get("side") == "same"
            else ("bottom" if member.side == "top" else "top")
        )
        q = dict(
            r,
            pads=[f"{member.ref}.{name}" for name in r["pads"]],
            side="same" if absolute == macro_side else "opposite",
            member=member.ref,
        )
        out.append(q)
    return out


def reserve_rects(comp):
    """[(side, ReserveRect)] of the component's recipes at its current pose."""
    from .geometry import ReserveRect

    if not comp.reserves:
        return []
    pads = {p.name: p.offset for p in comp.pads}
    th = math.radians(comp.rot)
    ct, st = math.cos(th), math.sin(th)
    out = []
    for r in comp.reserves:
        if r.get("kind") != KIND or r["pads"][0] not in pads or r["pads"][1] not in pads:
            continue
        P, N = pads[r["pads"][0]], pads[r["pads"][1]]
        distance = math.dist(P, N)
        if distance < 1e-9:
            continue
        axis = ((P[0] - N[0]) / distance, (P[1] - N[1]) / distance)
        tangent = (-axis[1], axis[0])
        mid = ((P[0] + N[0]) / 2, (P[1] + N[1]) / 2)
        side = comp.side if r.get("side") == "same" else ("bottom" if comp.side == "top" else "top")
        for key, run in sorted(r["runs_mm"].items()):
            sign = int(key)
            corners = []
            for t in (0.0, run + r["via_radius_mm"]):
                for a in (-r["half_width_mm"], r["half_width_mm"]):
                    x = mid[0] + sign * tangent[0] * t + axis[0] * a
                    y = mid[1] + sign * tangent[1] * t + axis[1] * a
                    corners.append((comp.pos[0] + x * ct - y * st, comp.pos[1] + x * st + y * ct))
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            out.append(
                (
                    side,
                    ReserveRect(
                        (min(xs) + max(xs)) / 2,
                        (min(ys) + max(ys)) / 2,
                        max(xs) - min(xs),
                        max(ys) - min(ys),
                        side=side,
                        owner=comp.ref,
                        label=f"{r.get('pair', '')}:{'/'.join(r['pads'])}:{'+' if sign > 0 else '-'}",
                    ),
                )
            )
    return out
