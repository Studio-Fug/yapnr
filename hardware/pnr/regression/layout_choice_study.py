"""Reproducible synthetic contextual-layout A/B. Placement evidence only.

Run with the numerical PnR runtime:
python -m regression.layout_choice_study --out RESULTS --seeds 0 1 2
Every case executes in its own bounded child; all attempts are retained.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from regression.layout_choice_cases import CASES

from pnr.hier.layout_search import LayoutSearch

METHODS = {
    "control": LayoutSearch(),
    "independent": LayoutSearch(method="independent"),
    "frozen": LayoutSearch(method="frozen"),
    "alternating": LayoutSearch(method="alternating", variants=2),
    "diverse": LayoutSearch(method="alternating", variants=2, diverse=True),
    "paired": LayoutSearch(method="paired", variants=2, diverse=True),
}


def worker(case, method, seed, out, iters):
    from pnr.hier.top import hierarchical_place
    from pnr.place.cost_inspect import Objective
    from pnr.place.metrics import hard_violations, hpwl

    g, c, r, lib = CASES[case]()
    (out / "input.json").write_text(g.to_json())
    (out / "library.json").write_text(json.dumps(lib, sort_keys=True, indent=2))
    (out / "constraints.json").write_text(
        json.dumps(asdict(c), default=lambda v: v.value, sort_keys=True, indent=2)
    )
    record = dict(
        case=case, method=method, seed=seed, iterations=iters, config=asdict(METHODS[method])
    )
    start = time.monotonic()
    try:
        flat, report, meta = hierarchical_place(
            g, c, r, lib, seed, iters=iters, layout_search=METHODS[method]
        )
        violations = hard_violations(flat, c, 0.2)
        record.update(
            legal=report.legal and not any(violations.values()),
            violations=violations,
            hpwl_mm=hpwl(flat),
            cost=float(Objective(flat, c).evaluate()[1].sum()),
            selected={n: v["tier_rank"] for n, v in meta["blocks"].items()},
            evaluations=meta.get("layout_search", {}).get("evaluations", 1),
        )
        (out / "placed.json").write_text(flat.to_json())
        (out / "search.json").write_text(
            json.dumps(meta, sort_keys=True, indent=2, allow_nan=False)
        )
    except Exception as error:
        record.update(legal=False, error=f"{type(error).__name__}: {error}")
    record["seconds"] = time.monotonic() - start
    record["artifacts"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*.json"))
    }
    (out / "result.json").write_text(json.dumps(record, sort_keys=True, indent=2, allow_nan=False))
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--iters", type=int, default=120)
    ap.add_argument("--worker", nargs=3)
    a = ap.parse_args()
    if a.worker:
        worker(a.worker[0], a.worker[1], int(a.worker[2]), a.out, a.iters)
        return
    a.out.mkdir(parents=True, exist_ok=False)
    clean = {k: v for k, v in os.environ.items() if not k.startswith("PNR_")}
    clean.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    files = [
        *Path(__file__).parent.glob("layout_choice*.py"),
        *Path(__file__).parents[1].joinpath("pnr").rglob("*.py"),
    ]
    (a.out / "manifest.json").write_text(
        json.dumps(
            dict(
                seeds=a.seeds,
                iters=a.iters,
                worker_timeout_seconds=180,
                threads=1,
                qualification="Synthetic placement; no native routing or electrical qualification",
                sources={
                    str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(
                        p.read_bytes()
                    ).hexdigest()
                    for p in sorted(files)
                },
            ),
            indent=2,
        )
    )
    results = []
    for case in CASES:
        for seed in a.seeds:
            for method in METHODS:
                dest = a.out / f"{case}-{method}-s{seed}"
                dest.mkdir()
                cmd = [
                    sys.executable,
                    "-m",
                    "regression.layout_choice_study",
                    "--out",
                    str(dest),
                    "--iters",
                    str(a.iters),
                    "--worker",
                    case,
                    method,
                    str(seed),
                ]
                try:
                    with (dest / "worker.log").open("w") as log:
                        subprocess.run(
                            cmd,
                            env=clean,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            timeout=180,
                            check=True,
                        )
                    row = json.loads((dest / "result.json").read_text())
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
                    row = dict(
                        case=case, seed=seed, method=method, legal=False, error=type(error).__name__
                    )
                results.append(row)
                (a.out / "results.json").write_text(json.dumps(results, sort_keys=True, indent=2))
                print(case, seed, method, row.get("legal"), row.get("hpwl_mm"), flush=True)


if __name__ == "__main__":
    main()
