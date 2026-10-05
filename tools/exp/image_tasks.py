"""What the campaign generators of solver task images share (openems_plan.py, palace_plan.py).

A task image is a third-party solver built by Cloud Build from ``docker/<name>`` into the
project's private ``images`` repository (``[gcp] images``) and run by ``yapnr exp`` campaigns
that name its interpreter in a ``[runtime]`` table (docs/cloud-experiments.md, "Task images").
This module holds the parts that do not depend on the solver: the owner configuration, the Cloud
Build submit, the digests of the built tags, pinning a campaign's image, the jobs file, input
copies, the TOML writer and the fetched task layout.
"""

from __future__ import annotations

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
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
DEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
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


# --- image: the Cloud Build of docker/<name>


def build_command(
    gcp,
    docker_dir: Path,
    image_name: str,
    tag: str,
    configuration: Optional[str],
    asynchronous: bool,
    substitutions: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """The ``gcloud builds submit`` of ``docker_dir`` with its ``cloudbuild.yaml``, as the build
    account, pushing ``<images>/<image_name>:<tag>...``; ``substitutions`` add to ``_IMAGE`` and
    ``_TAG``."""
    build_account = "projects/%s/serviceAccounts/%s" % (
        gcp.project,
        gcp.account(gcp.image_build_service_account),
    )
    values = {"_IMAGE": "%s/%s" % (images_repo(gcp), image_name), "_TAG": tag}
    values.update(substitutions or {})
    for key, value in values.items():
        if "," in value:
            raise JobError("substitution %s=%r has a comma" % (key, value))
    argv = gcloud_argv(gcp, configuration) + [
        "builds",
        "submit",
        str(docker_dir),
        "--config",
        str(docker_dir / "cloudbuild.yaml"),
        "--region",
        gcp.home_region,
        "--service-account",
        build_account,
        "--gcs-source-staging-dir",
        "gs://%s/cloudbuild/source" % gcp.inputs_bucket,
        "--substitutions",
        ",".join("%s=%s" % kv for kv in values.items()),
    ]
    return argv + (["--async"] if asynchronous else [])


def digests(
    gcp, image_name: str, tag: str, variants: Sequence[str], configuration: Optional[str]
) -> Dict[str, Dict[str, Any]]:
    """{variant: {ref, digest, size_bytes, created}} of the tags ``<tag>-<variant>``, from
    Artifact Registry."""
    repo = "%s/%s" % (images_repo(gcp), image_name)
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
        for variant in variants:
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


# --- plan: the jobs file, its inputs, the campaign file


def load_jobs(path: Path) -> Dict[str, Any]:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    if tomllib is None:
        raise JobError("TOML job files need Python 3.11 or later")
    return tomllib.loads(path.read_text())


def positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def check_inputs_env(doc: Mapping[str, Any]) -> List[str]:
    """Errors in a jobs file's ``inputs`` (name -> directory) and ``env`` (name -> string)."""
    errors = []
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
    return errors


def copy_input(src: Path, dest: Path) -> None:
    if not src.is_dir():
        raise JobError("input %s is not a directory" % src)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*SKIP_NAMES, "*.pyc"))


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


def campaign_toml(
    doc: Mapping[str, Any],
    image: str,
    runtime: Mapping[str, str],
    placement: Mapping[str, Any],
    generator: str,
    image_title: str,
) -> str:
    """The ``mc-eval`` campaign file of a stage plan that runs in a task image."""
    lines = [
        "# Written by tools/exp/%s; plan it with `yapnr exp plan`." % generator,
        'schema = "yapnr-campaign-v1"',
        'kind = "mc-eval"',
        "name = %s" % toml_value(doc["name"]),
        'source = "none"  # the models and scripts come as inputs of the stage plan',
        "image = %s" % toml_value(image),
        "visibility = %s" % toml_value(doc["visibility"]),
        'determinism = "seeded"',
        "",
        "# The %s image is not a yapnr image: its interpreter, no KiCad launcher." % image_title,
        "[runtime]",
    ]
    lines += ["%s = %s" % (k, toml_value(v)) for k, v in sorted(runtime.items())]
    lines += ["", "[config]", 'stage_plan = "stage.jsonl"', "", "[placement]"]
    lines += ["%s = %s" % (k, toml_value(v)) for k, v in sorted(placement.items())]
    return "\n".join(lines) + "\n"


# --- collect: the fetched campaign's layout


def task_key(task_id: str) -> str:
    return task_id.replace("/", "~")


def fetched_tasks(plan_dir: Path, fetched: Path):
    """(model, task base directory, its output root or None) for every task of a plan; the root
    is the full result's ``out`` when it was fetched with ``--full``, else the summary."""
    tasks = [json.loads(x) for x in (plan_dir / "tasks.jsonl").read_text().splitlines() if x]
    for task in tasks:
        model = task["labels"].get("model") or task["labels"].get("candidate")
        base = fetched / "tasks" / task_key(task["id"])
        root = base / "result" / "out"
        if not root.is_dir():
            root = base / "summary"
        done = (base / "_DONE").is_file() and root.is_dir()
        yield model, base, (root if done else None)


def store_dirs(cfg, campaign: str, plan: Optional[Path], fetched: Optional[Path]):
    """The plan and fetched directories of a campaign in the local store (a private campaign's
    live in the private store), unless given."""
    private = (cfg.local.store_path(True) / "plans" / campaign).is_dir()
    root = cfg.local.store_path(private)
    return plan or root / "plans" / campaign, fetched or root / "fetched" / campaign
