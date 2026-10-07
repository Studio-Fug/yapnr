"""Duration prediction, the packer (LPT, bundles, stragglers, the quota cap), packed Batch
submissions, VM-time cost from audit logs and the spend reconciliation; offline (FakeCloud)."""

from __future__ import annotations

import datetime as _dt
import json
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import packing
from yapnr.exp import plan as planning
from yapnr.exp import spend, testing
from yapnr.exp.backends import gcp_batch
from yapnr.exp.cloud import FakeCloud, Result

UTC = _dt.timezone.utc
TABLE = {
    "schema": "yapnr-price-table-v1",
    "accessed": "2026-10-02",
    "disks": {"hyperdisk-balanced": {"gib_month": 0.08}, "pd-balanced": {"gib_month": 0.1}},
    "overheads": {"boot_disk_gib": 30, "preemption_rework": 0.08, "vm_start_s": 120},
    "families": {
        "c4d": {
            "arch": "x86_64",
            "threads_per_core": 2,
            "batch": True,
            "boot_disk": "hyperdisk-balanced",
            "memory_gb_per_vcpu": {"highcpu": 1.875},
            "vcpus": [2, 4, 8, 16],
            "speed_vs_reference": 0.8,
            "spot": {"r1": [0.01, 0.001]},
        }
    },
}


def record(case, seed, wall, machine="c4d-highcpu-16", verdict="pass", **kw):
    return dict(
        {
            "campaign": "c1",
            "task": "ladder/%s/s%d" % (case, seed),
            "submission": 1,
            "attempt": "s1r0",
            "kind": "ladder-cell",
            "labels": {"case": case, "config": "a", "seed": str(seed)},
            "machine": {"machine_type": machine} if machine else {},
            "backend": "gcp-batch" if machine else "local",
            "verdict": verdict,
            "wall_s": wall,
        },
        **kw,
    )


def task(case, seed=0, config="a", max_wall=900):
    return {
        "kind": "ladder-cell",
        "labels": {"case": case, "config": config, "seed": str(seed)},
        "resources": {"max_wall_s": max_wall},
    }


class PredictorTest(unittest.TestCase):
    def test_history_beats_the_calibration_and_the_kind_default(self):
        history = packing.ingest(
            [record("big", 0, 400.0), record("big", 1, 600.0), record("big", 2, 500.0)]
        )
        predictor = packing.Predictor(history, speed=lambda family: 0.8)
        # The median wall time scaled to the reference core by the family's speed.
        self.assertEqual(predictor.predict(task("big", 7), 900.0, 50.0), (400.0, "history"))
        # Another configuration of the same case: the case's median.
        self.assertEqual(
            predictor.predict(task("big", config="b"), 900.0), (400.0, "history (case)")
        )
        self.assertEqual(predictor.predict(task("new"), 900.0, 50.0), (50.0, "calibration"))
        self.assertEqual(predictor.predict(task("new"), 120.0), (120.0, "kind default"))
        # Never above the task's own limit.
        self.assertEqual(predictor.predict(task("big", max_wall=100), 900.0)[0], 100.0)
        self.assertEqual(
            predictor.sources,
            {"history": 2, "history (case)": 1, "calibration": 1, "kind default": 1},
        )

    def test_ingest_counts_each_attempt_once_and_skips_errors(self):
        rows = [
            record("a", 0, 10.0),
            record("a", 0, 10.0),  # the same attempt again
            record("a", 1, 30.0, verdict="error"),
            record("a", 2, 900.0, verdict="timeout", timed_out=True),
            record("a", 3, 12.0, machine=None),  # the development Mac
        ]
        data = packing.ingest(rows)
        cell = data["cells"]["ladder-cell"][packing.cell_key(task("a"))]
        self.assertEqual(cell["c4d"], {"wall_s": [10.0, 900.0], "timed_out": 1})
        self.assertEqual(cell["reference"]["wall_s"], [12.0])
        again = packing.ingest([record("a", 0, 10.0), record("a", 4, 11.0)], data)
        cell = again["cells"]["ladder-cell"][packing.cell_key(task("a"))]
        self.assertEqual(cell["c4d"]["wall_s"], [10.0, 900.0, 11.0])
        # An unknown family has no speed: its samples are not used.
        predictor = packing.Predictor(again, speed=lambda family: None)
        self.assertEqual(predictor.predict(task("a"), 99.0)[0], 12.0)

    def test_records_from_concatenated_text(self):
        text = json.dumps({"a": 1}, indent=2) + "\n" + json.dumps({"b": 2}) + "\n"
        self.assertEqual(packing.records_from_text(text), [{"a": 1}, {"b": 2}])


class PackerTest(unittest.TestCase):
    def test_simulate_bills_boot_busy_and_idle_time_of_used_vms(self):
        vm_s, makespan = packing.simulate([100, 100, 100], 2, 2, boot_s=10, idle_s=5)
        # Three tasks on the first VM's two slots and the second VM's first slot.
        self.assertEqual((vm_s, makespan), (2 * (10 + 100 + 5), 110))
        vm_s, _ = packing.simulate([100], 4, 8, boot_s=10, idle_s=5)
        self.assertEqual(vm_s, 115)  # VMs without a task never start

    def test_longest_first_and_vm_count_from_work(self):
        seconds = {i: s for i, s in enumerate([60, 600, 300, 300, 600, 120])}
        packed = packing.pack(list(seconds), seconds, per_vm=2, max_vms=8, bundle=False)
        (job,) = packed.jobs
        self.assertEqual(job.groups, [[1], [4], [2], [3], [5], [0]])  # LPT, ties by line
        # 1980 s of work in units of at most 610 s: two VMs of two slots keep it near 610 s.
        self.assertEqual((job.vms, job.parallelism), (2, 4))
        self.assertLessEqual(packed.makespan_s, 1.25 * (610 + 610 + packing.VM_BOOT_S))

    def test_the_quota_caps_the_vms(self):
        seconds = {i: 600.0 for i in range(64)}
        packed = packing.pack(list(seconds), seconds, per_vm=8, max_vms=3, bundle=False)
        self.assertEqual((packed.jobs[0].vms, packed.jobs[0].parallelism), (3, 24))
        uncapped = packing.pack(list(seconds), seconds, per_vm=8, max_vms=100, bundle=False)
        self.assertEqual(uncapped.jobs[0].vms, 8)

    def test_short_cells_are_bundled_within_the_longest_task(self):
        seconds = {0: 600.0, **{i: 30.0 for i in range(1, 41)}}
        packed = packing.pack(list(seconds), seconds, per_vm=8, max_vms=4)
        (job,) = packed.jobs
        bundles = [g for g in job.groups if len(g) > 1]
        self.assertEqual(sorted(line for g in job.groups for line in g), list(range(41)))
        self.assertTrue(bundles)
        self.assertTrue(all(sum(seconds[i] for i in g) <= 600.0 for g in bundles))
        self.assertTrue(all(len(g) <= packing.MAX_BUNDLE for g in job.groups))
        self.assertEqual(job.groups[0], [0])
        self.assertIn("bundled", " ".join(packed.notes))
        # The member cap (the wall-time limit of a Batch task) and bundle=False.
        capped = packing.pack(list(seconds), seconds, per_vm=8, max_vms=4, max_members=3)
        self.assertTrue(all(len(g) <= 3 for g in capped.jobs[0].groups))
        single = packing.pack(list(seconds), seconds, per_vm=8, max_vms=4, bundle=False)
        self.assertEqual(len(single.jobs[0].groups), 41)

    def test_a_straggler_gets_its_own_job_only_where_that_saves(self):
        seconds = {0: 14400.0, **{i: 300.0 for i in range(1, 101)}}
        joint = packing.pack(list(seconds), seconds, per_vm=8, max_vms=4, bundle=False)
        # On the same shape the straggler's VM carries the short tasks too: one job.
        self.assertEqual(len(joint.jobs), 1)
        self.assertIn("stay in the job", " ".join(joint.notes))
        split = packing.pack(
            list(seconds), seconds, per_vm=8, max_vms=4, bundle=False, small=(1, 0.125)
        )
        self.assertEqual([j.straggler for j in split.jobs], [True, False])
        self.assertEqual(split.jobs[0].groups, [[0]])
        self.assertEqual(split.jobs[0].per_vm, 1)
        self.assertLess(split.priced_vm_hours, joint.priced_vm_hours)
        self.assertEqual(sorted(split.jobs[1].lines), list(range(1, 101)))

    def test_indices_round_trip(self):
        groups = [[4], [0, 3, 1], [2]]
        self.assertEqual(packing.parse_indices(packing.indices_text(groups)), groups)
        self.assertEqual(packing.parse_indices("1\n2\n"), [[1], [2]])


class PackedSubmitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp)
        self.repo = testing.fixture_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, durations=None):
        path = testing.write_campaign(self.tmp / "campaign.toml", testing.LADDER_CAMPAIGN)
        return planning.make_plan(
            path,
            "gcp-batch",
            self.config,
            out=self.tmp / "plan",
            repo=self.repo,
            offline=True,
            image_digest=testing.DIGEST,
            today=testing.TODAY,
            durations=durations,
            replace=True,
        )

    def test_the_plan_predicts_from_history_and_records_its_packing(self):
        rows = []
        for task in self.plan().tasks:
            if task["labels"]["case"] == "01-connector-led-2":
                row = record("01-connector-led-2", int(task["labels"]["seed"]), 20.0)
                row["labels"] = task["labels"]
                rows.append(row)
        path = self.tmp / "durations.json"
        packing.write(packing.ingest(rows), path)
        plan = self.plan(str(path))
        self.assertEqual(plan.meta["wrapper"]["version"], 2)
        # The measured case from history (both seeds), the other case from the kind's table.
        self.assertEqual(plan.meta["prediction"]["sources"], {"history": 2, "kind default": 2})
        cls = plan.classes[0]
        measured = [
            s
            for line, s in zip(cls.lines, cls.reference_s)
            if plan.tasks[line]["labels"]["case"] == "01-connector-led-2"
        ]
        self.assertEqual(measured, [20.0 * 0.79] * 2)  # c4d's table speed
        packed = plan.meta["packing"][cls.name]
        self.assertEqual(sum(job["cells"] for job in packed["jobs"]), 4)
        est = plan.meta["estimate"]["classes"][0]
        self.assertGreater(est["vm_hours"], 0)
        self.assertEqual(est["vms"], 1)

    def test_a_dry_run_submit_writes_bundled_indices_and_the_packed_job(self):
        plan = self.plan()
        cloud = FakeCloud(impersonate="x")
        out = gcp_batch.GcpBatch().submit(
            plan, self.config, dry_run=True, cloud=cloud, yes=True, say=lambda _: None
        )
        self.assertEqual(len(out), 1)
        (sub,) = out
        key = "gs://example-yapnr-runs/campaigns/%s/submissions/%d.indices" % (
            plan.id,
            sub.number,
        )
        groups = packing.parse_indices(cloud.objects[key].decode())
        self.assertEqual(sorted(line for g in groups for line in g), [0, 1, 2, 3])
        # All four cells are short (the kind's 8.7 s and 8 s): one bundle, one Batch task.
        self.assertEqual(len(groups), 1)
        job = json.loads(
            (plan.dir / "submissions" / str(sub.number) / ("%s.job.json" % sub.cls)).read_text()
        )
        group = job["taskGroups"][0]
        self.assertEqual((group["taskCount"], group["parallelism"]), ("1", "1"))
        # The runnable's timeout covers the bundle's cells one after the other.
        wall = plan.classes[0].max_wall_s
        timeout = int(group["taskSpec"]["runnables"][0]["timeout"].rstrip("s"))
        self.assertEqual(timeout, 4 * wall + gcp_batch.UPLOAD_GRACE_S)
        self.assertEqual(sub.record["packing"]["batch_tasks"], 1)
        self.assertEqual(sub.record["packing"]["bundled_cells"], 4)

    def test_a_packed_job_claims_its_lines_in_order(self):
        from yapnr.exp import packing as pk

        plan = self.plan()
        job = pk.Job(groups=[[2], [0, 1], [3]], seconds=[], vms=1, per_vm=8, parallelism=3)
        files = gcp_batch.GcpBatch().render(
            plan, self.config, plan.classes[0], 1, job.lines, 1, job
        )
        rendered = json.loads(next(iter(files.values())))
        group = rendered["taskGroups"][0]
        self.assertEqual((group["taskCount"], group["parallelism"]), ("3", "3"))
        env = group["taskSpec"]["environment"]["variables"]
        self.assertEqual(env["YAPNR_CLAIM_BUCKET"], "example-yapnr-runs")

    def test_an_old_wrapper_keeps_one_cell_per_task(self):
        plan = self.plan()
        plan.meta["wrapper"]["version"] = 1
        jobs = gcp_batch.GcpBatch().layout(plan, self.config, plan.classes[0], [0, 1, 2, 3])
        self.assertEqual([len(g) for job in jobs for g in job.groups], [1, 1, 1, 1])


def audit(name, method, when, last=True, severity="NOTICE", zone="us-west4-a"):
    return {
        "timestamp": when,
        "severity": severity,
        "operation": {"last": last},
        "resource": {"labels": {"zone": zone, "instance_id": "1"}},
        "protoPayload": {
            "methodName": method,
            "resourceName": "projects/1/zones/%s/instances/%s" % (zone, name),
        },
    }


UID = "yapnr-20261007-lad-aaaa0000-1111-22220"
VM1 = UID + "-group0-0-abcd"
VM2 = UID + "-group0-0-efgh"
INSERT = "v1.compute.instances.insert"
DELETE = "v1.compute.instances.delete"


class SpendTest(unittest.TestCase):
    def entries(self):
        return [
            audit(VM1, INSERT, "2026-10-07T10:00:00.5Z", last=False),  # the first half
            audit(VM1, INSERT, "2026-10-07T10:00:30Z", severity="ERROR"),  # quota: never ran
            audit(VM1, INSERT, "2026-10-07T10:01:00Z"),
            audit(VM2, INSERT, "2026-10-07T10:02:00Z"),
            audit(VM1, DELETE, "2026-10-07T11:01:00Z"),
            audit(VM2, "compute.instances.preempted", "2026-10-07T10:32:00Z"),
            audit(VM2, DELETE, "2026-10-07T10:33:00Z"),
            audit(VM1, INSERT, "2026-10-07T12:00:00Z"),  # the name again: still running
        ]

    def test_vm_lifetimes_from_the_audit_log(self):
        found = spend.vms(self.entries())
        self.assertEqual(len(found), 3)
        self.assertEqual([vm.job_uid for vm in found], [UID] * 3)
        now = _dt.datetime(2026, 10, 7, 12, 30, tzinfo=UTC)
        self.assertEqual([round(vm.hours(now), 3) for vm in found], [1.0, 0.5, 0.5])
        self.assertEqual([vm.preempted for vm in found], [False, True, False])
        self.assertEqual(found[0].region, "us-west4")
        self.assertIn('"instances/%s-group"' % UID, spend.audit_filter([UID]))

    def test_campaign_cost_prices_vm_time_and_measures_utilisation(self):
        cloud = FakeCloud(impersonate="x")
        entries = self.entries()[:-1]
        tasks = [
            {
                "status": {
                    "statusEvents": [
                        {"taskState": "RUNNING", "type": "RUNNING", "eventTime": t0},
                        {"taskState": "RUNNING", "type": "RUNNABLE_EVENT", "eventTime": t1},
                        {"taskState": "SUCCEEDED", "type": "SUCCEEDED", "eventTime": t1},
                    ]
                }
            }
            for t0, t1 in (("2026-10-07T10:01:30Z", "2026-10-07T10:31:30Z"),) * 4
        ]

        def handler(args):
            if args[:2] == ["logging", "read"]:
                return Result(0, json.dumps(entries), "")
            if args[:3] == ["batch", "tasks", "list"]:
                return Result(0, json.dumps(tasks), "")
            return None

        cloud.handlers.append(handler)
        from yapnr.exp import cost

        table = cost.PriceTable(TABLE)
        records = [
            {
                "submission": 1,
                "tasks": 4,
                "job": {"id": "yapnr-x-s1", "uid": UID, "region": "r1"},
                "placement": {"shape": "c4d-highcpu-16", "region": "r1", "tasks_per_vm": 8},
                "packing": {"batch_tasks": 4, "predicted_vm_hours": 1.2},
            },
            {"submission": 2, "dry_run": True, "job": {"id": "y", "uid": "z", "region": "r1"}},
        ]
        now = _dt.datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        report = spend.campaign_cost(
            cloud, {"id": "c", "estimate": {"expected_usd": 0.5}}, records, table, now
        )
        price = 16 * 0.01 + 16 * 1.875 * 0.001 + 0.08 * 30 / 730.0
        self.assertEqual((report["vms"], report["vm_hours"]), (2, 1.5))
        self.assertAlmostEqual(report["usd"], round(1.5 * price, 4), places=4)
        self.assertEqual(report["task_hours"], 2.0)
        row = report["submissions"][0]
        self.assertAlmostEqual(row["slot_utilisation"], round(2.0 / (1.5 * 8), 3))
        self.assertEqual(row["preempted"], 1)
        self.assertAlmostEqual(report["makespan_h"], 1.0, places=3)

    def test_reconcile_finds_the_lag_and_the_factor(self):
        start = _dt.datetime(2026, 10, 1, tzinfo=UTC)

        def vm_usd(t):  # $1 of VM time per hour from day 1, 00:00
            return max(0.0, (t - start).total_seconds() / 3600.0)

        # The bill: 1.2 x VM time, 6 h late.
        points = [
            (start + _dt.timedelta(hours=h), 1.2 * vm_usd(start + _dt.timedelta(hours=h - 6)))
            for h in range(8, 40, 2)
        ]
        now = start + _dt.timedelta(hours=41)
        out = spend.reconcile(points, vm_usd, now)
        self.assertEqual((out["lag_h"], out["factor"], out["fitted"]), (6.0, 1.2, True))
        # Billed at 38 h covers VM time to 32 h; VM time ran 9 h more by 41 h.
        self.assertAlmostEqual(out["unbilled_usd"], 1.2 * 9, places=2)
        self.assertAlmostEqual(out["projected_usd"], 1.2 * 32 + 1.2 * 9, places=2)
        few = spend.reconcile(points[:1], vm_usd, now)
        self.assertEqual((few["lag_h"], few["factor"], few["fitted"]), (8.0, 1.15, False))

    def test_month_spend_from_fake_logs(self):
        from yapnr.exp import cost

        table = cost.PriceTable(TABLE)
        guard = [
            {
                "timestamp": "2026-10-07T09:00:00Z",
                "jsonPayload": {"guard": "budget", "ratio": 0.01},
            },
            {
                "timestamp": "2026-10-07T13:00:00Z",
                "jsonPayload": {"guard": "budget", "ratio": 0.02, "cost": 3.0, "budget": 150},
            },
            {"timestamp": "2026-09-30T13:00:00Z", "jsonPayload": {"guard": "budget", "ratio": 0.9}},
        ]
        now = _dt.datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
        out = spend.month_spend(
            None,
            table,
            {UID: ("c4d-highcpu-16", "r1")},
            budget=100.0,
            now=now,
            entries=(self.entries(), guard),
        )
        self.assertEqual(out["month"], "2026-10")
        self.assertEqual(out["readings"], 2)  # September's reading is not this month's
        self.assertEqual(out["billed_usd"], 3.0)  # the logged amount beats ratio x budget
        self.assertEqual(out["running_vms"], 1)
        self.assertEqual(out["vm_hours"], 3.5)
        self.assertEqual(out["unknown_jobs"], [])
        self.assertFalse(out["fitted"])
        readings = spend.readings(guard)
        self.assertEqual(readings[1].billed(100.0), 1.0)  # ratio x the budget given

    def test_job_shapes_from_job_listings(self):
        cloud = FakeCloud(impersonate="x")
        jobs = [
            {
                "uid": "u1",
                "status": {"taskGroups": {"g": {"instances": [{"machineType": "c4d-highcpu-16"}]}}},
            },
            {
                "uid": "u2",
                "allocationPolicy": {
                    "instances": [{"instanceTemplate": "yapnr-c4-highcpu-8-spot-r1"}]
                },
            },
            {
                "uid": "u3",
                "allocationPolicy": {"instances": [{"policy": {"machineType": "t2d-standard-8"}}]},
            },
        ]
        cloud.handlers.append(
            lambda args: (
                Result(0, json.dumps(jobs), "") if args[:3] == ["batch", "jobs", "list"] else None
            )
        )
        self.assertEqual(
            spend.job_shapes(cloud, ["r1"]),
            {
                "u1": ("c4d-highcpu-16", "r1"),
                "u2": ("c4-highcpu-8", "r1"),
                "u3": ("t2d-standard-8", "r1"),
            },
        )


if __name__ == "__main__":
    unittest.main()
