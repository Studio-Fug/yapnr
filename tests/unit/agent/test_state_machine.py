"""Real persistent transitions, stale-evidence rejection and acceptance gates."""

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from yapnr.agent import workflow


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workflow.init(self.root, "Design a ring")
        (
            self.root / "requirements/model.yaml"
        ).unlink()  # Retain explicit legacy Markdown migration coverage.

    def documents(self, text="Initial"):
        (self.root / "requirements/manifest.md").write_text(text + "\nREQ-1: native connectivity\n")
        (self.root / "requirements/risks.md").write_text(
            text + "\nRisk: unintended disconnection\n"
        )

    def artifact(self, path="design/board.kicad_pcb", content="test board"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {"path": path, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    def receipt(self, event, **fields):
        current = workflow.query(self.root)
        data = {
            "schema": "yapnr-workflow-evidence-v1",
            "event": event,
            "revision": current["revision"],
            "contract": workflow.contract(self.root),
            "source": "agent",
            "note": "Test evidence",
            **fields,
        }
        path = "reports/receipt.json"
        (self.root / path).write_text(json.dumps(data))
        return path

    def advance(self, event, **fields):
        return workflow.next_state(self.root, event, self.receipt(event, **fields))

    def start(self, speculative=False):
        self.documents()
        self.advance("requirements-specification-ready", requirements=["REQ-1"])
        if speculative:
            self.advance("speculative-work-started")
        else:
            self.advance(
                "user-accepted-requirements-specification", source="user", reviewer="designer"
            )

    def route(self):
        artifact = self.artifact()
        self.advance("schematic-ready", artifacts=[artifact])
        self.advance("routing-finished", artifacts=[artifact])
        return artifact

    def verdict(self, artifact):
        return {
            "artifacts": [self.artifact("reports/native.json", '{"status":"pass"}')],
            "final_artifact_sha256": artifact["sha256"],
            "checks": [
                {"requirement_id": "REQ-1", "status": "pass", "artifact_sha256": artifact["sha256"]}
            ],
            "gates": {
                key: {"status": "pass", "artifact_sha256": artifact["sha256"]}
                for key in ("native_drc", "connectivity")
            },
        }

    def test_resume_preserves_history_and_project(self):
        self.start()
        before = workflow.query(self.root)
        workflow.init(self.root, "Different directive")
        after = workflow.query(self.root)
        self.assertEqual(before, after)
        self.assertTrue((self.root / ".git").is_dir())

    def test_query_does_not_bootstrap_missing_project(self):
        missing = self.root / "missing"
        with self.assertRaises(ValueError):
            workflow.query(missing)
        self.assertFalse(missing.exists())

    def test_query_reports_state_when_a_contract_document_is_missing(self):
        self.start()
        (self.root / "requirements/risks.md").unlink()
        state = workflow.query(self.root)
        self.assertEqual(state["state"], "schematic")
        self.assertFalse(state["contract_current"])
        self.assertIsNone(state["evidence_template"]["contract"])

    def test_acceptance_requires_user_and_rejects_old_receipt_without_mutation(self):
        self.start(speculative=True)
        before = workflow.query(self.root)
        with self.assertRaises(ValueError):
            self.advance("user-accepted-requirements-specification", reviewer="designer")
        path = self.receipt(
            "user-accepted-requirements-specification", source="user", reviewer="designer"
        )
        receipt = json.loads((self.root / path).read_text())
        receipt["revision"] -= 1
        (self.root / path).write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            workflow.next_state(self.root, receipt["event"], path)
        self.assertEqual(before, workflow.query(self.root))

    def test_failure_repair_loop_preserves_content_addressed_evidence(self):
        self.start()
        self.route()
        report = self.artifact("reports/failure.json", '{"failure":"coupling"}')
        self.advance("verification-failed", artifacts=[report])
        self.advance("engine-repair-required", artifacts=[report])
        state = self.advance(
            "engine-repair-ready", artifacts=[self.artifact(content="repaired engine inputs")]
        )
        self.assertEqual(state["state"], "schematic")
        artifact = self.route()
        state = self.advance("verification-passed", **self.verdict(artifact))
        self.assertEqual(state["state"], "complete")
        store = self.root / ".yapnr/workflow/evidence"
        self.assertEqual((store / report["sha256"]).read_text(), '{"failure":"coupling"}')

    def test_templates_and_skipping_stages_are_rejected(self):
        with self.assertRaises(ValueError):
            self.advance("requirements-specification-ready", requirements=["REQ-1"])
        with self.assertRaises(ValueError):
            self.advance("routing-finished", artifacts=[self.artifact()])
        self.assertEqual(workflow.query(self.root)["revision"], 0)

    def test_speculative_route_cannot_complete_until_current_revision_accepted(self):
        self.start(speculative=True)
        artifact = self.route()
        verdict = self.verdict(artifact)
        with self.assertRaises(ValueError):
            self.advance("verification-passed", **verdict)
        self.advance("user-accepted-requirements-specification", source="user", reviewer="designer")
        state = self.advance("verification-passed", **verdict)
        self.assertEqual(state["state"], "complete")
        self.assertFalse(state["speculative"])

    def test_revision_restarts_review_and_invalidates_artifacts_and_acceptance(self):
        self.start()
        self.route()
        old = workflow.query(self.root)
        self.documents("Changed geometry")
        self.assertFalse(workflow.query(self.root)["contract_current"])
        with self.assertRaises(ValueError):
            self.advance("verification-passed", **self.verdict(self.artifact()))
        new = self.advance(
            "user-revised-requirements-specification",
            source="user",
            reviewer="designer",
            requirements=["REQ-1", "REQ-2"],
        )
        self.assertEqual(new["revision"], old["revision"] + 1)
        self.assertEqual(new["state"], "requirements_review")
        self.assertIsNone(new["accepted_revision"])
        self.assertEqual(new["artifacts"], [])
        self.assertGreater(len(new["history"]), len(old["history"]))

    def test_missing_stale_and_mixed_artifact_verification_is_rejected(self):
        self.start()
        artifact = self.route()
        for field, value in (("checks", []), ("gates", {}), ("final_artifact_sha256", "wrong")):
            verdict = self.verdict(artifact)
            verdict[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.advance("verification-passed", **verdict)
        (self.root / artifact["path"]).write_text("modified copper")
        with self.assertRaises(ValueError):
            self.advance("verification-passed", **self.verdict(artifact))

    def test_resume_requires_user_receipt_and_parser_disallows_ambiguous_events(self):
        self.start()
        self.advance("blocked")
        with self.assertRaises(ValueError):
            self.advance("resume")
        self.assertEqual(
            self.advance("resume", source="user", reviewer="designer")["state"], "schematic"
        )
        parser = argparse.ArgumentParser()
        workflow.register(parser.add_subparsers())
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["workflow", "next", "--blocked", "--cancelled", "--evidence", "x.json"]
            )

    def test_revision_during_cancellation_preserves_stop_and_resumes_into_review(self):
        self.start()
        self.advance("blocked")
        self.advance("cancelled")
        self.documents("New requirements while stopped")
        state = self.advance(
            "user-revised-requirements-specification",
            source="user",
            reviewer="designer",
            requirements=["REQ-1"],
        )
        self.assertEqual(state["state"], "cancelled")
        self.assertEqual(state["resume_state"], "requirements_review")
        state = self.advance("resume", source="user", reviewer="designer")
        self.assertEqual(state["state"], "requirements_review")
        self.assertTrue(state["speculative"])


if __name__ == "__main__":
    unittest.main()
