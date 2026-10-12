"""Opt-in continuation of native refinement in bounded, recoverable chunks.

A worker deadline is not convergence. Checkpoints retain accepted copper and
search history; only completed sweeps can establish a sampled plateau.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checkpoint(board, rules, constraints, **search):
    board, rules, constraints = map(lambda p: Path(p).resolve(), (board, rules, constraints))
    artifacts = {
        str(p): digest(p)
        for p in (
            board,
            board.with_suffix(".kicad_pro"),
            board.with_suffix(".kicad_dru"),
            rules,
            constraints,
        )
        if p.is_file()
    }
    return dict(
        schema="pnr-native-continuation-v1",
        board=str(board),
        rules=str(rules),
        constraints=str(constraints),
        artifacts=artifacts,
        **search,
    )


def validate_checkpoint(state, board, rules, constraints):
    if state.get("schema") != "pnr-native-continuation-v1":
        raise ValueError("unsupported continuation checkpoint")
    for name, path in zip(("board", "rules", "constraints"), (board, rules, constraints)):
        if Path(path).resolve() != Path(state[name]).resolve():
            raise ValueError("continuation " + name + " differs from checkpoint")
    for path, sha in state["artifacts"].items():
        if digest(path) != sha:
            raise ValueError("continuation input changed: " + path)
    for name in ("board", "rules", "constraints"):
        if state[name] not in state["artifacts"]:
            raise ValueError("missing continuation input hash: " + name)


def decision(state, timed_out=False):
    reason = state.get("termination")
    if timed_out:
        return "continue"
    if reason in ("zero_native_opens", "sampled_plateau", "no_legal_untried_moves"):
        return reason
    return "continue"


def main():
    from pnr.native_loop import save
    from pnr.proc import DeadlineExceeded, run_checked

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--chunk-seconds", type=int, default=1200)
    parser.add_argument("--total-seconds", type=int, default=21600)
    parser.add_argument("--plateau-cycles", type=int, default=6)
    parser.add_argument("native_args", nargs=argparse.REMAINDER)
    a = parser.parse_args()
    if min(a.chunk_seconds, a.total_seconds, a.plateau_cycles) <= 0:
        parser.error("positive worker, campaign and plateau budgets required")
    extra = a.native_args[1:] if a.native_args[:1] == ["--"] else a.native_args
    forbidden = {
        "--rules",
        "--constraints",
        "--out-dir",
        "--seconds",
        "--cycles",
        "--resume-search",
        "--plateau-cycles",
        "--early-pairs",
    }
    if any(v in forbidden for v in extra):
        parser.error("continuation owns native checkpoint, cycle and budget arguments")
    root = a.out_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "controller.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    journal_path = root / "continuation.json"
    journal = (
        json.loads(journal_path.read_text())
        if journal_path.exists()
        else dict(
            status="running",
            checkpoint=str(a.checkpoint.resolve()),
            chunks=[],
            elapsed_seconds=0,
            plateau_observed=False,
            max_seconds=a.total_seconds,
            chunk_seconds=a.chunk_seconds,
            plateau_cycles=a.plateau_cycles,
            native_args=extra,
            qualification="Native search only; final electrical and physical audits remain required.",
        )
    )
    if (
        journal["max_seconds"],
        journal["chunk_seconds"],
        journal["plateau_cycles"],
        journal["native_args"],
    ) != (a.total_seconds, a.chunk_seconds, a.plateau_cycles, extra):
        raise ValueError("continuation settings changed; start a new recorded campaign")
    if journal["status"] not in ("running", "paused"):
        raise ValueError(
            "continuation is terminal; inspect its receipt before starting another campaign"
        )
    if journal["chunks"] and journal["chunks"][-1]["status"] == "running":
        raise ValueError("interrupted controller: reconcile the recorded worker before resuming")
    journal["status"] = "running"
    elapsed = journal["elapsed_seconds"]
    started = time.monotonic()
    consecutive_errors = 0
    while elapsed + time.monotonic() - started < a.total_seconds:
        if shutil.disk_usage(root).free < 5 * 1024**3:
            journal["status"] = "disk_budget"
            break
        if (root / "PAUSE").exists():
            journal["status"] = "paused"
            break
        checkpoint_path = Path(journal["checkpoint"])
        state = json.loads(checkpoint_path.read_text())
        validate_checkpoint(state, state["board"], state["rules"], state["constraints"])
        remaining = a.total_seconds - elapsed - (time.monotonic() - started)
        # Leave a bounded finalization allowance; do not launch a knowingly doomed chunk.
        if remaining < 900:
            journal["status"] = "resource_budget"
            break
        index = len(journal["chunks"]) + 1
        folder = root / f"chunk-{index:03d}"
        command = [
            sys.executable,
            "-m",
            "pnr.native_loop",
            state["board"],
            "--rules",
            state["rules"],
            "--constraints",
            state["constraints"],
            "--out-dir",
            str(folder),
            "--resume-search",
            str(checkpoint_path),
            "--plateau-cycles",
            str(a.plateau_cycles),
            "--cycles",
            "10000",
            "--seconds",
            str(min(a.chunk_seconds, int(remaining) - 900)),
            *extra,
        ]
        entry = dict(
            index=index, input_checkpoint=str(checkpoint_path), command=command, status="running"
        )
        journal["chunks"].append(entry)
        save(journal_path, journal)
        timed_out = False
        try:
            with (root / f"chunk-{index:03d}.log").open("w") as log:
                run_checked(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
                    timeout=min(remaining, a.chunk_seconds + 900),
                )
        except (subprocess.SubprocessError, OSError) as exc:
            timed_out = isinstance(exc, DeadlineExceeded)
            entry.update(status="worker_timeout" if timed_out else "worker_error", error=str(exc))
            consecutive_errors += 1
        else:
            entry["status"] = "completed"
            consecutive_errors = 0
        saved = folder / "resume.json"
        if not saved.exists():
            journal["status"] = "worker_error"
            break
        state = json.loads(saved.read_text())
        validate_checkpoint(state, state["board"], state["rules"], state["constraints"])
        journal.update(checkpoint=str(saved), opens=state["opens"])
        entry.update(opens=state["opens"], termination=state["termination"])
        if consecutive_errors >= 2 or entry["status"] == "worker_error":
            journal["status"] = "worker_error"
            break
        result = decision(state, timed_out)
        if result != "continue":
            journal.update(status=result, plateau_observed=result == "sampled_plateau")
            break
        journal["elapsed_seconds"] = elapsed + time.monotonic() - started
        save(journal_path, journal)
    else:
        journal["status"] = "resource_budget"
    journal["elapsed_seconds"] = elapsed + time.monotonic() - started
    save(journal_path, journal)
    print(
        json.dumps(
            {k: journal[k] for k in ("status", "checkpoint", "plateau_observed", "elapsed_seconds")}
        )
    )


if __name__ == "__main__":
    main()
