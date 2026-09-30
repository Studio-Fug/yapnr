"""Top-level hierarchical placement: choose block layouts, place macros, expand.

Layout choice per template is sampled from that template's library of routed
local layouts, restricted mechanically to the best measured tier (all
internally complete layouts if any exist, else the minimum-missing ones).
Within the tier the layouts stay in library rank order (the full native rank
key of :func:`pnr.hier.synth_native.rank_key`, i.e. subwidth ascending among
complete layouts) and the Monte Carlo draw is geometric in that rank: tier
entry ``i`` is drawn with probability proportional to ``0.5 ** i`` from the
seeded rng, so better-measured layouts are preferred while every tier member
stays reachable. ``PNR_HIER_RANK_DECAY`` overrides the ratio (1 = uniform).

N-0001 flags (all default off; unset they change nothing):
``PNR_LIBRARY_RANK_USED=1`` ranks the tier with the used-extent area band
(:func:`pnr.hier.extent.stamp_used`, :func:`pnr.hier.synth_native.rank_key`);
``PNR_MACRO_SHRINK=1`` / ``PNR_MACRO_HULL=1`` measure each drawn layout's routed
board and pass it to :func:`pnr.hier.macro.collapse` (used-extent courtyard /
per-side occupancy hull).
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

from pnr.graph import BoardGraph
from pnr.hier.blocks import extract_blocks
from pnr.hier.macro import collapse
from pnr.hier.synth import instance_board, local_key


def _is_tier_map(data):
    """True for a frozen ``{block name: [layout rec, ...]}`` map (``load_library`` output)."""
    return isinstance(data, dict) and all(
        isinstance(v, list) and all(isinstance(r, dict) and "missing" in r for r in v)
        for v in data.values()
    )


def _libraries(source: Path):
    """Library documents from a synth output root or from one library JSON file.

    A file holds one ``library.json`` document, a list of them or a
    ``{template id: library}`` mapping.
    """
    source = Path(source)
    if not source.is_file():
        return [json.loads(path.read_text()) for path in sorted(source.glob("*/library.json"))]
    data = json.loads(source.read_text())
    if isinstance(data, dict) and "ranked" in data:
        return [data]
    if isinstance(data, list) and all(isinstance(v, dict) and "ranked" in v for v in data):
        return data
    if isinstance(data, dict) and all(isinstance(v, dict) and "ranked" in v for v in data.values()):
        return list(data.values())
    raise ValueError(f"{source}: not a block library or library snapshot")


def snapshot_library(root: Path, out: Path):
    """Freeze ``load_library(root)`` into ``out`` (atomic).

    Same bytes as ``pnr.mc.halving``'s ``OUT/library.snapshot.json`` (sorted
    keys, indent 1), so there is one snapshot format and both read back through
    :func:`load_library`.
    """
    out = Path(out)
    tmp = out.with_name(".%s.%d.tmp" % (out.name, os.getpid()))
    tmp.write_text(json.dumps(load_library(root), sort_keys=True, indent=1))
    os.replace(tmp, out)
    return out


def load_library(source: Path):
    """{block name: [layout rec, ...]} over the best tier, in rank order.

    ``source`` is a synth output root (``*/library.json``), one library JSON
    file (see :func:`_libraries`) or a frozen snapshot of this function's own
    output (:func:`snapshot_library`, ``pnr.mc.halving``'s
    ``library.snapshot.json``). From libraries the tier is every layout with the
    minimum ``missing``, stable re-sorted by the native rank key (so hand-edited
    or older libraries are ordered too), which :func:`draw_layout` relies on. A
    snapshot is already that tier and is returned as frozen (blocks with an
    empty tier dropped, as for a library without ranked layouts), exactly what
    halving's workers draw from, so a seed reproduces their layout choice.
    """
    from pnr.hier.synth_native import rank_key

    source = Path(source)
    if source.is_file():
        data = json.loads(source.read_text())
        if _is_tier_map(data):
            return {name: tier for name, tier in data.items() if tier}
    lib = {}
    for data in _libraries(source):
        ranked = data.get("ranked") or []
        if not ranked:
            continue
        best = min(r["missing"] for r in ranked)
        tier = [r for r in ranked if r["missing"] == best]
        if os.environ.get("PNR_LIBRARY_RANK_USED") == "1":
            from pnr.hier.extent import stamp_used

            tier = stamp_used(tier)
        tier = sorted(tier, key=rank_key)
        for name in data["blocks"]:
            lib[name] = tier
    return lib


def draw_layout(tier, rng, ratio=None):
    """Return (index, rec): tier entry ``i`` with probability ``ratio**i / sum``.

    ``ratio`` defaults to ``PNR_HIER_RANK_DECAY`` or 0.5; one ``rng.random()``
    draw per call keeps the choice reproducible from the placement seed.
    """
    ratio = float(os.environ.get("PNR_HIER_RANK_DECAY", 0.5)) if ratio is None else ratio
    weights = [ratio**i for i in range(len(tier))]
    x = rng.random() * sum(weights)
    for i, w in enumerate(weights):
        x -= w
        if x < 0:
            return i, tier[i]
    return len(tier) - 1, tier[-1]


def macro_pair_weights(pair_weights, plan):
    """Flat pad-pair weights {(ref_a, pad_a, ref_b, pad_b): w} on the macro graph.

    A block member's pad is the macro pad ``'<ref>.<pad>'`` of its macro; pairs
    inside one macro are rigid there and dropped; duplicates add up."""
    out = {}
    for (ra, pa, rb, pb), w in (pair_weights or {}).items():
        ma, mb = plan.member_of.get(ra), plan.member_of.get(rb)
        if ma is not None and ma == mb:
            continue
        a = (ma, "%s.%s" % (ra, pa)) if ma else (ra, pa)
        b = (mb, "%s.%s" % (rb, pb)) if mb else (rb, pb)
        out[(a[0], a[1], b[0], b[1])] = out.get((a[0], a[1], b[0], b[1]), 0.0) + float(w)
    return out or None


def hierarchical_place(graph, constraints, rules, library, seed, iters=600, pair_weights=None):
    """Return (flat placed graph, placement report, choice record).

    ``pair_weights`` (flat refs, pnr.feedback) become a pad-pair attraction on the
    macro placement; None leaves it as before."""
    from pnr.place.initial_pool import _prepared_source, preserve_source_locks
    from pnr.place.placer import place

    rng = random.Random(seed)
    constraints = preserve_source_locks(graph, constraints)
    source = _prepared_source(graph, constraints, rules)
    blocks = [b for b in extract_blocks(source, constraints) if b.name in library]
    by_template = {}
    for b in blocks:
        by_template.setdefault(b.template, []).append(b)
    shaped = os.environ.get("PNR_MACRO_SHRINK") == "1" or os.environ.get("PNR_MACRO_HULL") == "1"
    geometry = {} if shaped else None
    layouts, choice = [], {}
    for template, members in sorted(by_template.items(), key=lambda kv: kv[1][0].name):
        tier = library[members[0].name]
        rank, rec = draw_layout(tier, rng)
        for b in members:
            sub, _, _ = instance_board(
                source, constraints, rules, b, rec["layout"], rec["width"], rec["height"]
            )
            layouts.append((b, sub, rec["width"], rec["height"]))
            boards = {
                i["instance"]: i.get("dir") for i in rec.get("instances", []) if isinstance(i, dict)
            }
            choice[b.name] = dict(
                width=rec["width"],
                height=rec["height"],
                seed=rec["seed"],
                utilisation=rec["utilisation"],
                aspect=rec["aspect"],
                missing=rec["missing"],
                port_debt_mm=rec["port_debt_mm"],
                objective=rec.get("objective"),
                tier_rank=rank,
                tier_size=len(tier),
                native_dir=boards.get(b.name),
            )
            if shaped:
                # Each instance (twins included) is measured on its own routed board.
                from pnr.hier.extent import safe_geometry

                d = boards.get(b.name)
                geometry[b.name] = safe_geometry(
                    sub.components, d and os.path.join(d, "electrical", "board.kicad_pcb"), rules
                )
    if shaped:
        mgraph, mcon, mrules, plan = collapse(
            source, constraints, rules, layouts, geometry=geometry
        )
    else:
        mgraph, mcon, mrules, plan = collapse(source, constraints, rules, layouts)
    placed_macro, report = place(
        mgraph,
        mcon,
        seed=seed,
        iters=iters,
        orient=True,
        spread=1.0,
        channel_rules=mrules,
        pair_weights=macro_pair_weights(pair_weights, plan),
    )
    flat = plan.expand(placed_macro, source)
    macros = {
        m: dict(block=v["block"], width=v["width"], height=v["height"])
        for m, v in plan.macros.items()
    }
    out = dict(blocks=choice, macro_legal=report.legal, macros=macros)
    if shaped:
        by_ref = {c.ref: c for c in placed_macro.components}
        for m, v in plan.macros.items():
            mc = by_ref[m]
            macros[m].update(
                courtyard=list(v.get("courtyard", ())),
                origin=list(v.get("origin", ())),
                shape=v.get("shape"),
                reason=v.get("reason"),
                hull=v.get("hull"),
                pose=[mc.pos[0], mc.pos[1], mc.rot, mc.side],
            )
            geo = geometry.get(v["block"])
            if geo is not None and geo.ok:
                macros[m]["extent"] = [round(x, 4) for x in geo.extent]
        out["nested"] = nested_parts(placed_macro, plan)
    return flat, report, out


def nested_parts(placed_macro, plan):
    """{macro ref: [[ref, side], ...]} top-level parts whose courtyard meets a placed macro's courtyard."""
    from pnr.place.geometry import courtyard_rect, occupied_sides

    comps = placed_macro.components
    out = {}
    for m in plan.macros:
        mc = next(c for c in comps if c.ref == m)
        box = courtyard_rect(mc)
        hits = [
            [c.ref, "+".join(occupied_sides(c))]
            for c in comps
            if c.ref not in plan.macros and courtyard_rect(c).overlaps(box)
        ]
        if hits:
            out[m] = sorted(hits)
    return out
