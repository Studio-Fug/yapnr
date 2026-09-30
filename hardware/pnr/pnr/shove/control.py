"""Controller-side helpers for the PNR_SHOVE=1 hook in pnr.native_loop.

Runs in the PnR runtime (no pcbnew). The hook fires after a power/plane route
failed and electrical_blocker_repair did not rescue it; the transaction itself is
the ``python -m pnr.shove`` KiCad worker, whose candidate then passes
native_loop's unchanged outer gate (worker 'check' + native DRC + strict gate).
"""

import math

SHOVE_STATUSES = (
    "no_current_sized_channel",
    "no_surface_channel",
    "no_qualified_power_access",
    "time_budget",
)


class ShoveBudget:
    """At most one transaction per (board, job) and ``limit`` per loop."""

    def __init__(self, limit):
        self.limit = limit
        self.tried = set()
        self.count = 0
        self.accepted = 0

    def reserve(self, board_hash, key):
        identity = (board_hash, tuple(key))
        if identity in self.tried or self.count >= self.limit:
            return False
        self.tried.add(identity)
        self.count += 1
        return True

    def record(self, outcome):
        self.accepted += int(bool(outcome.get("accepted")))

    def summary(self):
        return dict(limit=self.limit, used=self.count, accepted=self.accepted)


def eligible(target, outcome, enabled, electrical, placement_trial, remaining):
    return bool(
        enabled
        and electrical
        and not placement_trial
        and target.get("mode") in ("power", "plane")
        and not outcome.get("accepted")
        and outcome.get("status") in SHOVE_STATUSES
        and remaining > 90
    )


def nudge_candidates(inventory, constraints_path, points, radius=3.0, max_pads=4, exclude=()):
    """Footprints the make-room model may nudge near ``points`` (native mm):
    small (<= ``max_pads`` pads), not locked (physical, pair/length-match or
    source-intent protection from the inspect worker), not fixed by constraints,
    and not in ``exclude``. An endpoint's own small part may move: the claim's
    attach inside its land rides with it."""
    import json
    from pathlib import Path
    import yaml
    from pnr.graph import BoardGraph
    from pnr.constraints import compile_constraints
    from pnr.place.geometry import resolve_fixed_poses

    g = BoardGraph.from_json(json.dumps(inventory["graph"]))
    try:
        cc = compile_constraints(
            yaml.safe_load(Path(constraints_path).read_text()),
            g.refs,
            {c.address: c.ref for c in g.components},
            {f"{c.address}:{p.name}": p.net for c in g.components for p in c.pads},
        )
        fixed = set(resolve_fixed_poses(g, cc))
        holes = {h["name"] for h in cc.mounting_holes}
    except Exception:
        return []
    physical = set(inventory.get("physical_locks", []))
    poses = inventory.get("footprint_poses", {})
    out = []
    for c in g.components:
        if (
            c.locked
            or c.ref in fixed
            or c.ref in holes
            or c.ref in physical
            or c.ref in exclude
            or len(c.pads) > max_pads
        ):
            continue
        pose = poses.get(c.ref)
        if pose is None:
            continue
        d = min(math.dist(pose, p) for p in points)
        if d <= radius:
            out.append((d, c.ref))
    return [ref for _, ref in sorted(out)]


def merge_failure(outcome, shove, owners):
    """Fold a failed transaction's certificate into the route outcome, so the
    loop's failure history and component scores name the real obstacle."""
    blockers = dict(outcome.get("static_blockers") or {})
    for identity, weight in (shove.get("solid_blockers") or {}).items():
        blockers[identity] = blockers.get(identity, 0) + max(1, round(10 * weight))
    by_ref = {}
    for identity, ref in owners.items():
        by_ref.setdefault(ref, identity)
    for ref in shove.get("solid_parts") or []:
        if ref in by_ref:
            blockers[by_ref[ref]] = blockers.get(by_ref[ref], 0) + 10
    outcome = dict(outcome, static_blockers=blockers)
    return outcome


def event(target, shove, folder, budget):
    rungs = [(r.get("rung"), r.get("status")) for r in shove.get("rungs", [])]
    return dict(
        stage="shove_repair",
        target=target,
        status=shove.get("status"),
        accepted=shove.get("accepted", False),
        rung=shove.get("rung"),
        rungs=rungs,
        moved=len(shove.get("moved", [])),
        max_disp_mm=max([m.get("disp_mm", 0) for m in shove.get("moved", [])] + [0]),
        nudges=shove.get("nudges", []),
        ripped=shove.get("ripped", []),
        restored=[
            (r["net"], r["source"], r["target"], r["accepted"]) for r in shove.get("restored", [])
        ],
        certificate=[
            (c.get("a_label"), c.get("b_label"), c.get("multiplier"))
            for c in shove.get("certificate", [])[:5]
        ],
        solid_parts=shove.get("solid_parts", []),
        folder=str(folder),
        budget=budget.summary(),
        seconds=shove.get("seconds"),
    )


def feedback_section(loop_dir):
    """Summary of every ``shove_repair`` event under a native_loop output folder
    (the top loop and its nested early-power/plane loops) for feedback.json."""
    import json
    from collections import Counter
    from pathlib import Path

    events = []
    for progress in sorted(Path(loop_dir).glob("**/progress.json")):
        try:
            data = json.loads(progress.read_text())
        except (OSError, ValueError):
            continue
        for e in data.get("events", []):
            if e.get("stage") == "shove_repair":
                events.append(
                    dict(
                        e,
                        loop=(
                            str(progress.parent.relative_to(loop_dir))
                            if progress.parent != Path(loop_dir)
                            else "."
                        ),
                    )
                )
    parts = Counter(
        ref for e in events if not e.get("accepted") for ref in e.get("solid_parts", [])
    )
    return dict(
        transactions=len(events),
        accepted=sum(1 for e in events if e.get("accepted")),
        nudges=[n for e in events if e.get("accepted") for n in e.get("nudges", [])],
        ripped=[r for e in events if e.get("accepted") for r in e.get("ripped", [])],
        certificate_parts=dict(parts.most_common()),
        events=[
            {
                k: e.get(k)
                for k in (
                    "loop",
                    "target",
                    "status",
                    "accepted",
                    "rung",
                    "rungs",
                    "moved",
                    "max_disp_mm",
                    "nudges",
                    "ripped",
                    "solid_parts",
                    "folder",
                )
            }
            for e in events
        ],
    )
