"""Independent acceptance of the public feedback controller; real git, offline fake GitHub."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from yapnr.feedback import Conflict, Feedback

BASE = Path(__file__).parent / "fixtures" / "feedback"


def git(path, *args):
    p = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "123+fixture@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "123+fixture@users.noreply.github.com",
        },
    )
    if p.returncode:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()


def svg(name):
    p = json.loads((BASE / (name + ".json")).read_text())
    shapes = "".join(
        f'<rect x="{40+v[0]*40}" y="{40+v[1]*40}" width="20" height="20"/>'
        f'<text x="{40+v[0]*40}" y="{36+v[1]*40}">{k}</text>'
        for k, v in p.items()
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="260" height="170"><title>Synthetic '
        + name
        + "</title>"
        + shapes
        + "</svg>"
    )


class Gates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parent-feedback-")
        self.addCleanup(self.tmp.cleanup)
        self.r = Path(self.tmp.name)
        self.project = self.r / "project"
        self.project.mkdir()
        self.repo = self.r / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        self.bare = self.r / "remote.git"
        git(self.r, "init", "--bare", "-q", str(self.bare))
        git(self.repo, "remote", "add", "origin", str(self.bare))
        for n in ("validator.py", "before.json"):
            shutil.copy2(BASE / (n + ".txt" if n == "validator.py" else n), self.project / n)
        (self.project / "before.svg").write_text(svg("before"))
        (self.repo / "before.svg").write_text(svg("before"))
        self.bin = self.r / "bin"
        self.bin.mkdir()
        self.metadata = self.r / "gh.json"
        g = self.bin / "gh"
        g.write_text(
            '#!/usr/bin/env python3\nimport os\nprint(open(os.environ["PARENT_GH_METADATA"]).read())\n'
        )
        g.chmod(0o755)
        self.oldpath = os.environ["PATH"]
        os.environ["PATH"] = str(self.bin) + os.pathsep + self.oldpath
        os.environ["PARENT_GH_METADATA"] = str(self.metadata)
        self.addCleanup(self.restoreenv)
        self.store = Feedback(self.project)
        self.item = self.store.add(
            "Both R5/R6 must reach pads by displacing Q1, retaining clearance."
        )
        self.definition = {
            "repository": str(self.bare),
            "files": ["validator.py", "before.json", "before.svg"],
            "before_visual": "before.svg",
            "settings": {"seed": 0, "fixture": "synthetic"},
            "criteria": [
                {
                    "id": i,
                    "check": "placement",
                    "baseline": "pass" if i == "non-overlap" else "fail",
                }
                for i in ["R5-pad-proximity", "R6-pad-proximity", "Q1-displaced", "non-overlap"]
            ],
            "checks": [
                {
                    "id": "placement",
                    "validator": "validator.py",
                    "baseline": "before.json",
                    "argv": [sys.executable, "{validator}", "{candidate}"],
                    "timeout_seconds": 5,
                }
            ],
        }
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)

    def restoreenv(self):
        os.environ["PATH"] = self.oldpath
        os.environ.pop("PARENT_GH_METADATA", None)

    def commit(self, name="after", body=None):
        (self.repo / "after.json").write_bytes((BASE / (name + ".json")).read_bytes())
        (self.repo / "after.svg").write_text(svg(name))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--allow-empty", "-qm", name)
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "--force", "origin", "HEAD:refs/pull/1/head")
        url = "https://github.com/example/synthetic/pull/1"
        body = (
            body
            if body is not None
            else "\n".join(
                f"![{n}](https://raw.githubusercontent.com/example/synthetic/{head}/{n}.svg)"
                for n in ("before", "after")
            )
        )
        self.metadata.write_text(
            json.dumps({"headRefOid": head, "url": url, "body": body, "state": "OPEN"})
        )
        return head

    def dispatch(self):
        self.item = self.store.dispatch(
            self.item["id"], self.item["revision"], "separate-fixture-worker"
        )
        return self.item

    def submit(self, head, **extra):
        receipt = {
            "attempt": self.item["attempts"][-1]["id"],
            "pr": "https://github.com/example/synthetic/pull/1",
            "head": head,
            "outputs": {"placement": "after.json"},
            "before_visual": "before.svg",
            "after_visual": "after.svg",
            **extra,
        }
        self.item = self.store.submit(self.item["id"], self.item["revision"], receipt)
        return self.item

    def verify(self):
        self.item = self.store.verify(self.item["id"], self.item["revision"])
        return self.item

    def pass_it(self):
        head = self.commit()
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertEqual(self.item["state"], "addressed")

    def test_complete_exact_head_and_restart(self):
        self.pass_it()
        fresh = Feedback(self.project).query(self.item["id"])
        self.assertEqual(fresh["state"], "addressed")
        self.assertEqual(
            {c["id"] for c in fresh["verification"]["checks"][0]["cases"]},
            {c["id"] for c in self.definition["criteria"]},
        )

    def test_partial_rejected_retry_carries_failures(self):
        head = self.commit("half")
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertEqual(self.item["state"], "retry")
        failed = {
            c["id"] for c in self.item["verification"]["checks"][0]["cases"] if not c["passed"]
        }
        self.assertIn("R6-pad-proximity", failed)
        self.dispatch()
        task = json.loads(self.store.object(self.item["attempts"][-1]["bundle_sha256"]))
        self.assertEqual(task["previous_failures"]["passed"], False)
        head = self.commit("after")
        self.submit(head)
        self.verify()
        self.assertEqual(self.item["state"], "addressed")
        self.assertEqual(len(self.item["attempts"]), 2)

    def test_worker_pass_boolean_rejected(self):
        head = self.commit()
        self.dispatch()
        with self.assertRaises(ValueError):
            self.submit(head, passed=True)
        self.assertEqual(self.store.query(self.item["id"])["state"], "implementing")

    def test_changed_pr_head_revokes_closure(self):
        self.pass_it()
        self.commit("half")
        self.assertEqual(self.store.query(self.item["id"])["state"], "stale")

    def test_changed_feedback_revokes_closure(self):
        self.pass_it()
        self.item = self.store.add(
            "Also rotate both parts", identity=self.item["id"], revision=self.item["revision"]
        )
        self.assertEqual(self.item["state"], "needs_plan")
        self.assertIsNone(self.item["verification"])

    def test_changed_validator_revokes_closure(self):
        self.pass_it()
        p = self.project / "validator.py"
        p.write_text(p.read_text() + "\n# edited\n")
        self.assertEqual(self.store.query(self.item["id"])["state"], "stale")

    def test_plain_visual_mentions_do_not_count(self):
        head = self.commit(body="before.svg after.svg")
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertNotEqual(self.item["state"], "addressed")

    def test_wrong_commit_visuals_do_not_count(self):
        head = self.commit(
            body="![Before](https://raw.githubusercontent.com/example/synthetic/"
            + "a" * 40
            + "/before.svg)\n![After](https://raw.githubusercontent.com/example/synthetic/"
            + "a" * 40
            + "/after.svg)"
        )
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertNotEqual(self.item["state"], "addressed")

    def test_missing_visual_rejected(self):
        head = self.commit()
        (self.repo / "after.svg").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "missing")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "--force", "origin", "HEAD:refs/pull/1/head")
        m = json.loads(self.metadata.read_text())
        m["body"] = m["body"].replace(m["headRefOid"], head)
        m["headRefOid"] = head
        self.metadata.write_text(json.dumps(m))
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertNotEqual(self.item["state"], "addressed")

    def test_no_http_execution_or_forged_closure(self):
        for action in ["plan", "dispatch", "submit", "verify", "run", "addressed", "tested"]:
            with self.assertRaises(ValueError):
                self.store.api({"action": action, "id": self.item["id"], "passed": True})

    def test_pause_prevents_dispatch_until_resume(self):
        self.item = self.store.control(self.item["id"], self.item["revision"], "pause")
        with self.assertRaises(Conflict):
            self.dispatch()
        self.item = Feedback(self.project).control(self.item["id"], self.item["revision"], "resume")
        self.dispatch()
        self.assertEqual(self.item["state"], "implementing")

    def test_duplicate_submission_revision_refused(self):
        head = self.commit()
        self.dispatch()
        previous = self.item["revision"]
        self.submit(head)
        with self.assertRaises(Conflict):
            self.store.submit(self.item["id"], previous, self.item["submission"])

    def test_path_escape_and_symlink_refused(self):
        for name in ["../outside", "/outside", ".git/config"]:
            d = copy.deepcopy(self.definition)
            d["files"].append(name)
            with self.assertRaises(ValueError):
                self.store.plan(self.item["id"], self.item["revision"], d)
        (self.project / "escape").symlink_to(BASE / "validator.py.txt")
        d = copy.deepcopy(self.definition)
        d["files"].append("escape")
        with self.assertRaises(ValueError):
            self.store.plan(self.item["id"], self.item["revision"], d)

    def test_incomplete_journal_tail_recovered_without_losing_events(self):
        original = self.store.log.read_bytes()
        self.store.log.write_bytes(original + b'{"incomplete":')
        self.assertEqual(Feedback(self.project).query(self.item["id"])["state"], "ready")
        self.store.control(self.item["id"], self.item["revision"], "pause")
        self.assertTrue(self.store.log.read_bytes().startswith(original))
        self.assertEqual(len(self.store.events()), 3)

    def worker(self, always_half=False, delay=0):
        script = self.r / "worker.py"
        count = self.r / "worker-count"
        script.write_text(
            """import sys,json,time
from pathlib import Path
sys.path[:0]=%r
from test_feedback_acceptance import git,svg,BASE
b=Path(sys.argv[1]);repo=Path(%r);count=Path(%r);metadata=Path(%r)
n=int(count.read_text()) if count.exists() else 0
count.write_text(str(n+1));time.sleep(%r)
t=json.loads((b/'task.json').read_text())
if n>0:assert t['previous_failures']['passed'] is False
name='half' if n==0 or %r else 'after'
(repo/'after.json').write_bytes((BASE/(name+'.json')).read_bytes());(repo/'after.svg').write_text(svg(name))
git(repo,'add','.');git(repo,'commit','--allow-empty','-qm',name);head=git(repo,'rev-parse','HEAD');git(repo,'push','--force','origin','HEAD:refs/pull/1/head')
url='https://github.com/example/synthetic/pull/1'
body='\\n'.join(
    '![%%s](https://raw.githubusercontent.com/example/synthetic/%%s/%%s.svg)'%%(n,head,n)
    for n in ('before','after'))
metadata.write_text(json.dumps({'headRefOid':head,'url':url,'body':body,'state':'OPEN'}))
(b/'submission.json').write_text(json.dumps({'attempt':t['attempt']['id'],'pr':url,'head':head,'outputs':{'placement':'after.json'},'before_visual':'before.svg','after_visual':'after.svg'}))
"""
            % (
                [
                    str(Path(__file__).parent),
                    *[str(Path(entry or os.getcwd()).resolve()) for entry in sys.path],
                ],
                str(self.repo),
                str(count),
                str(self.metadata),
                delay,
                always_half,
            )
        )
        return [sys.executable, str(script), "{bundle}"]

    def test_automatic_loop_rejects_half_then_closes_full(self):
        self.item = self.store.run_loop(self.item["id"], self.worker(), max_attempts=3, timeout=5)
        self.assertEqual(self.item["state"], "addressed")
        self.assertEqual(len(self.item["attempts"]), 2)

    def test_budget_stops_unresolved_then_explicit_resume(self):
        argv = self.worker()
        self.item = self.store.run_loop(self.item["id"], argv, max_attempts=1, timeout=5)
        self.assertEqual(self.item["state"], "exhausted")
        self.item = self.store.control(self.item["id"], self.item["revision"], "resume")
        self.item = self.store.run_loop(self.item["id"], argv, max_attempts=1, timeout=5)
        self.assertEqual(self.item["state"], "addressed")

    def test_failed_worker_never_closes_and_stops_at_budget(self):
        self.item = self.store.run_loop(
            self.item["id"],
            [sys.executable, "-c", "raise SystemExit(7)"],
            max_attempts=2,
            timeout=5,
        )
        self.assertEqual(self.item["state"], "exhausted")
        self.assertEqual(len(self.item["attempts"]), 2)

    def test_pause_at_worker_return_preserves_submission(self):
        argv = self.worker(delay=0.4)
        (self.r / "worker-count").write_text("1")
        # Successful first candidate; no previous failure is required for this test.
        f = self.r / "worker.py"
        f.write_text(
            f.read_text().replace("if n>0:assert t['previous_failures']['passed'] is False", "")
        )
        errors = []

        def go():
            try:
                self.store.run_loop(self.item["id"], argv, max_attempts=2, timeout=5)
            except Exception as e:
                errors.append(str(e))

        thread = threading.Thread(target=go)
        thread.start()
        for _ in range(100):
            current = self.store.get(self.item["id"])
            if current["state"] == "implementing":
                break
            time.sleep(0.01)
        self.store.control(current["id"], current["revision"], "pause")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.item = self.store.query(self.item["id"])
        self.assertTrue(self.item["paused"])
        self.assertEqual(self.item["state"], "submitted")
        self.item = self.store.control(self.item["id"], self.item["revision"], "resume")
        self.item = self.store.run_loop(self.item["id"], argv, max_attempts=2, timeout=5)
        self.assertEqual(self.item["state"], "addressed")
        self.assertEqual((self.r / "worker-count").read_text(), "2")

    def test_pause_during_verification_is_resumable(self):
        v = self.project / "validator.py"
        v.write_text(
            v.read_text().replace(
                "p=json.load(open(sys.argv[1]));cases=[]",
                "p=json.load(open(sys.argv[1]));cases=[]\nif p['R5'][0]==0: __import__('time').sleep(.3)",
            )
        )
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)
        head = self.commit()
        self.dispatch()
        self.submit(head)
        errors = []

        def go():
            try:
                self.store.verify(self.item["id"], self.item["revision"])
            except Exception as e:
                errors.append(str(e))

        thread = threading.Thread(target=go)
        thread.start()
        for _ in range(100):
            current = self.store.get(self.item["id"])
            if current["state"] == "verifying":
                break
            time.sleep(0.01)
        self.store.control(current["id"], current["revision"], "pause")
        thread.join(10)
        self.assertFalse(errors)
        self.item = self.store.query(self.item["id"])
        self.assertTrue(self.item["paused"])
        self.assertEqual(self.item["state"], "submitted")
        self.item = self.store.control(self.item["id"], self.item["revision"], "resume")
        self.verify()
        self.assertEqual(self.item["state"], "addressed")

    def test_export_import_retains_every_feedback_evidence_hash(self):
        from yapnr.agent import workspace

        self.pass_it()
        archive = self.r / "workspace.tar.gz"
        workspace.export(self.project, archive)
        destination = self.r / "imported"
        workspace.import_archive(archive, destination)
        self.assertEqual(Feedback(destination).query(self.item["id"])["state"], "addressed")
        index = json.loads((destination / "workspace.json").read_text())
        self.assertEqual(index["feedback"], ".yapnr/feedback/manifest.json")
        self.assertEqual(Feedback(destination).log.read_bytes(), self.store.log.read_bytes())

    def test_concurrent_controller_cannot_duplicate_worker(self):
        argv = self.worker(always_half=True, delay=0.3)
        errors = []

        def go():
            try:
                self.store.run_loop(self.item["id"], argv, max_attempts=1, timeout=5)
            except Exception as e:
                errors.append(str(e))

        thread = threading.Thread(target=go)
        thread.start()
        for _ in range(100):
            if self.store.get(self.item["id"])["state"] == "implementing":
                break
            time.sleep(0.01)
        with self.assertRaises(Conflict):
            Feedback(self.project).run_loop(self.item["id"], argv, max_attempts=1, timeout=5)
        thread.join(10)
        self.assertFalse(errors)
        self.assertEqual((self.r / "worker-count").read_text(), "1")

    def test_partial_criterion_report_cannot_pass(self):
        v = self.project / "validator.py"
        v.write_text(
            v.read_text().replace(
                "print(json.dumps({'cases':cases}))",
                "print(json.dumps({'cases':cases[:1] if p['R5'][0]==0 else cases}))",
            )
        )
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)
        head = self.commit()
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertEqual(self.item["state"], "retry")

    def test_dirty_validator_checkout_cannot_pass(self):
        v = self.project / "validator.py"
        v.write_text(
            v.read_text().replace(
                "p=json.load(open(sys.argv[1]));cases=[]",
                "p=json.load(open(sys.argv[1]));cases=[]\nif p['R5'][0]==0: open('untracked.txt','w').write('dirty')",
            )
        )
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)
        head = self.commit()
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertEqual(self.item["state"], "retry")
        self.assertIn("dirty", self.item["verification"]["error"])

    def test_validator_timeout_never_closes(self):
        v = self.project / "validator.py"
        v.write_text(
            v.read_text().replace(
                "p=json.load(open(sys.argv[1]));cases=[]",
                "p=json.load(open(sys.argv[1]));cases=[]\nif p['R5'][0]==0: __import__('time').sleep(2)",
            )
        )
        self.definition["checks"][0]["timeout_seconds"] = 0.5
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)
        head = self.commit()
        self.dispatch()
        self.submit(head)
        self.verify()
        self.assertEqual(self.item["state"], "retry")
        self.assertFalse(self.item["verification"]["passed"])

    def test_declared_regression_guard_cannot_be_omitted(self):
        (self.project / "guard.py").write_text(
            "import json,sys\np=json.load(open(sys.argv[1]));ok=p['ok'];"
            "print(json.dumps({'cases':[{'id':'regression','passed':ok}]}));"
            "raise SystemExit(not ok)\n"
        )
        (self.project / "guard-baseline.json").write_text('{"ok":true}')
        self.definition["files"] += ["guard.py", "guard-baseline.json"]
        self.definition["criteria"].append(
            {"id": "regression", "check": "guard", "baseline": "pass"}
        )
        self.definition["checks"].append(
            {
                "id": "guard",
                "validator": "guard.py",
                "baseline": "guard-baseline.json",
                "argv": [sys.executable, "{validator}", "{candidate}"],
                "timeout_seconds": 5,
            }
        )
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)
        head = self.commit()
        self.dispatch()
        with self.assertRaises(ValueError):
            self.submit(head)
        (self.repo / "regressions.json").write_text('{"ok":false}')
        head = self.commit()
        self.submit(head, outputs={"placement": "after.json", "guard": "regressions.json"})
        self.verify()
        self.assertEqual(self.item["state"], "retry")
        self.assertFalse(self.item["verification"]["checks"][1]["passed"])

    def test_clarification_survives_source_replay_and_reaches_worker(self):
        original = self.item["text"]
        source = self.item["source"]
        self.item = self.store.control(
            self.item["id"], self.item["revision"], "clarify", "Which rotation?"
        )
        with self.assertRaises(ValueError):
            self.store.plan(self.item["id"], self.item["revision"], self.definition)
        self.item = self.store.control(
            self.item["id"], self.item["revision"], "answer", "Keep original cardinal rotation"
        )
        self.item = self.store.plan(self.item["id"], self.item["revision"], self.definition)
        rev = self.item["revision"]
        self.item = self.store.add(original, source, self.item["id"], rev)
        self.assertEqual(self.item["revision"], rev)
        self.assertEqual(self.item["state"], "ready")
        self.dispatch()
        task = json.loads(self.store.object(self.item["attempts"][-1]["bundle_sha256"]))
        self.assertEqual(task["clarifications"][-1]["answer"], "Keep original cardinal rotation")

    def test_superseded_worker_cannot_fail_new_attempt(self):
        argv = self.worker(delay=0.3)
        errors = []

        def go():
            try:
                self.store.run_loop(self.item["id"], argv, max_attempts=1, timeout=5)
            except Exception as e:
                errors.append(str(e))

        thread = threading.Thread(target=go)
        thread.start()
        for _ in range(100):
            current = self.store.get(self.item["id"])
            if current["state"] == "implementing":
                break
            time.sleep(0.01)
        changed = self.store.add(
            "New clarified geometry requirement",
            identity=current["id"],
            revision=current["revision"],
        )
        changed = self.store.plan(changed["id"], changed["revision"], self.definition)
        changed = self.store.dispatch(changed["id"], changed["revision"], "new-worker")
        new_attempt = changed["attempts"][-1]["id"]
        thread.join(10)
        self.assertFalse(errors)
        latest = self.store.query(changed["id"])
        self.assertEqual(latest["state"], "implementing")
        self.assertEqual(latest["attempts"][-1]["id"], new_attempt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
