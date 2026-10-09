"""Shared workspace HTTP integration, offline and without model turns."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from yapnr.agent import web, workspace


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        data = b"<html><head></head><body>OpenCode</body></html>"
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "first=1; HttpOnly")
        self.send_header("Set-Cookie", "second=2; HttpOnly")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.projects = Path(self.temp.name).resolve()
        self.root = self.projects / "article"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("Synthetic offline workspace test\n")
        self.origin = "https://workspace.example.test"
        self.upstream = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Upstream))
        self.server = self.start(
            web.Server(
                ("127.0.0.1", 0),
                f"http://127.0.0.1:{self.upstream.server_port}",
                self.projects,
                self.origin,
            )
        )

    def start(self, server):
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
        )
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        return server

    def request(self, path, data=None, origin=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = self.origin
        connection.request(
            "POST" if data is not None else "GET",
            path,
            json.dumps(data) if data is not None else None,
            headers,
        )
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()

    def test_proxy_preserves_both_auth_cookies_without_injecting_ui(self):
        status, headers, body = self.request("/upstream-resource")
        self.assertEqual(status, 200)
        self.assertEqual(
            [v for k, v in headers if k.lower() == "set-cookie"],
            ["first=1; HttpOnly", "second=2; HttpOnly"],
        )
        self.assertNotIn(b"/yapnr/workspace.js", body)

    def test_workspace_shell_owns_root_and_session_navigation(self):
        for path in ("/", "/workspace/article", "/workspace/article/session/ses_test"):
            status, _, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(b'id="chat"', body)
            self.assertIn(b'id="work"', body)
            self.assertNotIn(b"<body>OpenCode", body)

    def test_new_project_initializes_workflow_and_cannot_overwrite(self):
        data = {"name": "new-article", "directive": "A synthetic test article"}
        self.assertEqual(self.request("/yapnr/api/create-project", data, origin=False)[0], 400)
        self.assertEqual(self.request("/yapnr/api/create-project", data)[0], 200)
        self.assertEqual(web.query(self.projects / "new-article")["state"], "requirements_capture")
        self.assertEqual(self.request("/yapnr/api/create-project", data)[0], 400)
        self.assertEqual(self.request("/yapnr/api/create-project", {"name": "../outside"})[0], 400)

    def test_shared_note_write_requires_origin_and_is_visible_to_viewer_store(self):
        fields = {"title": "Finding", "body": "Synthetic observation"}
        self.assertEqual(self.request("/yapnr/api/note/article", fields, origin=False)[0], 400)
        status, _, body = self.request("/yapnr/api/note/article", fields)
        self.assertEqual(status, 200)
        note = json.loads(body)
        self.assertEqual(note["status"], "open")
        self.assertEqual(web.threads.notes(self.root).all()[0]["id"], note["id"])
        self.assertTrue((self.root / ".yapnr/workspace/conversation.jsonl").is_file())

    def test_review_uses_explicit_model_and_rejects_cross_project_sessions(self):
        info = {"directory": "/projects/article"}
        with patch.object(web, "api", side_effect=[info, [], None]) as remote:
            status, _, _ = self.request(
                "/yapnr/api/review/article",
                {
                    "session": "ses_test",
                    "text": "Review refinement",
                    "model": {"providerID": "test-provider", "modelID": "test-model"},
                },
            )
            self.assertEqual(status, 200)
            request = remote.call_args_list[-1].args[2]
            self.assertEqual(request["model"]["modelID"], "test-model")
            self.assertIn("prompt_async", remote.call_args_list[-1].args[1])
        with patch.object(web, "api", return_value={"directory": "/projects/other"}) as remote:
            self.assertEqual(
                self.request(
                    "/yapnr/api/review/article", {"session": "ses_test", "text": "Finding"}
                )[0],
                400,
            )
            self.assertEqual(remote.call_count, 1)

    def test_scene_assets_are_pinned_and_integrity_checked(self):
        (self.root / "scene.js").write_text("export function build() {}\n")
        item = workspace.publish(self.root, "scene.js", "scene", "Synthetic scene", {"seed": 42})
        status, headers, body = self.request("/yapnr/scene/article/" + item["id"])
        self.assertEqual(status, 200)
        self.assertRegex(body.decode(), r"Number\([\"']42[\"']\)")
        self.assertIn(b"yapnr-ready", body)
        self.assertTrue(any(k.lower() == "content-security-policy" for k, _ in headers))
        status, _, _ = self.request("/yapnr/vendor/three.module.js")
        self.assertEqual(status, 200)
        (self.root / item["path"]).write_text("corrupt")
        self.assertEqual(self.request("/yapnr/scene/article/" + item["id"])[0], 400)


if __name__ == "__main__":
    unittest.main()
