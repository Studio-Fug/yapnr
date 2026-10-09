"""On-demand native wire responses replay without network or operator credentials."""

import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from yapnr.frontends.atopile.picker import discovery
from yapnr.frontends.atopile.picker.server import PickerServer

PART = {
    "lcsc": 1234,
    "manufacturer_name": "Fixture",
    "part_number": "FixturePart",
    "attributes": {},
}


class DiscoveryTest(unittest.TestCase):
    def test_fetch_once_pin_and_replay_offline_without_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Mock(return_value=(200, {"components": [PART]}))
            with patch.dict(discovery.os.environ, {"YAPNR_COMPONENTS_API_TOKEN": "unused-token"}):
                capture = discovery.Discovery(directory, online=True, request=remote)
                with PickerServer([], resolve=capture.answer) as server:
                    request = Request(
                        server.url + "/v0/component/lcsc/1234",
                        headers={"Authorization": "Bearer loopback-placeholder"},
                    )
                    with urlopen(request) as response:
                        self.assertEqual(json.load(response)["components"], [PART])
            self.assertEqual(len(remote.call_args.args), 3)
            self.assertNotIn("unused-token", capture.path.read_text())
            offline = discovery.Discovery(
                directory, request=Mock(side_effect=AssertionError("Network forbidden"))
            )
            local = (200, {"components": []})
            self.assertEqual(
                offline.answer("GET", "/v0/component/lcsc/1234", None, local)[1]["components"],
                [PART],
            )
            self.assertEqual(offline.answer("GET", "/v0/component/lcsc/5678", None, local), local)
            self.assertEqual(offline.replayed_queries, 1)
            self.assertEqual(remote.call_count, 1)

    def test_public_supplier_failure_is_explicit_and_not_cached_as_an_empty_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Mock(
                return_value=(
                    502,
                    {"detail": "Public supplier unavailable; no Atopile login is used"},
                )
            )
            capture = discovery.Discovery(directory, online=True, request=remote)
            local = (200, {"components": []})
            result = capture.answer("GET", "/v0/component/lcsc/1234", None, local)
            self.assertEqual(result[0], 502)
            self.assertEqual(capture.failures[0]["status"], 502)
            self.assertFalse(capture.path.exists())
            capture.answer("GET", "/v0/component/lcsc/5678", None, local)
            self.assertEqual(remote.call_count, 1)

    def test_partial_batch_discovers_only_missing_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Mock(return_value=(200, {"results": [{"components": [PART]}]}))
            capture = discovery.Discovery(directory, online=True, request=remote)
            query = {"queries": [{"lcsc": 5678}, {"lcsc": 1234}]}
            existing = {**PART, "lcsc": 5678}
            status, response = capture.answer(
                "POST",
                "/v0/query",
                discovery.encoded(query),
                (200, {"results": [{"components": [existing]}, {"components": []}]}),
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(remote.call_args.args[2]), {"queries": [{"lcsc": 1234}]})
            self.assertEqual(
                [result["components"][0]["lcsc"] for result in response["results"]], [5678, 1234]
            )

    def test_failed_snapshot_write_is_reported_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = discovery.Discovery(
                directory, online=True, request=Mock(return_value=(200, {"components": [PART]}))
            )
            with patch.object(capture, "save", side_effect=OSError("fixture write failure")):
                status, response = capture.answer(
                    "GET", "/v0/component/lcsc/1234", None, (200, {"components": []})
                )
            self.assertEqual(status, 502)
            self.assertIn("could not be saved", response["detail"])
            self.assertEqual(capture.document["queries"], {})
            self.assertEqual(capture.failures[0]["status"], 502)

    def test_batch_cardinality_and_digest_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Mock(return_value=(200, {"results": []}))
            capture = discovery.Discovery(directory, online=True, request=remote)
            body = b'{"queries":[{"lcsc":1234}]}'
            self.assertEqual(
                capture.answer("POST", "/v0/query", body, (200, {"results": [{"components": []}]}))[
                    0
                ],
                502,
            )
            remote.return_value = (200, {"components": [PART]})
            capture.answer("GET", "/v0/component/lcsc/1234", None, (200, {"components": []}))
            data = json.loads(capture.path.read_text())
            next(iter(data["queries"].values()))["response"]["components"][0]["lcsc"] = 9999
            capture.path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                discovery.Discovery(directory)

    def test_no_remote_lookup_for_local_results_or_unrelated_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Mock(side_effect=AssertionError("Network forbidden"))
            capture = discovery.Discovery(directory, online=True, request=remote)
            for method, path, local in [
                ("GET", "/v0/motd", (200, {"message": None})),
                ("GET", "/v0/component/lcsc/1234", (200, {"components": [PART]})),
                ("POST", "/unknown", (404, {"detail": "not found"})),
            ]:
                self.assertEqual(capture.answer(method, path, None, local), local)


if __name__ == "__main__":
    unittest.main()
