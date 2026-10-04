"""BGA fanout generator (E4): declared fanouts of area-array parts, planned exactly.

A ``fanout:`` constraint entry (:mod:`.spec`) names a part. :func:`.planner.plan`
infers its ball lattice (:mod:`.lattice`), builds the copper objects it may use
(:mod:`.sites`), assigns every fanned-out pad jointly (:mod:`.assign`): signal
balls escape on the surface or through a dog-bone via to an escape layer, plane
balls drop a via of their class; and emits the ``fanout.json`` artifact: the
copper, every pad's terminal (where the router takes over) and diagnostics.
:mod:`pnr.route.detail.fanout` hands the plan to the detailed router;
``python -m pnr.fanout`` plans and verifies a board (:mod:`.verify`, KiCad).
"""

from .planner import SCHEMA, cached_plan, classify, inputs_sha256, plan, plan_layers
from .spec import FanoutError, check, class_for, expand_nets, parse, parse_all

__all__ = [
    "SCHEMA",
    "FanoutError",
    "cached_plan",
    "check",
    "class_for",
    "classify",
    "expand_nets",
    "inputs_sha256",
    "parse",
    "parse_all",
    "plan",
    "plan_layers",
]
