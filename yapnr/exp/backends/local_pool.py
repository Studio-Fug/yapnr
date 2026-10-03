#!/usr/bin/env python3
"""The local backend's process pool: runs the wrapper for each task, niced, a few at a time.

    python local_pool.py --store DIR --campaign CID --items FILE --workers N --nice N
                         --toolchain FILE [--python PY] [--state FILE]

``--items`` is a JSON list of ``[submission, index, cores, max_wall_s]``. A task takes as many
worker slots as it has cores. Each wrapper runs as its own process group with a wall-time limit
(the task's plus a grace for staging and upload); SIGTERM to the pool stops every running wrapper
the same way (they flush checkpoints and exit 75), and nothing it did not start.

Standard library only, so it runs with any Python 3.9+ the owner points it at. ``--state`` is
written at start (pid and start time) so ``yapnr exp cancel`` can verify the process it signals.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

GRACE_S = 900
STOP_WAIT_S = 60


def log(event, **fields):
    line = dict(pool=event, time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    line.update(fields)
    print(json.dumps(line, sort_keys=True), flush=True)


def _signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(args):
    items = json.loads(Path(args.items).read_text())
    store = Path(args.store)
    wrapper = store / "campaigns" / args.campaign / "task.py"
    stopping = {"flag": False}

    def stop(signum, frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    queue = list(items)
    running = []  # (proc, item, deadline)
    failures = 0
    while queue or running:
        if stopping["flag"]:
            queue = []
            for proc, item, _ in running:
                _signal_group(proc, signal.SIGTERM)
            end = time.monotonic() + STOP_WAIT_S
            while time.monotonic() < end and any(p.poll() is None for p, _, _ in running):
                time.sleep(0.5)
            for proc, item, _ in running:
                if proc.poll() is None:
                    _signal_group(proc, signal.SIGKILL)
                    proc.wait()
            log("stopped", running=len(running))
            return 75
        used = sum(item[2] for _, item, _ in running)
        while queue and (not running or used + queue[0][2] <= args.workers):
            submission, index, cores, wall = queue.pop(0)
            argv = [
                args.python or sys.executable,
                str(wrapper),
                "--store",
                str(store),
                "--inputs",
                str(store / "bundles"),
                "--campaign",
                args.campaign,
                "--submission",
                str(submission),
                "--index",
                str(index),
                "--toolchain",
                args.toolchain,
                # The pool is niced already and its children inherit that.
                "--nice",
                "0",
            ]
            if args.work_root:
                argv += ["--work-root", args.work_root]
            env = dict(os.environ, YAPNR_BACKEND="local")
            proc = subprocess.Popen(argv, env=env, start_new_session=True)
            running.append(
                (proc, [submission, index, cores, wall], time.monotonic() + wall + GRACE_S)
            )
            used += cores
            log("start", submission=submission, index=index, pid=proc.pid)
        still = []
        for proc, item, deadline in running:
            code = proc.poll()
            if code is None and time.monotonic() > deadline:
                _signal_group(proc, signal.SIGTERM)
                try:
                    code = proc.wait(timeout=STOP_WAIT_S)
                except subprocess.TimeoutExpired:
                    _signal_group(proc, signal.SIGKILL)
                    code = proc.wait()
            if code is None:
                still.append((proc, item, deadline))
                continue
            failures += code != 0
            log("end", submission=item[0], index=item[1], code=code)
        running = still
        time.sleep(0.5)
    log("finished", failures=failures)
    return 0 if failures == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--store", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--items", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--nice", type=int, default=10)
    parser.add_argument("--toolchain", required=True)
    parser.add_argument("--python")
    parser.add_argument("--work-root")
    parser.add_argument("--state")
    args = parser.parse_args(argv)
    if args.nice:
        os.nice(args.nice)
    if args.state:
        Path(args.state).parent.mkdir(parents=True, exist_ok=True)
        Path(args.state).write_text(json.dumps({"pid": os.getpid(), "campaign": args.campaign}))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
