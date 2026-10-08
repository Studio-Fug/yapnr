"""Opt-in saved-copper meander search, with a cold native DRC gate per edit.

Only free-space additions are attempted; other nets, vias and footprints stay fixed.
This adapter handles grid fallback routes after writeback and gloss. It never
changes routes carrying an explicit uncoupled-length contract.
"""

import argparse
import json
import math
import os
import re
import shutil
from pathlib import Path

from pnr import length_model as lm
from pnr.copper_clearance import CopperIndex, _seg_dist
from pnr.native_drc import run_drc
from pnr.route.detail import coupled


def chains(rows):
    adjacent = {}
    for i, row in enumerate(rows):
        for point in row[1:3]:
            adjacent.setdefault(point, []).append(i)
    used, result = set(), []
    for start in sorted(adjacent, key=lambda p: (len(adjacent[p]) == 2, p)):
        for first in adjacent[start]:
            if first in used:
                continue
            edge, point, points, ids = first, start, [start], []
            while edge not in used:
                used.add(edge)
                ids.append(edge)
                row = rows[edge]
                point = row[2] if row[1] == point else row[1]
                points.append(point)
                next_edges = [i for i in adjacent[point] if i not in used]
                if len(adjacent[point]) != 2 or not next_edges:
                    break
                edge = next_edges[0]
            result.append((coupled.simplify(points), ids))
    return sorted(result, key=lambda x: (-coupled.length(x[0]), x[0]))


def native_safe(before, after):
    """No opens or non-skew findings may be introduced or carried into an edit."""

    def skew_rules(result):
        return {
            (
                re.search(r"\(rule '([^']+)'", v.get("description", "")).group(1)
                if re.search(r"\(rule '([^']+)'", v.get("description", ""))
                else None
            )
            for v in result["violations"]
            if v["type"] == "skew_out_of_range"
        }

    return (
        skew_rules(after) <= skew_rules(before)
        and not before["unconnected_items"]
        and not after["unconnected_items"]
        and all(v["type"] == "skew_out_of_range" for v in before["violations"])
        and all(v["type"] == "skew_out_of_range" for v in after["violations"])
        and len(after["violations"]) < len(before["violations"])
    )


def run(board_path, rules, cli, work):
    import pcbnew as k

    work.mkdir(parents=True, exist_ok=True)

    def mm(p):
        return (p.x / 1e6, p.y / 1e6)

    env = dict(os.environ, PNR_TUNE_WINDOW_SEARCH="1")
    before = run_drc(cli, board_path, work / "before.json", env=env, final=True)
    report = dict(
        accepted=[],
        trials=0,
        stop="no_free_candidate",
        added_mm=0.0,
        pairs=[],
        clearance_rejections=0,
        own_route_rejections=0,
        cap_rejections=0,
        native_rejections=0,
        skipped_contracts=[],
    )
    if before["unconnected_items"] or any(
        v["type"] != "skew_out_of_range" for v in before["violations"]
    ):
        report["stop"] = "baseline_has_other_findings"
        return report
    tuning = rules.get("tuning") or {}
    if not tuning.get("meanders", True):
        report["stop"] = "meanders_disabled"
        return report
    cap = float(tuning.get("max_added_mm") if tuning.get("max_added_mm") is not None else 20.0)
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("max_added_mm must be finite and positive")
    for pair in rules.get("diff_pairs") or []:
        if not before["violations"]:
            break
        if "max_uncoupled_mm" in pair or pair.get("skew_ps") is not None:
            report["skipped_contracts"].append(pair.get("name"))
            continue
        p, n = pair["p"], pair["n"]
        budget = float(pair.get("skew_mm", 1.0))
        board = k.LoadBoard(str(board_path))
        layers = [board.GetLayerName(i) for i in board.GetEnabledLayers().CuStack()]
        rows, vias, pads, index = {}, {}, [], CopperIndex()
        for item in board.GetTracks():
            net = item.GetNetname()
            if isinstance(item, k.PCB_VIA):
                pos, radius = mm(item.GetPosition()), item.GetWidth(k.F_Cu) / 2e6
                vias.setdefault(net, []).append((*pos, radius))
                index.add_via(net, pos, radius)
            else:
                layer = board.GetLayerName(item.GetLayer())
                a, b, width = mm(item.GetStart()), mm(item.GetEnd()), item.GetWidth() / 1e6
                rows.setdefault(net, []).append((layer, a, b, width, item))
                index.add_track(net, layer, a, b, width / 2)
        for foot in board.GetFootprints():
            for pad in foot.Pads():
                lands = [la for la in layers if pad.IsOnLayer(board.GetLayerID(la))]
                if not lands:
                    continue
                outline = pad.GetEffectivePolygon(board.GetLayerID(lands[0])).COutline(0)
                poly = tuple(mm(outline.CPoint(i)) for i in range(outline.PointCount()))
                pads.append(
                    lm.PadCopper(
                        pad.GetNetname(),
                        mm(pad.GetPosition()),
                        frozenset(lands),
                        poly,
                        board.GetLayerName(foot.GetLayer()),
                    )
                )
                index.add_pad(pad.GetNetname(), lands, poly)
        stack = lm.read_stackup(board_path.read_text()) or lm.default_stackup(len(layers))

        def measure(net, copper=None):
            return lm.net_length(
                net,
                [r[:4] for r in (copper if copper is not None else rows.get(net, []))],
                vias.get(net, []),
                pads,
                stack,
            ).total_mm

        lengths = {net: measure(net) for net in (p, n)}
        report["pairs"].append(dict(name=pair.get("name"), lengths=lengths))
        short, long = sorted((p, n), key=lambda net: (lengths[net], net))
        if lengths[long] - lengths[short] <= budget:
            continue
        accepted = False
        for layer in sorted({r[0] for r in rows.get(short, [])}):
            selected = [r for r in rows[short] if r[0] == layer]
            for path, ids in chains(selected):
                width = max(selected[i][3] for i in ids)
                gap = float(pair.get("gap_mm") or 0.2)
                clearance = float((rules.get("fab") or {}).get("min_clearance_mm") or 0.2)
                untouched = [r for i, r in enumerate(selected) if i not in ids]

                def clear(net, a, b, w):
                    if not index.clear(net, layer, a, b, w / 2, lambda other: clearance):
                        report["clearance_rejections"] += 1
                        return False
                    if not all(
                        _seg_dist(a, b, r[1], r[2]) >= (w + r[3]) / 2 + gap - 1e-6
                        for r in untouched
                    ):
                        report["own_route_rejections"] += 1
                        return False
                    return True

                for residual in (budget * 0.5, budget * 0.9, budget * 0.99):
                    if report["trials"] >= 32:
                        report["stop"] = "trial_limit"
                        return report
                    paths = {short: path, long: []}
                    proposal = coupled.tune(
                        paths,
                        width,
                        gap,
                        residual,
                        clear,
                        {short: lengths[short] - coupled.length(path), long: lengths[long]},
                        baseline_paths=paths,
                    )
                    if proposal is None:
                        continue
                    points = proposal[short]
                    added = coupled.length(points) - coupled.length(path)
                    if added + report["added_mm"] > cap:
                        report["cap_rejections"] += 1
                        continue
                    trial = work / ("trial-%02d.kicad_pcb" % report["trials"])
                    removed = {id(selected[i][4]) for i in ids}
                    copper = [r for r in rows[short] if id(r[4]) not in removed]
                    copper += [(layer, a, b, width, None) for a, b in zip(points, points[1:])]
                    if abs(measure(short, copper) - lengths[long]) > budget:
                        continue
                    report["trials"] += 1
                    trial_board = k.LoadBoard(str(board_path))
                    uuids = {selected[i][4].m_Uuid.AsString() for i in ids}
                    code = selected[ids[0]][4].GetNetCode()
                    for item in list(trial_board.GetTracks()):
                        if item.m_Uuid.AsString() in uuids:
                            trial_board.Delete(item)
                    for a, b in zip(points, points[1:]):
                        item = k.PCB_TRACK(trial_board)
                        item.SetNetCode(code)
                        item.SetLayer(trial_board.GetLayerID(layer))
                        item.SetWidth(k.FromMM(width))
                        item.SetStart(k.VECTOR2I(*[k.FromMM(v) for v in a]))
                        item.SetEnd(k.VECTOR2I(*[k.FromMM(v) for v in b]))
                        trial_board.Add(item)
                    k.SaveBoard(str(trial), trial_board)
                    for suffix in (".kicad_pro", ".kicad_dru"):
                        src = board_path.with_suffix(suffix)
                        if src.exists():
                            shutil.copyfile(src, trial.with_suffix(suffix))
                    table = board_path.parent / "fp-lib-table"
                    if table.exists():
                        (trial.parent / "fp-lib-table").write_text(
                            table.read_text().replace(
                                "${KIPRJMOD}", str(board_path.parent.resolve())
                            )
                        )
                    after = run_drc(cli, trial, trial.with_suffix(".json"), env=env, final=True)
                    if native_safe(before, after):
                        shutil.copyfile(trial, board_path)
                        before, accepted = after, True
                        report["added_mm"] += added
                        report["accepted"].append(
                            dict(
                                pair=pair.get("name"),
                                net=short,
                                layer=layer,
                                length_mm=measure(short, copper),
                                skew_mm=abs(measure(short, copper) - lengths[long]),
                            )
                        )
                        break
                    report["native_rejections"] += 1
                if accepted:
                    break
            if accepted:
                break
    report["stop"] = "native_clean" if not before["violations"] else "no_free_candidate"
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--kicad-cli", required=True)
    ap.add_argument("--work", type=Path, required=True)
    args = ap.parse_args()
    os.environ["PNR_TUNE_WINDOW_SEARCH"] = "1"
    report = run(args.board, json.loads(args.rules.read_text()), args.kicad_cli, args.work)
    (args.work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
