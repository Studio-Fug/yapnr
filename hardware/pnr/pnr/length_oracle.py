"""KiCad's own routed length per net: the reference for :mod:`pnr.length_model`.

Runs a probe DRC on a copy of a board whose custom rules hold one
``length (max 0.001mm)`` rule per net, then reads each net's length from the
``length_out_of_range`` reports ("... actual 12.5881 mm"). That is exactly the number
KiCad's ``length`` and ``skew`` constraints compare. Used by tests and by scratch
verification of the length tuner; it is never the judge of a run (the judge is the
run's own DRC with the design's rules).

    python -m pnr.length_oracle BOARD.kicad_pcb --kicad-cli PATH [--net N ...] [--json OUT]

Needs only ``kicad-cli``; no pcbnew. The board's project file is copied along (its
DRC severities and net classes apply), the board's own custom rules are not.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional

PROBE_MAX_MM = 0.001


def board_nets(text: str) -> list:
    """Every named net of a ``.kicad_pcb`` (KiCad 10 ``(net "NAME")`` or numbered)."""
    names = re.findall(r'^\s*\(net\s+(?:\d+\s+)?"([^"]+)"\)', text, flags=re.M)
    return [n for n in dict.fromkeys(names) if n]


def dru_text(nets: Iterable[str]) -> str:
    rules = ["(version 1)"]
    for index, net in enumerate(nets):
        escaped = net.replace("\\", "\\\\").replace("'", "\\'")
        rules.append(
            '(rule "len%d"\n  (condition "A.NetName == \'%s\'")\n'
            "  (constraint length (max %gmm)))" % (index, escaped, PROBE_MAX_MM)
        )
    return "\n".join(rules) + "\n"


def parse_lengths(report: dict, nets: list) -> Dict[str, float]:
    """``{net: mm}`` from a probe DRC report (rules named ``len<index>``)."""
    out: Dict[str, float] = {}
    for item in report.get("violations", []):
        if item.get("type") != "length_out_of_range":
            continue
        rule = re.search(r"rule 'len(\d+)'", item["description"])
        actual = re.search(r"actual ([-\d.]+) mm", item["description"])
        if rule and actual:
            out[nets[int(rule.group(1))]] = float(actual.group(1))
    return out


def measure(
    board: Path,
    kicad_cli: str,
    nets: Optional[Iterable[str]] = None,
    workdir: Optional[Path] = None,
    timeout: float = 600,
) -> Dict[str, float]:
    """KiCad's length of each of ``nets`` (every net when None) on ``board``. A net
    with no copper KiCad can measure is absent from the result."""
    board = Path(board)
    text = board.read_text(encoding="utf-8")
    nets = list(nets) if nets is not None else board_nets(text)
    own = workdir is None
    work = Path(tempfile.mkdtemp(prefix="length-oracle-")) if own else Path(workdir)
    try:
        work.mkdir(parents=True, exist_ok=True)
        probe = work / "probe.kicad_pcb"
        probe.write_text(text, encoding="utf-8")
        pro = board.with_suffix(".kicad_pro")
        if pro.exists():
            shutil.copy2(pro, work / "probe.kicad_pro")
        else:
            (work / "probe.kicad_pro").write_text("{}\n")
        (work / "probe.kicad_dru").write_text(dru_text(nets))
        out = work / "probe-drc.json"
        subprocess.run(
            [kicad_cli, "pcb", "drc", str(probe), "--format", "json", "--output", str(out)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return parse_lengths(json.loads(out.read_text()), nets)
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--kicad-cli", required=True)
    ap.add_argument("--net", action="append")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    lengths = measure(args.board, args.kicad_cli, args.net)
    text = json.dumps(lengths, indent=2, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
