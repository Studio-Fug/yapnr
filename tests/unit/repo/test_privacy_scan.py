"""Tests for tools/privacy_scan.py, plus the whole-tree privacy gate.

Sample values are assembled at runtime so that this file never contains a
literal that looks like a real path, address or credential (hosting providers
scan pushes for token shapes). The file is also allowlisted in the scanner.
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from tools import privacy_scan
from tools.repo_root import workspace_root

SLASH = "/"
MAC_HOME = SLASH + "Users" + SLASH
LINUX_HOME = SLASH + "home" + SLASH
VOLUMES = SLASH + "Volumes" + SLASH
TAILNET = ".ts" + ".net"
AT = "@"

SHOULD_FLAG = {
    "home-path": [
        MAC_HOME + "alice/src/yapnr",
        "file://" + MAC_HOME + "alice/x",
        LINUX_HOME + "bob/work",
        "C:\\" + "Users\\carol\\yapnr",
    ],
    "volume-path": [
        VOLUMES + "External/Projects/yapnr",
        "/run/media/" + "dave/usb",
    ],
    "tailnet-host": ["build-box.tail0000" + TAILNET],
    "cgnat-address": ["100." + "64.0.1", "100." + "100.12.34", "http://100." + "127.255.1:8080/"],
    "email": ["jane.doe" + AT + "gmail.com", "Jane <jd" + AT + "corp.co.uk>"],
    "token": [
        "gh" + "p_" + "A1b2" * 9,
        "github" + "_pat_" + "X" * 60,
        "sk-" + "ant-" + "api03-" + "a" * 30,
        "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "xo" + "xb-" + "1234567890-abcdef",
        'api_key = "' + "ab12" * 8 + '"',
    ],
    "private-key": ["-----BEGIN " + "OPENSSH PRIVATE KEY-----"],
}

SHOULD_PASS = [
    MAC_HOME + "Shared/KiCad",
    MAC_HOME + "<name>/project",
    LINUX_HOME + "runner/work/yapnr",
    "https://example.com/home/bob",
    "~/Applications/KiCad-headless.app",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    VOLUMES + "<volume>/project",
    "*" + TAILNET,
    "100.63.1.1 and 100.128.0.1 are outside 100.64.0.0/10",
    "192.0.2.10",
    "6869039+fughilli" + AT + "users.noreply.github.com",
    "Co-Authored-By: Claude <noreply" + AT + "anthropic.com>",
    "git" + AT + "github.com:Studio-Fug/yapnr.git",
    "someone" + AT + "example.com",
    "uses: actions/checkout" + AT + "v4",
    "bazel-contrib/setup-bazel" + AT + "0.15.0",
    'token: "${{ secrets.GITHUB_TOKEN }}"',
    'password = "' + "a" * 30 + '"',
]


class RuleTest(unittest.TestCase):
    def test_flags_private_values(self):
        for rule, samples in SHOULD_FLAG.items():
            for sample in samples:
                with self.subTest(rule=rule, sample=privacy_scan.redact(sample)):
                    findings = privacy_scan.scan_text(sample)
                    self.assertTrue(findings, "not flagged")
                    self.assertIn(rule, {f.rule for f in findings})

    def test_passes_placeholders_and_allowlisted_values(self):
        for sample in SHOULD_PASS:
            with self.subTest(sample=sample):
                self.assertEqual(privacy_scan.scan_text(sample), [])

    def test_inline_marker_suppresses_a_line(self):
        line = MAC_HOME + "alice/x  # " + privacy_scan.INLINE_ALLOW_MARKER
        self.assertEqual(privacy_scan.scan_text(line), [])

    def test_findings_are_redacted(self):
        secret_path = MAC_HOME + "alice/very/secret/place"
        findings = privacy_scan.scan_text("x = '" + secret_path + "'", "f.py")
        self.assertEqual(len(findings), 1)
        rendered = findings[0].format()
        self.assertNotIn("alice", rendered)
        self.assertTrue(rendered.startswith("f.py:1:"))

    def test_stdin_mode_for_commit_identities(self):
        good = ("6869039+fughilli" + AT + "users.noreply.github.com ") * 2
        bad = "me" + AT + "home.example-isp.net noreply" + AT + "github.com"
        for text, expected in ((good.strip() + "\n", 0), (bad + "\n", 1)):
            with self.subTest(expected=expected):
                out = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(text)), contextlib.redirect_stdout(out):
                    self.assertEqual(privacy_scan.main(["--stdin"]), expected)


class TreeTest(unittest.TestCase):
    """The gate: the working tree must be clean."""

    @classmethod
    def setUpClass(cls):
        cls.root = workspace_root()

    def test_allowlist_entries_exist(self):
        for rel in sorted(privacy_scan.ALLOWLISTED_FILES):
            with self.subTest(path=rel):
                self.assertTrue(os.path.isfile(os.path.join(self.root, rel)), "stale allowlist")

    def test_tree_is_clean(self):
        files = privacy_scan.list_repo_files(self.root)
        self.assertIn("MODULE.bazel", files)
        findings = privacy_scan.scan_tree(self.root)
        self.assertEqual([f.format() for f in findings], [])


if __name__ == "__main__":
    unittest.main()
