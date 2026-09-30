"""``yapnr atopile build`` on its own: the action of bazel/atopile/defs.bzl (yapnr_atopile_build).

The same command as the ``yapnr`` CLI, without the engine's numerical stack.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from yapnr.frontends.atopile.cli import register_atopile


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="yapnr-atopile-build")
    register_atopile(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["atopile", "build", *(sys.argv[1:] if argv is None else argv)])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
