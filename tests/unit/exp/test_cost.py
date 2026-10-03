"""Shapes, placement, the estimate, the caps, Spot price refresh, ranking and calibration."""

from __future__ import annotations

import datetime as _dt
import unittest

from yapnr.exp import calibration, config, cost, prices

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
            "memory_gb_per_vcpu": {"highcpu": 1.875, "standard": 3.875},
            "vcpus": [2, 4, 8, 16, 32],
            "speed_vs_reference": 0.8,
            "spot": {"r1": [0.01, 0.001], "r2": [0.005, 0.0005]},
        },
        "t2d": {
            "arch": "x86_64",
            "threads_per_core": 1,
            "batch": True,
            "boot_disk": "pd-balanced",
            "memory_gb_per_vcpu": {"standard": 4.0},
            "vcpus": [1, 2, 4, 8, 16],
            "speed_vs_reference": 0.5,
            "memory_included": True,
            "spot": {"r1": [0.004, 0.0]},
        },
        "n4d": {
            "arch": "x86_64",
            "threads_per_core": 2,
            "batch": False,
            "boot_disk": "hyperdisk-balanced",
            "memory_gb_per_vcpu": {"highcpu": 2.0},
            "vcpus": [16],
            "speed_vs_reference": 0.8,
            "spot": {"r1": [0.001, 0.0001]},
        },
    },
}


class CostTest(unittest.TestCase):
    def setUp(self):
        self.table = cost.PriceTable(TABLE)
        self.none = cost.Calibration()

    def test_shape_packs_one_task_per_core(self):
        shape, vcpus, memory, milli, per_vm = cost.choose_shape(self.table, "c4d", 1, 3, 16)
        self.assertEqual((shape, vcpus, milli, per_vm), ("c4d-highcpu-16", 16, 2000, 8))
        shape, _, _, milli, per_vm = cost.choose_shape(self.table, "c4d", 1, 3, 16, packing="vcpu")
        # 16 tasks of 3 GB do not fit highcpu memory, so the standard type holds them.
        self.assertEqual((shape, milli, per_vm), ("c4d-standard-16", 1000, 16))
        shape, _, _, _, per_vm = cost.choose_shape(self.table, "c4d", 1, 6, 16)
        self.assertEqual((shape, per_vm), ("c4d-standard-16", 8))

    def test_batch_unsupported_families_are_never_placed(self):
        with self.assertRaises(cost.CostError):
            cost.place(self.table, self.none, cpus=1, memory_gb=1, families=["n4d"], regions=["r1"])

    def test_cost_and_first_family_preferences(self):
        # Per result: t2d in r1 0.064 $/VM-h / 16 tasks / 0.5 = 0.0080; c4d in r2 0.0950 / 8 / 0.8
        # = 0.0148; c4d in r1 0.190 / 8 / 0.8 = 0.0297.
        cheapest = cost.place(
            self.table,
            self.none,
            cpus=1,
            memory_gb=2,
            families=["c4d", "t2d"],
            regions=["r1", "r2"],
        )
        self.assertEqual((cheapest.family, cheapest.region), ("t2d", "r1"))
        first = cost.place(
            self.table,
            self.none,
            cpus=1,
            memory_gb=2,
            families=["c4d", "t2d"],
            regions=["r1", "r2"],
            prefer="first-family",
        )
        self.assertEqual((first.family, first.region), ("c4d", "r2"))
        ranked = cost.place(
            self.table,
            self.none,
            cpus=1,
            memory_gb=2,
            families=["t2d", "c4d"],
            regions=["r1", "r2"],
            ranking=[("c4d", "r1"), ("t2d", "r1")],
        )
        self.assertEqual((ranked.family, ranked.region), ("c4d", "r1"))

    def test_calibrated_speed_replaces_the_estimate(self):
        cal = cost.Calibration(
            {"schema": "yapnr-calibration-v1", "speed": {"c4d-highcpu-16": 1.25}}
        )
        p = cost.place(self.table, cal, cpus=1, memory_gb=2, families=["c4d"], regions=["r1"])
        self.assertEqual((p.speed, p.speed_source), (1.25, "calibration"))

    def test_estimate_numbers(self):
        p = cost.place(self.table, self.none, cpus=1, memory_gb=2, families=["c4d"], regions=["r1"])
        est = cost.estimate_gcp(
            self.table,
            [("c1m2", p, [3600.0] * 8, [7200.0] * 8, 8)],
            max_retries=3,
            max_parallel_vcpus=64,
            max_campaign_hours=12,
        )
        vm_hour = 16 * 0.01 + 30 * 0.001 + 0.08 * 30 / 730.0
        task_hours = 8 * 1.0 / 0.8 * 1.08
        expected = (task_hours / 8 + 1 * 120 / 3600.0) * vm_hour
        self.assertAlmostEqual(est.expected_usd, expected, places=6)
        worst = (8 * 2.0 * 4 / 8 + 120 / 3600.0) * vm_hour
        quota = 64 / 16.0 * 12 * vm_hour
        self.assertAlmostEqual(est.ceiling_usd, min(worst, quota), places=6)

    def test_caps(self):
        limits = config.Limits(max_tasks=10, confirm_usd=1, refuse_usd=5, hard_refuse_usd=20)
        est = cost.Estimate(backend="gcp-batch", expected_usd=2, ceiling_usd=8)
        check = cost.check_caps(est, limits, tasks=11, max_wall_s=99999)
        self.assertEqual(len(check.refusals), 3)
        self.assertTrue(check.confirm)
        self.assertEqual(
            cost.check_caps(est, limits, tasks=1, max_wall_s=60, max_usd=10).refusals, []
        )
        self.assertEqual(
            len(cost.check_caps(est, limits, tasks=1, max_wall_s=60, max_usd=30).refusals), 1
        )

    def test_committed_table_is_dated_and_sourced(self):
        table = cost.PriceTable.load()
        self.assertEqual(table.accessed, "2026-10-02")
        self.assertTrue(all(s["url"].startswith("https://") for s in table.data["sources"]))
        for name, family in table.families.items():
            self.assertIn("us-central1", family["spot"], name)


def sku(description, region, units, nanos):
    return {
        "description": description,
        "serviceRegions": [region],
        "pricingInfo": [
            {
                "pricingExpression": {
                    "tieredRates": [{"unitPrice": {"units": units, "nanos": nanos}}]
                }
            }
        ],
    }


class PricesTest(unittest.TestCase):
    def test_parse_and_refresh(self):
        skus = [
            sku(
                "Spot Preemptible C4D Instance Core running in Las Vegas", "us-west4", "0", 7000000
            ),
            sku("Spot Preemptible C4D Instance Ram running in Las Vegas", "us-west4", "0", 800000),
            sku(
                "Spot Preemptible C4D Instance Core running in Paris", "europe-west9", "0", 9000000
            ),
            sku("C4D Instance Core running in Las Vegas", "us-west4", "0", 30000000),
            sku("Spot Preemptible T2D AMD Instance Core running in Iowa", "r1", "0", 3000000),
            sku("Spot Preemptible T2D AMD Instance Ram running in Iowa", "r1", "0", 400000),
        ]
        parsed = prices.parse_skus(skus, ["c4d", "t2d"])
        self.assertEqual(parsed["c4d"], {"us-west4": [0.007, 0.0008]})  # Paris lacks its Ram SKU
        table = cost.PriceTable(TABLE)
        data, changes = prices.refresh(table, skus, _dt.date(2026, 11, 1))
        self.assertEqual(data["accessed"], "2026-11-01")
        self.assertEqual(data["families"]["c4d"]["spot"]["us-west4"], [0.007, 0.0008])
        self.assertAlmostEqual(data["families"]["t2d"]["spot"]["r1"][0], 0.003 + 4 * 0.0004)
        self.assertEqual(TABLE["families"]["c4d"]["spot"].get("us-west4"), None)  # input untouched
        self.assertEqual(len(changes), 2)

    def test_rank_skips_unpriced_regions(self):
        table = cost.PriceTable(TABLE)
        ranked = prices.rank(table, cost.Calibration(), ["c4d", "t2d"], ["r1", "r2", "r3"])
        self.assertEqual(
            [(f, r) for _, f, r, _ in ranked], [("t2d", "r1"), ("c4d", "r2"), ("c4d", "r1")]
        )

    def test_key_command_failure_does_not_leak(self):
        with self.assertRaises(prices.PriceError):
            prices.api_key(["false"])


class CalibrationTest(unittest.TestCase):
    def record(self, wall, machine=None, case="05-timer-led-10", seed="0", verdict="pass"):
        return {
            "kind": "ladder-cell",
            "labels": {"case": case, "seed": seed},
            "wall_s": wall,
            "verdict": verdict,
            "machine": {"machine_type": machine} if machine else {"platform": "darwin-arm64"},
        }

    def test_ingest_pairs_by_labels(self):
        reference = [
            self.record(30.0),
            self.record(60.0, case="06-chaser-14"),
            self.record(10, seed="1", verdict="error"),
        ]
        cloud = [
            self.record(40.0, "c4d-highcpu-8"),
            self.record(80.0, "c4d-highcpu-8", case="06-chaser-14"),
            self.record(50.0, "t2d-standard-8"),
            self.record(5.0, "t2d-standard-8", seed="1"),  # its reference errored: unpaired
        ]
        data = calibration.ingest(reference, cloud, _dt.date(2026, 10, 2))
        self.assertEqual(data["speed"]["c4d-highcpu-8"], 0.75)
        self.assertEqual(data["speed"]["c4d"], 0.75)
        self.assertEqual(data["speed"]["t2d-standard-8"], 0.6)
        self.assertEqual(data["samples"]["t2d"], 1)
        self.assertEqual(data["reference_seconds"]["ladder-cell"]["05-timer-led-10"], 30.0)
        cal = cost.Calibration(data)
        self.assertEqual(cal.speed("c4d-highcpu-16", "c4d"), 0.75)
        self.assertEqual(cal.reference_seconds("ladder-cell", "06-chaser-14"), 60.0)


if __name__ == "__main__":
    unittest.main()
