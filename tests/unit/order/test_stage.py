"""``yapnr order stage``: dry runs, prompts, the log, bundles, import links, --verify-url.

The whole module runs with sockets patched to fail: O1 opens no network connection (the mocked
--verify-url opener is the only network path, and it is a fake here).
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yapnr.fab import build, testing
from yapnr.order import card as card_mod
from yapnr.order import net, stage, vendors


def no_network(*_args, **_kwargs):
    raise AssertionError("yapnr order opened a network connection")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fake = testing.FakeKicadCli()
        for target, value in (
            ("yapnr.fab.build.kicad.find_cli", mock.Mock(return_value=Path("kicad-cli"))),
            ("yapnr.fab.build.kicad.Cli", mock.Mock(side_effect=lambda *a, **k: self.fake)),
            ("socket.socket.connect", no_network),
            ("socket.create_connection", no_network),
            ("webbrowser.open", mock.Mock(side_effect=AssertionError("webbrowser.open called"))),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.board = testing.write_board(self.dir / "src", "board", layers=4)
        self.opened = []

    def opts(self, vendor="oshpark", **kwargs):
        request = build.Request(board=self.board, vendor=vendor, out=self.dir / "out", name="demo")
        return stage.Options(vendor=vendor, board=self.board, build_request=request, **kwargs)

    def run_stage(self, opts, env=None, tty=False, answer="n", **kwargs):
        out, err = io.StringIO(), io.StringIO()
        code = stage.stage(
            opts,
            out=out,
            err=err,
            open_page=lambda url: self.opened.append(url) or True,
            ask=lambda prompt: answer,
            is_tty=lambda: tty,
            environ=env if env is not None else {},
            **kwargs,
        )
        return code, out.getvalue(), err.getvalue()

    def bundle_dir(self, vendor="oshpark"):
        return self.dir / "out" / {"oshpark": "demo-oshpark-4l", "jlcpcb": "demo-jlc-4l"}[vendor]


class DryRunTest(Base):
    def test_dry_run_prints_the_card_and_opens_nothing(self):
        code, out, _ = self.run_stage(self.opts(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("ORDER CARD  staging only", out)
        self.assertIn("DRY RUN: would open https://oshpark.com/", out)
        self.assertEqual(self.opened, [])
        self.assertFalse((self.bundle_dir() / "staged.jsonl").exists())
        self.assertTrue((self.bundle_dir() / "order-card.staged.json").is_file())

    def test_ci_and_the_environment_force_a_dry_run(self):
        for env in ({"CI": "true"}, {"YAPNR_ORDER_DRY_RUN": "1"}):
            code, out, _ = self.run_stage(self.opts(yes=True), env=env, tty=True, answer="y")
            self.assertEqual(code, 0)
            self.assertIn("DRY RUN (", out)
            self.assertEqual(self.opened, [])
        self.assertIsNone(stage.dry_run_forced({"CI": "false"}))

    def test_a_fab_check_error_stops_before_the_card(self):
        self.fake = testing.FakeKicadCli(
            violations=[{"type": "clearance", "severity": "error", "description": "C", "items": []}]
        )
        code, out, err = self.run_stage(self.opts(dry_run=True))
        self.assertEqual(code, 1)
        self.assertNotIn("ORDER CARD", out)
        self.assertIn("FAB-DRC", err)


class StagingTest(Base):
    def test_default_asks_and_no_means_print_only(self):
        code, out, _ = self.run_stage(self.opts(), tty=True, answer="n")
        self.assertEqual((code, self.opened), (0, []))
        self.assertIn("open        https://oshpark.com/", out)
        log = [json.loads(x) for x in (self.bundle_dir() / "staged.jsonl").read_text().splitlines()]
        self.assertEqual(log[0]["opened"], False)
        self.assertEqual(log[0]["mechanism"], "manual")
        self.assertEqual(
            set(log[0]),
            {"time", "vendor", "mechanism", "url", "zip_sha256", "opened", "verified_url"},
        )

    def test_yes_opens_the_page_and_logs_it(self):
        code, out, _ = self.run_stage(self.opts(yes=True))
        self.assertEqual((code, self.opened), (0, ["https://oshpark.com/"]))
        self.assertIn("yapnr uploaded nothing and ordered nothing", out)
        log = json.loads((self.bundle_dir() / "staged.jsonl").read_text())
        self.assertTrue(log["opened"])

    def test_a_tty_yes_opens_and_no_tty_never_waits(self):
        self.run_stage(self.opts(), tty=True, answer="y")
        self.assertEqual(self.opened, ["https://oshpark.com/"])
        self.opened.clear()
        self.run_stage(self.opts(), tty=False, answer="y")
        self.assertEqual(self.opened, [])

    def test_print_only_and_reveal(self):
        revealed = []
        code, out, _ = self.run_stage(
            self.opts(print_only=True, reveal=True), reveal=revealed.append
        )
        self.assertEqual((code, self.opened), (0, []))
        self.assertEqual([p.name for p in revealed], ["demo-oshpark-gerbers.zip"])

    def test_jlc_page_and_quantity_rule(self):
        code, out, _ = self.run_stage(self.opts("jlcpcb", dry_run=True, qty=10))
        self.assertEqual(code, 0)
        self.assertIn("https://cart.jlcpcb.com/quote", out)
        self.assertIn("qty         10 (5 or more)", out)
        with self.assertRaises(stage.StageError):
            self.run_stage(self.opts("oshpark", dry_run=True, qty=4))

    def test_service_changes_the_estimate(self):
        _, out, _ = self.run_stage(self.opts(dry_run=True, service="super-swift", qty=6))
        card = json.loads((self.bundle_dir() / "order-card.staged.json").read_text())
        self.assertEqual(card["service"]["id"], "super-swift")
        self.assertAlmostEqual(card["estimate"]["usd"], round(30 * 20 / 645.16 * 20 * 2, 2))
        self.assertIn("x 2 sets", out)


class BundleTest(Base):
    def test_stage_from_a_built_bundle_checks_its_hashes(self):
        build.build(self.opts().build_request)
        d = self.bundle_dir()
        opts = stage.Options(vendor="oshpark", bundle=d, dry_run=True)
        code, out, _ = self.run_stage(opts)
        self.assertEqual(code, 0)
        (d / "README.md").write_text("changed\n")
        with self.assertRaises(stage.StageError):
            self.run_stage(opts)
        with self.assertRaises(stage.StageError):
            self.run_stage(stage.Options(vendor="jlcpcb", bundle=d, dry_run=True))


class ImportUrlTest(Base):
    URL = "https://example.com/boards/demo.zip?raw=1&name=x y"

    def test_the_link_encodes_the_public_url(self):
        link = vendors.import_link("oshpark", self.URL)
        self.assertEqual(
            link,
            "https://oshpark.com/import?url=https://example.com/boards/demo.zip%3Fraw%3D1%26name%3Dx%20y",
        )
        for bad in (
            "http://example.com/a.zip",
            "file:///tmp/a.zip",
            "https://user:pw@example.com/a.zip",
            "https://example.com/a.zip?token=secret",
            "https://bucket.s3.amazonaws.com/a.zip?X-Amz-Signature=ab&X-Amz-Credential=cd",
            "https://example.blob.core.windows.net/a.zip?sv=2020&sig=ab",
        ):
            with self.assertRaises(vendors.StagingError):
                vendors.import_link("oshpark", bad)
        with self.assertRaises(vendors.StagingError):
            vendors.import_link("jlcpcb", self.URL)

    def test_import_url_stage_and_verify(self):
        built = build.build(self.opts().build_request)
        digest = built.card["files"]["upload"]["sha256"]
        seen = []

        def verify(url, expected):
            seen.append((url, expected))
            return expected

        opts = self.opts(via="import-url", url=self.URL, verify_url=True, yes=True)
        code, out, _ = self.run_stage(opts, verify=verify)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(self.URL, digest)])
        self.assertTrue(self.opened[0].startswith("https://oshpark.com/import?url="))
        self.assertIn("OSH Park fetches the public zip itself", out)

    def test_dry_run_never_verifies(self):
        def verify(*_):
            raise AssertionError("verified in a dry run")

        opts = self.opts(via="import-url", url=self.URL, verify_url=True, dry_run=True)
        code, out, _ = self.run_stage(opts, verify=verify)
        self.assertEqual(code, 0)
        self.assertIn("would verify", out)

    def test_import_url_needs_a_url(self):
        with self.assertRaises(stage.StageError):
            self.run_stage(self.opts(via="import-url", dry_run=True))


class VerifyUrlTest(unittest.TestCase):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def __init__(self, body):
            self.body, self.requests = body, []

        def open(self, request, timeout):
            self.requests.append((request.get_method(), request.full_url, request.data, timeout))
            return VerifyUrlTest.Response(self.body)

    def test_match_mismatch_and_https_only(self):
        import hashlib

        body = b"PK zip bytes"
        good = hashlib.sha256(body).hexdigest()
        opener = self.Opener(body)
        self.assertEqual(net.verify_url("https://example.com/z.zip", good, opener=opener), good)
        self.assertEqual(opener.requests, [("GET", "https://example.com/z.zip", None, net.TIMEOUT)])
        with self.assertRaises(net.VerifyError):
            net.verify_url("https://example.com/z.zip", "0" * 64, opener=opener)
        with self.assertRaises(net.VerifyError):
            net.verify_url("http://example.com/z.zip", good, opener=opener)

    def test_redirects_to_http_are_refused(self):
        handler = net._HttpsOnlyRedirect()
        with self.assertRaises(net.VerifyError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/z.zip")


class CardTest(unittest.TestCase):
    def test_text_markdown_and_json_agree(self):
        card = json.loads(
            json.dumps(
                {
                    "vendor": {"id": "oshpark", "title": "OSH Park", "country": "US"},
                    "service": {"id": "standard", "title": "4 Layer"},
                    "profile": {
                        "name": "oshpark-4l",
                        "status": "active",
                        "title": "OSH Park 4 Layer",
                    },
                    "board": {
                        "name": "rf-divider",
                        "size_mm": [50.0, 50.0],
                        "area_sq_in": 3.875,
                        "input_id": "3f2a" * 16,
                        "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                    },
                    "stackup": {
                        "id": "oshpark-4l-fr408hr",
                        "summary": "Cu / FR408HR",
                        "checkout": "Keep FR408-HR.",
                    },
                    "options": {
                        "thickness_mm": 1.6,
                        "finish": "ENIG",
                        "mask_color": "purple",
                        "via_covering": "tented",
                        "copper": "1 oz",
                        "fixed": True,
                    },
                    "impedance": "not controlled",
                    "qty": {"value": 3, "rule": "multiples of 3"},
                    "estimate": {
                        "usd": 38.75,
                        "formula": "3.875 sq in x $10 per set of 3",
                        "source": "https://docs.oshpark.com/services/ 2026-10-02",
                    },
                    "checks": {
                        "summary": {"error": 0, "warning": 1, "info": 1},
                        "drc_errors": 0,
                        "drc_warnings": 0,
                        "notable": ["FAB-ALTERNATE (info): keep FR408-HR"],
                    },
                    "files": {
                        "upload": {
                            "name": "z.zip",
                            "path": "rf/z.zip",
                            "sha256": "9c41" * 16,
                            "bytes": 214000,
                            "members": 12,
                        }
                    },
                    "assembly": None,
                    "staging": {
                        "url": "https://oshpark.com/",
                        "steps": ["Drop z.zip on the page."],
                    },
                    "warnings": [],
                }
            )
        )
        text = card_mod.text(card)
        self.assertTrue(text.startswith("ORDER CARD  staging only: yapnr uploads nothing"))
        for expected in (
            "vendor      OSH Park (US), 4 Layer service",
            "board       rf-divider   50.00 x 50.00 mm   3.875 sq in",
            "CHECKOUT    Keep FR408-HR.",
            "finish/mask ENIG / purple (fixed)   via covering: tented",
            "estimate    $38.75 (3.875 sq in x $10 per set of 3;",
            "            FAB-ALTERNATE (info): keep FR408-HR",
            "next        1. Drop z.zip on the page.",
        ):
            self.assertIn(expected, text)
        md = card_mod.markdown(card)
        self.assertIn("| CHECKOUT | Keep FR408-HR. |", md)
        self.assertEqual(json.loads(card_mod.to_json(card)), card)


class VendorsTest(unittest.TestCase):
    def test_mechanisms_name_what_is_not_built(self):
        osh = {m["mechanism"]: m["status"] for m in vendors.mechanisms("oshpark")}
        self.assertEqual(osh["manual"], "available (O1)")
        self.assertEqual(osh["import-url"], "available (O1)")
        self.assertIn("not built", osh["upload"])
        self.assertIn("not built", osh["api"])
        jlc = {m["mechanism"] for m in vendors.mechanisms("jlcpcb")}
        self.assertNotIn("import-url", jlc)


if __name__ == "__main__":
    unittest.main()
