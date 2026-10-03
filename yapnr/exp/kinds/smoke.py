"""``smoke``: tiny tasks that prove a backend end to end (and drill the kill switch).

Each task writes ``out/smoke.json`` with the interpreter and platform it ran on, optionally runs
``kicad-cli version``, sleeps ``sleep_s`` and exits with ``exit_code``. ``fail_until_retry`` makes
the first attempts exit 75 (a transient failure the backend retries). No source bundle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

from yapnr.exp.kinds import base

SCRIPT = """\
import json, os, platform, subprocess, sys, time
out, sleep_s, kicad, exit_code, fail_until = sys.argv[1], float(sys.argv[2]), sys.argv[3], \
int(sys.argv[4]), int(sys.argv[5])
if int(os.environ.get("YAPNR_ATTEMPT", "s0r0").split("r")[-1].split("-")[0]) < fail_until:
    sys.exit(75)
info = dict(python=platform.python_version(), platform=sys.platform + "-" + platform.machine(),
            task=os.environ.get("YAPNR_TASK_ID"), cpus=os.cpu_count())
if kicad:
    info["kicad"] = subprocess.run([kicad, "version"], capture_output=True, text=True,
                                   timeout=120).stdout.strip()
time.sleep(sleep_s)
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as handle:
    json.dump(info, handle)
sys.exit(exit_code)
"""


class Smoke(base.Kind):
    name = "smoke"
    short = "smoke"
    source_paths = ()
    default_resources = dict(cpus=1, memory_gb=1, disk_gb=2, max_wall_s=600)
    config_keys = frozenset({"count", "sleep_s", "kicad", "exit_code", "fail_until_retry"})
    matrix_axes = ("n",)

    def check(self, campaign: Mapping[str, Any]) -> List[str]:
        errors = super().check(campaign)
        config = campaign.get("config", {})
        for key in ("count", "exit_code", "fail_until_retry"):
            if key in config and (
                isinstance(config[key], bool) or not isinstance(config[key], int)
            ):
                errors.append("config.%s is an integer" % key)
        return errors

    def expand(self, campaign: Mapping[str, Any], ctx: base.Context) -> List[Dict[str, Any]]:
        config = campaign.get("config", {})
        matrix = campaign.get("matrix") or {"n": list(range(int(config.get("count", 1))))}
        tasks = []
        for row in base.matrix_product(matrix, ("n",)):
            n = row["n"]
            tasks.append(
                base.make_task(
                    ctx,
                    kind=self.name,
                    readable_id="smoke/%s" % n,
                    command=[
                        "${PYTHON}",
                        "-c",
                        SCRIPT,
                        "out/smoke.json",
                        str(config.get("sleep_s", 0)),
                        "${KICAD_CLI}" if config.get("kicad") else "",
                        str(config.get("exit_code", 0)),
                        str(config.get("fail_until_retry", 0)),
                    ],
                    outputs={"root": "out", "summary": ["smoke.json"], "prune": []},
                    done={"file": "out/smoke.json", "json": {}},
                    labels={"n": str(n)},
                )
            )
        return tasks

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        return float(task["command"][4]) + 5.0

    def assemble(
        self,
        plan: Mapping[str, Any],
        tasks: List[Dict[str, Any]],
        fetched: Path,
        dest: Path,
        allow_mixed: bool = False,
    ) -> Dict[str, Any]:
        report = base.new_report()
        rows = []
        for task in tasks:
            root = base.output_root(fetched, task)
            record = base.task_record(fetched, task) or {}
            if root is None:
                report["missing"].append(task["id"])
                continue
            info = None
            if (root / "smoke.json").is_file():
                info = json.loads((root / "smoke.json").read_text())
            rows.append(dict(task=task["id"], verdict=record.get("verdict"), info=info))
            report["tasks"] += 1
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "smoke.json").write_text(json.dumps(rows, indent=2))
        report["paths"].append(str(dest / "smoke.json"))
        return report
