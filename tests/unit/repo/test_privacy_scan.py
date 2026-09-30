"""Tests for tools/privacy_scan.py, plus the whole-tree privacy gate.

Sample values are assembled at runtime so that this file never contains a
literal that looks like a real path, address or credential (hosting providers
scan pushes for token shapes). The file is also allowlisted in the scanner.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
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
    "local-host": [
        "Alices-Mac-mini" + MDNS,
        "http://buildbox" + MDNS + ":8080/",
        # After `@`: not an e-mail finding (`local` is not in the root zone).
        "ssh pi" + AT + "raspberrypi" + MDNS,
        "+rsync -a out/ alice" + AT + "buildbox" + MDNS + ":~/x",
        "alice" + AT + "macmini.lab" + MDNS,
    ],
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


# What counts as an e-mail address (owner decision, docs/decisions.md): an `@`
# match whose local part has a letter or digit, that is not a call (followed by
# `(`), and whose top-level domain is in the IANA root zone.
#
# The imported engine history (PR1) as `git log -p` prints it: each line is a
# real line of that history, and together they held all 21 matches of the
# address pattern that are code, not addresses.
SKIP = AT + "unittest.skip"
IMPORTED_CODE_HISTORY = (
    # 17 top-level test decorators on added lines and one on a removed line:
    # the diff marker is the whole local part, and each is a call.
    ["+" + SKIP + 'If(pcbnew is None, "requires native KiCad Python")'] * 2
    + ["+" + SKIP + "Unless("] * 2
    + ["+" + SKIP + "Unless(importlib.util.find_spec('pcbnew'), 'requires native KiCad')"] * 11
    + ["+" + SKIP + "Unless(NATIVE, 'requires native KiCad Python')"] * 2
    + ["-" + SKIP + "Unless(importlib.util.find_spec('pcbnew'), 'requires KiCad')"]
    # A matrix product: a call, at a top-level domain that does not exist.
    + [
        "+        optimizer.zero_grad();field=max_move*torch.tanh(nodes);"
        "delta=W" + AT + "field.reshape(-1,2);pos=xy+delta"
    ]
    # Two endpoints in the engine's constraint syntax, at a top-level domain
    # that does not exist.
    + [
        '+        doc = {"diff_pair": [{"name": "usb", "p": "net' + AT + 'board.usbc:A6", '
        '"n": "net' + AT + 'board.usbc:A7"}]}'
    ]
)
IMPORTED_CODE_MATCHES = 21

# Each shape is rejected by exactly one condition of the refinement.
NOT_AN_ADDRESS = {
    "no letter or digit in the local part": ["+" + AT + "gmail.com", "x = ._" + AT + "corp.co.uk"],
    "a call": ["y = W" + AT + "x.to(device)", "jane.doe" + AT + "gmail.com(x)"],
    "no such top-level domain": ["net" + AT + "board.usbc:A6", "a" + AT + "b.skipif"],
}

# Personal addresses stay findings in files, in --stdin and in --all.
PERSONAL_ADDRESSES = [
    "jane.doe" + AT + "gmail.com",
    "someone" + AT + "corp.co.uk",
    "x" + AT + "example.io",  # only example.com/.net/.org are reserved
    "JANE.DOE" + AT + "GMAIL.COM",  # top-level domains compare in lower case
    "12345" + AT + "qq.com",  # a local part of digits only
    "jane.doe" + AT + "gmail.com (work)",  # a space before `(`: not a call
    "mailto:jane.doe" + AT + "gmail.com",
    "jane.doe" + AT + "gmail.com.",  # end of a sentence
    "jane" + AT + "mail.co",
    "jane" + AT + "fastmail.fm",
]
# The same addresses on added and removed lines of a patch: a diff marker glued
# to a real address leaves a letter or digit in the local part.
PERSONAL_PATCH_LINES = ["+" + a for a in PERSONAL_ADDRESSES] + ["-" + a for a in PERSONAL_ADDRESSES]


def _assert_redacted(test, out):
    for address in PERSONAL_ADDRESSES:
        test.assertNotIn(address.split(AT)[1].split(" ")[0].lower(), out.lower())


def _write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


class EmailRuleTest(unittest.TestCase):
    def test_imported_code_matches_the_address_pattern(self):
        # The fixture exercises the refinement: without it, every match is a finding.
        (email,) = [rule for rule in privacy_scan.RULES if rule.name == "email"]
        matches = [m for line in IMPORTED_CODE_HISTORY for m in email.pattern.finditer(line)]
        self.assertEqual(len(matches), IMPORTED_CODE_MATCHES)
        self.assertEqual([m for m in matches if email.allowed(m)], [])

    def test_imported_code_is_not_an_address(self):
        history = "\n".join(IMPORTED_CODE_HISTORY) + "\n"
        self.assertEqual(privacy_scan.scan_text(history, "<stdin>"), [])
        self.assertEqual(_run_main(["--stdin"], history), (0, ""))
        # The same lines as file contents (no diff marker), in files and --all.
        contents = "".join(line[1:] + "\n" for line in IMPORTED_CODE_HISTORY)
        with tempfile.TemporaryDirectory() as root:
            _write_tree(root, {"pnr/elastic.py": contents})
            self.assertEqual(privacy_scan.scan_tree(root), [])
            self.assertEqual(_run_main(["--all", "--root", root], ""), (0, ""))
            self.assertEqual(_run_main([os.path.join(root, "pnr/elastic.py")], ""), (0, ""))

    def test_each_condition_rejects_on_its_own(self):
        for condition, samples in NOT_AN_ADDRESS.items():
            for sample in samples:
                with self.subTest(condition=condition, sample=sample):
                    self.assertEqual(privacy_scan.scan_text(sample), [])

    def test_personal_addresses_are_findings(self):
        for sample in PERSONAL_ADDRESSES:
            with self.subTest(sample=privacy_scan.redact(sample)):
                findings = privacy_scan.scan_text(sample, "README.md")
                self.assertEqual([f.rule for f in findings], ["email"])
        for sample in PERSONAL_PATCH_LINES:
            with self.subTest(sample=privacy_scan.redact(sample)):
                findings = privacy_scan.scan_text(
                    sample, "<stdin>", privacy_scan.allowed_identities()
                )
                self.assertEqual([f.rule for f in findings], ["email"])

    def test_personal_addresses_fail_every_mode(self):
        # Mixed with the imported code: only the addresses are findings.
        history = "\n".join(IMPORTED_CODE_HISTORY + PERSONAL_PATCH_LINES) + "\n"
        code, out = _run_main(["--stdin"], history)
        self.assertEqual(code, 1)
        self.assertEqual(out.count(": email:"), len(PERSONAL_PATCH_LINES))
        _assert_redacted(self, out)
        contents = "".join(line[1:] + "\n" for line in IMPORTED_CODE_HISTORY)
        contents += "\n".join(PERSONAL_ADDRESSES) + "\n"
        with tempfile.TemporaryDirectory() as root:
            _write_tree(root, {"README.md": contents})
            findings = privacy_scan.scan_tree(root)
            self.assertEqual([f.rule for f in findings], ["email"] * len(PERSONAL_ADDRESSES))
            first = len(IMPORTED_CODE_HISTORY) + 1
            self.assertEqual(
                [f.line for f in findings], list(range(first, first + len(PERSONAL_ADDRESSES)))
            )
            for argv in (["--all", "--root", root], [os.path.join(root, "README.md")]):
                with self.subTest(argv=argv[0]):
                    code, out = _run_main(argv, "")
                    self.assertEqual(code, 1)
                    self.assertEqual(out.count(": email:"), len(PERSONAL_ADDRESSES))
                    _assert_redacted(self, out)

    def test_allowlisted_commit_address_is_unchanged(self):
        # Accepted in history (--stdin), a finding in files and in --all.
        history = OWNER + " " + OWNER + "\n+" + OWNER + "\n-" + OWNER + "\n"
        self.assertEqual(_run_main(["--stdin"], history), (0, ""))
        with tempfile.TemporaryDirectory() as root:
            _write_tree(root, {"README.md": OWNER + "\n"})
            self.assertEqual(_run_main(["--all", "--root", root], "")[0], 1)
            self.assertEqual(_run_main([os.path.join(root, "README.md")], "")[0], 1)

    def test_identity_gate_does_not_use_the_refinement(self):
        # `--identities` accepts only its allowlist, whatever the text rule thinks.
        for address in (
            "+" + AT + "gmail.com",
            "net" + AT + "board.usbc",
            "jane.doe" + AT + "gmail.com(",
            "alice" + AT + "buildbox.lan",
        ):
            with self.subTest(address=privacy_scan.redact(address)):
                self.assertFalse(privacy_scan.identity_allowed(address))

    def test_local_machine_after_at_stays_a_finding(self):
        # `user@<host>.local` was an e-mail finding before the refinement; the
        # machine name stays a finding (local-host) in every mode.
        line = "ssh alice" + AT + "macmini" + MDNS + "\n"
        self.assertEqual([f.rule for f in privacy_scan.scan_text(line)], ["local-host"])
        code, out = _run_main(["--stdin"], "+" + line)
        self.assertEqual(code, 1)
        self.assertNotIn("macmini", out)
        with tempfile.TemporaryDirectory() as root:
            _write_tree(root, {"README.md": line})
            for argv in (["--all", "--root", root], [os.path.join(root, "README.md")]):
                with self.subTest(argv=argv[0]):
                    code, out = _run_main(argv, "")
                    self.assertEqual(code, 1)
                    self.assertIn(": local-host:", out)
                    self.assertNotIn("macmini", out)

    def test_missing_tld_list_fails_closed(self):
        # Without the list every top-level domain counts: stricter, never looser.
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(privacy_scan.load_public_tlds(os.path.join(root, "none.txt")), set())
        with mock.patch.object(privacy_scan, "public_tlds", return_value=frozenset()):
            findings = privacy_scan.scan_text("net" + AT + "board.usbc:A6")
            self.assertEqual([f.rule for f in findings], ["email"])
            # The other two conditions do not depend on the list.
            self.assertEqual(privacy_scan.scan_text("+" + AT + "gmail.com"), [])
            self.assertEqual(privacy_scan.scan_text("W" + AT + "x.to(device)"), [])

    def test_parse_tld_list(self):
        text = "# Version 1\n\nCOM\n  io  # comment\nXN--P1AI\n"
        self.assertEqual(privacy_scan.parse_public_tlds(text), frozenset({"com", "io", "xn--p1ai"}))
        for bad in ("co.uk", "c m", AT + "com", "*"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, r"iana_tlds\.txt:2:"):
                    privacy_scan.parse_public_tlds("# ok\n" + bad + "\n")


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

    def test_tld_list_is_loaded(self):
        # The copy next to the scanner is the checkout's file: the IANA root zone.
        path = os.path.join(self.root, privacy_scan.PUBLIC_TLDS_FILE)
        with open(path, encoding="utf-8") as handle:
            in_checkout = privacy_scan.parse_public_tlds(handle.read())
        tlds = privacy_scan.public_tlds()
        self.assertEqual(tlds, in_checkout)
        self.assertGreater(len(tlds), 1000)
        for tld in ("com", "net", "org", "uk", "io", "de", "to", "arpa", "xn--p1ai"):
            with self.subTest(tld=tld):
                self.assertIn(tld, tlds)
        # Names from code, and special-use names that are not in the root zone.
        for name in ("skipif", "skipunless", "reshape", "usbc", "local", "localhost", "test"):
            with self.subTest(name=name):
                self.assertNotIn(name, tlds)

    def test_tld_list_is_the_upstream_file(self):
        # A provenance header, then the upstream file byte for byte from its
        # version line; the header records the upstream checksum.
        with open(os.path.join(self.root, privacy_scan.PUBLIC_TLDS_FILE), "rb") as handle:
            data = handle.read()
        header, version, upstream_rest = data.partition(b"\n# Version ")
        self.assertTrue(version, "no IANA version line")
        upstream = version[1:] + upstream_rest
        self.assertRegex(upstream.split(b"\n", 1)[0], rb"^# Version \d{10}, Last Updated ")
        header = header.decode("utf-8")
        self.assertIn("# Source: https://data.iana.org/TLD/tlds-alpha-by-domain.txt\n", header)
        self.assertRegex(header, r"\n# Retrieved: \d{4}-\d{2}-\d{2} \(UTC\)\n")
        checksum = re.search(r"\n# SHA-256 of the upstream file: ([0-9a-f]{64})\n", header)
        self.assertIsNotNone(checksum, "no upstream checksum")
        self.assertEqual(hashlib.sha256(upstream).hexdigest(), checksum.group(1))

    def test_tree_is_clean(self):
        files = privacy_scan.list_repo_files(self.root)
        self.assertIn("MODULE.bazel", files)
        findings = privacy_scan.scan_tree(self.root)
        self.assertEqual([f.format() for f in findings], [])


if __name__ == "__main__":
    unittest.main()
