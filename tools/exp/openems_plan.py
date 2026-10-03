#!/usr/bin/env python3
"""openEMS models as one ``yapnr exp`` campaign: build the image, plan N models, collect results.

    python3 tools/exp/openems_plan.py image [--tag T] [--run]       # Cloud Build of docker/openems
    python3 tools/exp/openems_plan.py digests [--tag T]              # the built tags' digests
    python3 tools/exp/openems_plan.py plan JOBS.toml --out DIR [--digest D | --offline]
    python3 tools/exp/openems_plan.py collect CID --dest TREE        # after `yapnr exp fetch --full`

``plan`` turns the models of ``JOBS.toml`` into an ``mc-eval`` campaign with one task per model,
each running ``SCRIPT ARGS`` through ``openems_job.py`` (the record, the log, openEMS's speed) in
the openEMS image (docker/openems), packed ``models_per_vm`` to a VM at ``threads`` cores each::

    name = "rfuni-em"                          # the campaign name
    image = "openems:0.37.0-rc3-x86-64-v4"     # a tag in [gcp] images, or a full reference
    threads = 4                                # openEMS threads per model (the task's cores)
    models_per_vm = 2                          # models side by side on one VM
    packing = "core"                           # "vcpu": threads on SMT siblings (half the VM)
    families = ["c4d"]                         # amd64 families with AVX-512 for the v4 build
    memory_gb = 4                              # per model; disk_gb and max_wall_s too
    max_wall_s = 7200
    visibility = "public"                      # or "private" (opaque ids, private store)

    [inputs]                                   # every task gets these directories, by name
    code = "code"                              # relative to the jobs file
    models = "models"
    w = "/path/to/examples/radar60/rf"

    [env]                                      # the tasks' environment
    RFMACRO_ROOT = "w"

    [[jobs]]
    id = "tx12-A-e1"                           # out/<id>/ (the model's output), out/<id>.log
    script = "code/feed_sim.py"
    args = ["models/tx12-A.json", "--out", "{out}", "--excite", "TX1.P0", "--threads", "{threads}"]
    # threads, memory_gb, disk_gb, max_wall_s, summary and labels per job, optionally

``DIR`` receives copies of the input directories, the job bundle (``openems_job.py``), the stage
plan, ``campaign.toml`` (image pinned by digest, ``[runtime]`` for the openEMS interpreter) and
``manifest.json``. Then::

    yapnr exp plan DIR/campaign.toml --backend gcp-batch
    yapnr exp submit CID && yapnr exp watch CID
    yapnr exp fetch CID --full
    python3 tools/exp/openems_plan.py collect CID --dest TREE

``collect`` lays the results out like local runs: ``TREE/runs/<id>/`` (the model's output),
``TREE/runs/<id>.log`` (ending in ``exit N``) and ``TREE/runs/<id>.job.json``, plus
``TREE/runs/<cid>.summary.json`` with each model's wall time and MCells/s.

``image`` prints (``--run``: runs) the Cloud Build submit of docker/openems/cloudbuild.yaml: the
generic x86-64 and the AVX-512 (x86-64-v4, Zen 4/5) builds, pushed as ``openems:<tag>-x86-64``
and ``openems:<tag>-x86-64-v4`` to ``[gcp] images`` in the home region, built as ``[gcp]
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
    import tomllib
except ImportError:  # Python < 3.11: JSON job files only
    tomllib = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DOCKER_DIR = REPO / "docker" / "openems"
RUNNER = "openems_job.py"
IMAGE_NAME = "openems"
DEFAULT_TAG = "0.37.0-rc3"
VARIANTS = ("x86-64", "x86-64-v4")
RUNTIME = {"python": "/opt/openEMS/venv/bin/python", "entrypoint": ""}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
DEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TOP_KEYS = {
    "name",
    "image",
    "threads",
    "models_per_vm",
    "packing",
    "families",
    "memory_gb",
    "disk_gb",
    "max_wall_s",
    "visibility",
    "summary",
    "inputs",
    "env",
    "jobs",
}
JOB_KEYS = {
    "id",
    "script",
    "args",
    "threads",
    "memory_gb",
    "disk_gb",
    "max_wall_s",
    "summary",
    "labels",
}
DEFAULTS = {
    "threads": 4,
    "models_per_vm": 2,
    "packing": "core",
    "families": ["c4d"],
    "memory_gb": 4,
    "disk_gb": 4,
    "max_wall_s": 7200,
    "visibility": "public",
    # Fetched without --full: the log, the record and the model's small result files.
    "summary": ["{id}.log", "{id}.job.json", "{id}/*.json", "{id}/*.csv"],
}
# Instance templates exist for these C4D shapes (infra/gcp template_shapes); others run from an
# instance policy.
TEMPLATE_VCPUS = (8, 16)
SKIP_NAMES = ("__pycache__", ".git", ".DS_Store")
GCLOUD_TIMEOUT_S = 120


class JobError(ValueError):
    pass


# --- the owner configuration (yapnr.exp.config), only where a command needs it


def owner_config(path: Optional[str]):
    sys.path.insert(0, str(REPO))
    from yapnr.exp import config as cloud_config

    return cloud_config.load(path)


def gcloud_argv(gcp, configuration: Optional[str]) -> List[str]:
    argv = [gcp.gcloud]
    configuration = configuration or gcp.gcloud_configuration
    if configuration:
        argv += ["--configuration", configuration]
    return argv + ["--project", gcp.project]


def images_repo(gcp, region: Optional[str] = None) -> str:
    return gcp.images.format(region=region or gcp.home_region, project=gcp.project)


# --- image: the Cloud Build of docker/openems


def build_command(gcp, tag: str, configuration: Optional[str], asynchronous: bool) -> List[str]:
    build_account = "projects/%s/serviceAccounts/%s" % (
        gcp.project,
        gcp.account(gcp.image_build_service_account),
    )
    argv = gcloud_argv(gcp, configuration) + [
        "builds",
        "submit",
        str(DOCKER_DIR),
        "--config",
        str(DOCKER_DIR / "cloudbuild.yaml"),
        "--region",
        gcp.home_region,
        "--service-account",
        build_account,
        "--gcs-source-staging-dir",
        "gs://%s/cloudbuild/source" % gcp.inputs_bucket,
        "--substitutions",
        "_IMAGE=%s/%s,_TAG=%s" % (images_repo(gcp), IMAGE_NAME, tag),
    ]
    return argv + (["--async"] if asynchronous else [])


def digests(gcp, tag: str, configuration: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """{variant: {ref, digest, size_bytes}} of the built tags, from Artifact Registry."""
    repo = "%s/%s" % (images_repo(gcp), IMAGE_NAME)
    argv = gcloud_argv(gcp, configuration) + [
        "artifacts",
        "docker",
        "images",
        "list",
        repo,
        "--include-tags",
        "--format=json",
    ]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_S)
    if done.returncode != 0:
        raise JobError("gcloud artifacts docker images list: %s" % done.stderr.strip()[-400:])
    out = {}
    for item in json.loads(done.stdout or "[]"):
        tags = item.get("tags") or []
        tags = tags.split(",") if isinstance(tags, str) else tags
        for variant in VARIANTS:
            if "%s-%s" % (tag, variant) in tags:
                out[variant] = {
                    "ref": "%s:%s-%s@%s" % (repo, tag, variant, item.get("version")),
                    "digest": item.get("version"),
                    "size_bytes": int((item.get("metadata") or {}).get("imageSizeBytes", 0) or 0),
                    "created": item.get("createTime"),
                }
    return out


def resolve_image(text: str, gcp, digest: Optional[str], offline: bool, configuration) -> str:
    """The campaign's image: a full reference, pinned by digest unless offline."""
    ref = text if "/" in text else "%s/%s" % (images_repo(gcp), text)
    if "@" in ref:
        return ref
    if digest:
        if not DIGEST_RE.match(digest):
            raise JobError("%r is not a sha256 digest" % digest)
        return "%s@%s" % (ref, digest)
    if offline:
        return ref
    argv = gcloud_argv(gcp, configuration) + [
        "artifacts",
        "docker",
        "images",
        "describe",
        ref,
        "--format=value(image_summary.digest)",
    ]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_S)
    found = done.stdout.strip()
    if done.returncode != 0 or not DIGEST_RE.match(found):
        raise JobError("cannot resolve %s: %s" % (ref, done.stderr.strip()[-400:]))
    return "%s@%s" % (ref, found)


# --- plan: N models -> one mc-eval campaign


def load_jobs(path: Path) -> Dict[str, Any]:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    if tomllib is None:
        raise JobError("TOML job files need Python 3.11 or later")
    return tomllib.loads(path.read_text())


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def check_jobs(doc: Mapping[str, Any]) -> List[str]:
    errors = ["unknown key %r" % k for k in sorted(set(doc) - TOP_KEYS)]
    if not isinstance(doc.get("name"), str) or not NAME_RE.match(doc["name"]):
        errors.append("name is a campaign name ([a-z0-9-], at most 41 characters)")
    if not isinstance(doc.get("image"), str) or not doc["image"]:
        errors.append("image is a tag in the images repository or a full reference")
    for key in ("threads", "models_per_vm", "memory_gb", "disk_gb", "max_wall_s"):
        if key in doc and not _positive(doc[key]):
            errors.append("%s is a positive number" % key)
    if doc.get("packing", "core") not in ("core", "vcpu"):
        errors.append("packing is 'core' or 'vcpu'")
    if doc.get("visibility", "public") not in ("public", "private"):
        errors.append("visibility is 'public' or 'private'")
    inputs = doc.get("inputs", {})
    if not isinstance(inputs, dict) or not all(
        isinstance(k, str) and DEST_RE.match(k) and isinstance(v, str) for k, v in inputs.items()
    ):
        errors.append("inputs maps a name to a directory")
    elif "job" in inputs or "out" in inputs:
        errors.append("inputs may not be named 'job' or 'out'")
    env = doc.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        errors.append("env maps names to strings")
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
            errors.append("%s: id is [A-Za-z0-9._-], at most 63 characters" % where)
        elif job["id"] in seen:
            errors.append("%s: id %r is repeated" % (where, job["id"]))
        else:
            seen.add(job["id"])
        if not isinstance(job.get("script"), str) or not job["script"]:
            errors.append("%s: script is the model script, relative to the work directory" % where)
        args = job.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.append("%s: args is a list of strings" % where)
        for key in ("threads", "memory_gb", "disk_gb", "max_wall_s"):
            if key in job and not _positive(job[key]):
                errors.append("%s: %s is a positive number" % (where, key))
    return errors


def copy_input(src: Path, dest: Path) -> None:
    if not src.is_dir():
        raise JobError("input %s is not a directory" % src)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*SKIP_NAMES, "*.pyc"))


def placement(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """K models of T threads per VM: 2*K*T vCPUs by core (C4D has two threads a core)."""
    threads = int(doc["threads"])
    per_vm = int(doc["models_per_vm"])
    vcpus = per_vm * threads * (2 if doc["packing"] == "core" else 1)
    out: Dict[str, Any] = {
        "families": list(doc["families"]),
        "vm_vcpus": vcpus,
        "packing": doc["packing"],
    }
    if vcpus not in TEMPLATE_VCPUS:
        out["template"] = False
    return out


def stage_line(job: Mapping[str, Any], doc: Mapping[str, Any]) -> Dict[str, Any]:
    jid = job["id"]
    threads = int(job.get("threads", doc["threads"]))
    record = "out/%s.job.json" % jid
    summary = [s.replace("{id}", jid) for s in job.get("summary", doc["summary"])]
    inputs = [{"dest": "job", "path": "job"}]
    inputs += [{"dest": dest, "path": "inputs/" + dest} for dest in sorted(doc.get("inputs", {}))]
    env = dict(doc.get("env", {}))
    env.update(OMP_NUM_THREADS=str(threads), OPENBLAS_NUM_THREADS="1")
    return {
        "id": jid,
        "command": ["${PYTHON}", "job/" + RUNNER, "--id", jid, "--threads", str(threads), "--"]
        + [job["script"]]
        + list(job.get("args", [])),
        "inputs": inputs,
        "env": env,
        "resources": {
            "cpus": threads,
            "memory_gb": job.get("memory_gb", doc["memory_gb"]),
            "disk_gb": job.get("disk_gb", doc["disk_gb"]),
            "max_wall_s": int(job.get("max_wall_s", doc["max_wall_s"])),
        },
        "record": record,
        "summary": summary,
        "verdict": {"file": record, "json_path": "ok"},
        "labels": dict({str(k): str(v) for k, v in job.get("labels", {}).items()}, model=jid),
    }


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


def campaign_toml(doc: Mapping[str, Any], image: str) -> str:
    lines = [
        "# Written by tools/exp/openems_plan.py; plan it with `yapnr exp plan`.",
        'schema = "yapnr-campaign-v1"',
        'kind = "mc-eval"',
        "name = %s" % toml_value(doc["name"]),
        'source = "none"  # the models and scripts come as inputs of the stage plan',
        "image = %s" % toml_value(image),
        "visibility = %s" % toml_value(doc["visibility"]),
        'determinism = "seeded"',
        "",
        "# The openEMS image is not a yapnr image: its interpreter, no KiCad launcher.",
        "[runtime]",
    ]
    lines += ["%s = %s" % (k, toml_value(v)) for k, v in sorted(RUNTIME.items())]
    lines += ["", "[config]", 'stage_plan = "stage.jsonl"', "", "[placement]"]
    lines += ["%s = %s" % (k, toml_value(v)) for k, v in sorted(placement(doc).items())]
    return "\n".join(lines) + "\n"


def generate(jobs_path: Path, out: Path, image: str) -> Dict[str, Any]:
    raw = load_jobs(jobs_path)
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
        copy_input(src, out / "inputs" / dest)
        sources[dest] = src.name
    lines = [stage_line(job, doc) for job in doc["jobs"]]
    (out / "stage.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    (out / "campaign.toml").write_text(campaign_toml(doc, image))
    manifest = {
        "schema": "yapnr-openems-plan-v1",
        "image": image,
        "placement": placement(doc),
        "threads": doc["threads"],
        "models_per_vm": doc["models_per_vm"],
        "inputs": sources,
        "jobs": [line["id"] for line in lines],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


# --- collect: fetched results -> the local runs layout


def task_key(task_id: str) -> str:
    return task_id.replace("/", "~")


def collect(plan_dir: Path, fetched: Path, dest: Path) -> Dict[str, Any]:
    """Copy each fetched model's outputs into ``dest/runs`` as a local run would have left them."""
    tasks = [json.loads(x) for x in (plan_dir / "tasks.jsonl").read_text().splitlines() if x]
    runs = dest / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {"campaign": plan_dir.name, "models": {}, "missing": []}
    for task in tasks:
        model = task["labels"].get("model") or task["labels"].get("candidate")
        base = fetched / "tasks" / task_key(task["id"])
        root = base / "result" / "out"
        if not root.is_dir():
            root = base / "summary"
        if not (base / "_DONE").is_file() or not root.is_dir():
            summary["missing"].append(model)
            continue
        if (root / model).is_dir():
            shutil.copytree(root / model, runs / model, dirs_exist_ok=True)
        for suffix in (".log", ".job.json"):
            if (root / (model + suffix)).is_file():
                shutil.copyfile(root / (model + suffix), runs / (model + suffix))
        record_path = runs / (model + ".job.json")
        record = json.loads(record_path.read_text()) if record_path.is_file() else {}
        speeds = [r.get("mcells_per_s") for r in record.get("openems_runs", [])]
        summary["models"][model] = {
            "ok": record.get("ok"),
            "wall_s": record.get("wall_s"),
            "mcells_per_s": speeds,
            "cpu": (record.get("cpu") or {}).get("model"),
            "arch_flags": record.get("arch_flags"),
            "full": (base / "result").is_dir(),
        }
    path = runs / ("%s.summary.json" % plan_dir.name)
    path.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    summary["path"] = str(path)
    return summary


# --- the command line


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="the owner config (default ~/.config/yapnr/cloud.toml)")
    ap.add_argument("--configuration", help="the gcloud configuration (default: [gcp]'s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("image", help="the Cloud Build of docker/openems (prints it; --run)")
    p.add_argument("--tag", default=DEFAULT_TAG)
    p.add_argument("--run", action="store_true", help="submit the build and wait for it")
    p.add_argument("--async", dest="asynchronous", action="store_true", help="do not wait")
    p = sub.add_parser("digests", help="the digests and sizes of the built tags")
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
            image = load_jobs(args.jobs).get("image", "")
            if not ("@" in image or (args.offline and "/" in image)):
                gcp = owner_config(args.config).require_gcp()
                image = resolve_image(image, gcp, args.digest, args.offline, args.configuration)
            manifest = generate(args.jobs, args.out, image)
            print(
                "%d models, %s, %s -> %s"
                % (
                    len(manifest["jobs"]),
                    image,
                    manifest["placement"],
                    args.out / "campaign.toml",
                )
            )
            return 0
        cfg = owner_config(args.config)
        if args.cmd == "collect":
            # A private campaign's plan and results live in the private store.
            private = (cfg.local.store_path(True) / "plans" / args.campaign).is_dir()
            root = cfg.local.store_path(private)
            plan_dir = args.plan or root / "plans" / args.campaign
            fetched = args.fetched or root / "fetched" / args.campaign
            summary = collect(plan_dir, fetched, args.dest)
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
        print("openems_plan: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
