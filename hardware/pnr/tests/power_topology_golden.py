"""Regenerate the power-first derivation fixtures and goldens from the Mini inputs.

    PYTHONPATH=hardware/pnr python hardware/pnr/tests/power_topology_golden.py [inputs_dir]

For each block this writes ``testdata/power_topology/<name>.json`` (the block's
sub-board graph, its sub-board rules, its authored constraint document and the
compiled net classes the sub-board inherits from the parent board) and
``<name>.golden.json`` (the roles :func:`pnr.power_topology.derive` returns for
that fixture). Goldens are only ever produced by this script, never hand-edited;
tests/test_power_first.py checks derive() against them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve()
PNR = HERE.parents[1]
OUT = PNR / 'testdata' / 'power_topology'
BLOCKS = {'converter': ('board.converter', 30.0, 30.0), 'pd': ('board.pd', 30.0, 30.0)}


def compile_fixture(doc):
    """(graph, constraints, rules) of a stored fixture."""
    from pnr.constraints import NetClass, compile_constraints
    from pnr.graph import BoardGraph
    graph = BoardGraph.from_json(json.dumps(doc['graph']))
    constraints = compile_constraints(
        doc['constraints'], graph.refs, {c.address: c.ref for c in graph.components if c.address},
        {f"{c.address}:{p.name}": p.net for c in graph.components if c.address for p in c.pads if p.name})
    constraints.net_classes = [NetClass(**dict(nc, nets=tuple(nc['nets']))) for nc in doc['net_classes']]
    return graph, constraints, doc['rules']


def main(inputs=None):
    import dataclasses
    from pnr.hier.blocks import block_constraints_doc, extract_blocks, sub_board
    from pnr.mc.halving import _load
    from pnr.power_topology import derive
    inputs = Path(inputs) if inputs else HERE.parents[4] / 'inputs2'
    cons_path = PNR.parent / 'splanc_dev' / 'mini-constraints.yaml'
    graph, constraints, rules = _load(inputs, cons_path)
    blocks = {b.name: b for b in extract_blocks(graph, constraints)}
    doc0 = yaml.safe_load(cons_path.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    for key, (name, w, h) in BLOCKS.items():
        sg, sc, sr = sub_board(graph, constraints, rules, blocks[name], w, h)
        fixture = dict(source=dict(inputs=inputs.name, block=name, width=w, height=h),
                       graph=json.loads(sg.to_json()), rules=sr,
                       constraints=block_constraints_doc(doc0, [c.address for c in sg.components], w, h),
                       net_classes=[dataclasses.asdict(nc) for nc in sc.net_classes])
        g2, c2, r2 = compile_fixture(json.loads(json.dumps(fixture)))
        roles = derive(g2, c2, r2)
        if json.dumps(roles, sort_keys=True) != json.dumps(derive(sg, sc, sr), sort_keys=True):
            raise SystemExit(f'{name}: stored fixture does not reproduce the sub-board derivation')
        (OUT / f'{key}.json').write_text(json.dumps(fixture, indent=1, sort_keys=True) + '\n')
        (OUT / f'{key}.golden.json').write_text(json.dumps(roles, indent=1, sort_keys=True) + '\n')
        print(key, 'tier1', roles['tier1'], 'loops', [(l['labels'], l['hot']) for l in roles['loops']])


if __name__ == '__main__':
    main(*sys.argv[1:])
