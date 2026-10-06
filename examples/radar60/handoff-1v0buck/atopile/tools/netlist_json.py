#!/usr/bin/env python3
"""Write a pad-level JSON netlist of a .kicad_pcb using KiCad's headless pcbnew.

Run with KiCad 10's bundled Python (pcbnew must be importable):
    python3 netlist_json.py BOARD.kicad_pcb OUT.json

Output: {"board": <basename>, "kicad": <version>, "pads": [{"ref", "pad",
"pinfunction", "pintype", "net"}...]} sorted by (ref, pad). Copper is not
consulted: the net is the one KiCad stores on each pad.
"""
import json
import os
import sys

import pcbnew


def main(board_path: str, out_path: str) -> None:
    board = pcbnew.LoadBoard(board_path)
    pads = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            pads.append(
                {
                    "ref": ref,
                    "pad": pad.GetNumber(),
                    "pinfunction": pad.GetPinFunction() or None,
                    "pintype": pad.GetPinType() or None,
                    "net": pad.GetNetname() or "",
                }
            )
    pads.sort(key=lambda p: (p["ref"], p["pad"]))
    with open(out_path, "w") as f:
        json.dump(
            {"board": os.path.basename(board_path), "kicad": pcbnew.Version(), "pads": pads},
            f,
            indent=1,
        )
        f.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
