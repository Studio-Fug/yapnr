#!/usr/bin/env python3
"""Turn a list of RF jobs into an ``mc-eval`` campaign: one ``yapnr.rf`` run per task.

    python3 tools/exp/rf_stage_plan.py JOBS.toml --repo CHECKOUT [--commit REV] --out DIR
                                       [--plain-lines]

``JOBS.toml`` (or ``.json``) lists the runs; each names a preset case and optionally a spec
file that replaces the case's spec (the case's criteria still judge it), the starting design
(``seed``), an iteration cap, the threads and the task's resources::

    name = "rf-divider"            # the campaign name
    image = "edge"                 # optional; visibility, determinism, sync_every_s too
    paths = ["yapnr"]              # what the source bundle holds (default)

    [defaults]                     # every job's defaults (these are the built-in ones)
    threads = 4                    # torch and OpenMP threads; cpus (physical cores) follow
    memory_gb = 8
    disk_gb = 10
    max_wall_s = 14400

    [placement]                    # optional, copied into the campaign (vm_vcpus, shape...)
    vm_vcpus = 8

    [[jobs]]
    id = "divider"
    case = "divider"

    [[jobs]]
    id = "d1-star"
    case = "divider"
    spec = "specs/d1.yaml"         # relative to the jobs file
    seed = "star"
    max_iterations = 60
    args = ["--no-fine"]           # more options of `yapnr.rf.cases run`

    [[jobs]]
    id = "diag"
    diagnostic = true              # tools/exp/rf_diag.py: what the image offers yapnr.rf

``DIR`` receives the source bundle (``git archive`` of the committed ``REV`` of ``CHECKOUT``,
never its working tree), the job bundle (the runner, the diagnostic and the spec files), the
stage plan, ``campaign.toml`` and ``manifest.json``; then ``yapnr exp plan DIR/campaign.toml``.

Every RF task runs niced in the image with ``PYTHONPATH=src``, ``OMP_NUM_THREADS`` and
``MKL_NUM_THREADS`` at its threads and ``OPENBLAS_NUM_THREADS=1`` (docs/rf-inverse-design.md),
records ``out/<id>/validation.json`` (verdict: its ``ok``) and is resumable: the run
directory's top-level files (``checkpoint.npz``, ``history.json``...) are synced to the store and
restored after a Spot preemption, and the driver resumes from them. ``--plain-lines`` leaves out
the line keys that need a recent ``mc-eval`` (``checkpoint``, ``prune``, ``verdict``): such tasks
start again after a preemption.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    import tomllib
except ImportError:  # Python < 3.11: JSON job files only
    tomllib = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
RUNNER, DIAGNOSTIC = "rf_job.py", "rf_diag.py"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SPEC_SUFFIXES = (".yaml", ".yml", ".json")
TOP_KEYS = {
    "name",
    "image",
    "visibility",
    "determinism",
    "paths",
    "sync_every_s",
    "defaults",
    "placement",
    "jobs",
}
RESOURCE_KEYS = ("cpus", "memory_gb", "disk_gb", "max_wall_s")
JOB_KEYS = {
    "id",
    "case",
    "spec",
    "seed",
    "threads",
    "max_iterations",
    "smoke",
    "args",
    "labels",
    "diagnostic",
} | set(RESOURCE_KEYS)
DEFAULTS = {"threads": 4, "memory_gb": 8, "disk_gb": 10, "max_wall_s": 14400, "args": []}
DIAGNOSTIC_RESOURCES = {"cpus": 1, "memory_gb": 2, "disk_gb": 4, "max_wall_s": 600}
# The engine sets torch's threads to min(cap, solver.threads) (yapnr/rf/fdtd/engine.py).
THREAD_CAP_RE = re.compile(r"set_num_threads\(\s*max\(\s*1\s*,\s*min\(\s*(\d+)\s*,")
GIT_TIMEOUT_S = 120
# Stage-line keys an mc-eval before resumable lines refuses.
EXTENDED_KEYS = ("checkpoint", "prune", "verdict")


class JobError(ValueError):
    pass


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=GIT_TIMEOUT_S
    ).stdout


def load_jobs(path: Path) -> Dict[str, Any]:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    if tomllib is None:
        raise JobError("TOML job files need Python 3.11 or later")
    return tomllib.loads(path.read_text())


def source_bundle(repo: Path, commit: str, paths: List[str], dest: Path) -> Dict[str, Any]:
    """``git archive`` of ``commit``'s ``paths``, gzipped without a time stamp (reproducible)."""
    raw = git(repo, "archive", "--format=tar", commit, "--", *paths)
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
        gz.write(raw)
    data = out.getvalue()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"path": dest.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def thread_cap(repo: Path, commit: str) -> Optional[int]:
    try:
        text = git(repo, "show", "%s:yapnr/rf/fdtd/engine.py" % commit).decode()
    except subprocess.CalledProcessError:
        return None
    match = THREAD_CAP_RE.search(text)
    return int(match.group(1)) if match else None


def check_jobs(doc: Mapping[str, Any]) -> List[str]:
    errors = ["unknown key %r" % k for k in sorted(set(doc) - TOP_KEYS)]
    if not isinstance(doc.get("name"), str) or not NAME_RE.match(doc["name"]):
        errors.append("name is a campaign name ([a-z0-9-], at most 41 characters)")
    errors += [
        "defaults: unknown key %r" % k
        for k in sorted(set(doc.get("defaults", {})) - JOB_KEYS - {"args"})
    ]
    jobs = doc.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return errors + ["jobs is a non-empty list"]
    seen = set()
    for n, job in enumerate(jobs):
        where = "jobs[%d]" % n
        if not isinstance(job, dict):
            errors.append("%s is a table" % where)
            continue
        errors += ["%s: unknown key %r" % (where, k) for k in sorted(set(job) - JOB_KEYS)]
        if not isinstance(job.get("id"), str) or not ID_RE.match(job["id"]):
            errors.append("%s: id is [a-z0-9._-], at most 63 characters" % where)
        elif job["id"] in seen:
            errors.append("%s: id %r is repeated" % (where, job["id"]))
        else:
            seen.add(job["id"])
        if job.get("diagnostic"):
            continue
        if not isinstance(job.get("case"), str):
            errors.append("%s: case names a preset case (it judges the run)" % where)
        if "spec" in job and not str(job["spec"]).endswith(SPEC_SUFFIXES):
            errors.append("%s: spec is a .yaml, .yml or .json file" % where)
        args = job.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.append("%s: args is a list of strings" % where)
    return errors


def stage_line(
    job: Mapping[str, Any],
    defaults: Mapping[str, Any],
    *,
    src: str,
    commit: str,
    sync_every_s: int,
    extended: bool,
) -> Dict[str, Any]:
    """The stage plan line of one job (``job`` paths already rewritten into the job bundle)."""
    jid = job["id"]
    out = "out/" + jid
    inputs = [{"dest": "src", "path": src}]
    labels = {"src": commit[:12]}
    labels.update({str(k): str(v) for k, v in job.get("labels", {}).items()})
    if job.get("diagnostic"):
        resources = {k: job.get(k, DIAGNOSTIC_RESOURCES[k]) for k in RESOURCE_KEYS}
        record = out + "/diag.json"
        line = {
            "id": jid,
            "command": ["${PYTHON}", "job/" + DIAGNOSTIC, "--out", out, "--src", "src"],
            "inputs": inputs + [{"dest": "job", "path": "job"}],
            "env": {"PYTHONPATH": "src", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
            "resources": resources,
            "record": record,
            "verdict": {"file": record, "json_path": "ok"},
            "labels": dict(labels, job="diagnostic"),
        }
        return line if extended else plain(line)
    merged = dict(DEFAULTS, **defaults)
    merged.update(job)
    threads = int(merged["threads"])
    flags: List[str] = []
    if merged.get("smoke"):
        flags.append("--smoke")
    if merged.get("max_iterations") is not None:
        flags += ["--max-iterations", str(int(merged["max_iterations"]))]
    flags += list(merged.get("args", []))
    runner_opts: List[str] = []
    if job.get("spec"):
        runner_opts += ["--spec", job["spec"]]
    if merged.get("seed") is not None:
        runner_opts += ["--seed", str(merged["seed"])]
    if "threads" in job or "threads" in defaults:
        runner_opts += ["--threads", str(threads)]
    if runner_opts:
        command = ["${PYTHON}", "job/" + RUNNER, job["case"], "--out", out] + runner_opts
        inputs.append({"dest": "job", "path": "job"})
    else:
        command = ["${PYTHON}", "-m", "yapnr.rf.cases", "run", job["case"], "--out", out]
    resources = {k: merged[k] for k in RESOURCE_KEYS if k in merged}
    resources.setdefault("cpus", threads)
    record = out + "/validation.json"
    line = {
        "id": jid,
        "command": command + flags,
        "inputs": inputs,
        "env": {
            "PYTHONPATH": "src",
            "OMP_NUM_THREADS": str(threads),
            "MKL_NUM_THREADS": str(threads),
            "OPENBLAS_NUM_THREADS": "1",
        },
        "resources": resources,
        "record": record,
        "summary": [jid + "/result.json", jid + "/spec.json"],
        "prune": [jid + "/cache"],
        "verdict": {"file": record, "json_path": "ok"},
        "labels": dict(labels, case=job["case"]),
    }
    line["checkpoint"] = {"path": out, "sync_every_s": int(sync_every_s)}
    return line if extended else plain(line)


def plain(line: Dict[str, Any]) -> Dict[str, Any]:
    """``line`` without the keys only a recent ``mc-eval`` reads."""
    return {k: v for k, v in line.items() if k not in EXTENDED_KEYS}


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise JobError("cannot write %r to TOML" % (value,))


def campaign_toml(doc: Mapping[str, Any]) -> str:
    lines = [
        "# Written by tools/exp/rf_stage_plan.py; plan it with `yapnr exp plan`.",
        'schema = "yapnr-campaign-v1"',
        'kind = "mc-eval"',
        "name = %s" % toml_value(doc["name"]),
        'source = "none"  # yapnr.rf comes from the source bundle of the stage plan',
        "image = %s" % toml_value(doc.get("image", "edge")),
        "visibility = %s" % toml_value(doc.get("visibility", "public")),
        "determinism = %s" % toml_value(doc.get("determinism", "seeded")),
        "",
        "[config]",
        'stage_plan = "stage.jsonl"',
    ]
    if doc.get("placement"):
        lines += ["", "[placement]"]
        lines += ["%s = %s" % (k, toml_value(v)) for k, v in sorted(doc["placement"].items())]
    return "\n".join(lines) + "\n"


def generate(
    jobs_path: Path, repo: Path, commit: str, out: Path, extended: bool = True
) -> Dict[str, Any]:
    doc = load_jobs(jobs_path)
    errors = check_jobs(doc)
    if errors:
        raise JobError("; ".join(errors))
    full = git(repo, "rev-parse", "--verify", "%s^{commit}" % commit).decode().strip()
    paths = list(doc.get("paths", ["yapnr"]))
    out.mkdir(parents=True, exist_ok=True)
    bundle = source_bundle(repo, full, paths, out / "bundles" / ("src-%s.tar.gz" % full[:12]))
    src = "bundles/" + bundle["path"]
    job_dir = out / "job"
    shutil.rmtree(job_dir, ignore_errors=True)
    (job_dir / "specs").mkdir(parents=True)
    for name in (RUNNER, DIAGNOSTIC):
        shutil.copyfile(HERE / name, job_dir / name)
    defaults = dict(doc.get("defaults", {}))
    cap = thread_cap(repo, full)
    warnings, lines, resolved = [], [], []
    for job in doc["jobs"]:
        job = dict(job)
        if job.get("spec"):
            spec_src = (jobs_path.parent / job["spec"]).resolve()
            spec_name = "specs/%s%s" % (job["id"], spec_src.suffix)
            shutil.copyfile(spec_src, job_dir / spec_name)
            job["spec"] = "job/" + spec_name
        line = stage_line(
            job,
            defaults,
            src=src,
            commit=full,
            sync_every_s=int(doc.get("sync_every_s", 300)),
            extended=extended,
        )
        threads = int(line["env"]["OMP_NUM_THREADS"])
        if cap is not None and threads > cap and not job.get("diagnostic"):
            warnings.append(
                "%s: %d threads, but this commit's engine runs torch on at most %d"
                % (job["id"], threads, cap)
            )
        if line["resources"]["cpus"] < threads:
            warnings.append(
                "%s: %d threads on %d cores" % (job["id"], threads, line["resources"]["cpus"])
            )
        lines.append(line)
        resolved.append(job)
    (out / "stage.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    (out / "campaign.toml").write_text(campaign_toml(doc))
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no", "--", *paths).decode()
    manifest = {
        "schema": "yapnr-rf-stage-plan-v1",
        "commit": full,
        "paths": paths,
        "source_bundle": bundle,
        "uncommitted_changes_left_out": bool(dirty.strip()),
        "engine_thread_cap": cap,
        "resumable": extended,
        "jobs": resolved,
        "warnings": warnings,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("jobs", type=Path, help="the jobs file (TOML or JSON)")
    ap.add_argument("--repo", type=Path, default=Path("."), help="checkout holding yapnr.rf")
    ap.add_argument("--commit", default="HEAD", help="the committed revision to archive")
    ap.add_argument("--out", type=Path, required=True, help="the campaign directory to write")
    ap.add_argument(
        "--plain-lines",
        dest="extended",
        action="store_false",
        help="no checkpoint, prune or verdict keys (for an mc-eval that refuses them)",
    )
    args = ap.parse_args(argv)
    try:
        manifest = generate(args.jobs, args.repo, args.commit, args.out, args.extended)
    except (JobError, OSError, subprocess.SubprocessError) as err:
        print("rf_stage_plan: %s" % err, file=sys.stderr)
        return 2
    for warning in manifest["warnings"]:
        print("warning: %s" % warning, file=sys.stderr)
    print(
        "%d tasks from %s (%s) -> %s"
        % (
            len(manifest["jobs"]),
            manifest["commit"][:12],
            "uncommitted changes left out" if manifest["uncommitted_changes_left_out"] else "clean",
            args.out / "campaign.toml",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
