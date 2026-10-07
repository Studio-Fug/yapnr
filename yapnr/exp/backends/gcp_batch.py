"""Google Cloud Batch: one job per submission, Spot VMs, the stores as two buckets.

``render_job`` is a pure function of the plan, the owner config and the submission; its output is
checked against the vendored Batch v1 discovery document before anything is sent. The job:

- runs the published image from the region's Artifact Registry remote repository (a pull-through
  cache of ghcr.io), pinned by digest, with the image's ``yapnr-kicad-env`` as entrypoint; an
  image named by its full reference elsewhere (the project's ``images`` repository) runs as
  named, with the campaign's ``runtime`` (interpreter, entrypoint) when it is not a yapnr image;
- runs ``task.py`` (the campaign's copy, in the runs bucket) once per task: ``BATCH_TASK_INDEX``
  maps through ``submissions/<n>.indices`` to a line of ``tasks.jsonl``;
- mounts the runs bucket read-write and the inputs bucket's ``bundles/`` read-only (gcsfuse);
- retries only infrastructure failures (Spot preemption 50001, VM lost 50002, VM rebooted 50003,
  VM recreated 50006) and the wrapper's transient 75, at most ``limits.max_retries`` times;
- packs ``tasks_per_vm`` tasks per VM (one per physical core by default), from an instance template
  for Hyperdisk-only families (C4D, C4, C4A, N4) or an instance policy otherwise;
- carries the labels the budget guard, the reaper and billing reports use (``yapnr``, ``campaign``,
  ``kind``, ``visibility``, ``submission``, ``deadline``).

Where a class runs is chosen at submit (``choose``): a class planned with several candidates (the
owner's ranked (family, region) pairs) goes to the first whose region has Spot quota for one more
of its VMs (``PREEMPTIBLE_CPUS`` limit less usage, from ``compute regions describe``, less what
this submit already sent there), skipping pairs without their instance template; when no region has
room it waits in the first. The submission record says what was chosen and why.

Every ``gcloud`` call goes through ``yapnr.exp.cloud.Gcloud`` with the impersonated submit account.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from yapnr.exp import batch_schema, cost, image, packing
from yapnr.exp import plan as planning
from yapnr.exp.backends.base import Backend, Stores, SubmitError
from yapnr.exp.cloud import CloudError, FakeCloud, Gcloud
from yapnr.exp.config import Config, Gcp
from yapnr.exp.store import GcsStore

RUNS_MOUNT = "/mnt/disks/runs"
INPUTS_MOUNT = "/mnt/disks/inputs"
IMAGE_PYTHON = image.YAPNR_PYTHON
IMAGE_ENTRYPOINT = image.YAPNR_ENTRYPOINT
WORK_ROOT = "/tmp/yapnr-work"
# Reserved Batch exit codes worth a retry (docs.cloud.google.com/batch/docs/troubleshooting):
# 50001 Spot preemption, 50002 VM stopped reporting, 50003 VM rebooted, 50006 VM recreated; and
# EX_TEMPFAIL, the wrapper's staging or upload failure. 50004 (cancel failed) and 50005 (the task
# outgrew maxRunDuration or its timeout) are not retried: a retry repeats them.
RETRY_EXIT_CODES = [50001, 50002, 50003, 50006, 75]
# The runnable's timeout leaves the wrapper time to upload after its own limit, and
# maxRunDuration more again, so the three limits are told apart in the logs.
UPLOAD_GRACE_S = 300
RUN_GRACE_S = 300
LABEL_VALUE_RE = re.compile(r"[^a-z0-9_-]")
# Job states in which tasks may still start or run (Batch v1 ``JobStatus.state``).
LIVE_STATES = ("STATE_UNSPECIFIED", "QUEUED", "SCHEDULED", "RUNNING")
JOB_ID_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


def label(value: Any) -> str:
    """A label value: lower case, [a-z0-9_-], at most 63 characters."""
    return LABEL_VALUE_RE.sub("-", str(value).lower())[:63]


def job_id(cid: str, submission: int) -> str:
    name = "yapnr-%s-s%d" % (cid, submission)
    if not JOB_ID_RE.match(name):
        raise SubmitError("%s is not a valid Batch job id" % name)
    return name


def template_name(gcp: Gcp, placement: cost.Placement) -> str:
    return gcp.template.format(
        shape=placement.shape, model=placement.model, region=placement.region
    )


SPOT_QUOTA = "PREEMPTIBLE_CPUS"


@dataclass
class RegionQuota:
    """A region's Spot (preemptible) CPU quota, or why it could not be read."""

    region: str
    limit: Optional[float] = None
    usage: Optional[float] = None
    error: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.limit is not None and self.usage is not None


def region_quota(cloud: Gcloud, region: str) -> RegionQuota:
    """The region's ``PREEMPTIBLE_CPUS`` limit and live usage (``compute regions describe``)."""
    try:
        data = cloud.json(["compute", "regions", "describe", region])
    except CloudError as err:
        return RegionQuota(region, error=str(err).splitlines()[0][:200])
    for quota in (data or {}).get("quotas", []) if isinstance(data, dict) else []:
        if quota.get("metric") == SPOT_QUOTA:
            try:
                return RegionQuota(region, float(quota["limit"]), float(quota.get("usage", 0)))
            except (KeyError, TypeError, ValueError):
                break
    return RegionQuota(region, error="no %s quota in the answer" % SPOT_QUOTA)


def template_exists(cloud: Gcloud, name: str) -> Optional[bool]:
    """Whether the global instance template exists; None when that could not be read."""
    try:
        cloud.json(["compute", "instance-templates", "describe", name, "--global"])
    except CloudError as err:
        return False if err.not_found else None
    return True


def vms_at_once(placement: cost.Placement, tasks: int, max_parallel_vcpus: int) -> int:
    """The VMs a job of ``tasks`` tasks runs at once (``parallelism`` in whole VMs)."""
    parallel = cost.parallel_tasks(tasks, placement, max_parallel_vcpus)
    return max(1, math.ceil(parallel / max(1, placement.tasks_per_vm)))


def bundles_allowed(plan) -> bool:
    """Whether the plan's wrapper runs several plan lines per Batch task."""
    return planning.wrapper_version(plan.meta) >= planning.BUNDLE_WRAPPER_VERSION


def smaller_placement(
    table: cost.PriceTable,
    cls: planning.ResourceClass,
    placement: cost.Placement,
    disk_free_gb: Optional[float] = None,
) -> Optional[cost.Placement]:
    """The next smaller shape of the placement's family that holds a task, for a straggler job."""
    try:
        sizes = sorted(v for v in table.family(placement.family)["vcpus"] if v < placement.vm_vcpus)
    except (cost.CostError, KeyError):
        return None
    task_vcpus = max(1, placement.cpu_milli // 1000)
    mode = "core" if task_vcpus == cls.cpus * placement.threads_per_core else "vcpu"
    for size in reversed(sizes):
        if size < task_vcpus:
            break
        try:
            shape, vcpus, memory, milli, per_vm = cost.choose_shape(
                table,
                placement.family,
                cls.cpus,
                cls.memory_gb,
                size,
                mode,
                cls.disk_gb,
                disk_free_gb,
            )
        except cost.CostError:
            continue
        if vcpus >= placement.vm_vcpus or milli != placement.cpu_milli:
            continue
        small = cost.Placement.from_json(placement.to_json())
        small.shape, small.vm_vcpus, small.vm_memory_gb, small.tasks_per_vm = (
            shape,
            vcpus,
            memory,
            per_vm,
        )
        return small
    return None


def layout(
    plan,
    config: Config,
    cls: planning.ResourceClass,
    lines: Sequence[int],
    table: cost.PriceTable,
    cloud: Optional[Gcloud] = None,
) -> List[packing.Job]:
    """The Batch jobs of one class's pending ``lines`` (``packing.pack``).

    A straggler job may use the next smaller shape of the family, when ``cloud`` shows its
    instance template exists (or the placement needs none); without ``cloud`` it does not.
    """
    gcp = config.require_gcp()
    placement = plan.placement(cls.name)
    small = None
    if cloud is not None:
        small = smaller_placement(
            table, cls, placement, gcp.boot_disk_gb - cost.BOOT_DISK_RESERVE_GB
        )
    if (
        small is not None
        and small.template
        and template_exists(cloud, template_name(gcp, small)) is not True
    ):
        small = None
    ratio = None
    if small is not None:
        disk = table.disk_hour(placement.boot_disk, table.data["overheads"]["boot_disk_gib"])
        ratio = (small.vm_hour + disk) / (placement.vm_hour + disk)
    packed = planning.class_packing(
        cls,
        lines,
        placement,
        config,
        table,
        bundle=bundles_allowed(plan),
        small=(small.tasks_per_vm, ratio) if small is not None else None,
    )
    for job in packed.jobs:
        if job.straggler and small is not None:
            job.placement = small
    return packed.jobs


def choose(
    plan,
    config: Config,
    todo: Dict[str, List[int]],
    cloud: Gcloud,
    *,
    region: Optional[str] = None,
    say: Callable[[str], None] = print,
    previous: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """The placement of every pending class among its candidates, set with ``plan.choose``.

    A class goes to the first candidate whose region has Spot quota for one more of its VMs: the
    ``PREEMPTIBLE_CPUS`` limit less its live usage, less the vCPUs this submit already sent to the
    region (classes are chosen one after the other, before any of their VMs exist). A template
    placement whose instance template does not exist is skipped. When no candidate has room, the
    class waits in the first usable one (Batch queues the job until quota frees up). ``region``
    keeps only the candidates in that region (``submit --region``). A class with one candidate
    makes no cloud call. A wall-clock-budgeted campaign runs on one machine type: the shape of its
    first submission (``previous``), else the shape its first class chose now, for every class.
    Returns, per class, the choice, why, and what was looked at.
    """
    gcp = config.require_gcp()
    quotas: Dict[str, RegionQuota] = {}
    templates: Dict[str, Optional[bool]] = {}
    claimed: Dict[str, int] = {}
    out: Dict[str, Dict[str, Any]] = {}
    one_type = plan.meta.get("determinism") == "wall_clock_budgeted"
    shape: Optional[str] = None
    kept = ""
    if (
        one_type
        and previous is not None
        and any(len(plan.candidates(c.name)) > 1 for c in plan.classes if c.name in todo)
    ):
        for record in previous():
            if not record.get("dry_run") and (record.get("placement") or {}).get("shape"):
                shape = record["placement"]["shape"]
                kept = " of %s, the campaign's machine type since its first submission" % shape
                break
    for cls in plan.classes:
        lines = todo.get(cls.name)
        if not lines:
            continue
        options = plan.candidates(cls.name)
        if shape:
            options = [p for p in options if p.shape == shape]
            if not options:
                raise SubmitError(
                    "class %s: a wall-clock-budgeted campaign runs on one machine type, %s since "
                    "its first submission, and the plan has no candidate of it" % (cls.name, shape)
                )
        if region:
            options = [p for p in options if p.region == region]
            if not options:
                raise SubmitError(
                    "class %s has no candidate in %s (candidates: %s); plan again with --region %s"
                    % (
                        cls.name,
                        region,
                        ", ".join(p.pair for p in plan.candidates(cls.name)),
                        region,
                    )
                )
        looked: List[Dict[str, Any]] = []
        chosen: Optional[cost.Placement] = None
        why = ""
        usable: List[cost.Placement] = []
        if len(options) == 1:
            chosen = options[0]
            why = "pinned to %s" % region if region else "the only candidate" + kept
        else:
            for p in options:
                seen: Dict[str, Any] = {"pair": p.pair, "shape": p.shape}
                looked.append(seen)
                if p.template:
                    name = template_name(gcp, p)
                    if name not in templates:
                        templates[name] = template_exists(cloud, name)
                    if templates[name] is False:
                        seen["skipped"] = "no instance template %s" % name
                        continue
                usable.append(p)
                if p.region not in quotas:
                    quotas[p.region] = region_quota(cloud, p.region)
                quota = quotas[p.region]
                if not quota.known:
                    seen["quota"] = "unknown: %s" % quota.error
                    continue
                free = quota.limit - quota.usage - claimed.get(p.region, 0)
                seen.update(
                    limit=quota.limit,
                    usage=quota.usage,
                    claimed=claimed.get(p.region, 0),
                    free=free,
                    vm_vcpus=p.vm_vcpus,
                )
                if free >= p.vm_vcpus:
                    chosen = p
                    why = "%s has room: %g of %g Spot vCPUs free, one VM takes %d" % (
                        p.region,
                        free,
                        quota.limit,
                        p.vm_vcpus,
                    )
                    break
                seen["full"] = True
            if chosen is None:
                if not usable:
                    raise SubmitError(
                        "class %s: none of its candidates has an instance template (%s); run "
                        "`yapnr exp doctor --backend gcp-batch` and check the templates in the "
                        "tfvars (region_template_shapes)"
                        % (cls.name, "; ".join(x.get("skipped", "") for x in looked))
                    )
                chosen = usable[0]
                unread = [x["pair"] for x in looked if "quota" in x]
                if len(unread) == len(usable):
                    # Nothing was read (a dry run reads nothing): no claim that the regions are full.
                    why = (
                        "the Spot quota could not be read in any candidate region; placed on "
                        "%s, the first usable candidate" % chosen.pair
                    )
                else:
                    note = " (unreadable: %s)" % ", ".join(unread) if unread else ""
                    why = (
                        "no candidate region has Spot quota for one more VM%s; waits in %s, the "
                        "first usable candidate" % (note, chosen.region)
                    )
        if one_type:
            shape = chosen.shape
        rank = [p.pair for p in plan.candidates(cls.name)].index(chosen.pair) + 1
        plan.choose(cls.name, chosen)
        packed = planning.class_packing(
            cls,
            lines,
            chosen,
            config,
            cost.PriceTable.load(config.price_table),
            bundle=bundles_allowed(plan),
        )
        vcpus = sum(job.vms for job in packed.jobs) * chosen.vm_vcpus
        claimed[chosen.region] = claimed.get(chosen.region, 0) + vcpus
        out[cls.name] = {
            "pair": chosen.pair,
            "region": chosen.region,
            "shape": chosen.shape,
            "rank": rank,
            "why": why,
            "looked_at": looked,
            "region_pin": region,
        }
        say(
            "class %s: %s in %s (candidate %d): %s"
            % (cls.name, chosen.shape, chosen.region, rank, why)
        )
    return out


def render_job(
    plan_meta: Dict[str, Any],
    config: Config,
    cls: planning.ResourceClass,
    placement: cost.Placement,
    submission: int,
    task_count: int,
    deadline: int,
    parallelism: Optional[int] = None,
    cells_per_task: int = 1,
    claims: bool = False,
) -> Dict[str, Any]:
    """The Batch job (REST ``Job``) for one submission of one resource class.

    ``parallelism`` (the packing's VM count in tasks) replaces the quota-bound default;
    ``cells_per_task`` (the largest bundle) scales the runnable's timeout, since the wrapper runs a
    bundle's cells one after the other, each within its own limit. ``claims``: each task claims
    the next line of the indices file in order (the wrapper's ``Claims``), since Batch does not
    start task indices in order.
    """
    gcp = config.require_gcp()
    limits = config.limits
    cid = plan_meta["id"]
    ref = image.parse(cls.image)
    if not ref.digest:
        raise SubmitError("the image %s is not pinned to a digest" % cls.image)
    registry = gcp.registry.format(region=placement.region, project=gcp.project)
    image_uri = image.mirror(ref, registry) if ref.registry == "ghcr.io" else ref.pinned
    if parallelism is None:
        parallelism = cost.parallel_tasks(task_count, placement, limits.max_parallel_vcpus)
    parallelism = max(1, min(int(parallelism), task_count))
    wall = int(cls.max_wall_s) * max(1, int(cells_per_task))
    options = "--init --shm-size 1g"
    if gcp.container_user:
        options += " --user %s" % gcp.container_user
    python, entrypoint = image.runtime(plan_meta.get("image") or {})
    commands = [
        python,
        "%s/campaigns/%s/task.py" % (RUNS_MOUNT, cid),
        "--store",
        RUNS_MOUNT,
        "--inputs",
        INPUTS_MOUNT,
        "--campaign",
        cid,
        "--submission",
        str(submission),
        "--toolchain",
        "image",
        "--work-root",
        WORK_ROOT,
    ]
    labels = {
        "yapnr": "1",
        "campaign": label(cid),
        "kind": label(plan_meta["kind"]),
        "visibility": label(plan_meta["visibility"]),
        "submission": label(submission),
        "class": label(cls.name),
        "deadline": label(deadline),
    }
    if placement.template:
        instance = {"instanceTemplate": template_name(gcp, placement)}
        network = None
    else:
        policy: Dict[str, Any] = {
            "machineType": placement.shape,
            "provisioningModel": "SPOT" if placement.model == "spot" else "STANDARD",
            # Hyperdisk-only families (C4D, C4, C4A, N4) normally run from a template; without
            # one, the boot disk type is the family's (the smoke job tests whether Batch takes it).
            "bootDisk": {"type": placement.boot_disk, "sizeGb": str(gcp.boot_disk_gb)},
        }
        instance = {"policy": policy, "blockProjectSshKeys": True}
        network = {
            "networkInterfaces": [
                {
                    "network": "projects/%s/global/networks/yapnr" % gcp.project,
                    "subnetwork": gcp.subnetwork.format(
                        project=gcp.project, region=placement.region
                    ),
                    "noExternalIpAddress": True,
                }
            ]
        }
    allocation: Dict[str, Any] = {
        "location": {"allowedLocations": ["regions/%s" % placement.region]},
        "instances": [instance],
        "serviceAccount": {"email": gcp.runner_email},
        "labels": labels,
    }
    if network:
        allocation["network"] = network
    container: Dict[str, Any] = {"imageUri": image_uri}
    if entrypoint:
        container["entrypoint"] = entrypoint
    container.update(commands=commands, options=options)
    task_group = {
        "taskCount": str(task_count),
        "parallelism": str(parallelism),
        "taskCountPerNode": str(placement.tasks_per_vm),
        "taskSpec": {
            "runnables": [
                {
                    "displayName": "yapnr-task",
                    "container": container,
                    "timeout": "%ds" % (wall + UPLOAD_GRACE_S),
                }
            ],
            "computeResource": {
                "cpuMilli": str(placement.cpu_milli),
                "memoryMib": str(placement.memory_mib),
            },
            "maxRunDuration": "%ds" % (wall + UPLOAD_GRACE_S + RUN_GRACE_S),
            "maxRetryCount": int(limits.max_retries),
            "lifecyclePolicies": [
                {"action": "RETRY_TASK", "actionCondition": {"exitCodes": RETRY_EXIT_CODES}}
            ],
            "environment": {
                "variables": dict(
                    {"YAPNR_BACKEND": "gcp-batch"},
                    **({"YAPNR_CLAIM_BUCKET": gcp.runs_bucket} if claims else {}),
                )
            },
            "volumes": [
                {
                    "gcs": {"remotePath": gcp.runs_bucket},
                    "mountPath": RUNS_MOUNT,
                    "mountOptions": list(gcp.gcsfuse_options),
                },
                {
                    "gcs": {"remotePath": "%s/bundles" % gcp.inputs_bucket},
                    "mountPath": INPUTS_MOUNT,
                    "mountOptions": list(gcp.gcsfuse_options) + ["-o ro"],
                },
            ],
        },
    }
    return {
        "taskGroups": [task_group],
        "allocationPolicy": allocation,
        # The reaper reads ``deadline`` from the job's own labels: allocationPolicy.labels are
        # documented for the VMs and disks, not for what a job listing returns.
        "labels": {
            "yapnr": "1",
            "campaign": label(cid),
            "submission": label(submission),
            "deadline": label(deadline),
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def check_job(job: Dict[str, Any]) -> Optional[str]:
    """Validate against the discovery document; returns its revision, or None if not shipped."""
    doc = batch_schema.load()
    if doc is None:
        return None
    errors = batch_schema.validate(job, doc)
    if errors:
        raise SubmitError("the rendered Batch job does not match the API: " + "; ".join(errors))
    return batch_schema.revision(doc)


def make_cloud(config: Config, dry_run: bool = False) -> Gcloud:
    gcp = config.require_gcp()
    kind = FakeCloud if dry_run else Gcloud
    return kind(
        project=gcp.project,
        impersonate=gcp.submit_email,
        gcloud=gcp.gcloud,
        configuration=gcp.gcloud_configuration,
    )


def list_tasks(record: Dict[str, Any], config: Config, cloud: Optional[Gcloud] = None) -> List[Any]:
    """The raw ``batch tasks list`` response for one submission's job -- every task's own status
    history (``status.statusEvents``), the shape :mod:`yapnr.exp.timing`'s task_timing mirror
    reads. ``[]`` for a dry run, a submission with no job yet, or an API call that fails
    (``check=False``, same tolerance ``state()`` already gives this call)."""
    cloud = cloud or make_cloud(config)
    job = record.get("job", {})
    if record.get("dry_run") or not job.get("id"):
        return []
    tasks = cloud.json(
        ["batch", "tasks", "list", "--job=%s" % job["id"], "--location=%s" % job["region"]],
        check=False,
    )
    return tasks if isinstance(tasks, list) else []


class GcpBatch(Backend):
    name = "gcp-batch"

    def stores(self, plan, config: Config, cloud=None) -> Stores:
        gcp = config.require_gcp()
        cloud = cloud or make_cloud(config)
        return Stores(GcsStore(gcp.runs_bucket, cloud), GcsStore(gcp.inputs_bucket, cloud))

    def render(self, plan, config, cls, submission, indices, deadline, job=None) -> Dict[str, str]:
        placement = (job.placement if job else None) or plan.placement(cls.name)
        rendered = render_job(
            plan.meta,
            config,
            cls,
            placement,
            submission,
            len(job.groups) if job else len(indices),
            deadline,
            parallelism=job.parallelism if job else None,
            cells_per_task=max((len(g) for g in job.groups), default=1) if job else 1,
            claims=bool(job) and len(job.groups) > 1 and bundles_allowed(plan),
        )
        check_job(rendered)
        return {"%s.job.json" % cls.name: json.dumps(rendered, indent=2, sort_keys=True) + "\n"}

    def layout(self, plan, config, cls, lines, cloud=None):
        table = cost.PriceTable.load(config.price_table)
        return layout(plan, config, cls, lines, table, cloud)

    live_states = LIVE_STATES

    def choose(self, plan, config, todo, cloud=None, *, region=None, say=print, previous=None):
        if region and region not in config.require_gcp().regions:
            raise SubmitError("region %s is not one of gcp.regions in the owner config" % region)
        return choose(
            plan,
            config,
            todo,
            cloud or make_cloud(config),
            region=region,
            say=say,
            previous=previous,
        )

    def check_limits(
        self, plan, config, todo, *, yes, max_usd, confirm, say, price_table=None, layouts=None
    ):
        # The kill switch's quota cut and the quota ceiling cover Spot (preemptible) CPUs only.
        on_demand = sorted(name for name in todo if plan.placement(name).model != "spot")
        if on_demand and not config.limits.allow_on_demand:
            raise SubmitError(
                "class(es) %s would run on-demand VMs, which the quota ceiling and the budget "
                "guard's quota cut do not cover; set limits.allow_on_demand = true in the owner "
                "config to allow it" % ", ".join(on_demand)
            )
        table = price_table or cost.PriceTable.load(config.price_table)
        packings = None
        if layouts:
            packings = {name: packing.Packing(jobs=list(jobs)) for name, jobs in layouts.items()}
        est = planning.estimate(
            self.name,
            plan.classes,
            plan.placements(),
            config,
            table,
            subset=todo,
            packings=packings,
            bundle=bundles_allowed(plan),
        )
        count = sum(len(v) for v in todo.values())
        check = cost.check_caps(
            est,
            config.limits,
            tasks=count,
            max_wall_s=max(c.max_wall_s for c in plan.classes if c.name in todo),
            max_usd=max_usd,
        )
        say(
            "estimate: expected $%.2f (VM time %.2f VM-h on %d VM(s)), ceiling $%.2f, about %.1f h "
            "(prices %s)"
            % (
                est.expected_usd,
                sum(c.vm_hours for c in est.classes),
                sum(c.vms for c in est.classes),
                est.ceiling_usd,
                est.makespan_h,
                table.accessed,
            )
        )
        if check.refusals:
            raise SubmitError("refused: " + "; ".join(check.refusals))
        if check.confirm and not yes:
            prompt = "expected $%.2f is above limits.confirm_usd $%.2f; submit? [y/N] " % (
                est.expected_usd,
                config.limits.confirm_usd,
            )
            if not confirm(prompt):
                raise SubmitError("not confirmed (pass --yes after reading the estimate)")

    def launch(
        self, plan, config, cls, submission, files, stores, cloud=None, dry_run=False, job=None
    ):
        gcp = config.require_gcp()
        placement = (job.placement if job else None) or plan.placement(cls.name)
        cloud = cloud or make_cloud(config, dry_run)
        name = job_id(plan.id, submission)
        path = files["%s.job.json" % cls.name]
        stores.runs.upload(path, "campaigns/%s/submissions/%d.job.json" % (plan.id, submission))
        result = cloud.json(
            [
                "batch",
                "jobs",
                "submit",
                name,
                "--location=%s" % placement.region,
                "--config=%s" % path,
            ]
        )
        uid = (result or {}).get("uid") if isinstance(result, dict) else None
        return {
            "name": "projects/%s/locations/%s/jobs/%s" % (gcp.project, placement.region, name),
            "id": name,
            "uid": uid,
            "region": placement.region,
            "summary": "%s in %s (%s, %s)"
            % (name, placement.region, placement.shape, placement.model),
        }

    def state(self, plan_meta, record, config, cloud=None):
        cloud = cloud or make_cloud(config)
        job = record.get("job", {})
        if record.get("dry_run") or not job.get("id"):
            return None
        data = cloud.json(
            ["batch", "jobs", "describe", job["id"], "--location=%s" % job["region"]], check=False
        )
        if not isinstance(data, dict):
            return {"state": "UNKNOWN", "counts": {}, "preemptions": 0}
        status = data.get("status", {})
        counts: Dict[str, int] = {}
        for group in (status.get("taskGroups") or {}).values():
            for key, value in (group.get("counts") or {}).items():
                counts[key] = counts.get(key, 0) + int(value)
        # Exit codes are on task-level status events only (StatusEvent.taskExecution: "only
        # defined for task-level status events where the task fails"), so count them per task.
        tasks = list_tasks(record, config, cloud)
        preemptions = sum(
            1
            for task in (tasks if isinstance(tasks, list) else [])
            for event in (task.get("status") or {}).get("statusEvents") or []
            if (event.get("taskExecution") or {}).get("exitCode") == 50001
        )
        return {
            "state": status.get("state", "UNKNOWN"),
            "counts": counts,
            "preemptions": preemptions,
        }

    def cancel(self, record, config, cloud=None, dry_run=False) -> str:
        cloud = cloud or make_cloud(config, dry_run)
        job = record.get("job", {})
        if not job.get("id"):
            return "submission %s has no job" % record.get("submission")
        cloud.run(["batch", "jobs", "cancel", job["id"], "--location=%s" % job["region"]])
        return "cancel requested for %s" % job["id"]

    def logs(self, record, config, cloud=None, index: Optional[int] = None, limit: int = 200):
        cloud = cloud or make_cloud(config)
        job = record.get("job", {})
        if not job.get("uid"):
            return []
        query = 'logName="projects/%s/logs/batch_task_logs" AND labels.job_uid="%s"' % (
            config.require_gcp().project,
            job["uid"],
        )
        if index is not None:
            query += ' AND labels.task_id:"-group0-%d/"' % index
        entries = cloud.json(
            ["logging", "read", query, "--limit=%d" % limit, "--order=asc", "--freshness=30d"]
        )
        return [e.get("textPayload") or json.dumps(e.get("jsonPayload")) for e in entries or []]


def doctor(config: Config, cloud: Gcloud, digest: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read-only checks of the infrastructure, each {'check', 'ok', 'detail'}."""
    gcp = config.require_gcp()
    checks: List[Dict[str, Any]] = []

    def run(name: str, args: Sequence[str], judge=None):
        try:
            data = cloud.json(list(args))
            ok, detail = judge(data) if judge else (True, "")
        except Exception as err:  # every failure is a finding, never a crash
            ok, detail = False, str(err).splitlines()[0][:200]
        checks.append({"check": name, "ok": ok, "detail": detail})

    for name in (gcp.runs_bucket, gcp.inputs_bucket):
        # An object listing, not `buckets describe`: the submit account holds object roles on the
        # buckets (objectAdmin), not storage.buckets.get, and object access is what it needs.
        run(
            "bucket %s" % name,
            ["storage", "objects", "list", "gs://%s/" % name, "--limit=1"],
        )
    frozen = GcsStore(gcp.runs_bucket, cloud)
    try:
        fired = frozen.exists("control/frozen")
        checks.append(
            {
                "check": "budget kill switch not fired",
                "ok": not fired,
                "detail": "control/frozen exists: run `yapnr exp unfreeze`" if fired else "",
            }
        )
    except Exception as err:
        checks.append(
            {"check": "budget kill switch not fired", "ok": False, "detail": str(err)[:200]}
        )
    # Every template, once: the ranked Hyperdisk pairs of each region need their own.
    templated: Dict[str, List[str]] = {}
    listing_error = ""
    try:
        listing = cloud.json(["compute", "instance-templates", "list", "--filter=name~^yapnr-"])
        for item in listing if isinstance(listing, list) else []:
            machine = str((item.get("properties") or {}).get("machineType", "")).rsplit("/", 1)[-1]
            if machine:
                templated.setdefault(machine, []).append(item.get("name", ""))
    except Exception as err:  # a finding, never a crash
        listing_error = str(err).splitlines()[0][:200]
    for region in gcp.regions:
        run(
            "batch jobs list in %s" % region,
            ["batch", "jobs", "list", "--location=%s" % region, "--limit=1"],
        )

        def quota(data, region=region):
            quotas = {q.get("metric"): q for q in (data or {}).get("quotas", [])}
            spot = quotas.get(SPOT_QUOTA, {})
            limit = spot.get("limit", 0)
            return limit > 0, "preemptible CPUs: %g of %g in use" % (spot.get("usage", 0), limit)

        run("quota in %s" % region, ["compute", "regions", "describe", region], quota)
        for family, ranked_region in gcp.ranking:
            if ranked_region != region or family not in gcp.template_families:
                continue
            shapes = sorted(
                shape
                for shape in templated
                if shape.split("-")[0] == family
                and gcp.template.format(shape=shape, model="spot", region=region)
                in templated[shape]
            )
            checks.append(
                {
                    "check": "templates for ranked %s/%s" % (family, region),
                    "ok": bool(shapes) and not listing_error,
                    "detail": listing_error
                    or (
                        ", ".join(shapes)
                        if shapes
                        else "none: add the region's shapes to "
                        "template_shapes or region_template_shapes in the tfvars and apply"
                    ),
                }
            )
        if digest:
            registry = gcp.registry.format(region=region, project=gcp.project)
            run(
                "image in %s" % region,
                [
                    "artifacts",
                    "docker",
                    "images",
                    "describe",
                    "%s/studio-fug/yapnr@%s" % (registry, digest),
                ],
            )
    return checks
