"""Evaluate a block layout with the full native electrical pipeline on its own sub-board."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

KI_PY = '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3'
PNR_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = Path(__file__).resolve().parents[4]


def edge_clearance(rules: dict) -> float:
    """Copper-to-Edge.Cuts clearance the sub-board is routed and checked with.

    Same default as pnr.writeback._fab and the native oracle.
    """
    return float((rules.get('fab') or {}).get('edge_clearance_mm', .2))


def with_apron(sub_graph, apron: float):
    """Copy of ``sub_graph`` on an outline grown by ``apron`` on every side.

    Components shift by +apron, so the placement keeps its original block
    rectangle ``[apron, apron+w] x [apron, apron+h]`` while ``Edge.Cuts`` (stamped
    by pnr.writeback at the outline) leaves room for edge clearance. Poses stay
    rigid, so pnr.hier.assemble (transform solved from footprint poses) is unaffected.
    Routed copper is bounded only by Edge.Cuts; see :func:`evaluate` for the
    apron limit that keeps it inside the block rectangle.
    """
    from pnr.graph import BoardOutline
    if apron < 0:
        raise ValueError('negative sub-board apron')
    g = copy.deepcopy(sub_graph)
    for c in g.components:
        c.pos = (c.pos[0] + apron, c.pos[1] + apron)
    # Sub-board outlines are rectangles (blocks.sub_board); a polygon would stay tight.
    g.outline = BoardOutline(g.outline.width + 2 * apron, g.outline.height + 2 * apron)
    return g


def evaluate(round_dir: Path, inputs: Path, constraints_doc: dict, sub_graph, sub_rules: dict,
             seconds: int, workers: int, repo: Path, live_lane: str = '', apron: float | None = None) -> dict:
    """Run pnr.full_iteration on the block alone; return objective summary.

    ``apron`` (mm, default PNR_SUBBOARD_APRON_MM, else the sub-board fab
    ``edge_clearance_mm``) grows the sub-board outline around the block rectangle;
    see :func:`with_apron`. Pads at the block edge then meet the edge clearance.
    The routers keep track/via copper that clearance from Edge.Cuts and the final
    DRC counts any closer copper as a copper_edge_clearance violation, so with an
    apron no wider than it routed copper stays inside the block rectangle (the
    region pnr.hier.assemble copies into and the top level reserves) or is
    penalised. A wider apron would let copper out and enlarge the routing area,
    so it is refused.
    """
    t = time.monotonic()
    edge = edge_clearance(sub_rules)
    if apron is None:
        apron = float(os.environ.get('PNR_SUBBOARD_APRON_MM', edge))
    if apron > edge + 1e-9:
        raise ValueError('sub-board apron %g mm exceeds the %g mm edge clearance: '
                         'routed copper could leave the block rectangle' % (apron, edge))
    placed = with_apron(sub_graph, apron)
    constraints_doc = copy.deepcopy(constraints_doc)
    # The constraint outline is the placement region native phases check against.
    constraints_doc.setdefault('board', {})['outline'] = {'w': placed.outline.width, 'h': placed.outline.height}
    round_dir = Path(round_dir)
    if round_dir.exists():
        shutil.rmtree(round_dir)
    round_dir.mkdir(parents=True)
    refs = [c.ref for c in sub_graph.components]
    (round_dir / 'keep.json').write_text(json.dumps(refs))
    env = dict(os.environ, PYTHONPATH=str(PNR_ROOT), PNR_SUBBOARD='1')
    subprocess.run([KI_PY, '-m', 'pnr.hier.subpcb', str(inputs / 'source.kicad_pcb'),
                    str(round_dir / 'source.kicad_pcb'), '--keep', str(round_dir / 'keep.json')],
                   env=env, check=True, capture_output=True, text=True)
    for name in ('source.kicad_pro', 'fp-lib-table'):
        shutil.copy2(inputs / name, round_dir / name)
    (round_dir / 'rules.json').write_text(json.dumps(sub_rules, indent=1, sort_keys=True))
    (round_dir / 'placed.json').write_text(placed.to_json())
    (round_dir / 'constraints.yaml').write_text(yaml.safe_dump(constraints_doc, sort_keys=False))
    dev = SRC_ROOT / 'hardware/splanc_dev'
    cmd = [sys.executable, '-m', 'pnr.full_iteration', str(round_dir),
           '--constraints', str(round_dir / 'constraints.yaml'),
           '--electrical-fab', str(dev / 'mini-routing-electrical-fab.json'),
           '--plane-fab', str(dev / 'mini-plane-access-fab.json'),
           '--annotation-source', str(dev / 'elec/src/splanc_mini.ato'),
           '--seconds', str(seconds)]
    run_env = dict(env, PNR_SINGLE_TRACK_WORKERS=str(workers), PNR_SUBBOARD='1')
    if live_lane:
        run_env['PNR_LIVE_CANDIDATE'] = live_lane
    with (round_dir / 'run.log').open('w') as log:
        code = subprocess.run(cmd, cwd=repo, env=run_env, stdout=log, stderr=subprocess.STDOUT).returncode
    rec = dict(exit_code=code, seconds=time.monotonic() - t, apron_mm=apron)
    ev = round_dir / 'evaluation.json'
    if ev.exists():
        e = json.loads(ev.read_text())
        rec.update(status='ok', objective=e['objective'], opens=e['objective'][5],
                   violations=e['objective'][0], subwidth=e['objective'][3])
        if os.environ.get('PNR_POWER_FIRST') == '1':
            # Routed power-path quality: one KiCad read of the final board, checked
            # against the final DRC. A failed analysis leaves hot_loops_open unset,
            # which ranks as worst (never helps a layout).
            from pnr.hier.power_quality import evaluate as power_quality
            try:
                pq = power_quality(round_dir, sub_graph, sub_rules)
            except Exception as error:
                pq = dict(valid=False, error=repr(error), hot_loops_open=None)
            (round_dir / 'power-quality.json').write_text(json.dumps(pq, indent=1))
            rec.update(power_quality=pq, hot_loops_open=pq.get('hot_loops_open'))
    else:
        rec['status'] = 'failed'
        rec['log_tail'] = (round_dir / 'run.log').read_text()[-1500:]
    return rec
