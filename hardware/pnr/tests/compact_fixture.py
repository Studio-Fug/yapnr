"""Ladder fixtures and the flag-off identity digests of the compact-placement tests.

``testdata/compact/<case>.json`` holds a ladder case's source graph (ingested by the
current ingest, so it carries ``Component.body``), its constraints and side policy:
04-inverter-leds-8 and 07-chaser-20 of the ladder, and the hard rung
09-mcu-usb-31-header (a 1x8 pin header whose origin is pin 1).

:func:`legal_digest` and :func:`place_digest` use only APIs that exist before
PNR_COMPACT, so the goldens in ``testdata/compact/identity.json`` were produced by
running them on the parent commit's tree (``python compact_fixture.py`` with that tree
on ``PYTHONPATH``); with the flags unset the current tree must reproduce them.
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "testdata" / "compact"
CASES = ("04-inverter-leds-8", "07-chaser-20")


def load(case):
    """``(graph, constraints, rules)`` of a fixture case."""
    from pnr.constraints import compile_constraints, compile_routing_rules
    from pnr.fab_profile import apply_rules
    from pnr.graph import BoardGraph
    from pnr.place.sides import with_policy

    doc = json.loads((DATA / (case + ".json")).read_text())
    graph = BoardGraph.from_json(json.dumps(doc["graph"]))
    constraints = compile_constraints(with_policy(doc["constraints"], doc.get("sides")), graph.refs)
    rules = apply_rules(compile_routing_rules(constraints, [n.name for n in graph.nets]))
    return graph, constraints, rules


def platform_key():
    return "%s-%s" % (sys.platform, platform.machine().lower())


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def legal_digest(case):
    """sha256 of the production legalizer's output (numpy only, platform independent) on
    seeded targets, plus the initial pool's explicit starts and the fixed poses."""
    from pnr.place.channels import ChannelModel
    from pnr.place.geometry import outline_size, resolve_fixed_poses
    from pnr.place.initial_pool import InitialPoolConfig, initial_starts
    from pnr.place.legalize import legalize, legalize_constraint_kwargs, pad_edge_rule

    graph, constraints, rules = load(case)
    width, height = outline_size(graph, constraints)
    rng = random.Random(7)
    targets = graph.__class__.from_json(graph.to_json())
    for comp in sorted(targets.components, key=lambda c: c.ref):
        comp.pos = (rng.uniform(0, width), rng.uniform(0, height))
        comp.rot = 90.0 * rng.randrange(4)
    poses = resolve_fixed_poses(graph, constraints)
    placed = legalize(
        targets,
        width,
        height,
        allow_rotation=True,
        channel_model=ChannelModel(targets, rules),
        clearance=float(constraints.board.default_clearance_mm),
        grid_mm=0.25,
        spread=1.3,
        **legalize_constraint_kwargs(graph, constraints, poses, pad_edge_rule(constraints, rules)),
    )
    starts = initial_starts(graph, constraints, InitialPoolConfig(), seed=0, orient=True)
    return _digest(
        json.dumps(
            dict(
                placed=json.loads(placed.to_json()),
                starts=starts,
                fixed={k: list(v) for k, v in sorted(poses.items())},
            ),
            sort_keys=True,
        )
    )


def place_digest(case):
    """sha256 of :func:`pnr.place.placer.place` (global placement included: torch, so the
    golden is per platform) at the ladder's spread."""
    from pnr.place.placer import place

    graph, constraints, rules = load(case)
    placed, report = place(graph, constraints, seed=0, iters=120, spread=1.3, channel_rules=rules)
    return _digest(placed.to_json() + report.summary())


def main():
    out = {}
    for case in CASES:
        out[case] = dict(legal=legal_digest(case), place={platform_key(): place_digest(case)})
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
