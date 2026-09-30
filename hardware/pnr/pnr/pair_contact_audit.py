"""Read-only timing audit of every declared differential-pair connector contact.

This is a centerline timing check, not impedance, native DRC, or reference
qualification. Unsupported arcs, buried/blind transitions, non-surface pair
tracks and ambiguous pad labels fail closed rather than inventing lengths.
"""

from collections import defaultdict

from pnr.native_electrical import connector_origins_qualified, duplicate_endpoint_metrics, xy


def audit_pair_contacts(board, rules):
    import pcbnew as k

    labels = defaultdict(list)
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            labels[footprint.GetReference() + "." + pad.GetNumber()].append(pad)
    reports = []
    for pair in rules.get("diff_pairs", []):
        unsupported = []
        chain = pair.get("terminal_chain", [])
        if len(chain) < 2:
            reports.append(
                dict(pair=pair.get("name"), qualified=False, unsupported=["missing terminal chain"])
            )
            continue
        origins = [chain[0]] + [
            g[s] for g in pair.get("auxiliary_pairs", []) for s in ("source", "target")
        ]
        required = {origin[key] for origin in origins + [chain[-1]] for key in ("p", "n")}
        for label in required:
            if len(labels[label]) != 1:
                unsupported.append("missing or ambiguous pad " + label)
            elif not labels[label][0].IsOnLayer(k.F_Cu):
                unsupported.append("endpoint not on F.Cu " + label)
        for origin in origins + [chain[-1]]:
            for key in ("p", "n"):
                found = labels[origin[key]]
                if len(found) == 1 and found[0].GetNetname() != pair[key]:
                    unsupported.append("literal pad/net mismatch " + origin[key])
        nets = {pair["p"], pair["n"]}
        tracks, vias = [], []
        for track in board.GetTracks():
            if track.GetNetname() not in nets:
                continue
            kind = track.GetClass()
            if kind == "PCB_VIA":
                if track.GetViaType() != k.VIATYPE_THROUGH:
                    unsupported.append(
                        "unsupported non-through via " + str(track.m_Uuid.AsString())
                    )
                vias.append((track.GetNetname(), xy(track.GetPosition())))
            elif kind == "PCB_TRACK" and track.GetLayer() in (k.F_Cu, k.B_Cu):
                tracks.append(
                    (
                        track.GetNetname(),
                        track.GetLayer(),
                        xy(track.GetStart()),
                        xy(track.GetEnd()),
                        track.GetWidth() / 1e6,
                    )
                )
            else:
                unsupported.append(
                    "unsupported " + kind + " on " + board.GetLayerName(track.GetLayer())
                )
        thickness = rules.get("electrical_fab", {}).get("board_thickness_mm")
        if not isinstance(thickness, (int, float)) or thickness <= 0:
            unsupported.append("missing physical board thickness")
        metrics = {}
        if not unsupported:
            metrics = duplicate_endpoint_metrics(
                pair, {label: labels[label][0] for label in required}, tracks, vias, thickness
            )
        skews = {
            name: abs(values[pair["p"]]["length_mm"] - values[pair["n"]]["length_mm"])
            for name, values in metrics.items()
            if all(v.get("valid", False) for v in values.values())
        }
        reports.append(
            dict(
                pair=pair.get("name"),
                p=pair["p"],
                n=pair["n"],
                skew_limit_mm=pair["skew_mm"],
                qualified=not unsupported and connector_origins_qualified(pair, metrics),
                connector_endpoint_metrics=metrics,
                skews_mm=skews,
                unsupported=unsupported,
            )
        )
    return dict(
        qualified=bool(reports) and all(p["qualified"] for p in reports),
        pairs=reports,
        scope="Centerline endpoint lengths including actual duplicate-contact branch joins; does not qualify impedance/reference/native DRC.",
    )
