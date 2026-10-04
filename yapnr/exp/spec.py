"""The two schemas of the experiment layer: ``yapnr-task-v1`` and ``yapnr-campaign-v1``.

A task is one unit of work (one ladder cell, one benchmark cell, one candidate evaluation...). Its
spec hash is the sha256 of its canonical JSON (sorted keys, no whitespace, UTF-8), so the same task
has the same hash on every backend. Store paths stay symbolic in a task: the command names the
toolchain through ``${PYTHON}``, ``${KICAD_CLI}``, ``${KICAD_PYTHON}`` and ``${FOOTPRINTS}``,
which the wrapper resolves from the backend's toolchain profile.

Paths in a task are relative to the task's work directory, except ``outputs.summary`` and
``outputs.prune``, which are globs relative to ``outputs.root``.

A campaign file (TOML) declares a matrix of tasks of one kind; see docs/cloud-experiments.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping

TASK_SCHEMA = "yapnr-task-v1"
CAMPAIGN_SCHEMA = "yapnr-campaign-v1"

KINDS = (
    "ladder-cell",
    "bench-cell",
    "mc-place",
    "mc-eval",
    "rf-run",
    "fea",
    "smoke",
    "calibrate",
)
VISIBILITIES = ("public", "private")
RESTARTS = ("scratch", "resume")
DETERMINISM = ("seeded", "wall_clock_budgeted")
INPUT_KINDS = ("source", "data", "private")
PLACEHOLDERS = ("PYTHON", "KICAD_CLI", "KICAD_PYTHON", "FOOTPRINTS")

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*){0,7}$")
PRIVATE_ID_RE = re.compile(r"^p/[0-9a-f]{16}$")
CAMPAIGN_ID_RE = re.compile(r"^[0-9]{8}-[a-z][a-z0-9]{0,11}-[0-9a-f]{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
# An image reference: registry/path, then a tag, a digest or both.
IMAGE_RE = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+"
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(@sha256:[0-9a-f]{64})?$"
)
# Environment variables a task may not set: credentials and cloud SDK configuration.
FORBIDDEN_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|CREDENTIAL|^GOOGLE_|^CLOUDSDK_|^AWS_|^GITHUB_)"
)


class SpecError(ValueError):
    """A task or campaign that does not match its schema; ``errors`` lists every finding."""

    def __init__(self, what: str, errors: List[str]):
        self.errors = list(errors)
        super().__init__("%s: %s" % (what, "; ".join(self.errors)))


def canonical_json(value: Any) -> bytes:
    """The canonical form hashed everywhere: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def spec_hash(task: Mapping[str, Any]) -> str:
    """The sha256 of a task's canonical JSON."""
    return sha256_hex(canonical_json(dict(task)))


def is_pinned(image: str) -> bool:
    """True when an image reference names a digest (``...@sha256:<64 hex>``)."""
    return bool(IMAGE_RE.match(image or "")) and "@sha256:" in image


def _relative(path: Any) -> bool:
    if not isinstance(path, str) or not path or path.startswith(("/", "~")) or "\\" in path:
        return False
    return ".." not in path.split("/")


def _number(value: Any, minimum: float, integer: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if integer and not isinstance(value, int):
        return False
    return value >= minimum


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


TASK_KEYS = {
    "schema",
    "id",
    "campaign",
    "kind",
    "visibility",
    "image",
    "entrypoint",
    "command",
    "env",
    "inputs",
    "outputs",
    "done",
    "verdict",
    "resources",
    "restart",
    "checkpoint",
    "determinism",
    "labels",
}


def task_errors(task: Any) -> List[str]:
    """Every way ``task`` differs from ``yapnr-task-v1``; empty when it is valid."""
    if not isinstance(task, dict):
        return ["a task is a JSON object"]
    errors = []
    for key in sorted(set(task) - TASK_KEYS):
        errors.append("unknown field %r" % key)
    for key in sorted(TASK_KEYS - set(task)):
        errors.append("missing field %r" % key)
    if errors:
        return errors
    if task["schema"] != TASK_SCHEMA:
        errors.append("schema must be %r" % TASK_SCHEMA)
    tid = task["id"]
    if not isinstance(tid, str) or not TASK_ID_RE.match(tid) or len(tid) > 200:
        errors.append("id %r is not a task id (letters, digits, ._- in up to 8 parts)" % (tid,))
    if task["visibility"] not in VISIBILITIES:
        errors.append("visibility must be one of %s" % ", ".join(VISIBILITIES))
    elif task["visibility"] == "private" and not PRIVATE_ID_RE.match(str(tid)):
        errors.append("a private task has an opaque id (p/<16 hex>)")
    if not isinstance(task["campaign"], str) or not CAMPAIGN_ID_RE.match(task["campaign"]):
        errors.append("campaign %r is not a campaign id" % (task["campaign"],))
    if task["kind"] not in KINDS:
        errors.append("kind must be one of %s" % ", ".join(KINDS))
    if not isinstance(task["image"], str) or not IMAGE_RE.match(task["image"]):
        errors.append("image %r is not an image reference" % (task["image"],))
    if task["entrypoint"] is not None and not (
        isinstance(task["entrypoint"], str) and task["entrypoint"].startswith("/")
    ):
        errors.append("entrypoint is null or an absolute path in the image")
    command = task["command"]
    if not _strings(command) or not command:
        errors.append("command is a non-empty list of strings")
    else:
        for arg in command:
            for name in PLACEHOLDER_RE.findall(arg):
                if name not in PLACEHOLDERS:
                    errors.append(
                        "command placeholder ${%s} is not one of %s"
                        % (name, ", ".join(PLACEHOLDERS))
                    )
    env = task["env"]
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        errors.append("env maps names to strings")
    else:
        for key in env:
            if not ENV_KEY_RE.match(key):
                errors.append("env name %r is not an environment variable name" % key)
            elif FORBIDDEN_ENV_RE.search(key):
                errors.append(
                    "env name %r looks like a credential; tasks never carry secrets" % key
                )
    errors += _inputs_errors(task["inputs"])
    errors += _outputs_errors(task["outputs"])
    errors += _done_errors(task["done"], task["verdict"])
    errors += resources_errors(task["resources"], required=True)
    if task["restart"] not in RESTARTS:
        errors.append("restart must be one of %s" % ", ".join(RESTARTS))
    checkpoint = task["checkpoint"]
    if checkpoint is None:
        if task["restart"] == "resume":
            errors.append("restart 'resume' needs a checkpoint")
    elif not isinstance(checkpoint, dict) or set(checkpoint) != {
        "path",
        "sync_every_s",
        "on_signal",
    }:
        errors.append("checkpoint is null or {path, sync_every_s, on_signal}")
    else:
        if not _relative(checkpoint["path"]):
            errors.append("checkpoint.path is a relative path")
        if not _number(checkpoint["sync_every_s"], 30, integer=True):
            errors.append("checkpoint.sync_every_s is an integer of at least 30")
        if not isinstance(checkpoint["on_signal"], bool):
            errors.append("checkpoint.on_signal is a boolean")
    if task["determinism"] not in DETERMINISM:
        errors.append("determinism must be one of %s" % ", ".join(DETERMINISM))
    labels = task["labels"]
    if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
        errors.append("labels map names to strings")
    else:
        for key in labels:
            if not LABEL_KEY_RE.match(key):
                errors.append("label name %r is not lower-case [a-z0-9_-]" % key)
    return errors


def _inputs_errors(inputs: Any) -> List[str]:
    if not isinstance(inputs, list):
        return ["inputs is a list"]
    errors, dests = [], set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {"dest", "bundle", "kind"}:
            errors.append("an input is {dest, bundle, kind}")
            continue
        if not _relative(item["dest"]):
            errors.append("input dest %r is a relative path" % (item["dest"],))
        if item["dest"] in dests:
            errors.append("input dest %r is used twice" % (item["dest"],))
        dests.add(item["dest"])
        if not isinstance(item["bundle"], str) or not SHA256_RE.match(item["bundle"]):
            errors.append("input bundle is a sha256")
        if item["kind"] not in INPUT_KINDS:
            errors.append("input kind must be one of %s" % ", ".join(INPUT_KINDS))
    return errors


def _outputs_errors(outputs: Any) -> List[str]:
    if not isinstance(outputs, dict) or set(outputs) != {"root", "summary", "prune"}:
        return ["outputs is {root, summary, prune}"]
    errors = []
    if not _relative(outputs["root"]):
        errors.append("outputs.root is a relative path")
    for key in ("summary", "prune"):
        if not _strings(outputs[key]) or not all(_relative(g) for g in outputs[key]):
            errors.append("outputs.%s is a list of relative globs" % key)
    return errors


def _done_errors(done: Any, verdict: Any) -> List[str]:
    errors = []
    if (
        not isinstance(done, dict)
        or set(done) != {"file", "json"}
        or not _relative(done.get("file"))
        or not isinstance(done.get("json"), dict)
    ):
        errors.append("done is {file, json} (json: top-level values the file must have)")
    if verdict is not None and (
        not isinstance(verdict, dict)
        or set(verdict) != {"file", "json_path"}
        or not _relative(verdict.get("file"))
        or not isinstance(verdict.get("json_path"), str)
    ):
        errors.append("verdict is null or {file, json_path}")
    return errors


RESOURCE_KEYS = {"cpus", "memory_gb", "disk_gb", "max_wall_s"}


def resources_errors(resources: Any, required: bool = False) -> List[str]:
    if not isinstance(resources, dict):
        return ["resources is a table"]
    errors = ["unknown resource %r" % k for k in sorted(set(resources) - RESOURCE_KEYS)]
    if required:
        errors += ["missing resource %r" % k for k in sorted(RESOURCE_KEYS - set(resources))]
    if "cpus" in resources and not _number(resources["cpus"], 1, integer=True):
        errors.append("resources.cpus is a whole number of physical cores, at least 1")
    for key in ("memory_gb", "disk_gb"):
        if key in resources and not _number(resources[key], 0.25):
            errors.append("resources.%s is a number of GB, at least 0.25" % key)
    if "max_wall_s" in resources and not _number(resources["max_wall_s"], 60, integer=True):
        errors.append("resources.max_wall_s is a whole number of seconds, at least 60")
    return errors


def check_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``task`` unchanged, or raise SpecError listing every finding."""
    errors = task_errors(task)
    if errors:
        raise SpecError("task %s" % (task.get("id") if isinstance(task, dict) else "?"), errors)
    return task


CAMPAIGN_KEYS = {
    "schema",
    "kind",
    "name",
    "visibility",
    "source",
    "image",
    "runtime",
    "matrix",
    "config",
    "configs",
    "tools",
    "resources",
    "placement",
    "determinism",
    "inputs",
}
PLACEMENT_KEYS = {
    "families",
    "prefer",
    "template",
    "spot",
    "shape",
    "region",
    "vm_vcpus",
    "packing",
}


RUNTIME_KEYS = {"python", "entrypoint"}


def runtime_errors(runtime: Any) -> List[str]:
    """``runtime``: how a campaign's image runs the task wrapper, for images other than yapnr's.

    ``python`` is the interpreter in the image that runs ``task.py`` and that ``${PYTHON}`` names
    in commands (default ``/opt/venv/bin/python``); ``entrypoint`` the launcher around both
    (default ``/usr/local/bin/yapnr-kicad-env``; ``""`` for none, the image's own entrypoint).
    """
    if not isinstance(runtime, dict):
        return ["runtime is a table {python, entrypoint}"]
    errors = ["unknown runtime key %r" % k for k in sorted(set(runtime) - RUNTIME_KEYS)]
    python = runtime.get("python", "/opt/venv/bin/python")
    if not (isinstance(python, str) and python.startswith("/") and len(python) > 1):
        errors.append("runtime.python is an absolute path in the image")
    entrypoint = runtime.get("entrypoint", "")
    if not (isinstance(entrypoint, str) and (entrypoint == "" or entrypoint.startswith("/"))):
        errors.append("runtime.entrypoint is an absolute path in the image, or '' for none")
    return errors


def campaign_errors(campaign: Any) -> List[str]:
    """Every way a parsed campaign file differs from ``yapnr-campaign-v1``."""
    if not isinstance(campaign, dict):
        return ["a campaign file is a TOML table"]
    errors = ["unknown key %r" % k for k in sorted(set(campaign) - CAMPAIGN_KEYS)]
    if campaign.get("schema") != CAMPAIGN_SCHEMA:
        errors.append("schema must be %r" % CAMPAIGN_SCHEMA)
    if campaign.get("kind") not in KINDS:
        errors.append("kind must be one of %s" % ", ".join(KINDS))
    if campaign.get("visibility", "public") not in VISIBILITIES:
        errors.append("visibility must be one of %s" % ", ".join(VISIBILITIES))
    if "name" in campaign and not (
        isinstance(campaign["name"], str)
        and re.match(r"^[a-z0-9][a-z0-9-]{0,40}$", campaign["name"])
    ):
        errors.append("name is lower-case letters, digits and dashes")
    source = campaign.get("source", "HEAD")
    if not isinstance(source, str) or not re.match(r"^(HEAD|none|[0-9a-f]{7,40})$", source):
        errors.append("source is 'HEAD', 'none' or a commit")
    image = campaign.get("image", "edge")
    if not isinstance(image, str) or not (
        re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", image) or IMAGE_RE.match(image)
    ):
        errors.append("image is a tag of the yapnr image or a full image reference")
    errors += runtime_errors(campaign.get("runtime", {}))
    for key in ("matrix", "config", "configs", "tools", "placement"):
        if key in campaign and not isinstance(campaign[key], dict):
            errors.append("%s is a table" % key)
    matrix = campaign.get("matrix", {})
    if isinstance(matrix, dict):
        for axis, values in matrix.items():
            if not isinstance(values, list) or not values:
                errors.append("matrix.%s is a non-empty list" % axis)
            elif len(set(map(json.dumps, values))) != len(values):
                errors.append("matrix.%s repeats a value" % axis)
    if "resources" in campaign:
        errors += resources_errors(campaign["resources"])
    placement = campaign.get("placement", {})
    if isinstance(placement, dict):
        errors += ["unknown placement key %r" % k for k in sorted(set(placement) - PLACEMENT_KEYS)]
        if "families" in placement and not (
            _strings(placement["families"]) and placement["families"]
        ):
            errors.append("placement.families is a non-empty list of machine families")
        for key in ("template", "spot"):
            if not isinstance(placement.get(key, True), bool):
                errors.append("placement.%s is a boolean" % key)
        for key in ("shape", "region"):
            if key in placement and not (
                isinstance(placement[key], str) and re.match(r"^[a-z][a-z0-9-]+$", placement[key])
            ):
                errors.append("placement.%s is a name such as c4d-highcpu-16 or us-west4" % key)
        if placement.get("prefer", "cost") not in ("cost", "first-family"):
            errors.append("placement.prefer is 'cost' or 'first-family'")
        if placement.get("packing", "core") not in ("core", "vcpu"):
            errors.append("placement.packing is 'core' or 'vcpu'")
        if "vm_vcpus" in placement and not _number(placement["vm_vcpus"], 2, integer=True):
            errors.append("placement.vm_vcpus is a whole number")
    if campaign.get("determinism", "seeded") not in DETERMINISM:
        errors.append("determinism must be one of %s" % ", ".join(DETERMINISM))
    inputs = campaign.get("inputs", [])
    if not isinstance(inputs, list):
        errors.append("inputs is an array of tables")
    else:
        for item in inputs:
            if (
                not isinstance(item, dict)
                or not set(item) <= {"dest", "path", "kind"}
                or not {"dest", "path"} <= set(item)
            ):
                errors.append("an input is {dest, path, kind}")
            elif not _relative(item["dest"]) or item.get("kind", "data") not in INPUT_KINDS[1:]:
                errors.append("input %r: dest is relative, kind is data or private" % item["dest"])
    return errors


def check_campaign(campaign: Dict[str, Any]) -> Dict[str, Any]:
    errors = campaign_errors(campaign)
    if errors:
        raise SpecError("campaign", errors)
    return campaign
