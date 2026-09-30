"""The part cache's HTTP service (``yapnr part-cache serve``); stdlib only.

Read API (public unless the server was started with ``--private-reads``; the health check is
always public)::

    GET    /v1/health                         {"status", "schema", "parts", "catalog", ...}
    GET    /v1/parts?lcsc=&manufacturer=&mpn=&name=&q=&all_versions=1&limit=&offset=
                                              {"parts": [summary, ...]}
    GET    /v1/parts/<id>                     the part manifest
    GET    /v1/lcsc/<C123>                    the newest part manifest for an LCSC id
    GET    /v1/blobs/<sha256>                 file bytes (application/octet-stream, immutable)
    GET    /v1/catalog[?lcsc=C1,C2]           a yapnr-picker-catalog-v1 document
    GET    /v1/catalog/<C123>                 one catalog entry with its provenance
    GET    /v0/component/..., POST /v0/query  atopile's components API, answered from the catalog

Write API (``Authorization: Bearer <token>``; scopes ``write`` and ``admin``)::

    PUT    /v1/blobs/<sha256>                 upload one file; the body must hash to <sha256>
    POST   /v1/parts                          {"name", "files": [{path, sha256, size}],
                                               "provenance": {...}, "licence": {...}}
    PUT    /v1/catalog/<C123>                 {"part": {...catalog v1 part...}, "provenance": {...}}
    DELETE /v1/parts/<id>                     admin; {"reason": "..."}: a takedown
    DELETE /v1/catalog/<C123>                 admin; {"reason": "..."}

Tokens live in a file of ``<sha256 of token> <scope> <label>`` lines; the server never sees a
token in the clear except in a request, and compares hashes in constant time. The service is
meant to run behind a TLS-terminating reverse proxy that also limits request rates
(docs/part-cache.md, "Running a public instance"). It binds the loopback interface unless
``--public`` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from yapnr.frontends.atopile.picker import catalog as catalog_mod
from yapnr.frontends.atopile.picker import server as picker_server
from yapnr.partcache import model
from yapnr.partcache.store import NotFound, Store, TakenDown

SCOPES = ("read", "write", "admin")
MAX_JSON = 1 << 20
_BLOB = re.compile(r"^/v1/blobs/([0-9a-f]{64})$")
_PART = re.compile(r"^/v1/parts/([0-9a-f]{64})$")
_LCSC = re.compile(r"^/v1/lcsc/([^/]+)$")
_CATALOG_ENTRY = re.compile(r"^/v1/catalog/([^/]+)$")


class HttpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_tokens(path: Optional[Path]) -> List[Tuple[str, str, str]]:
    """``(sha256, scope, label)`` for each line of a token file (``#`` starts a comment)."""
    if path is None:
        return []
    tokens = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split(None, 2)
        if len(fields) < 2 or not model.is_sha256(fields[0]) or fields[1] not in SCOPES:
            raise ValueError(f"{path}:{number}: expected '<sha256> <read|write|admin> [label]'")
        tokens.append((fields[0], fields[1], fields[2] if len(fields) > 2 else ""))
    return tokens


class CacheService:
    """Request handling, independent of the HTTP server (so tests can call it directly)."""

    def __init__(
        self,
        store: Store,
        tokens: List[Tuple[str, str, str]] = (),
        public_reads: bool = True,
        max_file_bytes: int = model.DEFAULT_MAX_FILE_BYTES,
    ):
        self.store = store
        self.tokens = list(tokens)
        self.public_reads = public_reads
        self.max_file_bytes = max_file_bytes
        self._catalog_lock = threading.Lock()
        self._catalog: Optional[catalog_mod.Catalog] = None
        self.log_requests = True

    # --- auth -------------------------------------------------------------------------------

    def authorize(self, header: Optional[str], need: str) -> str:
        """The label of the token in ``header`` if it has scope ``need``; HttpError otherwise."""
        if need == "read" and self.public_reads:
            return "anonymous"
        if not header or not header.startswith("Bearer "):
            raise HttpError(401, "a bearer token is required")
        digest = hash_token(header[len("Bearer ") :].strip())
        rank = SCOPES.index(need)
        for sha, scope, label in self.tokens:
            if hmac.compare_digest(sha, digest):
                if SCOPES.index(scope) >= rank:
                    return label or scope
                raise HttpError(403, f"this token lacks the {need!r} scope")
        raise HttpError(401, "unknown token")

    # --- catalog for the components API ---------------------------------------------------

    def catalog(self) -> catalog_mod.Catalog:
        with self._catalog_lock:
            if self._catalog is None:
                self._catalog = catalog_mod.Catalog([self.store.catalog()])
            return self._catalog

    def _catalog_changed(self) -> None:
        with self._catalog_lock:
            self._catalog = None

    # --- dispatch -----------------------------------------------------------------------------

    def handle(
        self, method: str, target: str, auth: Optional[str], body: Optional[bytes]
    ) -> Tuple[int, Any, str]:
        """Returns (status, payload, content type); payload is bytes or a JSON-able object."""
        url = urlsplit(target)
        path, query = url.path, parse_qs(url.query)
        try:
            if path.startswith("/v0/"):
                self.authorize(auth, "read")
                status, obj = picker_server.api_response(self.catalog(), method, target, body)
                return status, obj, "application/json"
            if method == "GET" and path == "/v1/health":  # public: counts only, for probes
                return self._get(path, query)
            if method == "GET":
                self.authorize(auth, "read")
                return self._get(path, query)
            if method == "PUT":
                who = self.authorize(auth, "write")
                return self._put(path, body or b"", who)
            if method == "POST" and path == "/v1/parts":
                who = self.authorize(auth, "write")
                request = self._json(body)
                manifest, created = self.store.put_part(request, who, self.max_file_bytes)
                return (201 if created else 200), manifest, "application/json"
            if method == "DELETE":
                self.authorize(auth, "admin")
                reason = str(self._json(body).get("reason", "")).strip()
                if not reason:
                    raise HttpError(400, "a takedown needs a reason")
                return self._delete(path, reason)
            raise HttpError(404 if method in ("GET", "POST") else 405, "not found")
        except HttpError as err:
            return err.status, {"detail": err.detail}, "application/json"
        except (NotFound, FileNotFoundError):
            return 404, {"detail": "not found"}, "application/json"
        except TakenDown as err:
            return 410, {"detail": str(err)}, "application/json"
        except (model.InvalidPart, catalog_mod.CatalogError, ValueError) as err:
            return 400, {"detail": str(err)}, "application/json"

    @staticmethod
    def _json(body: Optional[bytes]) -> Dict[str, Any]:
        try:
            doc = json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError) as err:
            raise HttpError(400, "the request body is not JSON") from err
        if not isinstance(doc, dict):
            raise HttpError(400, "the request body must be a JSON object")
        return doc

    def _get(self, path: str, query: Dict[str, List[str]]) -> Tuple[int, Any, str]:
        store = self.store
        if path == "/v1/health":
            return (
                200,
                {"status": "ok", "schema": model.CACHE_SCHEMA, **store.stats()},
                "application/json",
            )
        if path == "/v1/parts":

            def one(key: str) -> Optional[str]:
                values = query.get(key)
                return values[0] if values else None

            limit = min(max(int(one("limit") or 100), 1), 1000)
            parts = store.find_parts(
                lcsc=one("lcsc"),
                manufacturer=one("manufacturer"),
                mpn=one("mpn"),
                name=one("name"),
                text=one("q"),
                latest_only=one("all_versions") != "1",
                limit=limit,
                offset=max(int(one("offset") or 0), 0),
            )
            return 200, {"parts": parts}, "application/json"
        match = _PART.match(path)
        if match:
            return 200, store.manifest(match.group(1)), "application/json"
        match = _LCSC.match(path)
        if match:
            return 200, store.current(unquote(match.group(1))), "application/json"
        match = _BLOB.match(path)
        if match:
            return 200, store.read_blob(match.group(1)), "application/octet-stream"
        if path == "/v1/catalog":
            wanted = query.get("lcsc")
            lcsc = [x for v in wanted for x in v.split(",") if x] if wanted else None
            return 200, store.catalog(lcsc), "application/json"
        match = _CATALOG_ENTRY.match(path)
        if match:
            return 200, store.catalog_entry(unquote(match.group(1))), "application/json"
        raise HttpError(404, "not found")

    def _put(self, path: str, body: bytes, who: str) -> Tuple[int, Any, str]:
        match = _BLOB.match(path)
        if match:
            if len(body) > self.max_file_bytes:
                raise HttpError(413, "file too large")
            existed = self.store.has_blob(match.group(1))
            sha = self.store.put_blob(body, expected=match.group(1))
            return (200 if existed else 201), {"sha256": sha, "size": len(body)}, "application/json"
        match = _CATALOG_ENTRY.match(path)
        if match:
            doc = self._json(body)
            part = doc.get("part")
            if not isinstance(part, dict):
                raise HttpError(400, "expected {'part': {...}, 'provenance': {...}}")
            if catalog_mod.normalize_lcsc(part.get("lcsc")) != catalog_mod.normalize_lcsc(
                unquote(match.group(1))
            ):
                raise HttpError(400, "the entry's lcsc does not match the URL")
            entry = self.store.put_catalog(part, doc.get("provenance") or {}, who)
            self._catalog_changed()
            return 200, entry, "application/json"
        raise HttpError(404, "not found")

    def _delete(self, path: str, reason: str) -> Tuple[int, Any, str]:
        match = _PART.match(path)
        if match:
            return 200, self.store.delete_part(match.group(1), reason), "application/json"
        match = _CATALOG_ENTRY.match(path)
        if match:
            self.store.delete_catalog(unquote(match.group(1)), reason)
            self._catalog_changed()
            return 200, {"deleted": unquote(match.group(1))}, "application/json"
        raise HttpError(404, "not found")


def make_handler(service: CacheService, request_timeout: float = 60.0):
    class Handler(BaseHTTPRequestHandler):
        server_version = "yapnr-part-cache/1"
        timeout = request_timeout

        def _dispatch(self, method: str) -> None:
            body = None
            if method in ("PUT", "POST", "DELETE"):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                limit = service.max_file_bytes if method == "PUT" else MAX_JSON
                if length < 0 or length > limit:
                    self._reply(413, {"detail": "request body too large"}, "application/json")
                    self.close_connection = True
                    return
                body = self.rfile.read(length)
            status, payload, ctype = service.handle(
                method, self.path, self.headers.get("Authorization"), body
            )
            self._reply(status, payload, ctype)

        def _reply(self, status: int, payload: Any, ctype: str) -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # Stored files are user-generated content: never let a browser render them.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            if ctype == "application/octet-stream":
                self.send_header("Content-Disposition", "attachment")
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def log_message(self, fmt: str, *args: Any) -> None:
            # Method, path and status only; never headers (tokens), queries or bodies.
            if service.log_requests:
                status = args[1] if len(args) > 1 else ""
                sys.stderr.write(f"part-cache: {self.command} {self.path.split('?')[0]} {status}\n")

    return Handler


def make_server(service: CacheService, host: str, port: int, public: bool) -> ThreadingHTTPServer:
    try:
        loopback = ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and not public:
        raise ValueError(f"refusing to bind {host}: pass --public to serve beyond this machine")
    server_class = ThreadingHTTPServer
    if ":" in host:
        server_class = type("Server6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    httpd = server_class((host.strip("[]"), port), make_handler(service))
    httpd.daemon_threads = True
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yapnr part-cache serve", description=__doc__.split("\n\n")[0]
    )
    add_serve_arguments(parser)
    return serve(parser.parse_args(argv))


def add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, help="the cache directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--public", action="store_true", help="allow a non-loopback address")
    parser.add_argument("--tokens", help="token file: '<sha256> <read|write|admin> <label>' lines")
    parser.add_argument(
        "--private-reads", action="store_true", help="reads need a token with the read scope"
    )
    parser.add_argument("--max-file-mb", type=int, default=model.DEFAULT_MAX_FILE_BYTES >> 20)
    parser.add_argument("--create", action="store_true", help="create the cache if missing")
    parser.add_argument("--port-file", help="write the bound port here once listening")


def serve(args: argparse.Namespace) -> int:
    store = Store.open(args.root, create=args.create)
    tokens = load_tokens(Path(args.tokens)) if args.tokens else []
    service = CacheService(
        store,
        tokens=tokens,
        public_reads=not args.private_reads,
        max_file_bytes=args.max_file_mb << 20,
    )
    httpd = make_server(service, args.host, args.port, args.public)
    port = httpd.server_address[1]
    if args.port_file:
        Path(args.port_file).write_text(f"{port}\n", encoding="utf-8")
    print(
        f"part-cache: serving {args.root} on {args.host}:{port} ({store.stats()['parts']} parts,"
        f" {len(tokens)} tokens, reads {'private' if args.private_reads else 'public'})",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
