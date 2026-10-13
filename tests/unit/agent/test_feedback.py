"""Real isolated Git checkouts with deterministic fake PR/worker metadata; no model calls."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yapnr.feedback import Conflict, Feedback

VALIDATOR = """import json, sys
p=json.load(open(sys.argv[1]))
cases=[{"id": "R5", "passed": p["R5"] == 0}, {"id": "R6", "passed": p["R6"] == 0}]
print(json.dumps({"cases":cases}))
raise SystemExit(not all(c["passed"] for c in cases))
"""


def git(*args):
    return subprocess.check_output(
        ["git", *map(str, args)], stderr=subprocess.DEVNULL, text=True, timeout=30
    ).strip()


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "article"
        self.root.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git("init", self.repo)
        git("-C", self.repo, "config", "user.name", "Test Agent")
        git("-C", self.repo, "config", "user.email", "123+fixture@users.noreply.github.com")
        self.remote = self.base / "remote.git"
        git("init", "--bare", self.remote)
        for name, raw in {
            "validator.py": VALIDATOR,
            "before.json": '{"R5":4,"R6":4}',
            "before.svg": '<svg xmlns="http://www.w3.org/2000/svg"><text>Before</text></svg>',
        }.items():
            (self.root / name).write_text(raw)
            (self.repo / name).write_text(raw)
        self.commits = {}
        for name, r6 in (("half", 4), ("after", 0)):
            (self.repo / "candidate.json").write_text(json.dumps({"R5": 0, "R6": r6}))
            (self.repo / "after.svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg"><text>{name}</text></svg>'
            )
            git("-C", self.repo, "add", ".")
            git("-C", self.repo, "commit", "-m", name)
            self.commits[name] = git("-C", self.repo, "rev-parse", "HEAD")
        git("-C", self.repo, "push", self.remote, "HEAD:refs/heads/main")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.metadata = self.base / "pr.json"
        (self.bin / "gh").write_text(
            f'#!{sys.executable}\nimport os\nprint(open(os.environ["FAKE_PR_METADATA"]).read())\n'
        )
        (self.bin / "gh").chmod(0o755)
        self.env = patch.dict(
            os.environ,
            {
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "FAKE_PR_METADATA": str(self.metadata),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Feedback(self.root)
        self.item = self.store.add("Move both R5 and R6", identity="example")
        self.plan = {
            "repository": str(self.remote),
            "files": ["validator.py", "before.json", "before.svg"],
            "before_visual": "before.svg",
            "settings": {"seed": 0},
            "criteria": [{"id": r, "check": "placement", "baseline": "fail"} for r in ("R5", "R6")],
            "checks": [
                {
                    "id": "placement",
                    "validator": "validator.py",
                    "baseline": "before.json",
                    "argv": [sys.executable, "{validator}", "{candidate}"],
                    "timeout_seconds": 10,
                }
            ],
        }
        self.item = self.store.plan("example", self.item["revision"], self.plan)
        self.point("after")

    def point(self, name):
        head = self.commits[name]
        git("--git-dir", self.remote, "update-ref", "refs/pull/1/head", head)
        self.metadata.write_text(json.dumps(self.metadata_for(head)))

    def metadata_for(self, head):
        return {
            "url": "https://github.com/example/engine/pull/1",
            "headRefOid": head,
            "state": "OPEN",
            "body": "\n".join(
                f"![{name}](https://raw.githubusercontent.com/example/engine/{head}/{name}.svg)"
                for name in ("before", "after")
            ),
        }

    def submit(self, name="after"):
        self.point(name)
        item = self.store.dispatch(
            "example", self.store.get("example")["revision"], "isolated-worker"
        )
        receipt = {
            "attempt": item["attempts"][-1]["id"],
            "pr": "https://github.com/example/engine/pull/1",
            "head": self.commits[name],
            "outputs": {"placement": "candidate.json"},
            "before_visual": "before.svg",
            "after_visual": "after.svg",
        }
        return self.store.submit("example", item["revision"], receipt)

    def test_independent_full_pass_then_remote_revision_invalidates(self):
        item = self.submit()
        item = self.store.verify("example", item["revision"])
        self.assertEqual(item["state"], "addressed", item.get("verification"))
        self.assertEqual(Feedback(self.root).query("example")["state"], "addressed")
        self.point("half")
        self.assertEqual(self.store.query("example")["state"], "stale")

    def test_partial_stays_open_and_exact_failure_reaches_worker_bundle(self):
        item = self.submit("half")
        item = self.store.verify("example", item["revision"])
        self.assertEqual(item["state"], "retry")
        self.assertFalse(item["verification"]["checks"][0]["cases"][1]["passed"])
        item = self.store.dispatch("example", item["revision"], "second-worker")
        bundle = json.loads(self.store.object(item["attempts"][-1]["bundle_sha256"]))
        self.assertEqual(bundle["previous_failures"]["checks"][0]["cases"][1]["id"], "R6")
        self.assertEqual(bundle["submission_template"]["attempt"], item["attempts"][-1]["id"])
        self.assertEqual(set(bundle["submission_template"]["outputs"]), {"placement"})
        with self.assertRaises(Conflict):
            self.store.dispatch("example", item["revision"], "duplicate")

    def test_forged_report_and_http_execution_cannot_close(self):
        item = self.submit()
        with self.assertRaises(ValueError):
            self.store.api({"action": "verify", "id": "example", "passed": True})
        with self.assertRaises(ValueError):
            self.store.submit("example", item["revision"], {"passed": True})
        self.assertEqual(self.store.get("example")["state"], "submitted")

    def test_changed_input_and_feedback_revoke_closure(self):
        item = self.submit()
        item = self.store.verify("example", item["revision"])
        (self.root / "validator.py").write_text(VALIDATOR + "\n# revised criterion\n")
        self.assertEqual(self.store.query("example")["state"], "stale")
        item = self.store.get("example")
        item = self.store.add("Also move Q1", identity="example", revision=item["revision"])
        self.assertEqual(item["state"], "needs_plan")
        self.assertIsNone(item["plan"])

    def test_incomplete_tail_idempotency_and_export_copy(self):
        with self.store.log.open("ab") as stream:
            stream.write(b'{"partial":')
        item = self.store.control("example", self.item["revision"], "pause")
        self.assertTrue(item["paused"])
        copied = self.base / "import"
        shutil.copytree(self.root, copied)
        self.assertTrue(Feedback(copied).query("example")["paused"])
        self.assertEqual(len(Feedback(copied).query()["items"]), 1)
        self.assertTrue(self.store.log.read_bytes().endswith(b"\n"))

    def test_symlink_traversal_rejected(self):
        (self.root / "link.py").symlink_to(self.root / "validator.py")
        for name in ("../article/validator.py", "link.py"):
            plan = dict(self.plan, files=[name, "before.json", "before.svg"])
            with self.assertRaises(ValueError):
                self.store.plan("example", self.item["revision"], plan)

    def test_missing_criterion_rejected_not_subset_pass(self):
        (self.root / "validator.py").write_text(
            'import json; print(json.dumps({"cases":[{"id":"R5","passed":False}]})); raise SystemExit(1)'
        )
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.store.plan("example", self.item["revision"], self.plan)

    def test_wrong_commit_visual_reference_rejected(self):
        item = self.submit()
        data = self.metadata_for(self.commits["after"])
        data["body"] = "before.svg after.svg"
        self.metadata.write_text(json.dumps(data))
        item = self.store.verify("example", item["revision"])
        self.assertEqual(item["state"], "retry")
        self.assertIn("exact submitted commit", item["verification"]["error"])

    def test_bounded_worker_retry_then_real_independent_pass(self):
        script = self.base / "worker.py"
        script.write_text(
            """import json, pathlib, subprocess, sys
bundle=pathlib.Path(sys.argv[1]); task=json.loads((bundle/"task.json").read_text())
config=json.loads(sys.argv[2]); head=config["after"] if task["previous_failures"] else config["half"]
subprocess.run(["git","--git-dir",config["remote"],"update-ref","refs/pull/1/head",head],check=True,timeout=10)
body="\\n".join(
    f"![{name}](https://raw.githubusercontent.com/example/engine/{head}/{name}.svg)"
    for name in ("before","after"))
pathlib.Path(config["metadata"]).write_text(json.dumps({"url":"https://github.com/example/engine/pull/1","headRefOid":head,"state":"OPEN","body":body}))
(bundle/"submission.json").write_text(json.dumps({"attempt":task["attempt"]["id"],"pr":"https://github.com/example/engine/pull/1","head":head,"outputs":{"placement":"candidate.json"},"before_visual":"before.svg","after_visual":"after.svg"}))
"""
        )
        config = dict(self.commits, remote=str(self.remote), metadata=str(self.metadata))
        item = self.store.run_loop(
            "example",
            [sys.executable, str(script), "{bundle}", json.dumps(config)],
            max_attempts=2,
            timeout=30,
        )
        self.assertEqual(item["state"], "addressed", item.get("verification"))
        self.assertEqual(len(item["attempts"]), 2)
        self.assertFalse(item["attempts"][0]["verification"]["passed"])
        self.assertTrue(item["attempts"][1]["verification"]["passed"])

    def test_failed_worker_budget_pause_resume_remain_unresolved(self):
        item = self.store.run_loop(
            "example", [sys.executable, "-c", "raise SystemExit(1)"], max_attempts=1
        )
        self.assertEqual(item["state"], "exhausted")
        item = self.store.control("example", item["revision"], "pause")
        count = len(item["attempts"])
        self.assertTrue(self.store.run_loop("example", [sys.executable, "-c", "pass"])["paused"])
        self.assertEqual(len(self.store.get("example")["attempts"]), count)
        item = self.store.control("example", item["revision"], "resume")
        self.assertEqual(item["state"], "retry")


if __name__ == "__main__":
    unittest.main()
