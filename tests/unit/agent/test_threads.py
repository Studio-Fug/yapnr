"""Conversation elevation preserves notes, context and authority without paid turns."""

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.agent import threads, workspace

FOCUS = "01234567-89ab-cdef-0123-456789abcdef"


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "article"
        self.root.mkdir()
        self.calls = []
        self.directory = "/projects/article"
        self.turn = {
            "message": "Check U1",
            "answer": "Review its supply range",
            "selection": [{"kind": "component", "ref": "U1"}],
            "tools": [{"name": "read", "full": "datasheet evidence"}],
            "session": FOCUS,
            "owned": True,
            "cli_session": "private-backend-id",
            "turn": 1,
        }
        folder = self.root / "notes/conversations"
        folder.mkdir(parents=True)
        (folder / (FOCUS + ".jsonl")).write_bytes(
            workspace.encoded(self.turn).replace(b"\n", b" ") + b"\n"
        )

    def api(self, path, data=None):
        self.calls.append((path, data))
        if "/message?" in path:
            return (
                {"info": {"id": "msg_note"}}
                if data
                else [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "Design it"}]}]
            )
        return {"id": "ses_main", "directory": self.directory, "title": "Main"}

    def test_focused_thread_retains_tools_and_selection_without_backend_identity(self):
        listing = threads.focused(self.root)
        self.assertEqual(listing[0]["turn_count"], 1)
        record = threads.focused(self.root, FOCUS)
        self.assertEqual(record["turns"][0]["selection"], self.turn["selection"])
        self.assertEqual(record["turns"][0]["tools"], self.turn["tools"])
        self.assertNotIn("cli_session", record["turns"][0])

    def test_elevated_note_is_shared_and_immutable_and_never_runs_model(self):
        note = threads.save_note(
            self.root,
            self.api,
            "article",
            {
                "title": "Supply issue",
                "body": "Verify the supply range",
                "thread": {"kind": "focused", "id": FOCUS},
            },
        )
        result = threads.attach_note(
            self.root,
            self.api,
            "article",
            {"note": note["id"], "revision": note["rev"], "session": "ses_main"},
        )
        snapshot = json.loads((self.root / result["snapshot"]).read_bytes())
        self.assertEqual(snapshot["note"]["status"], "open")
        original = json.loads((self.root / snapshot["source"]["transcript"]).read_bytes())
        self.assertEqual(original["turns"][0]["tools"], self.turn["tools"])
        posts = [data for _, data in self.calls if data]
        self.assertEqual(len(posts), 1)
        self.assertIs(posts[0]["noReply"], True)
        self.assertTrue(all("prompt_async" not in path for path, _ in self.calls))
        self.assertEqual(threads.notes(self.root).all()[0]["id"], note["id"])
        self.assertEqual(threads.notes(self.root).all()[0]["status"], "open")

    def test_legacy_viewer_note_elevates_with_its_focused_transcript(self):
        note = threads.notes(self.root).create(
            {
                "title": "Ask finding",
                "body": "Check supply",
                "provenance": {"session": FOCUS, "lane": "lane1"},
            },
            {"kind": "user"},
        )
        result = threads.attach_note(
            self.root,
            self.api,
            "article",
            {"note": note["id"], "revision": note["rev"], "session": "ses_main"},
        )
        record = json.loads((self.root / result["snapshot"]).read_bytes())
        self.assertEqual(record["source"]["thread"]["id"], FOCUS)
        self.assertEqual(record["note"]["scope"], {"kind": "lane", "lane": "lane1"})

    def test_stale_note_and_foreign_session_do_not_attach(self):
        note = threads.notes(self.root).create(
            {"title": "Finding", "body": "Evidence"}, {"kind": "user"}
        )
        for directory, revision in (
            ("/projects/other", note["rev"]),
            ("/projects/article", note["rev"] + 1),
        ):
            self.directory = directory
            with self.assertRaises(ValueError):
                threads.attach_note(
                    self.root,
                    self.api,
                    "article",
                    {"note": note["id"], "revision": revision, "session": "ses_main"},
                )
        self.assertFalse(any(data for _, data in self.calls))

    def test_local_runtime_directory_mapping_preserves_workspace_scope(self):
        self.directory = str(self.root)
        result = threads.transcript(
            self.root, self.api, "article", "opencode", "ses_main", self.directory
        )
        self.assertEqual(result["id"], "ses_main")
        self.assertIn("directory=", self.calls[0][0])
        self.directory = str(self.root.parent / "other")
        with self.assertRaises(ValueError):
            threads.transcript(
                self.root, self.api, "article", "opencode", "ses_main", str(self.root)
            )

    def test_corrupt_transcript_and_symlink_are_rejected(self):
        note = threads.save_note(
            self.root,
            self.api,
            "article",
            {"title": "Finding", "body": "Evidence", "thread": {"kind": "focused", "id": FOCUS}},
        )
        registry = json.loads((self.root / ".yapnr/workspace/note-threads.json").read_bytes())
        (self.root / registry[note["id"]]["transcript"]).write_text("corrupt")
        with self.assertRaises(ValueError):
            threads.attach_note(
                self.root,
                self.api,
                "article",
                {"note": note["id"], "revision": note["rev"], "session": "ses_main"},
            )
        (self.root / "notes/notes.jsonl").unlink()
        (self.root / "notes/notes.jsonl").symlink_to(self.root.parent / "external")
        with self.assertRaises(ValueError):
            threads.notes(self.root)

    def test_archives_preserve_note_thread_link_and_attachments(self):
        note = threads.save_note(
            self.root,
            self.api,
            "article",
            {"title": "Finding", "body": "Evidence", "thread": {"kind": "focused", "id": FOCUS}},
        )
        workspace.export(self.root, self.root.parent / "article.tar.gz")
        restored = self.root.parent / "restored"
        manifest = workspace.import_archive(self.root.parent / "article.tar.gz", restored)
        self.assertEqual(manifest["notes"], "notes/notes.jsonl")
        self.assertEqual(threads.notes(restored).all()[0]["id"], note["id"])
        result = threads.attach_note(
            restored,
            self.api,
            "article",
            {"note": note["id"], "revision": note["rev"], "session": "ses_main"},
        )
        self.assertTrue((restored / result["snapshot"]).is_file())


if __name__ == "__main__":
    unittest.main()
