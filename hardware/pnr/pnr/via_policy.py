"""Via kinds a board may use, the copper span each one occupies, and the build.

A KiCad 10 via has a type (through, blind, buried or micro) and a layer pair; its
copper, hole and plane clearances exist only on the layers from one to the other
(the zone filler cuts antipads only there, and DRC checks clearance only there).

``rules["via_policy"]`` (:func:`resolve`, :func:`board_policy`) says which kinds a
route may use on this board::

    {"allowed": ["through", "blind", "buried", "micro"],
     "layers": ["F.Cu", "In1.Cu", ..., "B.Cu"],       # copper, outer to outer
     "gaps_mm": [0.09, 0.55, ...],                     # dielectric between neighbours
     "bonds": ["prepreg", "core", ...],                # what each dielectric is
     "sizes": {"blind": [0.6, 0.3], "buried": [0.6, 0.3], "micro": [0.4, 0.1]},
     "build": {"spans": [["F.Cu", "In1.Cu", "micro", "laser"], ...], ...},
     "return_tie": {"max_mm": 7.07, ...},
     "warnings": [...]}

It is absent when only through vias are allowed, or when no other span earns its
drill pair (:func:`select_build`): the board then routes exactly as before. The
allowed kinds are the ones the design declares (through only when it declares none)
minus every kind the board's custom rules (``.kicad_dru``) disallow; a disallow rule
limited by a layer or a condition still counts as a ban everywhere, which is the safe
reading. ``via`` bans every kind but through (through vias stay the engine's
fallback, the judge reports them), and ``buried_via`` bans blind vias too (KiCad up
to 9 named both kinds that way).

Buildable spans (stack indices ``t < b``, outer to outer), from the stackup rows:

``through``   F to B.
``micro``     (laser) two adjacent layers, one of them outer, whose dielectric is
              no deeper than the microvia drill (aspect ratio 1:1, IPC-2226); KiCad
              itself accepts any pair, so the engine keeps to this.
``blind``     (controlled depth) from an outer layer, no deeper than the blind
              via's drill (1:1), drilled into the finished board.
``blind``/``buried`` (laminated) the through hole of a sub-laminate: each end
              faces a prepreg bond line (or the board's face), so the span never
              splits a core, whose two copper faces are one laminate.

A board's vias come from one :func:`select_build` family: the laminated spans of a
family nest or are disjoint (a sequential-lamination tree), controlled-depth and
laser spans are drilled from the outside, and every non-through span is one more
drill pair (one more drilling and plating step, a laminated span one more
lamination cycle), priced :data:`DRILL_PAIR_COST` through-via prices: a pair enters
the build only where the vias the board needs save more than that.

A span's cost multiplier (the router's via price) is 1.0 for a through via, so a
through-only board prices exactly as before, and ``0.5 + 0.5 * depth / thickness``
for any other span: a shorter span drills less and blocks fewer layers.

``return_tie`` (:func:`return_tie_rule`): a signal via that moves a signal between
layers referenced to two plane layers of one net (two ground planes) needs a via of
that net joining both planes within ``max_mm`` of it, the distance whose return
detour (out and back) delays no more than ``k * t_rise`` (the engine's
discontinuity rule, k = 0.1 as for stubs); the route adds such ties
(pnr.route.detail.router.tie_plane_layers).

:class:`ViaModel` answers span questions on the stack and :class:`GridVias` on the
detailed router's grid (whose layers are the routed subset of the stack). Pure
stdlib.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

THROUGH, BLIND, BURIED, MICRO = "through", "blind", "buried", "micro"
KINDS = (THROUGH, BLIND, BURIED, MICRO)
# How a span is made: a through drill, a laser microvia, a controlled-depth drill
# from the outside, or the through hole of a sub-laminate.
DRILL, LASER, DEPTH, LAMINATE = "drill", "laser", "depth", "laminate"

# The price of one more drill pair in the build (through-via prices): a span is used
# only where the vias it serves save more than this (an engine default, the design's
# ``via_policy.drill_pair_cost`` overrides it).
DRILL_PAIR_COST = 2.0
# Families :func:`select_build` scores at most (deterministic order); beyond, the
# search stops and says so.
MAX_FAMILIES = 50000

# The return-tie rule (:func:`return_tie_rule`): the engine's discontinuity rule
# delay <= k * t_rise (pnr.si.bus_classes stub_k, owner decision 2026-09-29), with
# t_rise the design's (``via_policy.t_rise_ns``) or this default: a fast CMOS edge,
# conservative for a design that states none. FR-4's eps_r when the stack has none.
RETURN_K = 0.1
DEFAULT_T_RISE_NS = 1.0
DEFAULT_EPS_R = 4.5
C_MM_PER_PS = 0.299792458

# KiCad DRU ``disallow`` item -> the kinds it bans (through is never removed).
_DRU_ITEMS = {
    "via": (BLIND, BURIED, MICRO),
    "blind_via": (BLIND,),
    "buried_via": (BLIND, BURIED),
    "micro_via": (MICRO,),
}

# KiCad's defaults when a project states no microvia minimums.
_MIN_MICRO = (0.2, 0.1)


def dru_via_bans(text: Optional[str]) -> Tuple[set, List[str]]:
    """``(kinds, notes)``: the via kinds a ``.kicad_dru`` disallows, and one note per
    ban that a layer or condition limits (taken as a ban everywhere)."""
    banned: set = set()
    notes: List[str] = []
    if not text:
        return banned, notes
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for block in re.split(r"\(rule\s", text)[1:]:
        name = re.match(r'\s*"([^"]*)"|\s*(\S+)', block)
        label = (name.group(1) or name.group(2)) if name else "?"
        for items in re.findall(r"\(constraint\s+disallow\s+([^)]*)\)", block):
            kinds = {k for item in items.split() for k in _DRU_ITEMS.get(item, ())}
            if not kinds:
                continue
            banned |= kinds
            if re.search(r"\((layer|condition)\s", block):
                notes.append(
                    "rule %r disallows %s only somewhere; taken as a ban everywhere"
                    % (label, "/".join(sorted(kinds)))
                )
    return banned, notes


def copper_gaps(rows: Sequence[dict], names: Sequence[str]) -> Optional[List[float]]:
    """Dielectric thickness (mm) between each pair of neighbouring copper layers of
    ``names``, from a stackup block's rows (:func:`pnr.stack.stackup_rows`), or None
    when the block does not list exactly these copper layers or a thickness."""
    copper = [r["name"] for r in rows if r["type"] == "copper"]
    if list(copper) != list(names):
        return None
    gaps: List[float] = []
    current = None
    for row in rows:
        if row["type"] == "copper":
            if current is not None:
                gaps.append(round(current, 6))
            current = 0.0
        elif current is not None and (
            row["type"] in ("core", "prepreg") or row["name"].startswith("dielectric")
        ):
            if row["thickness_mm"] is None:
                return None
            current += row["thickness_mm"]
    return gaps if len(gaps) == len(names) - 1 else None


def copper_bonds(rows: Sequence[dict], names: Sequence[str]) -> Optional[List[Optional[str]]]:
    """What joins each pair of neighbouring copper layers of ``names``: ``prepreg``
    (a bond line a sub-laminate may end at; any prepreg row in the gap counts),
    ``core`` (the two faces of one laminate) or None when the rows do not say; None
    when the block does not list exactly these copper layers."""
    copper = [r["name"] for r in rows if r["type"] == "copper"]
    if list(copper) != list(names):
        return None
    bonds: List[Optional[str]] = []
    current = None
    for row in rows:
        if row["type"] == "copper":
            if current is not None:
                bonds.append(
                    "prepreg" if "prepreg" in current else "core" if current == {"core"} else None
                )
            current = set()
        elif current is not None and (
            row["type"] in ("core", "prepreg") or row["name"].startswith("dielectric")
        ):
            current.add(row["type"] if row["type"] in ("core", "prepreg") else "?")
    return bonds if len(bonds) == len(names) - 1 else None


def stack_eps_r(rows: Sequence[dict]) -> Optional[float]:
    """The largest dielectric constant of the stack's dielectric rows (the slowest
    medium, so the shortest return-tie distance), or None when none states one."""
    values = [
        r["epsilon_r"]
        for r in rows
        if r["type"] != "copper" and r.get("epsilon_r") and r["epsilon_r"] > 0
    ]
    return max(values) if values else None


def return_tie_rule(declared=None, eps_r: Optional[float] = None) -> dict:
    """The return-tie distance (module doc): ``max_mm = k * t_rise / (2 * delay)``,
    the return current's detour to the tie and back delayed no more than
    ``k * t_rise``; ``delay = sqrt(eps_r) / c`` (ps/mm). ``declared`` may give
    ``t_rise_ns``; ``eps_r`` is the stack's (:func:`stack_eps_r`)."""
    given = declared.get("t_rise_ns") if isinstance(declared, dict) else None
    t_rise = float(given) if given is not None else DEFAULT_T_RISE_NS
    if not t_rise > 0:
        raise ValueError("t_rise_ns must be positive")
    er = float(eps_r) if eps_r else DEFAULT_EPS_R
    delay = math.sqrt(er) / C_MM_PER_PS
    return dict(
        k=RETURN_K,
        t_rise_ns=t_rise,
        t_rise_source="design" if given is not None else "engine default (the design gives none)",
        eps_r=er,
        eps_r_source="stack" if eps_r else "FR-4 default (the stack gives none)",
        delay_ps_per_mm=round(delay, 4),
        max_mm=round(RETURN_K * t_rise * 1000.0 / (2.0 * delay), 4),
    )


def resolve(
    declared,
    layers: Sequence[str],
    *,
    gaps: Optional[Sequence[float]] = None,
    banned: Iterable[str] = (),
    via_size: Tuple[float, float] = (0.6, 0.3),
    microvia: Optional[Tuple[float, float]] = None,
    min_microvia: Tuple[float, float] = _MIN_MICRO,
    min_annular: float = 0.0,
    notes: Sequence[str] = (),
    bonds: Optional[Sequence[Optional[str]]] = None,
    eps_r: Optional[float] = None,
    needs: Optional[Sequence[tuple]] = None,
) -> Optional[dict]:
    """The board's via policy (module doc), or None for through vias only.

    ``declared``: the design's via policy (``{"allowed": [...], "microvia":
    {"diameter_mm", "drill_mm"}}``, or just the list of kinds); None declares
    through vias only. ``banned``: kinds the board's rules disallow
    (:func:`dru_via_bans`). ``via_size``: the routed via (diameter, drill), also the
    size of blind and buried vias. ``microvia``: the project's microvia size, used
    when the design gives none; a microvia under ``min_microvia`` is not used, and
    one whose ring is under ``min_annular`` (the board's minimum annular width,
    which KiCad applies to microvias too) is widened to meet it. ``bonds``: what
    each dielectric is (:func:`copper_bonds`; None: no laminated span qualifies).
    ``eps_r``: the stack's dielectric constant (:func:`return_tie_rule`).

    ``needs`` (:func:`board_needs`): the vias the board will want; with them the
    policy carries its ``build`` (:func:`select_build`), and is None when the
    cheapest build is through vias only. Without them there is no ``build`` and
    every buildable span is a candidate (:class:`ViaModel`)."""
    if declared is None:
        return None
    kinds = declared.get("allowed", []) if isinstance(declared, dict) else list(declared)
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ValueError("unknown via kind(s) %s; known: %s" % (unknown, ", ".join(KINDS)))
    warnings = list(notes)
    banned = set(banned)
    allowed = [k for k in KINDS if k in kinds and k not in banned]
    for kind in sorted(set(kinds) & banned - {THROUGH}):
        warnings.append("%s vias are declared but the board's rules disallow them" % kind)
    if THROUGH not in allowed:
        allowed.insert(0, THROUGH)
    layers = list(layers)
    if len(layers) < 3:
        return None
    sizes = {BLIND: list(via_size), BURIED: list(via_size)}
    if MICRO in allowed:
        size = None
        if isinstance(declared, dict) and declared.get("microvia"):
            size = (
                float(declared["microvia"]["diameter_mm"]),
                float(declared["microvia"]["drill_mm"]),
            )
        elif microvia:
            size = (float(microvia[0]), float(microvia[1]))
        if size is None:
            warnings.append("microvias are allowed but no microvia size is given: not used")
            allowed.remove(MICRO)
        elif size[0] < min_microvia[0] - 1e-9 or size[1] < min_microvia[1] - 1e-9:
            warnings.append(
                "microvia %.3g/%.3g mm is under the board's minimum %.3g/%.3g mm: not used"
                % (size + tuple(min_microvia))
            )
            allowed.remove(MICRO)
        elif size[1] >= size[0]:
            raise ValueError("microvia drill must be smaller than its diameter")
        else:
            ring = round(size[1] + 2 * min_annular, 6)
            if size[0] < ring - 1e-9:
                warnings.append(
                    "microvia %.3g/%.3g mm widened to %.3g/%.3g mm: the board's minimum "
                    "annular width is %.3g mm" % (size + (ring, size[1], min_annular))
                )
                size = (ring, size[1])
            sizes[MICRO] = list(size)
    if allowed == [THROUGH]:
        return None
    if gaps is not None and len(gaps) != len(layers) - 1:
        raise ValueError("gaps_mm must list one dielectric per neighbouring copper pair")
    if bonds is not None and len(bonds) != len(layers) - 1:
        raise ValueError("bonds must name one dielectric per neighbouring copper pair")
    out = dict(
        allowed=allowed,
        layers=layers,
        gaps_mm=None if gaps is None else [float(g) for g in gaps],
        bonds=None if bonds is None else list(bonds),
        sizes={k: v for k, v in sizes.items() if k in allowed},
        return_tie=return_tie_rule(declared, eps_r),
    )
    if MICRO in allowed and gaps is None:
        warnings.append("the stack states no dielectric thickness: no microvia span qualifies")
    if (BLIND in allowed or BURIED in allowed) and (bonds is None or None in bonds):
        warnings.append(
            "the stack does not say which dielectrics are cores and which prepreg: no "
            "laminated blind or buried span ends at such a gap"
        )
    if isinstance(declared, dict) and declared.get("drill_pair_cost") is not None:
        out["drill_pair_cost"] = float(declared["drill_pair_cost"])
    if warnings:
        out["warnings"] = warnings
    if needs is not None:
        out["build"] = select_build(out, needs, via_size)
        if not out["build"]["spans"]:
            return None
    return out


def board_policy(declared, board_path, rules: dict, graph=None) -> Optional[dict]:
    """:func:`resolve` for the board file ``board_path``: its stackup block (copper
    layers, dielectrics and their kinds), the custom rules beside it
    (``.kicad_dru``) and the project's microvia size and minimums (``.kicad_pro``).
    ``rules`` are the routing rules (layer count, the fab via size and the minimum
    annular width that writeback stamps into the project). With the board's
    ``graph`` (pnr.graph.BoardGraph) the policy carries its build
    (:func:`board_needs`, :func:`select_build`)."""
    from pnr.stack import copper_names, stackup_rows

    if declared is None:
        return None
    path = str(board_path)
    stem = path[: -len(".kicad_pcb")] if path.endswith(".kicad_pcb") else path
    text = _read(path) or ""
    rows = stackup_rows(text)
    names = [r["name"] for r in rows if r["type"] == "copper"]
    count = int(rules.get("layers", 2))
    if len(names) != count:
        names = copper_names(count)
    gaps = copper_gaps(rows, names)
    bonds = copper_bonds(rows, names)
    banned, notes = dru_via_bans(_read(stem + ".kicad_dru"))
    microvia = None
    minimum = _MIN_MICRO
    project = _read(stem + ".kicad_pro")
    if project:
        try:
            doc = json.loads(project)
        except ValueError:
            doc = {}
        design = (doc.get("board") or {}).get("design_settings") or {}
        limits = design.get("rules") or {}
        minimum = (
            float(limits.get("min_microvia_diameter", _MIN_MICRO[0])),
            float(limits.get("min_microvia_drill", _MIN_MICRO[1])),
        )
        for nc in (doc.get("net_settings") or {}).get("classes") or []:
            if nc.get("name") == "Default" and nc.get("microvia_diameter"):
                microvia = (float(nc["microvia_diameter"]), float(nc["microvia_drill"]))
    fab = rules.get("fab") or {}
    policy = resolve(
        declared,
        names,
        gaps=gaps,
        banned=banned,
        via_size=(float(fab.get("via_diameter_mm", 0.6)), float(fab.get("via_drill_mm", 0.3))),
        microvia=microvia,
        min_microvia=minimum,
        min_annular=float(fab.get("via_annular_mm") or 0.0),
        notes=notes,
        bonds=bonds,
        eps_r=stack_eps_r(rows),
        needs=None if graph is None else board_needs(graph, rules, names),
    )
    if policy is None and graph is not None and declared:
        import sys

        sys.stderr.write(
            "via policy: through vias only (no other kind allowed, or no span earns its "
            "drill pair on this board)\n"
        )
    return policy


def compatible(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Two laminated spans can be in one build: nested or disjoint (each is a node
    of one sequential-lamination tree)."""
    return (
        a[1] < b[0]
        or b[1] < a[0]
        or (a[0] <= b[0] and b[1] <= a[1])
        or (b[0] <= a[0] and a[1] <= b[1])
    )


def select_build(policy: dict, needs: Sequence[tuple], through=(0.6, 0.3)) -> dict:
    """The board's build: the family of buildable non-through spans (module doc)
    that serves ``needs`` cheapest.

    ``needs``: ``(top, bottom, weight, why)`` copper-layer pairs a via must join
    (:func:`board_needs`). A family's price is ``weight * cost`` of each need's
    cheapest span in it (a through via, 1.0, always serves) plus
    ``drill_pair_cost`` (the policy's, else :data:`DRILL_PAIR_COST`) per span.
    Every family is scored whose laminated spans nest or are disjoint
    (:func:`compatible`; laser and controlled-depth spans are drilled from the
    outside and fit any build), at most :data:`MAX_FAMILIES`; ties go to fewer
    spans, then the spans in stack order.

    Returns ``{"spans": [[top, bottom, kind, how], ...], "drill_pair_cost",
    "price", "through_price", "families", "needs"}``."""
    model = ViaModel(dict(policy, build=None), through)
    n = model.n
    price = float(policy.get("drill_pair_cost", DRILL_PAIR_COST))
    cands = [
        (t, b)
        for t in range(n)
        for b in range(t + 1, n)
        if (t, b) != (0, n - 1) and model.how(t, b) is not None
    ]
    lam = [s for s in cands if model.how(*s)[1] == LAMINATE]
    free = [s for s in cands if s not in lam]
    cost = {s: model.cost(s[0], s[1], model.how(*s)[0]) for s in cands}
    want = []
    for top, bottom, weight, *_ in needs:
        t, b = sorted((model.index(top), model.index(bottom)))
        if t < b and weight > 0:
            want.append((t, b, float(weight)))

    def score(family):
        total = price * len(family)
        for t, b, w in want:
            best = 1.0
            for s in family:
                if s[0] <= t and b <= s[1] and cost[s] < best:
                    best = cost[s]
            total += w * best
        return total

    laminar: List[Tuple[Tuple[int, int], ...]] = []

    def grow(i, chosen):
        if len(laminar) * (1 << len(free)) >= MAX_FAMILIES:
            return
        if i == len(lam):
            laminar.append(tuple(chosen))
            return
        grow(i + 1, chosen)
        if all(compatible(lam[i], c) for c in chosen):
            chosen.append(lam[i])
            grow(i + 1, chosen)
            chosen.pop()

    grow(0, [])
    best = None
    count = 0
    for base in laminar:
        for mask in range(1 << len(free)):
            family = tuple(sorted(base + tuple(f for k, f in enumerate(free) if mask >> k & 1)))
            count += 1
            key = (round(score(family), 9), len(family), family)
            if best is None or key < best:
                best = key
    out = dict(
        spans=[[model.layers[t], model.layers[b], *model.how(t, b)] for t, b in best[2]],
        drill_pair_cost=price,
        price=round(best[0], 4),
        through_price=round(score(()), 4),
        families=count,
        needs=[[model.layers[t], model.layers[b], round(w, 4)] for t, b, w in sorted(want)],
    )
    if count >= MAX_FAMILIES:
        out["truncated"] = True
    return out


def reference_planes(names: Sequence[str], dedicated: Sequence[Tuple[str, str]], name: str):
    """``[(layer, net)]``: the dedicated plane(s) nearest above and nearest below
    copper layer ``name`` (a stripline's two references, a surface layer's one)."""
    here = list(names).index(name)
    rows = [(list(names).index(la), la, net) for la, net in dedicated]
    out = []
    above = [r for r in rows if r[0] < here]
    below = [r for r in rows if r[0] > here]
    if above:
        top = max(r[0] for r in above)
        out += [(la, net) for i, la, net in above if i == top]
    if below:
        bottom = min(r[0] for r in below)
        out += [(la, net) for i, la, net in below if i == bottom]
    return out


def board_needs(graph, rules: dict, layers: Sequence[str]) -> List[tuple]:
    """The vias ``graph`` (pnr.graph.BoardGraph, its parts on the sides they start
    on) will want, as :func:`select_build` needs ``(top, bottom, weight, why)``:

    ``drop``    each surface pad of a net with a dedicated plane, to its nearest
                plane (the router's plane drop);
    ``signal``  each signal net's connections (pads - 1, shared out by the sides of
                its surface pads; a plated pad changes layer by itself), each
                leaving its pad's layer for each other routed layer with equal
                chance, two vias when it does: ``2 * connections / routed`` per
                routed layer;
    ``tie``     each such layer change whose reference planes (nearest above and
                below each end) are two plane layers of one net: a via of that net
                joining them (the return-tie rule, one per change at most);
    ``floor``   one via joining the plane layers of a net with several.

    An estimate: it only ranks the builds."""
    from pnr.stack import assess

    stack, _ = assess(rules, getattr(graph, "stack", None))
    names = list(layers)
    if stack is not None and list(stack.names) == names:
        routed = list(stack.grid_layers)
        dedicated = list(stack.dedicated)
    else:
        routed, dedicated = names, []
    pos = {n: i for i, n in enumerate(names)}
    plane_nets = {net for _, net in dedicated}
    weights: Dict[Tuple[str, str, str], float] = {}

    def add(a, b, w, why):
        if a == b or w <= 0:
            return
        a, b = sorted((a, b), key=pos.get)
        weights[(a, b, why)] = weights.get((a, b, why), 0.0) + w

    pads: Dict[str, Dict[str, int]] = {}
    for comp in graph.components:
        side = names[0] if comp.side == "top" else names[-1]
        for pad in comp.pads:
            if not pad.net:
                continue
            count = pads.setdefault(pad.net, {})
            key = "plated" if pad.through_hole else side
            count[key] = count.get(key, 0) + 1
            if pad.net in plane_nets and not pad.through_hole:
                planes = [la for la, net in dedicated if net == pad.net]
                nearest = min(planes, key=lambda la: (abs(pos[la] - pos[side]), pos[la]))
                add(side, nearest, 1.0, "drop")
    for net, count in sorted(pads.items()):
        if net in plane_nets:
            continue
        total = sum(count.values())
        if total < 2:
            continue
        for side, k in sorted(count.items()):
            if side == "plated" or side not in routed:
                continue
            share = 2.0 * (total - 1) * k / total / len(routed)
            for other in routed:
                if other == side:
                    continue
                add(side, other, share, "signal")
                refs = reference_planes(names, dedicated, side) + reference_planes(
                    names, dedicated, other
                )
                for plane_net in sorted({n for _, n in refs}):
                    joined = sorted({la for la, n in refs if n == plane_net}, key=pos.get)
                    if len(joined) > 1:
                        add(joined[0], joined[-1], share, "tie")
    for net in sorted(plane_nets):
        planes = [la for la, n in dedicated if n == net]
        if len(planes) > 1:
            add(planes[0], planes[-1], 1.0, "floor")
    return [(a, b, round(w, 6), why) for (a, b, why), w in sorted(weights.items())]


def _read(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ the stack model


class ViaModel:
    """Spans on the stack of ``policy`` (stack indices, 0 = F.Cu). The spans a via
    may take are the policy's ``build`` (:func:`select_build`); a policy without
    one (or ``build=None``) offers every buildable span (:meth:`how`), which is the
    candidate set the build is chosen from."""

    def __init__(self, policy: dict, through: Tuple[float, float]):
        self.policy = policy
        self.layers = list(policy["layers"])
        self.n = len(self.layers)
        self.allowed = set(policy["allowed"]) | {THROUGH}
        self.gaps = policy.get("gaps_mm")
        self.bonds = policy.get("bonds")
        self.sizes = {k: tuple(v) for k, v in (policy.get("sizes") or {}).items()}
        self.sizes[THROUGH] = tuple(through)
        build = policy.get("build")
        self.family = (
            None
            if build is None
            else {(self.index(s[0]), self.index(s[1])) for s in build.get("spans", [])}
        )
        self._cover: Dict[Tuple[int, int], Tuple[int, int, str]] = {}

    def index(self, name: str) -> int:
        return self.layers.index(name)

    def micro_ok(self, t: int, b: int) -> bool:
        """A microvia may join stack layers ``t < b``: adjacent, one of them outer,
        and a dielectric no deeper than its drill."""
        if MICRO not in self.allowed or b != t + 1 or not (t == 0 or b == self.n - 1):
            return False
        if self.gaps is None:
            return False
        return self.gaps[t] <= self.sizes[MICRO][1] + 1e-9

    def depth(self, t: int, b: int) -> Optional[float]:
        """Dielectric between stack layers ``t`` and ``b`` (mm), None if unknown."""
        return None if self.gaps is None else sum(self.gaps[t:b])

    def laminated(self, t: int, b: int) -> bool:
        """``t..b`` is the through hole of a sub-laminate: each end faces a prepreg
        bond line or the board's face (never a core's other face)."""
        if self.bonds is None:
            return False
        return (t == 0 or self.bonds[t - 1] == "prepreg") and (
            b == self.n - 1 or self.bonds[b] == "prepreg"
        )

    def how(self, t: int, b: int) -> Optional[Tuple[str, str]]:
        """``(kind, how)`` of a buildable via spanning exactly ``t..b`` of an
        allowed kind (module doc), whatever the build, or None: a laser microvia
        first, then a controlled-depth blind via, then a laminated one."""
        if not 0 <= t < b < self.n:
            raise ValueError("a via spans two distinct copper layers")
        if t == 0 and b == self.n - 1:
            return THROUGH, DRILL
        if self.micro_ok(t, b):
            return MICRO, LASER
        outer = t == 0 or b == self.n - 1
        depth = self.depth(t, b)
        if (
            outer
            and BLIND in self.allowed
            and depth is not None
            and depth <= self.sizes.get(BLIND, self.sizes[THROUGH])[1] + 1e-9
        ):
            return BLIND, DEPTH
        kind = BLIND if outer else BURIED
        if kind in self.allowed and self.laminated(t, b):
            return kind, LAMINATE
        return None

    def kind_of(self, t: int, b: int) -> Optional[str]:
        """The kind a via spanning exactly ``t..b`` is in this build, or None when
        the span is not buildable or not in the build."""
        got = self.how(t, b)
        if got is None:
            return None
        if got[0] != THROUGH and self.family is not None and (t, b) not in self.family:
            return None
        return got[0]

    def cost(self, t: int, b: int, kind: str) -> float:
        if kind == THROUGH:
            return 1.0
        if self.gaps:
            total = sum(self.gaps)
            depth = sum(self.gaps[t:b])
        else:
            total, depth = float(self.n - 1), float(b - t)
        return 0.5 + 0.5 * (depth / total if total > 0 else 1.0)

    def cover(self, t: int, b: int) -> Tuple[int, int, str]:
        """``(top, bottom, kind)``: the cheapest allowed span containing ``t..b``
        (a through via always does)."""
        if t > b:
            t, b = b, t
        key = (t, b)
        if key not in self._cover:
            best = None
            for top in range(t, -1, -1):
                for bottom in range(max(b, top + 1), self.n):
                    kind = self.kind_of(top, bottom)
                    if kind is None:
                        continue
                    rank = (self.cost(top, bottom, kind), bottom - top, top)
                    if best is None or rank < best[0]:
                        best = (rank, (top, bottom, kind))
            self._cover[key] = best[1]
        return self._cover[key]

    def size(self, kind: str) -> Tuple[float, float]:
        return self.sizes.get(kind, self.sizes[THROUGH])


@dataclass(frozen=True)
class Span:
    """A via on the router's grid: copper layers ``t..b`` of the stack (indices)
    named ``top``/``bottom``, the grid layers ``lo..hi`` it occupies, its kind, size
    (mm), keep-out (grid cells) and price multiplier."""

    t: int
    b: int
    lo: int
    hi: int
    top: str
    bottom: str
    kind: str
    diameter: float
    drill: float
    keepout: int
    cost: float

    @property
    def radius(self) -> float:
        return self.diameter / 2.0

    @property
    def through(self) -> bool:
        return self.kind == THROUGH

    def layers(self):
        return range(self.lo, self.hi + 1)


class GridVias:
    """:class:`ViaModel` on a detailed-routing grid whose layers ``grid_layers`` are
    a subset of the stack (the plane layers between them are not grid layers).

    ``through_keepout`` is the grid's own via keep-out radius: a through span keeps
    it exactly, so the through path of the router is unchanged; any other kind's
    radius follows the same rule for its own diameter against the largest via."""

    def __init__(
        self,
        policy: dict,
        grid_layers: Sequence[str],
        pitch: float,
        clearance: float,
        through: Tuple[float, float],
        through_keepout: int,
    ):
        self.model = ViaModel(policy, through)
        self.grid_layers = list(grid_layers)
        self.stack_index = [self.model.index(n) for n in self.grid_layers]
        if self.stack_index != sorted(self.stack_index) or len(set(self.stack_index)) != len(
            self.stack_index
        ):
            raise ValueError("grid layers must be distinct copper layers in stack order")
        self.pitch = float(pitch)
        self.clearance = float(clearance)
        self.through_keepout = int(through_keepout)
        self.max_radius = max(self.model.size(k)[0] / 2.0 for k in self.model.allowed)
        self._spans: Dict[Tuple[int, int], Span] = {}
        self.full = self.stack_span(0, self.model.n - 1)

    def stack_span(self, t: int, b: int) -> Span:
        """The cheapest allowed via containing stack layers ``t..b``."""
        key = (min(t, b), max(t, b))
        span = self._spans.get(key)
        if span is None:
            top, bottom, kind = self.model.cover(*key)
            covered = [g for g, s in enumerate(self.stack_index) if top <= s <= bottom]
            diameter, drill = self.model.size(kind)
            if kind == THROUGH:
                keepout = self.through_keepout
            else:
                keepout = max(
                    1,
                    math.ceil((diameter / 2.0 + self.max_radius + self.clearance) / self.pitch) - 1,
                )
            span = self._spans[key] = Span(
                top,
                bottom,
                covered[0] if covered else -1,
                covered[-1] if covered else -2,
                self.model.layers[top],
                self.model.layers[bottom],
                kind,
                diameter,
                drill,
                keepout,
                self.model.cost(top, bottom, kind),
            )
        return span

    def span(self, a: int, b: int) -> Span:
        """The via joining grid layers ``a`` and ``b``."""
        return self.stack_span(self.stack_index[a], self.stack_index[b])

    def to_layer(self, a: int, name: str) -> Span:
        """The via from grid layer ``a`` to copper layer ``name`` (a plane drop)."""
        return self.stack_span(self.stack_index[a], self.model.index(name))

    def reaches(self, span: Span, name: str) -> bool:
        """``span`` connects copper layer ``name``."""
        return span.t <= self.model.index(name) <= span.b

    def merged(self, spans: Iterable[Span]) -> List[Span]:
        """Same-net vias at one place: spans sharing a copper layer are one barrel
        (two holes there would be co-located); disjoint spans stay apart."""
        return [self.stack_span(t, b) for t, b in merge_ranges((s.t, s.b) for s in spans)]


def merge_ranges(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge ranges ``(t, b)`` that share an index; disjoint ranges stay separate."""
    out: List[List[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]
