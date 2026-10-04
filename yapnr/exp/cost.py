"""Prices, machine shapes, estimates and the per-submit spending caps.

Prices come from a dated price table (``data/gcp-spot-prices.json``, or the owner's refreshed copy
from ``yapnr exp prices``); speeds come from the table's PassMark ratios until a calibration file
(``yapnr exp calibration ingest``) replaces them with measurements. Every plan prints two numbers
(docs/design/cloud-experiments.md, section 10):

- expected: the sum over tasks of (reference wall time / speed) on the chosen shape, plus 8% for
  preemption rework and VM start-up, plus boot disks;
- ceiling: the smaller of every task at its maximum wall time with every retry, and the quota
  ceiling ``max_parallel_vcpus x max_campaign_hours``, at the same prices.

``submit`` asks for confirmation above ``confirm_usd`` and refuses a ceiling above ``refuse_usd``
unless ``--max-usd`` raises it, never above ``hard_refuse_usd``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

DATA = Path(__file__).resolve().parent / "data"
DEFAULT_TABLE = DATA / "gcp-spot-prices.json"
TYPES = ("highcpu", "standard", "highmem")
# us-east1 and us-west1 share us-central1's Spot prices (Spot pricing page, 2026-10-02).
SHARED_PRICES = {"us-east1": "us-central1", "us-west1": "us-central1"}
MAX_TASKS_PER_VM = 20  # Batch's limit (docs.cloud.google.com/batch/docs/create-run-job)
MEMORY_RESERVE_GB = 1.0  # left to the OS, Docker and the Batch agent on every VM
MEMORY_USABLE = 0.9
# Boot disk space that task work directories cannot use: Container-Optimized OS, the unpacked
# image (a few GB) and Docker's logs. A planning reserve, not a measurement; Batch itself does not
# pack tasks by disk, so tasks per VM are capped here to keep their disk_gb within the rest.
BOOT_DISK_RESERVE_GB = 12.0
# What a Batch attempt may run beyond the task's max_wall_s: the runnable's timeout adds 300 s
# for the upload, maxRunDuration 300 s more (backends/gcp_batch.py). The ceiling prices this.
BATCH_ATTEMPT_GRACE_S = 600
# The reaper (infra/gcp/functions/guard) cancels a job past its deadline label every 15 minutes.
REAPER_INTERVAL_H = 0.25


class CostError(ValueError):
    pass


class PriceTable:
    def __init__(self, data: Mapping[str, Any], path: Optional[str] = None):
        if data.get("schema") != "yapnr-price-table-v1":
            raise CostError("%s is not a yapnr-price-table-v1" % (path or "price table"))
        self.data = data
        self.path = path
        self.families = data["families"]

    @classmethod
    def load(cls, path: Optional[str] = None) -> "PriceTable":
        """The owner's table (``[prices] table``), else the committed snapshot."""
        target = Path(path).expanduser() if path else DEFAULT_TABLE
        label = str(target) if path else "yapnr/exp/data/%s (committed snapshot)" % target.name
        return cls(json.loads(target.read_text()), label)

    @property
    def accessed(self) -> str:
        return self.data.get("accessed", "unknown")

    def family(self, name: str) -> Mapping[str, Any]:
        try:
            return self.families[name]
        except KeyError:
            raise CostError("no prices for machine family %r" % name) from None

    def rate(self, family: str, region: str, model: str = "spot") -> Tuple[float, float, str]:
        """($ per vCPU-hour, $ per GB-hour, where the price came from)."""
        fam = self.family(family)
        prices = fam.get("spot" if model == "spot" else "on_demand", {})
        if region in prices:
            vcpu, gb = prices[region]
            return float(vcpu), float(gb), "table %s %s" % (self.accessed, region)
        shared = SHARED_PRICES.get(region)
        if shared in prices:
            vcpu, gb = prices[shared]
            return (
                float(vcpu),
                float(gb),
                "table %s %s (shared with %s)"
                % (
                    self.accessed,
                    shared,
                    region,
                ),
            )
        if "us-central1" in prices:
            vcpu, gb = prices["us-central1"]
            return (
                float(vcpu),
                float(gb),
                "table %s us-central1 (no %s price; fallback)"
                % (
                    self.accessed,
                    region,
                ),
            )
        raise CostError("no %s price for %s in %s" % (model, family, region))

    def disk_hour(self, disk_type: str, gib: float) -> float:
        per_month = self.data["disks"].get(disk_type, self.data["disks"]["pd-balanced"])
        return per_month["gib_month"] * gib / 730.0

    @property
    def rework(self) -> float:
        return float(self.data["overheads"]["preemption_rework"])

    @property
    def vm_start_s(self) -> float:
        return float(self.data["overheads"]["vm_start_s"])


class Calibration:
    """Measured speed factors (by machine type or family) and reference wall times per kind."""

    def __init__(self, data: Optional[Mapping[str, Any]] = None, path: Optional[str] = None):
        data = data or {}
        if data and data.get("schema") != "yapnr-calibration-v1":
            raise CostError("%s is not a yapnr-calibration-v1" % (path or "calibration"))
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: Optional[str]) -> "Calibration":
        if not path:
            return cls()
        target = Path(path).expanduser()
        if not target.is_file():
            return cls()
        return cls(json.loads(target.read_text()), str(target))

    def speed(self, shape: str, family: str) -> Optional[float]:
        speeds = self.data.get("speed", {})
        for key in (shape, family):
            if key in speeds:
                return float(speeds[key])
        return None

    def reference_seconds(self, kind: str, key: str) -> Optional[float]:
        value = self.data.get("reference_seconds", {}).get(kind, {}).get(key)
        return float(value) if value is not None else None


@dataclass
class Placement:
    """Where one resource class runs on Google Cloud: family, region, shape and packing."""

    family: str
    region: str
    shape: str
    vm_vcpus: int
    vm_memory_gb: float
    threads_per_core: int
    cpu_milli: int
    memory_mib: int
    tasks_per_vm: int
    boot_disk: str
    template: bool
    arch: str
    model: str
    vcpu_price: float
    gb_price: float
    price_source: str
    speed: float
    speed_source: str

    @property
    def vm_hour(self) -> float:
        return self.vm_vcpus * self.vcpu_price + self.vm_memory_gb * self.gb_price

    def to_json(self) -> Dict[str, Any]:
        out = asdict(self)
        out["vm_hour_usd"] = round(self.vm_hour, 5)
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Placement":
        """A placement from ``to_json`` (keys it added, such as prices, are left out)."""
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in names})

    @property
    def pair(self) -> str:
        return "%s/%s" % (self.family, self.region)


def choose_shape(
    table: PriceTable,
    family: str,
    cpus: int,
    memory_gb: float,
    vm_vcpus: int = 16,
    packing: str = "core",
    disk_gb: float = 0.0,
    disk_free_gb: Optional[float] = None,
) -> Tuple[str, int, float, int, int]:
    """(shape, vm vCPUs, vm memory GB, cpuMilli, tasks per VM) for tasks of ``cpus`` cores.

    With ``disk_free_gb`` (the boot disk less ``BOOT_DISK_RESERVE_GB``), the tasks on one VM also
    keep their ``disk_gb`` within it.
    """
    fam = table.family(family)
    threads = int(fam["threads_per_core"])
    task_vcpus = cpus * (threads if packing == "core" else 1)
    sizes = sorted(fam["vcpus"])
    if not sizes:
        raise CostError("%s has no shapes Batch can use" % family)
    candidates = [v for v in sizes if v >= max(vm_vcpus, task_vcpus)] or [sizes[-1]]
    vcpus = candidates[0]
    if vcpus < task_vcpus:
        raise CostError("%s has no shape with %d vCPUs" % (family, task_vcpus))
    per_vm = max(1, min(MAX_TASKS_PER_VM, vcpus // task_vcpus))
    if disk_gb > 0 and disk_free_gb is not None:
        if disk_gb > disk_free_gb:
            raise CostError(
                "a task of %.1f GB of disk does not fit the %.1f GB a boot disk leaves for tasks "
                "(raise boot_disk_gb)" % (disk_gb, disk_free_gb)
            )
        per_vm = min(per_vm, int(disk_free_gb // disk_gb))
    while per_vm >= 1:
        for kind in TYPES:
            ratio = fam["memory_gb_per_vcpu"].get(kind)
            if ratio is None:
                continue
            vm_memory = ratio * vcpus
            if per_vm * memory_gb <= vm_memory * MEMORY_USABLE - MEMORY_RESERVE_GB:
                return (
                    "%s-%s-%d" % (family, kind, vcpus),
                    vcpus,
                    vm_memory,
                    task_vcpus * 1000,
                    per_vm,
                )
        per_vm -= 1
    raise CostError(
        "a %s VM of %d vCPUs cannot hold a task of %.1f GB" % (family, vcpus, memory_gb)
    )


def ranked_pairs(
    ranking: Sequence[Tuple[str, str]], families: Sequence[str], regions: Sequence[str]
) -> List[Tuple[str, str]]:
    """The owner's ranked (family, region) pairs that ``families`` and ``regions`` allow."""
    return [(f, r) for f, r in ranking if f in families and r in regions]


def place(table: PriceTable, calibration: Calibration, **kwargs) -> Placement:
    """Where a resource class runs: the best of ``placements``."""
    return placements(table, calibration, **kwargs)[0]


def placements(
    table: PriceTable,
    calibration: Calibration,
    *,
    cpus: int,
    memory_gb: float,
    families: Sequence[str],
    regions: Sequence[str],
    ranking: Sequence[Tuple[str, str]] = (),
    vm_vcpus: int = 16,
    packing: str = "core",
    model: str = "spot",
    template_families: Sequence[str] = ("c4d", "c4", "c4a", "n4"),
    arch: Optional[str] = None,
    prefer: str = "cost",
    disk_gb: float = 0.0,
    disk_free_gb: Optional[float] = None,
) -> List[Placement]:
    """Every usable (family, region, shape) among the allowed ones, best first.

    - ``ranking`` (the owner's ranked pairs, from the calibration) wins when given: the usable
      pairs, in order (``submit`` takes the first whose region has Spot quota for one more VM).
    - ``prefer="first-family"``: the first family of ``families`` that has a usable shape, in its
      cheapest region (the fastest core first: wall-clock-budgeted campaigns, section 6.2).
    - ``prefer="cost"``: the cheapest pair per result (price / speed).
    """
    if prefer not in ("cost", "first-family"):
        raise CostError("prefer is 'cost' or 'first-family'")
    pairs = ranked_pairs(ranking, families, regions) or [(f, r) for f in families for r in regions]
    ranked = bool(ranked_pairs(ranking, families, regions))
    found: List[Tuple[Tuple[float, float], Placement]] = []
    errors = []
    for rank, (family, region) in enumerate(pairs):
        try:
            fam = table.family(family)
            if not fam.get("batch"):
                raise CostError("%s is not supported by Batch" % family)
            if arch and fam["arch"] != arch:
                raise CostError("%s is %s, the image is pinned to %s" % (family, fam["arch"], arch))
            shape, vcpus, vm_mem, cpu_milli, per_vm = choose_shape(
                table, family, cpus, memory_gb, vm_vcpus, packing, disk_gb, disk_free_gb
            )
            vcpu_price, gb_price, source = table.rate(family, region, model)
        except CostError as err:
            errors.append(str(err))
            continue
        speed = calibration.speed(shape, family)
        speed_source = "calibration" if speed else "table (PassMark single-thread ratio)"
        speed = speed or float(fam["speed_vs_reference"])
        if packing == "vcpu" and fam["threads_per_core"] > 1:
            speed *= 0.6  # an assumed SMT yield until the calibration measures both packings
            speed_source += ", SMT-shared core (x0.6 assumed)"
        placement = Placement(
            family=family,
            region=region,
            shape=shape,
            vm_vcpus=vcpus,
            vm_memory_gb=vm_mem,
            threads_per_core=int(fam["threads_per_core"]),
            cpu_milli=cpu_milli,
            memory_mib=int(math.ceil(memory_gb * 1024)),
            tasks_per_vm=per_vm,
            boot_disk=fam["boot_disk"],
            template=family in template_families,
            arch=fam["arch"],
            model=model,
            vcpu_price=vcpu_price,
            gb_price=gb_price,
            price_source=source,
            speed=speed,
            speed_source=speed_source,
        )
        per_result = (placement.vm_hour / per_vm) / speed
        if ranked:
            key = (float(rank), 0.0)
        elif prefer == "first-family":
            key = (float(list(families).index(family)), round(per_result, 9))
        else:
            key = (round(per_result, 9), 0.0)
        found.append((key, placement))
    if not found:
        raise CostError("no usable placement: " + "; ".join(errors or ["no candidates"]))
    # A stable sort: of two equal keys, the pair met first stays first.
    return [placement for _, placement in sorted(found, key=lambda item: item[0])]


def parallel_tasks(task_count: int, placement: Placement, max_parallel_vcpus: int) -> int:
    """A job's ``parallelism``: the tasks at once within ``max_parallel_vcpus``, in whole VMs.

    Batch starts VMs of ``vm_vcpus`` that hold up to ``tasks_per_vm`` tasks, so when memory packs
    fewer tasks per VM than it has cores, counting only the tasks' own vCPUs starts more VMs
    (and vCPUs) than the limit allows.
    """
    by_task = max_parallel_vcpus // max(1, placement.cpu_milli // 1000)
    by_vm = max(1, max_parallel_vcpus // max(1, placement.vm_vcpus)) * placement.tasks_per_vm
    return max(1, min(task_count, by_task, by_vm))


@dataclass
class ClassEstimate:
    name: str
    tasks: int
    expected_task_hours: float
    max_task_hours: float
    vm_hours: float
    parallel_tasks: int
    makespan_h: float
    expected_usd: float
    ceiling_usd: float
    vm_hour_usd: float
    disk_hour_usd: float


@dataclass
class Estimate:
    backend: str
    expected_usd: float = 0.0
    ceiling_usd: float = 0.0
    makespan_h: float = 0.0
    core_hours: float = 0.0
    classes: List[ClassEstimate] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        out = asdict(self)
        for key in ("expected_usd", "ceiling_usd"):
            out[key] = round(out[key], 4)
        out["makespan_h"] = round(out["makespan_h"], 3)
        out["core_hours"] = round(out["core_hours"], 3)
        return out


def estimate_gcp(
    table: PriceTable,
    classes: Sequence[Tuple[str, Placement, List[float], List[float], int]],
    *,
    max_retries: int,
    max_parallel_vcpus: int,
    max_campaign_hours: float,
) -> Estimate:
    """``classes``: (name, placement, reference seconds per task, the longest one attempt of each
    task may run, parallel). On Batch an attempt may run its max wall time plus
    ``BATCH_ATTEMPT_GRACE_S`` (maxRunDuration), which is what the caller passes.

    VMs are billed whole: when fewer tasks run at once than ``tasks_per_vm`` (a small submission,
    or a low ``parallel``), each VM holds only ``occupancy`` tasks and pays for the idle slots.
    The quota bound counts whole VMs within ``max_parallel_vcpus`` (as ``parallel_tasks`` does)
    for the campaign's hours plus the reaper's interval, after which it cancels the job.
    """
    est = Estimate(backend="gcp-batch")
    rework = table.rework
    for name, p, reference, max_wall, parallel in classes:
        disk_hour = table.disk_hour(p.boot_disk, table.data["overheads"]["boot_disk_gib"])
        per_vm_hour = p.vm_hour + disk_hour
        task_hours = sum(s / p.speed for s in reference) / 3600.0 * (1 + rework)
        at_once = max(1, min(len(reference), parallel))
        vms = max(1, math.ceil(at_once / p.tasks_per_vm))
        occupancy = max(1, min(p.tasks_per_vm, math.ceil(at_once / vms)))
        start_hours = vms * table.vm_start_s / 3600.0
        vm_hours = task_hours / occupancy + start_hours
        expected = vm_hours * per_vm_hour
        worst_task_hours = sum(max_wall) / 3600.0 * (1 + max_retries)
        worst = (worst_task_hours / occupancy + start_hours) * per_vm_hour
        quota_vms = max(1, max_parallel_vcpus // max(1, p.vm_vcpus))
        quota = quota_vms * (max_campaign_hours + REAPER_INTERVAL_H) * per_vm_hour
        slots = max(1, parallel)
        longest = max(reference) / p.speed / 3600.0 if reference else 0.0
        makespan = max(longest, task_hours / slots) + table.vm_start_s / 3600.0
        est.classes.append(
            ClassEstimate(
                name=name,
                tasks=len(reference),
                expected_task_hours=round(task_hours, 4),
                max_task_hours=round(worst_task_hours, 4),
                vm_hours=round(vm_hours, 4),
                parallel_tasks=parallel,
                makespan_h=round(makespan, 4),
                expected_usd=round(expected, 4),
                ceiling_usd=round(min(worst, quota), 4),
                vm_hour_usd=round(p.vm_hour, 5),
                disk_hour_usd=round(disk_hour, 5),
            )
        )
        est.expected_usd += expected
        est.ceiling_usd += min(worst, quota)
        est.makespan_h = max(est.makespan_h, makespan)
        est.core_hours += task_hours * p.cpu_milli / 1000.0 / max(1, p.threads_per_core)
        est.assumptions.append(
            "%s: %s in %s, %d tasks per VM, %s; prices: %s; speed %.2f (%s)"
            % (
                name,
                p.shape,
                p.region,
                p.tasks_per_vm,
                p.model,
                p.price_source,
                p.speed,
                p.speed_source,
            )
        )
    est.assumptions.append(
        "expected = reference wall time / speed x (1 + %.0f%% preemption rework and start-up), "
        "boot disks included; egress and storage excluded" % (rework * 100)
    )
    return est


def estimate_local(reference: Sequence[float], workers: int, load_factor: float = 1.0) -> Estimate:
    """Wall time of a local pool (no money): reference seconds on ``workers`` slots."""
    est = Estimate(backend="local")
    total = sum(reference) * load_factor / 3600.0
    longest = max(reference) * load_factor / 3600.0 if reference else 0.0
    est.makespan_h = max(longest, total / max(1, workers))
    est.core_hours = total
    est.assumptions.append(
        "reference wall times of the development Mac, %d workers, no allowance for load" % workers
    )
    return est


def estimate_slurm(
    reference: Sequence[float], cpus: Sequence[int], concurrent: int, speed: float = 1.0
) -> Estimate:
    """Core-hours (what an allocation is charged in) and wall time with ``concurrent`` slots."""
    est = Estimate(backend="slurm")
    hours = [s / speed / 3600.0 for s in reference]
    est.core_hours = sum(h * c for h, c in zip(hours, cpus))
    est.makespan_h = max(max(hours, default=0.0), sum(hours) / max(1, concurrent))
    est.assumptions.append(
        "core-hours at speed %.2f of the reference core (the site's `speed` until a calibration "
        "on the site measures it); sites that allocate whole nodes charge node-hours" % speed
    )
    return est


@dataclass
class CapCheck:
    refusals: List[str]
    confirm: bool
    limit_usd: float


def check_caps(
    estimate: Estimate,
    limits,
    *,
    tasks: int,
    max_wall_s: int,
    max_usd: Optional[float] = None,
) -> CapCheck:
    """Refusals (any one stops a submit) and whether to ask for confirmation."""
    refusals = []
    if tasks > limits.max_tasks:
        refusals.append("%d tasks, above limits.max_tasks %d" % (tasks, limits.max_tasks))
    if max_wall_s > limits.max_task_wall_s:
        refusals.append(
            "a task may run %d s, above limits.max_task_wall_s %d"
            % (max_wall_s, limits.max_task_wall_s)
        )
    limit = limits.refuse_usd
    if max_usd is not None:
        if max_usd > limits.hard_refuse_usd:
            refusals.append(
                "--max-usd %.2f is above limits.hard_refuse_usd %.2f (only the config file can "
                "raise it)" % (max_usd, limits.hard_refuse_usd)
            )
        limit = min(max_usd, limits.hard_refuse_usd)
    if estimate.ceiling_usd > limit:
        refusals.append(
            "the ceiling $%.2f is above $%.2f (limits.refuse_usd, or --max-usd)"
            % (estimate.ceiling_usd, limit)
        )
    return CapCheck(
        refusals=refusals, confirm=estimate.expected_usd > limits.confirm_usd, limit_usd=limit
    )
