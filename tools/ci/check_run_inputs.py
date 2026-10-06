#!/usr/bin/env python3
"""Check that a downloaded regression-ladder run directory has what a later CI step needs.

    python3 tools/ci/check_run_inputs.py RUN_DIR --file design.json --file result.json \\
        [--dir trace] [--seed N ...]

Reads ``RUN_DIR/summary.json`` (written by ``hardware/pnr/regression/run.py``) and, for every
result (optionally filtered to ``--seed``), checks that each ``--file`` name exists in the
case's directory and each ``--dir`` name exists there as a non-empty directory. Exits 1 with a
plain list of the case/file pairs that are missing; a run artifact that was uploaded without
one of these (e.g. an upload step's ``path:`` list left a file out) fails here instead of
inside whatever renders or reads the run next, with a clearer message than that tool's own
traceback. ``RUN_DIR/summary.json`` missing or unreadable is also an error, not a silent no-op
(an empty or failed download should not read as "nothing to check"). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def missing_inputs(run, files=(), dirs=(), seeds=None):
    """List of ``(case, seed, name)`` for every required file or directory not found under a
    result's directory. ``seeds`` (optional) restricts which results are checked."""
    summary_path = Path(run) / "summary.json"
    summary = json.loads(summary_path.read_text())
    missing = []
    for result in summary.get("results", []):
        if seeds is not None and result.get("seed") not in seeds:
            continue
        directory = Path(result["directory"])
        if not directory.is_absolute():
            directory = Path(run) / directory
        for name in files:
            if not (directory / name).is_file():
                missing.append((result.get("case"), result.get("seed"), name))
        for name in dirs:
            path = directory / name
            if not path.is_dir() or not any(path.iterdir()):
                missing.append((result.get("case"), result.get("seed"), name + "/"))
    return missing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", type=Path, metavar="RUN_DIR")
    ap.add_argument("--file", dest="files", action="append", default=[])
    ap.add_argument("--dir", dest="dirs", action="append", default=[])
    ap.add_argument("--seed", dest="seeds", type=int, action="append", default=None)
    a = ap.parse_args(argv)
    if not a.files and not a.dirs:
        ap.error("at least one of --file or --dir is required")

    try:
        missing = missing_inputs(a.run, a.files, a.dirs, a.seeds)
    except FileNotFoundError as exc:
        print("::error::%s has no summary.json (%s)" % (a.run, exc), file=sys.stderr)
        return 1

    if missing:
        print(
            "::error::%s is missing required inputs for %d case/file pair(s):"
            % (a.run, len(missing)),
            file=sys.stderr,
        )
        for case, seed, name in missing:
            print("  %s (seed %s): %s" % (case, seed, name), file=sys.stderr)
        return 1

    print("%s: every required input is present." % a.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
