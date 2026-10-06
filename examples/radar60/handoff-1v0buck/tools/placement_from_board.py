"""Write placement.json (ref, x, y, rotation, side) from a KiCad board. Run with KiCad's Python.

The campaign did not collect the select step's placement.json, so this rebuilds it from the routed
board: the board is the placement the route and check steps used.

usage: <kicad python> placement_from_board.py BOARD.kicad_pcb OUT.json
"""

import json
import sys

import pcbnew


def main(board_path, out_path):
    board = pcbnew.LoadBoard(board_path)
    parts = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        parts.append(
            dict(
                ref=fp.GetReference(),
                footprint=str(fp.GetFPID().GetUniStringLibId()),
                x_mm=round(pcbnew.ToMM(pos.x), 4),
                y_mm=round(pcbnew.ToMM(pos.y), 4),
                rotation_deg=round(fp.GetOrientationDegrees(), 3),
                side="bottom" if fp.IsFlipped() else "top",
                locked=bool(fp.IsLocked()),
            )
        )
    parts.sort(key=lambda p: p["ref"])
    doc = dict(
        schema="radar60-handoff-placement-v1",
        source="routed board (KiCad frame, y down)",
        parts=parts,
    )
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
