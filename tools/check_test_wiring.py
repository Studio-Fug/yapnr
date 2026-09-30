#!/usr/bin/env python3
"""Check that every test file is wired to a Bazel target.

A test file that no BUILD file mentions never runs, and nobody notices: Splanc
accumulated dozens of them. This check finds every ``test_*.py`` under the
scoped directories (``tests/`` by default) and requires that its Bazel package
(the nearest directory with a BUILD file) either

- calls ``yapnr_py_tests(`` (tools/bazel/py_tests.bzl), which generates one
  ``py_test`` per ``test_*.py`` directly in the package, or
- names the file, relative to the package, as a string in the BUILD file.

Usage::

    tools/check_test_wiring.py [--root R] [--scope tests/ ...]

Stdlib-only, Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from tools.privacy_scan import list_repo_files
except ImportError:  # run as a script from tools/
    from privacy_scan import list_repo_files  # type: ignore[no-redef]

DEFAULT_SCOPES = ("tests/",)
BUILD_FILE_NAMES = ("BUILD.bazel", "BUILD")
GLOB_MACRO_CALL = re.compile(r"^\s*yapnr_py_tests\(", re.MULTILINE)
TEST_FILE = re.compile(r"(?:^|/)test_[^/]*\.py$")


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _package_of(rel_path: str, files: "set[str]") -> Optional[str]:
    """The nearest ancestor directory (repository-relative, "" = root) with a BUILD file."""
    parts = rel_path.split("/")[:-1]
    while True:
        directory = "/".join(parts)
        for name in BUILD_FILE_NAMES:
            if (f"{directory}/{name}" if directory else name) in files:
                return directory
        if not parts:
            return None
        parts.pop()


def find_orphans(
    root: str, files: Sequence[str], scopes: Sequence[str] = DEFAULT_SCOPES
) -> List[Tuple[str, str]]:
    """Return (test file, reason) for every test file that no BUILD file wires."""
    file_set = set(files)
    build_text: Dict[str, str] = {}
    orphans: List[Tuple[str, str]] = []
    for rel in sorted(files):
        if not TEST_FILE.search(rel) or not rel.startswith(tuple(scopes)):
            continue
        package = _package_of(rel, file_set)
        if package is None:
            orphans.append((rel, "no BUILD file in any parent directory"))
            continue
        if package not in build_text:
            for name in BUILD_FILE_NAMES:
                path = os.path.join(root, package, name)
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as handle:
                        build_text[package] = _strip_comments(handle.read())
                    break
        text = build_text.get(package, "")
        in_package = rel[len(package) + 1 :] if package else rel
        if f'"{in_package}"' in text or f"'{in_package}'" in text:
            continue
        if "/" not in in_package and GLOB_MACRO_CALL.search(text):
            continue
        orphans.append((rel, f"not referenced by the BUILD file of //{package}"))
    return orphans


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=os.getcwd(), help="repository root (default: cwd)")
    parser.add_argument(
        "--scope",
        action="append",
        default=None,
        help="repository-relative directory prefix to check (repeatable; default: tests/)",
    )
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    orphans = find_orphans(root, list_repo_files(root), args.scope or DEFAULT_SCOPES)
    for rel, reason in orphans:
        print(f"{rel}: {reason}")
    if orphans:
        print(f"{len(orphans)} test file(s) are not wired to a Bazel target.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
