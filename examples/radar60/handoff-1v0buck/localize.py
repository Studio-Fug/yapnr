#!/usr/bin/env python3
"""Unpack this handoff bundle into a working directory on this machine.

Copies the bundle to OUT, gunzips every ``*.gz`` file (they were compressed to stay under the
repository's file-size limit) and replaces the two path placeholders in every text file:

``${HANDOFF}``  the unpacked bundle (OUT)
``${REPO}``     the yapnr checkout the flow runs from (default: this file's repository)

Usage: python3 localize.py OUT [--repo PATH]
"""

import argparse
import gzip
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--repo", type=Path, default=HERE.parents[2])
    a = ap.parse_args()
    out = a.out.resolve()
    if out.exists():
        raise SystemExit(f"{out} exists; pick a new directory")
    shutil.copytree(HERE, out, ignore=shutil.ignore_patterns("localize.py", "__pycache__"))
    for gz in sorted(out.rglob("*.gz")):
        with gzip.open(gz, "rb") as src, open(gz.with_suffix(""), "wb") as dst:
            shutil.copyfileobj(src, dst)
        gz.unlink()
    subs = {"${HANDOFF}": str(out), "${REPO}": str(a.repo.resolve())}
    for p in out.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = text
        for k, v in subs.items():
            new = new.replace(k, v)
        if new != text:
            p.write_text(new, encoding="utf-8")
    print(f"unpacked to {out}")


if __name__ == "__main__":
    main()
