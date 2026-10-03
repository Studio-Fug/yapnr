"""``rf-run``: one RF topology-optimisation case per task, resumable from its checkpoint.

The task runs ``python -m yapnr.rf.cases run CASE --out out/CASE`` from the source bundle. A run
directory with a checkpoint resumes bit for bit, so the task is ``restart: resume`` with the run
directory's top-level files (``checkpoint.npz``, ``history.json``, ``frames.npz``...) synced to
the store every ``sync_every_s`` and on a stop signal; caches below it are rebuilt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

from yapnr.exp.kinds import base

CASE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,60}$")


class RfRun(base.Kind):
    name = "rf-run"
    short = "rf"
    source_paths = ("yapnr",)
    default_resources = dict(cpus=4, memory_gb=8, disk_gb=10, max_wall_s=21600)
    default_determinism = "seeded"
    config_keys = frozenset({"args", "sync_every_s", "reference_seconds"})
    matrix_axes = ("case",)
    required_axes = ("case",)

    def check(self, campaign: Mapping[str, Any]) -> List[str]:
        errors = super().check(campaign)
        for case in campaign.get("matrix", {}).get("case", []):
            if not isinstance(case, str) or not CASE_RE.match(case):
                errors.append("matrix.case %r is not a case name" % (case,))
        args = campaign.get("config", {}).get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.append("config.args is a list of strings")
        return errors

    def expand(self, campaign: Mapping[str, Any], ctx: base.Context) -> List[Dict[str, Any]]:
        config = campaign.get("config", {})
        tasks = []
        for row in base.matrix_product(campaign["matrix"], ("case",)):
            case = row["case"]
            threads = str(ctx.resources.get("cpus", 4))
            tasks.append(
                base.make_task(
                    ctx,
                    kind=self.name,
                    readable_id="rf/" + case,
                    command=[
                        "${PYTHON}",
                        "-m",
                        "yapnr.rf.cases",
                        "run",
                        case,
                        "--out",
                        "out/" + case,
                    ]
                    + list(config.get("args", [])),
                    env={
                        "PYTHONPATH": "src",
                        "OMP_NUM_THREADS": threads,
                        "MKL_NUM_THREADS": threads,
                        "OPENBLAS_NUM_THREADS": threads,
                    },
                    outputs={
                        "root": "out",
                        "summary": ["%s/*.json" % case],
                        "prune": ["%s/cache" % case],
                    },
                    done={"file": "out/%s/validation.json" % case, "json": {}},
                    restart="resume",
                    checkpoint={
                        "path": "out/" + case,
                        "sync_every_s": int(config.get("sync_every_s", 300)),
                        "on_signal": True,
                    },
                    labels={"case": case},
                )
            )
        return tasks

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        return min(6600.0, float(task["resources"]["max_wall_s"]))

    def assemble(
        self,
        plan: Mapping[str, Any],
        tasks: List[Dict[str, Any]],
        fetched: Path,
        dest: Path,
        allow_mixed: bool = False,
    ) -> Dict[str, Any]:
        report = base.new_report()
        for task in tasks:
            case = task["labels"]["case"]
            root = base.output_root(fetched, task)
            if root is None or not (root / case).is_dir():
                report["missing"].append(task["id"])
                continue
            base.copy_tree(root / case, dest / case)
            report["tasks"] += 1
        report["paths"].append(str(dest))
        return report
