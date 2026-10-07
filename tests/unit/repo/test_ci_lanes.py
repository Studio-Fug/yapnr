"""Repository-level guards for RF selection, workflow triggers and cache failure/trust policy."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from tools.ci.check_rf_lanes import read_inventory
from tools.repo_root import workspace_root


def condition(expression, **values):
    """Evaluate the limited boolean GitHub expressions used by the cache gates."""
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    for name, value in sorted(values.items(), key=lambda item: -len(item[0])):
        expression = expression.replace(name, repr(value))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return eval(expression.strip(), {"__builtins__": {}}, {})


class CILanesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(workspace_root())

    def load(self, relative):
        return yaml.load((self.root / relative).read_text(), Loader=yaml.BaseLoader)

    def test_nightly_is_only_scheduled_or_dispatched_on_both_platforms(self):
        workflow = self.load(".github/workflows/rf-nightly.yaml")
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(
            set(workflow["jobs"]["rf"]["strategy"]["matrix"]["os"]),
            {"ubuntu-24.04-arm", "macos-latest"},
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        test = next(s for s in workflow["jobs"]["rf"]["steps"] if s.get("id") == "test")
        self.assertIn("--config=rf-nightly", test["run"])
        self.assertNotIn("continue-on-error", test)

    def test_ordinary_and_quick_lanes_exclude_expensive_rf_but_nightly_selects_it(self):
        lines = (self.root / ".bazelrc").read_text().splitlines()
        self.assertIn("test --test_tag_filters=-kicad,-rf-nightly", lines)
        self.assertIn("test:quick --test_tag_filters=-kicad,-rf-nightly,-slow", lines)
        self.assertIn("test:rf-nightly --test_tag_filters=rf-nightly,-kicad", lines)
        inventory = read_inventory(self.root / "tools/bazel/rf_lanes.bzl")
        self.assertEqual(inventory["//tests/unit/rf:test_inverse_smoke"], "ordinary")
        for package in ("tests/unit/rf", "tests/unit/rf_coupons"):
            for source in (self.root / package).glob("test_*.py"):
                self.assertIn(f"//{package}:{source.stem}", inventory)
            self.assertIn(
                "overrides = rf_overrides(", (self.root / package / "BUILD.bazel").read_text()
            )

    def test_completed_failures_save_but_cancelled_or_unstarted_tests_do_not(self):
        for filename, job in (
            ("ci.yaml", "test"),
            ("macos.yaml", "test-macos"),
            ("rf-nightly.yaml", "rf"),
        ):
            workflow = self.load(".github/workflows/" + filename)
            steps = workflow["jobs"][job]["steps"]
            test = next(s for s in steps if s.get("id") == "test")
            self.assertNotIn("continue-on-error", test)
            save = next(s for s in steps if s.get("uses") == "./.github/actions/bazel-cache-save")
            self.assertIn("!cancelled()", save["if"])
            for outcome in ("success", "failure", "skipped", "cancelled", ""):
                for cancelled in (True, False):
                    with self.subTest(workflow=filename, outcome=outcome, cancelled=cancelled):
                        self.assertEqual(
                            condition(
                                save["if"],
                                **{"cancelled()": cancelled, "steps.test.outcome": outcome},
                            ),
                            not cancelled and outcome in ("success", "failure"),
                        )
            self.assertTrue(any("check_rf_lanes.py" in s.get("run", "") for s in steps))

    def test_forks_restore_only_and_download_hits_are_not_saved_again(self):
        action = self.load(".github/actions/bazel-cache-save/action.yaml")
        for step in action["runs"]["steps"]:
            self.assertIn("!cancelled()", step["if"])
            self.assertEqual(step["uses"], "actions/cache/save@v6")
            for event, same_repo, allowed in (
                ("pull_request", False, False),
                ("pull_request", True, True),
                ("push", True, True),
                ("schedule", True, True),
                ("workflow_dispatch", True, True),
                ("pull_request_target", True, False),
            ):
                values = {
                    "cancelled()": False,
                    "inputs.repo-hit": "false",
                    "github.event_name": event,
                    "github.event.pull_request.head.repo.full_name": (
                        "project" if same_repo else "fork"
                    ),
                    "github.repository": "project",
                }
                self.assertEqual(condition(step["if"], **values), allowed)
                values["cancelled()"] = True
                self.assertFalse(condition(step["if"], **values))
            if "repo-hit" in step["if"]:
                self.assertFalse(
                    condition(
                        step["if"],
                        **{
                            "cancelled()": False,
                            "inputs.repo-hit": "true",
                            "github.event_name": "push",
                            "github.event.pull_request.head.repo.full_name": "project",
                            "github.repository": "project",
                        },
                    )
                )

    def test_restore_uses_fresh_snapshot_prefix_and_only_content_addressed_paths(self):
        action = self.load(".github/actions/bazel-cache-restore/action.yaml")
        restores = [
            s for s in action["runs"]["steps"] if s.get("uses") == "actions/cache/restore@v6"
        ]
        self.assertEqual(len(restores), 2)
        disk = next(s for s in restores if s["with"]["path"] == "~/.cache/bazel-disk")
        self.assertEqual(disk["with"]["restore-keys"], "${{ steps.keys.outputs.disk-prefix }}")
        self.assertEqual(disk["with"]["key"], "${{ steps.keys.outputs.disk-key }}")
        self.assertNotIn("output_base", str(restores))


if __name__ == "__main__":
    unittest.main()
