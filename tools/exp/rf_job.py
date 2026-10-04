#!/usr/bin/env python3
"""One RF job inside a cloud task: a preset case, with its spec replaced or adjusted.

    python job/rf_job.py CASE --out out/JOB [--spec job/specs/JOB.yaml] [--seed NAME|none]
                         [--threads N] [--criteria job/criteria/JOB.yaml]
                         [--attempt-s S [--end-s E]] [CASES-RUN-OPTIONS...]
    python job/rf_job.py CASE --out out/JOB --validate-from run [--criteria FILE]
                         [CASES-VALIDATE-OPTIONS...]

Runs what ``python -m yapnr.rf.cases run CASE --out out/JOB`` runs (optimize, export, and
re-validate with the case's criteria; a run directory with a checkpoint resumes), after
replacing the case's preset spec by ``--spec`` and setting ``optimizer.seed`` (the starting
design; ``none`` for the uniform start) and ``solver.threads``. Other options (``--smoke``,
``--max-iterations``, ``--no-fine``, ``--finer``...) go to the cases runner unchanged.

``--criteria`` replaces the case's pass criteria and dense re-validation frequencies (for a spec
in another band than the case's, which the case's criteria would judge at the wrong
frequencies): a JSON or YAML mapping of ``dense_ghz`` (frequencies in GHz, or ``[start, stop,
points]`` ranges) and the ``coarse`` and ``fine`` checks as ``python -m yapnr.rf.cases criteria
CASE`` prints them.

``--attempt-s`` is the time this attempt may take (a little under the task's ``max_wall_s``).
Once the attempt has run an iteration, the loop stops before one that would not end in time
(1.25 times the longest of the recent iterations), and the end (binarize, export, re-validate)
starts only with ``--end-s`` left; otherwise the job exits 75 with its checkpoint written, the
wrapper syncs it and the backend retries the task, which resumes. Every attempt makes progress,
and the retries stay within the campaign's ``max_retries``.

``--validate-from DIR`` re-validates a finished run instead: DIR's top-level files (not its
``validation.json``) are copied into ``--out``, then ``python -m yapnr.rf.cases validate`` runs
there.

``yapnr.rf`` comes from the source bundle on ``PYTHONPATH``; this file needs the standard library
and, for a YAML criteria file, PyYAML (in the image, as ``yapnr.rf`` needs it too).

Written by tools/exp/rf_stage_plan.py into the job bundle of an ``mc-eval`` stage plan.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import time

EXIT_CONTINUE = 75  # EX_TEMPFAIL: the wrapper syncs the checkpoint and the backend retries
ITERATION_MARGIN = 1.25
RECENT = 5
RECORD = "validation.json"


def load_mapping(path: str):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def linspace(start: float, stop: float, points: int):
    if points < 2:
        return [start][:points]
    step = (stop - start) / (points - 1)
    return [start + step * i for i in range(points - 1)] + [stop]


def use_criteria(cases, case: str, path: str) -> None:
    """Replace ``case``'s criteria and dense frequencies (in this process) by the file's."""
    try:
        import numpy as np
    except ImportError:  # only a stand-in yapnr.rf (the unit tests) runs without numpy
        np = None

    doc = load_mapping(path)
    missing = {"dense_ghz", "coarse", "fine"} - set(doc or {})
    if missing:
        raise SystemExit("rf_job: %s lacks %s" % (path, ", ".join(sorted(missing))))
    dense = []
    for item in doc["dense_ghz"]:
        if isinstance(item, (list, tuple)):
            start, stop, points = item
            dense.extend(linspace(float(start), float(stop), int(points)))
        else:
            dense.append(float(item))
    dense = sorted(dense)

    def check(fields):
        fields = dict(fields)
        for key in ("ports", "ghz", "at_ghz"):
            if fields.get(key) is not None:
                fields[key] = tuple(fields[key])
        return cases.Check(**fields)

    cases.CRITERIA[case] = {level: [check(c) for c in doc[level]] for level in ("coarse", "fine")}
    cases.DENSE_GHZ[case] = np.asarray(dense, dtype=np.float64) if np else dense


def limit_attempt(attempt_s: float, end_s: float, log=None) -> None:
    """Make the optimizer exit 75 before this attempt runs out of time (see the module doc)."""
    from yapnr.rf import driver

    log = log or (lambda text: print("rf_job: " + text, file=sys.stderr, flush=True))
    start = time.monotonic()
    walls = []  # this attempt's iterations
    iterate, finish = driver.Optimizer.iterate, driver.Optimizer.finish

    def left():
        return attempt_s - (time.monotonic() - start)

    def later(what, need):
        log(
            "%.0f s left in this attempt and %s needs about %.0f s: exit %d, the next attempt "
            "resumes from the checkpoint" % (left(), what, need, EXIT_CONTINUE)
        )
        raise SystemExit(EXIT_CONTINUE)

    def timed_iterate(self):
        if walls:
            recent = [float(r.get("wall_s") or 0.0) for r in self.history[-RECENT:]]
            need = ITERATION_MARGIN * max(walls[-RECENT:] + recent)
            if need > left():
                later("an iteration", need)
        t0 = time.monotonic()
        rec = iterate(self)
        walls.append(time.monotonic() - t0)
        return rec

    def timed_finish(self):
        if walls and end_s > left():
            later("the end (binarize, export, re-validate)", end_s)
        return finish(self)

    driver.Optimizer.iterate = timed_iterate
    driver.Optimizer.finish = timed_finish


def copy_run(src: str, out: str) -> None:
    """A finished run's top-level files into ``out``, without its validation report."""
    if not os.path.isfile(os.path.join(src, "spec.json")):
        raise SystemExit("rf_job: %s is not a run directory (no spec.json)" % src)
    os.makedirs(out, exist_ok=True)
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.isfile(path) and not name.startswith(".") and name != RECORD:
            shutil.copyfile(path, os.path.join(out, name))
    stale = os.path.join(out, RECORD)
    if os.path.exists(stale):
        os.remove(stale)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rf_job.py", description=__doc__.split("\n")[0])
    ap.add_argument("case", help="the preset case: its criteria and frequencies judge the run")
    ap.add_argument("--out", required=True, help="run directory")
    ap.add_argument("--spec", help="a spec file (YAML or JSON) to run instead of the preset")
    ap.add_argument("--seed", help="optimizer.seed, the starting design ('none': uniform)")
    ap.add_argument("--threads", type=int, help="solver.threads")
    ap.add_argument("--criteria", help="pass criteria and dense frequencies (JSON or YAML)")
    ap.add_argument("--attempt-s", type=float, help="the time this attempt may take (s)")
    ap.add_argument("--end-s", type=float, help="the time the end needs (s; default S/3, 1 h)")
    ap.add_argument("--validate-from", help="re-validate this finished run directory")
    args, rest = ap.parse_known_args(argv)
    if args.validate_from and (
        args.spec or args.seed is not None or args.threads is not None or args.attempt_s
    ):
        ap.error("--validate-from takes the run's spec: no --spec, --seed, --threads, --attempt-s")
    end_s = args.end_s if args.end_s is not None else min(3600.0, (args.attempt_s or 0) / 3.0)
    if args.attempt_s and not 0 <= end_s < args.attempt_s:
        ap.error("--end-s is from 0 to below --attempt-s")

    from yapnr.rf import cases

    if args.criteria:
        use_criteria(cases, args.case, args.criteria)
    if args.validate_from:
        copy_run(args.validate_from, args.out)
        return cases.main(["validate", args.case, "--out", args.out] + rest)

    if args.spec or args.seed is not None or args.threads is not None:
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
    if args.attempt_s:
        limit_attempt(args.attempt_s, end_s)
    return cases.main(["run", args.case, "--out", args.out] + rest)


if __name__ == "__main__":
    sys.exit(main())
