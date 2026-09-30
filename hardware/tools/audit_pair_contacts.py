#!/usr/bin/env python3
"""Run with KiCad's Python. No board, project, or rules mutation."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pnr"))
from pnr.pair_contact_audit import audit_pair_contacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return 2 for unqualified timing, otherwise inspection returns 0.",
    )
    args = parser.parse_args()
    import wx

    app = wx.App(False)
    import pcbnew as k

    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    before = {"board": sha(args.board), "rules": sha(args.rules)}
    board = k.LoadBoard(str(args.board))
    rules = json.loads(args.rules.read_text())
    result = audit_pair_contacts(board, rules)
    result.update(
        board=str(args.board.resolve()),
        rules=str(args.rules.resolve()),
        sha256=before,
        kicad_version=k.GetBuildVersion(),
        inputs_unchanged=before == {"board": sha(args.board), "rules": sha(args.rules)},
    )
    if not result["inputs_unchanged"]:
        result["qualified"] = False
    payload = json.dumps(result, indent=2) + "\n"
    if args.out:
        if args.out.resolve() in (
            args.board.resolve(),
            args.rules.resolve(),
            args.board.with_suffix(".kicad_pro").resolve(),
        ):
            raise ValueError("Report must not overwrite input files")
        args.out.write_text(payload)
    print(payload)
    return 2 if not result["inputs_unchanged"] or args.strict and not result["qualified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
