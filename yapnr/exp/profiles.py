"""Where a campaign's time goes: the ``pnr.profile`` records of a profiled campaign, aggregated.

A campaign with ``[profile] enabled = true`` runs every task with ``PNR_PROFILE_DIR`` set; each
profiled engine process (``pnr.profile.run``) writes one ``pnr-profile-v1`` record: its label,
wall and CPU seconds, its ``span()`` totals and its 100 hottest functions by self time. The
wrapper uploads those records beside the task's result, and ``yapnr exp fetch`` copies them to
``<fetched>/<cid>/tasks/<task>/profiles/``.

``aggregate`` adds them up over the campaign:

- per process label (a pipeline stage): processes, wall and CPU seconds, the share of the tasks'
  summed wall time;
- per ``span()`` name: calls and wall seconds;
- per function (file below the engine's package root, and name): self seconds and calls, summed
  over processes (each process lists its top 100, so a function that is never in a top 100 is
  missing: the totals are lower bounds);
- per module: self seconds;
- what no profiled process covers (task wall time outside every record: KiCad CLI and other
  native subprocesses, staging, the runner itself);
- per runner stage, from a runner's own ``result.json`` (``stages``/``cpu_stages`` seconds, as the
  regression runner writes them): needs no ``[profile]``, so every campaign has it.

With the campaign's VM-time cost (``yapnr exp cost``), each share is also given in VM-hours and
dollars: the VM time a stage takes is its share of the busy slot time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA = "pnr-profile-v1"
RUNNER_STAGES = "runner-stages"  # stage timing from a runner's result.json
ROOTS = ("hardware/pnr/", "site-packages/")


def short_path(path: str) -> str:
    """A function's file below a known root (``pnr/router.py``), else its last two parts."""
    text = str(path).replace("\\", "/")
    for root in ROOTS:
        if root in text:
            return text.split(root, 1)[1]
    if text.startswith(("~", "<")):
        return text  # built-ins ("~") and generated code ("<string>")
    return "/".join(text.split("/")[-2:])


def load_task_profiles(fetched: Path) -> List[Tuple[str, List[Dict[str, Any]], Optional[float]]]:
    """(task directory name, its profile records, its wall seconds) for every fetched task."""
    out = []
    for task_dir in sorted((Path(fetched) / "tasks").glob("*")):
        if not task_dir.is_dir():
            continue
        records = []
        for path in sorted((task_dir / "profiles").glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue
            if data.get("schema") == SCHEMA:
                records.append(data)
        wall = None
        record = task_dir / "record.json"
        if record.is_file():
            try:
                wall = float(json.loads(record.read_text()).get("wall_s") or 0) or None
            except ValueError:
                wall = None
        for result in sorted((task_dir / "summary").rglob("result.json")):
            # A runner's own stage timing (the regression runner's result.json: "stages" and
            # "cpu_stages", seconds per pipeline stage), with or without [profile].
            try:
                data = json.loads(result.read_text())
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("stages"), dict):
                cpu = data.get("cpu_stages") if isinstance(data.get("cpu_stages"), dict) else {}
                records.append(
                    {
                        "schema": RUNNER_STAGES,
                        "stages": {
                            str(k): (float(v), float(cpu.get(k) or 0))
                            for k, v in data["stages"].items()
                            if isinstance(v, (int, float))
                        },
                    }
                )
        out.append((task_dir.name, records, wall))
    return out


def aggregate(
    tasks: Iterable[Tuple[str, List[Mapping[str, Any]], Optional[float]]],
    cost: Optional[Mapping[str, Any]] = None,
    top: int = 25,
) -> Dict[str, Any]:
    stages: Dict[str, Dict[str, float]] = {}
    runner: Dict[str, Dict[str, float]] = {}
    spans: Dict[str, Dict[str, float]] = {}
    functions: Dict[Tuple[str, str], Dict[str, float]] = {}
    modules: Dict[str, float] = {}
    task_wall = 0.0
    profiled_wall = 0.0
    tasks_seen = tasks_profiled = processes = 0
    for _, records, wall in tasks:
        tasks_seen += 1
        task_wall += wall or 0.0
        if any(r.get("schema") == SCHEMA for r in records):
            tasks_profiled += 1
        covered = 0.0
        for record in records:
            if record.get("schema") == RUNNER_STAGES:
                for name, (stage_wall, stage_cpu) in record["stages"].items():
                    one = runner.setdefault(name, {"cells": 0, "wall_s": 0.0, "cpu_s": 0.0})
                    one["cells"] += 1
                    one["wall_s"] += stage_wall
                    one["cpu_s"] += stage_cpu
                continue
            processes += 1
            label = str(record.get("label") or "?")
            stage = stages.setdefault(label, {"processes": 0, "wall_s": 0.0, "cpu_s": 0.0})
            stage["processes"] += 1
            stage["wall_s"] += float(record.get("wall_seconds") or 0)
            stage["cpu_s"] += float(record.get("cpu_seconds") or 0)
            covered += float(record.get("wall_seconds") or 0)
            for name, item in (record.get("spans") or {}).items():
                one = spans.setdefault(name, {"calls": 0, "wall_s": 0.0, "cpu_s": 0.0})
                one["calls"] += int(item.get("calls") or 0)
                one["wall_s"] += float(item.get("wall_seconds") or 0)
                one["cpu_s"] += float(item.get("cpu_seconds") or 0)
            for row in record.get("hot_functions") or []:
                path = short_path(row.get("file", ""))
                key = (path, "%s:%s" % (row.get("function"), row.get("line")))
                one = functions.setdefault(key, {"self_s": 0.0, "calls": 0, "processes": 0})
                one["self_s"] += float(row.get("self_seconds") or 0)
                one["calls"] += int(row.get("calls") or 0)
                one["processes"] += 1
                modules[path] = modules.get(path, 0.0) + float(row.get("self_seconds") or 0)
        # Nested profiled processes are not expected (pnr.profile profiles one per process
        # tree); clamp so a task never shows more profiled than wall time.
        profiled_wall += min(covered, wall) if wall else covered
    hours = (cost or {}).get("vm_hours")
    usd = (cost or {}).get("usd")

    def share(seconds: float) -> Dict[str, Any]:
        part = seconds / task_wall if task_wall else 0.0
        out = {"share": round(part, 4)}
        if hours:
            out["vm_hours"] = round(part * hours, 4)
        if usd:
            out["usd"] = round(part * usd, 4)
        return out

    def ranked(items, key, limit):
        return sorted(items, key=lambda kv: -kv[1][key])[:limit]

    return {
        "tasks": tasks_seen,
        "tasks_profiled": tasks_profiled,
        "processes": processes,
        "task_wall_h": round(task_wall / 3600.0, 4),
        "profiled_wall_h": round(profiled_wall / 3600.0, 4),
        "unprofiled": dict(
            wall_h=round(max(0.0, task_wall - profiled_wall) / 3600.0, 4),
            **share(max(0.0, task_wall - profiled_wall)),
        ),
        "vm_hours": hours,
        "usd": usd,
        "stages": [
            dict(
                label=label,
                processes=int(v["processes"]),
                wall_h=round(v["wall_s"] / 3600, 4),
                cpu_h=round(v["cpu_s"] / 3600, 4),
                **share(v["wall_s"]),
            )
            for label, v in ranked(stages.items(), "wall_s", top)
        ],
        "runner_stages": [
            dict(
                stage=name,
                cells=int(v["cells"]),
                wall_h=round(v["wall_s"] / 3600, 4),
                cpu_h=round(v["cpu_s"] / 3600, 4),
                **share(v["wall_s"]),
            )
            for name, v in ranked(runner.items(), "wall_s", top)
        ],
        "spans": [
            dict(
                name=name,
                calls=int(v["calls"]),
                wall_h=round(v["wall_s"] / 3600, 4),
                **share(v["wall_s"]),
            )
            for name, v in ranked(spans.items(), "wall_s", top)
        ],
        "functions": [
            dict(
                file=path,
                function=func,
                self_h=round(v["self_s"] / 3600, 4),
                calls=int(v["calls"]),
                processes=int(v["processes"]),
                **share(v["self_s"]),
            )
            for (path, func), v in sorted(functions.items(), key=lambda kv: -kv[1]["self_s"])[:top]
        ],
        "modules": [
            dict(file=path, self_h=round(seconds / 3600, 4), **share(seconds))
            for path, seconds in sorted(modules.items(), key=lambda kv: -kv[1])[:top]
        ],
        "note": "function and module totals sum each process's 100 hottest functions by self "
        "time (lower bounds); shares are of the tasks' summed wall time, VM-hours and $ that "
        "share of the campaign's measured VM time",
    }


def table(report: Mapping[str, Any]) -> str:
    lines = [
        "%d task(s), %d profiled, %d profiled process(es); task wall %.2f h, profiled %.2f h, "
        "not covered %.2f h (%.0f%%)"
        % (
            report["tasks"],
            report["tasks_profiled"],
            report["processes"],
            report["task_wall_h"],
            report["profiled_wall_h"],
            report["unprofiled"]["wall_h"],
            report["unprofiled"]["share"] * 100,
        )
    ]
    if report.get("vm_hours"):
        lines.append(
            "VM time %.3f VM-h, $%.3f (yapnr exp cost)" % (report["vm_hours"], report["usd"] or 0)
        )

    def extra(row):
        return "  %.3f VM-h" % row["vm_hours"] if "vm_hours" in row else ""

    if report.get("runner_stages"):
        lines.append("runner stages (result.json stage timing, summed over cells):")
        for row in report["runner_stages"]:
            lines.append(
                "  %-34s %4d cells  wall %7.3f h  cpu %7.3f h  %5.1f%%%s"
                % (
                    row["stage"][:34],
                    row["cells"],
                    row["wall_h"],
                    row["cpu_h"],
                    row["share"] * 100,
                    extra(row),
                )
            )
    lines.append("stages (profiled processes by label):")
    for row in report["stages"]:
        lines.append(
            "  %-34s %4d proc  wall %7.3f h  cpu %7.3f h  %5.1f%%%s"
            % (
                row["label"][:34],
                row["processes"],
                row["wall_h"],
                row["cpu_h"],
                row["share"] * 100,
                extra(row),
            )
        )
    lines.append("spans:")
    for row in report["spans"]:
        lines.append(
            "  %-34s %8d calls  wall %7.3f h  %5.1f%%%s"
            % (row["name"][:34], row["calls"], row["wall_h"], row["share"] * 100, extra(row))
        )
    lines.append("hot functions (self time):")
    for row in report["functions"]:
        lines.append(
            "  %-40s %-34s %7.3f h  %5.1f%%  %d calls%s"
            % (
                row["file"][-40:],
                row["function"][:34],
                row["self_h"],
                row["share"] * 100,
                row["calls"],
                extra(row),
            )
        )
    lines.append("modules (self time):")
    for row in report["modules"]:
        lines.append(
            "  %-48s %7.3f h  %5.1f%%%s"
            % (row["file"][-48:], row["self_h"], row["share"] * 100, extra(row))
        )
    lines.append("note: " + report["note"])
    return "\n".join(lines) + "\n"
