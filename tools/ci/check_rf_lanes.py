"""Compare the checked-in RF inventory with Bazel's expanded test rules.

Input: bazel query 'kind(".*_test rule", ...)' --output=xml, including manual targets.
This catches additions, removals and a lost tag after macro expansion.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from xml.etree import ElementTree


def read_inventory(path):
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "RF_LANES" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("missing RF_LANES inventory")


def check(inventory, xml):
    rules = {}
    for rule in ElementTree.fromstring(xml).findall("rule"):
        tags = rule.find("list[@name='tags']")
        rules[rule.attrib["name"]] = (
            {item.attrib["value"] for item in tags} if tags is not None else set()
        )
    errors = []
    for name in sorted(inventory.keys() | rules.keys()):
        if name not in inventory:
            errors.append(f"unclassified target: {name}")
        elif name not in rules:
            errors.append(f"missing target: {name}")
        else:
            lane = inventory[name]
            expected = {
                "ordinary": set(),
                "nightly": {"rf-nightly"},
                "manual": {"manual"},
                "kicad": {"kicad"},
            }[lane]
            actual = rules[name] & {"rf-nightly", "manual", "kicad"}
            if actual != expected:
                errors.append(f"wrong lane: {name}: {sorted(actual)} != {lane}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("--inventory", default="tools/bazel/rf_lanes.bzl")
    args = parser.parse_args()
    inventory = read_inventory(args.inventory)
    errors = check(inventory, args.xml.read_text())
    for error in errors:
        print(error)
    if not errors:
        print(f"RF lane inventory: {len(inventory)} targets accounted for")
        for lane in ("ordinary", "nightly", "manual", "kicad"):
            targets = sorted(name for name, value in inventory.items() if value == lane)
            print(f"{lane}: {len(targets)} targets")
            for target in targets:
                print(f"  {target}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
