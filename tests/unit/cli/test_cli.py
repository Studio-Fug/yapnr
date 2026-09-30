"""Tests for the yapnr command-line entry point (PR0 stub: --version, doctor)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from yapnr import __version__, cli


def _module_bazel_path() -> str:
    srcdir = os.environ.get("TEST_SRCDIR")
    if srcdir:
        return os.path.join(srcdir, os.environ.get("TEST_WORKSPACE", "_main"), "MODULE.bazel")
    return os.path.join(os.path.dirname(__file__), "..", "..", "..", "MODULE.bazel")


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse exits for --version and usage errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class VersionTest(unittest.TestCase):
    def test_version_flag(self):
        code, out, _ = _run(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"yapnr {__version__}")

    def test_version_matches_module_bazel(self):
        with open(_module_bazel_path(), encoding="utf-8") as handle:
            text = handle.read()
        match = re.search(r'module\(\s*name = "yapnr",\s*version = "([^"]+)"', text)
        self.assertIsNotNone(match, 'module(name = "yapnr", version = ...) not found')
        self.assertEqual(match.group(1), __version__)

    def test_no_command_prints_usage(self):
        code, out, err = _run([])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("doctor", err)

    def test_python_dash_m(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        result = subprocess.run(
            [sys.executable, "-m", "yapnr", "--version"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"yapnr {__version__}")


class KicadCliStatusTest(unittest.TestCase):
    def test_not_configured(self):
        status = cli.kicad_cli_status({})
        self.assertFalse(status["configured"])
        self.assertIsNone(status["source"])

    def test_blank_value_is_not_configured(self):
        self.assertFalse(cli.kicad_cli_status({"YAPNR_KICAD_CLI": "  "})["configured"])

    def test_legacy_alias_is_accepted(self):
        status = cli.kicad_cli_status({"PNR_KICAD_CLI": "/nonexistent/kicad-cli"})
        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "PNR_KICAD_CLI")
        self.assertFalse(status["exists"])
        self.assertFalse(status["executable"])

    def test_yapnr_variable_wins(self):
        status = cli.kicad_cli_status(
            {"YAPNR_KICAD_CLI": "/opt/a/kicad-cli", "PNR_KICAD_CLI": "/opt/b/kicad-cli"}
        )
        self.assertEqual(status["source"], "YAPNR_KICAD_CLI")
        self.assertEqual(status["path"], "/opt/a/kicad-cli")

    def test_executable_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "kicad-cli")
            with open(fake, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\nexit 99\n")
            os.chmod(fake, 0o644)
            self.assertFalse(cli.kicad_cli_status({"YAPNR_KICAD_CLI": fake})["executable"])
            os.chmod(fake, 0o755)
            status = cli.kicad_cli_status({"YAPNR_KICAD_CLI": fake})
            self.assertTrue(status["exists"])
            self.assertTrue(status["executable"])


class DoctorTest(unittest.TestCase):
    def _doctor(self, argv, environ):
        # The doctor must never start KiCad (or anything else): a stray
        # kicad-cli launch on macOS shows a Dock icon and can hang a GUI.
        forbidden = mock.Mock(side_effect=AssertionError("doctor started a process"))
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(
            subprocess, "Popen", forbidden
        ), mock.patch.object(os, "system", forbidden):
            return _run(argv)

    def test_json_report(self):
        code, out, _ = self._doctor(["doctor", "--json"], {"YAPNR_KICAD_CLI": "/opt/kicad-cli"})
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["yapnr"], __version__)
        self.assertEqual(report["python"], ".".join(map(str, sys.version_info[:3])))
        self.assertIn("numpy", report)
        self.assertIn("torch", report)
        self.assertEqual(report["kicad_cli"]["source"], "YAPNR_KICAD_CLI")

    def test_text_report_when_unconfigured(self):
        code, out, _ = self._doctor(["doctor"], {})
        self.assertEqual(code, 0)
        self.assertIn(f"yapnr     {__version__}", out)
        self.assertIn("kicad-cli not configured (set YAPNR_KICAD_CLI or PNR_KICAD_CLI)", out)

    def test_text_report_flags_non_executable(self):
        code, out, _ = self._doctor(["doctor"], {"PNR_KICAD_CLI": "/nonexistent/kicad-cli"})
        self.assertEqual(code, 0)
        self.assertIn("configured via PNR_KICAD_CLI (NOT EXECUTABLE)", out)


if __name__ == "__main__":
    unittest.main()
