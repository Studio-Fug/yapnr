"""KiCad smoke test, run inside a yapnr or yapnr-kicad image by tools/image/smoke_image.sh.

    yapnr-kicad-env /usr/bin/python3 /smoke/pcbnew_smoke.py KICAD_VERSION

With KiCad's own Python (the one with pcbnew):

1. find the Resistor_SMD library in the user's global footprint table (seeded by
   yapnr-kicad-env from /etc/yapnr/kicad-seed) and expand its ${KICAD10_FOOTPRINT_DIR} URI;
2. load a footprint from it with pcbnew and put it on a new board, which is saved;
3. run `kicad-cli pcb drc` on the saved board and read the JSON report;
4. check that no GUI session is configured (DISPLAY, WAYLAND_DISPLAY).

Prints one JSON line with what it saw; exits non-zero on the first failure. Runs with
KiCad's Python (3.12 in the image), so it stays stdlib-only.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

LIBRARY = "Resistor_SMD"
FOOTPRINT = "R_0603_1608Metric"
TIMEOUT_S = 120


def fail(message):
    print(f"pcbnew smoke: FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def library_uri(table_path, nickname):
    with open(table_path, encoding="utf-8") as handle:
        text = handle.read()
    pattern = (
        r'\(lib\s+\(name\s+"?' + re.escape(nickname) + r'"?\)\s*'
        r'\(type\s+"?[^")]*"?\)\s*\(uri\s+"?([^")]+)"?\)'
    )
    match = re.search(pattern, text)
    if not match:
        fail(f"library {nickname} is not in {table_path}")
    return match.group(1), os.path.expandvars(match.group(1))


def main():
    if len(sys.argv) != 2:
        fail("usage: pcbnew_smoke.py KICAD_VERSION")
    expected = sys.argv[1]
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        if os.environ.get(var):
            fail(f"{var} is set; the image must run without a GUI session")

    series = os.environ.get("YAPNR_KICAD_SERIES", "")
    table = os.path.join(os.environ["HOME"], ".config", "kicad", series, "fp-lib-table")
    raw_uri, uri = library_uri(table, LIBRARY)
    if "${" in uri or not os.path.isdir(uri):
        fail(f"{LIBRARY} resolves to {uri!r} (from {raw_uri!r})")

    import pcbnew  # noqa: E402  (KiCad's module; only after the environment checks)

    version = pcbnew.Version()
    if not (version == expected or version.startswith((expected + "-", expected + "+"))):
        fail(f"pcbnew reports {version}, expected {expected}")

    footprint = pcbnew.FootprintLoad(uri, FOOTPRINT)
    if footprint is None:
        fail(f"pcbnew could not load {LIBRARY}:{FOOTPRINT} from {uri}")
    pads = len(footprint.Pads())

    work = tempfile.mkdtemp(prefix="yapnr-smoke-")
    board_path = os.path.join(work, "smoke.kicad_pcb")
    board = pcbnew.NewBoard(board_path)
    board.Add(footprint)
    if not pcbnew.SaveBoard(board_path, board):
        fail(f"pcbnew could not save {board_path}")

    report_path = os.path.join(work, "drc.json")
    result = subprocess.run(
        [
            os.environ.get("YAPNR_KICAD_CLI", "kicad-cli"),
            "pcb",
            "drc",
            "--format",
            "json",
            "--output",
            report_path,
            board_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0 or not os.path.isfile(report_path):
        fail(f"kicad-cli pcb drc exited {result.returncode}:\n{result.stdout}")
    with open(report_path, encoding="utf-8") as handle:
        report = json.load(handle)
    if "violations" not in report:
        fail(f"unexpected DRC report: {sorted(report)}")

    print(
        json.dumps(
            {
                "uid": os.getuid(),
                "home": os.environ["HOME"],
                "pcbnew": version,
                "footprint": f"{LIBRARY}:{FOOTPRINT}",
                "pads": pads,
                "drc_violations": len(report["violations"]),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
