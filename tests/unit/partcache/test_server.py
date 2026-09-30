"""The part cache server over HTTP, and the HTTP client against it (loopback, synthetic data)."""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from yapnr.frontends.atopile import testing
from yapnr.partcache import model, server
from yapnr.partcache.client import (
    CacheError,
    HttpPartCache,
    LocalPartCache,
    materialize,
    open_cache,
    read_part_dir,
)
from yapnr.partcache.store import NotFound

WRITE = "write-token-for-tests"
ADMIN = "admin-token-for-tests"
READ = "read-token-for-tests"
PROVENANCE = {"source": "self"}
LICENCE = {"spdx": "CC0-1.0"}


class ServerTest(unittest.TestCase):
    public_reads = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store = LocalPartCache(self.root / "cache", create=True).store
        tokens = [
            (server.hash_token(WRITE), "write", "writer"),
            (server.hash_token(ADMIN), "admin", "admin"),
            (server.hash_token(READ), "read", "reader"),
        ]
        self.service = server.CacheService(
            store, tokens=tokens, public_reads=self.public_reads, max_file_bytes=1 << 20
        )
        self.service.log_requests = False
        self.httpd = server.make_server(self.service, "127.0.0.1", 0, public=False)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05})
        thread.daemon = True
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.port}"
        self.part_dir = testing.write_part(self.root / "parts")

    def call(self, method, path, body=None, token=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        ctype = response.getheader("Content-Type")
        parsed = json.loads(payload) if ctype == "application/json" and payload else payload
        return response.status, parsed, dict(response.getheaders())

    def client(self, token, read_token=None):
        return HttpPartCache(
            self.url, token=token, read_token=read_token, blob_dir=self.root / "blob-cache"
        )

    def raw(self, request: bytes, wait: float = 5.0) -> bytes:
        """The status line the server answers ``request`` (bytes sent as they are) with."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=wait) as conn:
            conn.sendall(request)
            return conn.recv(4096).split(b"\r\n")[0]


class PublicReadsTest(ServerTest):
    def test_health(self):
        status, body, _ = self.call("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "yapnr-part-cache-v1")

    def test_writes_need_a_write_token(self):
        request, blobs = read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        sha, data = next(iter(blobs.items()))
        self.assertEqual(self.call("PUT", f"/v1/blobs/{sha}", raw=data)[0], 401)
        self.assertEqual(self.call("PUT", f"/v1/blobs/{sha}", raw=data, token="wrong")[0], 401)
        self.assertEqual(self.call("PUT", f"/v1/blobs/{sha}", raw=data, token=READ)[0], 403)
        self.assertEqual(self.call("PUT", f"/v1/blobs/{sha}", raw=data, token=WRITE)[0], 201)
        self.assertEqual(self.call("PUT", f"/v1/blobs/{sha}", raw=data, token=WRITE)[0], 200)
        self.assertEqual(self.call("PUT", f"/v1/blobs/{'0' * 64}", raw=data, token=WRITE)[0], 400)
        self.assertEqual(self.call("POST", "/v1/parts", request)[0], 401)

    def test_upload_read_materialize_takedown(self):
        writer = self.client(WRITE)
        request, blobs = read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        manifest = writer.upload_part(request, blobs)
        self.assertEqual(manifest["uploaded"]["by"], "writer")
        self.assertEqual(manifest["id"], model.part_id(request["name"], request["files"]))

        reader = self.client(None)
        self.assertEqual(reader.manifest(manifest["id"])["name"], testing.SYNTHETIC_PART)
        self.assertEqual(reader.current(testing.SYNTHETIC_LCSC)["id"], manifest["id"])
        self.assertEqual(len(reader.find(lcsc=testing.SYNTHETIC_LCSC)), 1)
        target = materialize(reader, manifest["id"], self.root / "project/parts")
        self.assertTrue((target / f"{testing.SYNTHETIC_PART}.ato").is_file())

        sha = manifest["files"][0]["sha256"]
        status, body, headers = self.call("GET", f"/v1/blobs/{sha}")
        self.assertEqual(status, 200)
        self.assertEqual(model.sha256_bytes(body), sha)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Content-Disposition"], "attachment")

        with self.assertRaisesRegex(CacheError, "401|403"):
            writer.delete_part(manifest["id"], "not an admin")
        admin = self.client(ADMIN)
        with self.assertRaisesRegex(CacheError, "reason"):
            admin.delete_part(manifest["id"], "")
        admin.delete_part(manifest["id"], "unit test takedown")
        with self.assertRaises(NotFound):
            reader.manifest(manifest["id"])
        with self.assertRaisesRegex(CacheError, "410"):
            writer.upload_part(request, blobs)

    def test_catalog_and_components_api(self):
        writer = self.client(WRITE)
        writer.put_catalog(testing.catalog_entry(), {"source": "unit test"})
        catalog = self.client(None).catalog([testing.SYNTHETIC_LCSC])
        self.assertEqual([p["lcsc"] for p in catalog["parts"]], [testing.SYNTHETIC_LCSC])
        status, body, _ = self.call("GET", "/v0/component/lcsc/990000001")
        self.assertEqual((status, body["components"][0]["part_number"]), (200, "SR1K"))
        status, body, _ = self.call("POST", "/v0/query", {"queries": [{"lcsc": 990000001}]})
        self.assertEqual(len(body["results"][0]["components"]), 1)
        status, body, _ = self.call(
            "PUT", "/v1/catalog/C5", {"part": testing.catalog_entry()}, token=WRITE
        )
        self.assertEqual(status, 400)

    def test_the_token_is_checked_before_the_body_is_read(self):
        # 60 MiB announced, none sent: the answer must come from the headers alone.
        start = time.monotonic()
        line = self.raw(
            f"PUT /v1/blobs/{'0' * 64} HTTP/1.1\r\nHost: x\r\nContent-Length: 62914560\r\n\r\n".encode()
        )
        self.assertIn(b" 401 ", line)
        self.assertLess(time.monotonic() - start, 4)
        line = self.raw(
            b"POST /v1/parts HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
            + READ.encode()
            + b"\r\nContent-Length: 1000\r\n\r\n"
        )
        self.assertIn(b" 403 ", line)

    def test_unused_uploads_are_never_served(self):
        data = b"<html><script>arbitrary content</script>"
        sha = model.sha256_bytes(data)
        self.assertEqual(self.call("PUT", f"/v1/blobs/{sha}", raw=data, token=WRITE)[0], 201)
        self.assertEqual(self.call("GET", f"/v1/blobs/{sha}")[0], 404)
        manifest = self.client(WRITE).upload_part(
            *read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        )
        used = manifest["files"][0]["sha256"]
        self.assertEqual(self.call("GET", f"/v1/blobs/{used}")[0], 200)

    def test_takedown_blocks_the_files(self):
        (self.part_dir / "MODEL.step").write_bytes(b"ISO-10303-21; synthetic model\n")
        request, blobs = read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        manifest = self.client(WRITE).upload_part(request, blobs)
        step = next(f["sha256"] for f in manifest["files"] if f["path"] == "MODEL.step")
        result = self.client(ADMIN).delete_part(manifest["id"], "vendor request")
        self.assertIn(step, result["blocked_files"])
        status, _body, _ = self.call("PUT", f"/v1/blobs/{step}", raw=blobs[step], token=WRITE)
        self.assertEqual(status, 410)
        self.assertEqual(self.call("GET", f"/v1/blobs/{step}")[0], 404)

    def test_local_only_parts_are_refused(self):
        licence = {**LICENCE, "distribution": "local-only"}
        request, blobs = read_part_dir(self.part_dir, PROVENANCE, licence)
        with self.assertRaisesRegex(CacheError, "local-only"):
            self.client(WRITE).upload_part(request, blobs)
        self.assertEqual(self.call("POST", "/v1/parts", request, token=WRITE)[0], 400)

    def test_tampered_blob_is_rejected_by_the_client(self):
        request, blobs = read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        manifest = self.client(WRITE).upload_part(request, blobs)
        sha = manifest["files"][0]["sha256"]
        self.service.store.blob_path(sha).write_bytes(b"tampered")
        with self.assertRaisesRegex(CacheError, "other content"):
            self.client(None).blob(sha)

    def test_bad_requests(self):
        self.assertEqual(self.call("GET", "/v1/parts/not-a-hash")[0], 404)
        self.assertEqual(self.call("GET", "/v1/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/v1/parts", raw=b"{", token=WRITE)[0], 400)
        request, _blobs = read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        status, body, _ = self.call("POST", "/v1/parts", request, token=WRITE)
        self.assertEqual(status, 400)
        self.assertIn("missing", body["detail"])
        # An oversized upload is refused from its Content-Length, before the body is read.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("PUT", f"/v1/blobs/{'1' * 64}")
        conn.putheader("Authorization", f"Bearer {WRITE}")
        conn.putheader("Content-Length", str((1 << 20) + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()


class PrivateReadsTest(ServerTest):
    public_reads = False

    def test_reads_need_a_token(self):
        self.assertEqual(self.call("GET", "/v1/health")[0], 200)  # always public
        self.assertEqual(self.call("GET", "/v1/parts")[0], 401)
        self.assertEqual(self.call("GET", "/v1/parts", token=READ)[0], 200)
        self.assertEqual(self.call("GET", "/v1/catalog", token=WRITE)[0], 200)
        self.assertEqual(self.call("GET", "/v0/motd")[0], 401)


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Records each request's Authorization header; redirects /redirect elsewhere."""

    seen: list = []
    target = ""

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", type(self).target + "/v1/catalog")
        else:
            self.send_response(200)
        body = b'{"schema": "yapnr-picker-catalog-v1", "provenance": {}, "parts": []}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ClientTokenTest(unittest.TestCase):
    """Where the client sends its tokens (loopback recorders, no cache server)."""

    def serve(self, handler):
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05})
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_redirects_are_refused_and_carry_no_token(self):
        sink = type("Sink", (_Recorder,), {"seen": []})
        redirector = type("Redirector", (_Recorder,), {"seen": [], "target": self.serve(sink)})
        client = HttpPartCache(self.serve(redirector) + "/redirect", read_token=READ, token=WRITE)
        with self.assertRaisesRegex(CacheError, "redirected"):
            client.catalog()
        self.assertEqual(sink.seen, [])

    def test_reads_send_the_read_token_only(self):
        recorder = type("Recorder", (_Recorder,), {"seen": []})
        url = self.serve(recorder)
        HttpPartCache(url, token=WRITE, read_token="").catalog()
        HttpPartCache(url, token=WRITE, read_token=READ).catalog()
        self.assertEqual([auth for _path, auth in recorder.seen], [None, f"Bearer {READ}"])

    def test_no_token_over_plain_http_beyond_loopback(self):
        client = HttpPartCache("http://parts.example.org", token=WRITE, read_token="")
        with self.assertRaisesRegex(CacheError, "plain http"):
            client.put_catalog(testing.catalog_entry(), {"source": "unit test"})
        HttpPartCache("https://parts.example.org", token=WRITE)  # https is fine
        with self.assertRaisesRegex(CacheError, "query"):
            HttpPartCache("https://parts.example.org/?x=1")


class BusyTest(ServerTest):
    def setUp(self):
        super().setUp()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = server.make_server(self.service, "127.0.0.1", 0, False, max_connections=1)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05})
        thread.daemon = True
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def test_requests_beyond_the_limit_get_503(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as held:
            held.sendall(b"GET /v1/health HTTP/1.1\r\n")  # never finished: holds the only slot
            deadline = time.monotonic() + 5
            line = b""
            while time.monotonic() < deadline and b" 503 " not in line:
                line = self.raw(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
                time.sleep(0.05)
            self.assertIn(b" 503 ", line)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b" 200 " not in line:
            line = self.raw(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(0.05)
        self.assertIn(b" 200 ", line)


class BindTest(unittest.TestCase):
    def test_public_bind_needs_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalPartCache(Path(tmp) / "cache", create=True).store
            service = server.CacheService(store)
            with self.assertRaisesRegex(ValueError, "--public"):
                server.make_server(service, "0.0.0.0", 0, public=False)

    def test_local_only_parts_stay_on_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = LocalPartCache(Path(tmp) / "cache", create=True)
            part = testing.write_part(Path(tmp) / "parts")
            request, blobs = read_part_dir(
                part, PROVENANCE, {**LICENCE, "distribution": "local-only"}
            )
            cache.upload_part(request, blobs)
            service = server.CacheService(cache.store)
            with self.assertRaisesRegex(ValueError, "local-only"):
                server.make_server(service, "0.0.0.0", 0, public=True)
            httpd = server.make_server(service, "127.0.0.1", 0, public=False)
            httpd.server_close()

    def test_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens"
            path.write_text(f"# comment\n{server.hash_token('t')} write ci runner\n")
            self.assertEqual(
                server.load_tokens(path), [(server.hash_token("t"), "write", "ci runner")]
            )
            path.write_text("abc write\n")
            with self.assertRaises(ValueError):
                server.load_tokens(path)

    def test_open_cache_picks_the_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(open_cache(Path(tmp) / "c", create=True), LocalPartCache)
        self.assertIsInstance(open_cache("https://parts.example.org"), HttpPartCache)
        with self.assertRaises(CacheError):
            HttpPartCache("https://user:pw@parts.example.org")


if __name__ == "__main__":
    unittest.main()
