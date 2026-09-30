"""The loopback picker speaks atopile 0.15.8's components API (synthetic catalog)."""

from __future__ import annotations

import http.client
import json
import unittest
from urllib.parse import quote

from yapnr.frontends.atopile.picker import catalog as C
from yapnr.frontends.atopile.picker import server as S

CATALOG = C.validate(
    {
        "schema": C.SCHEMA,
        "provenance": {"source": "unit test"},
        "parts": [
            {
                "lcsc": "C900101",
                "mpn": "SYN R1K",
                "manufacturer": "Synthetic Parts Co",
                "package": "0603",
                "kind": "resistor",
                "params": {"resistance_ohm": 1000, "tolerance_pct": 1},
            },
            {
                "lcsc": "C900102",
                "mpn": "SYN-R1K-0402",
                "manufacturer": "Synthetic Parts Co",
                "package": "0402",
                "kind": "resistor",
                "params": {"resistance_ohm": 1000, "tolerance_pct": 1},
            },
        ],
    }
)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.server = S.PickerServer([CATALOG], on_request=self.requests.append).start()
        self.addCleanup(self.server.close)

    def call(self, method, path, body=None, host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers = {"Authorization": "Bearer placeholder"}
        if host is not None:
            headers["Host"] = host
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        if response.getheader("Content-Type") != "application/json":
            return response.status, None
        return response.status, json.loads(payload) if payload else None

    def test_url(self):
        self.assertEqual(self.server.url, f"http://127.0.0.1:{self.server.port}")

    def test_lcsc_lookup(self):
        status, body = self.call("GET", "/v0/component/lcsc/900101")
        self.assertEqual(status, 200)
        self.assertEqual([c["lcsc"] for c in body["components"]], [900101])
        self.assertEqual(self.call("GET", "/v0/component/lcsc/42")[1], {"components": []})

    def test_mfr_segments_are_url_decoded(self):
        path = f"/v0/component/mfr/{quote('Synthetic Parts Co')}/{quote('SYN R1K')}"
        status, body = self.call("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual([c["lcsc"] for c in body["components"]], [900101])

    def test_unknown_routes_are_404(self):
        self.assertEqual(self.call("GET", "/v0/component/other/1")[0], 404)
        self.assertEqual(self.call("POST", "/v0/elsewhere", {})[0], 404)
        self.assertEqual(self.call("DELETE", "/v0/query")[0], 501)

    def test_motd_and_health(self):
        self.assertEqual(self.call("GET", "/v0/motd"), (200, {"message": None}))
        self.assertEqual(self.call("GET", "/healthz"), (200, {"status": "ok", "parts": 2}))

    def test_empty_query_list(self):
        self.assertEqual(self.call("POST", "/v0/query", {"queries": []}), (200, {"results": []}))

    def test_batch_query_by_lcsc_and_type_with_package(self):
        type_query = {
            "endpoint": "resistors",
            "qty": 1,
            "resistance": C.quantity(950, 1050, "ohm"),
            "package": {"type": "EnumSet", "data": {"elements": [{"name": "R0402"}]}},
            "max_power": None,
            "max_voltage": None,
        }
        status, body = self.call(
            "POST", "/v0/query", {"queries": [{"lcsc": 900101, "quantity": 1}, type_query]}
        )
        self.assertEqual(status, 200)
        found = [[c["lcsc"] for c in r["components"]] for r in body["results"]]
        self.assertEqual(found, [[900101], [900102]])
        status, body = self.call("POST", "/v0/query/resistors", {**type_query, "package": None})
        self.assertEqual([c["lcsc"] for c in body["components"]], [900101, 900102])

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/v0/query", raw=b"{")[0], 400)
        self.assertEqual(self.call("POST", "/v0/query", {"queries": [1]})[0], 400)
        self.assertEqual(self.call("GET", "/v0/motd", host="attacker.example")[0], 403)

    def test_requests_are_recorded_without_headers(self):
        self.call("POST", "/v0/query", {"queries": []})
        self.assertEqual(self.requests[-1]["path"], "/v0/query")
        self.assertNotIn("Authorization", json.dumps(self.requests))


class LoopbackTest(unittest.TestCase):
    def test_only_loopback_addresses(self):
        self.assertEqual(S.loopback_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(S.loopback_host("[::1]"), "::1")
        for host in ("0.0.0.0", "192.0.2.10", "localhost", "example.com", "::"):
            with self.assertRaises(S.NotLoopback, msg=host):
                S.PickerServer([], host=host)

    def test_main_refuses_bad_catalogs(self):
        self.assertEqual(S.main(["--catalog", "/nonexistent/catalog.json"]), 2)


if __name__ == "__main__":
    unittest.main()
