#!/usr/bin/env python3
"""One RF job inside a cloud task: a preset case, with its spec replaced or adjusted.

    python job/rf_job.py CASE --out out/JOB [--spec job/specs/JOB.yaml] [--seed NAME|none]
                         [--threads N] [CASES-RUN-OPTIONS...]

Runs what ``python -m yapnr.rf.cases run CASE --out out/JOB`` runs (optimize, export, and
re-validate with the case's criteria; a run directory with a checkpoint resumes), after
replacing the case's preset spec by ``--spec`` and setting ``optimizer.seed`` (the starting
design; ``none`` for the uniform start) and ``solver.threads``. Other options (``--smoke``,
``--max-iterations``, ``--no-fine``, ...) go to the cases runner unchanged. ``yapnr.rf`` comes
from the source bundle on ``PYTHONPATH``; this file only needs the standard library.

Written by tools/exp/rf_stage_plan.py into the job bundle of an ``mc-eval`` stage plan.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rf_job.py", description=__doc__.split("\n")[0])
    ap.add_argument("case", help="the preset case: its criteria and frequencies judge the run")
    ap.add_argument("--out", required=True, help="run directory")
    ap.add_argument("--spec", help="a spec file (YAML or JSON) to run instead of the preset")
    ap.add_argument("--seed", help="optimizer.seed, the starting design ('none': uniform)")
    ap.add_argument("--threads", type=int, help="solver.threads")
    args, rest = ap.parse_known_args(argv)

    from yapnr.rf import cases
    from yapnr.rf.spec import Spec

    preset = cases.spec_for

    def spec_for(case: str, scale: str = "full") -> Spec:
        spec = Spec.load(args.spec) if args.spec else preset(case, scale)
        if args.seed is not None:
            seed = None if args.seed == "none" else args.seed
            spec = dataclasses.replace(
                spec, optimizer=dataclasses.replace(spec.optimizer, seed=seed)
            )
        if args.threads is not None:
            spec = dataclasses.replace(
                spec, solver=dataclasses.replace(spec.solver, threads=args.threads)
            )
        return spec

    # cases.run looks spec_for up at call time; the validator reads the run directory's spec.
    cases.spec_for = spec_for
    return cases.main(["run", args.case, "--out", args.out] + rest)


if __name__ == "__main__":
    sys.exit(main())
