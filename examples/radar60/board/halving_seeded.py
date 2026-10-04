"""``python -m pnr.mc.halving`` with U1's fanout plan seeded into every process's planner cache.

Placement derives the fanout's bottom sites (pnr.fanout.bottom.derive) in each worker process,
and each would plan U1's fanout again (several minutes per process). ``integrate.py prepare``
keeps its plan in ``WORK/inputs/fanout-<digest>.json`` keyed by the planner's own input digest
(:func:`pnr.fanout.planner.inputs_sha256`); this wrapper loads those files into
``pnr.fanout.planner._CACHE`` at import time, which runs in the parent and, through the spawn
start method, in every worker (they import this file as their main module). A plan whose inputs
differ has another digest and is planned as usual, so the result is the one the engine computes.

Usage: python halving_seeded.py HALVING_ARGS... (with RADAR60_FANOUT_CACHE=WORK/inputs)
"""

import json
import os
import sys
from pathlib import Path


def seed(folder):
    from pnr.fanout import planner

    n = 0
    for path in sorted(Path(folder).glob("fanout-*.json")):
        hit = json.loads(path.read_text())
        planner._CACHE.setdefault(hit["key"], hit["plan"])
        n += 1
    return n


if os.environ.get("RADAR60_FANOUT_CACHE"):
    seed(os.environ["RADAR60_FANOUT_CACHE"])

if __name__ == "__main__":
    from pnr.mc import halving

    sys.exit(halving.main(sys.argv[1:]))
