"""The picker catalog schema, the Splanc converter and type-query matching (synthetic parts)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.frontends.atopile.picker import catalog as C


def part(lcsc, kind="resistor", package="0603", **params):
    return {
        "lcsc": lcsc,
        "mpn": f"SYN-{lcsc}",
        "manufacturer": "Synthetic Parts Co",
        "package": package,
        "description": "synthetic test part",
        "kind": kind,
        "params": params,
    }


def doc(*parts):
    return {"schema": C.SCHEMA, "provenance": {"source": "unit test"}, "parts": list(parts)}


def ohm_query(lo, hi, package=None):
    query = {"endpoint": "resistors", "qty": 1, "resistance": C.quantity(lo, hi, "ohm")}
    if package:
        query["package"] = {
            "type": "EnumSet",
            "data": {"elements": [{"name": package}], "enum": {"R0603": "R0603"}},
        }
    return query


class SchemaTest(unittest.TestCase):
    def test_normalize_lcsc(self):
        self.assertEqual(C.normalize_lcsc("C123"), "C123")
        self.assertEqual(C.normalize_lcsc(123), "C123")
        self.assertEqual(C.normalize_lcsc(" c77 "), "C77")
        for bad in ("", "C0", "C-1", "X12", True, None, "C12a"):
            with self.assertRaises(ValueError, msg=bad):
                C.normalize_lcsc(bad)

    def test_valid_catalog_is_normalized(self):
        out = C.validate(doc(part(990000001, resistance_ohm=1000)))
        entry = out["parts"][0]
        self.assertEqual(entry["lcsc"], "C990000001")
        self.assertEqual(entry["stock"], "unknown")
        self.assertFalse(entry["basic"])
        self.assertEqual(entry["params"], {"resistance_ohm": 1000.0})

    def test_every_problem_is_reported(self):
        bad = doc(
            {"lcsc": "nope", "mpn": 3, "manufacturer": "M", "kind": "gizmo"},
            part(900002, frequency=3),
            part(900002),
        )
        bad["extra"] = 1
        with self.assertRaises(C.CatalogError) as caught:
            C.validate(bad)
        text = str(caught.exception)
        for fragment in (
            "not an LCSC",
            "'mpn' must be a string",
            "kind 'gizmo'",
            "unknown param 'frequency'",
            "repeats",
            "unknown top-level",
        ):
            self.assertIn(fragment, text)

    def test_schema_and_provenance_are_required(self):
        with self.assertRaises(C.CatalogError):
            C.validate({"schema": "other", "parts": []})
        with self.assertRaises(C.CatalogError):
            C.validate({"schema": C.SCHEMA, "parts": []})

    def test_stock_and_price(self):
        good = C.validate(doc({**part(900003), "stock": 5, "price": 0.01}))["parts"][0]
        self.assertEqual((good["stock"], good["price"]), (5, 0.01))
        for bad in ({"stock": -1}, {"stock": "lots"}, {"price": -1}, {"datasheet_url": "ftp://x"}):
            with self.assertRaises(C.CatalogError, msg=bad):
                C.validate(doc({**part(900004), **bad}))

    def test_load_and_dump_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(doc(part(900006), part(900005))))
            loaded = C.load(path)
            again = json.loads(C.dump(loaded))
            self.assertEqual([p["lcsc"] for p in again["parts"]], ["C900005", "C900006"])
            self.assertEqual(C.validate(again), C.validate(loaded) | {"parts": again["parts"]})

    def test_load_rejects_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text("{")
            with self.assertRaises(C.CatalogError):
                C.load(path)


class SplancConversionTest(unittest.TestCase):
    def test_lists_become_kinds_and_only_given_facts_are_kept(self):
        splanc = {
            "_comment": "ignored",
            "ics": [
                {
                    "lcsc": 900010,
                    "mpn": "SYN-IC",
                    "manufacturer": "Syn",
                    "package": "SOT-23-5",
                    "description": "synthetic regulator",
                }
            ],
            "passives": [
                {
                    "lcsc": 900011,
                    "mpn": "SYN-C",
                    "manufacturer": "Syn",
                    "package": "0402",
                    "description": "100nF 50V X7R",
                },
                {
                    "lcsc": 900012,
                    "mpn": "SYN-R",
                    "manufacturer": "Syn",
                    "package": "0402",
                    "description": "1KΩ (1001) ±1%",
                },
                {
                    "lcsc": 900013,
                    "mpn": "SYN-L",
                    "manufacturer": "Syn",
                    "package": "0806",
                    "description": "4.7uH shielded",
                },
            ],
            "resistors": [
                {
                    "lcsc": 900014,
                    "mpn": "SYN-R2",
                    "manufacturer": "Syn",
                    "package": "0402",
                    "resistance_ohms": 150.0,
                    "basic": 1,
                }
            ],
        }
        out = C.from_splanc(splanc, source="synthetic", retrieved="2026-09-30")
        kinds = {p["lcsc"]: p["kind"] for p in out["parts"]}
        self.assertEqual(
            kinds,
            {
                "C900010": "ic",
                "C900011": "capacitor",
                "C900012": "resistor",
                "C900013": "inductor",
                "C900014": "resistor",
            },
        )
        r2 = next(p for p in out["parts"] if p["lcsc"] == "C900014")
        self.assertEqual(r2["params"], {"resistance_ohm": 150.0})
        self.assertTrue(r2["basic"])
        self.assertTrue(all(p["stock"] == "unknown" for p in out["parts"]))
        self.assertEqual(out["provenance"]["retrieved"], "2026-09-30")
        # Nothing is parsed out of descriptions but the kind.
        cap = next(p for p in out["parts"] if p["lcsc"] == "C900011")
        self.assertEqual(cap["params"], {})


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.catalog = C.Catalog(
            [
                C.validate(
                    doc(
                        part(900021, resistance_ohm=1000, tolerance_pct=1),
                        {**part(900022, resistance_ohm=1020, tolerance_pct=1), "basic": True},
                        part(900023, package="0402", resistance_ohm=1000, tolerance_pct=1),
                        part(900024, resistance_ohm=1000, tolerance_pct=10),
                        part(900025, kind="capacitor", capacitance_f=1e-7, voltage_max_v=50),
                        part(900026, resistance_ohm=4700),
                        part(900027, kind="capacitor", capacitance_f=1e-7),
                    )
                )
            ]
        )

    def lcscs(self, components):
        return [f"C{c['lcsc']}" for c in components]

    def test_value_band_must_fit_the_query(self):
        found = self.lcscs(self.catalog.query("resistors", ohm_query(950, 1050)))
        # 10 % tolerance leaves the band; the basic part ranks first, then the closest value.
        self.assertEqual(found, ["C900022", "C900021", "C900023"])

    def test_package_filter_ignores_the_r_prefix(self):
        found = self.lcscs(self.catalog.query("resistors", ohm_query(950, 1050, "R0603")))
        self.assertEqual(found, ["C900022", "C900021"])

    def test_kind_and_unknown_endpoint(self):
        caps = self.catalog.query(
            "capacitors", {"capacitance": C.quantity(9e-8, 1.1e-7, "farad"), "package": None}
        )
        self.assertEqual(self.lcscs(caps), ["C900025", "C900027"])
        self.assertEqual(self.catalog.query("leds", {}), [])

    def test_constrained_parameter_the_part_lacks_excludes_it(self):
        query = {
            "capacitance": C.quantity(9e-8, 1.1e-7, "farad"),
            "max_voltage": C.quantity(10, None, "volt"),  # at least 10 V (no upper bound)
        }
        self.assertEqual(self.lcscs(self.catalog.query("capacitors", query)), ["C900025"])
        query["max_voltage"] = C.quantity(60, None, "volt")
        self.assertEqual(self.catalog.query("capacitors", query), [])

    def test_component_shape(self):
        component = self.catalog.by_lcsc("C900021")[0]
        self.assertEqual(component["lcsc"], 900021)
        self.assertEqual(component["stock"], 0)
        self.assertEqual(component["price"][0]["price"], 0.0)
        band = C.intervals(component["attributes"]["resistance"])
        self.assertAlmostEqual(band[0][0], 990.0)
        self.assertAlmostEqual(band[0][1], 1010.0)
        self.assertIsNone(component["attributes"]["max_power"])
        self.assertEqual(component["attributes"]["resistance"]["data"]["unit"], "ohm")

    def test_lookups_and_answer_dispatch(self):
        self.assertEqual(self.lcscs(self.catalog.by_lcsc(900026)), ["C900026"])
        self.assertEqual(self.catalog.by_lcsc("garbage"), [])
        by_mpn = self.catalog.by_mfr("synthetic parts co", "syn-900026")
        self.assertEqual(self.lcscs(by_mpn), ["C900026"])
        answer = self.catalog.answer
        self.assertEqual(self.lcscs(answer({"lcsc": 900021, "quantity": 1})), ["C900021"])
        mpn_query = {"manufacturer_name": "Synthetic Parts Co", "part_number": "SYN-900023"}
        self.assertEqual(self.lcscs(answer(mpn_query)), ["C900023"])
        self.assertEqual(len(answer(ohm_query(4000, 5000))), 1)
        self.assertEqual(answer({"endpoint": 3}), [])

    def test_first_catalog_wins(self):
        first = C.validate(doc({**part(900030), "description": "first"}))
        second = C.validate(doc({**part(900030), "description": "second"}))
        merged = C.Catalog([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged.by_lcsc("C900030")[0]["description"], "first")


if __name__ == "__main__":
    unittest.main()
