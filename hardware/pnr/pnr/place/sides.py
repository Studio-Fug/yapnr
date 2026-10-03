"""Which side each part may take, and what a side choice costs (``board.sides``).

The board's side policy is an input (``board.sides`` in the constraints):

``single`` (the default)
    every part stays on the side it arrives on, except a part a ``side_pref`` names:
    the designer has said either side is acceptable, so the preference is a bias the
    placer weighs, not a lock;
``double``
    placement chooses the side of every part that nothing else holds.

A part is *held* on one side, whatever the policy, by a hard ``side`` rule or a
``fixed`` pose (its side, or its source side), a source lock, membership of a line
group or a row, being a block or line macro, a drilled pad (a through-hole body
occupies both sides and is soldered from the far one), a keep-out or copper keep-out
tied to its reference, a plane-access intent or a diff-pair landing reserve. Those
are geometric commitments made in the source side's frame; the placer does not
re-derive them for a mirrored footprint.

The *side cost* is the placer's estimate of what a side assignment costs, in
millimetres of wirelength:

* a layer change for every non-plane net whose surface pins sit on both sides and
  which has no drilled pin (a drilled pin already joins both outer layers), at
  :data:`VIA_MM` per net, the via cost of the relocation surrogate
  (:func:`pnr.place.relocate.distance_field`);
* :data:`SIDE_PREF_MM` times the ``side_pref`` weight for a part off its preferred
  side;
* :data:`FLIP_MM` for every part off its source side, so a part changes side only
  when that saves more than a token amount (and a mirror image of the whole board is
  not an equal alternative).

While a plan frees any part, two parts that *fan out* (:func:`fans_out`: surface parts
with at least :data:`STACK_PADS` connected pads, an IC rather than a two-terminal
passive) may not overlap on opposite sides either (:func:`stack_refs`; the
``stack`` plane of :func:`pnr.place.legalize.legalize` and of the placement
checkers). A through via inside such an overlap would land on the far part's pads, so
every pin under the stack could leave only on its own layer; on the two-layer chaser
rungs the finalists with two ICs back to back were the ones left with unrouted
connections. A two-terminal part (a decoupling capacitor) may still sit under an IC.
Without free parts nothing changes, so a single-sided board is checked as before.

Every function here is pure and stdlib-only; :func:`plan` reads the board graph and
the compiled constraints only.
"""

from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from pnr.graph import SIDE_BOTTOM, SIDE_TOP

from .geometry import resolve_hard_sides, set_component_side

VIA_MM = 3.0
SIDE_PREF_MM = 5.0
FLIP_MM = 0.5


def opposite(side: str) -> str:
    return SIDE_BOTTOM if side == SIDE_TOP else SIDE_TOP


def drilled(comp) -> bool:
    return any(p.through_hole for p in comp.pads)


def macro(comp) -> bool:
    footprint = comp.footprint or ""
    return footprint.startswith("block:") or footprint.startswith("line:") or bool(comp.hull)


# Parts with at least this many connected pads fan out through vias near their body
# and may not be stacked back to back (see the module docstring).
STACK_PADS = 3
# The occupancy plane / region tag of that rule.
STACK_PLANE = "stack"


def fans_out(comp) -> bool:
    """A surface part (no drilled pad, not a macro) with at least :data:`STACK_PADS`
    connected pads."""
    return (
        not drilled(comp) and not macro(comp) and sum(1 for p in comp.pads if p.net) >= STACK_PADS
    )


@dataclass(frozen=True)
class SidePlan:
    """The side policy resolved against one board.

    ``options`` maps every part to the sides it may take, its source side first;
    ``held`` names why a part has one option (only for parts the policy would
    otherwise free, or that ``side_pref`` names); ``preferred`` is the ``side_pref``
    bias ``{ref: (side, weight)}``; ``plane_nets`` the nets poured as planes, which
    cost no layer change; ``drilled_nets`` the nets with a drilled pin."""

    policy: str
    source: Dict[str, str]
    options: Dict[str, Tuple[str, ...]]
    held: Dict[str, str] = field(default_factory=dict)
    preferred: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    plane_nets: frozenset = frozenset()
    drilled_nets: frozenset = frozenset()

    @property
    def free(self) -> Tuple[str, ...]:
        """Refs that may take either side, sorted."""
        return tuple(sorted(r for r, o in self.options.items() if len(o) > 1))

    @property
    def active(self) -> bool:
        return any(len(o) > 1 for o in self.options.values())

    def allows(self, ref: str, side: str) -> bool:
        return side in self.options.get(ref, ())

    def nets(self, graph) -> List[Tuple[str, Tuple[str, ...]]]:
        """``[(net, refs)]`` of the nets a side choice can split: not a plane, no
        drilled pin, pins on at least two parts, at least one of them free."""
        free = set(self.free)
        out = []
        for net in graph.nets:
            if not net.name or net.name in self.plane_nets or net.name in self.drilled_nets:
                continue
            refs = tuple(sorted({ref for ref, _ in net.pins}))
            if len(refs) >= 2 and free.intersection(refs):
                out.append((net.name, refs))
        return out


def _plane_nets(graph, constraints) -> frozenset:
    patterns = [p for nc in constraints.net_classes if nc.plane_layer for p in nc.nets]
    return frozenset(
        n.name for n in graph.nets if any(fnmatch.fnmatchcase(n.name, p) for p in patterns)
    )


def plan(graph, constraints, rules: Optional[dict] = None) -> SidePlan:
    """Resolve the board's side policy (``constraints.board.sides``) against ``graph``.

    ``rules`` (the routing rules, optional) adds the plane-access intent holds."""
    policy = getattr(constraints.board, "sides", "single") or "single"
    hard = resolve_hard_sides(constraints)
    fixed = set(constraints.locked_refs)
    lined = {
        r for con in constraints.constraints if con.kind in ("line_group", "row") for r in con.refs
    }
    tied = {r for con in constraints.constraints if con.kind == "keepout" for r in con.refs}
    tied |= {k["ref"] for k in constraints.copper_keepouts}
    intents = {v.get("ref") for v in (rules or {}).get("plane_access_intents", []) or []}
    edge_side = {
        ref: con.params["side"]
        for con in constraints.constraints
        if con.kind == "edge_align" and con.params.get("side")
        for ref in con.refs
    }
    preferred = {}
    for con in constraints.constraints:
        if con.kind == "side_pref":
            for ref in con.refs:
                preferred[ref] = (con.params["side"], float(con.weight or 1.0))
    source, options, held = {}, {}, {}
    for comp in graph.components:
        ref = comp.ref
        source[ref] = comp.side
        reason = None
        if ref in hard:
            reason = "hard_side"
        elif ref in fixed:
            reason = "fixed"
        elif comp.locked:
            reason = "locked"
        elif macro(comp):
            reason = "macro"
        elif ref in lined:
            reason = "line_or_row"
        elif drilled(comp):
            reason = "drilled"
        elif ref in tied:
            reason = "keepout"
        elif ref in intents:
            reason = "plane_access"
        elif comp.reserves:
            reason = "landing_reserve"
        elif ref in edge_side:
            reason = "edge_side"
        wanted = policy == "double" or ref in preferred
        if reason is None and wanted:
            options[ref] = (comp.side, opposite(comp.side))
            continue
        side = hard.get(ref) or edge_side.get(ref) if reason in ("hard_side", "edge_side") else None
        options[ref] = (side or comp.side,)
        if wanted and reason is not None:
            held[ref] = reason
    drilled_nets = frozenset(
        p.net for c in graph.components if drilled(c) for p in c.pads if p.through_hole and p.net
    )
    return SidePlan(
        policy=policy,
        source=source,
        options=options,
        held=held,
        preferred={r: v for r, v in preferred.items() if r in options},
        plane_nets=_plane_nets(graph, constraints),
        drilled_nets=drilled_nets,
    )


def stack_refs(graph, constraints) -> frozenset:
    """The parts the no-stacking rule covers on this board: every part that
    :func:`fans_out` when the side plan frees any part, else none (without a
    ``double`` policy or a ``side_pref`` nothing is free, and the plan is not built)."""
    if getattr(getattr(constraints, "board", None), "sides", "single") != "double" and not any(
        con.kind == "side_pref" for con in getattr(constraints, "constraints", ())
    ):
        return frozenset()
    if not plan(graph, constraints).active:
        return frozenset()
    return frozenset(c.ref for c in graph.components if fans_out(c))


def with_policy(doc: Optional[dict], sides: Optional[str]) -> dict:
    """A constraints document with a tool-neutral side policy as ``board.sides``.

    ``double`` (either side, the tool decides) sets ``board.sides: double``; any other
    value (``single``; ``assigned``, whose side locks are already hard ``side`` rules
    in the document) returns the document unchanged. Never mutates ``doc``."""
    doc = dict(doc or {})
    if sides == "double":
        doc["board"] = dict(doc.get("board") or {}, sides="double")
    return doc


def sides_of(graph) -> Dict[str, str]:
    return {c.ref: c.side for c in graph.components}


def split_nets(graph, side_plan: SidePlan, sides: Optional[Dict[str, str]] = None) -> List[str]:
    """Nets that need a layer change under ``sides`` (default: the graph's own)."""
    sides = sides or sides_of(graph)
    return [
        name
        for name, refs in side_plan.nets(graph)
        if len({sides[r] for r in refs if r in sides}) > 1
    ]


def preference_cost(side_plan: SidePlan, sides: Dict[str, str]) -> float:
    return sum(
        SIDE_PREF_MM * weight
        for ref, (side, weight) in side_plan.preferred.items()
        if sides.get(ref, side) != side
    )


def flip_cost(side_plan: SidePlan, sides: Dict[str, str]) -> float:
    return FLIP_MM * sum(
        sides.get(ref, side) != side
        for ref, side in side_plan.source.items()
        if len(side_plan.options.get(ref, ())) > 1
    )


def side_cost(graph, side_plan: SidePlan, sides: Optional[Dict[str, str]] = None) -> float:
    """The side cost (mm) of ``sides`` (default: the graph's own)."""
    sides = sides or sides_of(graph)
    return (
        VIA_MM * len(split_nets(graph, side_plan, sides))
        + preference_cost(side_plan, sides)
        + flip_cost(side_plan, sides)
    )


def assign(graph, sides: Dict[str, str], side_plan: Optional[SidePlan] = None) -> List[str]:
    """Put each part of ``sides`` on its side (mirroring pads, KiCad Flip semantics);
    returns the refs that changed. Refuses a side ``side_plan`` does not allow."""
    changed = []
    for ref, side in sorted(sides.items()):
        comp = graph.component(ref)
        if comp.side == side:
            continue
        if side_plan is not None and not side_plan.allows(ref, side):
            raise ValueError("%s may not be placed on the %s side" % (ref, side))
        set_component_side(comp, side)
        changed.append(ref)
    return changed


def check_held(graph, side_plan: SidePlan) -> None:
    """Raise when a part ends on a side its plan does not allow (a held part flipped)."""
    bad = sorted(c.ref for c in graph.components if not side_plan.allows(c.ref, c.side))
    if bad:
        raise ValueError("held parts changed side: " + ", ".join(bad))


def same_footprint(comp, original) -> bool:
    """True when ``comp`` is ``original`` placed on either side: identical pads, with
    the pad offsets mirrored (y) when the sides differ."""
    pads = [asdict(p) for p in comp.pads]
    if comp.side != original.side:
        for pad in pads:
            pad["offset"] = (pad["offset"][0], -pad["offset"][1])
    return pads == [asdict(p) for p in original.pads] and comp.footprint == original.footprint


def report(graph, side_plan: SidePlan) -> dict:
    """The ``sides`` block of a placement report."""
    sides = sides_of(graph)
    split = split_nets(graph, side_plan, sides)
    reasons: Dict[str, int] = {}
    for reason in side_plan.held.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    return dict(
        policy=side_plan.policy,
        free=len(side_plan.free),
        held=dict(sorted(reasons.items())),
        bottom=sorted(r for r, s in sides.items() if s == SIDE_BOTTOM),
        flipped=sorted(r for r, s in sides.items() if s != side_plan.source.get(r, s)),
        split_nets=split,
        side_cost_mm=round(side_cost(graph, side_plan, sides), 3),
    )


def under_body_sides(graph, side_plan: SidePlan, rng=None) -> Dict[str, Tuple[str, str]]:
    """``{ref: (side, host)}``: every free two-pin part under the largest part (by pad
    count, at least six pads) it shares a net with, on the side opposite that host's
    source side. A start proposal, never a rule."""
    by_net: Dict[str, List[str]] = {}
    for comp in graph.components:
        for pad in comp.pads:
            if pad.net:
                by_net.setdefault(pad.net, []).append(comp.ref)
    size = {c.ref: len(c.pads) for c in graph.components}
    out = {}
    for ref in side_plan.free:
        comp = graph.component(ref)
        if len(comp.pads) != 2 or not all(p.net for p in comp.pads):
            continue
        hosts = {
            other
            for pad in comp.pads
            for other in by_net.get(pad.net, ())
            if other != ref and size[other] >= 6
        }
        if not hosts:
            continue
        host = min(hosts, key=lambda h: (-size[h], h))
        side = opposite(side_plan.source[host])
        if side_plan.allows(ref, side):
            out[ref] = (side, host)
    return out
