"""Collapse blocks into rigid macro components for top-level placement, and expand back.

A macro is an ordinary :class:`Component` whose courtyard is the block outline
and whose pads are the member pads in the block frame, so the unmodified global
placer and legalizer can place it. Internal nets become intra-component and
exert no pull; external nets pull the macro toward its neighbours.
"""
from __future__ import annotations

import copy
import math
from typing import Dict, List, Tuple

from pnr.constraints import CompiledConstraints, Constraint, Enforcement
from pnr.graph import BoardGraph, Component, Net, Pad


def _rot(x, y, deg):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


def _rot_size(size, deg):
    return (size[1], size[0]) if int(round(deg / 90)) % 2 else size


class MacroPlan:
    """Mapping between the flat graph and its macro-collapsed counterpart."""

    def __init__(self):
        self.macros: Dict[str, dict] = {}      # macro ref -> {block, width, height, members:{ref:(x,y,rot,side)}}
        self.member_of: Dict[str, str] = {}

    def expand(self, placed: BoardGraph, flat: BoardGraph) -> BoardGraph:
        """Return ``flat`` with members posed from their placed macros."""
        out = BoardGraph.from_json(flat.to_json())
        by_ref = {c.ref: c for c in placed.components}
        for comp in out.components:
            if comp.ref in self.member_of:
                m = self.macros[self.member_of[comp.ref]]
                mc = by_ref[self.member_of[comp.ref]]
                x, y, rot, side = m['members'][comp.ref]
                dx, dy = _rot(x - m['width'] / 2, y - m['height'] / 2, mc.rot)
                comp.pos = (mc.pos[0] + dx, mc.pos[1] + dy)
                comp.rot = float((rot + mc.rot) % 360)
                comp.side = side
            else:
                src = by_ref[comp.ref]
                comp.pos, comp.rot, comp.side = src.pos, src.rot, src.side
                comp.pads = copy.deepcopy(src.pads)  # keep any side mirroring the placer applied
        out.outline = copy.deepcopy(placed.outline)
        return out


def collapse(flat: BoardGraph, constraints: CompiledConstraints, rules: dict,
             layouts: List[Tuple[object, BoardGraph, float, float]]):
    """Build the macro graph.

    ``layouts`` holds (block, placed_sub_graph, width, height) for every block to
    collapse; the sub-graph poses are in the block frame (origin bottom-left).
    Returns (macro_graph, macro_constraints, placement_rules, plan).

    Raises ``ValueError`` when a relation cannot be expressed on the rigid macro:
    a HARD group, a multi-ref orientation or a ref-relative keepout that mixes
    members of one macro with parts outside it (straddling), or members of one
    macro whose orientation locks imply different macro rotations.
    """
    plan = MacroPlan()
    by_ref = {c.ref: c for c in flat.components}
    macro_graph = BoardGraph(name=flat.name + ":macro", outline=copy.deepcopy(flat.outline))
    pad_alias: Dict[Tuple[str, str], Tuple[str, str]] = {}
    # Orientation is a per-ref absolute axis: every ref of a lock counts, not just refs[0].
    orient_locks = {r: c for c in constraints.constraints if c.kind == 'orientation' for r in c.refs}
    macro_orientation = {}
    # Routed block copper may reach the block rectangle (sub-board apron equals the
    # edge clearance), so the macro reserves that clearance on every side: it then
    # also holds from the board edge, not only from neighbouring courtyards.
    margin = float(rules.get('fab', {}).get('edge_clearance_mm', 0.2))
    for index, (block, sub, w, h) in enumerate(layouts):
        mref = 'MB%02d' % index
        members = {}
        pads: List[Pad] = []
        for c in sub.components:
            members[c.ref] = (c.pos[0], c.pos[1], c.rot, c.side)
            plan.member_of[c.ref] = mref
            for p in c.pads:
                ox, oy = _rot(p.offset[0], p.offset[1], c.rot)
                q = copy.deepcopy(p)
                q.name = f'{c.ref}.{p.name}'
                q.offset = (c.pos[0] - w / 2 + ox, c.pos[1] - h / 2 + oy)
                q.size = _rot_size(p.size, c.rot)
                q.drill_size = _rot_size(p.drill_size, c.rot)
                pads.append(q)
                pad_alias[(c.ref, p.name)] = (mref, q.name)
            if c.ref in orient_locks:
                # The member's absolute axis is authored; the macro may not rotate it.
                lock = orient_locks[c.ref]
                rot = (float(lock.params.get('rot') or 0) - c.rot) % 360
                macro_orientation.setdefault(mref, []).append((rot, lock.enforcement, c.ref))
        plan.macros[mref] = dict(block=block.name, width=w, height=h, members=members)
        macro_graph.components.append(Component(
            ref=mref, footprint='block:' + block.name, pos=(0.0, 0.0), rot=0.0, side='top',
            courtyard=(w + 2 * margin, h + 2 * margin), bbox=(w + 2 * margin, h + 2 * margin),
            pads=pads, address='block:' + block.name))
    for c in flat.components:
        if c.ref not in plan.member_of:
            macro_graph.components.append(copy.deepcopy(c))
    for n in flat.nets:
        pins = [pad_alias.get(tuple(p), tuple(p)) for p in n.pins]
        macro_graph.nets.append(Net(name=n.name, code=n.code, pins=pins))

    def straddled(refs):
        """First macro whose members share ``refs`` with parts outside it, else None."""
        for m in dict.fromkeys(plan.member_of[r] for r in refs if r in plan.member_of):
            if any(plan.member_of.get(r) != m for r in refs):
                return m
        return None

    con = copy.deepcopy(constraints)
    kept = []
    for c in con.constraints:
        # Relations the rigid macro cannot carry: a hard radius is measured from the
        # member, not the macro centre; orientation of outside refs must not move
        # with (or be dropped along with) a member; a keepout is relative to the
        # member's own courtyard edge. Refuse instead of emitting a wrong constraint.
        checked = (list(c.refs) + [c.params.get('anchor')] if c.kind == 'group' and c.enforcement == Enforcement.HARD
                   else list(c.refs) if c.kind == 'orientation' and len(c.refs) > 1
                   else list(c.refs) if c.kind == 'keepout' and c.params.get('extent') and not c.params.get('polygon')
                   else [])
        m = straddled([r for r in checked if r])
        if m:
            inside = sorted({r for r in checked if r and plan.member_of.get(r) == m})
            outside = sorted({r for r in checked if r and plan.member_of.get(r) != m})
            label = ('hard group' if c.kind == 'group' else c.kind) + (' %r' % c.name if c.name else '')
            raise ValueError(f'{label} constraint straddles macro {m} (block {plan.macros[m]["block"]}): '
                             f'members {inside} vs outside {outside}; the block extraction must keep '
                             f'these parts together or leave the block flat')
        refs = tuple(dict.fromkeys(plan.member_of.get(r, r) for r in c.refs))
        if c.kind == 'orientation' and c.refs[0] in plan.member_of:
            continue
        if c.kind == 'side' and all(r in plan.member_of for r in c.refs):
            continue
        params = dict(c.params)
        anchor = params.get('anchor')
        if anchor:
            params['anchor'] = plan.member_of.get(anchor, anchor)
        if c.kind == 'group':
            others = [r for r in refs if r != params.get('anchor')]
            if not others:
                continue
        kept.append(Constraint(c.kind, c.enforcement, refs, params, c.weight, c.name))
    for mref, locks in macro_orientation.items():
        rot, enforcement, _ = locks[0]
        if any(abs((rot - other + 180) % 360 - 180) > 1e-6 for other, _, _ in locks):
            raise ValueError(f'macro {mref} ({plan.macros[mref]["block"]}): orientation locks of its members '
                             f'need macro rotations {sorted((ref, r) for r, _, ref in locks)}; the layout '
                             f'does not honour the authored axes')
        kept.append(Constraint('orientation', enforcement, (mref,), {'rot': rot, 'reason': 'member axis convention'}))
    con.constraints = kept
    con.copper_keepouts = [k for k in con.copper_keepouts if k.get('ref') not in plan.member_of]

    place_rules = copy.deepcopy(rules)
    place_rules['plane_access_intents'] = [i for i in rules.get('plane_access_intents', [])
                                           if i.get('ref') not in plan.member_of]
    return macro_graph, con, place_rules, plan
