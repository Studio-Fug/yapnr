#!/usr/bin/env python3
"""Privacy scan: keep machine- and person-specific details out of a public repo.

yapnr is a public repository, and its engine grew up on a private workstation.
This scanner is the gate that keeps local details out of commits, history
imports and bundles. It looks for generic patterns only, so the scanner itself
never needs to name the details it protects:

- absolute home-directory paths (macOS, Linux, Windows, WSL), mounted-volume
  paths (macOS volumes, Linux removable media), macOS per-user temporary
  directories, and absolute paths that agent tooling encodes with dashes;
- Tailscale ``*.ts.net`` host names and mDNS ``*.local`` machine names;
- carrier-grade NAT addresses (``100.64.0.0/10``, used by tailnets) and
  private (RFC 1918) addresses;
- e-mail addresses, except GitHub noreply addresses, ``noreply``/``no-reply``
  mailboxes, the git SSH user of code hosts, and reserved example domains. An
  ``@`` match counts as an address only if its local part has a letter or
  digit, it is not a call (followed by ``(``), and its top-level domain is in
  the IANA root zone (``tools/privacy/iana_tlds.txt``), so code such as a
  decorator on an added patch line, a matrix product or an ``x@y.z`` endpoint
  is not an address;
- API keys, access tokens and private-key blocks.

Findings are printed with the matched text redacted, so running the scan in a
public CI log does not leak what it found.

Usage::

    tools/privacy_scan.py FILE...          # what the pre-commit hook runs
    tools/privacy_scan.py --all [--root R] # every tracked or untracked file
    git log -p --diff-merges=separate origin/main..HEAD | tools/privacy_scan.py --stdin
    git log --format='%ae%n%ce' origin/main..HEAD | tools/privacy_scan.py --identities

``--identities`` is stricter than the text rules: it reads one commit e-mail
address per line and accepts only GitHub noreply addresses, GitHub's own
committer address and the addresses listed in
``tools/privacy/allowed_identities.txt`` (the owner's public commit address;
see ``check_identities``). The CI ``lint`` job runs it on every new commit.

File contents stay strict: an allowlisted commit address in a file is a
finding like any other personal address (only the allowlist file itself is
exempt). ``--stdin`` scans commit history (``git log -p`` with the identity
header, messages and patches), where these addresses appear by design, so it
accepts them.

A line that must contain a match (for example documentation of a pattern) can
carry the marker ``privacy-scan: allow``. Use it sparingly; reviewers see it.

The module is stdlib-only and Python 3.9 compatible, so it also runs under
KiCad's bundled Python and on bare CI runners.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import (
    AbstractSet,
    Callable,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Optional,
    Pattern,
    Sequence,
)

# The commit addresses `--identities` accepts besides GitHub noreply addresses
# (repository-relative; read next to this file, so it also works under Bazel).
ALLOWED_IDENTITIES_FILE = "tools/privacy/allowed_identities.txt"

# The top-level domains of the IANA root zone, a vendored static copy with its
# source and retrieval date (repository-relative; read next to this file). An
# `@` match counts as an e-mail address only if its top-level domain is listed.
PUBLIC_TLDS_FILE = "tools/privacy/iana_tlds.txt"

# Repository-relative files that are exempt: the scanner (its patterns), its
# test (synthetic samples), the migration plan (which describes the scrub) and
# the commit address allowlist (which must name the addresses).
ALLOWLISTED_FILES = frozenset(
    {
        "tools/privacy_scan.py",
        "tests/unit/repo/test_privacy_scan.py",
        "docs/migration-plan.md",
        ALLOWED_IDENTITIES_FILE,
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

# Placeholder account names that are fine in documentation and CI paths. A
# placeholder must be the whole path component: `user` passes, `user.x` does not.
_PLACEHOLDER_USERS = "Shared|runner|user|username|example|you|me"
_NOT_PLACEHOLDER = r"(?!(?:" + _PLACEHOLDER_USERS + r")(?![A-Za-z0-9._-]))"
_NOT_WINDOWS_PLACEHOLDER = r"(?!(?:Public|Default|" + _PLACEHOLDER_USERS + r")(?![A-Za-z0-9._-]))"
# An account name starts with a letter, digit or underscore, so `/Users/...`
# (an ellipsis in prose) is not a path.
_ACCOUNT = r"[A-Za-z0-9_][A-Za-z0-9._-]*"

# A path must start a token: not glued to a word, a URL host or a relative path.
_PATH_START = r"(?<![\w.~-])"

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
# An IPv4 address must not be glued to a longer dotted number on either side.
_IP_START = r"(?<![\d.])"
_IP_END = r"(?![\d.]*\d)"

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


def parse_public_tlds(text: str, path: str = PUBLIC_TLDS_FILE) -> FrozenSet[str]:
    """Top-level domains of a root zone list (lower case): one per line, `#` starts a comment."""
    tlds = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not re.fullmatch(r"[A-Za-z0-9-]+", entry):
            raise ValueError(f"{path}:{lineno}: expected one top-level domain per line")
        tlds.add(entry.lower())
    return frozenset(tlds)


def load_public_tlds(path: str) -> FrozenSet[str]:
    """The top-level domains listed in ``path``; empty if the file is missing."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_public_tlds(handle.read(), path)
    except FileNotFoundError:
        return frozenset()


@functools.lru_cache(maxsize=None)
def public_tlds() -> FrozenSet[str]:
    """The vendored IANA root zone top-level domains, read next to this file.

    Empty if the file is missing, and then every top-level domain counts (the
    scan fails closed: stricter, never looser).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return load_public_tlds(os.path.join(here, "privacy", os.path.basename(PUBLIC_TLDS_FILE)))


def _is_address(match: "re.Match[str]") -> bool:
    """Whether an `@` match is an e-mail address at all (owner decision, docs/decisions.md).

    Code glues names to `@` too: a top-level decorator on an added or removed
    patch line (`+@unittest.skipIf(`, with the diff marker as the local part),
    a matrix product (`W@field.reshape(`) or an endpoint (`net@board.usbc:A6`).
    A match counts only if all of these hold:

    1. its local part contains a letter or digit;
    2. it is not immediately followed by `(` (a call);
    3. its top-level domain is in the IANA root zone (``public_tlds()``),
       compared in lower case.
    """
    if not re.search(r"[A-Za-z0-9]", match.group(1)):
        return False
    if match.string[match.end() : match.end() + 1] == "(":
        return False
    tlds = public_tlds()
    return not tlds or match.group(2).rsplit(".", 1)[-1].lower() in tlds


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
    # Return False when a regex match is not what the rule looks for after all
    # (a credential that is not random-looking, an `@` that is not an address).
    confirm: Optional[Callable[["re.Match[str]"], bool]] = None


RULES: Sequence[Rule] = (
    Rule(
        "home-path",
        re.compile(_PATH_START + r"/Users/" + _NOT_PLACEHOLDER + _ACCOUNT, re.IGNORECASE),
        "absolute macOS home directory path",
    ),
    Rule(
        "home-path",
        re.compile(_PATH_START + r"/home/" + _NOT_PLACEHOLDER + _ACCOUNT, re.IGNORECASE),
        "absolute Linux home directory path",
    ),
    Rule(
        "home-path",
        re.compile(
            r"\b[A-Za-z]:(?:\\\\?|/)Users(?:\\\\?|/)" + _NOT_WINDOWS_PLACEHOLDER + _ACCOUNT,
            re.IGNORECASE,
        ),
        "absolute Windows home directory path",
    ),
    Rule(
        "home-path",
        re.compile(
            _PATH_START + r"/mnt/[A-Za-z]/Users/" + _NOT_WINDOWS_PLACEHOLDER + _ACCOUNT,
            re.IGNORECASE,
        ),
        "Windows home directory path under WSL",
    ),
    Rule(
        "encoded-path",
        # Agent tooling names per-project directories after the absolute path
        # with every `/` replaced by `-` (for example under ~/.claude/projects).
        re.compile(r"(?<![A-Za-z0-9._-])-(?:Users|home|Volumes)-[A-Za-z0-9._]+-", re.IGNORECASE),
        "absolute path encoded with dashes (agent project directory)",
    ),
    Rule(
        "volume-path",
        re.compile(_PATH_START + r"/Volumes/[A-Za-z0-9][A-Za-z0-9._-]*", re.IGNORECASE),
        "absolute macOS volume path",
    ),
    Rule(
        "volume-path",
        re.compile(_PATH_START + r"(?:/run)?/media/[A-Za-z0-9][A-Za-z0-9._-]*"),
        "absolute Linux removable-media path",
    ),
    Rule(
        "temp-path",
        re.compile(_PATH_START + r"/(?:private/)?var/folders/[A-Za-z0-9_+-]+"),
        "macOS per-user temporary directory",
    ),
    Rule(
        "tailnet-host",
        re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+ts\.net\b", re.IGNORECASE),
        "Tailscale *.ts.net host name",
    ),
    Rule(
        "local-host",
        # macOS names machines `<Owner>-Mac-mini` and the like, so a hyphenated
        # name with the `.local` suffix is a machine; so is any `.local` name
        # right after `//` (a URL host) or `@` (`user@<host>.local`, as in ssh or
        # git's guessed identity: `local` is not in the IANA root zone, so the
        # e-mail rule does not report it).
        re.compile(
            r"(?<![\w.-])(?:[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+|(?<=//)[A-Za-z0-9-]+"
            r"|(?<=@)(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+)\.local\b",
            re.IGNORECASE,
        ),
        "mDNS *.local machine name",
    ),
    Rule(
        "cgnat-address",
        re.compile(
            _IP_START
            + r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\."
            + _OCTET
            + r"\."
            + _OCTET
            + _IP_END
        ),
        "carrier-grade NAT address (100.64.0.0/10, tailnets)",
        # The network address itself names the range; it is not a host.
        allowed=lambda match: match.group(0) == "100.64.0.0",
    ),
    Rule(
        "private-address",
        re.compile(
            _IP_START
            + r"(?:10\."
            + _OCTET
            + r"|172\.(?:1[6-9]|2\d|3[01])|192\.168)\."
            + _OCTET
            + r"\."
            + _OCTET
            + _IP_END
        ),
        "private network address (RFC 1918)",
        # Network addresses (x.x.x.0, as in 192.168.1.0/24) name a range, not a host.
        allowed=lambda match: match.group(0).endswith(".0"),
    ),
    Rule(
        "email",
        re.compile(r"(?<![\w.+%-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])"),
        "personal e-mail address (use a users.noreply.github.com address)",
        allowed=_email_allowed,
        confirm=_is_address,
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


# Commit identities the CI `lint` job accepts: a GitHub noreply address
# (`<id>+<login>@users.noreply.github.com`, the older `<login>@...` form, or a
# GitHub App's `<id>+<app>[bot]@...`), GitHub's own committer address, which
# GitHub uses when it creates a commit (web merges and squashes), and the
# addresses in ALLOWED_IDENTITIES_FILE (the owner's public commit address).
# Anything else fails, including git's guessed `user@host` identity and empty
# addresses.
_NOREPLY_IDENTITY = re.compile(
    r"(?:[0-9]+\+)?[A-Za-z0-9-]+(?:\[bot\])?@users\.noreply\.github\.com"
)
_GITHUB_COMMITTER = "noreply@github.com"
# One whole address, as the `email` rule matches it.
_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
IDENTITIES_PATH = "<identities>"


def parse_allowed_identities(text: str, path: str = ALLOWED_IDENTITIES_FILE) -> FrozenSet[str]:
    """Addresses of an allowlist file (lower case): one per line, `#` starts a comment."""
    addresses = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not _ADDRESS.fullmatch(entry):
            raise ValueError(f"{path}:{lineno}: expected one e-mail address per line")
        addresses.add(entry.lower())
    return frozenset(addresses)


@functools.lru_cache(maxsize=None)
def allowed_identities() -> FrozenSet[str]:
    """The repository's allowlisted commit addresses; empty if the file is missing."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "privacy", os.path.basename(ALLOWED_IDENTITIES_FILE))
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_allowed_identities(handle.read())
    except FileNotFoundError:
        return frozenset()  # fail closed: only noreply addresses pass


def identity_allowed(address: str, allowed: Optional[AbstractSet[str]] = None) -> bool:
    """Whether a commit address passes; ``allowed`` defaults to ``allowed_identities()``."""
    if address == _GITHUB_COMMITTER or _NOREPLY_IDENTITY.fullmatch(address):
        return True
    return address.lower() in (allowed_identities() if allowed is None else allowed)


def check_identities(text: str, allowed: Optional[AbstractSet[str]] = None) -> List[Finding]:
    """One commit e-mail address per line (`git log --format='%ae%n%ce'`)."""
    findings: List[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        address = line.strip()
        if identity_allowed(address, allowed):
            continue
        findings.append(
            Finding(
                path=IDENTITIES_PATH,
                line=lineno,
                column=1,
                rule="identity",
                description="commit address is neither a GitHub noreply address nor allowlisted",
                redacted=redact(address) if address else "empty",
            )
        )
    return findings


def _address_in(address: str, addresses: AbstractSet[str]) -> bool:
    return bool(addresses) and address.lstrip("+-").lower() in addresses


def scan_text(
    text: str, path: str = "<text>", commit_addresses: AbstractSet[str] = frozenset()
) -> List[Finding]:
    """Scan text line by line.

    ``commit_addresses`` (lower case) are e-mail addresses to accept; only the
    commit history scan (``--stdin``) passes the allowlisted commit addresses.
    """
    findings: List[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if INLINE_ALLOW_MARKER in line:
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                if rule.allowed is not None and rule.allowed(match):
                    continue
                # A `git log -p` line starts with its diff marker (`+`, `-`), which
                # the address pattern takes into the local part.
                if rule.name == "email" and _address_in(match.group(0), commit_addresses):
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
    identities = [finding for finding in findings if finding.rule == "identity"]
    if identities:
        yield (
            f"privacy scan: {len(identities)} commit address(es) rejected. Commits must be "
            "authored and committed with an address listed in "
            f"{ALLOWED_IDENTITIES_FILE} (the owner's public commit address) or a GitHub "
            "noreply address (<id>+<login>@users.noreply.github.com): set it with "
            "`git config user.email`, then rewrite the commits (for example `git rebase -r "
            "<base> --exec 'git commit --amend --no-edit --reset-author'`). "
            "See CONTRIBUTING.md."
        )
    content = [finding for finding in findings if finding.rule != "identity"]
    if content:
        yield (
            f"privacy scan: {len(content)} finding(s). Replace machine paths with "
            "repository-relative or ~ paths, host names and addresses with documentation "
            "values (example.com, 192.0.2.0/24), and e-mail addresses with GitHub noreply "
            "addresses (allowlisted commit addresses belong in commits, not in files). "
            "See CONTRIBUTING.md."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="files to scan (repository-relative)")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="scan standard input as commit history (`git log -p`), which may carry the "
        f"commit addresses listed in {ALLOWED_IDENTITIES_FILE}",
    )
    parser.add_argument(
        "--identities",
        action="store_true",
        help="read one commit e-mail address per line from standard input; accept only "
        "GitHub noreply addresses, noreply@github.com and the addresses in "
        f"{ALLOWED_IDENTITIES_FILE}",
    )
    parser.add_argument("--all", action="store_true", help="scan every file in the repository")
    parser.add_argument("--root", default=None, help="repository root for --all (default: cwd)")
    parser.add_argument("--list-rules", action="store_true", help="print the rules and exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        for rule in RULES:
            print(f"{rule.name:14} {rule.description}")
        return 0

    if args.stdin and args.identities:
        parser.error("--stdin and --identities both read standard input; pass one")

    findings: List[Finding] = []
    if args.identities:
        findings.extend(check_identities(sys.stdin.read()))
    if args.stdin:
        findings.extend(scan_text(sys.stdin.read(), "<stdin>", allowed_identities()))
    if args.all:
        findings.extend(scan_tree(os.path.abspath(args.root or os.getcwd())))
    if args.files:
        findings.extend(scan_files(args.files, root=args.root))
    if not (args.stdin or args.identities or args.all or args.files):
        parser.error("nothing to scan: pass FILE..., --stdin, --identities or --all")

    for line in _iter_report(findings):
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
