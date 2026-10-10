"""Shared workspace HTTP integration, offline and without model turns."""

import concurrent.futures
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
    def test_viewer_selection_query_does_not_corrupt_api_routes(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"lanes":{}}'
        with patch.object(web.urllib.request, "urlopen", return_value=response) as remote:
            self.assertEqual(
                web.api("https://viewer.example.test/?lane=selected#view", "/api/state"),
                {"lanes": {}},
            )
        self.assertEqual(remote.call_args.args[0].full_url, "https://viewer.example.test/api/state")

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

    def test_native_manufacturing_empty_state_and_module(self):
        status, _, data = self.request("/yapnr/api/manufacturing/article")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"releases": [], "candidates": []})
        status, _, data = self.request("/yapnr/manufacturing-view.js")
        self.assertEqual(status, 200)
        self.assertIn(b"mountManufacturing", data)

    def test_native_package_preparation_requires_origin_and_reports_actionable_errors(self):
        from yapnr.agent import manufacturing_packages

        payload = {"artifact": "a" * 64, "vendor": "jlcpcb", "quantity": 5, "finish": "ENIG"}
        with patch.object(
            manufacturing_packages, "prepare", return_value={"id": "b" * 64}
        ) as prepare:
            self.assertEqual(
                self.request("/yapnr/api/assembly-package/article", payload, origin=False)[0], 400
            )
            prepare.assert_not_called()
            status, _, body = self.request("/yapnr/api/assembly-package/article", payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["id"], "b" * 64)
        with patch.object(
            manufacturing_packages, "prepare", side_effect=ValueError("Source board changed")
        ):
            status, _, body = self.request("/yapnr/api/assembly-package/article", payload)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"], "Source board changed")

    def test_assembly_availability_requires_origin_and_dispatches_quantity(self):
        from yapnr.agent import assembly_availability

        payload = {"artifact": "fixture", "vendor": "jlcpcb", "quantity": 5}
        with patch.object(
            assembly_availability, "check", return_value={"all_available": False}
        ) as check:
            self.assertEqual(
                self.request("/yapnr/api/assembly-availability/article", payload, origin=False)[0],
                400,
            )
            check.assert_not_called()
            status, _, data = self.request("/yapnr/api/assembly-availability/article", payload)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(data)["all_available"])
            check.assert_called_once_with(self.root, "fixture", "jlcpcb", 5, False)

    def test_vendor_upload_is_an_explicit_origin_checked_action(self):
        from yapnr.agent import vendor_uploads

        with patch.object(
            vendor_uploads, "upload", return_value={"redirect": "https://www.pcbway.com/"}
        ) as upload:
            payload = {"artifact": "a" * 64}
            self.assertEqual(
                self.request("/yapnr/api/vendor-upload/article", payload, origin=False)[0], 400
            )
            upload.assert_not_called()
            self.assertEqual(self.request("/yapnr/api/vendor-upload/article", payload)[0], 200)
            upload.assert_called_once()

    def test_concurrent_startup_asset_burst(self):
        count = 32
        barrier = threading.Barrier(count)
        paths = [
            "workspace.js",
            "dock.js",
            "source-view.js",
            "search-view.js",
            "performance-view.js",
            "experiments-view.js",
            "traceability.js",
            "workbench.css",
        ]

        def fetch(index):
            barrier.wait(timeout=5)
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            try:
                connection.request("GET", "/yapnr/" + paths[index % len(paths)])
                response = connection.getresponse()
                return response.status, len(response.read())
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as executor:
            results = list(executor.map(fetch, range(count)))
        self.assertTrue(all(status == 200 and size > 0 for status, size in results), results)

    def test_search_build_history_and_project_cloud_settings(self):
        (self.root / "main.ato").write_text("module Main:\n    bypass = new Capacitor\n")
        status, _, body = self.request("/yapnr/api/search/article?q=bypass")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["results"][0]["line"], 2)
        report = self.root / "reports/build-01/result.json"
        report.parent.mkdir(parents=True)
        report.write_text('{"atopile":"0.15.8","returncode":1}')
        status, _, body = self.request("/yapnr/api/experiments/article")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["runs"][0]["status"], "failed")
        _, _, body = self.request("/yapnr/api/cloud/article")
        original = json.loads(body)
        payload = {"settings": {"local": {"workers": 2}}, "sha256": original["sha256"]}
        self.assertEqual(self.request("/yapnr/api/cloud/article", payload, origin=False)[0], 400)
        status, _, body = self.request("/yapnr/api/cloud/article", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["scope"], "project")
        self.assertEqual(self.request("/yapnr/api/cloud/article", payload)[0], 400)

    def test_brand_resources_presence_and_optimistic_source_edit(self):
        status, _, data = self.request("/yapnr/brand/fonts/SpaceGrotesk.woff2")
        self.assertEqual(status, 200)
        self.assertEqual(data[:4], b"wOF2")
        self.assertEqual(self.request("/yapnr/brand/../agent/web.py")[0], 400)
        payload = dict(
            action="open",
            client="00000000-0000-0000-0000-000000000000",
            session="00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(self.request("/yapnr/api/presence/article", payload, origin=False)[0], 400)
        self.assertEqual(self.request("/yapnr/api/presence/article", payload)[0], 200)
        status, _, data = self.request("/yapnr/api/timing/article")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)["sessions"]), 1)
        source = self.root / "main.ato"
        source.write_text("module Main:\n    pass\n")
        payload = dict(
            path="main.ato", sha256=workspace.file_sha(source), text="module Main:\n    x = 1\n"
        )
        self.assertEqual(self.request("/yapnr/api/source-update/article", payload)[0], 200)
        self.assertEqual(self.request("/yapnr/api/source-update/article", payload)[0], 400)

    def test_provider_catalog_omits_unused_payload_but_preserves_connect_choices(self):
        catalog = {
            "connected": ["paid"],
            "all": [
                {
                    "id": "paid",
                    "name": "Connected",
                    "models": {"model": {"name": "Model", "large": "unused"}},
                },
                {"id": "other", "name": "Other", "models": {"model": {"large": "unused"}}},
            ],
        }
        with patch.object(web, "api", return_value=catalog):
            status, _, raw = self.request("/yapnr/api/providers")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(raw),
            {
                "connected": ["paid"],
                "all": [
                    {"id": "paid", "name": "Connected", "models": {"model": {"name": "Model"}}},
                    {"id": "other", "name": "Other", "models": {}},
                ],
            },
        )

    def test_native_experiment_lanes_are_data_with_explicit_missing_inputs(self):
        status, _, raw = self.request("/yapnr/api/experiment-lanes/article")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"runs": []})
        self.server.experiments["article"] = "https://viewer.example.test"
        with patch.object(
            web,
            "api",
            return_value={"lanes": {"candidate": {"progress": {"state": "done", "fraction": 1}}}},
        ) as remote:
            status, _, raw = self.request("/yapnr/api/experiment-lanes/article")
        self.assertEqual(status, 200)
        run = json.loads(raw)["runs"][0]
        self.assertEqual(run["kind"], "pnr")
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["inputs"], [])
        remote.assert_called_once_with("https://viewer.example.test", "/api/state")

    def test_source_index_uses_configured_viewer_and_returns_only_navigation(self):
        self.server.experiments["article"] = "https://viewer.example.test"
        with patch.object(
            web,
            "api",
            return_value={
                "src_root": "/private/source",
                "files": [{"path": "parts/LED.ato"}],
                "modules": {"LED": {"file": "parts/LED.ato", "line": 3}},
                "components": {"irrelevant": {}},
            },
        ) as remote:
            status, _, raw = self.request("/yapnr/api/source-index/article")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(raw),
            {
                "files": [{"path": "parts/LED.ato"}],
                "modules": {"LED": {"file": "parts/LED.ato", "line": 3}},
            },
        )
        self.assertEqual(
            remote.call_args.args, ("https://viewer.example.test", "/api/source/index")
        )

    def test_main_message_arms_selected_model_and_stop_disables_continuation(self):
        with workspace.lock(self.root) as folder:
            workspace.atomic(
                folder / "thread-state.json", workspace.encoded({"main_thread": "ses_test"})
            )
        calls = []

        def remote(upstream, path, data=None, method=None):
            if data is not None:
                calls.append((path, data))
                return None
            if "/message?" in path:
                return []
            return {"directory": "/projects/article"}

        with patch.object(web, "api", side_effect=remote):
            status, _, _ = self.request(
                "/yapnr/api/message/article",
                {
                    "session": "ses_test",
                    "model": {"providerID": "fixture", "modelID": "selected"},
                    "text": "Synthetic design request",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(web.query(self.root)["state"], "requirements_capture")
            value = web.harness.read(self.root)
            self.assertTrue(value["enabled"])
            self.assertEqual(value["model"]["modelID"], "selected")
            self.assertTrue(calls[0][1]["parts"][0]["metadata"]["yapnr_request"])
            self.assertEqual(
                self.request("/yapnr/api/harness-stop/article", {"session": "ses_test"})[0], 200
            )
            self.assertFalse(web.harness.read(self.root)["enabled"])
            self.assertIn("/abort", calls[-1][0])

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
