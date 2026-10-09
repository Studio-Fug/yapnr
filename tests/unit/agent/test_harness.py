"""Harness advances idle sessions without inventing approvals or duplicating turns."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yapnr.agent import harness, workflow


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workflow.init(self.root, "Synthetic design")
        self.now = 1000.0
        self.status = {}
        self.questions = []
        self.permissions = []
        self.calls = []
        self.messages = [
            {
                "info": {"id": "msg_user", "role": "user", "time": {"created": 900000}},
                "parts": [{"type": "text", "text": "Design it"}],
            }
        ]
        self.manager = harness.Manager(
            lambda n: self.root,
            lambda n: "/projects/article",
            self.remote,
            lambda: ["article"],
            clock=lambda: self.now,
        )
        self.manager.arm("article", "ses_test", {"providerID": "test", "modelID": "fixture"})

    def remote(self, path, data=None, method=None):
        if data is not None:
            self.calls.append((path, data, method))
            if "/prompt_async" in path:
                self.messages.append(
                    {
                        "info": {
                            "id": "msg_native",
                            "role": "user",
                            "time": {"created": int(self.now * 1000)},
                        },
                        "parts": data["parts"],
                    }
                )
            return None
        if path.startswith("/session/status"):
            return self.status
        if path.startswith("/question"):
            return self.questions
        if path.startswith("/permission"):
            return self.permissions
        if "/message?" in path:
            return self.messages
        return {"id": "ses_test", "directory": "/projects/article"}

    def tick(self):
        self.manager.tick("article")
        self.manager.tick("article")

    def test_idle_continues_once_with_same_model_and_audited_synthetic_message(self):
        self.tick()
        posts = [d for p, d, _ in self.calls if "/prompt_async" in p]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["model"]["modelID"], "fixture")
        self.assertTrue(posts[0]["parts"][0]["metadata"]["yapnr_harness"])
        self.assertNotIn("messageID", posts[0])  # Let the runtime generate ordered native IDs.
        self.assertEqual(harness.read(self.root)["continuations"], 1)
        self.assertIn(
            "harness-request", (self.root / ".yapnr/workspace/conversation.jsonl").read_text()
        )

    def test_busy_and_pending_user_requests_never_get_extra_turns(self):
        self.status = {"ses_test": {"type": "busy"}}
        self.tick()
        self.assertEqual(self.calls, [])
        self.status = {}
        self.questions = [{"sessionID": "ses_test", "id": "que_fixture"}]
        self.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(harness.read(self.root)["status"], "waiting_question")
        self.questions = []
        self.permissions = [{"sessionID": "ses_test", "id": "per_fixture"}]
        self.tick()
        self.assertEqual(self.calls, [])

    def test_review_stops_and_publishes_both_documents_without_acceptance(self):
        state = workflow.query(self.root)
        state.update(state="requirements_review", revision=1)
        with patch.object(harness, "query", return_value=state):
            self.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(harness.read(self.root)["status"], "waiting_review")
        artifacts = json.loads((self.root / ".yapnr/workspace/artifacts.json").read_text())[
            "artifacts"
        ]
        self.assertEqual(
            {a["title"] for a in artifacts}, {"Requirements specification", "Risk analysis"}
        )
        self.assertIsNone(workflow.query(self.root)["accepted_revision"])

    def test_new_checkpoint_stops_busy_autonomous_turn_but_allows_user_review_reply(self):
        state = {**workflow.query(self.root), "state": "requirements_review", "revision": 1}
        self.status = {"ses_test": {"type": "busy"}}
        with patch.object(harness, "query", return_value=state):
            self.manager.tick("article")
            self.assertIn("/abort", self.calls[-1][0])
            self.calls.clear()
            self.manager.arm("article", "ses_test", {"providerID": "test", "modelID": "fixture"})
            self.manager.tick("article")
            self.assertEqual(self.calls, [])

    def test_stop_and_budgets_are_persistent_and_never_change_controller(self):
        self.manager.stop("article", "ses_test")
        self.tick()
        self.assertEqual(self.calls, [])
        self.manager.arm("article", "ses_test", {"providerID": "test", "modelID": "fixture"})
        self.now += 1801
        self.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(harness.read(self.root)["status"], "budget_reached")
        self.assertEqual(workflow.query(self.root)["state"], "requirements_capture")

    def test_missing_delivery_marker_pauses_instead_of_repeating_paid_request(self):
        self.manager.arm(
            "article",
            "ses_test",
            {"providerID": "test", "modelID": "fixture"},
            initial_request="uncertain",
        )
        self.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(harness.read(self.root)["status"], "delivery_uncertain")

    def test_idle_abandoned_tool_is_marked_interrupted_with_preserved_input(self):
        self.messages.append(
            {
                "info": {"id": "msg_old", "role": "assistant", "time": {"created": 800000}},
                "parts": [
                    {
                        "id": "prt_old",
                        "sessionID": "ses_test",
                        "messageID": "msg_old",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call_old",
                        "state": {
                            "status": "running",
                            "input": {"command": "yapnr workflow query", "timeout": 20000},
                            "time": {"start": 800000},
                        },
                    }
                ],
            }
        )
        self.tick()
        path, repaired, method = self.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(repaired["state"]["status"], "error")
        self.assertTrue(repaired["state"]["metadata"]["interrupted"])
        self.assertEqual(repaired["state"]["input"]["command"], "yapnr workflow query")
        self.assertNotIn("output", repaired["state"])

    def test_last_allowed_turn_finishes_before_kick_budget_pauses(self):
        value = harness.read(self.root)
        value.update(continuations=8)
        self.manager.write(self.root, value)
        self.status = {"ses_test": {"type": "busy"}}
        self.manager.tick("article")
        self.assertEqual(self.calls, [])
        self.assertEqual(harness.read(self.root)["status"], "running")
        self.status = {}
        self.tick()
        self.assertEqual(harness.read(self.root)["status"], "budget_reached")

    def test_imported_workspace_requires_explicit_resume(self):
        value = harness.read(self.root)
        value["workspace_identity"] = "another-workspace"
        self.manager.write(self.root, value)
        self.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(harness.read(self.root)["status"], "resume_required")

    def test_two_managers_share_claim_and_uncertain_post_does_not_retry(self):
        other = harness.Manager(
            lambda n: self.root,
            lambda n: "/projects/article",
            self.remote,
            lambda: [],
            clock=lambda: self.now,
        )
        value = harness.read(self.root)
        value.update(status="sending", last_request="not-yet-delivered")
        self.manager.write(self.root, value)
        other.tick("article")
        other.tick("article")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
