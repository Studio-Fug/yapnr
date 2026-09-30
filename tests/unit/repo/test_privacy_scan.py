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
MDNS = ".lo" + "cal"
AT = "@"
NOREPLY = AT + "users.noreply" + ".github.com"

SHOULD_FLAG = {
    "home-path": [
        MAC_HOME + "alice/src/yapnr",
        "file://" + MAC_HOME + "alice/x",
        "+" + MAC_HOME + "alice/x",  # an added line in `git log -p`
        MAC_HOME.lower() + "alice/x",
        MAC_HOME + "user.alice/x",  # a placeholder name must be the whole component
        LINUX_HOME + "bob/work",
        LINUX_HOME + "runner-x/work",
        "C:\\" + "Users\\carol\\yapnr",
        "C:" + MAC_HOME + "carol/yapnr",
        "/mnt/c" + MAC_HOME + "carol/yapnr",
    ],
    "encoded-path": [
        "~/.claude/projects/-Users" + "-alice-src-yapnr/x.jsonl",
        "/tmp/claude-501/-Volumes" + "-Scratch-Projects-x/y",
        "-home" + "-bob-work-yapnr",
    ],
    "volume-path": [
        VOLUMES + "External/Projects/yapnr",
        VOLUMES.lower() + "external/x",
        "/run/media/" + "dave/usb",
    ],
    "temp-path": ["/private/var" + "/folders/ab/cd1234/T/x", "/var" + "/folders/ab/cd1234"],
    "tailnet-host": ["build-box.tail0000" + TAILNET],
    "local-host": ["Alices-Mac-mini" + MDNS, "http://buildbox" + MDNS + ":8080/"],
    "cgnat-address": ["100." + "64.0.1", "100." + "100.12.34", "http://100." + "127.255.1:8080/"],
    "private-address": ["192." + "168.1.23", "10." + "0.0.5", "http://172." + "20.1.2:8000/"],
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
    MAC_HOME.lower() + "shared/x",
    MAC_HOME + "<name>/project",
    MAC_HOME + "user/project",
    MAC_HOME + "... and /mnt/c" + MAC_HOME + "... (an ellipsis is not an account)",
    LINUX_HOME + "runner/work/yapnr",
    "https://example.com/home/bob",
    "https://api.github.com" + MAC_HOME.lower() + "octocat",
    "docs" + MAC_HOME + "guide.md",
    "~/.claude/projects/" + "-Users-<name>-src/",
    "pass --home-dir-override to the tool",
    "threading" + MDNS + "() and self" + MDNS,
    "/var" + "/folders/<id>/T/",
    "~/Applications/KiCad-headless.app",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    VOLUMES + "<volume>/project",
    "*" + TAILNET,
    "100.63.1.1 and 100.128.0.1 are outside 100.64.0.0/10",
    "192.0.2.10",
    "networks " + "192." + "168.1.0/24, 10." + "0.0.0/8 and 172." + "16.0.0/12",
    "KiCad 10.0.6, torch 2.3.1, 172.32.0.1",
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

    def test_stdin_mode_scans_text(self):
        good = ("6869039+fughilli" + NOREPLY + " ") * 2
        bad = "me" + AT + "home.example-isp.net noreply" + AT + "github.com"
        for text, expected in ((good.strip() + "\n", 0), (bad + "\n", 1)):
            with self.subTest(expected=expected):
                self.assertEqual(_run_main(["--stdin"], text)[0], expected)


# Commit identities (`--identities`, the CI lint job). Only GitHub noreply
# addresses and GitHub's own committer address pass.
IDENTITY_OK = [
    "6869039+fughilli" + NOREPLY,
    "fughilli" + NOREPLY,
    "41898282+github-actions[bot]" + NOREPLY,
    "noreply" + AT + "github.com",
]
IDENTITY_BAD = [
    "",  # user.email unset and no guess
    "   ",
    "alice" + AT + "macmini",  # git's guessed identity: leaks the host name
    "alice" + AT + "macmini.(none)",
    "alice" + AT + "Alices-Mac-mini" + MDNS,
    "root" + AT + "localhost",
    "someone" + AT + "example.com",
    "noreply" + AT + "example-isp.net",
    "jane.doe" + AT + "gmail.com",
    "6869039+fughilli" + NOREPLY + ".example.net",
    "evil" + AT + "x.users.noreply.github.com",
    "6869039+fughilli" + NOREPLY + " 6869039+fughilli" + NOREPLY,  # two per line
]


def _run_main(argv, stdin_text):
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(stdin_text)), contextlib.redirect_stdout(out):
        code = privacy_scan.main(argv)
    return code, out.getvalue()


class IdentityTest(unittest.TestCase):
    def test_noreply_addresses_pass(self):
        for address in IDENTITY_OK:
            with self.subTest(address=address):
                self.assertTrue(privacy_scan.identity_allowed(address))
        self.assertEqual(privacy_scan.check_identities("\n".join(IDENTITY_OK) + "\n"), [])

    def test_other_addresses_fail(self):
        for address in IDENTITY_BAD:
            with self.subTest(address=privacy_scan.redact(address)):
                findings = privacy_scan.check_identities(address + "\n")
                self.assertEqual([f.rule for f in findings], ["identity"])

    def test_cli_reads_one_address_per_line(self):
        # `git log --format='%ae%n%ce'`: author, then committer, per commit.
        good = "\n".join(IDENTITY_OK) + "\n"
        self.assertEqual(_run_main(["--identities"], good), (0, ""))
        self.assertEqual(_run_main(["--identities"], ""), (0, ""))  # empty range
        code, out = _run_main(["--identities"], good + "alice" + AT + "macmini\n\n")
        self.assertEqual(code, 1)
        self.assertIn(":5:1: identity:", out)
        self.assertIn(":6:1: identity:", out)
        self.assertNotIn("macmini", out)

    def test_identities_and_stdin_are_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _run_main(["--identities", "--stdin"], "")


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
