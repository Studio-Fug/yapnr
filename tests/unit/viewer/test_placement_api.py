"""Agent API exercises transport, decision provenance, controls and safe history paging."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from yapnr.placement import PlacementClient, PlacementError, register
from yapnr.viewer.notes.store import NotesStore
from yapnr.viewer.services.placement_api import PlacementAPI
from yapnr.viewer.services.placement_lab import PlacementLab, digest, write
from yapnr.viewer.testing import start_viewer, stop


def capture(lab, ident, distance=2):
    folder = lab.folder(ident)
    write(folder / "status.json", dict(state="complete", legal=True))
    write(folder / "request.json", dict(seed=0, source_sha256="a" * 64))
    path = folder / "captures/decision.json"
    write(
        path,
        dict(
            ref="C1",
            graph={
                "outline": {"width": 5, "height": 5},
                "components": [
                    {"ref": "C1", "pos": [distance, 0], "rot": 90, "bbox": [1, 1], "pads": []}
                ],
            },
            target=[0, 0],
            original_position=[0, 0],
            original_rotation=0,
            position=[distance, 0],
            rotation=90,
            stationary_neighbors=[],
            terms=[dict(key="target_displacement", weight=1, weighted=distance**2)],
            total=distance**2,
            candidate_fields=[],
        ),
    )
    write(
        folder / "review.json",
        dict(
            graph={},
            backtracks=0,
            motion=dict(sum_mm=distance, parts={"C1": [distance, 90]}),
            decisions=[dict(path=str(path), sha256=digest(path), ref="C1")],
        ),
    )
    return folder


class PlacementAPITest(unittest.TestCase):
    def test_cli_forwards_step_count_and_surface_parameters(self):
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers())
        cases = [
            (["step", "demo", "--count", "3"], "step", dict(id="demo", count=3)),
            (
                ["surface", "demo", "--ref", "C1", "--expression", "2*wirelength", "--pitch", "2"],
                "surface",
                dict(id="demo", ref="C1", expression="2*wirelength", pitch=2.0),
            ),
        ]
        for command, action, fields in cases:
            args = parser.parse_args(["placement", "--url", "http://localhost", *command])
            with patch(
                "yapnr.placement.PlacementClient.call", return_value={}
            ) as call, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
                call.assert_called_once_with(action, **fields)

    def test_decision_notes_are_hash_bound_and_never_user_authored_or_approved(self):
        with tempfile.TemporaryDirectory() as td:
            lab = PlacementLab(td, td)
            capture(lab, "a")
            capture(lab, "b", 1)
            notes = NotesStore(Path(td) / "notes")
            api = PlacementAPI(lab, notes)
            result = api.call(dict(action="decision", id="a", ref="C1"))["result"]
            self.assertEqual(result["step"], 0)
            svg = api.call(dict(action="render", id="a", step=0))["result"]
            self.assertIn("translate(2.0 -0.0)", svg["svg"])
            self.assertEqual(svg["decision_sha256"], result["decision"]["decision_sha256"])
            with self.assertRaises(ValueError):
                api.call(dict(action="decision", id="a", ref="C1", step=0))
            note = api.call(dict(action="note", id="a", step=0, body="Reduce movement"))["result"][
                "note"
            ]
            self.assertEqual(note["author"], "agent")
            self.assertEqual(note["status"], "open")
            self.assertEqual(
                note["provenance"]["phase"], "legalization:" + result["decision"]["decision_sha256"]
            )
            api.call(dict(action="reply", id="a", note=note["id"], body="Investigating"))
            with self.assertRaises(ValueError):
                api.call(dict(action="reply", id="b", note=note["id"], body="wrong run"))
            stored = api.call(dict(action="notes", id="a", ref="C1"))["result"]["notes"][0]
            self.assertEqual(stored["comments"][0]["author"], "agent")
            for bad in (dict(status="approved"), dict(actor="user"), dict(provenance={})):
                with self.assertRaises(ValueError):
                    api.call(dict(action="note", id="a", step=0, body="x", **bad))
            diff = api.call(dict(action="compare", id="a", other="b"))["result"]
            self.assertEqual(diff["delta_translation_mm"], 1)
            self.assertTrue(diff["same_source"])
            rows = api.call(dict(action="decisions", id="a"))["result"]["decisions"]
            self.assertEqual(rows[0]["translation_mm"], 2)
            with self.assertRaises(ValueError):
                api.call(dict(action="evidence", id="a", name="../../private"))

    def test_history_paging_does_not_drop_partial_writes_or_accept_arbitrary_offsets(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            path = folder / "iterations.jsonl"
            one = b'{"step":0}\n'
            path.write_bytes(one + b'{"step":')
            first = PlacementAPI.history(folder, 0, 1)
            self.assertEqual(first["events"], [{"step": 0}])
            cursor = first["cursor"]
            self.assertEqual(cursor, len(one))
            self.assertEqual(PlacementAPI.history(folder, cursor, 1)["cursor"], cursor)
            with path.open("ab") as f:
                f.write(b"1}\n")
            second = PlacementAPI.history(folder, cursor, 1)
            self.assertEqual(second["events"], [{"step": 1}])
            self.assertFalse(second["more"])
            for bad in (1, -1, 1000, True):
                with self.assertRaises(ValueError):
                    PlacementAPI.history(folder, bad, 1)

    def test_sources_are_content_addressed_and_stale_controls_cannot_overwrite_edits(self):
        with tempfile.TemporaryDirectory() as td:
            lab = PlacementLab(td, td)
            api = PlacementAPI(lab, None)
            doc = dict(title="Demo", graph=dict(components=[dict(ref="C1")]), constraints={})
            first = api.call(dict(action="register-source", document=doc))["result"]
            second = api.call(dict(action="register-source", document=doc))["result"]
            self.assertEqual(first, second)
            source = api.call(dict(action="source", source=first["id"]))["result"]
            self.assertEqual(source["sha256"], first["sha256"])
            with self.assertRaises(ValueError):
                api.call(dict(action="register-source", document=dict(doc, constraints=None)))
            folder = capture(lab, "running")
            write(
                folder / "control.json",
                dict(expression="source_constraints", revision=2, paused=False),
            )
            lab.active, lab.process = "running", MagicMock()
            lab.process.poll.return_value = None
            with self.assertRaisesRegex(ValueError, "Stale"):
                api.call(
                    dict(
                        action="apply",
                        id="running",
                        expression="wirelength+source_constraints",
                        expect_revision=1,
                    )
                )
            self.assertEqual(json.loads((folder / "control.json").read_text())["revision"], 2)
            result = api.call(dict(action="pause", id="running", expect_revision=2))["result"]
            self.assertEqual(result["control"]["revision"], 3)
            self.assertTrue(result["control"]["paused"])

    def test_real_http_client_contract_and_origin_guard(self):
        with tempfile.TemporaryDirectory() as td:
            lab = PlacementLab(td, td)
            capture(lab, "demo")
            process, base = start_viewer(td, "--agent", "off", "--net-summaries", "off")
            try:
                client = PlacementClient(base)
                parser = argparse.ArgumentParser()
                register(parser.add_subparsers())
                args = parser.parse_args(["placement", "--url", base, "status", "demo"])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(args.func(args), 0)
                self.assertEqual(json.loads(output.getvalue())["id"], "demo")
                self.assertIn("decision", client.call("schema")["actions"])
                self.assertEqual(client.status("demo")["status"]["state"], "complete")
                self.assertEqual(client.wait("demo", seconds=1)["status"]["state"], "complete")
                self.assertEqual(client.decision("demo", ref="C1")["step"], 0)
                self.assertIn("step=0", client.link("demo", step=0))
                with self.assertRaisesRegex(PlacementError, "HTTP 400"):
                    client.call("approve", id="demo")
                req = Request(
                    base + "/api/placement-lab/v1",
                    data=b'{"action":"catalog"}',
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(req, timeout=5)
                self.assertEqual(caught.exception.code, 403)
                with patch(
                    "yapnr.placement.PlacementClient.status",
                    return_value={"status": {"state": "running"}},
                ):
                    with self.assertRaisesRegex(PlacementError, "has not been stopped"):
                        client.wait("demo", seconds=0.01)
                self.assertEqual(client.status("demo")["status"]["state"], "complete")
            finally:
                stop(process)


if __name__ == "__main__":
    unittest.main()
