"""Bridge existing native copper to the signal router without changing it.

Export uses exactly the ingest frame. Append verifies the source hash and adds
only newly routed geometry; it never replaces tracks or regenerates footprints.
Native DRC, connectivity, pad-entry and electrical gates follow the transaction.
"""

import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(board):
    import pcbnew as k
    from pnr.ingest import _board_frame

    frame, _ = _board_frame(board)
    tracks = []
    vias = []
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            if t.GetViaType() != k.VIATYPE_THROUGH:
                raise ValueError("unsupported fixed blind/buried via")
            p = t.GetPosition()
            vias.append(
                dict(
                    net=t.GetNetname(),
                    xy=frame.point(p.x, p.y),
                    diameter_mm=max(
                        t.GetWidth(la)
                        for la in board.GetEnabledLayers().CuStack()
                        if t.IsOnLayer(la)
                    )
                    / 1e6,
                    drill_mm=t.GetDrillValue() / 1e6,
                    type="through",
                )
            )
        elif t.GetClass() == "PCB_TRACK":
            a, z = t.GetStart(), t.GetEnd()
            tracks.append(
                [
                    t.GetNetname(),
                    board.GetLayerName(t.GetLayer()),
                    frame.point(a.x, a.y),
                    frame.point(z.x, z.y),
                    t.GetWidth() / 1e6,
                ]
            )
        else:
            raise ValueError("unsupported fixed copper " + t.GetClass())
    return dict(frame="engine-mm-y-up", tracks=tracks, vias=vias)


def export(source, folder):
    import pcbnew as k
    from pnr.ingest import build_graph

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    board = k.LoadBoard(str(source))
    copper = extract(board)
    copper["source_sha256"] = digest(source)
    (folder / "fixed.json").write_text(json.dumps(copper, indent=2))
    (folder / "placed.json").write_text(build_graph(board).to_json())


def append(source, fixed, routes, rules, out):
    import pcbnew as k
    from pnr.ingest import _board_frame
    from pnr.native_loop import copy_board
    from pnr.writeback import _net_code_map

    source, out = Path(source), Path(out)
    if source.resolve() == out.resolve():
        raise ValueError("append requires a new checkpoint")
    if fixed.get("source_sha256") != digest(source):
        raise ValueError("stale fixed copper source")
    if fixed.get("frame") != "engine-mm-y-up":
        raise ValueError("missing fixed frame")
    board = k.LoadBoard(str(source))
    frame, _ = _board_frame(board)
    codes = _net_code_map(board)
    fab = rules["fab"]
    from pnr.pad_entry import repair_changed_entries, snapshot

    board.BuildConnectivity()
    before_entries = snapshot(board, rules)
    keep = []

    def point(a):
        return k.VECTOR2I(round(frame._left + a[0] * 1e6), round(frame._bottom - a[1] * 1e6))

    for net, layer, a, z, w in routes.get("tracks", []):
        layer_id = board.GetLayerID(layer)
        if layer_id not in list(board.GetEnabledLayers().CuStack()):
            raise ValueError("unsupported track layer " + layer)
        t = k.PCB_TRACK(board)
        t.SetNetCode(codes[net])
        t.SetStart(point(a))
        t.SetEnd(point(z))
        t.SetWidth(round(w * 1e6))
        t.SetLayer(layer_id)
        board.Add(t)
        keep.append(t)
    # Profile 5B only: a grid via inside a same-net SMD pad (escape E2 or a maze via
    # the grid admitted as in-pad, RouteGrid.restrict_smd_vias) is the filled in-pad
    # class, with its unused inner pads removed, when it qualifies in the native land
    # (in_pad_size). Otherwise it keeps the default via, which the DRU's
    # via_to_smd_pad rule and validate()'s new_forbidden_smd_vias both reject.
    # Legacy: every via at the fab default.
    from pnr.fab_profile import geometry
    from pnr.via_in_pad import in_pad_size, style_in_pad_via

    g = geometry(rules)
    smd = [p for f in board.GetFootprints() for p in f.Pads()] if g.in_pad else []
    for net, x, y in routes.get("vias", []):
        t = k.PCB_VIA(board)
        t.SetNetCode(codes[net])
        t.SetPosition(point((x, y)))
        size = (
            in_pad_size(g, smd, net, (t.GetPosition().x / 1e6, t.GetPosition().y / 1e6))
            if g.in_pad
            else None
        )
        diameter, drill = size or (fab["via_diameter_mm"], fab["via_drill_mm"])
        t.SetFrontWidth(round(diameter * 1e6))
        t.SetDrill(round(drill * 1e6))
        t.SetViaType(k.VIATYPE_THROUGH)
        t.SetLayerPair(k.F_Cu, k.B_Cu)
        if size:
            style_in_pad_via(g, t)
        board.Add(t)
        keep.append(t)
    board.BuildConnectivity()
    entries = repair_changed_entries(board, rules, before_entries)
    copy_board(source, out)
    k.SaveBoard(str(out), board)
    out.with_suffix(".entry-repair.json").write_text(json.dumps(entries, indent=2))


def validate(source, candidate, rules, before_drc, after_drc, references=()):
    import pcbnew as k
    from pnr.native_electrical import pair_reference_validator
    from pnr.pad_entry import snapshot
    from pnr.via_coalesce import acceptable, partition, preserved

    a, b = (k.LoadBoard(str(p)) for p in (source, candidate))
    a.BuildConnectivity()
    b.BuildConnectivity()
    sa, sb = snapshot(a, rules), snapshot(b, rules)
    checks = dict(
        preserved=preserved(partition(a), partition(b)),
        lost_pad_entries=[i for i, v in sa.items() if v and not sb.get(i, False)],
        new_bad_entries=[i for i, v in sb.items() if not v and i not in sa],
    )
    # Fab profile only (legacy: always empty, KiCad DRC is the whole rule): vias
    # breaking 5A via-to-SMD-pad / 5B in-pad geometry, which the DRU cannot see for
    # the 0.20-drill in-pad class.
    from pnr.via_in_pad import new_forbidden

    checks["new_forbidden_smd_vias"] = new_forbidden(a, b, rules)

    # Saved source tracks must be byte-for-byte equivalent in normalized geometry.
    def copper(board):
        return {
            t.m_Uuid.AsString(): t.GetClass() + " " + str(extract_item(t, board))
            for t in board.GetTracks()
        }

    old, new = copper(a), copper(b)
    checks["fixed_copper_preserved"] = all(new.get(i) == v for i, v in old.items())
    checks["reference_failures"] = []
    pairs = {p["name"]: p for p in rules.get("diff_pairs", [])}
    for ref in references or rules.get("routed_pair_references", []):
        pair = pairs[ref["pair"]]
        valid = pair_reference_validator(b, pair, rules)
        for si, segment in enumerate(ref["segments"]):
            if not valid(segment["reference_paths"], 0):
                checks["reference_failures"].append(dict(pair=pair["name"], segment=si))
    checks["accepted"] = (
        acceptable(before_drc, after_drc, checks)
        and not checks["new_bad_entries"]
        and checks["fixed_copper_preserved"]
        and not checks["reference_failures"]
        and not checks["new_forbidden_smd_vias"]
    )
    return checks


def extract_item(t, board):
    if t.GetClass() == "PCB_TRACK":
        a, z = t.GetStart(), t.GetEnd()
        return [t.GetNetname(), t.GetLayer(), a.x, a.y, z.x, z.y, t.GetWidth()]
    if t.GetClass() == "PCB_VIA":
        p = t.GetPosition()
        return [
            t.GetNetname(),
            p.x,
            p.y,
            t.GetViaType(),
            t.GetDrillValue(),
            [(la, t.GetWidth(la)) for la in board.GetEnabledLayers().CuStack() if t.IsOnLayer(la)],
        ]
    raise ValueError("unsupported copper type")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("board")
    p.add_argument("--export-dir")
    p.add_argument("--fixed")
    p.add_argument("--routes")
    p.add_argument("--rules")
    p.add_argument("--out")
    p.add_argument("--validate")
    p.add_argument("--before-drc")
    p.add_argument("--after-drc")
    p.add_argument("--references")
    a = p.parse_args()
    if a.validate:
        read = lambda p: json.loads(Path(p).read_text())
        checks = validate(
            a.board,
            a.validate,
            read(a.rules),
            read(a.before_drc),
            read(a.after_drc),
            read(a.references) if a.references else [],
        )
        Path(a.out).write_text(json.dumps(checks, indent=2))
        if not checks["accepted"]:
            raise SystemExit("staged signal geometry rejected; see " + a.out)
    elif a.export_dir:
        export(a.board, a.export_dir)
    else:
        if not all([a.fixed, a.routes, a.rules, a.out]):
            p.error("append requires fixed, routes, rules and out")
        read = lambda p: json.loads(Path(p).read_text())
        append(a.board, read(a.fixed), read(a.routes), read(a.rules), a.out)


if __name__ == "__main__":
    main()
