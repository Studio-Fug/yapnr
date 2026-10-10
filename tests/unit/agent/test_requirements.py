"""Authoritative YAML traces, immutable report evidence and real review gates."""

import json
import tempfile
import unittest
from pathlib import Path

from rules_requirements._vendor import yaml

from yapnr.agent import requirements, reviews, workflow, workspace

MODEL = """project:
  name: Synthetic blinker
user_needs:
  - id: UN-1
    title: See the requested colors
requirements:
  - id: REQ-1
    title: Cycle three colors
    description: Cycle red, green and blue with 1 second dwell, within 10 percent.
    satisfies: [UN-1]
    method: simulation
    verified_by:
      - target: "record:simulation"
        cases: ["timing"]
risks:
  - id: RISK-1
    title: LED overcurrent
    hazard: Excess current
    hazardous_situation: LED overdriven
    harm: LED damage
    severity: low
    likelihood: possible
mitigations:
  - id: MIT-1
    title: Bound LED drive
    type: protective
    mitigates: [RISK-1]
    implemented_by: [REQ-1]
"""


class RequirementsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workflow.init(self.root, "Synthetic design")
        (self.root / "requirements/model.yaml").write_text(MODEL)

    def capture(self):
        receipt = {
            **workflow.query(self.root)["evidence_template"],
            "event": "requirements-specification-ready",
            "note": "Synthetic model captured",
            "requirements": ["REQ-1"],
        }
        path = self.root / "reports/ready.json"
        path.write_text(json.dumps(receipt))
        workflow.next_state(self.root, receipt["event"], "reports/ready.json")
        artifact = workspace.publish(
            self.root, "requirements/model.yaml", "document", "Requirements model"
        )
        reviews.request(self.root, [artifact["id"]], "requirements_review", 1)
        return artifact

    def test_yaml_is_authoritative_and_traces_are_unverified_without_evidence(self):
        view = requirements.review(self.root)
        self.assertEqual(
            {e["kind"] for e in view["entities"]},
            {"user_need", "requirement", "risk", "mitigation"},
        )
        req = next(e for e in view["entities"] if e["id"] == "REQ-1")
        self.assertEqual(req["status"], "UNVERIFIED")
        self.assertTrue(any(e["relation"] == "mitigates" for e in view["graph"]["edges"]))
        before = workflow.contract(self.root)
        (self.root / "requirements/manifest.md").write_text("Historical prose changed")
        self.assertEqual(workflow.contract(self.root), before)
        (self.root / "requirements/model.yaml").write_text(MODEL.replace("1 second", "2 seconds"))
        self.assertNotEqual(workflow.contract(self.root), before)

    def test_report_link_preserves_actual_result_and_staleness(self):
        (self.root / "reports/simulation.json").write_text('{"result":"passed","seed":42}\n')
        report = workspace.publish(
            self.root, "reports/simulation.json", "report", "Simulation result"
        )
        record = self.root / "requirements/evidence/timing.rr.yaml"
        stamp = requirements.review(self.root)["current_build"]
        record.write_text(
            yaml.safe_dump(
                {
                    "target": "record:simulation",
                    "evidence": [
                        {
                            "name": "timing",
                            "status": "passed",
                            "requirement": "REQ-1",
                            "level": "simulation",
                            "artifact": stamp,
                        }
                    ],
                }
            )
        )
        requirements.link_evidence(
            self.root,
            "requirements/evidence/timing.rr.yaml",
            report["id"],
            "timing",
            "record:simulation",
        )
        view = requirements.review(self.root)
        req = next(e for e in view["entities"] if e["id"] == "REQ-1")
        self.assertEqual(req["status"], "VERIFIED")
        self.assertIn(report["id"], {a["id"] for m in req["members"] for a in m["artifacts"]})
        (self.root / "requirements/model.yaml").write_text(MODEL.replace("1 second", "2 seconds"))
        req = next(e for e in requirements.review(self.root)["entities"] if e["id"] == "REQ-1")
        self.assertEqual(req["status"], "UNDER-VERIFIED")
        workspace.relative_file(self.root, report["path"]).write_text("Changed bytes")
        req = next(e for e in requirements.review(self.root)["entities"] if e["id"] == "REQ-1")
        self.assertEqual(req["status"], "FAILED")

    def test_question_and_feedback_block_approval_until_user_resolves(self):
        artifact = self.capture()
        note = reviews.feedback(self.root, artifact["id"], "Why this timing?", True)
        self.assertEqual(len(reviews.state(self.root)["items"][artifact["id"]]["questions"]), 1)
        with self.assertRaises(ValueError):
            reviews.approve(self.root, [artifact["id"]])
        self.assertIsNone(workflow.query(self.root)["accepted_revision"])
        reviews.resolve(self.root, artifact["id"], note["id"], note["rev"])
        reviews.approve(self.root, [artifact["id"]])
        self.assertEqual(workflow.query(self.root)["state"], "schematic")
        self.assertEqual(workflow.query(self.root)["accepted_revision"], 1)
        self.assertEqual(reviews.state(self.root)["items"][artifact["id"]]["status"], "approved")

    def test_approval_cannot_accept_model_changed_after_review_request(self):
        artifact = self.capture()
        (self.root / "requirements/model.yaml").write_text(MODEL.replace("1 second", "2 seconds"))
        with self.assertRaises(ValueError):
            reviews.approve(self.root, [artifact["id"]])
        self.assertIsNone(workflow.query(self.root)["accepted_revision"])

    def test_new_artifact_revision_keeps_unresolved_feedback_and_needs_new_approval(self):
        artifact = self.capture()
        reviews.feedback(self.root, artifact["id"], "Refine the tolerance")
        (self.root / "requirements/model.yaml").write_text(MODEL.replace("10 percent", "5 percent"))
        changed = workspace.publish(
            self.root, "requirements/model.yaml", "document", "Requirements model"
        )
        reviews.request(self.root, [changed["id"]], "requirements_review", 2)
        state = reviews.state(self.root)
        self.assertEqual(state["items"][artifact["id"]]["status"], "superseded")
        self.assertEqual(state["items"][changed["id"]]["status"], "feedback_pending")
        with self.assertRaises(ValueError):
            reviews.approve(self.root, [changed["id"]])


if __name__ == "__main__":
    unittest.main()
