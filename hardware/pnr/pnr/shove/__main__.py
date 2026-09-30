"""``python -m pnr.shove BOARD --rules R --target-json T --out-dir D --kicad-cli CLI
[--constraints C --placement-python PY --origin-board O]``.

Headless make-room transaction for one blocked power/plane target (see
:mod:`pnr.shove.ladder`). Writes D/result.json ({accepted, status, rung, rungs,
moved, nudges, ripped, restored, certificate, solid_blockers, solid_parts,
seconds}); when accepted also D/candidate.kicad_pcb (+ .kicad_pro, .kicad_dru,
fp-lib-table, candidate.drc.json) for native_loop's unchanged outer gate.
Requires PNR_SHOVE=1.
"""
import argparse
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('board', type=Path)
    ap.add_argument('--rules', type=Path, required=True)
    ap.add_argument('--target-json', type=Path, required=True)
    ap.add_argument('--out-dir', type=Path, required=True)
    ap.add_argument('--kicad-cli', required=True)
    ap.add_argument('--adapter', help='hardware/tools/keyhole_region.py (rung 4 restores)')
    ap.add_argument('--annotation-source', action='append', default=[], type=Path)
    ap.add_argument('--seconds', type=float, default=150)
    ap.add_argument('--pitch', type=float, default=.1)
    ap.add_argument('--bounds', type=float, nargs=4)
    ap.add_argument('--parts', default='', help='comma-separated nudgeable references (rung 2)')
    ap.add_argument('--rungs', default='1,2,4')
    ap.add_argument('--rip-radius', type=float, default=4.0)
    ap.add_argument('--constraints', type=Path,
                    help='the loop constraints.yaml: part nudges must add no hard placement violation')
    ap.add_argument('--placement-python',
                    help='PnR runtime python (with yaml) that runs pnr.shove.placement; without it and '
                         '--constraints no part may be nudged')
    ap.add_argument('--origin-board', type=Path,
                    help='board holding the flow ORIGIN placement; nudges are capped at 0.5 mm from it in total '
                         '(default: this transaction\'s own board)')
    a = ap.parse_args(argv)
    from pnr.shove import enabled
    if not enabled():
        ap.error('pnr.shove requires PNR_SHOVE=1')
    if '4' in a.rungs.split(',') and not a.adapter:
        ap.error('rung 4 requires --adapter')
    from pnr.shove.ladder import run
    return run(a)


if __name__ == '__main__':
    from pnr.profile import run as profiled
    profiled('shove', main)
