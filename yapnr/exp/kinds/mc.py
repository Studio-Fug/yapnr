"""``mc-place`` and ``mc-eval``: the stages of a Monte Carlo successive-halving run as tasks.

The halving driver stays the coordinator, on the owner's machine: its plan mode writes a stage's
pending evaluations as a *stage plan* (JSON Lines), ``yapnr exp`` runs them in parallel, and its
import mode ingests the records that ``assemble`` writes (``dataset.jsonl``, campaign order),
with the driver's own per-record resume logic. The generator of stage plans for private boards
lives in the private repository; this module only knows the generic format.

A stage plan line::

    {"id": "rung1/cand-03",                 # unique within the plan
     "command": ["${PYTHON}", "-m", "...", "--out", "out/eval"],
     "inputs": [{"dest": "bundle", "path": "bundles/cand-03.tar.gz"}],   # relative to the plan
     "record": "out/eval/record.json",      # the evaluation's record, relative to the work dir
     "resources": {"cpus": 2, "memory_gb": 8, "max_wall_s": 7200},       # optional
     "labels": {"rung": "1"}, "env": {"PNR_X": "1"}}                     # optional
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

from yapnr.exp import bundle, spec
from yapnr.exp.kinds import base

LINE_KEYS = {"id", "command", "inputs", "record", "resources", "labels", "env", "summary"}


def read_stage_plan(path: Path) -> List[Dict[str, Any]]:
    lines = []
    for number, text in enumerate(path.read_text().splitlines(), 1):
        if not text.strip():
            continue
        try:
            line = json.loads(text)
        except ValueError as err:
            raise ValueError("%s:%d: %s" % (path.name, number, err)) from err
        errors = []
        if not isinstance(line, dict):
            errors.append("a JSON object")
        else:
            errors += ["unknown key %r" % k for k in sorted(set(line) - LINE_KEYS)]
            if not isinstance(line.get("id"), str) or not spec.TASK_ID_RE.match(line["id"]):
                errors.append("id is a task id")
            if not (
                isinstance(line.get("command"), list)
                and line["command"]
                and all(isinstance(x, str) for x in line["command"])
            ):
                errors.append("command is a list of strings")
            if not isinstance(line.get("record"), str) or not line["record"].startswith("out/"):
                errors.append("record is a path under out/")
        if errors:
            raise ValueError("%s:%d: %s" % (path.name, number, "; ".join(errors)))
        lines.append(line)
    ids = [line["id"] for line in lines]
    if len(set(ids)) != len(ids):
        raise ValueError("%s repeats an id" % path.name)
    return lines


class McEval(base.Kind):
    name = "mc-eval"
    short = "mceval"
    source_paths = ("hardware", "yapnr")
    default_resources = dict(cpus=2, memory_gb=8, disk_gb=10, max_wall_s=14400)
    default_determinism = "wall_clock_budgeted"
    config_keys = frozenset({"stage_plan", "reference_seconds"})
    matrix_axes = ()

    def check(self, campaign: Mapping[str, Any]) -> List[str]:
        errors = super().check(campaign)
        if not isinstance(campaign.get("config", {}).get("stage_plan"), str):
            errors.append("config.stage_plan names the stage plan (JSON Lines)")
        return errors

    def expand(self, campaign: Mapping[str, Any], ctx: base.Context) -> List[Dict[str, Any]]:
        plan_path = (ctx.base_dir / campaign["config"]["stage_plan"]).resolve()
        input_kind = "private" if ctx.visibility == "private" else "data"
        common = ([ctx.source_input] if ctx.source_input else []) + list(ctx.data_inputs)
        tasks = []
        for line in read_stage_plan(plan_path):
            inputs = list(common)
            for item in line.get("inputs", []):
                if ctx.bundle_dir is None:
                    raise ValueError("stage plan inputs need a bundle directory")
                digest, _ = bundle.data_bundle(plan_path.parent / item["path"], ctx.bundle_dir)
                inputs.append({"dest": item["dest"], "bundle": digest, "kind": input_kind})
            record = line["record"]
            summary = [record[len("out/") :]] + list(line.get("summary", []))
            tasks.append(
                base.make_task(
                    ctx,
                    kind=campaign["kind"],
                    readable_id="mc/" + line["id"],
                    command=line["command"],
                    env=line.get("env", {}),
                    inputs=inputs,
                    outputs={"root": "out", "summary": summary, "prune": []},
                    done={"file": record, "json": {}},
                    resources=line.get("resources"),
                    labels=dict(line.get("labels", {}), candidate=line["id"]),
                )
            )
        return tasks

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        return min(1800.0, float(task["resources"]["max_wall_s"]))

    def assemble(
        self,
        plan: Mapping[str, Any],
        tasks: List[Dict[str, Any]],
        fetched: Path,
        dest: Path,
        allow_mixed: bool = False,
    ) -> Dict[str, Any]:
        report = base.new_report()
        dest.mkdir(parents=True, exist_ok=True)
        lines = []
        for task in tasks:
            root = base.output_root(fetched, task)
            record_name = task["done"]["file"][len("out/") :]
            path = root / record_name if root else None
            if path is None or not path.is_file():
                report["missing"].append(task["id"])
                continue
            record = json.loads(path.read_text())
            shard = base.shard(base.task_record(fetched, task), task)
            lines.append(
                json.dumps(
                    {"candidate": task["labels"]["candidate"], "record": record, "shard": shard},
                    sort_keys=True,
                )
            )
            report["tasks"] += 1
        (dest / "dataset.jsonl").write_text("".join(line + "\n" for line in lines))
        report["paths"].append(str(dest / "dataset.jsonl"))
        return report


class McPlace(McEval):
    """Stage 0 of a halving run (placement starts) as one task with several processes."""

    name = "mc-place"
    short = "mcplace"
    default_resources = dict(cpus=4, memory_gb=8, disk_gb=10, max_wall_s=3600)

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        return min(120.0, float(task["resources"]["max_wall_s"]))
