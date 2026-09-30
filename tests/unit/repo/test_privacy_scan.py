"""Tests for tools/privacy_scan.py, plus the whole-tree privacy gate.

Sample values are assembled at runtime so that this file never contains a
literal that looks like a real path, address or credential (hosting providers
scan pushes for token shapes). The file is also allowlisted in the scanner.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
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
# The owner's public commit address, listed in tools/privacy/allowed_identities.txt.
OWNER = "fughilli" + AT + "gmail" + ".com"

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

    def test_stdin_mode_accepts_allowlisted_commit_addresses(self):
        # `git log -p --format='%ae %ce%n%B'`: the identity header, a trailer and
        # the patch that adds the allowlist entry all carry the owner's address.
        history = (
            OWNER + " " + OWNER + "\n"
            "Co-Authored-By: Kevin <" + OWNER.upper() + ">\n"
            "+" + OWNER + "\n"
        )
        self.assertEqual(_run_main(["--stdin"], history), (0, ""))
        # Any other personal address in the history is still a finding.
        code, out = _run_main(["--stdin"], history + "+jane.doe" + AT + "gmail.com\n")
        self.assertEqual(code, 1)
        self.assertIn("<stdin>:4:1: email:", out)
        self.assertNotIn("jane.doe", out)

    def test_allowlisted_commit_address_in_file_contents_is_a_finding(self):
        for sample in (OWNER, "Kevin <" + OWNER + ">", "mailto:" + OWNER):
            with self.subTest(sample=privacy_scan.redact(sample)):
                findings = privacy_scan.scan_text(sample, "README.md")
                self.assertEqual([f.rule for f in findings], ["email"])
        with tempfile.TemporaryDirectory() as root:
            for rel in ("README.md", privacy_scan.ALLOWED_IDENTITIES_FILE):
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w", encoding="utf-8") as handle:
                    handle.write(OWNER + "\n")
            findings = privacy_scan.scan_tree(root)
            # Only the allowlist file itself is exempt.
            self.assertEqual([(f.path, f.rule) for f in findings], [("README.md", "email")])
            code, out = _run_main([os.path.join(root, "README.md")], "")
            self.assertEqual(code, 1)
            self.assertNotIn(OWNER, out)


# Commit identities (`--identities`, the CI lint job). Only GitHub noreply
# addresses, GitHub's own committer address and the addresses in
# tools/privacy/allowed_identities.txt pass.
IDENTITY_OK = [
    "6869039+fughilli" + NOREPLY,
    "fughilli" + NOREPLY,
    "41898282+github-actions[bot]" + NOREPLY,
    "noreply" + AT + "github.com",
    OWNER,
    OWNER.upper(),  # addresses are matched without regard to case
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
    "fughilli.x" + AT + "gmail.com",  # a different address at the same provider
    "xfughilli" + AT + "gmail.com",
    "fughilli" + AT + "gmail.co",
    "fughilli" + AT + "googlemail.com",
    OWNER + ".example.net",
    "6869039+fughilli" + NOREPLY + ".example.net",
    "evil" + AT + "x.users.noreply.github.com",
    "6869039+fughilli" + NOREPLY + " 6869039+fughilli" + NOREPLY,  # two per line
    OWNER + " " + OWNER,
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
        bad_line = len(IDENTITY_OK) + 1
        self.assertIn(f":{bad_line}:1: identity:", out)
        self.assertIn(f":{bad_line + 1}:1: identity:", out)
        self.assertNotIn("macmini", out)

    def test_identities_and_stdin_are_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _run_main(["--identities", "--stdin"], "")

    def test_allowlist_is_the_only_extra_source(self):
        # With an empty allowlist only the GitHub addresses pass.
        self.assertFalse(privacy_scan.identity_allowed(OWNER, allowed=frozenset()))
        self.assertTrue(privacy_scan.identity_allowed("noreply" + AT + "github.com", frozenset()))
        other = "jane.doe" + AT + "gmail.com"
        self.assertTrue(privacy_scan.identity_allowed(other, allowed=frozenset({other})))
        self.assertEqual(privacy_scan.check_identities(other + "\n", frozenset({other})), [])

    def test_parse_allowlist(self):
        text = (
            "# comment\n"
            "\n"
            "  " + OWNER.upper() + "  # trailing comment\n"
            "other" + AT + "example.org\n"
        )
        self.assertEqual(
            privacy_scan.parse_allowed_identities(text),
            frozenset({OWNER, "other" + AT + "example.org"}),
        )
        for bad in (
            "not-an-address",
            OWNER + " " + OWNER,
            "*" + AT + "gmail.com",
            AT + "gmail.com",
        ):
            with self.subTest(bad=privacy_scan.redact(bad)):
                with self.assertRaisesRegex(ValueError, r"allowed_identities\.txt:2:"):
                    privacy_scan.parse_allowed_identities("# ok\n" + bad + "\n")


class TreeTest(unittest.TestCase):
    """The gate: the working tree must be clean."""

    @classmethod
    def setUpClass(cls):
        cls.root = workspace_root()

    def test_allowlist_entries_exist(self):
        for rel in sorted(privacy_scan.ALLOWLISTED_FILES):
            with self.subTest(path=rel):
                self.assertTrue(os.path.isfile(os.path.join(self.root, rel)), "stale allowlist")

    def test_identity_allowlist_is_loaded(self):
        # The copy next to the scanner (under Bazel: a data dependency) is the
        # checkout's file, and it names the owner's public commit address.
        with open(
            os.path.join(self.root, privacy_scan.ALLOWED_IDENTITIES_FILE), encoding="utf-8"
        ) as handle:
            in_checkout = privacy_scan.parse_allowed_identities(handle.read())
        self.assertEqual(privacy_scan.allowed_identities(), in_checkout)
        self.assertIn(OWNER, in_checkout)

    def test_tree_is_clean(self):
        files = privacy_scan.list_repo_files(self.root)
        self.assertIn("MODULE.bazel", files)
        findings = privacy_scan.scan_tree(self.root)
        self.assertEqual([f.format() for f in findings], [])


if __name__ == "__main__":
    unittest.main()
