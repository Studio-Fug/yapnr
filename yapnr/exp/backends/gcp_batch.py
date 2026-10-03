"""Google Cloud Batch: one job per submission, Spot VMs, the stores as two buckets.

``render_job`` is a pure function of the plan, the owner config and the submission; its output is
checked against the vendored Batch v1 discovery document before anything is sent. The job:

- runs the published image from the region's Artifact Registry remote repository (a pull-through
  cache of ghcr.io), pinned by digest, with the image's ``yapnr-kicad-env`` as entrypoint;
- runs ``task.py`` (the campaign's copy, in the runs bucket) once per task: ``BATCH_TASK_INDEX``
  maps through ``submissions/<n>.indices`` to a line of ``tasks.jsonl``;
- mounts the runs bucket read-write and the inputs bucket's ``bundles/`` read-only (gcsfuse);
- retries only infrastructure failures (Spot preemption 50001, VM lost 50002, VM rebooted 50003,
  VM recreated 50006) and the wrapper's transient 75, at most ``limits.max_retries`` times;
- packs ``tasks_per_vm`` tasks per VM (one per physical core by default), from an instance template
  for Hyperdisk-only families (C4D, C4, C4A, N4) or an instance policy otherwise;
- carries the labels the budget guard, the reaper and billing reports use (``yapnr``, ``campaign``,
  ``kind``, ``visibility``, ``submission``, ``deadline``).

Every ``gcloud`` call goes through ``yapnr.exp.cloud.Gcloud`` with the impersonated submit account.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from yapnr.exp import batch_schema, cost, image
from yapnr.exp import plan as planning
from yapnr.exp.backends.base import Backend, Stores, SubmitError
from yapnr.exp.cloud import FakeCloud, Gcloud
from yapnr.exp.config import Config, Gcp
from yapnr.exp.store import GcsStore

RUNS_MOUNT = "/mnt/disks/runs"
INPUTS_MOUNT = "/mnt/disks/inputs"
IMAGE_PYTHON = "/opt/venv/bin/python"
IMAGE_ENTRYPOINT = "/usr/local/bin/yapnr-kicad-env"
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


def render_job(
    plan_meta: Dict[str, Any],
    config: Config,
    cls: planning.ResourceClass,
    placement: cost.Placement,
    submission: int,
    task_count: int,
    deadline: int,
) -> Dict[str, Any]:
    """The Batch job (REST ``Job``) for one submission of one resource class."""
    gcp = config.require_gcp()
    limits = config.limits
    cid = plan_meta["id"]
    ref = image.parse(cls.image)
    if not ref.digest:
        raise SubmitError("the image %s is not pinned to a digest" % cls.image)
    registry = gcp.registry.format(region=placement.region, project=gcp.project)
    image_uri = image.mirror(ref, registry) if ref.registry == "ghcr.io" else ref.pinned
    parallelism = cost.parallel_tasks(task_count, placement, limits.max_parallel_vcpus)
    wall = int(cls.max_wall_s)
    options = "--init --shm-size 1g"
    if gcp.container_user:
        options += " --user %s" % gcp.container_user
    commands = [
        IMAGE_PYTHON,
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
    task_group = {
        "taskCount": str(task_count),
        "parallelism": str(parallelism),
        "taskCountPerNode": str(placement.tasks_per_vm),
        "taskSpec": {
            "runnables": [
                {
                    "displayName": "yapnr-task",
                    "container": {
                        "imageUri": image_uri,
                        "entrypoint": IMAGE_ENTRYPOINT,
                        "commands": commands,
                        "options": options,
                    },
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
            "environment": {"variables": {"YAPNR_BACKEND": "gcp-batch"}},
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


class GcpBatch(Backend):
    name = "gcp-batch"

    def stores(self, plan, config: Config, cloud=None) -> Stores:
        gcp = config.require_gcp()
        cloud = cloud or make_cloud(config)
        return Stores(GcsStore(gcp.runs_bucket, cloud), GcsStore(gcp.inputs_bucket, cloud))

    def render(self, plan, config, cls, submission, indices, deadline) -> Dict[str, str]:
        placement = plan.placement(cls.name)
        job = render_job(plan.meta, config, cls, placement, submission, len(indices), deadline)
        check_job(job)
        return {"%s.job.json" % cls.name: json.dumps(job, indent=2, sort_keys=True) + "\n"}

    live_states = LIVE_STATES

    def check_limits(self, plan, config, todo, *, yes, max_usd, confirm, say, price_table=None):
        # The kill switch's quota cut and the quota ceiling cover Spot (preemptible) CPUs only.
        on_demand = sorted(name for name in todo if plan.placement(name).model != "spot")
        if on_demand and not config.limits.allow_on_demand:
            raise SubmitError(
                "class(es) %s would run on-demand VMs, which the quota ceiling and the budget "
                "guard's quota cut do not cover; set limits.allow_on_demand = true in the owner "
                "config to allow it" % ", ".join(on_demand)
            )
        table = price_table or cost.PriceTable.load(config.price_table)
        est = planning.estimate(
            self.name, plan.classes, plan.placements(), config, table, subset=todo
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
            "estimate: expected $%.2f, ceiling $%.2f, about %.1f h (prices %s)"
            % (est.expected_usd, est.ceiling_usd, est.makespan_h, table.accessed)
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

    def launch(self, plan, config, cls, submission, files, stores, cloud=None, dry_run=False):
        gcp = config.require_gcp()
        placement = plan.placement(cls.name)
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
        tasks = cloud.json(
            ["batch", "tasks", "list", "--job=%s" % job["id"], "--location=%s" % job["region"]],
            check=False,
        )
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
        run("bucket %s" % name, ["storage", "buckets", "describe", "gs://%s" % name])
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
    for region in gcp.regions:
        run(
            "batch jobs list in %s" % region,
            ["batch", "jobs", "list", "--location=%s" % region, "--limit=1"],
        )

        def quota(data, region=region):
            quotas = {q.get("metric"): q for q in (data or {}).get("quotas", [])}
            spot = quotas.get("PREEMPTIBLE_CPUS", {})
            limit = spot.get("limit", 0)
            return limit > 0, "preemptible CPUs: %s" % limit

        run("quota in %s" % region, ["compute", "regions", "describe", region], quota)
        for family, ranked_region in gcp.ranking:
            if ranked_region != region or family not in gcp.template_families:
                continue
            shape = "%s-highcpu-16" % family
            name = gcp.template.format(shape=shape, model="spot", region=region)
            run(
                "template %s" % name,
                ["compute", "instance-templates", "describe", name, "--global"],
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
