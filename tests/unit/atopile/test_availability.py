"""Assembly stock evidence is exact, quantity-aware and reproducibly replayable."""

import copy
import unittest
from unittest.mock import Mock

from yapnr.frontends.atopile.picker import availability as stock
from yapnr.frontends.atopile.picker import catalog

PART = {"lcsc": "C990000001", "mpn": "Fixture", "refs": ["R1"], "per_board": 1}
FACT = {
    "componentCode": PART["lcsc"],
    "componentModelEn": "Fixture",
    "stockCount": 19,
    "canPresaleNumber": 19,
    "leastPatchNumber": 20,
    "lossNumber": 10,
    "minPurchaseNum": 1,
}


class AvailabilityTest(unittest.TestCase):
    def check(self, parts=None, fact=None, quantity=5):
        return stock.check(
            parts or [PART],
            "jlcpcb",
            quantity,
            read=Mock(return_value=FACT if fact is None else fact),
            retrieved="2026-01-01T00:00:00+00:00",
        )

    def test_minimum_and_attrition_are_included(self):
        row = self.check()["results"][0]
        self.assertEqual(
            (row["needed"], row["screening_required"], row["status"]), (5, 20, "shortage")
        )
        row = self.check(fact={**FACT, "stockCount": 100, "canPresaleNumber": 100}, quantity=95)[
            "results"
        ][0]
        self.assertEqual(row["screening_required"], 105)
        self.assertEqual(row["status"], "shortage")

    def test_grouped_bom_demand_and_one_lookup_per_identity(self):
        parts = stock.bom_parts(
            b"Reference,MPN,LCSC,Mount\nR1,Fixture,C990000001,SMD\n"
            b"R2,Fixture,C990000001,SMD\nJ1,Header,C990000002,THT\n"
        )
        read = Mock(return_value={**FACT, "stockCount": 100, "canPresaleNumber": 100})
        report = stock.check(parts, "jlcpcb", 20, read=read)
        self.assertTrue(report["all_available"])
        self.assertEqual(report["results"][0]["needed"], 40)
        read.assert_called_once_with(PART["lcsc"])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            stock.aggregate([PART, PART])

    def test_negative_orderable_zero_and_missing_rules_never_pass(self):
        for fact, expected in (
            ({**FACT, "canPresaleNumber": -51}, "shortage"),
            ({**FACT, "stockCount": 0}, "shortage"),
            ({**FACT, "lossNumber": None}, "unknown"),
            ({**FACT, "componentModelEn": "wrong"}, "unknown"),
        ):
            with self.subTest(fact=fact):
                row = self.check(fact=fact)["results"][0]
                self.assertEqual(row["status"], expected)
        report = stock.check([PART], "jlcpcb", 5, read=Mock(side_effect=OSError("offline")))
        self.assertEqual(report["results"][0]["status"], "unknown")

    def test_snapshot_replay_no_network_and_cannot_change_demand_or_facts(self):
        report = self.check()
        read = Mock(side_effect=AssertionError("network"))
        self.assertEqual(stock.check([PART], "jlcpcb", 5, snapshot=report, read=read), report)
        read.assert_not_called()
        with self.assertRaisesRegex(ValueError, "does not match"):
            stock.check([PART], "jlcpcb", 6, snapshot=report)
        tampered = copy.deepcopy(report)
        tampered["facts"][0]["stockCount"] = 1000
        with self.assertRaisesRegex(ValueError, "hash changed"):
            stock.check([PART], "jlcpcb", 5, snapshot=tampered)

    def test_catalog_quantity_filter_rejects_unknown_zero_and_insufficient(self):
        parts = [{"stock": n} for n in (0, 3, 10)]
        self.assertEqual(catalog.stock_filter(parts, {"required_quantity": 5}), [{"stock": 10}])
        self.assertEqual(catalog.stock_filter(parts, {}), parts)
        for value in (True, 0, -1, "5"):
            with self.assertRaises(ValueError):
                catalog.stock_filter(parts, {"required_quantity": value})

    def test_pcbway_requires_its_own_supplier_confirmation(self):
        read = Mock(side_effect=AssertionError("JLC stock isn't PCBWay stock"))
        report = stock.check([PART], "pcbway", 5, read=read)
        read.assert_not_called()
        self.assertEqual(report["results"][0]["status"], "quote_required")
        self.assertFalse(report["all_available"])


if __name__ == "__main__":
    unittest.main()
