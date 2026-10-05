"""Register source footprint libraries for independent KiCad DRC.

:func:`library_table` builds a portable ``fp-lib-table`` from a list of ``.kicad_mod`` files,
grouped by their parent directory (the library nickname). :func:`extract_footprints` makes
those files in the first place, for a board whose footprints have no checked-in source library
on disk (generated or vendor-cached parts, embedded in the ``.kicad_pcb`` with a synthetic lib
nickname): every distinct ``nickname:name`` footprint on the board is saved out, one KiCad
library directory per nickname, from the board's own copy (so it matches exactly; nothing is
reconstructed). ``--from-board`` wires this into the CLI: the usual failure mode this clears is
KiCad DRC's ``lib_footprint_issues`` ("the current configuration does not include the footprint
library ...") for every nickname the project's ``fp-lib-table`` doesn't know about.
"""

import argparse
import json
from pathlib import Path


def library_table(files, portable=False):
    libraries = {}
    for filename in files:
        directory = Path(filename).resolve().parent
        name = directory.name
        if name in libraries and libraries[name] != directory:
            raise ValueError(f"Ambiguous footprint library {name}")
        libraries[name] = directory
    rows = []
    for name, directory in sorted(libraries.items()):
        uri = "${KIPRJMOD}/footprints/" + name if portable else str(directory)
        rows.append(
            f'  (lib (name {json.dumps(name)}) (type "KiCad") '
            f'(uri {json.dumps(uri)}) (options "") (descr "Source library"))'
        )
    return "(fp_lib_table (version 7)\n" + "\n".join(rows) + "\n)\n"


def extract_footprints(board, out_dir):
    """Every distinct footprint on ``board`` (a path, or an already-loaded ``pcbnew.BOARD``),
    saved as its own KiCad library file under ``out_dir/<lib nickname>/<name>.kicad_mod``: one
    library directory per nickname, matching what :func:`library_table` groups by. Footprints
    with no lib nickname (board-local, generated in place) are skipped; they need no library
    entry. Returns the written paths, sorted, deduplicated by (nickname, name) so a part used
    many times over is saved once."""
    import pcbnew as k

    out_dir = Path(out_dir)
    b = board if hasattr(board, "GetFootprints") else k.LoadBoard(str(board))
    plug = k.PCB_IO_MGR.FindPlugin(k.PCB_IO_MGR.KICAD_SEXP)
    seen = set()
    written = []
    for fp in b.GetFootprints():
        lib_id = fp.GetFPID()
        nickname, name = str(lib_id.GetLibNickname()), str(lib_id.GetLibItemName())
        if not nickname or (nickname, name) in seen:
            continue
        seen.add((nickname, name))
        lib_dir = out_dir / nickname
        lib_dir.mkdir(parents=True, exist_ok=True)
        plug.FootprintSave(str(lib_dir), fp)
        written.append(lib_dir / (name + ".kicad_mod"))
    return sorted(written)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--portable", action="store_true")
    parser.add_argument(
        "--from-board",
        help="extract every distinct footprint from this .kicad_pcb into --libs-dir first "
        "(requires KiCad's own Python: pcbnew), then build the table from those files",
    )
    parser.add_argument(
        "--libs-dir", help="where --from-board writes the extracted libraries (required with it)"
    )
    parser.add_argument("files", nargs="*", help="extra .kicad_mod files, beside --from-board's")
    args = parser.parse_args()
    files = [Path(f) for f in args.files]
    if args.from_board:
        if not args.libs_dir:
            parser.error("--from-board needs --libs-dir")
        files += extract_footprints(args.from_board, args.libs_dir)
    Path(args.out).write_text(library_table(files, args.portable))


if __name__ == "__main__":
    main()
