"""Compare source placement finalists using complete native electrical runs.

Signal/capacity results only shortlist starts. Every selected candidate receives
all electrical phases and the same refinement budget. A finite comparison is not
an observed placement plateau, and an experimental winner is not a qualified PCB.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from pnr import proc


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def candidate_placements(diagnostics):
    """Keep the source-loop result plus all shortlisted, actually routed starts."""
    root = Path(diagnostics)
    candidates = [("source-selected", root / "placed.json")]
    report = root / "initial-pool/report.json"
    if report.exists():
        manifest = json.loads(report.read_text())
        for name in manifest.get("route_finalists", []):
            if not isinstance(name, str) or not re.fullmatch(r"start-\d+", name):
                raise ValueError("unsafe initial finalist name")
            candidates.append((name, root / "initial-pool" / name / "placed.json"))
    unique, seen = [], set()
    for name, path in candidates:
        graph = json.loads(path.read_text())
        # Canonicalize key ordering only; differing metadata conservatively keeps
        # another evaluation rather than asserting geometric equivalence.
        geometry = json.dumps(graph, sort_keys=True, separators=(",", ":"))
        if geometry not in seen:
            seen.add(geometry)
            unique.append((name, path))
    return unique


def validate_evaluation(folder):
    """Reject partial, stale, missing-phase or inconsistent candidate reports."""
    from pnr.full_iteration import objective

    folder = Path(folder)
    result = json.loads((folder / "evaluation.json").read_text())
    if (
        not result.get("all_phases_completed")
        or result.get("score_scope") != "post-electrical-final-refill"
    ):
        raise ValueError("candidate did not finish all electrical phases")
    # Native versions may add suffixes; use their actual persisted phase names.
    phases = sorted(p for p in (folder / "phases").iterdir() if p.is_dir())
    required_prefixes = [f"{n:02d}-" for n in range(10)]
    if any(not any(p.name.startswith(prefix) for p in phases) for prefix in required_prefixes):
        raise ValueError("missing full electrical phase snapshot")
    final = folder / "phases/09-final-audit"
    record = json.loads((final / "phase.json").read_text())
    if sha(final / "diagnostic.kicad_pcb") != record["sha256"]:
        raise ValueError("stale final board hash")
    if result["final"]["sha256"] != record["sha256"]:
        raise ValueError("evaluation refers to a different final board")
    drc = json.loads((final / "diagnostic.drc.json").read_text())
    entries = json.loads((folder / "electrical/pad-entry.json").read_text())
    audit = json.loads((folder / "electrical/audit.json").read_text())
    score = objective(drc, entries, audit)
    if (
        score != result["objective"]
        or record["opens"] != score[-1]
        or record["violations"] != score[0]
    ):
        raise ValueError("candidate counts do not match saved native reports")
    if result.get("guard_valid") is False:
        raise ValueError("candidate failed preserved-connectivity guards")
    # Qualification unknowns stay visible; they are not converted into success.
    return dict(
        objective=score,
        qualified=audit["qualified"],
        sha256=record["sha256"],
        folder=str(folder.resolve()),
    )


def choose_completed(results):
    valid = [
        (name, result)
        for name, result in results.items()
        if result.get("completed") and result.get("validated")
    ]
    if not valid:
        raise RuntimeError("no candidate completed native electrical evaluation")
    return min(valid, key=lambda item: (tuple(item[1]["objective"]), item[0]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--diagnostics", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--constraints", type=Path, required=True)
    ap.add_argument("--electrical-fab", type=Path, required=True)
    ap.add_argument("--plane-fab", type=Path, required=True)
    ap.add_argument("--annotation-source", action="append", type=Path, required=True)
    ap.add_argument("--seconds", type=int, default=1800)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--route-workers", type=int, default=4)
    ap.add_argument("--kicad-python", required=True)
    ap.add_argument("--kicad-cli", required=True)
    a = ap.parse_args(argv)
    if min(a.seconds, a.workers, a.route_workers) <= 0 or a.workers * a.route_workers > 16:
        ap.error("positive budgets required; total routing workers must not exceed16")
    root, diagnostic = a.out_dir.resolve(), a.diagnostics.resolve()
    candidates = candidate_placements(diagnostic)
    root.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    from pnr.live import emit

    lane = os.environ.get("PNR_LIVE_CANDIDATE", "source")
    results = {}
    common = [diagnostic / name for name in ["source.kicad_pcb", "rules.json", "fp-lib-table"]]
    inputs = (
        common
        + [p for _, p in candidates]
        + [a.constraints, a.electrical_fab, a.plane_fab]
        + a.annotation_source
    )
    if (diagnostic / "source.kicad_pro").exists():
        inputs.append(diagnostic / "source.kicad_pro")
    frozen = {str(p.resolve()): sha(p) for p in inputs}
    (root / "input-hashes.json").write_text(json.dumps(frozen, indent=2))

    def evaluate(name, placed):
        folder = root / "candidates" / name
        folder.mkdir(parents=True, exist_ok=False)
        for source in common:
            shutil.copy2(source, folder / source.name)
        shutil.copy2(placed, folder / "placed.json")
        project = diagnostic / "source.kicad_pro"
        if project.exists():
            shutil.copy2(project, folder / project.name)
        env = dict(
            os.environ,
            PNR_CANDIDATE_WORKERS=str(a.workers),
            PNR_SINGLE_TRACK_WORKERS=str(a.route_workers),
            PNR_LIVE_CANDIDATE=lane + "/electrical-" + name,
            PNR_PROFILE_DIR=str(folder / "profiles"),
            PYTHONPATH=os.pathsep.join(
                dict.fromkeys([str(Path(__file__).resolve().parent.parent), *sys.path])
            ),
        )
        # Keep equal comparison budgets; UI controls are intentionally not adopted
        # mid-pool. Subsequent pools may read newly requested configuration.
        env.pop("PNR_CONTROL_FILE", None)
        env.pop("PNR_COST_CAPTURE_DIR", None)
        cmd = [
            sys.executable,
            "-m",
            "pnr.full_iteration",
            str(folder),
            "--constraints",
            str(a.constraints.resolve()),
            "--electrical-fab",
            str(a.electrical_fab.resolve()),
            "--plane-fab",
            str(a.plane_fab.resolve()),
            "--seconds",
            str(a.seconds),
            "--python",
            a.kicad_python,
            "--cli",
            a.kicad_cli,
        ]
        for source in a.annotation_source:
            cmd.extend(["--annotation-source", str(source.resolve())])
        (folder / "command.json").write_text(json.dumps(cmd, indent=2))
        emit(
            "candidate_queued",
            candidate=env["PNR_LIVE_CANDIDATE"],
            data=dict(
                phase="full electrical finalist",
                native_seconds=a.seconds,
                route_workers=a.route_workers,
                provisional=True,
            ),
        )
        started = time.monotonic()
        # Bounded (PNR_EVALUATION_TIMEOUT); the child stays in this process group.
        with (folder / "evaluation.log").open("w") as log:
            code, timed_out = proc.run_status(
                cmd,
                timeout=proc.evaluation_timeout(a.seconds),
                session=False,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        (folder / "process-exit.json").write_text(
            json.dumps(
                dict(
                    returncode=code,
                    seconds=time.monotonic() - started,
                    **(dict(timed_out=True) if timed_out else {}),
                )
            )
        )
        if code:
            raise RuntimeError(f"full electrical candidate exited{code}")
        return dict(validate_evaluation(folder), completed=True, validated=True)

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(evaluate, name, placed): name for name, placed in candidates}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = dict(completed=False, validated=False, error=str(exc))
                emit("candidate_failed", candidate=lane + "/electrical-" + name, data=results[name])
            (root / "candidate-results.json").write_text(json.dumps(results, indent=2))
    changed = [name for name, value in frozen.items() if sha(name) != value]
    if changed:
        raise RuntimeError("source inputs changed during full electrical pool: " + repr(changed))
    name, winner = choose_completed(results)
    selected = Path(winner["folder"])
    (root / "best").mkdir()
    (root / "policy").mkdir()
    final = selected / "phases/09-final-audit"
    for extension in [".kicad_pcb", ".kicad_pro", ".drc.json"]:
        shutil.copy2(final / ("diagnostic" + extension), root / "best" / ("candidate" + extension))
    shutil.copy2(final / "fp-lib-table", root / "best/fp-lib-table")
    shutil.copy2(selected / "evaluated-rules.json", root / "policy/prepare.json")
    shutil.copy2(selected / "evaluated-placed.json", root / "selected-placed.json")
    shutil.copy2(selected / "electrical/plane-access.json", root / "selected-plane-access.json")
    progress = json.loads((selected / "electrical/native-loop/progress.json").read_text())
    progress["native_pre_cleanup_opens"] = progress.get("opens")
    progress.update(
        opens=winner["objective"][-1],
        best=str(root / "best/candidate.kicad_pcb"),
        best_sha256=winner["sha256"],
    )
    progress["full_electrical_pool"] = dict(
        selected=name,
        candidates=results,
        native_seconds_per_candidate=a.seconds,
        seconds=time.monotonic() - start,
        reason="full_electrical_finalist_budget_completed",
        plateau_observed=False,
        final_objective=winner["objective"],
        qualified=winner["qualified"],
        changed_inputs=changed,
    )
    (root / "progress.json").write_text(json.dumps(progress, indent=2))
    (root / "termination.json").write_text(json.dumps(progress["full_electrical_pool"], indent=2))
    print(json.dumps(progress["full_electrical_pool"]))
    return root / "best/candidate.kicad_pcb"


if __name__ == "__main__":
    main()
