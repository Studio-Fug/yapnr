"""Board graph of a native KiCad board for the cost replay (runs under KiCad's Python).

Usage: ``<kicad python> cost_extract.py <board.kicad_pcb> <out graph.json> <board sha256>``, with
``PYTHONPATH`` set to the engine runtime only (the viewer's ``runtime.kicad_env``). Stdlib plus
``pcbnew`` and ``pnr.ingest``; nothing GUI-bound (no ``wx.App``), 3.9-parseable.
"""

import hashlib
import sys
from pathlib import Path


def main(argv):
    import pcbnew

    from pnr.ingest import build_graph

    board = Path(argv[1])
    assert hashlib.sha256(board.read_bytes()).hexdigest() == argv[3], "Board changed"
    b = pcbnew.LoadBoard(str(board))
    Path(argv[2]).write_text(build_graph(b).to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
