"""Bounded, per-instance hierarchical layout search; alternating by default.

Methods: independent seeded draws; frozen-pose coordinate swaps; alternating
swaps and warm parent optimization; paired swaps to escape one-block minima.
Candidates never inherit parent routing evidence. Native block provenance stays
attached to each selected record; the assembled parent must be routed/validated.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import random
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class LayoutSearch:
    method: str = "alternating"
    rounds: int = 3
    max_evaluations: int = 96
    variants: int = 6
    relax_iters: int = 60
    diverse: bool = True
    score: str = "objective"

    def __post_init__(self):
        if self.method not in ("off", "independent", "frozen", "alternating", "paired"):
            raise ValueError("Unknown hierarchical layout search method")
        for name, upper in (
            ("rounds", 20),
            ("max_evaluations", 4096),
            ("variants", 64),
            ("relax_iters", 2000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"{name} must be an integer in [1, {upper}]")
        if type(self.diverse) is not bool or self.score not in ("objective", "hpwl", "power"):
            raise ValueError("Invalid layout portfolio/scoring setting")

    @classmethod
    def from_env(cls):
        return cls(
            method=os.environ.get("PNR_HIER_LAYOUT_SEARCH", "alternating"),
            diverse=os.environ.get("PNR_HIER_LAYOUT_DIVERSE", "1") == "1",
            score="power" if os.environ.get("PNR_POWER_FIRST") == "1" else "objective",
        )


def record_id(record):
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def eligible(record):
    """Do not trade any recorded internal failure for a cheaper parent placement.

    Placement-only synthetic records may omit a native objective; their result
    never claims native validation. Native records must have all six violation /
    connectivity / width / pair counters clear, not just minimum missing count.
    Every enabled electrical guard also needs explicit evidence: absent native
    side counters are unknown, never zero.
    """
    objective = record.get("objective")
    native = "objective" in record or record.get("stage") == "native"
    if native:
        if not isinstance(objective, (list, tuple)) or len(objective) < 6:
            return False
        required = []
        if os.environ.get("PNR_POWER_BRANCHES", "1") == "1":
            required.extend(("hot_loops_open", "power_loop_failures"))
        elif os.environ.get("PNR_POWER_FIRST") == "1":
            required.append("hot_loops_open")
        if os.environ.get("PNR_SMPS_LAYOUT", "1") == "1":
            required.extend(("smps_layout_failures", "smps_layout_unqualified"))
        if os.environ.get("PNR_SI") == "1":
            required.append("si_layout_failures")
        if any(record.get(k) != 0 for k in required):
            return False
    return (
        record.get("status", "ok") == "ok"
        and record.get("missing") == 0
        and not record.get("length_unmatched", 0)
        and not any(
            record.get(k, 0) != 0
            for k in (
                "si_layout_failures",
                "hot_loops_open",
                "power_loop_failures",
                "smps_layout_failures",
                "smps_layout_unqualified",
            )
        )
        and (objective is None or (len(objective) >= 6 and all(v == 0 for v in objective[:6])))
    )


def _distance(a, b):
    # Shape in log-mm plus normalized local poses: distinguishes pad ordering
    # and internal rotations even when two layouts have identical envelopes.
    value = sum((math.log(float(a[k]) / float(b[k]))) ** 2 for k in ("width", "height"))
    keys = sorted(set(a["layout"]) & set(b["layout"]))
    for key in keys:
        x, y, rot, side = a["layout"][key]
        xx, yy, rr, ss = b["layout"][key]
        value += (
            (x / a["width"] - xx / b["width"]) ** 2 + (y / a["height"] - yy / b["height"]) ** 2
        ) / max(1, len(keys))
        value += (min((rot - rr) % 360, (rr - rot) % 360) / 180) ** 2 / max(1, len(keys))
        value += (side != ss) / max(1, len(keys))
    return value


def portfolio(tier, limit, diverse=False):
    """Indices of eligible records, retaining rank 0 then farthest-first diversity.

    Operates before the search budget truncates the library; never resurrects
    failed records. Full native libraries remain unchanged on disk.
    """
    indices = [i for i, r in enumerate(tier) if eligible(r)]
    if not diverse:
        return indices[:limit]
    chosen = indices[:1]
    remaining = indices[1:]
    while remaining and len(chosen) < limit:
        pick = max(remaining, key=lambda i: (min(_distance(tier[i], tier[j]) for j in chosen), -i))
        chosen.append(pick)
        remaining.remove(pick)
    return chosen


def macro_clearance_violations(graph, constraints):
    """Validate hull clearance with the same occupancy contract as legalization.

    Hull covers already round the physical shapes out to grid cells. Growing
    those covers by another half-clearance can reject legal interlocks. Instead
    test the union of physical shapes grown by half-clearance and the original
    cover, using the existing conservative hull masks. Ordinary components and
    landing reservations retain their full authored spacing. Other hard rules
    and the separate expanded-board check remain unchanged.
    """
    from pnr.place import hull
    from pnr.place.compact import placement_clearance
    from pnr.place.geometry import MountedRect, Rect, ReserveRect, courtyard_rect, placement_rects
    from pnr.place.metrics import hard_violations

    clearance = placement_clearance(constraints)
    violations = hard_violations(graph, constraints, clearance=clearance)
    if not hull.active(graph.components) or not violations["overlaps"]:
        return violations
    comps = {c.ref: c for c in graph.components}
    occupied = {}

    def shapes(ref):
        if ref in occupied:
            return occupied[ref]
        comp = comps[ref]
        rects = placement_rects(comp)
        if not comp.hull:
            result = [(side, replace(r, w=r.w + clearance, h=r.h + clearance)) for side, r in rects]
        else:
            cr = courtyard_rect(comp)
            grid = float(comp.hull["grid_mm"])
            bw, bh = (int(math.ceil((size + clearance) / grid)) for size in (cr.w, cr.h))
            masks = hull.slot_masks(comp, grid, clearance, bw, bh)
            xs, ys = hull.lattice(bw, grid), hull.lattice(bh, grid)
            mount = next(
                (getattr(r, "mount", None) for _, r in rects if not isinstance(r, ReserveRect)),
                None,
            )
            result = []
            for side, mask in masks.items():
                for x0, y0, x1, y1 in hull.merge_cells(mask, xs, ys):
                    args = (
                        comp.pos[0] + (x0 + x1) / 2,
                        comp.pos[1] + (y0 + y1) / 2,
                        x1 - x0,
                        y1 - y0,
                    )
                    rect = Rect(*args) if mount is None else MountedRect(*args, mount=mount)
                    result.append((side, rect))
            result.extend(
                (side, replace(r, w=r.w + clearance, h=r.h + clearance))
                for side, r in rects
                if isinstance(r, ReserveRect)
            )
        occupied[ref] = result
        return result

    violations["overlaps"] = [
        (a, b)
        for a, b in violations["overlaps"]
        if not (comps[a].hull or comps[b].hull)
        or any(c.hull and not c.hull.get("shapes") for c in (comps[a], comps[b]))
        or any(sa == sb and ra.overlaps(rb) for sa, ra in shapes(a) for sb, rb in shapes(b))
    ]
    return violations


def search(graph, constraints, rules, library, seed, iters, pair_weights, geometry_of, config):
    from pnr.hier.blocks import extract_blocks
    from pnr.hier.top import _hierarchical_place, draw_layout
    from pnr.place.compact import placement_clearance
    from pnr.place.initial_pool import _prepared_source, preserve_source_locks
    from pnr.place.legalize import LegalizationError
    from pnr.place.metrics import hard_violations, hpwl

    constraints = preserve_source_locks(graph, constraints)
    source = _prepared_source(graph, constraints, rules)
    blocks = [b for b in extract_blocks(source, constraints) if b.name in library]
    groups = {}
    for b in blocks:
        groups.setdefault(b.template, []).append(b)
    ordered = [
        b for _, members in sorted(groups.items(), key=lambda kv: kv[1][0].name) for b in members
    ]
    rng = random.Random(seed)
    selection = {}
    for members in sorted(groups.values(), key=lambda members: members[0].name):
        rank, _ = draw_layout(library[members[0].name], rng)
        for b in members:
            selection[b.name] = rank
    # Independent sampling is an ablation, not an improvement-only policy.
    if config.method == "independent":
        rng = random.Random(seed)
        selection = {b.name: draw_layout(library[b.name], rng)[0] for b in ordered}
    pools = {b.name: portfolio(library[b.name], config.variants, config.diverse) for b in ordered}
    trials, accepted = [], []

    def evaluate(picks, warm=None, frozen=False, stage="initial"):
        if len(trials) >= config.max_evaluations:
            return None
        record = dict(index=len(trials), stage=stage, selection=dict(picks), frozen=frozen)
        trials.append(record)
        try:
            result = _hierarchical_place(
                source,
                constraints,
                rules,
                library,
                seed,
                iters if warm is None else config.relax_iters,
                pair_weights,
                geometry_of,
                selection=picks,
                warm=warm,
                frozen=frozen,
            )
            flat, report, meta = result
            violations = hard_violations(
                flat, constraints, clearance=placement_clearance(constraints)
            )
            internal = all(eligible(library[name][i]) for name, i in picks.items())
            legal = (
                report.legal
                and not any(violations.values())
                and internal
                and not any(meta["macro_clearance_violations"].values())
            )
            wire = hpwl(flat)
            if config.score == "power":
                from pnr.place.power_first import placement_quality
                from pnr.power_topology import derive

                cost = placement_quality(flat, derive(flat, constraints, rules))["q_place"]
            elif config.score == "objective":
                from pnr.place.cost_inspect import Objective

                _, weighted = Objective(flat, constraints).evaluate()
                cost = float(weighted.sum())
            else:
                cost = wire
            # Preserve routing-feedback attraction when supplied to parent placement.
            if pair_weights:
                from pnr.place.geometry import pin_positions

                pins = {(c.ref, p): xy for c in flat.components for p, xy in pin_positions(c)}
                cost += sum(
                    w * math.dist(pins[(a, ap)], pins[(b, bp)])
                    for (a, ap, b, bp), w in pair_weights.items()
                )
            if not math.isfinite(cost):
                raise ValueError("Non-finite contextual placement score")
            record.update(
                legal=legal,
                cost=cost,
                hpwl_mm=wire,
                violations=violations,
                internal_eligible=internal,
                macro_violations=meta["macro_clearance_violations"],
                placement_legal=bool(report.legal),
            )
            return result, cost if legal else math.inf, dict(picks)
        except (ValueError, KeyError, RuntimeError) as error:
            # A geometrically incompatible variant is rejected transactionally.
            record.update(legal=False, error=f"{type(error).__name__}: {error}")
            return None

    current = evaluate(selection)
    if current is None:
        # Preserve the original failure rather than inventing a usable placement.
        error = LegalizationError(
            "Initial hierarchical layout failed: " + trials[0].get("error", "unknown")
        )
        error.layout_search_trials = trials
        raise error
    initial = current

    def remember(candidate, stage):
        result, cost, picks = candidate
        accepted.append(
            dict(
                stage=stage,
                cost=cost if math.isfinite(cost) else None,
                selection=picks,
                graph=json.loads(result[0].to_json()),
            )
        )

    remember(current, "initial")
    if config.method not in ("independent",):
        for round_no in range(config.rounds):
            before = current
            warm = current[0][2]["macro_poses"]
            # Also evaluate the incumbent with the same relaxation budget. This
            # separates ordinary extra optimization from a useful variant swap.
            if config.method in ("alternating", "paired"):
                trial = evaluate(current[2], warm, stage="relax-incumbent")
                if trial is not None and trial[1] < current[1] - 1e-9:
                    current = trial
                    remember(current, "relax-incumbent")
            for b in ordered:
                winner = current
                warm = current[0][2]["macro_poses"]
                for index in pools[b.name]:
                    if len(trials) >= config.max_evaluations:
                        break
                    if index == current[2][b.name]:
                        continue
                    picks = dict(current[2], **{b.name: index})
                    trial = evaluate(picks, warm, frozen=config.method == "frozen", stage="swap")
                    if trial is not None and trial[1] < winner[1] - 1e-9:
                        winner = trial
                if winner is not current:
                    current = winner
                    remember(current, "swap")
            # A small two-block neighbourhood crosses minima where neither
            # one-block change is beneficial. Both variants change atomically.
            if config.method == "paired":
                winner = current
                for a, b in itertools.combinations(ordered, 2):
                    if len(trials) >= config.max_evaluations:
                        break
                    for ia, ib in itertools.product(pools[a.name], pools[b.name]):
                        if len(trials) >= config.max_evaluations:
                            break
                        if ia == current[2][a.name] or ib == current[2][b.name]:
                            continue
                        picks = dict(current[2], **{a.name: ia, b.name: ib})
                        trial = evaluate(picks, current[0][2]["macro_poses"], stage="paired-swap")
                        if trial is not None and trial[1] < winner[1] - 1e-9:
                            winner = trial
                if winner is not current:
                    current = winner
                    remember(current, "paired-swap")
            if current is before or len(trials) >= config.max_evaluations:
                break
    result, cost, picks = current
    if not math.isfinite(cost):
        error = LegalizationError("No eligible legal hierarchical layout within the search budget")
        error.layout_search_trials = trials
        raise error
    meta = result[2]
    meta["layout_search"] = dict(
        schema="pnr-contextual-layout-v1",
        config=asdict(config),
        seed=seed,
        input_sha256=hashlib.sha256(source.to_json().encode()).hexdigest(),
        library_sha256=record_id(library),
        initial_cost=initial[1] if math.isfinite(initial[1]) else None,
        final_cost=cost if math.isfinite(cost) else None,
        placement_legal=math.isfinite(cost),
        parent_routing_required=True,
        evaluations=len(trials),
        budget_exhausted=len(trials) >= config.max_evaluations,
        trials=trials,
        accepted=accepted,
        selected={name: record_id(library[name][i]) for name, i in picks.items()},
        limitation="Placement proxy only; parent copper and electrical evidence must be regenerated.",
    )
    return result
