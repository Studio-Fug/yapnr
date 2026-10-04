#!/usr/bin/env python3
"""Palace models as one ``yapnr exp`` campaign: build the image, plan N models, collect results.

    python3 tools/exp/palace_plan.py image [--tag T] [--run]       # Cloud Build of docker/palace
    python3 tools/exp/palace_plan.py digests [--tag T]              # the built tag's digest
    python3 tools/exp/palace_plan.py plan JOBS.toml --out DIR [--digest D | --offline]
    python3 tools/exp/palace_plan.py collect CID --dest TREE        # after `yapnr exp fetch`

``plan`` turns the models of ``JOBS.toml`` into an ``mc-eval`` campaign with one task per model,
each running a Palace configuration through ``palace_job.py`` (the record, the log, Palace's own
numbers) in the Palace image (docker/palace) on ``ranks`` MPI ranks, ``models_per_vm`` models to
a VM (one by default: a model gets the VM's cores, ranks bound to them)::

    name = "palace-smoke"                      # the campaign name
    image = "palace:b797ea8-x86-64-v3"         # a tag in [gcp] images, or a full reference
    ranks = 8                                  # MPI ranks per model, one per physical core
    models_per_vm = 1                          # models side by side on one VM (ranks unbound)
    packing = "core"                           # "vcpu": a rank per hardware thread
    families = ["c4d"]
    memory_gb = 24                             # per model: picks highcpu or standard shapes
    max_wall_s = 7200                          # per model; Palace does not checkpoint
    visibility = "public"                      # or "private" (opaque ids, private store)
    prune = ["*/postpro/paraview/**"]          # left out of the result archive (optional)

    [inputs]                                   # every task gets these directories, by name
    models = "models"                          # relative to the jobs file
    code = "code"

    [set]                                      # every model: Dotted.Key = value in its config
    "Solver.Order" = 2

    [[jobs]]
    id = "cpw-wave-r8"                         # out/<id>/ (config.json, postpro/), out/<id>.log
    config = "/opt/palace/share/palace/examples/cpw/cpw_wave_uniform.json"
    reference = "/opt/palace/share/palace/regression/cpw/wave_uniform"   # optional port-S check
    # set = {"Solver.Order" = 3}; prepare = ["code/mesh.py", "models/a.json", "--out", "{out}"];
    # ranks, bind, memory_gb, disk_gb, max_wall_s, summary, labels, dry_run_only per job. A job
    # with fewer ranks still reserves the campaign's cores; one that needs more cores or memory
    # gets its own VM shape, and the campaign then runs from instance policies (no template).

    [[jobs]]                                   # refinement, then a sweep of the refined mesh
    id = "tx12-A"
    stages = ["models/tx12-A/palace-amr.json"] # solved first ("CONFIG@1": on one rank)
    mesh_from = "models/tx12-A/palace-amr.json"  # the main solve uses that stage's adapted mesh
    config = "models/tx12-A/palace-sweep.json"

``config`` and ``reference`` are paths in the task: relative to the work directory (an input) or
absolute in the image, ``{out}`` being the model's output directory (a ``prepare`` script writes
the mesh and the configuration there). ``DIR`` receives copies of the input directories, the job
bundle (``palace_job.py``), the stage plan, ``campaign.toml`` (image pinned by digest,
``[runtime]`` for the image's interpreter) and ``manifest.json``. Then::

    yapnr exp plan DIR/campaign.toml --backend gcp-batch
    yapnr exp submit CID && yapnr exp status CID   # until every task is done
    yapnr exp fetch CID [--full]                   # the summary: logs, records, port-S.csv, ...
    python3 tools/exp/palace_plan.py collect CID --dest TREE

``collect`` lays the results out like local runs (``TREE/runs/<id>/``, ``<id>.log``,
``<id>.job.json``) with ``TREE/runs/<cid>.summary.json``: per model the verdict, wall time,
degrees of freedom, AMR refinements and solves, linear iterations, peak memory and the
reference check.

``image`` prints (``--run``: runs) the Cloud Build submit of docker/palace/cloudbuild.yaml, which
pushes ``palace:<tag>-x86-64-v3`` (and its dependency stage, ``palace-deps:<tag>-x86-64-v3``, the
cache of the next build) to ``[gcp] images`` in the home region, built as ``[gcp]
image_build_service_account``. It needs an identity that may submit builds and act as that
account (the project owner); task VMs pull with the runner account.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from tools.exp import image_tasks
except ImportError:  # run as a script: python3 tools/exp/palace_plan.py (its directory on the path)
    import image_tasks  # type: ignore[no-redef]

JobError = image_tasks.JobError

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DOCKER_DIR = REPO / "docker" / "palace"
RUNNER = "palace_job.py"
IMAGE_NAME = "palace"
# The Palace commit of docker/palace (its cloudbuild.yaml _TAG): main after PR 962.
DEFAULT_TAG = "b797ea8"
VARIANTS = ("x86-64-v3",)
RUNTIME = {"python": "/opt/palace/venv/bin/python", "entrypoint": ""}
BINDS = ("core", "hwthread", "none")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")
TOP_KEYS = {
    "name",
    "image",
    "ranks",
    "models_per_vm",
    "packing",
    "bind",
    "families",
    "memory_gb",
    "disk_gb",
    "max_wall_s",
    "visibility",
    "summary",
    "prune",
    "set",
    "inputs",
    "env",
    "jobs",
}
JOB_KEYS = {
    "id",
    "config",
    "stages",
    "mesh_from",
    "reference",
    "reference_tol",
    "prepare",
    "set",
    "ranks",
    "bind",
    "dry_run_only",
    "memory_gb",
    "disk_gb",
    "max_wall_s",
    "summary",
    "labels",
}
DEFAULTS = {
    "ranks": 8,
    "models_per_vm": 1,
    "packing": "core",
    "families": ["c4d"],
    "memory_gb": 24,
    "disk_gb": 10,
    "max_wall_s": 7200,
    "visibility": "public",
    "prune": [],
    # Fetched without --full: the log, the record, the effective configuration and Palace's
    # tables (port-S.csv, ...) and palace.json, not the fields (ParaView) or meshes.
    "summary": [
        "{id}.log",
        "{id}.job.json",
        "{id}/*.json",
        "{id}/postpro/*.csv",
        "{id}/postpro/*.json",
        # stages (--stage): their tables, and every refinement iteration's
        "{id}/stage-*/*.csv",
        "{id}/stage-*/*.json",
        "{id}/stage-*/iteration*/*.csv",
        "{id}/stage-*/iteration*/*.json",
    ],
}
# Spot instance templates exist for these shapes (infra/gcp: template_shapes, and C4 in the
# example's second region through region_template_shapes). A model whose VM shape has a template
# in none of its families runs from an instance policy.
TEMPLATE_SHAPES = (
    "c4d-highcpu-8",
    "c4d-highcpu-16",
    "c4d-standard-16",
    "c4-highcpu-8",
    "c4-highcpu-16",
)


# --- image: the Cloud Build of docker/palace


def source_revision() -> str:
    """The yapnr commit the image is built from (recorded as the image's
    ``org.opencontainers.image.revision``), with ``-dirty`` when docker/palace has uncommitted
    changes; empty outside a git checkout."""
    try:
        rev = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        dirty = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain", "--", str(DOCKER_DIR)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if rev.returncode:
        return ""
    return rev.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def build_command(gcp, tag: str, configuration: Optional[str], asynchronous: bool) -> List[str]:
    return image_tasks.build_command(
        gcp,
        DOCKER_DIR,
        IMAGE_NAME,
        tag,
        configuration,
        asynchronous,
        {"_YAPNR_COMMIT": source_revision()},
    )


def digests(gcp, tag: str, configuration: Optional[str]) -> Dict[str, Dict[str, Any]]:
    return image_tasks.digests(gcp, IMAGE_NAME, tag, VARIANTS, configuration)


# --- plan: N models -> one mc-eval campaign


def _check_model(where: str, item: Mapping[str, Any]) -> List[str]:
    errors = []
    for key in ("ranks", "memory_gb", "disk_gb", "max_wall_s", "reference_tol"):
        if key in item and not image_tasks.positive(item[key]):
            errors.append("%s%s is a positive number" % (where, key))
    if "ranks" in item and int(item["ranks"]) != item["ranks"]:
        errors.append("%sranks is a whole number" % where)
    if "bind" in item and item["bind"] not in BINDS:
        errors.append("%sbind is one of %s" % (where, ", ".join(BINDS)))
    sets = item.get("set", {})
    if not isinstance(sets, dict) or not all(isinstance(k, str) and KEY_RE.match(k) for k in sets):
        errors.append("%sset maps Dotted.Keys of the configuration to values" % where)
    for key in ("summary", "prune", "prepare"):
        value = item.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            errors.append("%s%s is a list of strings" % (where, key))
    return errors


def check_jobs(doc: Mapping[str, Any]) -> List[str]:
    errors = ["unknown key %r" % k for k in sorted(set(doc) - TOP_KEYS)]
    errors += _check_model("", doc)
    if not isinstance(doc.get("name"), str) or not image_tasks.NAME_RE.match(doc["name"]):
        errors.append("name is a campaign name ([a-z0-9-], at most 41 characters)")
    if not isinstance(doc.get("image"), str) or not doc["image"]:
        errors.append("image is a tag in the images repository or a full reference")
    if "models_per_vm" in doc and not image_tasks.positive(doc["models_per_vm"]):
        errors.append("models_per_vm is a positive number")
    if doc.get("packing", "core") not in ("core", "vcpu"):
        errors.append("packing is 'core' or 'vcpu'")
    if doc.get("visibility", "public") not in ("public", "private"):
        errors.append("visibility is 'public' or 'private'")
    families = doc.get("families", ["c4d"])
    if (
        not isinstance(families, list)
        or not families
        or not all(isinstance(f, str) for f in families)
    ):
        errors.append("families is a list of machine families")
    errors += image_tasks.check_inputs_env(doc)
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
        if not isinstance(job.get("id"), str) or not image_tasks.ID_RE.match(job["id"]):
            errors.append("%s: id is [A-Za-z0-9._-], at most 63 characters" % where)
        elif job["id"] in seen:
            errors.append("%s: id %r is repeated" % (where, job["id"]))
        else:
            seen.add(job["id"])
        if not isinstance(job.get("config"), str) or not job["config"]:
            errors.append("%s: config is the Palace configuration's path in the task" % where)
        if "reference" in job and (not isinstance(job["reference"], str) or not job["reference"]):
            errors.append("%s: reference is a directory with port-S.csv" % where)
        stages = job.get("stages", [])
        if not isinstance(stages, list) or not all(isinstance(v, str) and v for v in stages):
            errors.append("%s: stages is a list of configuration paths" % where)
        elif "mesh_from" in job and job["mesh_from"] not in [v.rsplit("@", 1)[0] for v in stages]:
            errors.append("%s: mesh_from names one of the stages" % where)
        if not isinstance(job.get("dry_run_only", False), bool):
            errors.append("%s: dry_run_only is true or false" % where)
        errors += _check_model(where + ": ", job)
    return errors


def bind_of(item: Mapping[str, Any], doc: Mapping[str, Any]) -> str:
    """Ranks bound to cores for one model per VM (to hardware threads with ``vcpu`` packing),
    unbound when several models share a VM: two mpiruns would bind to the same cores."""
    if item.get("bind") or doc.get("bind"):
        return item.get("bind") or doc["bind"]
    if int(doc["models_per_vm"]) > 1:
        return "none"
    return "hwthread" if doc["packing"] == "vcpu" else "core"


def vm_shape(family: str, ranks: int, memory_gb: float, vcpus: int, packing: str) -> str:
    """The machine type ``yapnr exp plan`` will choose (yapnr.exp.cost.choose_shape)."""
    sys.path.insert(0, str(REPO))
    from yapnr.exp import cost

    return cost.choose_shape(cost.PriceTable.load(), family, ranks, memory_gb, vcpus, packing)[0]


def job_cpus(job: Mapping[str, Any], doc: Mapping[str, Any]) -> int:
    """The cores a job reserves: its ranks, but never fewer than the campaign's ``ranks``, so that
    a job with fewer ranks (a rank-scaling run) still has the campaign's VM share to itself rather
    than being packed next to another job whose ranks would bind to the same cores."""
    return max(int(job.get("ranks", doc["ranks"])), int(doc["ranks"]))


def placement(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """K models of R ranks per VM: 2*K*R vCPUs by core (C4D has two threads a core); an instance
    policy when, for any job, no family's shape has an instance template.

    ``yapnr exp`` places each resource class (cores, memory) of the plan on its own shape, but
    the template flag is the campaign's: a job whose ``ranks`` or ``memory_gb`` needs a shape
    without a template (c4d-highmem-16 for 56 GB, a 32-vCPU shape for 16 ranks) makes the whole
    campaign run from instance policies, which every shape has, instead of being refused at
    submit."""
    ranks = int(doc["ranks"])
    per_vm = int(doc["models_per_vm"])
    vcpus = per_vm * ranks * (2 if doc["packing"] == "core" else 1)
    out: Dict[str, Any] = {
        "families": list(doc["families"]),
        "vm_vcpus": vcpus,
        "packing": doc["packing"],
    }
    needs = {(ranks, float(doc["memory_gb"]))}
    for job in doc.get("jobs", []):
        needs.add((job_cpus(job, doc), float(job.get("memory_gb", doc["memory_gb"]))))
    for cpus, memory in sorted(needs):
        shapes = [vm_shape(f, cpus, memory, vcpus, doc["packing"]) for f in doc["families"]]
        if not any(shape in TEMPLATE_SHAPES for shape in shapes):
            out["template"] = False
    return out


def stage_line(job: Mapping[str, Any], doc: Mapping[str, Any]) -> Dict[str, Any]:
    jid = job["id"]
    ranks = int(job.get("ranks", doc["ranks"]))
    bind = bind_of(job, doc)
    record = "out/%s.job.json" % jid
    summary = [s.replace("{id}", jid) for s in job.get("summary", doc["summary"])]
    inputs = [{"dest": "job", "path": "job"}]
    inputs += [{"dest": dest, "path": "inputs/" + dest} for dest in sorted(doc.get("inputs", {}))]
    env = dict(doc.get("env", {}))
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    flags = ["--id", jid, "--ranks", str(ranks), "--bind", bind]
    sets = dict(doc.get("set", {}))
    sets.update(job.get("set", {}))
    for key, value in sets.items():
        flags += ["--set", "%s=%s" % (key, json.dumps(value))]
    if job.get("prepare"):
        flags += ["--prepare", json.dumps(list(job["prepare"]))]
    for spec in job.get("stages", []):
        flags += ["--stage", spec]
    if job.get("mesh_from"):
        flags += ["--mesh-from", job["mesh_from"]]
    if job.get("reference"):
        flags += ["--reference", job["reference"]]
        if "reference_tol" in job:
            flags += ["--reference-tol", repr(float(job["reference_tol"]))]
    if job.get("dry_run_only"):
        flags.append("--dry-run-only")
    labels = {str(k): str(v) for k, v in job.get("labels", {}).items()}
    labels.update(model=jid, ranks=str(ranks))
    line = {
        "id": jid,
        "command": ["${PYTHON}", "job/" + RUNNER] + flags + ["--", job["config"]],
        "inputs": inputs,
        "env": env,
        "resources": {
            "cpus": job_cpus(job, doc),
            "memory_gb": job.get("memory_gb", doc["memory_gb"]),
            "disk_gb": job.get("disk_gb", doc["disk_gb"]),
            "max_wall_s": int(job.get("max_wall_s", doc["max_wall_s"])),
        },
        "record": record,
        "summary": summary,
        "verdict": {"file": record, "json_path": "ok"},
        "labels": labels,
    }
    if doc.get("prune"):
        line["prune"] = list(doc["prune"])
    return line


def campaign_toml(doc: Mapping[str, Any], image: str) -> str:
    return image_tasks.campaign_toml(
        doc, image, RUNTIME, placement(doc), "palace_plan.py", "Palace"
    )


def generate(jobs_path: Path, out: Path, image: str) -> Dict[str, Any]:
    raw = image_tasks.load_jobs(jobs_path)
    errors = check_jobs(raw)
    if errors:
        raise JobError("; ".join(errors))
    doc = dict(DEFAULTS, **raw)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("job", "inputs"):
        shutil.rmtree(out / name, ignore_errors=True)
    (out / "job").mkdir()
    shutil.copyfile(HERE / RUNNER, out / "job" / RUNNER)
    sources = {}
    for dest, path in sorted(doc.get("inputs", {}).items()):
        src = (jobs_path.parent / path).resolve()
        image_tasks.copy_input(src, out / "inputs" / dest)
        sources[dest] = src.name
    lines = [stage_line(job, doc) for job in doc["jobs"]]
    (out / "stage.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    (out / "campaign.toml").write_text(campaign_toml(doc, image))
    manifest = {
        "schema": "yapnr-palace-plan-v1",
        "image": image,
        "placement": placement(doc),
        "ranks": doc["ranks"],
        "models_per_vm": doc["models_per_vm"],
        "inputs": sources,
        "jobs": [line["id"] for line in lines],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


# --- collect: fetched results -> the local runs layout


def collect(
    plan_dir: Path, fetched: Path, dest: Path, campaign: Optional[str] = None
) -> Dict[str, Any]:
    """Copy each fetched model's outputs into ``dest/runs`` as a local run would have left them;
    ``campaign`` (default: the plan directory's name, as in the store) names the summary."""
    campaign = campaign or plan_dir.name
    runs = dest / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {"campaign": campaign, "models": {}, "missing": []}
    for model, base, root in image_tasks.fetched_tasks(plan_dir, fetched):
        if root is None:
            summary["missing"].append(model)
            continue
        if (root / model).is_dir():
            shutil.copytree(root / model, runs / model, dirs_exist_ok=True)
        for suffix in (".log", ".job.json"):
            if (root / (model + suffix)).is_file():
                shutil.copyfile(root / (model + suffix), runs / (model + suffix))
        record_path = runs / (model + ".job.json")
        record = json.loads(record_path.read_text()) if record_path.is_file() else {}
        palace = record.get("palace") or {}
        summary["models"][model] = {
            "ok": record.get("ok"),
            "failed": record.get("failed"),
            "ranks": record.get("ranks"),
            "wall_s": record.get("wall_s"),
            "solve_s": (record.get("stages") or {}).get("solve", {}).get("wall_s"),
            "dofs": palace.get("dofs"),
            "mesh_elements": palace.get("mesh_elements"),
            "amr_refinements": (record.get("amr_completed") or {}).get("iterations"),
            "adaptation_solves": palace.get("adaptation_solves"),
            "linear_iterations": palace.get("linear_iterations"),
            "peak_memory_mb_sum": palace.get("peak_memory_mb_sum"),
            "peak_memory_mb_max_rank": palace.get("peak_memory_mb_max_rank"),
            "reference": record.get("reference"),
            "cpu": (record.get("cpu") or {}).get("model"),
            "palace": (record.get("image") or {}).get("palace.version"),
            "full": (base / "result").is_dir(),
        }
    path = runs / ("%s.summary.json" % campaign)
    path.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    summary["path"] = str(path)
    return summary


# --- the command line


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="the owner config (default ~/.config/yapnr/cloud.toml)")
    ap.add_argument("--configuration", help="the gcloud configuration (default: [gcp]'s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("image", help="the Cloud Build of docker/palace (prints it; --run)")
    p.add_argument("--tag", default=DEFAULT_TAG)
    p.add_argument("--run", action="store_true", help="submit the build and wait for it")
    p.add_argument("--async", dest="asynchronous", action="store_true", help="do not wait")
    p = sub.add_parser("digests", help="the digest and size of the built tag")
    p.add_argument("--tag", default=DEFAULT_TAG)
    p = sub.add_parser("plan", help="models -> an mc-eval campaign directory")
    p.add_argument("jobs", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--digest", help="pin the image to this digest (no registry lookup)")
    p.add_argument("--offline", action="store_true", help="leave a tag unpinned")
    p = sub.add_parser("collect", help="fetched results -> TREE/runs like local runs")
    p.add_argument("campaign", help="the campaign id")
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--plan", type=Path, help="the plan directory (default: the local store's)")
    p.add_argument("--fetched", type=Path, help="the fetched campaign (default: the store's)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            image = image_tasks.load_jobs(args.jobs).get("image", "")
            if not ("@" in image or (args.offline and "/" in image)):
                gcp = image_tasks.owner_config(args.config).require_gcp()
                image = image_tasks.resolve_image(
                    image, gcp, args.digest, args.offline, args.configuration
                )
            manifest = generate(args.jobs, args.out, image)
            print(
                "%d models, %s, %s -> %s"
                % (len(manifest["jobs"]), image, manifest["placement"], args.out / "campaign.toml")
            )
            return 0
        cfg = image_tasks.owner_config(args.config)
        if args.cmd == "collect":
            plan_dir, fetched = image_tasks.store_dirs(cfg, args.campaign, args.plan, args.fetched)
            summary = collect(plan_dir, fetched, args.dest, args.campaign)
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 0 if not summary["missing"] else 1
        gcp = cfg.require_gcp()
        if args.cmd == "digests":
            print(json.dumps(digests(gcp, args.tag, args.configuration), indent=1, sort_keys=True))
            return 0
        command = build_command(gcp, args.tag, args.configuration, args.asynchronous)
        if not args.run:
            print(" ".join(command))
            return 0
        return subprocess.run(command).returncode
    except (JobError, OSError, ValueError, subprocess.SubprocessError) as err:
        print("palace_plan: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
