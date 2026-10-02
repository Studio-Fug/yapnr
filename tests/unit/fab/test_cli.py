"""``yapnr fab`` and ``yapnr order`` through the top-level command line (fake kicad-cli)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yapnr import cli
from yapnr.fab import testing


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fake = testing.FakeKicadCli()
        for target, value in (
            ("yapnr.fab.build.kicad.find_cli", mock.Mock(return_value=Path("kicad-cli"))),
            ("yapnr.fab.build.kicad.Cli", mock.Mock(side_effect=lambda *a, **k: self.fake)),
            ("webbrowser.open", mock.Mock(side_effect=AssertionError("webbrowser.open"))),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.board = testing.write_board(self.dir, "board", layers=4)

    def test_profiles_and_show(self):
        code, out, _ = run(["fab", "profiles", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("oshpark-4l", [p["name"] for p in json.loads(out)])
        code, out, _ = run(["fab", "show", "oshpark-4l-fr408hr"])
        self.assertIn("nominal W50 0.442 mm", out)
        code, out, _ = run(["fab", "show", "--sources"])
        self.assertIn("https://docs.oshpark.com/services/four-layer/", out)
        code, _, err = run(["fab", "show", "nothing"])
        self.assertEqual(code, 2)

    def test_check_build_and_stage(self):
        out_dir = str(self.dir / "fab")
        code, out, _ = run(
            ["fab", "check", str(self.board), "--profile", "oshpark-4l", "--out", out_dir]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("0 errors", out)
        code, out, _ = run(
            ["fab", "build", str(self.board), "--vendor", "oshpark", "--out", out_dir]
        )
        self.assertEqual(code, 0)
        self.assertIn("ORDER CARD", out)
        manifest = json.loads((self.dir / "fab/board-oshpark-4l/manifest.json").read_text())
        self.assertEqual(manifest["command"][:3], ["yapnr", "fab", "build"])
        gerbers = self.dir / "fab/board-oshpark-4l/board-oshpark-gerbers.zip"
        code, out, _ = run(["fab", "preview", str(gerbers), "--out", str(self.dir / "preview")])
        self.assertEqual(code, 0)
        self.assertIn("composite-top.svg", out)
        self.assertTrue((self.dir / "preview/layer-In1_Cu.svg").is_file())
        code, _, err = run(["fab", "preview", str(self.board)])
        self.assertEqual(code, 2)
        code, out, _ = run(
            [
                "order",
                "stage",
                "--vendor",
                "oshpark",
                "--bundle",
                str(self.dir / "fab/board-oshpark-4l"),
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        code, out, _ = run(
            ["order", "stage", str(self.board), "--vendor", "jlcpcb", "--dry-run", "--out", out_dir]
        )
        self.assertEqual(code, 0)
        self.assertIn("cart.jlcpcb.com", out)

    def test_stage_needs_exactly_one_source(self):
        code, _, err = run(["order", "stage", "--vendor", "oshpark", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("BOARD or --bundle", err)

    def test_no_option_takes_a_credential_or_places_an_order(self):
        import argparse

        def subparsers(parser):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    return action.choices
            return {}

        options = []
        for command in ("fab", "order"):
            for sub in subparsers(subparsers(cli.build_parser())[command]).values():
                options += [o for a in sub._actions for o in a.option_strings]
        self.assertIn("--dry-run", options)
        for option in options:
            for word in ("token", "key", "password", "pay", "api", "upload", "submit", "confirm"):
                self.assertNotIn(word, option, option)

    def test_relative_paths_under_bazel_run(self):
        import os

        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        from yapnr.fab import cli as fab_cli

        fab_cli.from_working_directory({})
        self.assertEqual(os.getcwd(), here)
        fab_cli.from_working_directory({"BUILD_WORKING_DIRECTORY": str(self.dir)})
        self.assertEqual(Path(os.getcwd()).resolve(), self.dir.resolve())

    def test_vendors(self):
        code, out, _ = run(["order", "vendors"])
        self.assertEqual(code, 0)
        self.assertIn("OSH Park (US)", out)
        self.assertIn("O2, not built", out)


if __name__ == "__main__":
    unittest.main()
