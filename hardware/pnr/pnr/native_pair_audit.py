"""Read-only coupling gate on saved two-terminal differential-pair copper.

Native DRC remains the clearance/connectivity/skew judge. This additional gate
rejects independent matched legs, ambiguous paths and unsupported topology.
"""

import argparse
import itertools
import json
import math
from pathlib import Path

from pnr import length_model as lm
from pnr.route.detail import coupled


def measure_paths(paths, width, gap, cap):
    runs = coupled.uncoupled_runs(paths, width, gap)
    totals = {net: sum(row["length_mm"] for row in value["runs"]) for net, value in runs.items()}
    return dict(
        passed=all(v <= cap + 1e-6 for v in totals.values()),
        uncoupled_mm=totals,
        uncoupled_runs=runs,
        max_uncoupled_mm=cap,
    )


def audit(board_path, rules):
    import pcbnew as k

    board = k.LoadBoard(str(board_path))
    layers = [board.GetLayerName(i) for i in board.GetEnabledLayers().CuStack()]
    stack = lm.read_stackup(board_path.read_text()) or lm.default_stackup(len(layers))
    heights = {la: lm.layer_distance(stack, layers[0], la) for la in layers}

    def xy(p):
        return (p.x / 1e6, p.y / 1e6)

    tracks, vias, pads = {}, {}, {}
    via_copper, pad_copper = {}, []
    unsupported = set()
    for item in board.GetTracks():
        net = item.GetNetname()
        if isinstance(item, k.PCB_VIA):
            q = xy(item.GetPosition())
            via_copper.setdefault(net, []).append((*q, item.GetWidth(k.F_Cu) / 2e6))
            vias.setdefault(net, []).append(
                (
                    xy(item.GetPosition()),
                    [la for la in layers if item.IsOnLayer(board.GetLayerID(la))],
                )
            )
        elif isinstance(item, k.PCB_ARC):
            unsupported.add(net)
        else:
            tracks.setdefault(net, []).append(
                (
                    board.GetLayerName(item.GetLayer()),
                    xy(item.GetStart()),
                    xy(item.GetEnd()),
                    item.GetWidth() / 1e6,
                )
            )
    for foot in board.GetFootprints():
        for pad in foot.Pads():
            if any(pad.IsOnLayer(board.GetLayerID(la)) for la in layers):
                pads.setdefault(pad.GetNetname(), []).append((foot.GetReference(), pad))
                lands = [la for la in layers if pad.IsOnLayer(board.GetLayerID(la))]
                polygon = pad.GetEffectivePolygon(board.GetLayerID(lands[0]))
                outline = polygon.COutline(0)
                pad_copper.append(
                    lm.PadCopper(
                        pad.GetNetname(),
                        xy(pad.GetPosition()),
                        frozenset(lands),
                        tuple(xy(outline.CPoint(i)) for i in range(outline.PointCount())),
                        board.GetLayerName(foot.GetLayer()),
                    )
                )
    reports = []
    for pair in rules.get("diff_pairs", []):
        p, n = pair["p"], pair["n"]
        report = dict(name=pair.get("name"), passed=False)
        reports.append(report)
        if any(net in unsupported or len(pads.get(net, [])) != 2 for net in (p, n)):
            report["reason"] = "unsupported_pair_topology"
            continue
        # Retain polarity and associate endpoints by shared part, then distance.
        pp, nn = pads[p], pads[n]
        nn = min(
            (nn, list(reversed(nn))),
            key=lambda ns: (
                -sum(a[0] == b[0] for a, b in zip(pp, ns)),
                sum(
                    math.dist(xy(a[1].GetPosition()), xy(b[1].GetPosition()))
                    for a, b in zip(pp, ns)
                ),
            ),
        )
        paths = {}
        for net, endpoints in ((p, pp), (n, nn)):
            copper = tracks.get(net, [])

            def contacts(pad):
                found = set()
                for la, a, b, _ in copper:
                    li = board.GetLayerID(la)
                    if not pad.IsOnLayer(li):
                        continue
                    poly = pad.GetEffectivePolygon(li)
                    for q in (a, b):
                        if poly.Contains(k.VECTOR2I(round(q[0] * 1e6), round(q[1] * 1e6))):
                            found.add((q, la))
                return sorted(found, key=lambda q: math.dist(q[0], xy(pad.GetPosition())))[:8]

            options = []
            for source, target in itertools.product(*(contacts(pad) for _, pad in endpoints)):
                metric = coupled.path_metrics(
                    [row[:3] for row in copper],
                    vias.get(net, []),
                    source,
                    target,
                    layer_heights=heights,
                )
                if metric.get("valid"):
                    options.append(metric)
            if not options:
                report["reason"] = "no_unambiguous_endpoint_path"
                break
            metric = min(options, key=lambda row: (row["branch_vertices"], row["length_mm"]))
            # A two-terminal contract does not authorize external stubs.
            if metric["branch_vertices"]:
                report["reason"] = "unexpected_pair_branch"
                break
            paths[net] = coupled.path_steps(metric["path"])
        if len(paths) != 2:
            continue
        width = float(pair.get("width_mm") or max(row[3] for net in (p, n) for row in tracks[net]))
        gap = float(pair.get("gap_mm") or (rules.get("fab") or {}).get("clearance_mm", 0.2))
        report.update(measure_paths(paths, width, gap, float(pair.get("max_uncoupled_mm", 2))))
        report["reason"] = "coupled" if report["passed"] else "uncoupled_budget"
        delay = lm.DelayModel(stack) if pair.get("skew_ps") is not None else None
        measures = {
            net: lm.net_length(net, tracks[net], via_copper.get(net, []), pad_copper, stack, delay)
            for net in (p, n)
        }
        report["lengths_mm"] = {net: value.total_mm for net, value in measures.items()}
        if delay is not None:
            report["delays_ps"] = {net: value.delay_ps for net, value in measures.items()}
            skew = abs(measures[p].delay_ps - measures[n].delay_ps)
            budget = float(pair["skew_ps"])
            report["skew_ps"] = skew
        else:
            skew = abs(measures[p].total_mm - measures[n].total_mm)
            budget = float(pair.get("skew_mm", 0.5))
            report["skew_mm"] = skew
        if skew > budget + 1e-6:
            report.update(passed=False, reason="whole_run_skew")
        report["via_sites"] = {net: [q for q, _ in vias.get(net, [])] for net in (p, n)}
    return dict(passed=all(row["passed"] for row in reports), pairs=reports)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    result = audit(args.board, json.loads(args.rules.read_text()))
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
