"""Reproduce a single pinned real-chain experiment, in an isolated output folder.

The saved case supplies the ORIGINAL witness only; candidate paths are generated
by energy_track.py. Existing proof candidates/results are never imported.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from prototype.energy_track import Config, Controller, Energy, Obstacle, Planner, Track, length
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import unary_union


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def polygons(q):
    out = []
    for i in range(q.OutlineCount()):
        ring = q.COutline(i)
        shell = [(ring.CPoint(j).x / 1e6, ring.CPoint(j).y / 1e6) for j in range(ring.PointCount())]
        holes = []
        for h in range(q.HoleCount(i)):
            r = q.CHole(i, h)
            holes.append(
                [(r.CPoint(j).x / 1e6, r.CPoint(j).y / 1e6) for j in range(r.PointCount())]
            )
        if len(shell) > 2:
            out.append(Polygon(shell, holes).buffer(0))
    return unary_union(out)


def native_tracks(board, excluded=()):
    from pnr.gloss import pt, uid

    out = []
    for t in board.GetTracks():
        if uid(t) in excluded:
            continue
        row = [
            uid(t),
            t.GetClass(),
            t.GetNetname(),
            t.GetLayer(),
            pt(t.GetStart()),
            pt(t.GetEnd()),
            (t.GetWidth(t.GetLayer()) if t.GetClass() == "PCB_VIA" else t.GetWidth()),
            t.IsLocked(),
        ]
        if t.GetClass() == "PCB_VIA":
            row += [t.GetDrillValue(), t.TopLayer(), t.BottomLayer()]
        out.append(row)
    return sorted(out)


def immutable_metadata(board):
    """Record footprint, pad, group and zone definitions before a track edit."""
    from pnr.gloss import pt, uid

    footprints = []
    for f in board.GetFootprints():
        pads = []
        for p in f.Pads():
            pads.append(
                [
                    uid(p),
                    p.GetNumber(),
                    p.GetNetname(),
                    pt(p.GetPosition()),
                    pt(p.GetSize()),
                    pt(p.GetDrillSize()),
                    int(p.GetShape()),
                    p.GetOrientationDegrees(),
                    list(p.GetLayerSet().Seq()),
                ]
            )
        footprints.append(
            [
                uid(f),
                f.GetReference(),
                f.GetValue(),
                pt(f.GetPosition()),
                f.GetOrientationDegrees(),
                f.IsFlipped(),
                f.IsLocked(),
                sorted(pads),
            ]
        )
    zones = []
    for z in board.Zones():
        zones.append(
            [
                uid(z),
                z.GetNetname(),
                z.IsLocked(),
                z.GetIsRuleArea(),
                list(z.GetLayerSet().Seq()),
                polygons(z.Outline()).wkb_hex,
            ]
        )
    groups = sorted(
        [uid(g), g.GetName(), sorted(uid(i) for i in g.GetItems())] for g in board.Groups()
    )
    return dict(footprints=sorted(footprints), zones=sorted(zones), groups=groups)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", type=Path, required=True)
    ap.add_argument("--case", type=Path, required=True)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--kicad-cli", required=True)
    ap.add_argument(
        "--spatial", action="store_true", help="Use the optional layer-aware obstacle broadphase"
    )
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    import pcbnew as k

    from pnr import gloss as gl
    from pnr import gloss_geometry as g
    from pnr.fab_profile import load_board
    from pnr.ir_extract import report as ir_report

    input_hash = digest(a.board)
    rules = json.loads(a.rules.read_text())
    case = json.loads(a.case.read_text())["case"]
    old = [tuple(p) for p in case["points"]]
    net = case["net"]
    board = load_board(a.board)
    board.BuildConnectivity()
    # Current cold native baseline, not inherited success from the older proof.
    for tag, path in [("baseline", a.board)]:
        with (a.out / (tag + "-drc.log")).open("w") as log:
            subprocess.run(
                [
                    a.kicad_cli,
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--severity-all",
                    "-o",
                    str(a.out / (tag + "-drc.json")),
                    str(path),
                ],
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
    drc0 = json.loads((a.out / "baseline-drc.json").read_text())
    model = gl.Model(board, rules, path=a.board, drc=drc0, guard_open_nets=True)
    layer = model.lid[case["layer"]]
    chains = model.chainset(net, layer)
    chain = next(c for c in chains.chains if c.legs == old)
    if chain.frozen or not model.info(net, layer)["eligible"]:
        raise RuntimeError("Real witness is not eligible")
    original_ctx = model.context(net, layer, chain, chains)
    allowed = model.allowed_items(net, layer, old, chain.width, chain.seg_ids)
    info = model.info(net, layer)
    width = chain.width / 1e6
    roi = g.bbox(old, g.TUBE + 1_000_000)
    obstacles = []
    dependent = set()
    zone_shapes = {}
    for item in model.layer_items[layer] + model.fills[layer] + model.keepouts[layer]:
        uid = gl.uid(item)
        if uid in allowed or not gl.boxes_overlap(gl.item_box(item), roi):
            continue
        cls = item.GetClass()
        ownnet = item.GetNetname()
        gap = (
            0
            if ownnet == net
            else max(info["clearance"] / 1e6, model.policy(ownnet)["clearance_mm"]) + 0.001
        )
        radius = 0
        if cls == "ZONE":
            keepout = item.GetIsRuleArea()
            geom = polygons(item.Outline() if keepout else item.GetFilledPolysList(layer))
            if not keepout:
                if item.IsLocked() or item.GetParentGroup():
                    # Protected macro copper is never silently treated as refillable.
                    raise RuntimeError("Protected plane in experiment region")
                dependent.add(uid)
                zone_shapes[uid] = geom
            else:
                gap = 0.001
        elif cls == "PCB_TRACK":
            geom = LineString([(p.x / 1e6, p.y / 1e6) for p in (item.GetStart(), item.GetEnd())])
            radius = item.GetWidth() / 2e6
        elif cls == "PCB_VIA":
            p = item.GetPosition()
            geom = Point(p.x / 1e6, p.y / 1e6)
            radius = item.GetWidth(layer) / 2e6
        else:
            q = k.SHAPE_POLY_SET()
            item.TransformShapeToPolygon(q, layer, 0, 1000, k.ERROR_OUTSIDE)
            geom = polygons(q)
        obstacles.append(Obstacle(uid, geom, radius, gap, case["layer"], ownnet, uid in dependent))
    if not dependent:
        raise RuntimeError("Expected dependent plane absent")
    # Fork ONLY the candidate-generation oracle. Remove the exact dependent zone
    # indices from this fork's buckets; fixed objects of the same net stay present.
    candidate_oracle = model.oracle.fork()
    plane_indices = {i for i, row in enumerate(candidate_oracle.obstacles) if row[3] in dependent}
    for key, indices in candidate_oracle.buckets.items():
        candidate_oracle.buckets[key] = indices - plane_indices
    candidate_oracle.cache = {}
    ctx = g.Context(
        chain.width,
        clear=lambda a, b: candidate_oracle.clear(
            net, layer, tuple(x / 1e6 for x in a), tuple(x / 1e6 for x in b), width
        ),
        contact=original_ctx._contact,
        obstacles=model.obstacle_points(layer, roi, set(allowed) | dependent),
        guards=original_ctx.guards,
        anchor_legs=original_ctx.anchor_legs,
        bounds=original_ctx.bounds,
        clearance=original_ctx.clearance,
    )

    def nm(points):
        return [tuple(round(v * 1e6) for v in point) for point in points]

    def segment_gate(a, b):
        aa, bb = nm((a, b))
        return ctx.clear(aa, bb) and not ctx.contact(aa, bb)

    def path_gate(path):
        pp = nm(path)
        return g.check_edit(old, pp, ctx, reference=old, tube=g.TUBE) is None and g.rule_r(
            old, pp, ctx.anchor_legs
        )

    track = Track(
        net,
        net,
        tuple(tuple(v / 1e6 for v in p) for p in old),
        width,
        info["clearance"] / 1e6,
        case["layer"],
    )
    config = Config(
        growth_mm=1,
        spatial_broadphase=a.spatial,
        growth_levels=(1.0,),
        max_nodes=150,
        max_states=50000,
        max_edges_checked=20000,
        seconds=90,
    )
    lookup = Controller([track], obstacles, config=config) if a.spatial else None
    candidate_obstacles = lookup.environment(track.id) if lookup else obstacles
    started = time.monotonic()
    plan = Planner(config, Energy()).plan(
        track,
        candidate_obstacles,
        allow_dependent=True,
        segment_gate=segment_gate,
        path_gate=path_gate,
    )
    dump(a.out / "plan.json", asdict(plan))
    dump(
        a.out / "geometry.json",
        {
            "track": asdict(track),
            "obstacles": [
                dict(
                    id=o.id,
                    geom=mapping(o.shape),
                    radius_mm=o.radius_mm,
                    clearance_mm=o.clearance_mm,
                    dependent_plane=o.dependent_plane,
                    net=o.net,
                )
                for o in obstacles
            ],
        },
    )
    print(
        "CANDIDATE",
        plan.reason,
        "bends",
        g.bends(old),
        "->",
        g.bends(nm(plan.after)),
        "length",
        length(plan.before),
        "->",
        length(plan.after),
        "seconds",
        time.monotonic() - started,
        flush=True,
    )
    if plan.after == plan.before:
        raise RuntimeError("No generated improvement")
    new = nm(plan.after)
    spec = gl.chain_spec(
        g,
        "gloss",
        model,
        net,
        layer,
        chain,
        new,
        {s.id: s for s in model.segs(net, layer)},
        anchor_legs=ctx.anchor_legs,
    )
    reason = gl.precheck(model, spec, False)
    if reason:
        raise RuntimeError("Precheck: " + str(reason))
    facts0 = gl.board_facts(board, rules, a.board.read_text())
    unchanged0 = native_tracks(board, chain.seg_ids)
    metadata0 = immutable_metadata(board)
    ir0 = ir_report(board, rules, a.out / "baseline-ir", str(a.board), False)
    applied, keep_alive = gl.apply_spec(model, spec)
    k.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    candidate = a.out / "candidate.kicad_pcb"
    for ext in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(a.board.with_suffix(ext), candidate.with_suffix(ext))
    if (a.board.parent / "fp-lib-table").exists():
        shutil.copyfile(a.board.parent / "fp-lib-table", a.out / "fp-lib-table")
    k.SaveBoard(str(candidate), board)

    # Homotopy must preserve FIXED copper, not stale tessellation points of the
    # jointly regenerated plane. All other native rechecks remain unchanged.
    class JointPlaneModel(gl.Model):
        def obstacle_points(self, la, box, exclude=()):
            return super().obstacle_points(la, box, set(exclude) | dependent)

    after = JointPlaneModel(board, rules, path=candidate, base=model)
    recheck = gl.recheck(after, spec, applied)
    unchanged1 = native_tracks(board, applied["created"])
    immutable_ok = unchanged0 == unchanged1
    metadata_ok = metadata0 == immutable_metadata(board)
    with (a.out / "candidate-drc.log").open("w") as log:
        subprocess.run(
            [
                a.kicad_cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "-o",
                str(a.out / "candidate-drc.json"),
                str(candidate),
            ],
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
    drc1 = json.loads((a.out / "candidate-drc.json").read_text())
    facts1 = gl.board_facts(board, rules, candidate.read_text())
    native_ok, reasons, new_keys = gl.compare(drc0, facts0, drc1, facts1, end=True)
    ir1 = ir_report(board, rules, a.out / "candidate-ir", str(candidate), False)
    # No rail may acquire an open or a status regression; changed passing rails
    # must remain within their original budget. Already-failing rails may not
    # worsen in resistance, even if marked soft in the source rules.
    ir_reasons = []
    ir_rows = {}
    for n, before in ir0.items():
        after_ir = ir1[n]
        r0 = before.get("r_eff_mohm")
        r1 = after_ir.get("r_eff_mohm")
        if set(after_ir.get("opens", [])) - set(before.get("opens", [])):
            ir_reasons.append(n + ":new_open")
        if before["status"] == "pass" and after_ir["status"] != "pass":
            ir_reasons.append(n + ":budget")
        if before["status"] != "pass" and r0 is not None and (r1 is None or r1 > r0 + 1e-6):
            ir_reasons.append(n + ":regression")
        ir_rows[n] = {
            "before_status": before["status"],
            "after_status": after_ir["status"],
            "before_mohm": r0,
            "after_mohm": r1,
            "change_percent": 100 * (r1 / r0 - 1) if r0 and r1 else None,
            "budget_mohm": before.get("budget_mohm"),
        }
    planes = []
    for z in board.Zones():
        if gl.uid(z) in dependent:
            p0 = zone_shapes[gl.uid(z)]
            p1 = polygons(z.GetFilledPolysList(layer))
            planes.append(
                {
                    "id": gl.uid(z),
                    "net": z.GetNetname(),
                    "lost_mm2": p0.difference(p1).area,
                    "gained_mm2": p1.difference(p0).area,
                }
            )
    report = {
        "accepted_isolated_prototype": native_ok
        and recheck is None
        and immutable_ok
        and metadata_ok
        and not ir_reasons,
        "engine_commit": "dd1ba7d47c2e18607f1772a9a48acabe8a62045d",
        "spatial_broadphase": a.spatial,
        "obstacles_before_broadphase": len(obstacles),
        "obstacles_after_broadphase": len(candidate_obstacles),
        "spatial_stats": dict(lookup._spatial.stats) if lookup else None,
        "kicad": k.Version(),
        "source_sha256": input_hash,
        "source_unchanged": digest(a.board) == input_hash,
        "native_gate": native_ok,
        "native_reasons": reasons,
        "new_native_violation_keys": list(new_keys),
        "recheck": recheck,
        "unmodified_copper_identical": immutable_ok,
        "footprints_pads_zone_definitions_groups_identical": metadata_ok,
        "opens_before": len(drc0["unconnected_items"]),
        "opens_after": len(drc1["unconnected_items"]),
        "violations_before": dict(Counter(v["type"] for v in drc0["violations"])),
        "violations_after": dict(Counter(v["type"] for v in drc1["violations"])),
        "objective_before": gl.objective(drc0, facts0),
        "objective_after": gl.objective(drc1, facts1),
        "length_before_mm": g.length(old) / 1e6,
        "length_after_mm": g.length(new) / 1e6,
        "bends_before": g.bends(old),
        "bends_after": g.bends(new),
        "energy_before_mm_equivalent": plan.energy_before,
        "energy_after_mm_equivalent": plan.energy_after,
        "ir_reasons": ir_reasons,
        "ir": ir_rows,
        "planes": planes,
        "reference_before": facts0["reference"],
        "reference_after": facts1["reference"],
        "skews_before": facts0["skews"],
        "skews_after": facts1["skews"],
        "limits": [
            "Native no-regression on this existing failing board, not a clean-board certificate.",
            "IR model uses existing 0.1 mm raster and existing current/load assumptions.",
            "Reference contract checked; no new RF full-wave/EM validation performed.",
            "One pinned selected chain, not boardwide optimization or a generality claim.",
        ],
    }
    dump(a.out / "validation.json", report)
    dump(a.out / "spec.json", spec)
    dump(a.out / "facts-before.json", facts0)
    dump(a.out / "facts-after.json", facts1)
    print("VALIDATION", json.dumps(report, sort_keys=True), flush=True)
    if not report["accepted_isolated_prototype"]:
        raise RuntimeError(
            "Candidate rejected; inspect validation.json. No production promotion occurred."
        )


if __name__ == "__main__":
    main()
