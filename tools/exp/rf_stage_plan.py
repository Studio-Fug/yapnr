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
    threads = 4                    # the FDTD threads (YAPNR_RF_THREADS); cpus (cores) follow
    memory_gb = 8
    disk_gb = 10
    max_wall_s = 14400
    require_native = true          # YAPNR_RF_REQUIRE_NATIVE=1: no silent numpy fallback
    # backend = "native"           # YAPNR_RF_BACKEND (auto, native, numpy, torch); unset: spec's
    # dtype = "float64"            # YAPNR_RF_DTYPE (float64, float32); unset: the spec's
    # env = {YAPNR_RF_TBLOCK = "auto"}   # more YAPNR_RF_* settings of the native kernel

    [placement]                    # optional, copied into the campaign (vm_vcpus, shape...)
    vm_vcpus = 8

    [[jobs]]
    id = "divider"
    case = "divider"

    [[jobs]]
    id = "d1-star"
    case = "divider"
    spec = "specs/d1.yaml"         # relative to the jobs file
    criteria = "specs/d1-criteria.yaml"  # the spec's own band: criteria and dense frequencies
    seed = "star"
    max_iterations = 60
    args = ["--no-fine"]           # more options of `yapnr.rf.cases run`

    [[jobs]]
    id = "d1-star-fine"
    case = "divider"
    validate = "fetched/d1-star"   # re-validate a finished (fetched) run directory
    criteria = "specs/d1-criteria.yaml"
    args = ["--finer", "0"]        # more options of `yapnr.rf.cases validate`

    [[jobs]]
    id = "diag"
    diagnostic = true              # tools/exp/rf_diag.py: what the image offers yapnr.rf

``DIR`` receives the source bundle (``git archive`` of the committed ``REV`` of ``CHECKOUT``,
never its working tree), the job bundle (the runner, the diagnostic, the spec and criteria
files), copies of the run directories to re-validate, the stage plan, ``campaign.toml`` and
``manifest.json``; then ``yapnr exp plan DIR/campaign.toml``.

Every RF task runs niced in the image with ``PYTHONPATH=src``, ``OMP_NUM_THREADS``,
``MKL_NUM_THREADS`` and ``YAPNR_RF_THREADS`` (the native FDTD pool, re-validations included) at
its threads, ``OPENBLAS_NUM_THREADS=1`` (docs/rf-inverse-design.md) and, unless a job says
``require_native = false``, ``YAPNR_RF_REQUIRE_NATIVE=1``: the image's native library is built
from the C sources of the commit it was built from, and the loader refuses it for a bundle whose
sources differ, which without this variable would run the numpy reference 12-60 times slower
(docs/rf-solver-backends.md); the task then fails at its first simulation instead, and the
diagnostic's ``ok`` requires the library. ``backend``, ``dtype`` and ``env`` (other
``YAPNR_RF_*`` variables) override the spec's solver settings per job. ``--image-commit REV``
(the image's source revision) checks before anything is uploaded that the bundle's C sources
are that commit's (an error when a job requires native). Each task records
``out/<id>/validation.json`` (verdict: its ``ok``). A run is resumable: the run
directory's top-level files (``checkpoint.npz``, ``history.json``...) are synced to the store and
restored after a Spot preemption, and the driver resumes from them. A run longer than one task
attempt goes on over several: ``attempt_s`` (default ``max_wall_s`` less the larger of 120 s and
a 24th of it; 0: off) makes the runner exit 75 with its checkpoint synced before the wrapper's
limit, which the backend retries (``limits.max_retries`` times per submission; ``submit`` again
resumes after that), and ``end_s`` (default a third of ``attempt_s``, at most 3600 s) is the time
the end (binarize, export, re-validate) needs, which it starts only with that much left.
``--plain-lines`` leaves out the line keys that need a recent ``mc-eval`` (``checkpoint``,
``prune``, ``verdict``) and the attempts: such tasks start again after a preemption.
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
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
    "criteria",
    "seed",
    "threads",
    "max_iterations",
    "smoke",
    "args",
    "labels",
    "attempt_s",
    "end_s",
    "validate",
    "diagnostic",
    "backend",
    "dtype",
    "require_native",
    "env",
} | set(RESOURCE_KEYS)
# What only an optimization takes; a `validate` job re-validates the run directory's spec.
RUN_ONLY_KEYS = ("spec", "seed", "max_iterations", "attempt_s", "end_s")
DEFAULTS = {"threads": 4, "memory_gb": 8, "disk_gb": 10, "max_wall_s": 14400, "args": []}
DIAGNOSTIC_RESOURCES = {"cpus": 1, "memory_gb": 2, "disk_gb": 4, "max_wall_s": 600}
# The solver's environment (yapnr/rf/fdtd/native_kernel.py, docs/rf-solver-backends.md): the keys
# that set some of it, and what `env` may set besides.
BACKENDS = ("auto", "native", "numpy", "torch")
DTYPES = ("float64", "float32", "f64", "f32")
ENV_REQUIRE, ENV_BACKEND, ENV_DTYPE, ENV_THREADS = (
    "YAPNR_RF_REQUIRE_NATIVE",
    "YAPNR_RF_BACKEND",
    "YAPNR_RF_DTYPE",
    "YAPNR_RF_THREADS",
)
ENV_BY_KEY = {ENV_REQUIRE: "require_native", ENV_BACKEND: "backend", ENV_DTYPE: "dtype",
              ENV_THREADS: "threads"}  # fmt: skip
ENV_RE = re.compile(r"^YAPNR_RF_[A-Z0-9_]+$")
# The native library's C sources, whose sha256 the loader compares with the library's.
NATIVE_SOURCES = ("yapnr/rf/fdtd/native/fdtd.c", "yapnr/rf/fdtd/native/fdtd_kernels.h")
# The engine of a bundle from before the native kernel set torch's threads to min(cap,
# solver.threads) (yapnr/rf/fdtd/engine.py); a later engine runs native by default, whose pool
# takes the job's threads (solver.threads, or YAPNR_RF_THREADS), and does not match.
THREAD_CAP_RE = re.compile(r"set_num_threads\(\s*max\(\s*1\s*,\s*min\(\s*(\d+)\s*,")
GIT_TIMEOUT_S = 120
# A run's attempt ends this long (or a 24th of max_wall_s) before the wrapper's limit; the end
# (binarize, export, re-validate) starts only with END_S (or a third of the attempt) left.
ATTEMPT_MARGIN_S = 120
END_S = 3600
RUN_FILES_LEFT_OUT = ("validation.json",)
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


def copy_run_dir(src: Path, dest: Path) -> str:
    """A finished run directory's top-level files (not its report) for a re-validation input."""
    if not (src / "spec.json").is_file():
        raise JobError("%s is not a run directory (no spec.json)" % src)
    dest.mkdir(parents=True)
    for path in sorted(src.iterdir()):
        if path.is_file() and not path.name.startswith(".") and path.name not in RUN_FILES_LEFT_OUT:
            shutil.copyfile(path, dest / path.name)
    return "runs/" + dest.name


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
        for key in ("spec", "criteria"):
            if key in job and not str(job[key]).endswith(SPEC_SUFFIXES):
                errors.append("%s: %s is a .yaml, .yml or .json file" % (where, key))
        if "validate" in job:
            if not isinstance(job["validate"], str):
                errors.append("%s: validate is a run directory" % where)
            errors += [
                "%s: %s does not go with validate" % (where, key)
                for key in RUN_ONLY_KEYS
                if key in job
            ]
        for key in ("attempt_s", "end_s"):
            value = job.get(key, doc.get("defaults", {}).get(key, 0))
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append("%s: %s is a whole number of seconds" % (where, key))
        args = job.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.append("%s: args is a list of strings" % where)
    defaults = doc.get("defaults", {})
    known = solver_env_errors(defaults)
    errors += ["defaults: %s" % e for e in known]
    for n, job in enumerate(jobs):
        if isinstance(job, dict):
            errors += [
                "jobs[%d]: %s" % (n, e)
                for e in solver_env_errors(dict(defaults, **job))
                if e not in known
            ]
    return errors


def solver_env_errors(merged: Mapping[str, Any]) -> List[str]:
    """What is wrong with the solver settings (`backend`, `dtype`, `require_native`, `env`) of a
    job merged over the defaults."""
    errors = []
    backend, dtype = merged.get("backend"), merged.get("dtype")
    if backend is not None and backend not in BACKENDS:
        errors.append("backend is one of %s" % ", ".join(BACKENDS))
    if dtype is not None and dtype not in DTYPES:
        errors.append("dtype is float64 or float32")
    require = merged.get("require_native", True)
    if not isinstance(require, bool):
        errors.append("require_native is true or false")
    elif require and backend in ("numpy", "torch"):
        errors.append("backend %s does not go with require_native (set it false)" % backend)
    env = merged.get("env", {})
    if not isinstance(env, dict):
        return errors + ["env is a table of YAPNR_RF_* variables"]
    for name, value in sorted(env.items()):
        if name in ENV_BY_KEY:
            errors.append("env: %s is set by the key %r" % (name, ENV_BY_KEY[name]))
        elif not ENV_RE.match(str(name)):
            errors.append("env: %s is not a YAPNR_RF_* variable" % name)
        elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
            errors.append("env: %s is a string or a number" % name)
    return errors


def solver_env(merged: Mapping[str, Any], threads: int) -> Dict[str, str]:
    """The solver's environment of a job (its settings over the defaults, already merged)."""
    env = {ENV_THREADS: str(threads)}
    if merged.get("require_native", True):
        env[ENV_REQUIRE] = "1"
    if merged.get("backend") is not None:
        env[ENV_BACKEND] = str(merged["backend"])
    if merged.get("dtype") is not None:
        env[ENV_DTYPE] = str(merged["dtype"])
    env.update({str(k): str(v) for k, v in merged.get("env", {}).items()})
    return env


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
            "env": dict(
                {"PYTHONPATH": "src", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
                **({ENV_REQUIRE: "1"} if dict(defaults, **job).get("require_native", True) else {}),
            ),
            "resources": resources,
            "record": record,
            "verdict": {"file": record, "json_path": "ok"},
            "labels": dict(labels, job="diagnostic"),
        }
        return line if extended else plain(line)
    merged = dict(DEFAULTS, **defaults)
    merged.update(job)
    validating = bool(job.get("validate"))
    if validating:  # the run directory's spec: nothing of the optimization from the defaults
        merged = {k: v for k, v in merged.items() if k not in RUN_ONLY_KEYS or k in job}
    threads = int(merged["threads"])
    flags: List[str] = []
    if merged.get("smoke"):
        flags.append("--smoke")
    if merged.get("max_iterations") is not None:
        flags += ["--max-iterations", str(int(merged["max_iterations"]))]
    flags += list(merged.get("args", []))
    runner_opts: List[str] = []
    if validating:
        runner_opts += ["--validate-from", "run"]
        inputs.append({"dest": "run", "path": job["validate"]})
    if job.get("spec"):
        runner_opts += ["--spec", job["spec"]]
    if job.get("criteria"):
        runner_opts += ["--criteria", job["criteria"]]
    if merged.get("seed") is not None:
        runner_opts += ["--seed", str(merged["seed"])]
    if not validating and ("threads" in job or "threads" in defaults):
        runner_opts += ["--threads", str(threads)]
    resources = {k: merged[k] for k in RESOURCE_KEYS if k in merged}
    resources.setdefault("cpus", threads)
    if extended and not validating:
        attempt_s, end_s = attempt_times(merged, int(resources["max_wall_s"]))
        if attempt_s:
            runner_opts += ["--attempt-s", str(attempt_s), "--end-s", str(end_s)]
    if runner_opts:
        command = ["${PYTHON}", "job/" + RUNNER, job["case"], "--out", out] + runner_opts
        inputs.append({"dest": "job", "path": "job"})
    else:
        command = ["${PYTHON}", "-m", "yapnr.rf.cases", "run", job["case"], "--out", out]
    record = out + "/validation.json"
    line = {
        "id": jid,
        "command": command + flags,
        "inputs": inputs,
        "env": dict(
            {
                "PYTHONPATH": "src",
                "OMP_NUM_THREADS": str(threads),
                "MKL_NUM_THREADS": str(threads),
                "OPENBLAS_NUM_THREADS": "1",
            },
            **solver_env(merged, threads),
        ),
        "resources": resources,
        "record": record,
        "summary": [jid + "/result.json", jid + "/spec.json"],
        "prune": [jid + "/cache"],
        "verdict": {"file": record, "json_path": "ok"},
        "labels": dict(labels, case=job["case"], job="validate" if validating else "run"),
    }
    if not validating:  # a re-validation is not resumable: it starts again after a preemption
        line["checkpoint"] = {"path": out, "sync_every_s": int(sync_every_s)}
    return line if extended else plain(line)


def attempt_times(merged: Mapping[str, Any], max_wall_s: int) -> Tuple[int, int]:
    """(attempt_s, end_s) of a run: 0 when one attempt is all it gets."""
    attempt_s = merged.get("attempt_s")
    if attempt_s is None:
        attempt_s = max_wall_s - max(ATTEMPT_MARGIN_S, max_wall_s // 24)
    attempt_s = int(attempt_s)
    if attempt_s <= 0:
        return 0, 0
    if attempt_s >= max_wall_s:
        raise JobError("attempt_s %d is not below max_wall_s %d" % (attempt_s, max_wall_s))
    end_s = merged.get("end_s")
    end_s = int(min(END_S, attempt_s // 3) if end_s is None else end_s)
    if end_s >= attempt_s:
        raise JobError("end_s %d is not below attempt_s %d" % (end_s, attempt_s))
    return attempt_s, end_s


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


def native_sources_sha256(repo: Path, commit: str) -> Optional[str]:
    """sha256 of the native FDTD library's C sources at `commit`, as the loader computes it
    (`native_kernel.source_sha256`); None for a commit from before the native kernel."""
    try:
        return hashlib.sha256(
            b"".join(git(repo, "show", "%s:%s" % (commit, p)) for p in NATIVE_SOURCES)
        ).hexdigest()
    except subprocess.CalledProcessError:
        return None


def generate(
    jobs_path: Path,
    repo: Path,
    commit: str,
    out: Path,
    extended: bool = True,
    image_commit: Optional[str] = None,
) -> Dict[str, Any]:
    doc = load_jobs(jobs_path)
    errors = check_jobs(doc)
    if errors:
        raise JobError("; ".join(errors))
    full = git(repo, "rev-parse", "--verify", "%s^{commit}" % commit).decode().strip()
    native_sha = native_sources_sha256(repo, full)
    defaults = dict(doc.get("defaults", {}))
    requiring = [j["id"] for j in doc["jobs"] if dict(defaults, **j).get("require_native", True)]
    image_sha = None
    if image_commit is not None:
        image_full = git(repo, "rev-parse", "--verify", "%s^{commit}" % image_commit)
        image_sha = native_sources_sha256(repo, image_full.decode().strip())
        if image_sha != native_sha and requiring:
            raise JobError(
                "the bundle's native C sources (%s) are not the image's (%s at %s), so the"
                " image's library would be refused; jobs %s require it (build the bundle from"
                " a commit with the image's sources, or set require_native = false)"
                % (
                    (native_sha or "none")[:12],
                    (image_sha or "none")[:12],
                    image_commit,
                    ", ".join(requiring),
                )
            )
    paths = list(doc.get("paths", ["yapnr"]))
    out.mkdir(parents=True, exist_ok=True)
    bundle = source_bundle(repo, full, paths, out / "bundles" / ("src-%s.tar.gz" % full[:12]))
    src = "bundles/" + bundle["path"]
    job_dir = out / "job"
    shutil.rmtree(job_dir, ignore_errors=True)
    (job_dir / "specs").mkdir(parents=True)
    for name in (RUNNER, DIAGNOSTIC):
        shutil.copyfile(HERE / name, job_dir / name)
    cap = thread_cap(repo, full)
    warnings, lines, resolved = [], [], []
    runs_dir = out / "runs"
    shutil.rmtree(runs_dir, ignore_errors=True)
    for job in doc["jobs"]:
        job = dict(job)
        for key, folder in (("spec", "specs"), ("criteria", "criteria")):
            if job.get(key):
                src_file = (jobs_path.parent / job[key]).resolve()
                name = "%s/%s%s" % (folder, job["id"], src_file.suffix)
                (job_dir / folder).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_file, job_dir / name)
                job[key] = "job/" + name
        if job.get("validate"):
            job["validate"] = copy_run_dir(
                (jobs_path.parent / job["validate"]).resolve(), runs_dir / job["id"]
            )
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
        if job.get("spec") and not job.get("criteria"):
            warnings.append(
                "%s: a spec of its own, judged by the %s case's criteria and frequencies; give"
                " criteria unless it is in that case's band" % (job["id"], job["case"])
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
        "native_sources_sha256": native_sha,
        "image_commit": image_commit,
        "image_native_sources_sha256": image_sha,
        "require_native": requiring,
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
        "--image-commit",
        help="the image's source revision: refuse a bundle whose native C sources differ from"
        " it while a job requires the native library",
    )
    ap.add_argument(
        "--plain-lines",
        dest="extended",
        action="store_false",
        help="no checkpoint, prune or verdict keys (for an mc-eval that refuses them)",
    )
    args = ap.parse_args(argv)
    try:
        manifest = generate(
            args.jobs, args.repo, args.commit, args.out, args.extended, args.image_commit
        )
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
