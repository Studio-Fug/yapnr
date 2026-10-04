"""Bridge existing native copper to the signal router without changing it.

Export uses exactly the ingest frame. Append verifies the source hash and adds
only newly routed geometry; it never replaces tracks or regenerates footprints.
Native DRC, connectivity, pad-entry and electrical gates follow the transaction.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(board, blocks=None):
    """The board's existing copper in the engine frame (``fixed.json``).

    Schema 1 (``tracks``, ``vias``) for straight copper; schema 2 (pnr.fixed_block)
    when the board has arcs (``arcs``) or ``blocks`` are declared (the constraints'
    ``fixed_block`` entries): each block's tracks, arcs, vias, pad and solid-zone
    polygons, rule areas, footprints and copper digest, out of the top-level lists."""
    import pcbnew as k

    from pnr.ingest import _board_frame
    from pnr.writeback import group_name

    frame, _ = _board_frame(board)
    tracks = []
    vias = []
    arcs = []
    records = {b["group"]: _block_record(b) for b in blocks or []}
    if records:
        present = {g.GetName() for g in board.Groups()}
        missing = sorted(set(records) - present)
        if missing:
            raise ValueError("fixed block group(s) not on the board: %s" % ", ".join(missing))
    kinds = {
        int(k.VIATYPE_THROUGH): "through",
        int(k.VIATYPE_BLIND): "blind",
        int(k.VIATYPE_BURIED): "buried",
        int(k.VIATYPE_MICROVIA): "micro",
    }
    for t in board.GetTracks():
        target = records.get(group_name(t)) if records else None
        if t.GetClass() == "PCB_VIA":
            kind = kinds.get(int(t.GetViaType()))
            if kind is None:
                raise ValueError("unsupported fixed via type %r" % t.GetViaType())
            p = t.GetPosition()
            via = dict(
                net=t.GetNetname(),
                xy=frame.point(p.x, p.y),
                diameter_mm=max(
                    t.GetWidth(la) for la in board.GetEnabledLayers().CuStack() if t.IsOnLayer(la)
                )
                / 1e6,
                drill_mm=t.GetDrillValue() / 1e6,
                type=kind,
            )
            if kind != "through":
                # A blind, buried or micro via occupies its span only: its layers.
                via["layers"] = [
                    pcbnew_name(k, t.TopLayer()),
                    pcbnew_name(k, t.BottomLayer()),
                ]
            (target["vias"] if target else vias).append(via)
        elif t.GetClass() == "PCB_TRACK":
            a, z = t.GetStart(), t.GetEnd()
            (target["tracks"] if target else tracks).append(
                [
                    t.GetNetname(),
                    board.GetLayerName(t.GetLayer()),
                    frame.point(a.x, a.y),
                    frame.point(z.x, z.y),
                    t.GetWidth() / 1e6,
                ]
            )
        elif t.GetClass() == "PCB_ARC":
            a, m, z = t.GetStart(), t.GetMid(), t.GetEnd()
            (target["arcs"] if target else arcs).append(
                [
                    t.GetNetname(),
                    board.GetLayerName(t.GetLayer()),
                    frame.point(a.x, a.y),
                    frame.point(m.x, m.y),
                    frame.point(z.x, z.y),
                    t.GetWidth() / 1e6,
                ]
            )
        else:
            raise ValueError("unsupported fixed copper " + t.GetClass())
    out = dict(frame="engine-mm-y-up", tracks=tracks, vias=vias)
    if arcs or records:
        from pnr.fixed_block import SCHEMA

        out["schema"] = SCHEMA
    if arcs:
        out["arcs"] = arcs
    if records:
        spec = {b["group"]: b for b in blocks}
        for group, record in records.items():
            _block_shapes(board, frame, group, spec[group], record)
            record["sha256"] = block_digest(board, group, spec[group].get("anchor"))
        out["blocks"] = [records[b["group"]] for b in blocks]
    return out


def _block_record(spec):
    record = dict(name=spec["name"], group=spec["group"])
    if spec.get("anchor"):
        record["anchor"] = spec["anchor"]
    record.update(tracks=[], arcs=[], vias=[], polygons=[], refs=[])
    return record


def _poly_set(frame, polys):
    """``[(outline, holes)]`` of a SHAPE_POLY_SET in the engine frame."""
    out = []
    for i in range(polys.OutlineCount()):
        chain = polys.Outline(i)
        outline = [
            frame.point(chain.CPoint(n).x, chain.CPoint(n).y) for n in range(chain.PointCount())
        ]
        holes = []
        for h in range(polys.HoleCount(i)):
            hole = polys.Hole(i, h)
            holes.append(
                [frame.point(hole.CPoint(n).x, hole.CPoint(n).y) for n in range(hole.PointCount())]
            )
        out.append((outline, holes))
    return out


def _block_shapes(board, frame, group, spec, record):
    """A block's footprints (``refs``), their pads' copper on every copper layer
    (``pad`` polygons, 1 um outside), its copper zones on the solid layers
    (``zone``) and its rule areas that forbid tracks or vias (``rule_area``)."""
    import pcbnew as k

    from pnr.writeback import group_name

    copper = list(board.GetEnabledLayers().CuStack())
    solid = set(spec.get("solid_layers") or [])
    zones = [z for z in board.Zones() if group_name(z) == group]
    for fp in board.GetFootprints():
        if group_name(fp) != group:
            continue
        record["refs"].append(fp.GetReference())
        zones.extend(fp.Zones())
        for pad in fp.Pads():
            for layer in copper:
                if not pad.IsOnLayer(layer):
                    continue
                polys = k.SHAPE_POLY_SET()
                pad.TransformShapeToPolygon(polys, layer, 0, 1000, k.ERROR_OUTSIDE)
                for outline, holes in _poly_set(frame, polys):
                    record["polygons"].append(
                        dict(
                            net=pad.GetNetname(),
                            layer=board.GetLayerName(layer),
                            outline=outline,
                            holes=holes,
                            kind="pad",
                        )
                    )
    record["refs"].sort()
    for zone in zones:
        names = [board.GetLayerName(la) for la in copper if zone.IsOnLayer(la)]
        if zone.GetIsRuleArea():
            barred = dict(
                tracks=bool(zone.GetDoNotAllowTracks()), vias=bool(zone.GetDoNotAllowVias())
            )
            if not any(barred.values()):
                continue
            for outline, holes in _poly_set(frame, zone.Outline()):
                record["polygons"].append(
                    dict(
                        net="",
                        layers=names,
                        outline=outline,
                        holes=holes,
                        kind="rule_area",
                        **barred
                    )
                )
            continue
        for name in names:
            if name not in solid:
                continue
            for outline, holes in _poly_set(frame, zone.Outline()):
                record["polygons"].append(
                    dict(
                        net=zone.GetNetname(), layer=name, outline=outline, holes=holes, kind="zone"
                    )
                )


def _via_kinds():
    import pcbnew as k

    return {
        int(k.VIATYPE_THROUGH): "through",
        int(k.VIATYPE_BLIND): "blind",
        int(k.VIATYPE_BURIED): "buried",
        int(k.VIATYPE_MICROVIA): "micro",
    }


def block_digest(board, group=None, anchor=None, rename=None):
    """sha256 of a block's copper: every track, arc and via of KiCad group ``group``
    (every one on the board when None) in the frame of footprint ``anchor`` (its
    position and orientation; the board's lower-left corner without one), to the
    nanometre: kind, points, width or size and drill, layers and net (renamed by
    ``rename``). Equal digests mean the same copper wherever the block sits."""
    import hashlib

    from pnr.writeback import group_name

    VIA_KINDS = _via_kinds()
    if anchor:
        fp = board.FindFootprintByReference(anchor)
        if fp is None:
            raise ValueError("fixed block anchor %r is not on the board" % anchor)
        origin, turn = fp.GetPosition(), fp.GetOrientationDegrees()
        ox, oy = origin.x, origin.y
    else:
        from pnr.ingest import _board_frame

        frame, _ = _board_frame(board)
        ox, oy, turn = frame._left, frame._bottom, 0.0
    quarter = round(turn / 90.0)
    exact = abs(turn - 90.0 * quarter) < 1e-9

    def local(p):
        dx, dy = p.x - ox, p.y - oy
        if exact:
            for _ in range(quarter % 4):  # undo a CCW (KiCad y-down) turn by 90 deg
                dx, dy = -dy, dx
            return "%d %d" % (dx, dy)
        a = math.radians(turn)
        c, s_ = math.cos(a), math.sin(a)
        return "%d %d" % (round(c * dx - s_ * dy), round(s_ * dx + c * dy))

    rows = []
    for t in board.GetTracks():
        if group is not None and group_name(t) != group:
            continue
        net = t.GetNetname()
        net = (rename or {}).get(net, net)
        kind = t.GetClass()
        if kind == "PCB_VIA":
            layers = "%s-%s" % (
                board.GetLayerName(t.TopLayer()),
                board.GetLayerName(t.BottomLayer()),
            )
            row = [
                "via",
                local(t.GetPosition()),
                "d %d" % t.GetWidth(t.TopLayer()),
                "drill %d" % t.GetDrillValue(),
                "type %s" % VIA_KINDS.get(int(t.GetViaType()), str(int(t.GetViaType()))),
                layers,
            ]
        elif kind == "PCB_ARC":
            row = [
                "arc",
                local(t.GetStart()),
                local(t.GetMid()),
                local(t.GetEnd()),
                "w %d" % t.GetWidth(),
                board.GetLayerName(t.GetLayer()),
            ]
        elif kind == "PCB_TRACK":
            row = [
                "segment",
                local(t.GetStart()),
                local(t.GetEnd()),
                "w %d" % t.GetWidth(),
                board.GetLayerName(t.GetLayer()),
            ]
        else:
            raise ValueError("unsupported fixed copper " + kind)
        rows.append("|".join(row + [net]))
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def pcbnew_name(k, layer):
    """KiCad's standard name of copper layer id ``layer`` (the names rules use)."""
    return k.BOARD.GetStandardLayerName(layer)


def export(source, folder, rules=None):
    """``fixed.json`` and the routing graph ``placed.json`` of a placed board. With
    ``rules`` declaring fixed blocks, their copper is exported as blocks (each digest
    checked against its declared ``sha256``) and their footprints are held out of
    the graph (pnr.fixed_block.hold_out)."""
    import pcbnew as k

    from pnr.fixed_block import block_refs, hold_out
    from pnr.ingest import build_graph

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    board = k.LoadBoard(str(source))
    blocks = (rules or {}).get("fixed_blocks") or []
    copper = extract(board, blocks)
    for spec, block in zip(blocks, copper.get("blocks") or []):
        if spec.get("sha256") and spec["sha256"] != block["sha256"]:
            raise ValueError(
                "fixed block %r copper digest %s differs from the declared %s"
                % (spec["name"], block["sha256"], spec["sha256"])
            )
    copper["source_sha256"] = digest(source)
    (folder / "fixed.json").write_text(json.dumps(copper, indent=2))
    graph = build_graph(board)
    hold_out(graph, block_refs(blocks, copper))
    (folder / "placed.json").write_text(graph.to_json())


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

    # A declared fanout (pnr.route.detail.fanout) sizes its own vias and may lock
    # its copper; both keys are absent otherwise.
    own_sizes = {(n, x, y): (d, h) for n, x, y, d, h in routes.get("via_sizes", [])}
    locked = routes.get("locked") or {}
    locked_tracks = {(n, la, tuple(a), tuple(b)) for n, la, a, b in locked.get("tracks", [])}
    locked_vias = {tuple(v) for v in locked.get("vias", [])}
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
        if locked_tracks and (net, layer, tuple(a), tuple(z)) in locked_tracks:
            t.SetLocked(True)
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
    # Blind, buried and micro vias (routes["via_spans"], pnr.via_policy).
    from pnr.writeback import span_via

    spans = {}
    for net, x, y, top, bottom, kind in routes.get("via_spans", []):
        spans.setdefault((net, x, y), []).append((top, bottom, kind))
    sizes = (rules.get("via_policy") or {}).get("sizes") or {}
    for net, x, y in routes.get("vias", []):
        if spans.get((net, x, y)):
            for top, bottom, kind in spans[(net, x, y)]:
                diameter, drill = sizes.get(kind) or (fab["via_diameter_mm"], fab["via_drill_mm"])
                t = span_via(board, point((x, y)), codes[net], top, bottom, kind, diameter, drill)
                board.Add(t)
                keep.append(t)
            continue
        t = k.PCB_VIA(board)
        t.SetNetCode(codes[net])
        t.SetPosition(point((x, y)))
        size = (
            in_pad_size(g, smd, net, (t.GetPosition().x / 1e6, t.GetPosition().y / 1e6))
            if g.in_pad
            else None
        )
        diameter, drill = size or own_sizes.get(
            (net, x, y), (fab["via_diameter_mm"], fab["via_drill_mm"])
        )
        t.SetFrontWidth(round(diameter * 1e6))
        t.SetDrill(round(drill * 1e6))
        t.SetViaType(k.VIATYPE_THROUGH)
        t.SetLayerPair(k.F_Cu, k.B_Cu)
        if size:
            style_in_pad_via(g, t)
        if (net, x, y) in locked_vias:
            t.SetLocked(True)
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
    blocks_ok = True
    if rules.get("fixed_blocks"):
        # Fixed blocks: the same copper digest (and the declared one), the same lock
        # state of every block item, the same zones of the block.
        checks["fixed_blocks"] = block_checks(a, b, rules["fixed_blocks"])
        blocks_ok = all(v["accepted"] for v in checks["fixed_blocks"].values())
        checks["fixed_block_digest_equal"] = all(
            v["digest_equal"] for v in checks["fixed_blocks"].values()
        )
    checks["accepted"] = (
        acceptable(before_drc, after_drc, checks)
        and not checks["new_bad_entries"]
        and checks["fixed_copper_preserved"]
        and not checks["reference_failures"]
        and not checks["new_forbidden_smd_vias"]
        and blocks_ok
    )
    return checks


def block_checks(a, b, blocks):
    """Per block: ``digest_equal`` (source and candidate, and the declared
    ``sha256``), ``locked_preserved`` (every source item of the group keeps its lock
    state) and ``zones_preserved`` (the group's zones keep layers, net and outline)."""
    from pnr.writeback import group_name

    def state(board, group):
        items = {}
        for t in board.GetTracks():
            if group_name(t) == group:
                items[t.m_Uuid.AsString()] = bool(t.IsLocked())
        zones = {}
        for z in board.Zones():
            if group_name(z) == group:
                chain = z.Outline()
                pts = [
                    (chain.CVertex(n).x, chain.CVertex(n).y) for n in range(chain.TotalVertices())
                ]
                zones[z.m_Uuid.AsString()] = (
                    z.GetNetname(),
                    str(z.GetLayerSet().FmtHex()),
                    pts,
                    bool(z.IsLocked()),
                )
        return items, zones

    out = {}
    for spec in blocks:
        group, anchor = spec["group"], spec.get("anchor")
        da, db = block_digest(a, group, anchor), block_digest(b, group, anchor)
        ia, za = state(a, group)
        ib, zb = state(b, group)
        record = dict(
            digest_equal=da == db and (not spec.get("sha256") or spec["sha256"] == db),
            source_sha256=da,
            candidate_sha256=db,
            locked_preserved=all(ib.get(i) == v for i, v in ia.items()),
            zones_preserved=all(zb.get(i) == v for i, v in za.items()),
        )
        record["accepted"] = (
            record["digest_equal"] and record["locked_preserved"] and record["zones_preserved"]
        )
        out[spec["name"]] = record
    return out


def extract_item(t, board):
    if t.GetClass() == "PCB_TRACK":
        a, z = t.GetStart(), t.GetEnd()
        return [t.GetNetname(), t.GetLayer(), a.x, a.y, z.x, z.y, t.GetWidth()]
    if t.GetClass() == "PCB_ARC":
        a, m, z = t.GetStart(), t.GetMid(), t.GetEnd()
        return [t.GetNetname(), t.GetLayer(), a.x, a.y, m.x, m.y, z.x, z.y, t.GetWidth()]
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
    p.add_argument(
        "--digest",
        metavar="GROUP",
        help="print the copper digest of KiCad group GROUP ('' for every track, arc and via)",
    )
    p.add_argument("--anchor", help="--digest: the footprint whose frame the digest uses")
    p.add_argument("--rename", help="--digest: JSON net-name map applied before hashing")
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
    elif a.digest is not None:
        import pcbnew as k

        rename = json.loads(Path(a.rename).read_text()) if a.rename else None
        board = k.LoadBoard(str(a.board))
        print(block_digest(board, a.digest or None, a.anchor, rename))
    elif a.export_dir:
        export(a.board, a.export_dir, json.loads(Path(a.rules).read_text()) if a.rules else None)
    else:
        if not all([a.fixed, a.routes, a.rules, a.out]):
            p.error("append requires fixed, routes, rules and out")
        read = lambda p: json.loads(Path(p).read_text())
        append(a.board, read(a.fixed), read(a.routes), read(a.rules), a.out)


if __name__ == "__main__":
    main()
