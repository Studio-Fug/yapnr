"""Write a copy of a source board containing only the listed footprints (KiCad python).

    python -m pnr.hier.subpcb source.kicad_pcb out.kicad_pcb --keep refs.json
"""
import argparse
import json


def main():
    import pcbnew
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source')
    ap.add_argument('out')
    ap.add_argument('--keep', required=True)
    a = ap.parse_args()
    keep = set(json.load(open(a.keep)))
    board = pcbnew.LoadBoard(a.source)
    removed = 0
    present = set()
    # Read every reference before mutating: this SWIG build returns raw pointers
    # from GetFootprints() once the collection has changed.
    for fp in [(fp.GetReference(), fp) for fp in board.GetFootprints()]:
        ref, fp = fp
        if ref in keep:
            present.add(ref)
        else:
            board.Remove(fp)
            removed += 1
    # Source boards carry no copper; pnr.writeback clears tracks on placement anyway.
    missing = keep - present
    if missing:
        raise SystemExit('missing footprints: %s' % sorted(missing))
    board.BuildConnectivity()
    pcbnew.SaveBoard(a.out, board)
    print(json.dumps(dict(kept=len(keep), removed=removed)), flush=True)
    import os
    os._exit(0)  # KiCad's python can crash during interpreter teardown


if __name__ == '__main__':
    main()
