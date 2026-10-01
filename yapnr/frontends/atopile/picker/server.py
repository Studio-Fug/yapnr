"""An offline, loopback-only implementation of atopile's components API.

atopile 0.15.8 resolves parameterised parts (a ``Resistor`` with a ``resistance`` and a
``package``) and explicit picks (``lcsc_id``, ``mpn``) by asking a components service. This
service answers from local catalogs (``catalog.py``), so ``ato build`` never reaches the hosted
one. The footprints of a picked part come from the project's parts directory, which the runner
fills from the part cache; the atopile hook makes ``ato`` use them (``hook/``).

The wire protocol is atopile 0.15.8's (``faebryk/libs/picker/api/api.py``)::

    GET  /v0/component/lcsc/<id>          -> {"components": [...]}
    GET  /v0/component/mfr/<mfr>/<pn>     -> {"components": [...]}   (URL-decoded segments)
    POST /v0/query/<method>  params       -> {"components": [...]}
    POST /v0/query  {"queries": [...]}    -> {"results": [{"components": [...]}, ...]}

plus ``GET /v0/motd`` (atopile 0.15.8 fetches a message of the day from the components URL on
every command; the answer is always empty) and ``GET /healthz``. Unknown routes are 404. An
empty query list is answered with an empty result list. The ``Authorization`` header atopile
sends is ignored and never logged.

Imported from rules_atopile's ``tools/atopile-picker/server.py`` (as copied into Splanc's
``hardware/tools/picker_server.py``) and rewritten: catalogs are arguments, validated at start;
the server binds loopback only; the port can be 0 with a port file; mfr and pn segments are
URL-decoded; unknown routes are 404; type queries cover resistors, capacitors and inductors and
filter by package; the ``Host`` header must name the loopback address.

Stdlib only, so that any Python 3.9+ interpreter can run it.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

if __package__ in (None, ""):  # `python server.py`: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from yapnr.frontends.atopile.picker import catalog as catalog_mod  # noqa: E402

MAX_BODY = 1 << 20
_LCSC_PATH = re.compile(r"^/v0/component/lcsc/([^/]+)$")
_MFR_PATH = re.compile(r"^/v0/component/mfr/([^/]+)/([^/]+)$")
_QUERY_PATH = re.compile(r"^/v0/query/([A-Za-z_][A-Za-z0-9_]*)$")


class NotLoopback(ValueError):
    """A bind address or URL that is not the loopback interface."""


def loopback_host(host: str) -> str:
    """``host`` if it is a literal loopback address (127.0.0.0/8 or ::1), else NotLoopback."""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError as err:
        raise NotLoopback(f"{host!r} is not a literal loopback address") from err
    if not address.is_loopback:
        raise NotLoopback(f"{host!r} is not a loopback address")
    return str(address)


def api_response(
    catalog: catalog_mod.Catalog, method: str, path: str, body: Optional[bytes]
) -> Tuple[int, Dict[str, Any]]:
    """The status and JSON body for one components-API request (shared with the part cache)."""
    path = urlsplit(path).path
    if method == "GET":
        if path == "/v0/motd":  # atopile 0.15.8 asks on every command; there is no message
            return 200, {"message": None}
        match = _LCSC_PATH.match(path)
        if match:
            return 200, {"components": catalog.by_lcsc(unquote(match.group(1)))}
        match = _MFR_PATH.match(path)
        if match:
            mfr, pn = (unquote(g) for g in match.groups())
            return 200, {"components": catalog.by_mfr(mfr, pn)}
        return 404, {"detail": "not found"}
    if method != "POST":
        return 405, {"detail": "method not allowed"}
    if path != "/v0/query" and not _QUERY_PATH.match(path):
        return 404, {"detail": "not found"}
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return 400, {"detail": "request body is not JSON"}
    if not isinstance(data, dict):
        return 400, {"detail": "request body must be a JSON object"}
    if path == "/v0/query":
        queries = data.get("queries", [])
        if not isinstance(queries, list) or not all(isinstance(q, dict) for q in queries):
            return 400, {"detail": "queries must be a list of objects"}
        return 200, {"results": [{"components": catalog.answer(q)} for q in queries]}
    method_name = _QUERY_PATH.match(path).group(1)
    return 200, {"components": catalog.query(method_name, data)}


def _allowed_host(header: Optional[str], port: int) -> bool:
    """A ``Host`` header that names the loopback interface (guards against DNS rebinding)."""
    if not header:
        return False
    host = urlsplit("//" + header).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "yapnr-picker/1"
    catalog: catalog_mod.Catalog
    on_request: Optional[Callable[[Dict[str, Any]], None]] = None

    def _reply(self, status: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        if not _allowed_host(self.headers.get("Host"), self.server.server_address[1]):
            self._reply(403, {"detail": "the Host header must name the loopback address"})
            return
        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._reply(413, {"detail": "request body too large"})
                return
            body = self.rfile.read(length)
        status, obj = api_response(self.catalog, method, self.path, body)
        if self.on_request is not None:
            try:
                parsed = json.loads(body) if body else None
            except ValueError:
                parsed = None
            self.on_request({"method": method, "path": self.path, "body": parsed, "status": status})
        self._reply(status, obj)

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        if urlsplit(self.path).path == "/healthz":
            self._reply(200, {"status": "ok", "parts": len(self.catalog)})
            return
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def log_message(self, *_args: Any) -> None:  # requests are recorded through on_request only
        pass


class PickerServer:
    """A running picker on the loopback interface; use as a context manager or call ``close``."""

    def __init__(
        self,
        catalogs: Sequence[Dict[str, Any]],
        host: str = "127.0.0.1",
        port: int = 0,
        on_request: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        host = loopback_host(host)
        handler = type(
            "Handler",
            (_Handler,),
            {"catalog": catalog_mod.Catalog(catalogs), "on_request": staticmethod(on_request)},
        )
        server_class = ThreadingHTTPServer
        if ":" in host:
            server_class = type(
                "Server6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6}
            )
        self.httpd = server_class((host, port), handler)
        self.httpd.daemon_threads = True
        self.host = host
        self.port = self.httpd.server_address[1]
        self.parts = len(handler.catalog)
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> "PickerServer":
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=10)
        self.httpd.server_close()

    def __enter__(self) -> "PickerServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--catalog", action="append", default=[], help="catalog file (repeatable)")
    parser.add_argument("--host", default="127.0.0.1", help="loopback address to bind")
    parser.add_argument("--port", type=int, default=0, help="0 lets the system choose")
    parser.add_argument("--port-file", help="write the bound port here once listening")
    args = parser.parse_args(argv)
    try:
        catalogs = [catalog_mod.load(path) for path in args.catalog]
        server = PickerServer(catalogs, host=args.host, port=args.port)
    except (catalog_mod.CatalogError, NotLoopback, OSError) as err:
        print(f"picker: {err}", file=sys.stderr)
        return 2
    if args.port_file:
        Path(args.port_file).write_text(f"{server.port}\n", encoding="utf-8")
    print(f"picker: {server.url} ({server.parts} parts)", file=sys.stderr)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
