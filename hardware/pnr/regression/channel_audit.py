"""Audit the placement channel model against routed regression runs.

usage: channel_audit.py [--json OUT] [--layers | --no-layers] RUN_DIR...

Each RUN_DIR is searched for case folders (``placed.json``, ``routes.json``,
``rules.json`` and ``result.json`` side by side, as ``run.py`` writes them). For each
case :func:`pnr.place.channel_audit.audit` compares the channel model's predicted
surface demand with the tracks the router really ran along each channel, and the
table groups the totals by the board's signal-layer count. Only cases the
regression judged as passing are tallied (a failed route says nothing about how
much room a finished one needs).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pnr.graph import BoardGraph  # noqa: E402
from pnr.place.channel_audit import audit, signal_layers, summarize  # noqa: E402
from pnr.place.channels import ChannelModel  # noqa: E402


def cases(roots):
    for root in roots:
        for placed in sorted(Path(root).rglob("placed.json")):
            folder = placed.parent
            if folder.parent.name in ("initial-pool",) or "rounds" in folder.parts:
                continue
            if all((folder / name).exists() for name in ("routes.json", "rules.json")):
                yield folder


def passed(folder: Path) -> bool:
    result = folder / "result.json"
    if not result.exists():
        return False
    data = json.loads(result.read_text())
    return bool(data.get("passed", data.get("status") == "pass"))


def run_case(folder: Path, **model_kwargs) -> dict:
    graph = BoardGraph.from_json((folder / "placed.json").read_text())
    rules = json.loads((folder / "rules.json").read_text())
    routes = json.loads((folder / "routes.json").read_text())
    model = ChannelModel(graph, rules, **model_kwargs)
    records = audit(graph, model, routes.get("tracks", []), routes.get("vias", []))
    return dict(
        case=folder.name,
        path=str(folder),
        layers=int(rules.get("layers") or 2),
        signal_layers=signal_layers(rules),
        passed=passed(folder),
        summary=summarize(records),
        records=records,
    )


def table(results) -> str:
    groups = defaultdict(list)
    for result in results:
        if result["passed"]:
            groups[(result["layers"], result["signal_layers"])].append(result)
    lines = [
        "| copper (signal) layers | cases | priced channels | predicted short | "
        "required mm | used mm | ratio | predicted tracks | used tracks | "
        "nets on surface / dropped / elsewhere |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for key in sorted(groups, key=lambda k: (k[0], k[1] or 0)):
        rows = [r["summary"] for r in groups[key]]
        total = defaultdict(float)
        fates = defaultdict(int)
        for row in rows:
            for name in ("priced", "predicted_short", "required_mm", "used_mm"):
                total[name] += row[name]
            for name in ("predicted_tracks", "used_tracks"):
                total[name] += row[name]
            for fate, count in row["fates"].items():
                fates[fate] += count
        nets = sum(fates.values()) or 1
        ratio = total["required_mm"] / total["used_mm"] if total["used_mm"] else float("inf")
        lines.append(
            "| %d (%s) | %d | %d | %d (%.0f%%) | %.1f | %.1f | %.2f | %d | %d | %.0f%% / %.0f%% / %.0f%% |"
            % (
                key[0],
                key[1],
                len(rows),
                total["priced"],
                total["predicted_short"],
                100 * total["predicted_short"] / max(total["priced"], 1),
                total["required_mm"],
                total["used_mm"],
                ratio,
                total["predicted_tracks"],
                total["used_tracks"],
                100 * fates["surface"] / nets,
                100 * fates["dropped"] / nets,
                100 * fates["elsewhere"] / nets,
            )
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--json", type=Path, help="write every record here")
    parser.add_argument(
        "--layers",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="price with the layer-aware model (--no-layers: surface only; "
        "default: PNR_CHANNEL_LAYERS)",
    )
    args = parser.parse_args(argv)
    results = [run_case(folder, layers=args.layers) for folder in cases(args.roots)]
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))
    print(table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
