"""Run the production placement/detail feedback API, no fixture-specific routes."""

import json
import os
import sys
import time
from pathlib import Path

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.fab_profile import apply_rules
from pnr.graph import BoardGraph
from pnr.route.feedback import route_and_place
from pnr.via_policy import board_policy

root = Path(sys.argv[1])
seed = int(sys.argv[2])
rounds = int(sys.argv[3])
spec = json.loads((root / "design.json").read_text())
g = BoardGraph.from_json((root / "source-graph.json").read_text())
c = compile_constraints(spec["constraints"], g.refs)
# Route under the fab profile writeback stamps and KiCad judges (PNR_FAB_PROFILE; legacy: unchanged).
rules = apply_rules(compile_routing_rules(c, [n.name for n in g.nets]))
# The rung's tool-neutral via policy, less what the board's own rules disallow, on
# its declared stack (pnr.via_policy); none for through vias only, as before.
via_policy = board_policy(spec.get("via_policy"), root / "source.kicad_pcb", rules)
if via_policy:
    rules["via_policy"] = via_policy
(root / "rules.json").write_text(json.dumps(rules, indent=2))
os.environ["PNR_ROUND_DIAGNOSTICS"] = str(root / "rounds")
t = time.monotonic()
g, report = route_and_place(
    g,
    c,
    seed=seed,
    iters=350,
    max_rounds=rounds,
    detail_rules=rules,
    detail_pitch_mm=0.25,
    detail_iters=8,
    spread=1.3,
)
(root / "placed.json").write_text(g.to_json())
r = report.detail_result
if r is None:
    raise RuntimeError("No detailed route produced")
routes = dict(tracks=r.tracks, vias=r.vias, unrouted=r.result.unrouted)
if getattr(r, "via_spans", None):
    routes["via_spans"] = r.via_spans  # blind, buried and micro vias (pnr.via_policy)
(root / "routes.json").write_text(json.dumps(routes, indent=2))
(root / "pnr-report.json").write_text(
    json.dumps(
        dict(
            converged=report.converged,
            legal=report.placement.legal,
            rounds=report.rounds,
            best_round=report.best_round,
            termination=report.termination,
            connection_history=report.connection_history,
            unrouted=r.result.unrouted,
            deferred=report.deferred_nets,
            initial_pool=getattr(report, "initial_pool", {}),
            escape_diagnostics=getattr(r, "escape_diagnostics", {}),
            elapsed_seconds=time.monotonic() - t,
            summary=report.summary(),
        ),
        indent=2,
    )
)
