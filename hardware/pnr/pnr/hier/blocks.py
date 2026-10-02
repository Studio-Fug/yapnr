"""Derive placement blocks from the source hierarchy, and cut per-block sub-boards.

Blocks come from two authored sources only, never from a hand list:

* atopile module paths: every component under ``<root>.<module>.*`` with at
  least two members forms one block;
* hard proximity groups among the remaining top-level parts: an anchor and its
  hard-group members (transitively) form one block.

Blocks whose members have identical address suffixes, footprints and internal
pin-level connectivity share a ``template`` key, so one local layout can be
instantiated for each (e.g. two identical LED channels).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pnr.constraints import CompiledConstraints, Constraint
from pnr.graph import BoardGraph, BoardOutline, Net


@dataclass
class Block:
    name: str
    refs: List[str]
    template: str = ""
    prefix: str = ""  # address prefix for module blocks
    source: str = "module"  # module | group
    internal_nets: List[str] = field(default_factory=list)
    external_nets: List[str] = field(default_factory=list)

    def to_dict(self):
        return dict(
            name=self.name,
            refs=self.refs,
            template=self.template,
            prefix=self.prefix,
            source=self.source,
            internal_nets=self.internal_nets,
            external_nets=self.external_nets,
        )


def _module_of(address: str) -> Optional[str]:
    parts = (address or "").split(".")
    return ".".join(parts[:2]) if len(parts) > 2 else None


def _suffix(address: str, prefix: str) -> str:
    if not prefix or not address.startswith(prefix):
        return address
    return address[len(prefix) :].lstrip(".") or "@"


def extract_blocks(graph: BoardGraph, constraints: CompiledConstraints) -> List[Block]:
    by_ref = {c.ref: c for c in graph.components}
    # Parts carrying board-level pose relations (edge rows, fixed poses, line
    # groups, alignments) are interface parts: they stay top-level so those relations
    # remain exact. (A region on a block member becomes a body of the block macro.)
    interface = {
        r
        for con in constraints.constraints
        if con.kind in ("row", "fixed", "edge_align", "line_group", "align")
        for r in con.refs
    }
    modules: Dict[str, List[str]] = {}
    for c in graph.components:
        if c.ref in interface:
            continue
        m = _module_of(c.address)
        if m:
            modules.setdefault(m, []).append(c.ref)
    blocks = [
        Block(name=m, refs=sorted(refs), prefix=m, source="module")
        for m, refs in sorted(modules.items())
        if len(refs) >= 2
    ]
    taken = {r for b in blocks for r in b.refs} | interface

    # Union-find over hard groups restricted to parts not already in a module block.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for con in constraints.constraints:
        if con.kind != "group" or con.enforcement.name != "HARD":
            continue
        anchor = con.params.get("anchor")
        refs = [r for r in con.refs if r not in taken]
        if anchor and anchor not in taken:
            refs = refs + [anchor]
        refs = [r for r in refs if r in by_ref]
        if len(refs) < 2:
            continue
        for r in refs[1:]:
            parent[find(r)] = find(refs[0])
    clusters: Dict[str, List[str]] = {}
    for r in parent:
        clusters.setdefault(find(r), []).append(r)
    for refs in clusters.values():
        if len(refs) < 2:
            continue
        # Name after the member with the most pads (the anchoring IC).
        head = max(refs, key=lambda r: (len(by_ref[r].pads), r))
        blocks.append(
            Block(
                name="group:" + (by_ref[head].address or head),
                refs=sorted(refs),
                prefix=by_ref[head].address or head,
                source="group",
            )
        )

    net_pins: Dict[str, List[Tuple[str, str]]] = {n.name: list(n.pins) for n in graph.nets}
    for b in blocks:
        inside = set(b.refs)
        for net, pins in net_pins.items():
            touched = [p for p in pins if p[0] in inside]
            if not touched:
                continue
            if len(touched) == len(pins):
                b.internal_nets.append(net)
            else:
                b.external_nets.append(net)
        b.internal_nets.sort()
        b.external_nets.sort()
        b.template = _template_key(graph, b, net_pins)
    return blocks


def _template_key(graph: BoardGraph, block: Block, net_pins) -> str:
    """Placement-template signature invariant to designators and net names.

    Parts (local path, footprint) and the pin structure of *internal* nets must
    match. External connections may differ (e.g. an address strap tied to a
    different rail); each instance is still routed and checked individually.
    """
    by_ref = {c.ref: c for c in graph.components}
    local = {r: _suffix(by_ref[r].address, block.prefix) or r for r in block.refs}
    parts = sorted((local[r], by_ref[r].footprint) for r in block.refs)
    inside = set(block.refs)
    nets = []
    for net in block.internal_nets:
        nets.append(tuple(sorted((local[r], p) for r, p in net_pins[net] if r in inside)))
    return repr((parts, sorted(nets)))


# ------------------------------------------------------------------ sub-boards


def block_area(graph: BoardGraph, block: Block) -> float:
    by_ref = {c.ref: c for c in graph.components}
    return sum(by_ref[r].courtyard[0] * by_ref[r].courtyard[1] for r in block.refs)


def sub_board(
    graph: BoardGraph,
    constraints: CompiledConstraints,
    rules: dict,
    block: Block,
    width: float,
    height: float,
):
    """Return (graph, constraints, rules) for ``block`` alone on a width x height board.

    Nets keep only their in-block pins; nets that leave the block are marked in
    ``rules['block_ports']`` so callers can score port accessibility. Board-level
    constraints that reference parts outside the block are dropped.
    """
    inside = set(block.refs)
    sub = BoardGraph(name=graph.name + ":" + block.name)
    for c in graph.components:
        if c.ref in inside:
            cc = copy.deepcopy(c)
            cc.pos = (width / 2, height / 2)
            cc.locked = False
            sub.components.append(cc)
    for n in graph.nets:
        pins = [p for p in n.pins if p[0] in inside]
        if pins:
            sub.nets.append(Net(name=n.name, code=n.code, pins=pins))
    sub.outline = BoardOutline(width, height)

    con = copy.deepcopy(constraints)
    con.board.width, con.board.height = float(width), float(height)
    kept = []
    for c in con.constraints:
        refs = tuple(r for r in c.refs if r in inside)
        if not refs:
            continue
        if c.kind in ("fixed", "row", "edge_align", "keepout", "line_group", "region", "align"):
            # Absolute/edge poses, regions and alignments are board-level decisions,
            # made when the block is placed (a region on the block macro's bodies).
            continue
        anchor = c.params.get("anchor")
        if anchor and anchor not in inside:
            continue
        kept.append(Constraint(c.kind, c.enforcement, refs, c.params, c.weight, c.name))
    con.constraints = kept
    con.copper_keepouts = [k for k in con.copper_keepouts if k.get("ref") in inside]
    con.mounting_holes = []

    r = copy.deepcopy(rules)
    r["plane_access_intents"] = [
        i for i in r.get("plane_access_intents", []) if i.get("ref") in inside
    ]
    r["copper_keepouts"] = (
        [k for k in r.get("copper_keepouts", []) if k.get("ref") in inside]
        if isinstance(r.get("copper_keepouts"), list)
        else r.get("copper_keepouts")
    )
    r["mounting_holes"] = []
    present = {n.name for n in sub.nets}

    def chain_inside(d):
        # Terminal/auxiliary endpoints are "REF.pin"; a chain leaving the block
        # can neither be resolved (electrical.resolve_pair_chains) nor audited here.
        ends = [e for t in d.get("terminal_chain", []) for e in t.values()]
        ends += [
            e
            for x in d.get("auxiliary_pairs", [])
            for s in ("source", "target")
            for e in x.get(s, {}).values()
        ]
        return all(e.rsplit(".", 1)[0] in inside for e in ends)

    r["diff_pairs"] = [
        d
        for d in r.get("diff_pairs", [])
        if d.get("p") in present and d.get("n") in present and chain_inside(d)
    ]
    # Accepted pair reference witnesses are copper paths in the parent board frame.
    r["routed_pair_references"] = []
    if "length_match" in r:
        r["length_match"] = [
            g for g in r["length_match"] if all(n in present for n in g.get("nets", []))
        ]
    r["current_intents"] = [i for i in r.get("current_intents", []) if i.get("ref") in inside]
    if "electrical_nets" in r:
        # Parent per-net current policy restricted to nets with pins here;
        # electrical.compile_policy inherits it under PNR_SUBBOARD=1.
        r["electrical_nets"] = {n: p for n, p in r["electrical_nets"].items() if n in present}
    if "si_intents" in r:
        # PNR_SI=1: a requirement is evaluated in a block only if its whole chain
        # (driver, series parts, connector, return, shunt pads) is inside it.
        def si_inside(i):
            refs = [i["driver"]["ref"], i["connector"]["ref"]] + [
                s["ref"] for s in i.get("series", [])
            ]
            refs += [s["ref"] for s in i.get("shunts", [])] + (
                [i["return"]["ref"]] if i.get("return") else []
            )
            return all(x in inside for x in refs)

        r["si_intents"] = [i for i in r["si_intents"] if si_inside(i)]
    if "terminal_width_intents" in r:
        # PNR_TERMINAL_MIN_WIDTH=1 contracts, by ref like current_intents.
        r["terminal_width_intents"] = [
            i for i in r["terminal_width_intents"] if i.get("ref") in inside
        ]
    r["block_ports"] = block.external_nets
    return sub, con, r


def aspect_sizes(
    graph: BoardGraph,
    block: Block,
    utilisations=(0.25, 0.35, 0.45, 0.55),
    aspects=(1.0, 1.5, 1 / 1.5),
) -> List[Tuple[float, float, float, float]]:
    """Candidate (width, height, utilisation, aspect) outlines for local layout."""
    area = block_area(graph, block)
    by_ref = {c.ref: c for c in graph.components}
    span = max(max(by_ref[r].courtyard) for r in block.refs) + 1.0
    out = []
    for u in utilisations:
        for a in aspects:
            w = math.sqrt(area / u * a)
            h = area / u / w
            w, h = max(w, span), max(h, span)
            out.append((round(w * 4) / 4, round(h * 4) / 4, u, a))
    return out


def block_constraints_doc(doc: dict, addresses, width: float, height: float) -> dict:
    """Authored constraints restricted to one block's parts, on a width x height board.

    Selectors are kept only where they match block members; board-level relations
    (rows, fixed poses) are dropped because the block is posed at top level.
    """
    import fnmatch

    addresses = set(addresses)

    def hits(selector):
        if not isinstance(selector, str) or not selector.startswith("@"):
            return False
        return any(fnmatch.fnmatchcase(a, selector[1:]) for a in addresses)

    def net_hit(selector):
        if not isinstance(selector, str) or not selector.startswith("net@"):
            return False
        return any(fnmatch.fnmatchcase(a, selector[4:].split(":", 1)[0]) for a in addresses)

    out = copy.deepcopy(doc)
    out.setdefault("board", {})["outline"] = {"w": float(width), "h": float(height)}
    out.pop("row", None)
    out.pop("line_group", None)
    # Board-coordinate regions and alignments apply to the placed block macro.
    out.pop("region", None)
    out.pop("align", None)
    out["fixed"] = {}
    out.pop("layout_array", None)
    if "side" in out:
        out["side"] = {k: [s for s in v if hits(s)] for k, v in out["side"].items()}
        out["side"] = {k: v for k, v in out["side"].items() if v}
    for key in ("keepout", "copper_keepout"):
        if key in out:
            out[key] = [k for k in out[key] if hits(k.get("ref"))]
    if "group" in out:
        groups = []
        for g in out["group"]:
            if g.get("anchor") and not hits(g["anchor"]):
                continue
            members = [m for m in g.get("members", []) if hits(m)]
            if members:
                groups.append(dict(g, members=members))
        out["group"] = groups
    if "orientation" in out:
        out["orientation"] = {k: v for k, v in out["orientation"].items() if hits(k)}
    if "net_class" in out:
        classes = {}
        for name, spec in out["net_class"].items():
            nets = [n for n in spec.get("nets", []) if net_hit(n)]
            if nets:
                classes[name] = dict(spec, nets=nets)
        out["net_class"] = classes
    if "diff_pair" in out:
        out["diff_pair"] = [
            d for d in out["diff_pair"] if net_hit(d.get("p")) and net_hit(d.get("n"))
        ]
    return out
