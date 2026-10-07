"""What Batch actually billed: VM time from Compute Engine audit logs, and the budget guard's figure.

Batch bills VM time (docs/cloud-experiments.md, "Cost model"). Every Batch VM is created and
deleted by the job's managed instance group, and the Compute Engine audit log records both: a VM
is billed from its ``instances.insert`` completing to its ``instances.delete`` completing (or to
its Spot preemption). Its name begins with the job's uid, which joins it to a submission record.

- ``campaign_cost``: per submission of a campaign, the VMs, VM-hours and their price (the price
  table's rate for the record's shape and region, plus the boot disk), the Batch task time, and
  the slot utilisation (task time / VM time x tasks per VM).
- ``guard_readings``: the budget guard's log lines (its ratio of the month's cost to the budget,
  and, from guards that log them, the cost and budget amounts).
- ``reconcile``: billed spend lags VM time by hours, and is somewhat above it (networking,
  storage, registry, logging); the fit finds the lag and the factor over the month's readings, so
  the VM time since the last billed reading gives the not-yet-billed estimate.

Read-only: the submit account reads logs and Batch jobs; nothing here changes a cloud.
"""

from __future__ import annotations

import datetime as _dt
import json
import zoneinfo as _zoneinfo
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from yapnr.exp import cost

COST_SCHEMA = "yapnr-campaign-cost-v1"
INSTANCE_PREFIX = "yapnr-"
BILLING_TZ = "America/Los_Angeles"  # Cloud Billing's month boundary, not UTC's
# A Batch VM's name: <job uid>-group<n>-<m>-<suffix>.
GROUP_SEP = "-group"
MAX_LAG_H = 48.0


def parse_time(text: str) -> _dt.datetime:
    """An RFC 3339 timestamp (nanoseconds allowed) as an aware UTC datetime."""
    text = text.strip().replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        # Only the fractional-second digits, stopping at the zone's sign: "".join(isdigit())
        # over the whole tail also ate the "00:00" of a +00:00 zone, truncating or losing the
        # zone and raising ValueError on every real (fractional-second) timestamp.
        cut = 0
        while cut < len(rest) and rest[cut].isdigit():
            cut += 1
        digits, zone = rest[:cut], rest[cut:] or "+00:00"
        text = "%s.%s%s" % (head, (digits + "000000")[:6], zone)
    value = _dt.datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)


def audit_filter(uids: Optional[Sequence[str]] = None) -> str:
    """The audit-log filter for the VMs of ``uids`` (Batch job uids), or of every yapnr VM."""
    names = " OR ".join('"instances/%s%s"' % (uid, GROUP_SEP) for uid in uids or ())
    name = "(%s)" % names if uids else '"instances/%s"' % INSTANCE_PREFIX
    return (
        'resource.type="gce_instance" AND logName:"cloudaudit.googleapis.com" AND '
        "protoPayload.resourceName:%s AND ((protoPayload.methodName:"
        '("instances.insert" OR "instances.delete") AND operation.last=true AND '
        'severity=NOTICE) OR protoPayload.methodName="compute.instances.preempted")' % name
    )


@dataclass
class Vm:
    name: str
    job_uid: str
    zone: str
    start: _dt.datetime
    end: Optional[_dt.datetime]  # None: still running
    preempted: bool = False

    @property
    def region(self) -> str:
        return self.zone.rsplit("-", 1)[0] if self.zone else ""

    def hours(self, until: Optional[_dt.datetime] = None, since=None) -> float:
        end = self.end or until
        if end is None:
            return 0.0
        if until is not None:
            end = min(end, until)
        start = max(self.start, since) if since else self.start
        return max(0.0, (end - start).total_seconds() / 3600.0)


def vms(entries: Iterable[Mapping[str, Any]]) -> List[Vm]:
    """VM lifetimes from audit entries: a completed insert opens one, a completed delete or a
    preemption closes it (a managed instance group reuses names, so a name may have several)."""
    events = []
    for entry in entries:
        payload = entry.get("protoPayload") or {}
        name = str(payload.get("resourceName", "")).rsplit("/", 1)[-1]
        method = str(payload.get("methodName", ""))
        if not name.startswith(INSTANCE_PREFIX) or "timestamp" not in entry:
            continue
        if "preempted" in method:
            kind = "end"
            preempted = True
        elif entry.get("severity") != "NOTICE" or not (entry.get("operation") or {}).get("last"):
            continue
        elif method.endswith("instances.insert"):
            kind, preempted = "start", False
        elif method.endswith("instances.delete"):
            kind, preempted = "end", False
        else:
            continue
        zone = ((entry.get("resource") or {}).get("labels") or {}).get("zone", "")
        events.append((parse_time(entry["timestamp"]), name, kind, preempted, zone))
    events.sort(key=lambda e: (e[0], e[1]))
    open_vms: Dict[str, Vm] = {}
    out: List[Vm] = []
    for when, name, kind, preempted, zone in events:
        if kind == "start":
            if name in open_vms:  # a second insert without a delete: close the first there
                open_vms[name].end = when
            vm = Vm(name, name.split(GROUP_SEP, 1)[0], zone, when, None)
            open_vms[name] = vm
            out.append(vm)
        elif name in open_vms:
            vm = open_vms.pop(name)
            vm.end = when
            vm.preempted = preempted
    return out


def read_vms(cloud, uids: Optional[Sequence[str]] = None, freshness: str = "35d") -> List[Vm]:
    entries = cloud.json(
        ["logging", "read", audit_filter(uids), "--freshness=%s" % freshness, "--limit=100000"]
    )
    return vms(entries if isinstance(entries, list) else [])


def vm_price(table: cost.PriceTable, shape: str, region: str, model: str = "spot") -> float:
    """$ per hour of one VM of ``shape`` in ``region`` with its boot disk."""
    family, kind, vcpus = shape.split("-")
    fam = table.family(family)
    vcpu_price, gb_price, _ = table.rate(family, region, model)
    memory = float(fam["memory_gb_per_vcpu"][kind]) * int(vcpus)
    disk = table.disk_hour(fam["boot_disk"], table.data["overheads"]["boot_disk_gib"])
    return int(vcpus) * vcpu_price + memory * gb_price + disk


def task_hours(tasks: Any) -> Tuple[float, int]:
    """(Batch task hours, tasks with a run) from ``batch tasks list``: RUNNING to its end."""
    total, count = 0.0, 0
    for task in tasks if isinstance(tasks, list) else []:
        start = None
        for event in (task.get("status") or {}).get("statusEvents") or []:
            state, kind = event.get("taskState"), event.get("type")
            if state == "RUNNING" and kind == "RUNNING":
                start = parse_time(event["eventTime"])
            elif start and state in ("SUCCEEDED", "FAILED", "PENDING") and kind != "RUNNABLE_EVENT":
                total += (parse_time(event["eventTime"]) - start).total_seconds() / 3600.0
                count += 1
                start = None
    return total, count


def campaign_cost(
    cloud,
    plan_meta: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    table: cost.PriceTable,
    now: Optional[_dt.datetime] = None,
) -> Dict[str, Any]:
    """The VM-time cost of a campaign's Batch submissions (``yapnr-campaign-cost-v1``)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    real = [r for r in records if not r.get("dry_run") and (r.get("job") or {}).get("uid")]
    found = read_vms(cloud, [r["job"]["uid"] for r in real]) if real else []
    rows = []
    for record in real:
        job = record["job"]
        placement = record.get("placement") or {}
        mine = [vm for vm in found if vm.job_uid == job["uid"]]
        price = vm_price(
            table,
            placement["shape"],
            job.get("region") or placement["region"],
            placement.get("model", "spot"),
        )
        vm_hours = sum(vm.hours(now) for vm in mine)
        tasks = cloud.json(
            ["batch", "tasks", "list", "--job=%s" % job["id"], "--location=%s" % job["region"]],
            check=False,
        )
        busy, ran = task_hours(tasks)
        per_vm = int(placement.get("tasks_per_vm") or 1)
        starts = [vm.start for vm in mine]
        ends = [vm.end or now for vm in mine]
        rows.append(
            {
                "submission": record.get("submission"),
                "job": job["id"],
                "shape": placement.get("shape"),
                "region": job.get("region"),
                "tasks": record.get("tasks"),
                "batch_tasks": (record.get("packing") or {}).get(
                    "batch_tasks", record.get("tasks")
                ),
                "vms": len(mine),
                "running": sum(1 for vm in mine if vm.end is None),
                "preempted": sum(1 for vm in mine if vm.preempted),
                "vm_hours": round(vm_hours, 4),
                "task_hours": round(busy, 4),
                "tasks_run": ran,
                "slot_utilisation": round(busy / (vm_hours * per_vm), 3) if vm_hours else None,
                "vm_hour_usd": round(price, 5),
                "usd": round(vm_hours * price, 4),
                "first_vm": min(starts).isoformat() if starts else None,
                "last_vm_end": max(ends).isoformat() if ends else None,
                "makespan_h": (
                    round((max(ends) - min(starts)).total_seconds() / 3600.0, 4) if starts else None
                ),
                "predicted_vm_hours": (record.get("packing") or {}).get("predicted_vm_hours"),
            }
        )
    starts = [parse_time(r["first_vm"]) for r in rows if r["first_vm"]]
    ends = [parse_time(r["last_vm_end"]) for r in rows if r["last_vm_end"]]
    estimate = plan_meta.get("estimate") or {}
    return {
        "schema": COST_SCHEMA,
        "campaign": plan_meta.get("id"),
        "measured": now.isoformat(),
        "prices": table.accessed,
        "submissions": rows,
        "vms": sum(r["vms"] for r in rows),
        "running": sum(r["running"] for r in rows),
        "vm_hours": round(sum(r["vm_hours"] for r in rows), 4),
        "task_hours": round(sum(r["task_hours"] for r in rows), 4),
        "usd": round(sum(r["usd"] for r in rows), 4),
        "makespan_h": (
            round((max(ends) - min(starts)).total_seconds() / 3600.0, 4) if starts else None
        ),
        "estimate_usd": estimate.get("expected_usd"),
        "note": "VM time from the Compute Engine audit log at Spot list prices plus boot disks; "
        "the bill adds networking, storage and logging (about 15%) and lags by hours",
    }


@dataclass
class Reading:
    time: _dt.datetime
    ratio: float
    cost_usd: Optional[float] = None
    budget_usd: Optional[float] = None

    def billed(self, budget: Optional[float]) -> Optional[float]:
        if self.cost_usd is not None:
            return self.cost_usd
        amount = self.budget_usd or budget
        return self.ratio * amount if amount else None


GUARD_FILTER = (
    'resource.type=("cloud_run_revision" OR "cloud_function") AND jsonPayload.guard="budget" '
    "AND jsonPayload.ratio:*"
)


def readings(entries: Iterable[Mapping[str, Any]]) -> List[Reading]:
    out = []
    for entry in entries:
        payload = entry.get("jsonPayload") or {}
        if "ratio" not in payload or "timestamp" not in entry:
            continue
        out.append(
            Reading(
                parse_time(entry["timestamp"]),
                float(payload["ratio"]),
                float(payload["cost"]) if payload.get("cost") is not None else None,
                float(payload["budget"]) if payload.get("budget") is not None else None,
            )
        )
    return sorted(out, key=lambda r: r.time)


def guard_readings(cloud, freshness: str = "35d") -> List[Reading]:
    entries = cloud.json(
        ["logging", "read", GUARD_FILTER, "--freshness=%s" % freshness, "--limit=5000"]
    )
    return readings(entries if isinstance(entries, list) else [])


def _billing_tz():
    try:
        return _zoneinfo.ZoneInfo(BILLING_TZ)
    except _zoneinfo.ZoneInfoNotFoundError:
        # No tzdata installed: Pacific's fixed-offset approximation (wrong across a DST
        # transition, right on every other day) beats UTC's being off by a steady 7-8 h.
        return _dt.timezone(_dt.timedelta(hours=-8))


def month_start(when: _dt.datetime) -> _dt.datetime:
    """UTC midnight is 7-8 h off Cloud Billing's own month boundary (Pacific time): a guard
    reading taken in that gap is still of the previous month, and read as this month's it can
    look, wrongly, like a drop in the ratio -- which ``budget_history`` reads as a raised
    budget."""
    local = when.astimezone(_billing_tz()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(_dt.timezone.utc)


def reconcile(
    points: Sequence[Tuple[_dt.datetime, float]],
    vm_usd: Callable[[_dt.datetime], float],
    now: _dt.datetime,
    max_lag_h: float = MAX_LAG_H,
    step_h: float = 0.5,
) -> Dict[str, Any]:
    """Fit billed(t) = factor x VM-time(t - lag) over the readings; estimate the unbilled part.

    ``points``: (time, billed $ month to date); ``vm_usd(t)``: VM-time $ from the month's start
    to ``t``. With fewer than three readings that saw any spend, the lag and factor fall back to
    8 h and 1.15 (what October 2026's readings gave).
    """
    usable = [(t, b) for t, b in points if b is not None]
    last_t, last_b = usable[-1] if usable else (now, 0.0)
    lag_h, factor, rms, fitted = 8.0, 1.15, None, False
    if sum(1 for _, b in usable if b > 0) >= 3:
        best = None
        steps = int(max_lag_h / step_h)
        for index in range(steps + 1):
            lag = index * step_h
            v = [vm_usd(t - _dt.timedelta(hours=lag)) for t, _ in usable]
            vv = sum(x * x for x in v)
            if vv <= 0:
                continue
            k = sum(x * b for x, (_, b) in zip(v, usable)) / vv
            err = sum((b - k * x) ** 2 for x, (_, b) in zip(v, usable))
            if best is None or err < best[0] - 1e-12:
                best = (err, lag, k)
        if best is not None:
            lag_h, factor = best[1], best[2]
            rms = (best[0] / len(usable)) ** 0.5
            fitted = True
    billed_cut = last_t - _dt.timedelta(hours=lag_h)
    vm_billed = vm_usd(billed_cut)
    vm_now = vm_usd(now)
    unbilled = max(0.0, factor * (vm_now - vm_billed))
    return {
        "billed_usd": round(last_b, 2),
        "billed_at": last_t.isoformat(),
        "lag_h": lag_h,
        "factor": round(factor, 3),
        "fitted": fitted,
        "rms_usd": round(rms, 3) if rms is not None else None,
        "vm_usd_to_billed_cut": round(vm_billed, 2),
        "vm_usd_now": round(vm_now, 2),
        "unbilled_usd": round(unbilled, 2),
        "projected_usd": round(last_b + unbilled, 2),
    }


def month_spend(
    cloud,
    table: cost.PriceTable,
    shapes: Mapping[str, Tuple[str, str]],
    budget: Optional[float],
    now: Optional[_dt.datetime] = None,
    entries: Optional[Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Month-to-date billed spend, the VM time behind it and the unbilled estimate.

    ``shapes``: Batch job uid -> (machine type, region), from the jobs' descriptions; a VM of an
    unknown job is priced at the dearest known rate. ``entries`` (audit, guard) replace the two
    log reads (tests).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    start = month_start(now)
    if entries is None:
        found = read_vms(cloud)
        guard = guard_readings(cloud)
    else:
        found, guard = vms(entries[0]), readings(entries[1])
    guard = [r for r in guard if r.time >= start]
    rates: Dict[str, float] = {}
    for uid, (shape, region) in shapes.items():
        try:
            rates[uid] = vm_price(table, shape, region)
        except (cost.CostError, KeyError, ValueError):
            continue
    fallback = max(rates.values()) if rates else 0.0
    unknown = sorted({vm.job_uid for vm in found if vm.job_uid not in rates})

    def vm_usd(until: _dt.datetime) -> float:
        return sum(
            vm.hours(until, since=start) * rates.get(vm.job_uid, fallback)
            for vm in found
            if vm.start < until and (vm.end is None or vm.end > start)
        )

    budgets, changes = budget_history(guard, budget)
    points = monotone([(r.time, r.billed(amount)) for r, amount in zip(guard, budgets)])
    out = reconcile(points, vm_usd, now)
    out.update(
        month=start.strftime("%Y-%m"),
        budget_usd=budget,
        readings=len(guard),
        readings_used=len(points),
        ratio=guard[-1].ratio if guard else None,
        vms=sum(1 for vm in found if vm.end is None or vm.end > start),
        running_vms=sum(1 for vm in found if vm.end is None),
        vm_hours=round(sum(vm.hours(now, since=start) for vm in found), 2),
        unknown_jobs=unknown,
        budget_changes=changes,
    )
    if guard and all(r.cost_usd is None and r.budget_usd is None for r in guard) and not budget:
        out["warning"] = "no budget amount: set [prices] budget_usd or pass --budget-usd"
    return out


def monotone(points: Sequence[Tuple[_dt.datetime, Optional[float]]]):
    """The readings a month-to-date cost can have produced: none above a later one (a
    kill-switch drill's test message, a late message of another period)."""
    out: List[Tuple[_dt.datetime, Optional[float]]] = []
    floor = float("inf")
    for when, billed in reversed(list(points)):
        if billed is None:
            continue
        if billed <= floor + 0.01:
            out.append((when, billed))
            floor = min(floor, billed)
    return list(reversed(out))


def budget_history(
    guard: Sequence[Reading], budget: Optional[float]
) -> Tuple[List[Optional[float]], List[Dict[str, Any]]]:
    """The budget each reading's ratio is of, with ``budget`` the current one.

    A month's cost never falls, so a ratio that drops between two readings (that log no amounts)
    means the budget was raised: the earlier readings are of a budget smaller by the drop's
    factor (the cost is taken as unchanged across the change).
    """
    out: List[Optional[float]] = [budget] * len(guard)
    changes: List[Dict[str, Any]] = []
    if not budget:
        return out, changes
    current = budget
    for index in range(len(guard) - 1, -1, -1):
        out[index] = current
        if index == 0:
            break
        before, after = guard[index - 1], guard[index]
        if (
            before.cost_usd is None
            and after.cost_usd is None
            and after.ratio > 0
            and before.ratio > after.ratio * 1.02
        ):
            current = current * after.ratio / before.ratio
            changes.append(
                {"at": after.time.isoformat(), "from_usd": round(current, 2), "to_usd": out[index]}
            )
    return out, list(reversed(changes))


def job_shapes(cloud, regions: Sequence[str]) -> Dict[str, Tuple[str, str]]:
    """Batch job uid -> (machine type, region) from ``batch jobs list`` in each region."""
    out: Dict[str, Tuple[str, str]] = {}
    for region in regions:
        jobs = cloud.json(["batch", "jobs", "list", "--location=%s" % region], check=False)
        for job in jobs if isinstance(jobs, list) else []:
            shape = _job_shape(job)
            if job.get("uid") and shape:
                out[job["uid"]] = (shape, region)
    return out


def _job_shape(job: Mapping[str, Any]) -> Optional[str]:
    for group in ((job.get("status") or {}).get("taskGroups") or {}).values():
        for instance in group.get("instances") or []:
            if instance.get("machineType"):
                return instance["machineType"]
    for instance in (job.get("allocationPolicy") or {}).get("instances") or []:
        policy = instance.get("policy") or {}
        if policy.get("machineType"):
            return policy["machineType"]
        template = instance.get("instanceTemplate") or ""
        # yapnr's templates are named yapnr-<shape>-<model>-<region> (gcp.template).
        parts = template.split("-")
        if template.startswith("yapnr-") and len(parts) >= 4:
            return "-".join(parts[1:4])
    return None


def ledger_rows(ledger: Iterable[Mapping[str, Any]], since: _dt.datetime) -> List[Dict[str, Any]]:
    """Campaign cost records (``yapnr exp cost``) measured since ``since``, newest first."""
    rows = []
    for item in ledger:
        try:
            when = parse_time(item.get("measured", ""))
        except ValueError:
            continue
        if when >= since:
            rows.append(dict(item))
    return sorted(rows, key=lambda r: r.get("measured", ""), reverse=True)


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"
