"""Planning: campaign expansion, ids, resource classes, placement and the plan directory."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import plan as planning
from yapnr.exp import spec, testing


class PlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp)
        self.repo = testing.fixture_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, text, backend="gcp-batch", name="campaign.toml", **kw):
        path = testing.write_campaign(self.tmp / name, text)
        kw.setdefault("image_digest", testing.DIGEST)
        kw.setdefault("offline", True)
        kw.setdefault("repo", self.repo)
        kw.setdefault("today", testing.TODAY)
        return planning.make_plan(path, backend, self.config, **kw)

    def test_ladder_expands_the_matrix_with_stable_ids(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        self.assertEqual(
            [t["id"] for t in plan.tasks],
            [
                "ladder/01-connector-led-2/s0",
                "ladder/01-connector-led-2/s1",
                "ladder/02-resistor-led-3/s0",
                "ladder/02-resistor-led-3/s1",
            ],
        )
        self.assertEqual(plan.check(), [])
        task = plan.tasks[0]
        self.assertEqual(task["command"][:2], ["${PYTHON}", "src/hardware/pnr/regression/run.py"])
        self.assertIn("--case", task["command"])
        self.assertEqual(task["inputs"][0]["dest"], "src")
        self.assertEqual(task["inputs"][0]["bundle"], plan.meta["source"]["bundle"])
        self.assertEqual(plan.meta["source"]["commit"], testing.git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(len(plan.classes), 1)
        self.assertEqual(plan.meta["image"]["ref"], "ghcr.io/studio-fug/yapnr@" + testing.DIGEST)

    def test_campaign_id_is_deterministic_and_depends_on_the_backend(self):
        a = self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "a")
        b = self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "b")
        c = self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "c", backend="local")
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.id, c.id)
        self.assertRegex(a.id, r"^20261002-ladder-[0-9a-f]{6}$")
        self.assertEqual(a.meta["source"]["bundle"], b.meta["source"]["bundle"])

    def test_existing_plan_directory_needs_replace(self):
        self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "a")
        with self.assertRaises(planning.PlanError):
            self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "a")
        self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "a", replace=True)

    def test_dirty_checkout_is_refused_unless_allowed(self):
        (self.repo / "hardware" / "tools" / "tool.py").write_text("# changed\n")
        with self.assertRaises(planning.PlanError):
            self.plan(testing.LADDER_CAMPAIGN)
        plan = self.plan(testing.LADDER_CAMPAIGN, allow_dirty=True)
        self.assertTrue(plan.meta["source"]["dirty"])

    def test_cloud_backends_need_a_pinned_image_offline(self):
        with self.assertRaises(planning.PlanError):
            self.plan(testing.LADDER_CAMPAIGN, image_digest=None)
        local = self.plan(testing.LADDER_CAMPAIGN, image_digest=None, backend="local")
        self.assertEqual(local.meta["image"]["ref"], "ghcr.io/studio-fug/yapnr:edge")

    def test_private_campaign_gets_opaque_ids_and_no_readable_spec(self):
        text = testing.LADDER_CAMPAIGN.replace('name = "ladder-small"', 'visibility = "private"')
        plan = self.plan(text)
        self.assertRegex(plan.id, r"^20261002-p-[0-9a-f]{6}$")
        for task in plan.tasks:
            self.assertRegex(task["id"], r"^p/[0-9a-f]{16}$")
        self.assertIsNone(plan.meta["spec"])
        self.assertTrue((plan.dir / "private-ids.json").is_file())
        self.assertNotIn("01-connector", (plan.dir / "campaign.json").read_text())
        self.assertTrue(str(plan.dir).startswith(str(self.tmp / "private")))

    def test_resource_classes_group_by_resources(self):
        tasks = [
            {
                "image": "i",
                "resources": {"cpus": 1, "memory_gb": 3, "disk_gb": 4, "max_wall_s": 60},
            },
            {
                "image": "i",
                "resources": {"cpus": 4, "memory_gb": 8, "disk_gb": 4, "max_wall_s": 90},
            },
            {
                "image": "i",
                "resources": {"cpus": 1, "memory_gb": 3, "disk_gb": 4, "max_wall_s": 120},
            },
        ]
        classes = planning.resource_classes(tasks, [1.0, 2.0, 3.0])
        self.assertEqual(
            [(c.name, c.lines, c.max_wall_s) for c in classes],
            [("c1m3", [0, 2], 120), ("c4m8", [1], 90)],
        )

    def test_wall_clock_budgeted_ladder_prefers_the_first_family(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        placement = plan.placement("c1m3")
        self.assertEqual(placement.family, "c4d")
        self.assertEqual(placement.shape, "c4d-highcpu-16")
        self.assertEqual(placement.region, "us-west4")  # the cheaper of the two enabled regions
        self.assertEqual(placement.cpu_milli, 2000)  # one task per physical core
        self.assertEqual(placement.tasks_per_vm, 8)
        self.assertTrue(placement.template)

    def test_seeded_campaign_takes_the_cheapest_per_result(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        self.assertEqual(plan.placement(plan.classes[0].name).family, "c3d")

    def test_ranking_in_the_owner_config_wins(self):
        self.config = testing.load_config(self.tmp, gcp=True, extra="")
        self.config.gcp.ranking = [("t2d", "northamerica-northeast1"), ("c4d", "us-west4")]
        plan = self.plan(
            testing.SMOKE_CAMPAIGN.replace(
                "[config]", '[placement]\nfamilies = ["t2d", "c4d"]\n\n[config]'
            )
        )
        placement = plan.placement(plan.classes[0].name)
        self.assertEqual((placement.family, placement.region), ("t2d", "northamerica-northeast1"))

    def test_shape_and_region_overrides(self):
        plan = self.plan(
            testing.LADDER_CAMPAIGN, shape="c3d-highcpu-8", region="northamerica-northeast1"
        )
        placement = plan.placement("c1m3")
        self.assertEqual(
            (placement.shape, placement.region), ("c3d-highcpu-8", "northamerica-northeast1")
        )
        with self.assertRaises(planning.PlanError):
            self.plan(testing.LADDER_CAMPAIGN, out=self.tmp / "x", region="europe-west4")

    def test_invalid_campaign_lists_every_finding(self):
        text = testing.LADDER_CAMPAIGN.replace("seed = [0, 1]", 'seed = [0, -1]\ncolour = ["red"]')
        with self.assertRaises(spec.SpecError) as ctx:
            self.plan(text)
        self.assertIn("matrix.seed -1", str(ctx.exception))
        self.assertIn("matrix.colour is not an axis", str(ctx.exception))

    def test_plan_check_detects_tampering(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        lines = (plan.dir / "tasks.jsonl").read_text().replace("--seed", "--seeds")
        (plan.dir / "tasks.jsonl").write_text(lines)
        errors = planning.Plan(plan.dir).check()
        self.assertTrue(any("does not match its hash" in e for e in errors), errors)

    def test_estimate_and_caps_are_recorded(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        est = plan.meta["estimate"]
        self.assertGreater(est["expected_usd"], 0)
        self.assertGreaterEqual(est["ceiling_usd"], est["expected_usd"])
        self.assertEqual(plan.meta["caps"]["refusals"], [])
        self.assertIn("committed snapshot", plan.meta["prices"]["table"])
        json.dumps(plan.meta)  # serializable


if __name__ == "__main__":
    unittest.main()
