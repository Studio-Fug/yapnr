#!/usr/bin/env python3
"""Check that no third-party part data is committed (docs/part-cache.md, "What may be stored").

yapnr commits no EasyEDA-derived part files and no data obtained through a vendor API; such
data lives in a part cache outside every repository. This check fails on:

- an ``.ato`` file whose ``is_auto_generated`` trait names an ``easyeda:`` source (a part made
  by atopile's ``ato create part``);
- any file under a ``cache/parts/easyeda`` directory (atopile's raw EasyEDA cache, which also
  holds the uploader's personal data);
- a ``.step``/``.stp``/``.wrl`` model or a ``.kicad_mod``/``.kicad_sym`` file next to such an
  ``.ato`` file.

Usage::

    tools/check_part_data.py [--root R]

Stdlib-only, Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Sequence, Tuple

try:
    from tools.privacy_scan import list_repo_files
except ImportError:  # run as a script from tools/
    from privacy_scan import list_repo_files  # type: ignore[no-redef]

GENERATED = re.compile(r"trait\s+is_auto_generated\s*<[^>]*source\s*=\s*\"easyeda:", re.S)
RAW_CACHE = re.compile(r"(?:^|/)cache/parts/easyeda/")
PART_FILES = (".kicad_mod", ".kicad_sym", ".step", ".stp", ".wrl")


def find_part_data(root: str, files: Sequence[str]) -> List[Tuple[str, str]]:
    """(file, reason) for every committed file that is third-party part data."""
    findings: List[Tuple[str, str]] = []
    generated_dirs = set()
    for rel in files:
        if RAW_CACHE.search(rel):
            findings.append((rel, "atopile's raw EasyEDA cache"))
            continue
        if rel.endswith(".ato"):
            try:
                with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            if GENERATED.search(text):
                findings.append((rel, "a part generated from EasyEDA data (is_auto_generated)"))
                generated_dirs.add(os.path.dirname(rel))
    for rel in files:
        if os.path.dirname(rel) in generated_dirs and rel.lower().endswith(PART_FILES):
            findings.append((rel, "a file of a part generated from EasyEDA data"))
    return sorted(findings)


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    findings = find_part_data(args.root, list_repo_files(args.root))
    for rel, reason in findings:
        print(f"{rel}: {reason}", file=sys.stderr)
    if findings:
        print("keep part data in a part cache (docs/part-cache.md)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
