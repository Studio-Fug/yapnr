"""Task durations from history, and the packing of a Batch job's tasks onto VMs.

Google Cloud Batch bills VM time, not task time (docs/cloud-experiments.md, "Cost model"): every
VM is billed from its start to its deletion, about 30 s before its first task and 45 s after its
last one, whether or not its slots are busy. So what a class costs depends on how its tasks are
laid out on VMs, and that needs a prediction of each task's duration.

Prediction (``Predictor``), per task, on the reference core (the estimator divides by the shape's
speed as before):

1. the task's cell (kind and labels without ``seed``) in the durations history: the median of
   its measured wall times, each scaled to the reference core by its machine family's speed;
2. other cells of the same kind and case (the median over them);
3. the calibration's ``reference_seconds`` for the case;
4. the kind's own default (``reference_seconds``), the safe value for an unseen cell.

The history (``yapnr-durations-v1``) is built from task records by ``yapnr exp durations
ingest`` (from fetched campaigns or straight from the runs store).

Packing (``pack``) of one class's pending tasks, all pure and deterministic:

- tasks predicted shorter than ``bundle_below_s`` are bundled (first-fit decreasing) into one
  Batch task each, of at most the longest task's duration, so one container start and bundle
  staging serve several cells; the wrapper runs a bundle's cells one after the other;
- extreme stragglers (units longer than ``straggler_ratio`` x what the rest needs) go to their
  own job, on a smaller shape of the family when one is usable, when that is predicted to cost
  less VM time (with the longest units first, a joint job already gathers them on its first VM,
  so on the same shape a split never saves);
- each job's VM count comes from its predicted work: as few VMs as keep the simulated makespan
  within ``1 + slack`` of the shortest the quota's VMs allow (about the longest unit when the
  work fits, so VMs stay busy and the makespan stays near the longest task);
- units are ordered longest first; Batch starts task indices in no particular order, so the
  wrapper's claims (``task.Claims``) hand them out in this order as tasks start (LPT).

Nothing about a task changes: the same lines of ``tasks.jsonl`` run with the same commands,
seeds and outputs; only which Batch task runs them, in which order and on how many VMs.
"""

from __future__ import annotations

import datetime as _dt
import heapq
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from yapnr.exp import spec

DURATIONS_SCHEMA = "yapnr-durations-v1"
USABLE = ("pass", "fail", "done")
# Measured on Batch Spot VMs (docs/cloud-experiments.md, "Cost model"); a price table's
# ``overheads`` may override each.
VM_BOOT_S = 30.0  # VM running (billed) until its first task starts
VM_IDLE_S = 45.0  # its last task's end until the VM is deleted
TASK_OVERHEAD_S = 10.0  # container start, bundle staging and upload, per Batch task
# Packing defaults.
BUNDLE_BELOW_S = 60.0
STRAGGLER_RATIO = 3.0
SLACK = 0.25
# Cells per bundle at most (a Batch task's timeout covers the sum of its cells' limits).
MAX_BUNDLE = 20


def cell_key(task: Mapping[str, Any]) -> str:
    """A task's cell: its labels without ``seed`` (seeds of one cell take about as long)."""
    labels = {k: v for k, v in (task.get("labels") or {}).items() if k != "seed"}
    return spec.canonical_json(labels).decode()


def _family(machine_type: Optional[str]) -> Optional[str]:
    return machine_type.split("-")[0] if machine_type else None


def ingest(
    records: Iterable[Mapping[str, Any]],
    previous: Optional[Mapping[str, Any]] = None,
    created: Optional[_dt.date] = None,
) -> Dict[str, Any]:
    """A durations history from task records (merged into ``previous``).

    Per kind, cell and machine (family, or ``reference`` for the development Mac's records): the
    measured wall times, at most the last 20, and how many of them hit the wall-time limit. A
    record counts once (by task, submission and attempt).
    """
    cells: Dict[str, Dict[str, Dict[str, Any]]] = {}
    seen = set()
    if previous:
        if previous.get("schema") != DURATIONS_SCHEMA:
            raise ValueError("not a %s file" % DURATIONS_SCHEMA)
        cells = json.loads(json.dumps(previous.get("cells", {})))
        seen = set(previous.get("seen", []))
    for record in records:
        timed_out = bool(record.get("timed_out"))
        if not (record.get("verdict") in USABLE or timed_out) or not record.get("wall_s"):
            continue
        ident = "%s/%s/%s/%s" % (
            record.get("campaign"),
            record.get("task"),
            record.get("submission"),
            record.get("attempt"),
        )
        if ident in seen:
            continue
        seen.add(ident)
        machine = _family((record.get("machine") or {}).get("machine_type"))
        if not machine:
            machine = "reference" if record.get("backend") == "local" else "unknown"
        entry = (
            cells.setdefault(record.get("kind", ""), {})
            .setdefault(cell_key(record), {})
            .setdefault(machine, {"wall_s": [], "timed_out": 0})
        )
        entry["wall_s"] = (entry["wall_s"] + [round(float(record["wall_s"]), 1)])[-20:]
        entry["timed_out"] += int(timed_out)
    return {
        "schema": DURATIONS_SCHEMA,
        "created": (created or _dt.date.today()).isoformat(),
        "cells": cells,
        "seen": sorted(seen),
    }


def records_from_text(text: str) -> List[Dict[str, Any]]:
    """Task records concatenated (``gcloud storage cat`` of several ``record.json``)."""
    decoder = json.JSONDecoder()
    out, index = [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return out
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            return out
        if isinstance(value, dict):
            out.append(value)


def load(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    target = Path(path).expanduser()
    if not target.is_file():
        return None
    data = json.loads(target.read_text())
    if data.get("schema") != DURATIONS_SCHEMA:
        raise ValueError("%s is not a %s file" % (target, DURATIONS_SCHEMA))
    return data


def write(data: Mapping[str, Any], path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


class Predictor:
    """Predicted reference-core seconds per task (see the module docstring for the order)."""

    def __init__(
        self,
        history: Optional[Mapping[str, Any]] = None,
        speed: Optional[Callable[[str], Optional[float]]] = None,
    ):
        self.cells = (history or {}).get("cells", {})
        self.speed = speed or (lambda family: None)
        self.sources: Dict[str, int] = {}

    def _reference_seconds(self, machines: Mapping[str, Mapping[str, Any]]) -> Optional[float]:
        values = []
        for machine, entry in machines.items():
            factor = 1.0 if machine == "reference" else self.speed(machine)
            if factor is None:
                continue
            values += [float(s) * factor for s in entry.get("wall_s", [])]
        return statistics.median(values) if values else None

    def predict(
        self,
        task: Mapping[str, Any],
        default: float,
        calibrated: Optional[float] = None,
    ) -> Tuple[float, str]:
        """(reference seconds, where they came from), at most the task's ``max_wall_s``."""
        limit = float(task["resources"]["max_wall_s"])
        kind_cells = self.cells.get(task.get("kind", ""), {})
        value = self._reference_seconds(kind_cells.get(cell_key(task), {}))
        source = "history"
        if value is None:
            case = (task.get("labels") or {}).get("case")
            values = []
            for key, machines in kind_cells.items() if case else ():
                if json.loads(key).get("case") == case:
                    one = self._reference_seconds(machines)
                    if one is not None:
                        values.append(one)
            if values:
                value, source = statistics.median(values), "history (case)"
        if value is None and calibrated is not None:
            value, source = calibrated, "calibration"
        if value is None:
            value, source = default, "kind default"
        self.sources[source] = self.sources.get(source, 0) + 1
        return min(value, limit), source


@dataclass
class Job:
    """One Batch job of a class: its tasks (each a list of plan lines) in submission order."""

    groups: List[List[int]]
    seconds: List[float]  # predicted seconds of each group on its shape, with the task overhead
    vms: int
    per_vm: int
    parallelism: int
    vm_hours: float = 0.0
    makespan_s: float = 0.0
    straggler: bool = False
    # The straggler job's smaller shape: its VM-hour price relative to the class's shape.
    price_ratio: float = 1.0
    # Where the job runs when not on the class's placement (the backend sets it; a cost.Placement).
    placement: Any = None

    @property
    def lines(self) -> List[int]:
        return [line for group in self.groups for line in group]

    def to_json(self) -> Dict[str, Any]:
        return {
            "groups": self.groups,
            "seconds": self.seconds,
            "vms": self.vms,
            "per_vm": self.per_vm,
            "parallelism": self.parallelism,
            "vm_hours": round(self.vm_hours, 4),
            "makespan_s": round(self.makespan_s, 1),
            "straggler": self.straggler,
            "price_ratio": round(self.price_ratio, 4),
        }


@dataclass
class Packing:
    jobs: List[Job] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def vm_hours(self) -> float:
        return sum(job.vm_hours for job in self.jobs)

    @property
    def priced_vm_hours(self) -> float:
        """VM-hours at the class shape's price (a straggler job's smaller VMs count less)."""
        return sum(job.vm_hours * job.price_ratio for job in self.jobs)

    @property
    def makespan_s(self) -> float:
        return max((job.makespan_s for job in self.jobs), default=0.0)

    @property
    def utilisation(self) -> float:
        """Busy slot-hours / billed slot-hours (1.0: every slot of every VM busy all along)."""
        busy = sum(sum(job.seconds) for job in self.jobs) / 3600.0
        billed = sum(job.vm_hours * job.per_vm for job in self.jobs)
        return busy / billed if billed else 0.0


def simulate(
    seconds: Sequence[float],
    vms: int,
    per_vm: int,
    boot_s: float = VM_BOOT_S,
    idle_s: float = VM_IDLE_S,
) -> Tuple[float, float]:
    """(VM-seconds, makespan seconds) of tasks handed out in order to ``vms`` x ``per_vm`` slots.

    Each next task starts on the slot that frees first (the wrapper's claims hand lines out in
    order as tasks start); a VM is billed from ``boot_s`` before its first task to ``idle_s``
    after its last one. A VM that gets no task is never started.
    """
    if not seconds:
        return 0.0, 0.0
    slots = [(0.0, vm, slot) for vm in range(max(1, vms)) for slot in range(max(1, per_vm))]
    heapq.heapify(slots)
    ends = [0.0] * max(1, vms)
    used = [False] * max(1, vms)
    for duration in seconds:
        start, vm, slot = heapq.heappop(slots)
        end = start + duration
        ends[vm] = max(ends[vm], end)
        used[vm] = True
        heapq.heappush(slots, (end, vm, slot))
    vm_seconds = sum(boot_s + end + idle_s for end, on in zip(ends, used) if on)
    return vm_seconds, boot_s + max(ends)


def _bundles(
    items: List[Tuple[float, int]], capacity: float, members: int = MAX_BUNDLE
) -> List[List[Tuple[float, int]]]:
    """First-fit decreasing of (seconds, line) into bins of ``capacity`` seconds."""
    bins: List[List[Tuple[float, int]]] = []
    loads: List[float] = []
    for item in items:
        for index, load in enumerate(loads):
            if load + item[0] <= capacity and len(bins[index]) < members:
                bins[index].append(item)
                loads[index] += item[0]
                break
        else:
            bins.append([item])
            loads.append(item[0])
    return bins


def vm_count(
    seconds: Sequence[float],
    per_vm: int,
    max_vms: int,
    slack: float = SLACK,
    boot_s: float = VM_BOOT_S,
    idle_s: float = VM_IDLE_S,
) -> int:
    """As few VMs as keep the makespan within ``1 + slack`` of the shortest one possible.

    The shortest possible makespan is that of every VM allowed (at most one per ``per_vm``
    units); fewer VMs keep their slots busier and bill less boot and idle time.
    """
    if not seconds:
        return 0
    top = max(1, min(max_vms, math.ceil(len(seconds) / per_vm)))
    best = simulate(seconds, top, per_vm, boot_s, idle_s)[1]
    for vms in range(1, top):
        if simulate(seconds, vms, per_vm, boot_s, idle_s)[1] <= (1.0 + slack) * best:
            return vms
    return top


def _job(units, per_vm, max_vms, slack, boot_s, idle_s, straggler=False, ratio=1.0) -> Job:
    durations = [s for s, _ in units]
    vms = vm_count(durations, per_vm, max_vms, slack, boot_s, idle_s)
    vm_seconds, makespan = simulate(durations, vms, per_vm, boot_s, idle_s)
    return Job(
        groups=[group for _, group in units],
        seconds=[round(s, 1) for s in durations],
        vms=vms,
        per_vm=per_vm,
        parallelism=min(len(units), vms * per_vm),
        vm_hours=vm_seconds / 3600.0,
        makespan_s=makespan,
        straggler=straggler,
        price_ratio=ratio,
    )


def pack(
    lines: Sequence[int],
    seconds: Mapping[int, float],
    *,
    per_vm: int,
    max_vms: int,
    overhead_s: float = TASK_OVERHEAD_S,
    boot_s: float = VM_BOOT_S,
    idle_s: float = VM_IDLE_S,
    bundle_below_s: float = BUNDLE_BELOW_S,
    max_bundle_s: Optional[float] = None,
    straggler_ratio: float = STRAGGLER_RATIO,
    small: Optional[Tuple[int, float]] = None,
    slack: float = SLACK,
    bundle: bool = True,
    max_members: int = MAX_BUNDLE,
) -> Packing:
    """The jobs of one class: ``seconds`` per plan line on the class's shape (no overhead).

    ``max_bundle_s`` caps a bundle's predicted seconds (a Batch task's own timeout covers the
    whole bundle); ``bundle=False`` keeps one cell per Batch task. ``small`` is a smaller shape of
    the same family for a straggler job: (tasks per VM, its VM-hour price / the class shape's).
    Stragglers get their own job only when that is predicted to cost less VM time: with the
    longest units first, a joint job already gathers them on its first VM.
    """
    per_vm = max(1, per_vm)
    max_vms = max(1, max_vms)
    order = sorted(lines, key=lambda line: (-seconds[line], line))
    out = Packing()
    if not order:
        return out
    longest = seconds[order[0]]
    short = [(seconds[line], line) for line in order if bundle and seconds[line] < bundle_below_s]
    units: List[Tuple[float, List[int]]] = [
        (seconds[line] + overhead_s, [line])
        for line in order
        if not (bundle and seconds[line] < bundle_below_s)
    ]
    if short:
        # A bundle runs at most as long as the longest task, so the makespan does not grow.
        capacity = max(bundle_below_s, longest)
        if max_bundle_s:
            capacity = min(capacity, max_bundle_s)
        bins = _bundles(short, capacity, max(1, min(MAX_BUNDLE, max_members)))
        for group in bins:
            units.append((sum(s for s, _ in group) + overhead_s, sorted(line for _, line in group)))
        if len(bins) < len(short):
            out.notes.append(
                "%d cells under %.0f s run as %d bundled tasks"
                % (len(short), bundle_below_s, len(bins))
            )
    units.sort(key=lambda unit: (-unit[0], unit[1][0]))
    joint = _job(units, per_vm, max_vms, slack, boot_s, idle_s)
    # Stragglers: units longer than straggler_ratio x what the rest needs on the VMs allowed.
    cut = 0
    while cut < len(units) - 1:
        rest = [s for s, _ in units[cut + 1 :]]
        if units[cut][0] > straggler_ratio * max(rest[0], sum(rest) / (max_vms * per_vm)):
            cut += 1
        else:
            break
    if cut:
        s_per_vm, ratio = small or (per_vm, 1.0)
        side = _job(units[:cut], s_per_vm, max_vms, slack, boot_s, idle_s, True, ratio)
        # side and main run at once and share the one quota max_vms is drawn from, so main
        # cannot also get the full max_vms: that lets the pair's VM count exceed the quota.
        main = _job(units[cut:], per_vm, max(1, max_vms - side.vms), slack, boot_s, idle_s)
        split_cost = side.vm_hours * ratio + main.vm_hours
        if split_cost < 0.98 * joint.vm_hours:
            out.jobs = [side, main]
            out.notes.append(
                "%d straggler task(s) of %.0f s or more run as their own job (%.2f instead of "
                "%.2f VM-hours)" % (cut, units[cut - 1][0], split_cost, joint.vm_hours)
            )
            return out
        out.notes.append(
            "%d straggler task(s) stay in the job: on its first VM they cost less than a job "
            "of their own" % cut
        )
    out.jobs = [joint]
    return out


def indices_text(groups: Sequence[Sequence[int]]) -> str:
    """``submissions/<n>.indices``: one line per backend task, the plan lines it runs."""
    return "".join(" ".join(str(line) for line in group) + "\n" for group in groups)


def parse_indices(text: str) -> List[List[int]]:
    return [[int(x) for x in line.split()] for line in text.splitlines() if line.strip()]
