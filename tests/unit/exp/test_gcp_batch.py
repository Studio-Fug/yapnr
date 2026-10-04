"""The gcp-batch backend: golden job JSON validated against the Batch v1 discovery document, the
exact gcloud calls of a dry-run submit, caps, the freeze, status and cancel; all with FakeCloud."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import batch_schema, cost
from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import base, gcp_batch
from yapnr.exp.cloud import FakeCloud


def render(plan, config, cls_name=None, submission=1):
    cls = (
        plan.classes[0] if cls_name is None else next(c for c in plan.classes if c.name == cls_name)
    )
    return gcp_batch.render_job(
        plan.meta,
        config,
        cls,
        plan.placement(cls.name),
        submission,
        len(cls.lines),
        testing.DEADLINE,
    )


class GcpBatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp)
        self.repo = testing.fixture_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, text, **kw):
        path = testing.write_campaign(self.tmp / "campaign.toml", text)
        kw.setdefault("out", self.tmp / "plan")
        return planning.make_plan(
            path,
            "gcp-batch",
            self.config,
            repo=self.repo,
            offline=True,
            image_digest=testing.DIGEST,
            today=testing.TODAY,
            **kw,
        )

    def golden_json(self, name, job):
        testing.check_golden(
            self, name, testing.normalize(json.dumps(job, indent=2, sort_keys=True) + "\n")
        )

    def test_ladder_job_on_a_c4d_template(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        job = render(plan, self.config)
        self.assertEqual(batch_schema.validate(job, batch_schema.load()), [])
        self.golden_json("batch-ladder-c4d-template.job.json", job)
        group = job["taskGroups"][0]
        self.assertEqual(group["taskCount"], "4")
        self.assertEqual(group["taskSpec"]["computeResource"]["cpuMilli"], "2000")
        self.assertEqual(
            group["taskSpec"]["lifecyclePolicies"][0]["actionCondition"]["exitCodes"],
            [50001, 50002, 50003, 50006, 75],
        )
        container = group["taskSpec"]["runnables"][0]["container"]
        self.assertEqual(
            container["imageUri"],
            "us-west4-docker.pkg.dev/example-project/ghcr/studio-fug/yapnr@" + testing.DIGEST,
        )
        instance = job["allocationPolicy"]["instances"][0]
        self.assertEqual(instance, {"instanceTemplate": "yapnr-c4d-highcpu-16-spot-us-west4"})
        self.assertNotIn("network", job["allocationPolicy"])  # the template's network applies

    def test_smoke_job_without_a_template(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN, shape="c4d-highcpu-4", template=False)
        job = render(plan, self.config)
        self.assertEqual(batch_schema.validate(job, batch_schema.load()), [])
        self.golden_json("batch-smoke-c4d-policy.job.json", job)
        policy = job["allocationPolicy"]["instances"][0]["policy"]
        self.assertEqual(policy["provisioningModel"], "SPOT")
        self.assertEqual(policy["bootDisk"]["type"], "hyperdisk-balanced")
        nic = job["allocationPolicy"]["network"]["networkInterfaces"][0]
        self.assertTrue(nic["noExternalIpAddress"])

    def test_limits_reach_the_job(self):
        self.config.limits.max_retries = 1
        self.config.limits.max_parallel_vcpus = 4
        plan = self.plan(testing.LADDER_CAMPAIGN)
        group = render(plan, self.config)["taskGroups"][0]
        self.assertEqual(group["taskSpec"]["maxRetryCount"], 1)
        self.assertEqual(group["parallelism"], "2")  # 4 vCPUs / 2 vCPUs per task
        self.assertEqual(group["taskSpec"]["maxRunDuration"], "2400s")  # 1800 + 300 + 300
        self.assertEqual(group["taskSpec"]["runnables"][0]["timeout"], "2100s")

    def test_an_image_of_the_project_with_its_own_runtime(self):
        # A task image from the project's images repository (openEMS): pulled as named, run with
        # the campaign's interpreter and without the yapnr launcher.
        ref = "us-west4-docker.pkg.dev/example-project/images/openems:x86-64-v4"
        stage = self.tmp / "stage.jsonl"
        stage.write_text(
            json.dumps(
                {
                    "id": "m0",
                    "command": ["${PYTHON}", "job/run.py"],
                    "record": "out/m0/job.json",
                    "resources": {"cpus": 4, "memory_gb": 4, "max_wall_s": 3600},
                }
            )
            + "\n"
        )
        campaign = (
            'schema = "yapnr-campaign-v1"\nkind = "mc-eval"\nname = "em"\nsource = "none"\n'
            'image = "%s"\n\n[runtime]\npython = "/opt/openEMS/venv/bin/python"\n'
            'entrypoint = ""\n\n[config]\nstage_plan = "stage.jsonl"\n' % ref
        )
        plan = self.plan(campaign)
        self.assertEqual(
            plan.meta["image"]["runtime"],
            {"python": "/opt/openEMS/venv/bin/python", "entrypoint": ""},
        )
        job = render(plan, self.config)
        self.assertEqual(batch_schema.validate(job, batch_schema.load()), [])
        container = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]
        self.assertEqual(container["imageUri"], ref.rsplit(":", 1)[0] + "@" + testing.DIGEST)
        self.assertNotIn("entrypoint", container)
        self.assertEqual(container["commands"][0], "/opt/openEMS/venv/bin/python")
        tasks = [json.loads(x) for x in (plan.dir / "tasks.jsonl").read_text().splitlines()]
        self.assertEqual([t["entrypoint"] for t in tasks], [None])
        # The yapnr image keeps its launcher and interpreter.
        job = render(self.plan(testing.SMOKE_CAMPAIGN, replace=True), self.config)
        container = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]
        self.assertEqual(container["entrypoint"], gcp_batch.IMAGE_ENTRYPOINT)
        self.assertEqual(container["commands"][0], gcp_batch.IMAGE_PYTHON)

    def test_reaper_finds_the_deadline_on_the_job(self):
        # The reaper (infra/gcp/functions/guard) cancels a job by its own `deadline` label.
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        job = render(plan, self.config)
        self.assertEqual(job["labels"]["deadline"], str(testing.DEADLINE))
        self.assertEqual(job["labels"]["yapnr"], "1")
        self.assertEqual(job["allocationPolicy"]["labels"]["deadline"], str(testing.DEADLINE))

    def test_labels_and_job_ids(self):
        self.assertEqual(gcp_batch.label("Ladder/Cell.1"), "ladder-cell-1")
        self.assertEqual(
            gcp_batch.job_id("20261002-ladder-3f9a1c", 2), "yapnr-20261002-ladder-3f9a1c-s2"
        )

    def test_validator_catches_mistakes(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        job = render(plan, self.config)
        job["taskGroups"][0]["taskSpec"]["maxRunDuration"] = "2h"
        job["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"] = "CHEAP"
        job["status"] = {}
        job["taskGroups"][0]["taskSpec"]["computeResource"]["gpus"] = 1
        errors = batch_schema.validate(job, batch_schema.load())
        self.assertEqual(len(errors), 4, errors)

    def test_dry_run_submit_makes_exactly_these_calls(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        cloud = gcp_batch.make_cloud(self.config, dry_run=True)
        said = []
        done = gcp_batch.GcpBatch().submit(
            plan,
            self.config,
            dry_run=True,
            cloud=cloud,
            now=lambda: testing.DEADLINE,
            say=said.append,
        )
        self.assertEqual([s.number for s in done], [1])
        text = testing.normalize(cloud.transcript() + "\n", self.tmp)
        testing.check_golden(self, "batch-submit-dry-run.txt", text)
        for call in cloud.calls:
            self.assertIn("--impersonate-service-account=" + self.config.gcp.submit_email, call)
            self.assertIn("--project=example-project", call)
        record = json.loads(
            cloud.objects["gs://example-yapnr-runs/campaigns/%s/submissions/1.json" % plan.id]
        )
        self.assertEqual(record["job"]["id"], "yapnr-%s-s1" % plan.id)
        self.assertEqual(record["deadline"], testing.DEADLINE + 12 * 3600)

    def test_second_submit_sends_only_pending_tasks(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        cloud = FakeCloud()
        cloud.impersonate = self.config.gcp.submit_email
        backend = gcp_batch.GcpBatch()
        first = backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
        cloud.jobs[first[0].record["job"]["name"]]["status"]["state"] = "FAILED"
        marker = {"task": plan.tasks[0]["id"], "attempt": "s1r0", "verdict": "done"}
        key = "gs://example-yapnr-runs/campaigns/%s/tasks/%s/_DONE" % (plan.id, "smoke~0")
        cloud.objects[key] = (json.dumps(marker) + "\n").encode()
        again = backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
        self.assertEqual(again[0].number, 2)
        self.assertEqual(again[0].indices, [1])
        job = json.loads(Path(plan.dir / "submissions" / "2" / "c1m1.job.json").read_text())
        self.assertEqual(job["taskGroups"][0]["taskCount"], "1")
        self.assertEqual(
            cloud.objects["gs://example-yapnr-runs/campaigns/%s/submissions/2.indices" % plan.id],
            b"1\n",
        )
        cloud.objects["gs://example-yapnr-runs/campaigns/%s/tasks/smoke~1/_DONE" % plan.id] = (
            json.dumps(dict(marker, task=plan.tasks[1]["id"])) + "\n"
        ).encode()
        self.assertEqual(backend.submit(plan, self.config, cloud=cloud, say=lambda s: None), [])

    def test_a_live_job_holding_pending_tasks_refuses_a_resubmit(self):
        # A repeated submit (or a retry loop around it) must not run and bill the tasks twice.
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        cloud = FakeCloud()
        backend = gcp_batch.GcpBatch()
        first = backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
        name = first[0].record["job"]["name"]
        for state in ("QUEUED", "SCHEDULED", "RUNNING"):
            cloud.jobs[name]["status"]["state"] = state
            with self.assertRaises(base.SubmitError) as ctx:
                backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
            self.assertIn("twice", str(ctx.exception))
        self.assertEqual(sum(c[1:4] == ["batch", "jobs", "submit"] for c in cloud.calls), 1)
        cloud.jobs[name]["status"]["state"] = "CANCELLED"
        again = backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
        self.assertEqual(again[0].number, 2)

    def test_on_demand_needs_the_owner_config(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN + "\n[placement]\nspot = false\n")
        backend = gcp_batch.GcpBatch()
        with self.assertRaises(base.SubmitError) as ctx:
            backend.submit(plan, self.config, cloud=FakeCloud(), say=lambda s: None)
        self.assertIn("allow_on_demand", str(ctx.exception))
        self.config.limits.allow_on_demand = True
        done = backend.submit(plan, self.config, cloud=FakeCloud(), say=lambda s: None)
        self.assertEqual(len(done), 1)

    def test_frozen_store_refuses(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        cloud = FakeCloud()
        cloud.objects["gs://example-yapnr-runs/control/frozen"] = b"budget 100%\n"
        with self.assertRaises(base.SubmitError) as ctx:
            gcp_batch.GcpBatch().submit(plan, self.config, cloud=cloud, say=lambda s: None)
        self.assertIn("frozen", str(ctx.exception))
        self.assertFalse(any(c[1:3] == ["batch", "jobs"] for c in cloud.calls))

    def test_ceiling_above_refuse_usd_is_refused_and_max_usd_is_bounded(self):
        self.config.limits.refuse_usd = 0.001
        plan = self.plan(testing.LADDER_CAMPAIGN)
        backend = gcp_batch.GcpBatch()
        with self.assertRaises(base.SubmitError):
            backend.submit(plan, self.config, cloud=FakeCloud(), say=lambda s: None)
        with self.assertRaises(base.SubmitError) as ctx:
            backend.submit(plan, self.config, cloud=FakeCloud(), max_usd=1000, say=lambda s: None)
        self.assertIn("hard_refuse_usd", str(ctx.exception))
        done = backend.submit(plan, self.config, cloud=FakeCloud(), max_usd=50, say=lambda s: None)
        self.assertEqual(len(done), 1)

    def test_confirmation_above_confirm_usd(self):
        self.config.limits.confirm_usd = 0
        plan = self.plan(testing.LADDER_CAMPAIGN)
        backend = gcp_batch.GcpBatch()
        asked = []
        with self.assertRaises(base.SubmitError):
            backend.submit(
                plan,
                self.config,
                cloud=FakeCloud(),
                confirm=lambda p: asked.append(p) or False,
                say=lambda s: None,
            )
        self.assertEqual(len(asked), 1)
        done = backend.submit(plan, self.config, cloud=FakeCloud(), yes=True, say=lambda s: None)
        self.assertEqual(len(done), 1)

    def test_status_and_cancel(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        cloud = FakeCloud()
        backend = gcp_batch.GcpBatch()
        backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
        record = base.submissions(backend.stores(plan, self.config, cloud).runs, plan.id)[0]
        name = record["job"]["name"]
        cloud.jobs[name]["status"] = {
            "state": "RUNNING",
            "taskGroups": {"group0": {"counts": {"RUNNING": "1", "SUCCEEDED": "1"}}},
            "statusEvents": [{"type": "STATUS_CHANGED", "description": "Job state is RUNNING"}],
        }
        # Exit codes are on the tasks' own status events (StatusEvent.taskExecution).
        preempted = {"taskExecution": {"exitCode": 50001}, "taskState": "FAILED"}
        cloud.tasks[name] = [
            {"name": name + "/taskGroups/group0/tasks/0", "status": {"statusEvents": [preempted]}},
            {
                "name": name + "/taskGroups/group0/tasks/1",
                "status": {"statusEvents": [preempted, {"taskExecution": {"exitCode": 75}}]},
            },
        ]
        view = backend.state(plan.meta, record, self.config, cloud)
        self.assertEqual(
            view, {"state": "RUNNING", "counts": {"RUNNING": 1, "SUCCEEDED": 1}, "preemptions": 2}
        )
        self.assertIn("--job=" + record["job"]["id"], cloud.calls[-1])
        backend.cancel(record, self.config, cloud)
        self.assertEqual(cloud.jobs[name]["status"]["state"], "CANCELLATION_IN_PROGRESS")
        self.assertIn("cancel", cloud.calls[-1])

    def test_doctor_reports_each_check(self):
        cloud = FakeCloud()
        cloud.handlers.append(
            lambda args: (
                __import__("yapnr.exp.cloud", fromlist=["Result"]).Result(
                    0, json.dumps({"quotas": [{"metric": "PREEMPTIBLE_CPUS", "limit": 64}]}), ""
                )
                if args[:3] == ["compute", "regions", "describe"]
                else None
            )
        )
        checks = gcp_batch.doctor(self.config, cloud)
        names = [c["check"] for c in checks]
        self.assertIn("budget kill switch not fired", names)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        # Bucket access is checked with an object listing the submit account is allowed to make.
        listings = [c for c in cloud.calls if c[1:4] == ["storage", "objects", "list"]]
        self.assertEqual(len(listings), 2, cloud.calls)
        self.assertFalse(any(c[1:4] == ["storage", "buckets", "describe"] for c in cloud.calls))

    # Quota-aware placement at submit: the first ranked pair whose region has Spot quota for one
    # more VM, with the quota and the templates answered by the fake gcloud.

    def ranked_plan(self, **kw):
        self.config.gcp.ranking = list(testing.RANKING)
        text = testing.SMOKE_CAMPAIGN.replace("[config]", testing.RANKED_FAMILIES + "\n[config]")
        return self.plan(text, **kw)

    def two_regions(self, west, northeast, templates=None):
        cloud = FakeCloud()
        cloud.quotas = {"us-west4": west, "northamerica-northeast1": northeast}
        cloud.templates = dict(testing.TEMPLATES if templates is None else templates)
        return cloud

    def submit(self, plan, cloud, **kw):
        said = []
        done = gcp_batch.GcpBatch().submit(plan, self.config, cloud=cloud, say=said.append, **kw)
        return done, said

    @staticmethod
    def calls(cloud, *prefix):
        return [c[1:] for c in cloud.calls if c[1 : 1 + len(prefix)] == list(prefix)]

    def test_a_class_goes_to_the_first_ranked_region_with_room(self):
        plan = self.ranked_plan()
        cloud = self.two_regions(west=(64, 48), northeast=(64, 0))
        done, said = self.submit(plan, cloud)
        record = done[0].record
        self.assertEqual(record["job"]["region"], "us-west4")
        choice = record["placement"]["choice"]
        self.assertEqual((choice["pair"], choice["rank"]), ("c4d/us-west4", 1))
        self.assertIn("16 of 64 Spot vCPUs free", choice["why"])
        self.assertEqual(choice["looked_at"][0]["free"], 16)
        # The first region had room, so the second was not asked.
        self.assertEqual(
            [c[3] for c in self.calls(cloud, "compute", "regions", "describe")], ["us-west4"]
        )
        self.assertTrue(any("c4d-highcpu-16 in us-west4 (candidate 1)" in s for s in said), said)

    def test_a_full_region_spills_to_the_next_ranked_pair(self):
        plan = self.ranked_plan()
        cloud = self.two_regions(west=(64, 56), northeast=(64, 0))
        done, _ = self.submit(plan, cloud)
        record = done[0].record
        self.assertEqual(record["job"]["region"], "northamerica-northeast1")
        self.assertEqual(record["placement"]["region"], "northamerica-northeast1")
        self.assertEqual(record["placement"]["shape"], "c4-highcpu-16")
        choice = record["placement"]["choice"]
        self.assertEqual((choice["pair"], choice["rank"]), ("c4/northamerica-northeast1", 2))
        self.assertTrue(choice["looked_at"][0]["full"])
        self.assertEqual(choice["looked_at"][0]["free"], 8)  # less than one 16-vCPU VM
        submit = self.calls(cloud, "batch", "jobs", "submit")[0]
        self.assertIn("--location=northamerica-northeast1", submit)
        job = json.loads(Path(plan.dir / "submissions" / "1" / "c1m1.job.json").read_text())
        self.assertEqual(
            job["allocationPolicy"]["instances"][0],
            {"instanceTemplate": "yapnr-c4-highcpu-16-spot-northamerica-northeast1"},
        )
        self.assertEqual(
            job["allocationPolicy"]["location"]["allowedLocations"],
            ["regions/northamerica-northeast1"],
        )
        image = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["imageUri"]
        self.assertTrue(image.startswith("northamerica-northeast1-docker.pkg.dev/"), image)
        self.assertEqual(batch_schema.validate(job, batch_schema.load()), [])

    def test_when_no_region_has_room_the_class_waits_in_the_first(self):
        plan = self.ranked_plan()
        cloud = self.two_regions(west=(64, 64), northeast=(64, 60))
        done, _ = self.submit(plan, cloud)
        choice = done[0].record["placement"]["choice"]
        self.assertEqual((choice["pair"], choice["rank"]), ("c4d/us-west4", 1))
        self.assertIn("waits in us-west4", choice["why"])
        self.assertEqual(len(choice["looked_at"]), 3)
        self.assertEqual(done[0].record["job"]["region"], "us-west4")

    def test_a_pair_without_its_template_is_skipped(self):
        plan = self.ranked_plan()
        templates = dict(testing.TEMPLATES)
        del templates["yapnr-c4d-highcpu-16-spot-us-west4"]
        cloud = self.two_regions(west=(64, 0), northeast=(64, 0), templates=templates)
        done, _ = self.submit(plan, cloud)
        choice = done[0].record["placement"]["choice"]
        self.assertEqual(choice["pair"], "c4/northamerica-northeast1")
        self.assertIn("no instance template", choice["looked_at"][0]["skipped"])
        # Not one candidate with a template: refused before anything is uploaded.
        plan = self.ranked_plan(out=self.tmp / "none")
        cloud = self.two_regions(west=(64, 0), northeast=(64, 0), templates={})
        with self.assertRaises(base.SubmitError) as ctx:
            self.submit(plan, cloud)
        self.assertIn("region_template_shapes", str(ctx.exception))
        self.assertEqual(self.calls(cloud, "batch", "jobs", "submit"), [])
        self.assertEqual(self.calls(cloud, "storage", "cp"), [])

    def test_an_unreadable_quota_is_not_room(self):
        plan = self.ranked_plan()
        cloud = self.two_regions(west=(64, 0), northeast=(64, 0))
        del cloud.quotas["us-west4"]  # the fake answers without quotas
        done, _ = self.submit(plan, cloud)
        choice = done[0].record["placement"]["choice"]
        self.assertEqual(choice["pair"], "c4/northamerica-northeast1")
        self.assertIn("unknown", choice["looked_at"][0]["quota"])
        # Nothing readable anywhere: the first candidate, as before quotas were read.
        plan = self.ranked_plan(out=self.tmp / "blind")
        cloud = self.two_regions(west=(64, 0), northeast=(64, 0))
        cloud.quotas = {}
        done, _ = self.submit(plan, cloud)
        self.assertEqual(done[0].record["placement"]["choice"]["pair"], "c4d/us-west4")

    def test_submit_region_pins_the_region(self):
        plan = self.ranked_plan()
        cloud = self.two_regions(west=(64, 0), northeast=(64, 64))
        done, _ = self.submit(plan, cloud, region="northamerica-northeast1")
        choice = done[0].record["placement"]["choice"]
        self.assertEqual(choice["pair"], "c4/northamerica-northeast1")
        self.assertEqual(choice["why"], "pinned to northamerica-northeast1")
        self.assertEqual(choice["region_pin"], "northamerica-northeast1")
        self.assertEqual(self.calls(cloud, "compute", "regions", "describe"), [])
        # A region the config does not enable, or one the plan has no candidate in.
        with self.assertRaises(base.SubmitError):
            self.submit(self.ranked_plan(out=self.tmp / "eu"), cloud, region="europe-west4")
        pinned = self.ranked_plan(out=self.tmp / "ne", region="northamerica-northeast1")
        with self.assertRaises(base.SubmitError) as ctx:
            self.submit(pinned, cloud, region="us-west4")
        self.assertIn("no candidate in us-west4", str(ctx.exception))

    def test_classes_of_one_submit_count_against_the_region(self):
        # Two classes: the first takes two 16-vCPU VMs of us-west4's last 32 free vCPUs, so the
        # second goes to the next region instead of queueing behind it.
        lines = [
            {
                "id": "a%d" % i,
                "command": ["${PYTHON}", "-c", "pass"],
                "record": "out/a%d.json" % i,
                "resources": {"cpus": 1, "memory_gb": 1, "disk_gb": 1, "max_wall_s": 600},
            }
            for i in range(16)
        ]
        lines.append(
            {
                "id": "b0",
                "command": ["${PYTHON}", "-c", "pass"],
                "record": "out/b0.json",
                "resources": {"cpus": 4, "memory_gb": 4, "max_wall_s": 600},
            }
        )
        (self.tmp / "stage.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        campaign = (
            'schema = "yapnr-campaign-v1"\nkind = "mc-eval"\nname = "two"\nsource = "none"\n'
            'image = "edge"\ndeterminism = "seeded"\n\n%s\n[config]\nstage_plan = "stage.jsonl"\n'
            % testing.RANKED_FAMILIES
        )
        self.config.gcp.ranking = list(testing.RANKING)
        plan = self.plan(campaign)
        self.assertEqual([len(c.lines) for c in plan.classes], [16, 1])
        self.assertEqual(plan.placement(plan.classes[0].name).tasks_per_vm, 8)
        cloud = self.two_regions(west=(64, 32), northeast=(64, 0))
        done, _ = self.submit(plan, cloud)
        first, second = (d.record["placement"]["choice"] for d in done)
        self.assertEqual(first["pair"], "c4d/us-west4")
        self.assertEqual(second["pair"], "c4/northamerica-northeast1")
        self.assertEqual(second["looked_at"][0]["claimed"], 32)
        self.assertEqual(second["looked_at"][0]["free"], 0)
        # The estimate the caps checked priced each class where it went.
        regions = [d.record["job"]["region"] for d in done]
        self.assertEqual(regions, ["us-west4", "northamerica-northeast1"])

    def test_a_wall_clock_budgeted_campaign_keeps_its_machine_type(self):
        # The ladder runs on one machine type: the first submission chooses it by quota (C4 in
        # Montreal here), and a later submission keeps it although us-west4 has room again.
        self.config.gcp.ranking = list(testing.RANKING)
        plan = self.plan(testing.LADDER_CAMPAIGN + "\n" + testing.RANKED_FAMILIES)
        self.assertEqual(len(plan.candidates("c1m3")), 3)
        cloud = self.two_regions(west=(64, 64), northeast=(64, 0))
        backend = gcp_batch.GcpBatch()
        first = backend.submit(plan, self.config, cloud=cloud, say=lambda s: None)
        self.assertEqual(first[0].record["placement"]["shape"], "c4-highcpu-16")
        cloud.jobs[first[0].record["job"]["name"]]["status"]["state"] = "FAILED"
        cloud.quotas["us-west4"] = (64, 0)
        cloud.quotas["northamerica-northeast1"] = (64, 64)
        again = backend.submit(
            planning.Plan(plan.dir), self.config, cloud=cloud, say=lambda s: None
        )
        record = again[0].record
        self.assertEqual(record["placement"]["shape"], "c4-highcpu-16")
        # C4 in us-west4 (rank 3) has room, but no template there: it waits in Montreal.
        self.assertEqual(record["placement"]["choice"]["pair"], "c4/northamerica-northeast1")
        self.assertIn("waits", record["placement"]["choice"]["why"])
        cloud.templates["yapnr-c4-highcpu-16-spot-us-west4"] = "c4-highcpu-16"
        cloud.jobs[record["job"]["name"]]["status"]["state"] = "FAILED"
        third = backend.submit(
            planning.Plan(plan.dir), self.config, cloud=cloud, say=lambda s: None
        )
        self.assertEqual(third[0].record["placement"]["choice"]["pair"], "c4/us-west4")
        # Pinned to us-west4, the C4D pair there is not the campaign's machine type.
        cloud.jobs[third[0].record["job"]["name"]]["status"]["state"] = "FAILED"
        fourth = backend.submit(
            planning.Plan(plan.dir), self.config, cloud=cloud, say=lambda s: None, region="us-west4"
        )
        choice = fourth[0].record["placement"]["choice"]
        self.assertEqual((choice["pair"], choice["why"]), ("c4/us-west4", "pinned to us-west4"))

    def test_a_class_with_one_candidate_reads_no_quota(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        cloud = FakeCloud()
        done, _ = self.submit(plan, cloud)
        self.assertEqual(done[0].record["placement"]["choice"]["why"], "the only candidate")
        self.assertEqual(self.calls(cloud, "compute"), [])

    def test_doctor_checks_the_templates_of_every_ranked_pair(self):
        self.config.gcp.ranking = list(testing.RANKING)
        cloud = self.two_regions(west=(64, 16), northeast=(64, 0))
        checks = {c["check"]: c for c in gcp_batch.doctor(self.config, cloud)}
        west = checks["templates for ranked c4d/us-west4"]
        self.assertTrue(west["ok"])
        self.assertEqual(west["detail"], "c4d-highcpu-16, c4d-highcpu-8, c4d-standard-16")
        northeast = checks["templates for ranked c4/northamerica-northeast1"]
        self.assertEqual(
            (northeast["ok"], northeast["detail"]), (True, "c4-highcpu-16, c4-highcpu-8")
        )
        # The ranked C4 pair in us-west4 has no template there: a finding, with what to do.
        self.assertFalse(checks["templates for ranked c4/us-west4"]["ok"])
        self.assertIn(
            "region_template_shapes", checks["templates for ranked c4/us-west4"]["detail"]
        )
        self.assertEqual(checks["quota in us-west4"]["detail"], "preemptible CPUs: 16 of 64 in use")
        self.assertTrue(checks["quota in northamerica-northeast1"]["ok"])

    def test_unknown_region_in_the_table_is_priced_conservatively(self):
        table = cost.PriceTable.load()
        vcpu, _, source = table.rate("c4d", "europe-west99")
        self.assertEqual(vcpu, table.rate("c4d", "us-central1")[0])
        self.assertIn("fallback", source)


if __name__ == "__main__":
    unittest.main()
