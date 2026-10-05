"""Planning: campaign expansion, ids, resource classes, placement and the plan directory."""

from __future__ import annotations

import dataclasses
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

    def test_live_absent_is_strictly_byte_identical_and_is_recorded_when_set(self):
        # No [live] section: the key itself is left out of meta, not set to a default-off
        # value, so a campaign.json from before this feature existed stays byte-identical
        # (review finding 5).
        plan = self.plan(testing.LADDER_CAMPAIGN)
        self.assertNotIn("live", plan.meta)
        with_live = testing.LADDER_CAMPAIGN + "\n[live]\nenabled = true\ninterval_s = 30\n"
        plan = self.plan(with_live, name="live.toml", out=self.tmp / "live-plan")
        self.assertEqual(plan.meta["live"], {"enabled": True, "interval_s": 30, "mode": "full"})
        # [live] present but enabled left at its default (false): still recorded, since the
        # section itself was given.
        off_but_present = testing.LADDER_CAMPAIGN + "\n[live]\ninterval_s = 60\n"
        plan = self.plan(off_but_present, name="live-off.toml", out=self.tmp / "live-off-plan")
        self.assertEqual(plan.meta["live"], {"enabled": False, "interval_s": 60, "mode": "full"})

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

    def ranked_smoke(self, **kw):
        self.config.gcp.ranking = list(testing.RANKING)
        text = testing.SMOKE_CAMPAIGN.replace("[config]", testing.RANKED_FAMILIES + "\n[config]")
        return self.plan(text, **kw)

    def test_ranked_pairs_are_the_candidates_in_order(self):
        plan = self.ranked_smoke()
        name = plan.classes[0].name
        self.assertEqual(
            [p.pair for p in plan.candidates(name)],
            ["c4d/us-west4", "c4/northamerica-northeast1", "c4/us-west4"],
        )
        self.assertEqual(plan.placement(name).pair, "c4d/us-west4")  # what the estimate prices
        rows = plan.meta["candidates"][name]
        self.assertEqual(
            [r["shape"] for r in rows], ["c4d-highcpu-16", "c4-highcpu-16", "c4-highcpu-16"]
        )
        # Each candidate carries its prices and the class's estimate there.
        self.assertTrue(all("fallback" not in r["price_source"] for r in rows), rows)
        montreal, vegas = rows[1], rows[2]
        self.assertLess(montreal["vm_hour_usd"], vegas["vm_hour_usd"] / 2)
        self.assertLess(montreal["expected_usd"], vegas["expected_usd"])
        self.assertLessEqual(rows[0]["expected_usd"], rows[0]["ceiling_usd"])
        self.assertAlmostEqual(plan.meta["estimate"]["expected_usd"], rows[0]["expected_usd"], 3)

    def test_ranked_families_are_allowed_by_default(self):
        # A campaign that names no families may use every family the owner ranked.
        self.assertEqual(planning.default_families(testing.RANKING), ["c4d", "c4", "c3d", "t2d"])
        self.config.gcp.ranking = list(testing.RANKING)
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        self.assertEqual(
            [p.pair for p in plan.candidates(plan.classes[0].name)],
            ["c4d/us-west4", "c4/northamerica-northeast1", "c4/us-west4"],
        )
        # A campaign's own families still restrict them.
        plan = self.plan(
            testing.SMOKE_CAMPAIGN.replace(
                "[config]", '[placement]\nfamilies = ["c4d"]\n\n[config]'
            ),
            out=self.tmp / "c4d",
        )
        self.assertEqual([p.pair for p in plan.candidates(plan.classes[0].name)], ["c4d/us-west4"])

    def test_without_a_ranking_a_class_has_one_candidate(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        name = plan.classes[0].name
        self.assertEqual(len(plan.meta["candidates"][name]), 1)
        self.assertEqual(plan.candidates(name), [plan.placement(name)])

    def test_wall_clock_budgeted_candidates_keep_one_machine_type(self):
        # Two classes; 8 tasks of 3.4 GB fill a C4 highcpu VM but not a C4D one (30 GB), so C4D
        # would run the classes on two machine types and is dropped from both.
        def task(name, memory_gb):
            return {
                "id": name,
                "command": ["${PYTHON}", "-c", "pass"],
                "record": "out/%s.json" % name,
                "resources": {"cpus": 1, "memory_gb": memory_gb, "disk_gb": 1, "max_wall_s": 600},
            }

        stage = [task("a%d" % i, 1) for i in range(8)] + [task("b%d" % i, 3.4) for i in range(8)]
        (self.tmp / "stage.jsonl").write_text("".join(json.dumps(t) + "\n" for t in stage))
        text = (
            'schema = "yapnr-campaign-v1"\nkind = "mc-eval"\nname = "wcb"\nsource = "none"\n'
            'image = "edge"\n\n%s\n[config]\nstage_plan = "stage.jsonl"\n' % testing.RANKED_FAMILIES
        )
        self.config.gcp.ranking = [
            ("c4", "northamerica-northeast1"),
            ("c4d", "us-west4"),
            ("c4", "us-west4"),
        ]
        plan = self.plan(text)
        self.assertEqual(plan.meta["determinism"], "wall_clock_budgeted")
        for cls in plan.classes:
            self.assertEqual(
                [p.pair for p in plan.candidates(cls.name)],
                ["c4/northamerica-northeast1", "c4/us-west4"],
            )
            self.assertEqual({p.shape for p in plan.candidates(cls.name)}, {"c4-highcpu-16"})
        # With C4D preferred, the classes cannot share a machine type: as before, an error.
        self.config.gcp.ranking = list(testing.RANKING)
        with self.assertRaises(planning.PlanError):
            self.plan(text, out=self.tmp / "c4d")
        # One class: every ranked family is a candidate.
        plan = self.plan(
            testing.LADDER_CAMPAIGN + "\n" + testing.RANKED_FAMILIES, out=self.tmp / "l"
        )
        self.assertEqual(
            [p.pair for p in plan.candidates("c1m3")],
            ["c4d/us-west4", "c4/northamerica-northeast1", "c4/us-west4"],
        )

    def test_a_region_pinned_at_plan_keeps_only_its_candidates(self):
        plan = self.ranked_smoke(region="northamerica-northeast1")
        name = plan.classes[0].name
        self.assertEqual([p.pair for p in plan.candidates(name)], ["c4/northamerica-northeast1"])

    def test_a_chosen_placement_replaces_the_planned_one(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        meta = dict(plan.meta)
        meta.pop("candidates")  # a plan made before candidates existed
        (plan.dir / "campaign.json").write_text(json.dumps(meta))
        old = planning.Plan(plan.dir)
        name = old.classes[0].name
        self.assertEqual(old.candidates(name), [old.placement(name)])
        moved = dataclasses.replace(old.placement(name), region="us-west4")
        old.choose(name, moved)
        self.assertEqual(old.placement(name), moved)
        self.assertEqual(old.placements()[name], moved)
        self.assertNotEqual(planning.Plan(plan.dir).placement(name), moved)  # not written

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
