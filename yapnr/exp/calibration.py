"""Calibration: measured speed factors and reference wall times from fetched campaign records.

The calibration campaign (docs/cloud-experiments.md, "Calibration") runs one fixed set of tasks
on the development Mac (the reference) and on each candidate machine type. ``ingest`` pairs the
records by kind and labels, and writes a ``yapnr-calibration-v1`` file:

- ``speed[<machine type>]`` and ``speed[<family>]``: the median over paired tasks of reference wall
  time / cloud wall time (1.0 = as fast as the reference core);
- ``reference_seconds[<kind>][<case>]``: the median reference wall time per case;
- ``samples`` and ``spread``: how many pairs each factor rests on and their interquartile range.

Only tasks with a real result count (verdict pass, fail or done); errors and timeouts are left out.
The owner config's ``[prices] calibration`` points the estimator at the file.
"""

from __future__ import annotations

import datetime as _dt
import json
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from yapnr.exp import spec

SCHEMA = "yapnr-calibration-v1"
USABLE = ("pass", "fail", "done")


def pair_key(record: Mapping[str, Any]) -> Tuple[str, str]:
    return record.get("kind", ""), spec.canonical_json(record.get("labels") or {}).decode()


def _usable(record: Mapping[str, Any]) -> bool:
    return record.get("verdict") in USABLE and (record.get("wall_s") or 0) > 0


def _quartiles(values: List[float]) -> Tuple[float, float]:
    if len(values) < 4:
        return min(values), max(values)
    q = statistics.quantiles(values, n=4)
    return q[0], q[2]


def ingest(
    reference: Iterable[Mapping[str, Any]],
    cloud: Iterable[Mapping[str, Any]],
    created: Optional[_dt.date] = None,
) -> Dict[str, Any]:
    ref_times: Dict[Tuple[str, str], List[float]] = {}
    by_case: Dict[str, Dict[str, List[float]]] = {}
    for record in reference:
        if not _usable(record):
            continue
        ref_times.setdefault(pair_key(record), []).append(float(record["wall_s"]))
        case = (record.get("labels") or {}).get("case")
        if case:
            by_case.setdefault(record["kind"], {}).setdefault(case, []).append(
                float(record["wall_s"])
            )
    ratios: Dict[str, List[float]] = {}
    for record in cloud:
        if not _usable(record):
            continue
        machine = (record.get("machine") or {}).get("machine_type")
        ref = ref_times.get(pair_key(record))
        if not machine or not ref:
            continue
        ratio = statistics.median(ref) / float(record["wall_s"])
        ratios.setdefault(machine, []).append(ratio)
        ratios.setdefault(machine.split("-")[0], []).append(ratio)
    speed = {k: round(statistics.median(v), 4) for k, v in sorted(ratios.items())}
    spread = {k: [round(x, 4) for x in _quartiles(v)] for k, v in sorted(ratios.items())}
    return {
        "schema": SCHEMA,
        "created": (created or _dt.date.today()).isoformat(),
        "reference": "the reference records' machine (the development Mac)",
        "speed": speed,
        "samples": {k: len(v) for k, v in sorted(ratios.items())},
        "spread": spread,
        "reference_seconds": {
            kind: {case: round(statistics.median(v), 1) for case, v in sorted(cases.items())}
            for kind, cases in sorted(by_case.items())
        },
    }


def write(data: Mapping[str, Any], path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
