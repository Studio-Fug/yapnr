"""Public facts enter our picker without hosted Atopile calls or credentials."""

import unittest
from unittest.mock import Mock

from yapnr.frontends.atopile.picker import catalog, supplier

ROW = {
    "lcsc": 990000001,
    "mfr": "FixtureCap",
    "package": "0603",
    "capacitance_farads": 1e-7,
    "tolerance_fraction": 0.1,
    "voltage_rating": 50,
    "description": "Synthetic fixture",
    "stock": 10,
    "is_basic": True,
}
IDENTITY = {
    "easyeda_component_details": {
        "lcsc": 990000001,
        "easyeda_json": {
            "dataStr": {
                "head": {"c_para": {"Manufacturer": "Fixture", "Manufacturer Part": "FixtureCap"}}
            }
        },
    }
}


class SupplierTest(unittest.TestCase):
    def test_public_facts_are_matched_and_both_sources_captured(self):
        read = Mock(side_effect=[{"capacitors": [ROW]}, IDENTITY])
        body = supplier.encoded(
            {
                "queries": [
                    {
                        "endpoint": "capacitors",
                        "package": {"data": {"elements": ["0603"]}},
                        "capacitance": catalog.quantity(9e-8, 1.1e-7, "farad"),
                        "max_voltage": catalog.quantity(16, 50, "volt"),
                    }
                ]
            }
        )
        response = supplier.discover("POST", "/v0/query", body, read)
        part = response["results"][0]["components"][0]
        self.assertEqual(part["manufacturer_name"], "Fixture")
        self.assertEqual(part["lcsc"], ROW["lcsc"])
        self.assertEqual(part["attributes"]["max_voltage"], catalog.quantity(50, 50, "volt"))
        self.assertEqual(len(response["_sources"]), 2)
        self.assertTrue(
            all(call.args[0].startswith(supplier.SOURCE) for call in read.call_args_list)
        )
        self.assertNotIn("atopileapi", repr(read.call_args_list))
        self.assertIn("capacitance=1e-07", read.call_args_list[0].args[0])

    def test_missing_voltage_or_tolerance_cannot_satisfy_constraint(self):
        for fields in ({"voltage_rating": None}, {"tolerance_fraction": None}):
            read = Mock(return_value={"capacitors": [{**ROW, **fields}]})
            body = supplier.encoded(
                {
                    "queries": [
                        {"endpoint": "capacitors", "max_voltage": catalog.quantity(16, 50, "volt")}
                    ]
                }
            )
            response = supplier.discover("POST", "/v0/query", body, read)
            self.assertEqual(response["results"][0]["components"], [])
            self.assertEqual(read.call_count, 1)

    def test_identity_disagreement_is_rejected(self):
        read = Mock(side_effect=[{"components": [ROW]}, {"easyeda_component_details": {"lcsc": 2}}])
        with self.assertRaisesRegex(ValueError, "different LCSC"):
            supplier.discover("GET", "/v0/component/lcsc/990000001", None, read)

    def test_exact_lcsc_lookup_filters_fuzzy_search_results(self):
        read = Mock(side_effect=[{"components": [{**ROW, "lcsc": 123}, ROW]}, IDENTITY])
        response = supplier.discover("GET", "/v0/component/lcsc/990000001", None, read)
        self.assertEqual([part["lcsc"] for part in response["components"]], [990000001])
        self.assertEqual(read.call_count, 2)

    def test_unsupported_type_is_explicit_without_network(self):
        read = Mock(side_effect=AssertionError("Network forbidden"))
        with self.assertRaisesRegex(ValueError, "explicit LCSC/MPN"):
            supplier.discover("POST", "/v0/query/inductors", b"{}", read)


if __name__ == "__main__":
    unittest.main()
