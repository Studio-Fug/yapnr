"""Failure review provenance, persistence, concurrency and authority."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from yapnr.viewer.services.connection_review import Conflict, ConnectionReview
from yapnr.viewer.testing import start_viewer, stop


class ReviewTest(unittest.TestCase):
    def setup_store(self, root):
        r = Path(root)
        (r / "geometry").mkdir(exist_ok=True)
        geometry = dict(
            frame="mm-y-up",
            width=20,
            height=20,
            copper_layers=["F.Cu", "B.Cu"],
            parts=[
                dict(
                    ref="A",
                    xy=[4, 5],
                    pads=[dict(number="1", net="signal", xy=[4, 5], size=[1, 1], layers=["F.Cu"])],
                ),
                dict(
                    ref="B",
                    xy=[8, 5],
                    pads=[dict(number="1", net="signal", xy=[8, 5], size=[1, 1], layers=["F.Cu"])],
                ),
            ],
            tracks=[],
            vias=[],
            zones=[],
            airwires=[dict(net="signal", a=[4, 5], b=[8, 5])],
        )
        (r / "geometry" / ("a" * 64 + ".json")).write_text(json.dumps(geometry))
        lanes = {
            "run/one": dict(
                frames=[
                    dict(
                        name="09-final-audit",
                        board_sha256="a" * 64,
                        event_id="e1",
                        opens=1,
                        violations=0,
                    )
                ]
            )
        }
        store = ConnectionReview(r)
        return store, lanes

    def test_clarification_survives_review_replay_and_replanning(self):
        from yapnr.feedback import Feedback

        with tempfile.TemporaryDirectory() as td:
            review, lanes = self.setup_store(td)
            card = review.scan(lanes)["cards"][0]
            card = review.decide(card["id"], card["revision"], "different", "Move both parts")
            store = Feedback(td)
            item = card["resolution"]
            item = store.control(
                item["id"], item["revision"], "clarify", "Keep the fixed interface?"
            )
            item = store.control(
                item["id"], item["revision"], "answer", "Yes, preserve the connector"
            )
            root = Path(td)
            (root / "validator.py").write_text(
                'import json; print(json.dumps({"cases":[{"id":"move","passed":False}]})); raise SystemExit(1)'
            )
            (root / "before.json").write_text("{}")
            (root / "before.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><text>Before</text></svg>'
            )
            plan = dict(
                repository="https://github.com/example/engine.git",
                files=["validator.py", "before.json", "before.svg"],
                before_visual="before.svg",
                settings={"seed": 0},
                criteria=[dict(id="move", check="geometry", baseline="fail")],
                checks=[
                    dict(
                        id="geometry",
                        validator="validator.py",
                        baseline="before.json",
                        argv=[sys.executable, "{validator}", "{candidate}"],
                        timeout_seconds=10,
                    )
                ],
            )
            item = store.plan(item["id"], item["revision"], plan)
            replayed = ConnectionReview(td).card(card["id"])["resolution"]
            self.assertEqual(replayed["revision"], item["revision"])
            self.assertEqual(replayed["state"], "ready")
            self.assertEqual(replayed["answer"], "Yes, preserve the connector")
            item = store.dispatch(item["id"], item["revision"], "worker")
            task = json.loads(store.object(item["attempts"][-1]["bundle_sha256"]))
            self.assertEqual(task["clarifications"][0]["answer"], "Yes, preserve the connector")

    def test_dedup_and_board_revision_are_independent(self):
        with tempfile.TemporaryDirectory() as td:
            s, lanes = self.setup_store(td)
            self.assertEqual(s.scan(lanes)["added"], 1)
            self.assertEqual(s.scan(lanes)["added"], 0)
            self.assertEqual(s.scan(lanes, "another")["total"], 0)
            original = s.list()["cards"][0]
            p = Path(td) / "geometry"
            (p / ("b" * 64 + ".json")).write_bytes((p / ("a" * 64 + ".json")).read_bytes())
            lanes["run/one"]["frames"][0]["board_sha256"] = "b" * 64
            self.assertEqual(s.scan(lanes)["added"], 1)
            self.assertEqual(s.card(original["id"])["board_sha256"], "a" * 64)

    def test_review_reload_undo_and_stale_annotation(self):
        with tempfile.TemporaryDirectory() as td:
            s, lanes = self.setup_store(td)
            c = s.scan(lanes)["cards"][0]
            c = s.annotate(
                c["id"],
                c["revision"],
                dict(hypothesis="A backside via may fit", confidence="medium"),
            )
            reviewed = s.decide(
                c["id"], c["revision"], "different", "Move the upper part", [[3, 4, 5, 6]]
            )
            s = ConnectionReview(td)
            self.assertEqual(s.card(c["id"])["decision"]["rectangles"], [[3, 4, 5, 6]])
            with self.assertRaises(Conflict):
                s.decide(c["id"], c["revision"], "agree")
            changed = s.annotate(
                c["id"], reviewed["revision"], dict(hypothesis="Rotate the upper part")
            )
            self.assertEqual(changed["review_state"], "outdated")
            undone = s.decide(c["id"], changed["revision"], "undo")
            self.assertEqual(undone["review_state"], "unreviewed")
            self.assertEqual(
                [e["actor"] for e in s.events()], ["engine", "agent", "user", "agent", "user"]
            )

    def test_unknown_highlights_and_agent_verdicts_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            s, lanes = self.setup_store(td)
            c = s.scan(lanes)["cards"][0]
            for annotation in (
                dict(refs=["MISSING"]),
                dict(nets=["missing"]),
                dict(bounds=[0, 0, 0, 1]),
                dict(confidence="certain"),
                dict(bounds=[0, 0, float("nan"), 2]),
            ):
                with self.assertRaises(ValueError):
                    s.annotate(c["id"], c["revision"], annotation)
            for action in ("agree", "decision", "approve"):
                with self.assertRaises(ValueError):
                    s.call(dict(action=action, id=c["id"]))
            with self.assertRaises(ValueError):
                s.decide(c["id"], c["revision"], "different")
            with self.assertRaises(ValueError):
                s.decide(c["id"], c["revision"], "agree")
            with self.assertRaises(ValueError):
                s.geometry("../private")

    def test_agent_response_preserves_user_verdict_and_hypothesis_revision(self):
        with tempfile.TemporaryDirectory() as td:
            s, lanes = self.setup_store(td)
            c = s.scan(lanes)["cards"][0]
            c = s.annotate(c["id"], c["revision"], dict(hypothesis="Inspect escape"))
            c = s.decide(c["id"], c["revision"], "agree", "Restore all severed connections")
            answered = s.call(
                dict(
                    action="respond",
                    id=c["id"],
                    expect_revision=c["revision"],
                    body="Added a native restoration regression",
                    status="tested",
                )
            )
            self.assertEqual(answered["review_state"], "agree")
            self.assertEqual(answered["definition_revision"], c["definition_revision"])
            self.assertEqual(answered["decision"], c["decision"])
            self.assertEqual(s.card(c["id"])["followup"]["status"], "tested")
            self.assertEqual(s.card(c["id"])["resolution"]["state"], "needs_plan")
            with self.assertRaises(Conflict):
                s.respond(c["id"], c["revision"], "Stale response", "working")

    def test_torn_last_line_does_not_erase_acknowledged_history(self):
        with tempfile.TemporaryDirectory() as td:
            s, lanes = self.setup_store(td)
            c = s.scan(lanes)["cards"][0]
            with s.log.open("ab") as f:
                f.write(b'{"partial":')
            s.annotate(c["id"], c["revision"], dict(hypothesis="Inspect escape"))
            self.assertEqual(len(s.events()), 2)
            self.assertEqual(s.list()["total"], 1)

    def test_http_origin_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            s, lanes = self.setup_store(td)
            c = s.scan(lanes)["cards"][0]
            process, base = start_viewer(td, "--agent", "off", "--net-summaries", "off")
            try:

                def request(body, origin=True, path="/api/connection-review/v1"):
                    headers = {"Content-Type": "application/json"}
                    if origin:
                        headers["Origin"] = base
                    with urlopen(
                        Request(base + path, data=json.dumps(body).encode(), headers=headers),
                        timeout=10,
                    ) as r:
                        return json.load(r)

                with self.assertRaises(HTTPError) as ex:
                    request(dict(action="list"), False)
                self.assertEqual(ex.exception.code, 403)
                got = request(
                    dict(
                        action="annotate",
                        id=c["id"],
                        expect_revision=c["revision"],
                        annotation=dict(hypothesis="Inspect escape"),
                    )
                )
                with self.assertRaises(HTTPError) as ex:
                    request(
                        dict(id=c["id"], expect_revision=c["revision"], verdict="agree"),
                        path="/api/connection-review/decision",
                    )
                self.assertEqual(ex.exception.code, 409)
                self.assertEqual(
                    request(dict(action="card", id=c["id"]))["revision"], got["revision"]
                )
                with self.assertRaises(HTTPError) as ex:
                    request(dict(action="query"), False, path="/api/feedback/v1")
                self.assertEqual(ex.exception.code, 403)
                feedback = request(
                    dict(action="add", text="Move the blocking component"), path="/api/feedback/v1"
                )
                self.assertEqual(feedback["state"], "needs_plan")
                with self.assertRaises(HTTPError) as ex:
                    request(
                        dict(action="run", id=feedback["id"], worker_command=["false"]),
                        path="/api/feedback/v1",
                    )
                self.assertEqual(ex.exception.code, 400)
                with urlopen(base + "/connection-review.html", timeout=10) as response:
                    page = response.read()
                    self.assertIn(b"Different finding", page)
                    self.assertIn(b"Engineering resolution", page)
            finally:
                stop(process)


if __name__ == "__main__":
    unittest.main()
