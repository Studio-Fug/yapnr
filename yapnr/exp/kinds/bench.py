"""``bench-cell``: one tool-comparison cell (rung x tool x seed) per task, judged in the same task.

Each tool is a ``[tools.<name>]`` table: the command that routes a prepared rung, an optional judge
command (``measure.py`` and KiCad's DRC), its image (default: the campaign's) and its resources.
Commands take ``{rung}``, ``{seed}``, ``{tool}``, ``{rung_dir}`` (the rung's prepared inputs,
``bench/<rung>`` from the campaign's ``bench`` input) and ``{out}`` (the cell's output directory,
``out/<rung>/results/<tool>-s<seed>``) as format fields, and the toolchain placeholders.
``assemble`` writes the harness's layout: ``<rung>/results/<tool>-s<seed>/`` plus ``cells.json``.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Mapping

from yapnr.exp import spec
from yapnr.exp.kinds import base

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,60}$")
# {rung}, {seed}, {tool}, {rung_dir} and {out}; not the toolchain's ${PYTHON} placeholders.
FIELD_RE = re.compile(r"(?<!\$)\{(rung|seed|tool|rung_dir|out)\}")


def fill(arg: str, fields: Mapping[str, Any]) -> str:
    return FIELD_RE.sub(lambda m: str(fields[m.group(1)]), arg)


TOOL_KEYS = {
    "command",
    "judge",
    "image",
    "entrypoint",
    "cpus",
    "memory_gb",
    "disk_gb",
    "max_wall_s",
    "summary",
    "done_file",
    "verdict_json_path",
    "reference_seconds",
    "env",
}


class BenchCell(base.Kind):
    name = "bench-cell"
    short = "bench"
    source_paths = None
    default_resources = dict(cpus=1, memory_gb=3, disk_gb=4, max_wall_s=3600)
    default_determinism = "wall_clock_budgeted"
    config_keys = frozenset({"rungs_input"})
    matrix_axes = ("rung", "tool", "seed")
    required_axes = ("rung", "tool", "seed")

    def check(self, campaign: Mapping[str, Any]) -> List[str]:
        errors = super().check(campaign)
        tools = campaign.get("tools", {})
        matrix = campaign.get("matrix", {})
        for name in matrix.get("tool", []):
            if name not in tools:
                errors.append("matrix.tool names %r, which has no [tools.%s]" % (name, name))
        for name in matrix.get("rung", []):
            if not isinstance(name, str) or not NAME_RE.match(name):
                errors.append("matrix.rung %r is not a rung name" % (name,))
        for name, tool in tools.items():
            if not NAME_RE.match(name) or not isinstance(tool, dict):
                errors.append("[tools.%s]: a lower-case name and a table" % name)
                continue
            for key in sorted(set(tool) - TOOL_KEYS):
                errors.append("tools.%s.%s is not a tool option" % (name, key))
            for key in ("command", "judge"):
                if key in tool and not (
                    isinstance(tool[key], list)
                    and tool[key]
                    and all(isinstance(x, str) for x in tool[key])
                ):
                    errors.append("tools.%s.%s is a list of strings" % (name, key))
            if "command" not in tool:
                errors.append("tools.%s.command is required" % name)
            if "image" in tool and not spec.IMAGE_RE.match(str(tool["image"])):
                errors.append("tools.%s.image is an image reference" % name)
            errors += [
                "tools.%s: %s" % (name, e)
                for e in spec.resources_errors(
                    {k: tool[k] for k in spec.RESOURCE_KEYS if k in tool}
                )
            ]
        return errors

    def expand(self, campaign: Mapping[str, Any], ctx: base.Context) -> List[Dict[str, Any]]:
        tools = campaign["tools"]
        rungs_input = campaign.get("config", {}).get("rungs_input", "bench")
        tasks = []
        for row in base.matrix_product(campaign["matrix"], ("rung", "tool", "seed")):
            rung, tool_name, seed = row["rung"], row["tool"], row["seed"]
            tool = tools[tool_name]
            cell = "%s/results/%s-s%s" % (rung, tool_name, seed)
            fields = dict(
                rung=rung,
                seed=seed,
                tool=tool_name,
                rung_dir="%s/%s" % (rungs_input, rung),
                out="out/" + cell,
            )
            steps = [["mkdir", "-p", fields["out"]], [fill(a, fields) for a in tool["command"]]]
            if tool.get("judge"):
                steps.append([fill(a, fields) for a in tool["judge"]])
            script = "\n".join(shlex.join(step) for step in steps)
            resources = {k: tool[k] for k in spec.RESOURCE_KEYS if k in tool}
            env = dict(base.SINGLE_THREAD_ENV) if resources.get("cpus", 1) == 1 else {}
            env.update(tool.get("env", {}))
            done_file = tool.get("done_file", "measure.json")
            verdict_path = tool.get("verdict_json_path")
            tasks.append(
                base.make_task(
                    ctx,
                    kind=self.name,
                    readable_id="bench/%s/%s/s%s" % (rung, tool_name, seed),
                    command=["/bin/sh", "-ec", script],
                    env=env,
                    outputs={
                        "root": "out",
                        "summary": [
                            "%s/%s" % (cell, g) for g in tool.get("summary", ["*.json", "*.log"])
                        ],
                        "prune": [],
                    },
                    done={"file": "%s/%s" % (fields["out"], done_file), "json": {}},
                    verdict=(
                        {"file": "%s/%s" % (fields["out"], done_file), "json_path": verdict_path}
                        if verdict_path
                        else None
                    ),
                    resources=resources,
                    labels={"rung": rung, "tool": tool_name, "seed": str(seed)},
                    image=tool.get("image"),
                    entrypoint=tool.get("entrypoint", base.IMAGE_ENTRYPOINT),
                )
            )
        return tasks

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        return min(120.0, float(task["resources"]["max_wall_s"]))

    def assemble(
        self,
        plan: Mapping[str, Any],
        tasks: List[Dict[str, Any]],
        fetched: Path,
        dest: Path,
        allow_mixed: bool = False,
    ) -> Dict[str, Any]:
        report = base.new_report()
        cells = []
        for task in tasks:
            labels = task["labels"]
            cell = "%s/results/%s-s%s" % (labels["rung"], labels["tool"], labels["seed"])
            root = base.output_root(fetched, task)
            record = base.task_record(fetched, task)
            if root is None or not (root / cell).is_dir():
                report["missing"].append(task["id"])
                continue
            base.copy_tree(root / cell, dest / cell)
            cells.append(dict(labels, **base.shard(record, task)))  # the shard names the task
            report["tasks"] += 1
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "cells.json").write_text(
            json.dumps({"schema": "yapnr-bench-cells-v1", "cells": cells}, indent=2)
        )
        report["paths"].append(str(dest))
        return report
