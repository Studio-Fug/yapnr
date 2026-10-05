#!/usr/bin/env python3
"""One PnR exploration task: a chain of driver steps against a board's ``integrate.py``-shaped
driver (a script with a single positional ``step`` argument, run once per step; any board
example that follows this convention, not only radar60).

    python pnr_explore_job.py --driver DRIVER --python PY --work-dir WORK --out OUT
        [--seed-from DIR] [--common JSON] [--step JSON]... [--collect GLOB]...
        [--embed KEY=PATH]... [--record result.json]

``--driver`` is the step script (``examples/.../board/integrate.py``), run as
``PY DRIVER STEP --work WORK --out ... <step args>`` for each ``--step``. A step is a JSON array,
its first element the step name and the rest its own arguments (``["place", "--seed", "3"]``);
``--common`` (a JSON array, optional, repeatable) is inserted after ``STEP`` and before the
step's own arguments, for flags every step takes (``--engine``, ``--python``, ``--work``,
``--out``...). ``--seed-from DIR`` copies a prepared snapshot (an earlier ``source``/``prepare``
run, read-only in the task's inputs) into ``--work-dir`` before the first step, so a cloud task
with no network access does not have to redo whatever ``source`` needs (an atopile board, say).

Steps run in order and stop at the first failure; each one's exit code, wall time and log are
recorded. On the way out, ``--collect`` globs (resolved against the current directory, so
relative to ``--work-dir`` and ``--out``'s driver-side directory both work) are copied into
``OUT/files/`` by basename, and ``--embed KEY=PATH`` reads a JSON file at PATH (when it exists)
into the record's ``data[KEY]`` -- the way a board's own ``check`` step hands its DRC/IR/opens
report straight to the campaign's ranking table without a second parse of the fetched tree.

The record (``OUT/<--record>``, default ``result.json``) is ``{"ok": bool, "driver": ...,
"steps": [{"step", "args", "returncode", "seconds"}...], "wall_s": ..., "data": {...embedded...},
"files": [...collected basenames...]}``; ``ok`` is true iff every step exited 0. Written by
tools/exp/pnr_explore_plan.py into the job bundle of an ``mc-eval`` stage plan
(docs/cloud-experiments.md, "Any command as a task").
"""

from __future__ import annotations

import argparse
import glob as glob_mod
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _json_arg(text: str) -> List[str]:
    value = json.loads(text)
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise argparse.ArgumentTypeError("not a JSON array of strings: %r" % text)
    return value


def _kv(text: str) -> tuple:
    if "=" not in text:
        raise argparse.ArgumentTypeError("not KEY=PATH: %r" % text)
    key, path = text.split("=", 1)
    if not key:
        raise argparse.ArgumentTypeError("empty key in %r" % text)
    return key, path


def run_steps(
    driver: str,
    python: str,
    common: List[str],
    steps: List[List[str]],
    log_dir: Path,
) -> Dict[str, Any]:
    """Run ``steps`` in order against ``driver``; stop at the first failure."""
    log_dir.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    ok = True
    for index, step in enumerate(steps):
        name, extra = step[0], step[1:]
        argv = [python, driver, name] + common + extra
        start = time.time()
        proc = subprocess.run(argv, capture_output=True, text=True)
        seconds = time.time() - start
        log_path = log_dir / ("%02d-%s.log" % (index, name))
        log_path.write_text((proc.stdout or "") + (proc.stderr or ""))
        records.append(
            {
                "step": name,
                "args": extra,
                "returncode": proc.returncode,
                "seconds": round(seconds, 3),
                "log": log_path.name,
            }
        )
        if proc.returncode != 0:
            ok = False
            break
    return {"ok": ok, "steps": records}


def collect_files(patterns: List[str], dest: Path) -> List[str]:
    names = []
    for pattern in patterns:
        for found in sorted(glob_mod.glob(pattern)):
            path = Path(found)
            target = dest / path.name
            dest.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                shutil.copyfile(path, target)
            names.append(path.name)
    return names


def embed_data(pairs: List[tuple]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for key, path in pairs:
        candidate = Path(path)
        if candidate.is_file():
            try:
                data[key] = json.loads(candidate.read_text())
            except ValueError as err:
                data[key] = {"error": "could not parse %s: %s" % (path, err)}
        else:
            data[key] = None
    return data


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--driver", required=True, help="the integrate.py-shaped step script")
    ap.add_argument("--python", default=sys.executable, help="the numeric Python that runs it")
    ap.add_argument("--work-dir", required=True, help="the driver's scratch/work directory")
    ap.add_argument("--seed-from", help="copy this prepared snapshot into --work-dir first")
    ap.add_argument("--common", action="append", default=[], type=_json_arg)
    ap.add_argument("--step", dest="steps", action="append", required=True, type=_json_arg)
    ap.add_argument("--collect", action="append", default=[], help="glob to copy into OUT/files")
    ap.add_argument("--embed", action="append", default=[], type=_kv, help="KEY=PATH, a JSON file")
    ap.add_argument("--out", required=True, help="this task's own result directory")
    ap.add_argument("--record", default="result.json")
    args = ap.parse_args(argv)

    work_dir = Path(args.work_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.seed_from:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        shutil.copytree(args.seed_from, work_dir)
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    common: List[str] = []
    for group in args.common:
        common += group

    start = time.time()
    outcome = run_steps(args.driver, args.python, common, args.steps, out_dir / "logs")
    outcome["wall_s"] = round(time.time() - start, 3)
    outcome["driver"] = args.driver
    outcome["files"] = collect_files(args.collect, out_dir / "files")
    outcome["data"] = embed_data(args.embed)

    (out_dir / args.record).write_text(json.dumps(outcome, indent=1, sort_keys=True))
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
