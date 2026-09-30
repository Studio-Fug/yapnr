"""The part cache server over HTTP, and the HTTP client against it (loopback, synthetic data)."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
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

    def client(self, token):
        return HttpPartCache(self.url, token=token, blob_dir=self.root / "blob-cache")


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
        status, body, _ = self.call("GET", "/v0/component/lcsc/900001")
        self.assertEqual((status, body["components"][0]["part_number"]), (200, "SR1K"))
        status, body, _ = self.call("POST", "/v0/query", {"queries": [{"lcsc": 900001}]})
        self.assertEqual(len(body["results"][0]["components"]), 1)
        status, body, _ = self.call(
            "PUT", "/v1/catalog/C5", {"part": testing.catalog_entry()}, token=WRITE
        )
        self.assertEqual(status, 400)

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


class BindTest(unittest.TestCase):
    def test_public_bind_needs_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalPartCache(Path(tmp) / "cache", create=True).store
            service = server.CacheService(store)
            with self.assertRaisesRegex(ValueError, "--public"):
                server.make_server(service, "0.0.0.0", 0, public=False)

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
