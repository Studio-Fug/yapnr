"""Route source-declared pairs before ordinary signals claim their corridors.

Controller uses the PnR interpreter; each exact geometry/DRC transaction runs
in a fresh KiCad process. Only legal intermediate-package proposals are tried.
An unsuccessful pair remains explicitly pending for the final completeness gate.

src13: with PNR_PAIR_HAND_SWAP_TRIAL=1 the swap trial runs the opposite of the
bridge hand the worker's joint search runs first (derived through
pnr.pair_joint.first_joint_hand, not assumed); PNR_PAIR_LANDING_RESERVE=1 makes
D-move proposals respect pair via landing reserves.
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from pnr.native_loop import copy_board, pair_placements
from pnr.placement_trials import diverse_pair_poses

# PNR_PAIR_PARALLEL=1 runs a pair's trials concurrently (PNR_PAIR_PARALLEL_WORKERS,
# default 3) and commits the lowest-index accepted trial, exactly as the serial scan.
# PNR_PAIR_HAND_SWAP_TRIAL=1 (only with PNR_PAIR_JOINT_TOPOLOGIES=1; default off) inserts
# one extra trial right after trial 0: the unmoved pose again, with the worker told
# (PNR_PAIR_JOINT_HAND_FIRST=-1, see pnr.native_electrical) to run bridge hand -1 first.
# The default joint order runs hand +1 first and it can use the whole joint window
# (60 s of a 90 s trial), so hand -1 is otherwise never tried on the unmoved pose.
# src13: the swap trial's hand is derived, not assumed: run() asks
# pnr.pair_joint.first_joint_hand (the worker's own enumeration + ordering, with the
# trial-0 environment) which hand the joint search runs first, and the swap trial
# runs the opposite one first. No joint configuration -> no swap trial.
# PNR_PAIR_LANDING_RESERVE=1: pair_placements checks D-moves against pair via
# landing reserves (pnr.place.pair_landing); see pnr.native_loop.


def trial_schedule(poses, first_hand=1):
    """[(pose, hand_first)] for the trials of one pair; hand_first None = worker default.

    first_hand: the bridge hand the worker's joint search runs first on trial 0
    (derived by swap_trial_hand); the swap trial runs -first_hand first. None
    (the pair has no joint configuration) inserts no swap trial."""
    schedule = [(pose, None) for pose in poses]
    if (
        os.environ.get("PNR_PAIR_HAND_SWAP_TRIAL") == "1"
        and os.environ.get("PNR_PAIR_JOINT_TOPOLOGIES", "0") == "1"
        and schedule
        and first_hand
    ):
        schedule.insert(1, (schedule[0][0], -first_hand))
    return schedule


def swap_trial_hand(pair, inventory):
    """First joint bridge hand of an unmoved default trial (None: no joint search).

    Same enumeration/ordering/environment the worker uses for trial 0 (trial_env
    drops PNR_PAIR_JOINT_HAND_FIRST there); pad positions from the inventory graph
    (joint_topologies only needs contact distances, so the y-up frame is fine)."""
    if (
        os.environ.get("PNR_PAIR_HAND_SWAP_TRIAL") != "1"
        or os.environ.get("PNR_PAIR_JOINT_TOPOLOGIES", "0") != "1"
    ):
        return None
    from pnr.pair_joint import first_joint_hand

    positions = {}
    for c in inventory["graph"].get("components", []):
        # Pad centre = pose + rotated pad offset (pnr.place.geometry.pin_positions,
        # kept import-free here: the controller must not need torch/yaml for this).
        th = math.radians(float(c["rot"]))
        ct, st = math.cos(th), math.sin(th)
        for pad in c.get("pads", []):
            ox, oy = pad["offset"]
            positions[c["ref"] + "." + pad["name"]] = (
                c["pos"][0] + ox * ct - oy * st,
                c["pos"][1] + ox * st + oy * ct,
            )
    return first_joint_hand(
        pair,
        positions,
        hand_first=None,
        max_trials=int(os.environ.get("PNR_PAIR_JOINT_MAX_TRIALS", "6")),
        budget_scope=os.environ.get("PNR_PAIR_AUXILIARY_SCOPE", "separate"),
    )


def trial_env(env, hand_first):
    trial = dict(env)
    trial.pop("PNR_PAIR_JOINT_HAND_FIRST", None)
    if hand_first:
        trial["PNR_PAIR_JOINT_HAND_FIRST"] = str(hand_first)
    return trial


def _invoke(args, log, env):
    from pnr.proc import run_checked, worker_timeout  # stays in this process group

    with log.open("w") as f:
        run_checked(
            args,
            timeout=worker_timeout(args),
            session=False,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
        )


def _trial(args, log, env, stop, poll=0.2, timeout=None):
    """Cancellable _invoke for parallel trials: True if the worker finished, False if stopped.

    Bounded like _invoke (``timeout``, default pnr.proc.worker_timeout of the trial's
    own --seconds): a trial still running at its deadline is killed and raises
    pnr.proc.DeadlineExceeded (-9).
    """
    from pnr import proc as bounded

    limit = bounded.worker_timeout(args) if timeout is None else timeout
    deadline = time.monotonic() + limit
    with log.open("w") as f:
        proc = subprocess.Popen(args, env=env, stdout=f, stderr=subprocess.STDOUT)
        try:
            while True:
                try:
                    code = proc.wait(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    if stop.is_set():
                        return False
                    if time.monotonic() >= deadline:
                        bounded.kill_tree(proc)
                        proc.wait()  # the trial and every worker it started
                        f.write(
                            "\n[paired_bootstrap] trial killed after its %g s deadline\n" % limit
                        )
                        raise bounded.DeadlineExceeded(args, limit)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                f.write(
                    "\n[paired_bootstrap] trial stopped: a lower-index trial decided this pair\n"
                )
    if code:
        raise subprocess.CalledProcessError(code, args)
    return True


def first_accepted(count, launch, accepted, workers=3, done=None):
    """Run launch(i,stop) for i<count, <=workers at a time in index order; decide like the serial scan.

    Returns (outcomes,winner): winner is the lowest accepted index, else None; a
    worker error is re-raised only when the serial scan would reach it first.
    Once trial i is accepted or fails, later trials are stopped (running) or
    never launched (outcome None); lower trials always finish. done(i,outcome)
    runs on this controller thread as each launched trial ends.
    """
    stops = [threading.Event() for _ in range(count)]
    outcomes = {}
    errors = {}
    futures = {}
    pending = set()
    nxt = 0
    cut = count
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        while True:
            while nxt < cut and len(pending) < max(1, workers):
                future = pool.submit(launch, nxt, stops[nxt])
                futures[future] = nxt
                pending.add(future)
                nxt += 1
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(finished, key=futures.get):
                i = futures[future]
                try:
                    outcomes[i] = future.result()
                except Exception as error:
                    errors[i] = error
                    outcomes[i] = dict(status="worker_error", error=repr(error))
                if i < cut and (i in errors or accepted(outcomes[i])):
                    cut = i + 1  # no later trial can change the serial decision
                    for stop in stops[cut:]:
                        stop.set()
                if done:
                    done(i, outcomes[i])
    finally:
        for stop in stops:
            stop.set()
        pool.shutdown(wait=True)
    for i in range(count):
        outcomes.setdefault(i, None)
    for i in range(count):
        if i in errors:
            raise errors[i]
        if accepted(outcomes[i]):
            return outcomes, i
    return outcomes, None


def run(
    board,
    rules,
    constraints,
    out,
    kicad_python,
    kicad_cli,
    seconds=600,
    attempts=5,
    search_seconds=90,
    allow_placement=True,
):
    # Env overrides for the phase / per-trial search budgets (the joint-topology window is
    # search_seconds minus a fallback reserve, so PNR_PAIR_JOINT_TRIAL_SECONDS alone cannot widen it).
    seconds = float(os.environ.get("PNR_PAIR_PHASE_SECONDS", seconds))
    search_seconds = float(os.environ.get("PNR_PAIR_SEARCH_SECONDS", search_seconds))
    if not (
        math.isfinite(seconds)
        and seconds > 0
        and math.isfinite(search_seconds)
        and search_seconds > 0
    ):
        raise ValueError("invalid pair budget seconds")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    board = Path(board).resolve()
    rules = Path(rules).resolve()
    current = out / "baseline.kicad_pcb"
    copy_board(board, current)
    # Workers share precisely this isolated source tree, never a live checkout.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    started = time.monotonic()
    events = []
    policy = json.loads(rules.read_text())
    references = list(policy.get("routed_pair_references", []))
    rules = out / "rules.json"
    rules.write_text(json.dumps(policy, indent=2))

    def invoke(args, log):
        _invoke(args, log, env)

    parallel = os.environ.get("PNR_PAIR_PARALLEL") == "1"
    workers = max(1, int(os.environ.get("PNR_PAIR_PARALLEL_WORKERS", "3"))) if parallel else 1
    policy = json.loads(rules.read_text())
    for pi, pair in enumerate(policy.get("diff_pairs", [])):
        if time.monotonic() - started >= seconds:
            break
        folder = out / f"pair-{pi:02}"
        folder.mkdir()
        inv = folder / "inventory.json"
        invoke(
            [
                kicad_python,
                "-m",
                "pnr.native_loop",
                str(current),
                "--worker",
                "inspect",
                "--rules",
                str(rules),
                "--report",
                str(inv),
            ],
            folder / "inventory.log",
        )
        inventory = json.loads(inv.read_text())
        jobs = [t for t in inventory["targets"] if t["net"] in (pair["p"], pair["n"])]
        if not jobs:
            events.append(dict(pair=pair["name"], status="already_connected"))
            continue
        target = jobs[0]
        base = [
            kicad_python,
            "-m",
            "pnr.native_electrical",
            str(current),
            "--rules",
            str(rules),
            "--net",
            target["net"],
            "--source-pad",
            target["source"],
            "--target-pad",
            target["target"],
            "--bounds",
            *map(str, inventory["bounds"]),
            "--kicad-cli",
            kicad_cli,
        ]
        proposals = (
            (
                pair_placements(inventory, constraints, pair, rules=policy)
                if os.environ.get("PNR_PAIR_LANDING_RESERVE") == "1"
                else pair_placements(inventory, constraints, pair)
            )
            if allow_placement
            else []
        )
        first_hand = swap_trial_hand(pair, inventory)
        pp = folder / "proposals.json"
        pp.write_text(json.dumps(proposals, indent=2))
        if proposals:
            screen = folder / "screen"
            invoke(
                base + ["--out-dir", str(screen), "--placement-candidates", str(pp)],
                folder / "screen.log",
            )
            proposals = json.loads((screen / "result.json").read_text())["proposals"]
        if parallel:
            schedule = trial_schedule(
                [None] + diverse_pair_poses(proposals, attempts - 1), first_hand
            )
            poses = [pose for pose, _ in schedule]
            finished = {}

            def launch(ti, stop):
                if stop.is_set():
                    return dict(status="cancelled")
                remaining = seconds - (time.monotonic() - started)
                if remaining <= 0:
                    return dict(status="budget_skipped")  # serial scan would have stopped here
                trial = folder / f"trial-{ti:02}"
                private = folder / f"input-{ti:02}" / current.name
                # Private input: KiCad writes project-local files (.kicad_prl) beside the board it reads.
                copy_board(current, private)
                if current.with_suffix(".kicad_dru").exists():
                    shutil.copyfile(
                        current.with_suffix(".kicad_dru"), private.with_suffix(".kicad_dru")
                    )
                cmd = (
                    base[:3]
                    + [str(private)]
                    + base[4:]
                    + ["--out-dir", str(trial), "--seconds", str(min(search_seconds, remaining))]
                )
                if poses[ti] is not None:
                    spec = folder / f"pose-{ti:02}.json"
                    spec.write_text(json.dumps(poses[ti]))
                    cmd += ["--placement-spec", str(spec)]
                t0 = time.monotonic()
                if not _trial(
                    cmd, folder / f"trial-{ti:02}.log", trial_env(env, schedule[ti][1]), stop
                ):
                    return dict(status="cancelled", wall_seconds=time.monotonic() - t0)
                return dict(
                    result=json.loads((trial / "result.json").read_text()),
                    wall_seconds=time.monotonic() - t0,
                )

            def trial_event(ti, outcome, winner=None):
                outcome = outcome or dict(status="cancelled")
                result = outcome.get("result")
                event = dict(
                    pair=pair["name"],
                    trial=ti,
                    proposal=poses[ti],
                    status=result.get("status") if result else outcome["status"],
                    accepted=result.get("accepted", False) if result else False,
                    folder=str(folder / f"trial-{ti:02}"),
                    parallel=True,
                )
                if schedule[ti][1]:
                    event["hand_first"] = schedule[ti][1]
                if "wall_seconds" in outcome:
                    event["wall_seconds"] = outcome["wall_seconds"]
                if "error" in outcome:
                    event["error"] = outcome["error"]
                if winner is not None and ti > winner:
                    event["ignored"] = True  # superseded by a lower-index acceptance
                return event

            def shown(record, winner=None):
                return [
                    trial_event(ti, o, winner)
                    for ti, o in sorted(record.items())
                    if (o or {}).get("status") != "budget_skipped"
                ]

            def progress(ti, outcome):
                finished[ti] = outcome
                (out / "progress.json").write_text(
                    json.dumps(
                        dict(events=events + shown(finished), seconds=time.monotonic() - started),
                        indent=2,
                    )
                )

            outcomes, winner = first_accepted(
                len(poses),
                launch,
                lambda o: bool(o and (o.get("result") or {}).get("accepted")),
                workers,
                progress,
            )
            events.extend(shown(outcomes, winner))
            (out / "progress.json").write_text(
                json.dumps(dict(events=events, seconds=time.monotonic() - started), indent=2)
            )
            if winner is not None:
                trial = folder / f"trial-{winner:02}"
                result = outcomes[winner]["result"]
                current = trial / "candidate.kicad_pcb"
                references = [ref for ref in references if ref["pair"] != pair["name"]]
                references.append(dict(pair=pair["name"], segments=result["segments"]))
                policy["routed_pair_references"] = references
                rules.write_text(json.dumps(policy, indent=2))
            continue
        for ti, (proposal, hand_first) in enumerate(
            trial_schedule([None] + diverse_pair_poses(proposals, attempts - 1), first_hand)
        ):
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            trial = folder / f"trial-{ti:02}"
            cmd = base + ["--out-dir", str(trial), "--seconds", str(min(search_seconds, remaining))]
            if proposal is not None:
                spec = folder / f"pose-{ti:02}.json"
                spec.write_text(json.dumps(proposal))
                cmd += ["--placement-spec", str(spec)]
            _invoke(cmd, folder / f"trial-{ti:02}.log", trial_env(env, hand_first))
            result = json.loads((trial / "result.json").read_text())
            events.append(
                dict(
                    pair=pair["name"],
                    trial=ti,
                    proposal=proposal,
                    status=result["status"],
                    accepted=result.get("accepted", False),
                    folder=str(trial),
                )
            )
            if hand_first:
                events[-1]["hand_first"] = hand_first
            (out / "progress.json").write_text(
                json.dumps(dict(events=events, seconds=time.monotonic() - started), indent=2)
            )
            if result.get("accepted"):
                current = trial / "candidate.kicad_pcb"
                references = [ref for ref in references if ref["pair"] != pair["name"]]
                references.append(dict(pair=pair["name"], segments=result["segments"]))
                policy["routed_pair_references"] = references
                rules.write_text(json.dumps(policy, indent=2))
                break
    final = out / "candidate.kicad_pcb"
    copy_board(current, final)
    (out / "paired-reference.json").write_text(json.dumps(references, indent=2))
    (out / "result.json").write_text(
        json.dumps(
            dict(events=events, seconds=time.monotonic() - started, board=str(final)), indent=2
        )
    )
    return final


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("board")
    p.add_argument("--rules", required=True)
    p.add_argument("--constraints", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--kicad-python", required=True)
    p.add_argument("--kicad-cli", required=True)
    p.add_argument("--seconds", type=float, default=600)
    p.add_argument("--attempts", type=int, default=5)
    a = p.parse_args()
    run(
        a.board,
        a.rules,
        a.constraints,
        a.out_dir,
        a.kicad_python,
        a.kicad_cli,
        a.seconds,
        a.attempts,
    )


if __name__ == "__main__":
    main()
