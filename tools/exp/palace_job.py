#!/usr/bin/env python3
"""Run one Palace model inside a task and record what it did (tools/exp/palace_plan.py).

    python3 job/palace_job.py --id ID --ranks R [--bind core|hwthread|none] [--set KEY=VALUE]...
        [--prepare JSON_ARGV] [--reference DIR] [--reference-tol T] [--dry-run-only] -- CONFIG

Runs the Palace configuration CONFIG on R MPI ranks in the task's work directory:

1. ``--prepare '["code/mesh.py", "models/a.json", "--out", "{out}"]'`` (optional) first runs that
   script with the interpreter running this file, for a task that meshes its model (gmsh) and
   writes CONFIG itself;
2. CONFIG (Palace's relaxed JSON: comments, trailing commas, integer ranges) is read, each
   ``--set Dotted.Key=VALUE`` (VALUE as JSON, else a string; list items by index) replaces a
   value, ``Problem.Output`` becomes ``out/ID/postpro`` and a relative ``Model.Mesh`` becomes
   absolute; the result is ``out/ID/config.json``;
3. ``palace --version`` and ``palace --dry-run`` (one process) check the build and the
   configuration;
4. ``mpirun -np R`` runs the solve from CONFIG's directory, ranks bound to cores (``--bind
   core``, one model per VM), to hardware threads (``hwthread``) or not at all (``none``, for
   several models on one VM);
5. with ``--reference DIR``, the solve's ``port-S.csv`` is compared with ``DIR/port-S.csv`` as
   complex S-parameters (``max |dS|`` against ``--reference-tol``, default 0.01).

``{out}`` in CONFIG, the prepare arguments and DIR becomes ``out/ID`` and ``{ranks}`` becomes R.
Output goes to ``out/ID.log``, ending in ``exit N``; the last lines also go to stdout for the
wrapper's log tail. ``out/ID.job.json`` (written last; the task's record) holds the exit code and
the stage that failed, wall and CPU seconds, the peak memory of the largest process, the CPU and
its vector extensions, the image's build, and Palace's own numbers from ``palace.json``: degrees
of freedom and mesh elements, the solves of an adaptive refinement and the unknowns of each, linear
solves and iterations, timers, peak memory per rank (maximum) and summed over the ranks. ``ok`` is
a solve that exited 0 (and matched the reference, if one was given).

Standard library only: it runs in the Palace image, not in yapnr's.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
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
PALACE_HOME = Path("/opt/palace")
BIND_FLAGS = {
    "core": ["--bind-to", "core", "--map-by", "core"],
    "hwthread": ["--use-hwthread-cpus", "--bind-to", "hwthread", "--map-by", "hwthread"],
    "none": ["--bind-to", "none", "--oversubscribe"],
}
# Tasks run as root in the container, which has no ptrace capability (no cross-memory attach).
# One VM, no RDMA: OpenMPI's own point-to-point layer over shared memory (TCP between VMs), not
# UCX or libfabric, which would only probe for fabrics the VM does not have. The environment's
# own values win.
MPI_ENV = {
    "OMPI_ALLOW_RUN_AS_ROOT": "1",
    "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
    "OMPI_MCA_btl_vader_single_copy_mechanism": "none",
    "OMPI_MCA_pml": "ob1",
    "OMPI_MCA_btl": "self,vader,tcp",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
}
AMR_ITER_RE = re.compile(r"Adaptive mesh refinement \(AMR\) iteration (\d+):")
AMR_DONE_RE = re.compile(r"Completed (\d+) iterations? of adaptive mesh refinement")
INDICATOR_RE = re.compile(r"Indicator norm = ([0-9.eE+-]+), global unknowns = (\d+)")
LEVEL_RE = re.compile(r"^\s*Level (\d+)( \(auxiliary\))? \(p = (\d+)\): (\d+) unknowns")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")
RANGE_ARRAY_RE = re.compile(r"\[([0-9,\s-]*)\]")
RANGE_RE = re.compile(r"^(-?\d+)-(-?\d+)$")
S_MAG_RE = re.compile(r"^\|S\[(\d+)\]\[(\d+)\]\| \(dB\)$")


# --- Palace's configuration files


def _expand_ranges(match):
    items = []
    for token in match.group(1).split(","):
        token = "".join(token.split())
        bounds = RANGE_RE.match(token)
        if bounds:
            items += [str(v) for v in range(int(bounds.group(1)), int(bounds.group(2)) + 1)]
        elif token:
            items.append(token)
    return "[" + ",".join(items) + "]"


def relaxed_json(text):
    """Palace's configuration syntax to JSON: no // or /* */ comments, no trailing commas,
    integer ranges in arrays expanded ([1-3, 5] is [1, 2, 3, 5]), as Palace's own reader."""
    parts, code, i, n = [], [], 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            parts.append(("code", "".join(code)))
            parts.append(("string", text[i : j + 1]))
            code, i = [], j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            code.append(char)
            i += 1
    parts.append(("code", "".join(code)))
    out = []
    for kind, chunk in parts:
        if kind == "code":
            chunk = re.sub(r",+(\s*[}\]])", r"\1", chunk)
            chunk = RANGE_ARRAY_RE.sub(_expand_ranges, chunk)
        out.append(chunk)
    return json.loads("".join(out))


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def set_path(config, key, value):
    """``config[a][b]... = value`` for the dotted ``key`` (a number indexes a list)."""
    if not KEY_RE.match(key):
        raise ValueError("bad key %r" % key)
    node, parts = config, key.split(".")
    for n, part in enumerate(parts):
        last = n == len(parts) - 1
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                raise ValueError("%s: %r is not an index of a list of %d" % (key, part, len(node)))
            part = int(part)
        elif not isinstance(node, dict):
            raise ValueError("%s: %r is not inside an object" % (key, part))
        if last:
            node[part] = value
        else:
            if isinstance(node, dict) and part not in node:
                node[part] = {}
            node = node[part]


def effective_config(path, sets, out_dir):
    """The configuration to solve: ``sets`` applied, output in ``out_dir/postpro``, the mesh path
    absolute (relative to the configuration's directory)."""
    config = relaxed_json(Path(path).read_text())
    for key, value in sets:
        set_path(config, key, value)
    config.setdefault("Problem", {})["Output"] = str(out_dir / "postpro")
    model = config.get("Model") or {}
    mesh = model.get("Mesh")
    if isinstance(mesh, str) and not os.path.isabs(mesh):
        model["Mesh"] = str((Path(path).resolve().parent / mesh).resolve())
    return config


# --- what Palace reports


def palace_binary(given):
    if given:
        return given
    if os.environ.get("PALACE_BIN"):
        return os.environ["PALACE_BIN"]
    found = sorted(glob.glob(str(PALACE_HOME / "bin" / "palace-*.bin")))
    if len(found) != 1:
        raise FileNotFoundError("no single palace-*.bin in %s/bin: %s" % (PALACE_HOME, found))
    return found[0]


def parse_log(lines):
    """AMR iterations ({iteration, indicator, unknowns}) and the finest unknowns of every
    assembly, from Palace's log."""
    amr, assemblies, pending, completed = [], [], None, None
    in_levels, finest = False, None
    for line in lines:
        level = LEVEL_RE.match(line)
        if level:
            if not level.group(2):
                finest = int(level.group(4))
            in_levels = True
            continue
        if in_levels:
            if finest is not None:
                assemblies.append(finest)
            in_levels, finest = False, None
        match = AMR_ITER_RE.search(line)
        if match:
            pending = int(match.group(1))
            continue
        match = AMR_DONE_RE.search(line)
        if match:
            completed = int(match.group(1))
            pending = "done"
            continue
        match = INDICATOR_RE.search(line)
        if match and pending is not None:
            entry = {"indicator": float(match.group(1)), "unknowns": int(match.group(2))}
            if pending == "done":
                completed = {"iterations": completed, **entry}
            else:
                amr.append(dict(iteration=pending, **entry))
            pending = None
    if in_levels and finest is not None:
        assemblies.append(finest)
    return {"amr": amr, "amr_completed": completed, "assembled_unknowns": assemblies}


def palace_metadata(post_dir):
    """The numbers of ``palace.json`` the record keeps (None without the file)."""
    try:
        meta = json.loads((post_dir / "palace.json").read_text())
    except (OSError, ValueError):
        return None
    problem = meta.get("Problem") or {}
    linear = meta.get("LinearSolver") or {}
    durations = (meta.get("ElapsedTime") or {}).get("Durations") or {}
    peak = meta.get("PeakMemoryMegabytes") or {}
    node = meta.get("PeakNodeMemoryMegabytes") or {}
    return {
        "git_tag": meta.get("GitTag"),
        "mpi_size": problem.get("MPISize"),
        "dofs": problem.get("DegreesOfFreedom"),
        "multigrid_dofs": problem.get("MultigridDegreesOfFreedom"),
        "mesh_elements": problem.get("MeshElements"),
        # The solves of an adaptive refinement, the first one included (1 without refinement).
        "adaptation_solves": problem.get("Iteration"),
        "linear_solves": linear.get("TotalSolves"),
        "linear_iterations": linear.get("TotalIts"),
        "elapsed_s": durations.get("Total"),
        "durations_s": durations,
        "peak_memory_mb_max_rank": peak.get("Max"),
        "peak_memory_mb_sum": peak.get("Total"),
        "peak_node_memory_mb_sum": node.get("Total"),
    }


def read_port_s(path):
    """{frequency: {(i, j): complex S}} of a Palace ``port-S.csv``."""
    with open(path, newline="") as handle:
        rows = [[cell.strip() for cell in row] for row in csv.reader(handle) if row]
    header, out = rows[0], {}
    columns = []
    for c, name in enumerate(header):
        match = S_MAG_RE.match(name)
        if match:
            ij = (int(match.group(1)), int(match.group(2)))
            phase = header.index("arg(S[%d][%d]) (deg.)" % ij)
            columns.append((ij, c, phase))
    for row in rows[1:]:
        values = {}
        for ij, mag, phase in columns:
            amplitude = 10.0 ** (float(row[mag]) / 20.0)
            angle = math.radians(float(row[phase]))
            values[ij] = complex(amplitude * math.cos(angle), amplitude * math.sin(angle))
        out[round(float(row[0]), 9)] = values
    return out


def compare_port_s(actual_path, reference_path, tol):
    actual, reference = read_port_s(actual_path), read_port_s(reference_path)
    worst, where, compared = 0.0, None, 0
    for freq, ref in reference.items():
        got = actual.get(freq)
        if got is None:
            return {"ok": False, "error": "no row at %g GHz" % freq, "tol": tol}
        for ij, value in ref.items():
            if ij not in got:
                return {"ok": False, "error": "no S[%d][%d]" % ij, "tol": tol}
            delta = abs(got[ij] - value)
            compared += 1
            if delta > worst:
                worst, where = delta, {"f_ghz": freq, "s": "S[%d][%d]" % ij}
    return {
        "ok": worst <= tol,
        "max_abs_ds": worst,
        "at": where,
        "values": compared,
        "frequencies": len(reference),
        "tol": tol,
    }


def cpu_info():
    """The CPU's model name (``/proc/cpuinfo``; Python's platform.processor() says x86_64 on
    Linux), its AVX2 and AVX-512 support and the CPU count."""
    model, flags = None, set()
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name" and not model:
            model = value.strip()
        elif key.strip() == "flags" and not flags:
            flags = set(value.split())
    if not text:
        return {
            "model": platform.processor() or None,
            "avx2": None,
            "avx512f": None,
            "count": os.cpu_count(),
        }
    return {
        "model": model or platform.processor() or None,
        "avx2": "avx2" in flags,
        "avx512f": "avx512f" in flags,
        "count": os.cpu_count(),
    }


def mem_total_gb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1048576.0, 2)
    except (OSError, ValueError, IndexError):
        pass
    return None


def image_build():
    out = {}
    for name in ("palace.version", "palace.commit", "arch-flags.txt"):
        try:
            out[name] = (PALACE_HOME / name).read_text().strip()
        except OSError:
            out[name] = None
    return out


# --- the run


class Run:
    """The task's log (``out/ID.log``) and its stages."""

    def __init__(self, log_path):
        self.log = open(log_path, "w")
        self.tail = deque(maxlen=TAIL_LINES)
        self.stages = {}

    def stage(self, name, argv, cwd=None, env=None, keep=None):
        """Run ``argv``, its output into the log; {exit, wall_s} under ``name``."""
        self.log.write("== %s: %s\n" % (name, " ".join(argv)))
        self.log.flush()
        start = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                text=True,
                errors="replace",
            )
        except OSError as err:
            self.log.write("%s\n" % err)
            self.stages[name] = {"exit": 127, "wall_s": 0.0, "error": str(err)}
            return 127
        for line in proc.stdout:
            self.log.write(line)
            self.tail.append(line)
            if keep is not None:
                keep.append(line)
        code = proc.wait()
        self.stages[name] = {"exit": code, "wall_s": round(time.time() - start, 3)}
        return code

    def note(self, text):
        self.log.write(text + "\n")
        self.tail.append(text + "\n")

    def close(self, code):
        self.log.write("exit %d\n" % code)
        self.log.close()


def subst(text, out_dir, ranks):
    return text.replace("{out}", str(out_dir)).replace("{ranks}", str(ranks))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--id", required=True, help="the model run's name (out/ID, out/ID.log)")
    ap.add_argument("--ranks", type=int, required=True, help="MPI ranks")
    ap.add_argument("--bind", choices=sorted(BIND_FLAGS), default="core")
    ap.add_argument(
        "--set", action="append", default=[], help="Dotted.Key=VALUE in the configuration"
    )
    ap.add_argument("--prepare", help="a JSON list: a script and its arguments, run first")
    ap.add_argument("--reference", help="a directory with the expected port-S.csv")
    ap.add_argument("--reference-tol", type=float, default=0.01, help="max |dS| (default 0.01)")
    ap.add_argument("--dry-run-only", action="store_true", help="check the configuration only")
    ap.add_argument("--palace", help="the Palace binary (default: /opt/palace/bin/palace-*.bin)")
    ap.add_argument("--mpirun", default="mpirun")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- CONFIG")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if len(command) != 1:
        ap.error("one configuration file after --")
    if args.ranks < 1:
        ap.error("--ranks is at least 1")
    try:
        sets = []
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep or not KEY_RE.match(key):
                raise ValueError("--set %r is not Dotted.Key=VALUE" % item)
            sets.append((key, parse_value(value)))
        prepare = json.loads(args.prepare) if args.prepare else None
        if prepare is not None and (
            not isinstance(prepare, list)
            or not prepare
            or not all(isinstance(a, str) for a in prepare)
        ):
            raise ValueError("--prepare is a JSON list of strings")
    except ValueError as err:
        ap.error(str(err))
    out_rel = Path("out") / args.id
    out_dir = Path.cwd() / out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(subst(command[0], out_rel, args.ranks))
    reference = Path(subst(args.reference, out_rel, args.ranks)) if args.reference else None
    env = dict(os.environ)
    for key, value in MPI_ENV.items():
        env.setdefault(key, value)
    run = Run(Path("out") / ("%s.log" % args.id))
    start, cpu0 = time.time(), resource.getrusage(resource.RUSAGE_CHILDREN)
    record = {
        "schema": "yapnr-palace-job-v1",
        "id": args.id,
        "config": str(command[0]),
        "ranks": args.ranks,
        "bind": args.bind,
        "sets": {k: v for k, v in sets},
        "prepare": prepare,
        "reference": None,
        "palace": None,
        "attempt": os.environ.get("YAPNR_ATTEMPT"),
    }
    failed, code, solve_lines = None, 0, []
    if prepare:
        argv_prep = [sys.executable] + [subst(a, out_rel, args.ranks) for a in prepare]
        code = run.stage("prepare", argv_prep, env=env)
        failed = "prepare" if code else None
    if not failed:
        try:
            binary = palace_binary(args.palace)
            config = effective_config(config_path, sets, out_dir)
            (out_dir / "config.json").write_text(json.dumps(config, indent=1) + "\n")
        except (OSError, ValueError) as err:
            run.note("palace_job: %s" % err)
            failed, code = "config", 2
    if not failed:
        cwd = str(config_path.resolve().parent)
        version_lines = []
        run.stage("version", [binary, "--version"], cwd=cwd, env=env, keep=version_lines)
        record["palace_version"] = "".join(version_lines).strip() or None
        code = run.stage("dry_run", [binary, "--dry-run", str(out_dir / "config.json")], cwd, env)
        failed = "dry_run" if code else None
    if not failed and not args.dry_run_only:
        argv_solve = [args.mpirun, "-np", str(args.ranks)] + BIND_FLAGS[args.bind]
        argv_solve += [binary, str(out_dir / "config.json")]
        code = run.stage("solve", argv_solve, cwd=cwd, env=env, keep=solve_lines)
        failed = "solve" if code else None
        record["palace"] = palace_metadata(out_dir / "postpro")
        record.update(parse_log(solve_lines))
        if reference is not None and not failed:
            try:
                record["reference"] = dict(
                    compare_port_s(
                        out_dir / "postpro" / "port-S.csv",
                        reference / "port-S.csv",
                        args.reference_tol,
                    ),
                    dir=str(reference),
                )
            except (OSError, ValueError, IndexError, KeyError) as err:
                record["reference"] = {"ok": False, "error": str(err), "dir": str(reference)}
            run.note("reference: %s" % json.dumps(record["reference"], sort_keys=True))
    run.close(code)
    wall = time.time() - start
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    sys.stdout.write("".join(run.tail))
    reference_ok = record["reference"] is None or bool(record["reference"].get("ok"))
    record.update(
        ok=not failed and reference_ok,
        exit=code,
        failed=failed or (None if reference_ok else "reference"),
        stages=run.stages,
        wall_s=round(wall, 3),
        user_s=round(usage.ru_utime - cpu0.ru_utime, 3),
        sys_s=round(usage.ru_stime - cpu0.ru_stime, 3),
        max_rss_mb=round(usage.ru_maxrss / 1024.0, 1),
        cpu=cpu_info(),
        mem_total_gb=mem_total_gb(),
        image=image_build(),
    )
    record_path = Path("out") / ("%s.job.json" % args.id)
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, record_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
