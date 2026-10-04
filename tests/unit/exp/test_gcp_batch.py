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

    def test_unknown_region_in_the_table_is_priced_conservatively(self):
        table = cost.PriceTable.load()
        vcpu, _, source = table.rate("c4d", "europe-west99")
        self.assertEqual(vcpu, table.rate("c4d", "us-central1")[0])
        self.assertIn("fallback", source)


if __name__ == "__main__":
    unittest.main()
