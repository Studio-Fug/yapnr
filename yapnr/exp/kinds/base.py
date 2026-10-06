"""What every kind of task shares: the expansion context, the task factory and the assembly helpers.

A kind turns a campaign file into ``yapnr-task-v1`` records (``expand``), predicts each task's wall
time on the reference core for the estimator (``reference_seconds``), and rebuilds the layout the
local tools read from fetched results (``assemble``).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from yapnr.exp import spec

# The image's KiCad environment launcher (docs/containers.md): the container entrypoint of every
# task that runs in the yapnr image.
IMAGE_ENTRYPOINT = "/usr/local/bin/yapnr-kicad-env"
# One thread per task unless a kind asks for more: the engine is single-threaded Python, and
# numpy/torch would otherwise start one thread per vCPU of a shared VM.
SINGLE_THREAD_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


@dataclass
class Context:
    """What a kind needs to expand a campaign into tasks."""

    campaign_id: str
    image: str
    visibility: str
    determinism: str
    resources: Dict[str, Any]
    source_input: Optional[Dict[str, str]]
    data_inputs: List[Dict[str, str]] = field(default_factory=list)
    base_dir: Path = Path(".")
    bundle_dir: Optional[Path] = None
    repo: Optional[Path] = None


def task_id(ctx: Context, readable: str) -> str:
    """The task id: ``readable``, or an opaque ``p/<16 hex>`` for private campaigns."""
    if ctx.visibility == "private":
        digest = hashlib.sha256(("%s\0%s" % (ctx.campaign_id, readable)).encode()).hexdigest()
        return "p/" + digest[:16]
    return readable


def make_task(
    ctx: Context,
    *,
    kind: str,
    readable_id: str,
    command: Sequence[str],
    outputs: Mapping[str, Any],
    done: Mapping[str, Any],
    verdict: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    inputs: Optional[Sequence[Mapping[str, str]]] = None,
    resources: Optional[Mapping[str, Any]] = None,
    restart: str = "scratch",
    checkpoint: Optional[Mapping[str, Any]] = None,
    labels: Optional[Mapping[str, str]] = None,
    image: Optional[str] = None,
    entrypoint: Optional[str] = IMAGE_ENTRYPOINT,
    determinism: Optional[str] = None,
) -> Dict[str, Any]:
    if inputs is None:
        inputs = ([ctx.source_input] if ctx.source_input else []) + list(ctx.data_inputs)
    task = {
        "schema": spec.TASK_SCHEMA,
        "id": task_id(ctx, readable_id),
        "campaign": ctx.campaign_id,
        "kind": kind,
        "visibility": ctx.visibility,
        "image": image or ctx.image,
        "entrypoint": entrypoint,
        "command": list(command),
        "env": dict(env or {}),
        "inputs": [dict(i) for i in inputs],
        "outputs": {
            "root": outputs["root"],
            "summary": list(outputs.get("summary", [])),
            "prune": list(outputs.get("prune", [])),
        },
        "done": {"file": done["file"], "json": dict(done.get("json", {}))},
        "verdict": dict(verdict) if verdict else None,
        "resources": dict(ctx.resources, **(resources or {})),
        "restart": restart,
        "checkpoint": dict(checkpoint) if checkpoint else None,
        "determinism": determinism or ctx.determinism,
        "labels": {k: str(v) for k, v in (labels or {}).items()},
    }
    return spec.check_task(task)


def matrix_product(
    matrix: Mapping[str, List[Any]], order: Sequence[str]
) -> Iterable[Dict[str, Any]]:
    """The rows of ``matrix`` in a stable order: ``order`` first, the other axes sorted."""
    axes = [a for a in order if a in matrix] + sorted(a for a in matrix if a not in order)
    for values in itertools.product(*(matrix[a] for a in axes)):
        yield dict(zip(axes, values))


class Kind:
    """One kind of task. Subclasses set the class attributes and implement ``expand``."""

    name = ""
    short = ""
    # Repository paths the source bundle needs; None: the whole tree; (): no source bundle.
    source_paths: Optional[Tuple[str, ...]] = None
    # Further paths the bundle carries when the source commit has them (data a checkout may
    # lack, such as a fixture repository's); never more than these, never a failure without.
    optional_source_paths: Tuple[str, ...] = ()
    default_resources: Dict[str, Any] = dict(cpus=1, memory_gb=2, disk_gb=4, max_wall_s=3600)
    default_determinism = "seeded"
    config_keys: frozenset = frozenset()
    matrix_axes: Tuple[str, ...] = ()
    required_axes: Tuple[str, ...] = ()

    def check(self, campaign: Mapping[str, Any]) -> List[str]:
        """Kind-specific findings in a campaign file."""
        errors = []
        matrix = campaign.get("matrix", {})
        for axis in self.required_axes:
            if axis not in matrix:
                errors.append("matrix.%s is required for %s" % (axis, self.name))
        for axis in matrix:
            if axis not in self.matrix_axes:
                errors.append("matrix.%s is not an axis of %s" % (axis, self.name))
        for key in sorted(set(campaign.get("config", {})) - set(self.config_keys)):
            errors.append("config.%s is not an option of %s" % (key, self.name))
        return errors

    def expand(self, campaign: Mapping[str, Any], ctx: Context) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        """Expected wall time on the reference core (the development Mac's performance core)."""
        return min(600.0, float(task["resources"]["max_wall_s"]))

    def assemble(
        self,
        plan: Mapping[str, Any],
        tasks: List[Dict[str, Any]],
        fetched: Path,
        dest: Path,
        allow_mixed: bool = False,
    ) -> Dict[str, Any]:
        """Default assembly: each task's outputs under ``dest/<task key>/``."""
        report = new_report()
        for task in tasks:
            root = output_root(fetched, task)
            if root is None:
                report["missing"].append(task["id"])
                continue
            target = dest / task_key(task["id"])
            copy_tree(root, target)
            report["tasks"] += 1
        report["paths"].append(str(dest))
        return report


def task_key(task_id: str) -> str:
    return task_id.replace("/", "~")


def new_report() -> Dict[str, Any]:
    return {"tasks": 0, "missing": [], "warnings": [], "paths": []}


def output_root(fetched: Path, task: Mapping[str, Any]) -> Optional[Path]:
    """Where a fetched task's outputs are: the full result if fetched, else its summary files."""
    base = fetched / task_key(task["id"])
    if not (base / "_DONE").is_file():
        return None
    full = base / "result" / task["outputs"]["root"]
    if full.is_dir():
        return full
    summary = base / "summary"
    return summary if summary.is_dir() else None


def task_record(fetched: Path, task: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    path = fetched / task_key(task["id"]) / "record.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def copy_tree(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    elif src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def shard(record: Optional[Mapping[str, Any]], task: Mapping[str, Any]) -> Dict[str, Any]:
    """The per-task provenance an assembled run lists under ``shards``."""
    record = record or {}
    machine = record.get("machine") or {}
    return {
        "task": task["id"],
        "attempt": record.get("attempt"),
        "backend": record.get("backend"),
        "machine_type": machine.get("machine_type"),
        "region": machine.get("region"),
        "cpu_model": machine.get("cpu_model"),
        "vcpus": machine.get("vcpus"),
        "threads_per_core": machine.get("threads_per_core"),
        "platform": machine.get("platform"),
        "image": (record.get("image") or {}).get("ref"),
        "wall_s": record.get("wall_s"),
        "cpu_s": record.get("cpu_s"),
        "peak_rss_mb": record.get("peak_rss_mb"),
        "verdict": record.get("verdict"),
    }
