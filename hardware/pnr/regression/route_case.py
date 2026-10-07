"""Run the production placement/detail feedback API, no fixture-specific routes."""

import json
import os
import sys
import time
from pathlib import Path

from pnr.board_edge import attach_edges
from pnr.constraints import compile_constraints, compile_routing_rules, infer_series_line_groups
from pnr.dru_rules import attach_dru
from pnr.fab_profile import apply_rules
from pnr.graph import BoardGraph
from pnr.length_model import attach_board
from pnr.place.route_compact import arm_deadline
from pnr.place.sides import plan as side_plan
from pnr.place.sides import report as sides_report
from pnr.place.sides import with_policy
from pnr.profile import run as profiled
from pnr.route.detail.exact_route import exact_mode
from pnr.route.detail.native_maze import status as maze_status
from pnr.route.feedback import route_and_place
from pnr.via_policy import board_policy

# PNR_ROUTE_COMPACT: the post-route pass keeps to the runner's stage budget from here.
arm_deadline()
root = Path(sys.argv[1])
seed = int(sys.argv[2])
rounds = int(sys.argv[3])
spec = json.loads((root / "design.json").read_text())
g = BoardGraph.from_json((root / "source-graph.json").read_text())
# The rung's tool-neutral side policy (``sides``) as the engine's ``board.sides``.
cons = with_policy(spec["constraints"], spec.get("sides"))
# A declared diff_pair's two series parts (same footprint, one per leg), where the
# design leaves them otherwise unconstrained: held side by side (pnr.constraints).
cons = infer_series_line_groups(cons, g)
c = compile_constraints(cons, g.refs)
# Route under the fab profile writeback stamps and KiCad judges (PNR_FAB_PROFILE; legacy: unchanged).
rules = apply_rules(compile_routing_rules(c, [n.name for n in g.nets]))
# ladder-v2 A/B (ab-pairs-pool): PNR_FORCE_ROUTE_PAIRS_FOR_DIFF_PAIRS=1 turns on coupled
# pair routing (board.route_pairs: coupled) for any design that declares a diff_pair,
# without editing each case; off (default) leaves the case's own declaration alone.
if os.environ.get("PNR_FORCE_ROUTE_PAIRS_FOR_DIFF_PAIRS") == "1" and rules.get("diff_pairs"):
    rules["route_pairs"] = "coupled"
# The rung's tool-neutral via policy, less what the board's own rules disallow, on
# its declared stack, with the build (drill pairs) its parts need (pnr.via_policy);
# none for through vias only, as before.
via_policy = board_policy(spec.get("via_policy"), root / "source.kicad_pcb", rules, graph=g)
if via_policy:
    rules["via_policy"] = via_policy
# A rung's fixed block (hard_rungs, native.py make): its footprints leave the
# placement graph and its copper rides in the rules, so every route of the loop
# reserves it (pnr.fixed_block).
if spec.get("fixed_block"):
    from pnr.fixed_block import block_refs, hold_out

    fixed = json.loads((root / "source-fixed.json").read_text())
    hold_out(g, block_refs(rules.get("fixed_blocks"), fixed))
    rules["fixed_copper"] = fixed
# Pairs and groups are tuned against the board's own stackup (via lengths) and the
# exact lands of their pads.
source_text = (root / "source.kicad_pcb").read_text()
attach_board(rules, source_text)
# board.edge: exact: the source outline (arcs, stroke) for the router (pnr.board_edge);
# board.dru_routing: the custom rules beside the board where they constrain routing
# (pnr.dru_rules). Nothing otherwise.
attach_edges(rules, source_text)
dru_path = root / "source.kicad_dru"
attach_dru(rules, dru_path.read_text() if dru_path.exists() else None, [n.name for n in g.nets])
(root / "rules.json").write_text(json.dumps(rules, indent=2))
os.environ["PNR_ROUND_DIAGNOSTICS"] = str(root / "rounds")
t = time.monotonic()
# PNR_PROFILE_DIR (yapnr exp [profile]): the place-route loop under pnr.profile, the ladder's
# largest stage; without it this is a plain call.
g, report = profiled(
    "route-case",
    lambda: route_and_place(
        g,
        c,
        seed=seed,
        iters=350,
        max_rounds=rounds,
        detail_rules=rules,
        detail_pitch_mm=0.25,
        detail_iters=8,
        spread=1.3,
    ),
)
(root / "placed.json").write_text(g.to_json())
plan = side_plan(BoardGraph.from_json((root / "source-graph.json").read_text()), c, rules)
r = report.detail_result
if r is None:
    raise RuntimeError("No detailed route produced")
routes = dict(tracks=r.tracks, vias=r.vias, unrouted=r.result.unrouted)
if getattr(r, "via_spans", None):
    routes["via_spans"] = r.via_spans  # blind, buried and micro vias (pnr.via_policy)
routes.update(
    getattr(r, "extras", dict)()
)  # a declared fanout's via sizes and locked copper (pnr.fanout)
(root / "routes.json").write_text(json.dumps(routes, indent=2))
if getattr(r, "traced_rails", None):
    # plane_partition candidates the allocation traced (pnr.rail_alloc): ordinary
    # nets for writeback, the planes stage and the checks, as they were for routing.
    from pnr.rail_alloc import routed_rules

    rules = routed_rules(rules, r.traced_rails)
    (root / "rules.json").write_text(json.dumps(rules, indent=2))
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
            sides=sides_report(g, plan),
            escape_diagnostics=getattr(r, "escape_diagnostics", {}),
            maze_kernel=maze_status(),
            exact_separation=exact_mode(),
            elapsed_seconds=time.monotonic() - t,
            summary=report.summary(),
            # Pair / group length tuning (pnr.route.detail.tune), only when declared.
            **({"length_tuning": r.length_report} if r.length_report is not None else {}),
            # PNR_SHRINK only (absent otherwise): the outline search and its choice.
            **({"shrink": report.shrink} if report.shrink is not None else {}),
            # PNR_COMPACT RELAX only (absent otherwise): the rounds placed relaxed.
            **({"relaxed": report.relaxed} if getattr(report, "relaxed", None) else {}),
            # PNR_ROUTE_COMPACT FLAT only (absent otherwise): the post-route compaction.
            **(
                {"route_compact": dict(flat=report.route_compact)}
                if getattr(report, "route_compact", None)
                else {}
            ),
        ),
        indent=2,
    )
)
