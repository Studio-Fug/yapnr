#!/usr/bin/env python3
"""radar60's ranking table over an `explore_jobs.toml` campaign's fetched results: DRC (every
severity, including the checks ``--severity-all`` never reports), opens by class, IR per rail,
LVDS/QSPI by connectivity and the R1 macro digest -- the knowledge of ``measure.json``'s shape
that tools/exp/pnr_explore_plan.py (generic) does not have.

    yapnr exp fetch PLAN
    python3 examples/radar60/board/explore_rank.py <store>/fetched/<campaign>/assembled/default

Reads ``dataset.jsonl`` (one line per task: ``{"candidate", "record", "shard"}``, written by
``mc-eval``'s own assembly -- docs/cloud-experiments.md); a task's ``record`` is this campaign's
``result.json`` (tools/exp/pnr_explore_job.py), whose ``data.measure`` is the embedded
``measure.json`` from its ``check`` step. A task that failed before ``check`` (or whose
``--embed`` found nothing there) has no ``measure`` and is listed as failed, not silently
dropped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _drc_total(measure: Dict[str, Any]) -> int:
    return sum(measure.get("drc_by_type", {}).values())


def _ir_fails(measure: Dict[str, Any]) -> List[str]:
    ir = measure.get("ir") or {}
    return sorted(net for net, rep in ir.items() if rep.get("status") not in ("pass", None))


def row(entry: Dict[str, Any]) -> Dict[str, Any]:
    record = entry.get("record") or {}
    measure = (record.get("data") or {}).get("measure")
    task = entry.get("candidate")
    if not record.get("ok") or measure is None:
        failed_step = next((s["step"] for s in record.get("steps", []) if s["returncode"]), None)
        return {
            "task": task,
            "ok": False,
            "failed_step": failed_step,
            "wall_s": record.get("wall_s"),
        }
    return {
        "task": task,
        "ok": True,
        "wall_s": record.get("wall_s"),
        "drc_total": _drc_total(measure),
        "drc_ignored_by_default": sum((measure.get("drc_ignored_by_default") or {}).values()),
        "unconnected": measure.get("unconnected"),
        "nets_connected": measure.get("nets_connected"),
        "nets_total": measure.get("nets_total"),
        "diff_pairs_legs": "%s/%s"
        % (measure.get("diff_pairs_connected_legs"), measure.get("diff_pairs_total_legs")),
        "qspi_max_length_mm": measure.get("qspi_max_length_mm"),
        "ir_fails": ",".join(_ir_fails(measure)) or "-",
        "r1_macro": measure.get("r1_macro"),
    }


def load(dataset: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]


def render(rows: List[Dict[str, Any]]) -> str:
    cols = [
        "task",
        "ok",
        "wall_s",
        "drc_total",
        "drc_ignored_by_default",
        "unconnected",
        "nets_connected",
        "nets_total",
        "diff_pairs_legs",
        "qspi_max_length_mm",
        "ir_fails",
        "r1_macro",
        "failed_step",
    ]
    present = [c for c in cols if any(c in r for r in rows)]
    lines = ["| " + " | ".join(present) + " |", "|" + "---|" * len(present)]
    for r in sorted(rows, key=lambda r: r["task"]):
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in present) + " |")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("assembled", type=Path, help="<store>/fetched/<campaign>/assembled/<config>")
    ap.add_argument("--json", action="store_true", help="print the rows as JSON, not a table")
    args = ap.parse_args(argv)
    entries = load(args.assembled / "dataset.jsonl")
    rows = [row(e) for e in entries]
    if args.json:
        print(json.dumps(rows, indent=1, sort_keys=True))
    else:
        print(render(rows))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
