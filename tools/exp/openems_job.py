#!/usr/bin/env python3
"""Run one openEMS model script inside a task and record what it did (tools/exp/openems_plan.py).

    python3 job/openems_job.py --id ID --threads T [--engine E] [--openems-option K[=V]]... \
        -- SCRIPT [ARGS...]

Runs ``SCRIPT ARGS`` with the interpreter running this file, in the task's work directory, with
``OMP_NUM_THREADS=T``; ``{out}`` in ARGS becomes ``out/ID`` (the model's output directory) and
``{threads}`` becomes T. ``--engine`` (basic, sse, sse-compressed, multithreaded; openEMS's
default is the fastest, multithreaded) and ``--openems-option`` (any option of the openEMS
executable, e.g. ``exact-endcriteria``) reach every ``openEMS.Run`` of the unchanged script: it
then runs under a bootstrap that hands them to the ``openEMS`` class its ``from openEMS import
openEMS`` finds. Its output goes to ``out/ID.log``, ending in ``exit N`` like the local
runs; the last lines also go to stdout for the wrapper's log tail. ``out/ID.job.json`` (written
last; the task's record) holds the exit code, wall and CPU seconds, peak memory, the CPU model and
whether it has AVX-512, the image's build flags and openEMS's own timing lines (iterations, cells,
seconds, MCells/s); ``ok`` is an exit code of 0.

Standard library only: it runs in the openEMS image, not in yapnr's.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

TAIL_LINES = 40
ARCH_FLAGS_FILE = Path("/opt/openEMS/arch-flags.txt")
TIME_RE = re.compile(r"Time for (\d+) iterations with ([0-9.eE+-]+) cells : ([0-9.eE+-]+) sec")
SPEED_RE = re.compile(r"^Speed: ([0-9.eE+-]+) MCells/s")
ENGINE_RE = re.compile(r"^Create FDTD engine \((.*)\)")
ENGINES = ("basic", "sse", "sse-compressed", "multithreaded", "fastest")
OPTIONS_ENV = "YAPNR_OPENEMS_OPTIONS"
# Runs the model script with openEMS.openEMS replaced by a subclass whose Run adds the options of
# OPTIONS_ENV (a JSON object of openEMS command-line options, as Run's keyword arguments).
BOOTSTRAP = (
    """
import json, os, runpy, sys
import openEMS
_options = json.loads(os.environ.pop(%r))
_base = openEMS.openEMS
class openEMS_(_base):
    def Run(self, sim_path, *args, **kw):
        kw.update(_options)
        return _base.Run(self, sim_path, *args, **kw)
openEMS_.__name__ = openEMS_.__qualname__ = "openEMS"
openEMS.openEMS = openEMS_
sys.argv = sys.argv[1:]
sys.path[0] = os.path.dirname(os.path.abspath(sys.argv[0]))
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    % OPTIONS_ENV
)


def cpu_info():
    model, flags = platform.processor() or None, set()
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return {"model": model, "avx512f": None, "count": os.cpu_count()}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name" and not model:
            model = value.strip()
        elif key.strip() == "flags" and not flags:
            flags = set(value.split())
    return {"model": model, "avx512f": "avx512f" in flags, "count": os.cpu_count()}


def parse_timing(lines):
    """openEMS's closing lines of every run in the log: iterations, cells, seconds, MCells/s."""
    runs, engines = [], []
    for line in lines:
        match = TIME_RE.search(line)
        if match:
            runs.append(
                {
                    "iterations": int(match.group(1)),
                    "cells": float(match.group(2)),
                    "seconds": float(match.group(3)),
                }
            )
            continue
        match = SPEED_RE.match(line.strip())
        if match and runs and "mcells_per_s" not in runs[-1]:
            runs[-1]["mcells_per_s"] = float(match.group(1))
            continue
        match = ENGINE_RE.match(line.strip())
        if match:
            engines.append(match.group(1))
    return runs, engines


def openems_options(engine, options):
    """Run's keyword arguments for --engine and --openems-option K[=V] (underscores, values)."""
    out = {}
    if engine:
        if engine not in ENGINES:
            raise ValueError("engine %r is not one of %s" % (engine, ", ".join(ENGINES)))
        out["engine"] = engine
    for text in options or []:
        key, sep, value = text.partition("=")
        key = key.strip().lstrip("-").replace("-", "_")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", key):
            raise ValueError("bad openEMS option %r" % text)
        out[key] = value if sep else True
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--id", required=True, help="the model run's name (out/ID, out/ID.log)")
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--engine", help="the openEMS engine (%s)" % ", ".join(ENGINES))
    ap.add_argument(
        "--openems-option",
        action="append",
        default=[],
        help="an openEMS option for every Run, K or K=V (repeatable)",
    )
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- SCRIPT [ARGS...]")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no script")
    out_dir = Path("out") / args.id
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        options = openems_options(args.engine, args.openems_option)
    except ValueError as err:
        ap.error(str(err))
    argv_run = (
        [sys.executable]
        + (["-c", BOOTSTRAP] if options else [])
        + [
            a.replace("{out}", str(out_dir)).replace("{threads}", str(args.threads))
            for a in command
        ]
    )
    env = dict(os.environ, OMP_NUM_THREADS=str(args.threads))
    if options:
        env[OPTIONS_ENV] = json.dumps(options, sort_keys=True)
    log_path = Path("out") / ("%s.log" % args.id)
    tail, timing_lines = deque(maxlen=TAIL_LINES), []
    start, cpu0 = time.time(), resource.getrusage(resource.RUSAGE_CHILDREN)
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            argv_run, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True
        )
        for line in proc.stdout:
            log.write(line)
            tail.append(line)
            if "iterations with" in line or line.startswith(("Speed:", "Create FDTD engine")):
                timing_lines.append(line)
        code = proc.wait()
        log.write("exit %d\n" % code)
    wall = time.time() - start
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    sys.stdout.write("".join(tail))
    runs, engines = parse_timing(timing_lines)
    try:
        arch_flags = ARCH_FLAGS_FILE.read_text().strip()
    except OSError:
        arch_flags = None
    record = {
        "schema": "yapnr-openems-job-v1",
        "id": args.id,
        "ok": code == 0,
        "exit": code,
        "command": command,
        "threads": args.threads,
        "openems_options": options,
        "wall_s": round(wall, 3),
        "user_s": round(usage.ru_utime - cpu0.ru_utime, 3),
        "sys_s": round(usage.ru_stime - cpu0.ru_stime, 3),
        "max_rss_mb": round(usage.ru_maxrss / 1024.0, 1),
        "cpu": cpu_info(),
        "arch_flags": arch_flags,
        "engines": engines,
        "openems_runs": runs,
        "attempt": os.environ.get("YAPNR_ATTEMPT"),
    }
    record_path = Path("out") / ("%s.job.json" % args.id)
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, record_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
