"""Monte-Carlo successive-halving driver for a ladder rung (``driver: mc``).

The engine's own search (``pnr.mc.halving``) runs its two open stages: N independent
global starts, each legalized and ranked by the capacity proxy (stage 0), then the
best K1 plus seeded uniform controls screened by a short detailed route (stage 1).
Its native stages are not used: they evaluate a candidate with ``pnr.full_iteration``
under a design's electrical annotation files, which an open ladder board does not
have. Instead the K2 best screens (halving's own screen ranking) are routed with the
ladder's detailed-route budget (``route_case.py``: 0.25 mm pitch, 8 iterations), and
the best by the initial pool's route objective (missing connections, unresolved nets,
vias, copper; then the candidate id) is kept. Every choice is mechanical.

Writes the same outputs as ``route_case.py`` (rules.json, placed.json, routes.json,
pnr-report.json) plus ``mc/`` (halving's dataset and status) and ``mc-summary.json``,
so the runner's native stages and gate run unchanged.

    python mc_case.py ROOT SEED ROUNDS   (ROUNDS is unused: the search has its own budget)
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.fab_profile import apply_rules
from pnr.graph import BoardGraph
from pnr.length_model import attach_board
from pnr.mc.halving import _rank_key
from pnr.place.initial_pool import _route_metrics, route_rank
from pnr.place.metrics import hpwl
from pnr.place.sides import plan as side_plan
from pnr.place.sides import report as sides_report
from pnr.place.sides import with_policy
from pnr.route.detail.exact_route import exact_mode
from pnr.route.detail.native_maze import status as maze_status
from pnr.route.detail.router import route_board
from pnr.via_policy import board_policy

root = Path(sys.argv[1]).resolve()
seed = int(sys.argv[2])
spec = json.loads((root / "design.json").read_text())
mc = spec["mc"]
graph = BoardGraph.from_json((root / "source-graph.json").read_text())
# The rung's side policy (``sides: double``) as the engine's ``board.sides``, for this
# driver and for halving's workers (constraints.yaml), as route_case.py maps it.
doc = with_policy(spec["constraints"], spec.get("sides"))
constraints = compile_constraints(doc, graph.refs)
rules = apply_rules(compile_routing_rules(constraints, [n.name for n in graph.nets]))
# As route_case.py: the rung's via policy on its board (none: through vias only).
via_policy = board_policy(spec.get("via_policy"), root / "source.kicad_pcb", rules, graph=graph)
if via_policy:
    rules["via_policy"] = via_policy
# Pairs and groups are tuned against the board's own stackup (via lengths) and the
# exact lands of their pads.
attach_board(rules, (root / "source.kicad_pcb").read_text())
(root / "rules.json").write_text(json.dumps(rules, indent=2))

inputs = root / "mc-inputs"
inputs.mkdir(exist_ok=True)
(inputs / "graph.json").write_text((root / "source-graph.json").read_text())
(inputs / "rules.json").write_text(json.dumps(rules, indent=2))
(inputs / "constraints.yaml").write_text(yaml.safe_dump(doc, sort_keys=True))
out = root / "mc"
started = time.monotonic()
cmd = [
    sys.executable,
    "-m",
    "pnr.mc.halving",
    "--out",
    str(out),
    "--inputs",
    str(inputs),
    "--constraints",
    str(inputs / "constraints.yaml"),
    "--repo",
    str(root),
    "--seed",
    str(seed),
    "--n0",
    str(mc["n0"]),
    "--k1",
    str(mc["k1"]),
    "--control",
    str(mc["control"]),
    "--iters",
    str(mc["iters"]),
    "--route-iters",
    str(mc["route_iters"]),
    "--procs",
    str(mc["procs"]),
    "--stop-after",
    "screen",
]
with (root / "mc-halving.log").open("w") as log:
    subprocess.run(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=float(mc.get("timeout", 7200)),
    )
search_seconds = time.monotonic() - started
records = [
    json.loads(line) for line in (out / "dataset.jsonl").read_text().splitlines() if line.strip()
]
placed = {r["id"]: r for r in records if r.get("stage") == "place"}
screens = [r for r in records if r.get("stage") == "screen" and r.get("status") == "ok"]
if not screens:
    raise RuntimeError("successive halving screened no legal placement")
finalists = sorted(screens, key=_rank_key("screen"))[: mc["k2"]]

evaluated = []
for rec in finalists:
    t = time.monotonic()
    cand = BoardGraph.from_json((out / "cand" / rec["id"] / "placed.json").read_text())
    route = route_board(
        cand, constraints, rules, pitch=mc.get("pitch_mm", 0.25), max_iters=mc["final_iters"]
    )
    metrics = _route_metrics(route)
    evaluated.append(
        dict(
            id=rec["id"],
            control=bool(rec.get("control")),
            proxy_score=placed[rec["id"]].get("proxy_score"),
            screen_objective=rec.get("objective"),
            metrics=metrics,
            hpwl_mm=hpwl(cand),
            seconds=time.monotonic() - t,
            graph=cand,
            route=route,
        )
    )
    print("final", rec["id"], metrics["objective"], "%.1fs" % (time.monotonic() - t), flush=True)
best = min(evaluated, key=lambda c: (route_rank(c["metrics"]), c["id"]))
route = best["route"]
(root / "placed.json").write_text(best["graph"].to_json())
routes = dict(tracks=route.tracks, vias=route.vias, unrouted=route.result.unrouted)
if getattr(route, "via_spans", None):
    routes["via_spans"] = route.via_spans
routes.update(
    getattr(route, "extras", dict)()
)  # a declared fanout's via sizes and locked copper (pnr.fanout)
(root / "routes.json").write_text(json.dumps(routes, indent=2))
status = json.loads((out / "status.json").read_text())
legal = [r for r in placed.values() if r.get("status") == "legal"]
summary = dict(
    schema="ladder-mc-v1",
    config=mc,
    seed=seed,
    starts=len(placed),
    legal=len(legal),
    illegal=sum(r.get("status") == "illegal" for r in placed.values()),
    failed=sum(r.get("status") == "failed" for r in placed.values()),
    hpwl_mm=dict(
        best_start=min((r["hpwl_mm"] for r in legal), default=None),
        median_start=sorted(r["hpwl_mm"] for r in legal)[len(legal) // 2] if legal else None,
        selected=best["hpwl_mm"],
    ),
    screen=status["stages"].get("screen"),
    finalists=[
        {k: v for k, v in c.items() if k not in ("graph", "route")}
        | dict(metrics={k: v for k, v in c["metrics"].items()})
        for c in evaluated
    ],
    selected=best["id"],
    search_seconds=search_seconds,
)
(root / "mc-summary.json").write_text(json.dumps(summary, indent=2))
unresolved = sorted(set(route.result.unrouted) - set(route.deferred_nets))
(root / "pnr-report.json").write_text(
    json.dumps(
        dict(
            converged=not route.result.unrouted and not route.deferred_nets,
            legal=True,  # halving keeps only legal placements (hard and source checks)
            rounds=1,
            best_round=1,
            termination="mc_successive_halving",
            connection_history=[best["metrics"]["missing_connections"]],
            unrouted=route.result.unrouted,
            deferred=sorted(route.deferred_nets),
            unresolved=unresolved,
            mc=dict(selected=best["id"], finalists=[c["id"] for c in evaluated]),
            sides=sides_report(best["graph"], side_plan(graph, constraints, rules)),
            escape_diagnostics=getattr(route, "escape_diagnostics", {}),
            maze_kernel=maze_status(),
            exact_separation=exact_mode(),
            elapsed_seconds=time.monotonic() - started,
            summary=route.summary(),
            # Pair / group length tuning (pnr.route.detail.tune), only when declared.
            **(
                {"length_tuning": route.length_report}
                if getattr(route, "length_report", None) is not None
                else {}
            ),
        ),
        indent=2,
    )
)
