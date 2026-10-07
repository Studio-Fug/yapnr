"""The RF inventory rejects targets that disappear, lose tags, or evade classification."""

import unittest
from xml.etree import ElementTree as ET

from tools.ci.check_rf_lanes import check


def rules_xml(rules):
    root = ET.Element("query")
    for name, tags in rules.items():
        rule = ET.SubElement(root, "rule", name=name)
        tag_list = ET.SubElement(rule, "list", name="tags")
        for tag in tags:
            ET.SubElement(tag_list, "string", value=tag)
    return ET.tostring(root)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.inventory = {
            "//rf:smoke": "ordinary",
            "//rf:gradient": "nightly",
            "//rf:full": "manual",
        }
        self.rules = {
            "//rf:smoke": ["rf-smoke"],
            "//rf:gradient": ["rf-nightly", "cpu:3"],
            "//rf:full": ["manual"],
        }

    def test_all_targets_accounted_for_with_resources_preserved(self):
        self.assertEqual(check(self.inventory, rules_xml(self.rules)), [])

    def test_unclassified_addition_is_rejected(self):
        self.rules["//rf:new"] = []
        self.assertIn("unclassified target: //rf:new", check(self.inventory, rules_xml(self.rules)))

    def test_target_removal_is_rejected(self):
        del self.rules["//rf:gradient"]
        self.assertIn("missing target: //rf:gradient", check(self.inventory, rules_xml(self.rules)))

    def test_lost_nightly_tag_and_smoke_accidentally_gated_are_rejected(self):
        self.rules["//rf:gradient"] = ["cpu:3"]
        self.rules["//rf:smoke"] = ["rf-nightly"]
        self.assertEqual(len(check(self.inventory, rules_xml(self.rules))), 2)

    def test_nightly_marked_manual_cannot_silently_disappear(self):
        self.rules["//rf:gradient"].append("manual")
        self.assertEqual(len(check(self.inventory, rules_xml(self.rules))), 1)


if __name__ == "__main__":
    unittest.main()
