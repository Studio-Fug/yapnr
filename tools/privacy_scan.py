#!/usr/bin/env python3
"""Privacy scan: keep machine- and person-specific details out of a public repo.

yapnr is a public repository, and its engine grew up on a private workstation.
This scanner is the gate that keeps local details out of commits, history
imports and bundles. It looks for generic patterns only, so the scanner itself
never needs to name the details it protects:

- absolute home-directory paths (macOS, Linux, Windows) and mounted-volume
  paths (macOS volumes, Linux removable media);
- Tailscale ``*.ts.net`` host names;
- carrier-grade NAT addresses (``100.64.0.0/10``, used by tailnets);
- e-mail addresses, except GitHub noreply addresses, ``noreply``/``no-reply``
  mailboxes, the git SSH user of code hosts, and reserved example domains;
- API keys, access tokens and private-key blocks.

Findings are printed with the matched text redacted, so running the scan in a
public CI log does not leak what it found.

Usage::

    tools/privacy_scan.py FILE...          # what the pre-commit hook runs
    tools/privacy_scan.py --all [--root R] # every tracked or untracked file
    git log --format='%ae %ce' origin/main..HEAD | tools/privacy_scan.py --stdin

A line that must contain a match (for example documentation of a pattern) can
carry the marker ``privacy-scan: allow``. Use it sparingly; reviewers see it.

The module is stdlib-only and Python 3.9 compatible, so it also runs under
KiCad's bundled Python and on bare CI runners.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, List, Optional, Pattern, Sequence

# Repository-relative files that are exempt: the scanner (its patterns), its
# test (synthetic samples) and the migration plan (which describes the scrub).
ALLOWLISTED_FILES = frozenset(
    {
        "tools/privacy_scan.py",
        "tests/unit/repo/test_privacy_scan.py",
        "docs/migration-plan.md",
    }
)

INLINE_ALLOW_MARKER = "privacy-scan: allow"

# Directories never scanned in --all mode when git is unavailable (names, plus
# repository-relative paths for build outputs).
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", ".yapnr", ".bazelisk", "node_modules"})
_SKIP_REL_DIRS = frozenset({"docs/site", "docs/_build"})
# Local, gitignored files that may legitimately hold machine paths.
_SKIP_FILES = frozenset({"user.bazelrc", "yapnr.local.toml"})

_MAX_BYTES = 8 * 1024 * 1024

# Placeholder account names that are fine in documentation and CI paths.
_PLACEHOLDER_USERS = "Shared|runner|user|username|example|you|me|USER|USERNAME"

# A path must start a token: not glued to a word, a URL host or a relative path.
_PATH_START = r"(?<![\w.~-])"

_EXAMPLE_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:com|net|org)|[a-z0-9-]+\.(?:example|invalid|test|localhost))$"
    r"|^(?:example|invalid|test|localhost)$",
    re.IGNORECASE,
)
_CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"})


def _email_allowed(match: "re.Match[str]") -> bool:
    local, domain = match.group(1).lower(), match.group(2).lower()
    if domain == "users.noreply.github.com" or domain.endswith(".users.noreply.github.com"):
        return True
    if local in ("noreply", "no-reply"):
        return True
    if local == "git" and domain in _CODE_HOSTS:
        return True
    return bool(_EXAMPLE_DOMAINS.search(domain))


def _looks_random(match: "re.Match[str]") -> bool:
    value = match.group("value")
    if any(marker in value for marker in ("${", "$(", "<", "{{", "...")):
        return False
    return bool(re.search(r"\d", value) and re.search(r"[A-Za-z]", value))


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: Pattern[str]
    description: str
    # Return True when a regex match is acceptable after all (allowlist).
    allowed: Optional[Callable[["re.Match[str]"], bool]] = None
    # For rules that must NOT match (e.g. random-looking values), False = skip.
    confirm: Optional[Callable[["re.Match[str]"], bool]] = None


RULES: Sequence[Rule] = (
    Rule(
        "home-path",
        re.compile(_PATH_START + r"/Users/(?!(?:" + _PLACEHOLDER_USERS + r")\b)[A-Za-z0-9._-]+"),
        "absolute macOS home directory path",
    ),
    Rule(
        "home-path",
        re.compile(_PATH_START + r"/home/(?!(?:" + _PLACEHOLDER_USERS + r")\b)[A-Za-z0-9._-]+"),
        "absolute Linux home directory path",
    ),
    Rule(
        "home-path",
        re.compile(
            r"\b[A-Za-z]:\\\\?Users\\\\?(?!(?:Public|Default|" + _PLACEHOLDER_USERS + r")\b)"
            r"[A-Za-z0-9._-]+"
        ),
        "absolute Windows home directory path",
    ),
    Rule(
        "volume-path",
        re.compile(_PATH_START + r"/Volumes/[A-Za-z0-9][A-Za-z0-9._-]*"),
        "absolute macOS volume path",
    ),
    Rule(
        "volume-path",
        re.compile(_PATH_START + r"(?:/run)?/media/[A-Za-z0-9][A-Za-z0-9._-]*"),
        "absolute Linux removable-media path",
    ),
    Rule(
        "tailnet-host",
        re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+ts\.net\b", re.IGNORECASE),
        "Tailscale *.ts.net host name",
    ),
    Rule(
        "cgnat-address",
        re.compile(
            r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])"
            r"\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.]*\d)"
        ),
        "carrier-grade NAT address (100.64.0.0/10, tailnets)",
        # The network address itself names the range; it is not a host.
        allowed=lambda match: match.group(0) == "100.64.0.0",
    ),
    Rule(
        "email",
        re.compile(r"(?<![\w.+%-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])"),
        "personal e-mail address (use a users.noreply.github.com address)",
        allowed=_email_allowed,
    ),
    Rule(
        "token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
        "GitHub token",
    ),
    Rule("token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    Rule(
        "token",
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
        "OpenAI-style secret key",
    ),
    Rule("token", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key id"),
    Rule("token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "Slack token"),
    Rule("token", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    Rule("token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    Rule("token", re.compile(r"\btskey-[A-Za-z0-9-]{16,}"), "Tailscale auth key"),
    Rule(
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
        "private key block",
    ),
    Rule(
        "token",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|auth)[A-Za-z0-9_]*\s*[:=]\s*"
            r"['\"](?P<value>[A-Za-z0-9+/=_.-]{24,})['\"]"
        ),
        "hard-coded credential",
        confirm=_looks_random,
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    rule: str
    description: str
    redacted: str

    def format(self) -> str:
        return (
            f"{self.path}:{self.line}:{self.column}: {self.rule}: {self.description} "
            f"[{self.redacted}]"
        )


def redact(text: str) -> str:
    """Show only enough of a match to find it again, never the full value."""
    keep = 4 if len(text) > 12 else 2
    return f"{text[:keep]}...({len(text)} chars)"


def scan_text(text: str, path: str = "<text>") -> List[Finding]:
    findings: List[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if INLINE_ALLOW_MARKER in line:
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                if rule.allowed is not None and rule.allowed(match):
                    continue
                if rule.confirm is not None and not rule.confirm(match):
                    continue
                findings.append(
                    Finding(
                        path=path,
                        line=lineno,
                        column=match.start() + 1,
                        rule=rule.name,
                        description=rule.description,
                        redacted=redact(match.group(0)),
                    )
                )
    return findings


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_BYTES or b"\0" in data[:8192]:
        return None  # large or binary: not a place for hand-written text
    return data.decode("utf-8", errors="replace")


def _normalize(path: str, root: Optional[str]) -> str:
    rel = os.path.relpath(path, root) if root else path
    return rel.replace(os.sep, "/")


def scan_files(paths: Iterable[str], root: Optional[str] = None) -> List[Finding]:
    findings: List[Finding] = []
    for path in paths:
        rel = _normalize(path, root)
        if rel.startswith("./"):
            rel = rel[2:]
        if rel in ALLOWLISTED_FILES:
            continue
        text = _read_text(path)
        if text is None:
            continue
        findings.extend(scan_text(text, rel))
    return findings


def list_repo_files(root: str) -> List[str]:
    """Tracked plus untracked-but-not-ignored files under ``root`` (relative paths).

    Uses git when available, so .gitignore is honoured. Falls back to a
    directory walk that skips build outputs, caches and known local files.
    """
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        files = sorted({name for name in out.decode("utf-8").split("\0") if name})
        return [f for f in files if os.path.isfile(os.path.join(root, f))]
    except (OSError, subprocess.CalledProcessError):
        pass
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIRS
            and not d.startswith(("bazel-", ".bazel-"))
            and _normalize(os.path.join(dirpath, d), root) not in _SKIP_REL_DIRS
        ]
        for name in filenames:
            if name in _SKIP_FILES:
                continue
            files.append(_normalize(os.path.join(dirpath, name), root))
    return sorted(files)


def scan_tree(root: str) -> List[Finding]:
    return scan_files((os.path.join(root, rel) for rel in list_repo_files(root)), root=root)


def _iter_report(findings: Sequence[Finding]) -> Iterator[str]:
    for finding in findings:
        yield finding.format()
    if findings:
        yield (
            f"privacy scan: {len(findings)} finding(s). Replace machine paths with "
            "repository-relative or ~ paths, host names and addresses with documentation "
            "values (example.com, 192.0.2.0/24), and e-mail addresses with GitHub noreply "
            "addresses. See CONTRIBUTING.md."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="files to scan (repository-relative)")
    parser.add_argument("--stdin", action="store_true", help="scan standard input")
    parser.add_argument("--all", action="store_true", help="scan every file in the repository")
    parser.add_argument("--root", default=None, help="repository root for --all (default: cwd)")
    parser.add_argument("--list-rules", action="store_true", help="print the rules and exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        for rule in RULES:
            print(f"{rule.name:14} {rule.description}")
        return 0

    findings: List[Finding] = []
    if args.stdin:
        findings.extend(scan_text(sys.stdin.read(), "<stdin>"))
    if args.all:
        findings.extend(scan_tree(os.path.abspath(args.root or os.getcwd())))
    if args.files:
        findings.extend(scan_files(args.files, root=args.root))
    if not (args.stdin or args.all or args.files):
        parser.error("nothing to scan: pass FILE..., --stdin or --all")

    for line in _iter_report(findings):
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
