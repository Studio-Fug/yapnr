"""Scratch designs for routed length matching, outside the ladder and its hard rungs.

No ladder rung declares a length-match group (``length_match``); the MCU rungs declare
two differential pairs with a skew budget. These designs exercise both on a small
board: an 8-net bus from an SMD connector to an SOIC (a group with a 0.5 mm
tolerance) and a differential pair whose pins are swapped between its two connectors
(about 5 mm of native skew, 1.0 mm budget); on ``lm-bus-pair`` the bus's pin order
is reversed between its two ends (a part facing the connector routes it with every
net crossing every other). ``lm-bus-corner`` turns the bus round a corner instead:
the inner nets are the short ones and have no room for meanders until the bus is
spaced out. ``bus-pair-4L-ps`` is the crossing board on
the 4L-SGPS stack with the bus's budget in picoseconds (judged by the engine's audit:
KiCad 10.0.6's ``kicad-cli pcb drc`` reads every delay as 0 ps, KiCad issue 23868, so
it cannot judge a time-domain rule); its pair keeps its budget in mm, which the
rung's own KiCad rule judges.

    python lenmatch_scratch.py write OUT.json          # the design list for run.py --design-json
    python lenmatch_scratch.py judge CASE_DIR --kicad-cli PATH [--json OUT]

``judge`` is KiCad's DRC on a copy of the routed board with one ``skew`` rule per pair
(``inDiffPair``) and per group (its net names) that has a budget in mm, and reports the
violations, each net's KiCad length and the engine's own length report
(``pnr-report.json``); a set with a budget in ps is reported from the engine's report
only (``engine_only``). The rung judge itself (``run.py``) is unchanged.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from designs import circuit
from hard_rungs import hard, pinned, with_stackup

BUS = ["B%d" % k for k in range(8)]


def bus_pair():
    """J1 (JST SH 8, fixed on the west edge) carries an 8-net bus to U1 (SOIC-16, free);
    J3 and J4 (2-pin headers) carry a differential pair whose P and N swap pins between
    the two connectors. J2 and C1 give the supply."""
    parts = [
        pinned("J1", "jst_sh_8", "Bus", {**{str(k + 1): n for k, n in enumerate(BUS)}, "MP": ""}),
        pinned(
            "U1",
            "counter",
            "Bus device",
            {
                **{str(k + 1): "" for k in range(16)},
                **{str(k + 1): n for k, n in enumerate(BUS)},
                "9": "GND",
                "16": "VCC",
            },
        ),
        pinned("J2", "connector", "Supply", ["VCC", "GND"]),
        pinned("C1", "capacitor", "100n", ["VCC", "GND"]),
        pinned("J3", "connector", "Pair in", ["DP", "DN"]),
        pinned("J4", "connector", "Pair out", ["DN", "DP"]),
    ]
    spec = circuit(
        "lm-bus-pair",
        "Length-matching scratch board: an 8-net bus (group, 0.5 mm) and a differential "
        "pair with swapped pins (1.0 mm skew).",
        parts,
        (34, 28),
    )
    cons = spec["constraints"]
    cons["fixed"] = {"J1": dict(at=[4.0, 14.0], rot=90, side="top")}
    cons["length_match"] = [dict(name="bus", nets=list(BUS), tolerance_mm=0.5)]
    cons["diff_pair"] = [dict(name="d", p="DP", n="DN", skew_mm=1.0)]
    return hard(spec, "lm-bus-pair", ["length-match", "diff-pair"], "manual", 30)


def bus_corner():
    """The bus turning a corner. J1 carries the bus in the order a part facing it
    takes without crossings (B0 at the top), and U1 is fixed above and to the right
    of J1 with its bus pins facing down, so the bus turns a corner: its inner nets
    run several mm shorter than the outer ones and, routed at the pins' pitch, have
    no room beside them for meanders."""
    spec = bus_pair()
    spec["name"] = "lm-bus-corner"
    spec["description"] = (
        "Length-matching scratch board: an 8-net bus turning a corner (group, 0.5 mm) "
        "and a differential pair with swapped pins (1.0 mm skew)."
    )
    for part in spec["parts"]:
        if part["ref"] == "J1":
            part["pins"] = {**{str(k + 1): BUS[7 - k] for k in range(8)}, "MP": ""}
    spec["constraints"]["fixed"]["U1"] = dict(at=[20.0, 22.0], rot=90, side="top")
    spec["checks"].append(
        dict(
            id="fixed-U1",
            kind="fixed",
            ref="U1",
            at=[20.0, 22.0],
            rot=90,
            side="top",
            tol_mm=0.01,
            engine="fixed",
        )
    )
    return spec


def bus_pair_ps():
    """The board on the 4L-SGPS stack, with the bus's budget in picoseconds (a set
    gives mm or ps, not both; the pair keeps 1.0 mm, the rung judge's KiCad rule)."""
    spec = with_stackup(bus_pair(), "4L-SGPS")
    spec["name"] = "lm-bus-pair-4L-ps"
    cons = spec["constraints"]
    cons["length_match"] = [dict(name="bus", nets=list(BUS), tolerance_ps=3.0)]
    return spec


def designs():
    return deepcopy([bus_pair(), bus_corner(), bus_pair_ps()])


def judge_rules(spec) -> str:
    """KiCad custom rules: one skew rule per pair and per group with a budget in mm."""
    rules = ["(version 1)"]
    for pair in spec["constraints"].get("diff_pair") or []:
        if pair.get("skew_ps") is not None:
            continue
        rules.append(
            '(rule "pair %s skew"\n  (condition "A.inDiffPair(\'%s\')")\n'
            "  (constraint skew (max %gmm)))"
            % (pair["name"], pair["p"][:-1], pair.get("skew_mm", 0.5))
        )
    for group in spec["constraints"].get("length_match") or []:
        if group.get("tolerance_ps") is not None:
            continue
        names = " || ".join("A.NetName == '%s'" % n for n in group["nets"])
        rules.append(
            '(rule "group %s skew"\n  (condition "%s")\n  (constraint skew (max %gmm)))'
            % (group["name"], names, group.get("tolerance_mm", 1.0))
        )
    return "\n".join(rules) + "\n"


def judge(case: Path, kicad_cli: str) -> dict:
    spec = json.loads((case / "design.json").read_text())
    nets = [n for p in spec["constraints"].get("diff_pair") or [] for n in (p["p"], p["n"])]
    nets += [n for g in spec["constraints"].get("length_match") or [] for n in g["nets"]]
    work = Path(tempfile.mkdtemp(prefix="lenmatch-judge-"))
    try:
        board = work / "judge.kicad_pcb"
        shutil.copy(case / "routed.kicad_pcb", board)
        shutil.copy(case / "routed.kicad_pro", work / "judge.kicad_pro")
        own = case / "routed.kicad_dru"
        text = own.read_text() if own.exists() else "(version 1)\n"
        extra = judge_rules(spec).split("\n", 1)[1]
        (work / "judge.kicad_dru").write_text(text.rstrip("\n") + "\n" + extra)
        out = work / "drc.json"
        subprocess.run(
            [kicad_cli, "pcb", "drc", str(board), "--format", "json", "--output", str(out)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        drc = json.loads(out.read_text())
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from pnr.length_oracle import measure

        lengths = measure(case / "routed.kicad_pcb", kicad_cli, nets)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    skews = [v["description"] for v in drc["violations"] if v["type"] == "skew_out_of_range"]
    pnr = json.loads((case / "pnr-report.json").read_text())
    sets = []
    for pair in spec["constraints"].get("diff_pair") or []:
        sets.append(
            (pair["name"], [pair["p"], pair["n"]], pair.get("skew_mm"), pair.get("skew_ps"))
        )
    for group in spec["constraints"].get("length_match") or []:
        sets.append(
            (group["name"], group["nets"], group.get("tolerance_mm"), group.get("tolerance_ps"))
        )
    kicad, engine_only = {}, {}
    engine = {r["name"]: r for r in pnr.get("length_tuning") or []}
    for name, members, budget_mm, budget_ps in sets:
        got = [lengths.get(n) for n in members]
        spread = None if None in got else round(max(got) - min(got), 4)
        if budget_ps is None:
            kicad[name] = dict(
                budget_mm=budget_mm if budget_mm is not None else 1.0, spread_mm=spread
            )
        else:
            # No KiCad judge for a time budget (module docstring): the engine's audit.
            entry = engine.get(name, {})
            engine_only[name] = dict(
                budget_ps=budget_ps,
                spread_ps=entry.get("spread"),
                status=entry.get("status"),
                kicad_spread_mm=spread,
            )
    return dict(
        case=case.name,
        skew_violations=len(skews),
        skew_messages=skews,
        other_violations=sorted(
            {v["type"] for v in drc["violations"] if v["type"] != "skew_out_of_range"}
        ),
        unconnected=len(drc["unconnected_items"]),
        kicad=kicad,
        engine_only=engine_only,
        lengths_mm=lengths,
        engine=pnr.get("length_tuning"),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("out", type=Path)
    j = sub.add_parser("judge")
    j.add_argument("case", type=Path)
    j.add_argument("--kicad-cli", required=True)
    j.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "write":
        args.out.write_text(json.dumps(designs(), indent=2) + "\n")
        return 0
    result = judge(args.case, args.kicad_cli)
    text = json.dumps(result, indent=2)
    if args.json:
        args.json.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
