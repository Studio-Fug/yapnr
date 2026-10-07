"""Default-off energy proposal adapter for the transactional native gloss pass.

No KiCad or Shapely import occurs at module import. Geometry only proposes;
existing cold native DRC and phase-end gates remain mandatory. Joint refill
adds stricter electrical completeness checks, never weaker existing gates.
"""

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

NM = 1_000_000


def configure_worker_dependencies():
    """Activate the separately locked KiCad-ABI directory only for enabled workers."""
    raw = os.environ.get("PNR_ENERGY_SITE_PACKAGES")
    if not raw:
        return
    directory = Path(raw)
    expected = "%d.%d" % sys.version_info[:2]
    try:
        compatible = (
            directory.is_dir()
            and (directory / "python-version.txt").read_text().strip() == expected
        )
    except OSError:
        compatible = False
    if not compatible:
        raise RuntimeError("energy dependency directory does not match the KiCad Python ABI")
    path = str(directory.resolve())
    if path not in sys.path:
        sys.path.append(path)


def polygons(polyset):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    rows = []
    for i in range(polyset.OutlineCount()):
        ring = polyset.COutline(i)
        shell = [(ring.CPoint(j).x / NM, ring.CPoint(j).y / NM) for j in range(ring.PointCount())]
        holes = []
        for h in range(polyset.HoleCount(i)):
            ring = polyset.CHole(i, h)
            holes.append(
                [(ring.CPoint(j).x / NM, ring.CPoint(j).y / NM) for j in range(ring.PointCount())]
            )
        if len(shell) > 2:
            rows.append(Polygon(shell, holes).buffer(0))
    return unary_union(rows)


def nm(points):
    return [tuple(round(x * NM) for x in p) for p in points]


def _zone_allowed(zone, model):
    return (
        not zone.GetIsRuleArea()
        and not zone.IsLocked()
        and not zone.GetParentGroup()
        and zone.GetNetname() in {r["net"] for r in model.rules.get("ir_drop", [])}
    )


def dependencies(model, spec):
    """Validate an exact dependent-zone allowlist; never exclude arbitrary copper."""
    value = spec.get("energy") or {}
    ids = value.get("dependent_zones", [])
    if (
        not isinstance(ids, list)
        or any(not isinstance(x, str) for x in ids)
        or len(set(ids)) != len(ids)
    ):
        return "energy:invalid_dependencies"
    if spec.get("step") != "gloss":
        return "energy:wrong_step"
    rows = spec.get("old_segments_nm") or []
    ops = spec.get("ops") or {}
    if not rows or len({row[3] for row in rows}) != 1 or ops.get("width") != rows[0][3]:
        return "energy:width_invariant"
    if not set(ops.get("remove", ())) <= {row[0] for row in rows}:
        return "energy:unrelated_removal"
    zones = {z.m_Uuid.AsString(): z for z in model.zones}
    for id in ids:
        if id not in zones or not _zone_allowed(zones[id], model):
            return "energy:protected_or_uncontracted_plane"
        if not zones[id].IsOnLayer(model.lid[spec["layer"]]):
            return "energy:dependency_layer"
    try:
        old = tuple(tuple(p) for p in spec["old_nm"])
        chain = next(
            (
                c
                for c in model.chainset(spec["net"], model.lid[spec["layer"]]).chains
                if tuple(c.legs) == old or tuple(reversed(c.legs)) == old
            ),
            None,
        )
        if chain is None or {row[0] for row in rows} != set(chain.seg_ids):
            return "energy:chain_scope"
        expected = model.g.chain_ops(
            chain,
            [tuple(p) for p in spec["new_nm"]],
            {seg.id: seg for seg in model.segs(spec["net"], model.lid[spec["layer"]])},
        )

        def canonical(values):
            return sorted(json.dumps(row, sort_keys=True) for row in values)

        if (
            set(ops.get("remove", ())) != set(expected["remove"])
            or canonical(ops.get("keep", ())) != canonical(expected["keep"])
            or ops["width"] != expected["width"]
        ):
            return "energy:operation_scope"
    except (KeyError, TypeError, ValueError):
        return "energy:invalid_scope"
    return None


def propose(model, net, layer, chain, chains, ctx, deadline):
    """Mechanically generate a proposal for one already-eligible native chain."""
    from shapely.geometry import LineString, Point

    from pnr import gloss
    from pnr import gloss_geometry as g
    from pnr.energy_track_geometry import Config, Controller, Energy, Obstacle, Planner, Track

    width = chain.width / NM
    info = model.info(net, layer)
    if info["si"] or any(model.by_uid[i].GetParentGroup() for i in chain.seg_ids):
        ctx.why = "energy:protected_si_or_group"
        return None, None
    old = chain.legs
    allowed = model.allowed_items(net, layer, old, chain.width, chain.seg_ids)
    # Native shape checks still query the full board. This spatial item grid is
    # only conservative candidate geometry; it never grants clearance by itself.
    box = g.bbox(old, g.TUBE + 2 * NM)
    items = sorted(model.layer_grid(layer).query(box), key=gloss.uid)
    obstacles = []
    dependent = set()
    for item in items:
        id = gloss.uid(item)
        if id in allowed:
            continue
        kind = item.GetClass()
        other = item.GetNetname()
        clearance = (
            0
            if other == net
            else max(info["clearance"] / NM, model.policy(other)["clearance_mm"]) + 0.001
        )
        radius = 0
        if kind == "ZONE":
            is_rule = item.GetIsRuleArea()
            shape = polygons(item.Outline() if is_rule else item.GetFilledPolysList(layer))
            if _zone_allowed(item, model) and other != net:
                dependent.add(id)
            if is_rule:
                clearance = 0.001
        elif kind == "PCB_TRACK":
            shape = LineString([(p.x / NM, p.y / NM) for p in (item.GetStart(), item.GetEnd())])
            radius = item.GetWidth() / 2 / NM
        elif kind == "PCB_VIA":
            p = item.GetPosition()
            shape = Point(p.x / NM, p.y / NM)
            radius = item.GetWidth(layer) / 2 / NM
        else:
            poly = model.k.SHAPE_POLY_SET()
            item.TransformShapeToPolygon(poly, layer, 0, gloss.POLY_ERROR, model.k.ERROR_OUTSIDE)
            shape = polygons(poly)
        if not shape.is_empty:
            obstacles.append(
                Obstacle(id, shape, radius, clearance, model.lname[layer], other, id in dependent)
            )
    oracle = model.oracle.fork(deadline=deadline)
    removed = {i for i, row in enumerate(oracle.obstacles) if row[3] in dependent}
    for key, indices in oracle.buckets.items():
        oracle.buckets[key] = indices - removed
    oracle.cache = {}
    candidate_ctx = g.Context(
        chain.width,
        clear=lambda a, b: oracle.clear(
            net, layer, tuple(x / NM for x in a), tuple(x / NM for x in b), width
        ),
        contact=ctx._contact,
        obstacles=model.obstacle_points(layer, box, set(allowed) | dependent),
        guards=ctx.guards,
        anchor_legs=ctx.anchor_legs,
        bounds=ctx.bounds,
        clearance=ctx.clearance,
        cap=ctx._cap,
    )

    def segment_gate(a, b):
        a, b = nm((a, b))
        return candidate_ctx.clear(a, b) and not candidate_ctx.contact(a, b)

    def path_gate(points):
        points = nm(points)
        return g.check_edit(
            old, points, candidate_ctx, reference=old, tube=g.TUBE
        ) is None and g.rule_r(old, points, candidate_ctx.anchor_legs)

    track = Track(
        net,
        net,
        tuple(tuple(x / NM for x in p) for p in old),
        width,
        info["clearance"] / NM,
        model.lname[layer],
    )
    config = Config(
        growth_mm=g.TUBE / NM,
        growth_levels=(1.0,),
        max_nodes=150,
        max_edges_checked=20000,
        max_states=50000,
        seconds=max(0.001, min(20, deadline - time.monotonic())),
        spatial_broadphase=True,
    )
    lookup = Controller([track], obstacles, config=config)
    proposal = Planner(config, Energy()).plan(
        track,
        lookup.environment(net),
        allow_dependent=True,
        segment_gate=segment_gate,
        path_gate=path_gate,
    )
    for key, value in candidate_ctx.calls.items():
        ctx.calls[key] += value
    ctx.why = proposal.reason
    if proposal.after == proposal.before:
        return None, None
    return nm(proposal.after), dict(
        dependent_zones=sorted(dependent),
        energy_before=proposal.energy_before,
        energy_after=proposal.energy_after,
        geometry=proposal.diagnostics,
        spatial=dict(lookup._spatial.stats),
    )


def _sexpr(text):
    """Parse native formatter output without a new external parser dependency."""
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[^\s()]+|[()]', text)
    stack = []
    root = None
    for token in tokens:
        if token == "(":
            node = []
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise ValueError("Multiple native serialization roots")
            else:
                root = node
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced native serialization")
            stack.pop()
        elif stack:
            stack[-1].append(token)
        else:
            raise ValueError("Atom outside native serialization root")
    if stack or root is None:
        raise ValueError("Incomplete native serialization")
    return root


def _serialized_board(board):
    import pcbnew as k

    formatter = k.STRING_FORMATTER()
    writer = k.PCB_IO_KICAD_SEXPR()
    writer.FormatBoardToFormatter(formatter, board)
    return _sexpr(formatter.GetString())


def _uuid(node):
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == "uuid":
            return child[1].strip('"')
    return None


def immutable(board):
    """Full native non-fill definitions, including stackup and protected metadata.

    Ordinary track bodies are governed by exact per-transaction receipts.
    Only regenerated, unprotected zone fill polygons and the runtime fill-state
    bit are omitted. Priority, clearance, thermal/fill settings, footprint/pad
    properties, locks, groups, protected arc midpoints and board setup remain.
    """
    from pnr.gloss import uid

    ordinary = {
        uid(t)
        for t in board.GetTracks()
        if t.GetClass() == "PCB_TRACK" and not t.IsLocked() and not t.GetParentGroup()
    }
    zones = list(board.Zones()) + [z for f in board.GetFootprints() for z in f.Zones()]
    protected = set()
    for zone in zones:
        parent = zone.GetParent()
        parent_protected = (
            parent is not None
            and parent.GetClass() == "FOOTPRINT"
            and (parent.IsLocked() or parent.GetParentGroup())
        )
        if zone.IsLocked() or zone.GetParentGroup() or parent_protected:
            protected.add(uid(zone))

    def normalize(node):
        if not isinstance(node, list):
            return node
        is_zone = bool(node and node[0] == "zone")
        locked = is_zone and _uuid(node) in protected
        out = []
        for child in node:
            if is_zone and isinstance(child, list) and child:
                if child[0] in ("filled_polygon", "fill_segments") and not locked:
                    continue
                if child[0] == "fill":
                    # yes/no describes the generated state, not a fill parameter.
                    child = [v for i, v in enumerate(child) if not (i == 1 and v in ("yes", "no"))]
            out.append(normalize(child))
        return out

    root = _serialized_board(board)
    rows = [
        normalize(row)
        for row in root[1:]
        if not (isinstance(row, list) and row and row[0] == "segment" and _uuid(row) in ordinary)
    ]
    settings = board.GetDesignSettings()
    scalar_settings = {}
    for name in sorted(n for n in dir(settings) if n.startswith("m_")):
        value = getattr(settings, name)
        if type(value) in (bool, int, float, str):
            scalar_settings[name] = value
    rows.append(["design_settings", scalar_settings])
    return hashlib.sha256(
        json.dumps(
            sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)), sort_keys=True
        ).encode()
    ).hexdigest()


def track_snapshot(board, exclude=()):
    """Full native serialization of every non-owned track, arc and via."""
    from pnr.gloss import uid

    ids = {uid(t) for t in board.GetTracks()} - set(exclude)
    return {
        _uuid(node): node
        for node in _serialized_board(board)[1:]
        if isinstance(node, list) and _uuid(node) in ids
    }


def fill_snapshot(board):
    return {
        (z.m_Uuid.AsString(), board.GetLayerName(la)): polygons(z.GetFilledPolysList(la))
        for z in board.Zones()
        if not z.GetIsRuleArea()
        for la in board.GetEnabledLayers().CuStack()
        if z.IsOnLayer(la)
    }


def dirty_fill(before, after):
    out = []
    for key in sorted(set(before) | set(after)):
        if key not in before or key not in after:
            shape = before.get(key) if key in before else after[key]
        else:
            shape = before[key].symmetric_difference(after[key])
        if not shape.is_empty:
            out.append(dict(zone=key[0], layer=key[1], box=[round(x * NM) for x in shape.bounds]))
    return out


def facts(board, rules, out_dir, board_path, sources=(), project_path=None):
    """Bounded by the enclosing native worker, with existing bounded IR solvers."""
    from pnr.ir_extract import report

    ir = report(board, rules, Path(out_dir), str(board_path), False)
    from pnr.gloss import si_nets

    si, _status = si_nets(board, rules, sources)
    # Existing RF/SI verification is not a full-wave certificate. A changed
    # plane on a board with RF/SI contracts is deliberately not promoted here.
    rf_si = bool(
        si is None
        or si
        or rules.get("si_intents")
        or any(str(key).lower().startswith("rf_") and value for key, value in rules.items())
        or any(group.GetName().upper().startswith(("RF", "RFM")) for group in board.Groups())
    )
    return dict(
        immutable=immutable(board),
        project=hashlib.sha256(
            Path(project_path or board_path).with_suffix(".kicad_pro").read_bytes()
        ).hexdigest(),
        rules=hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest(),
        ir=ir,
        ir_contracts=sorted(r["net"] for r in rules.get("ir_drop", [])),
        rf_si_requires_external_verification=rf_si,
    )


def compare(before, after, dependent_nets=()):
    """Extra, fail-closed acceptance conditions; callers also run gloss.compare."""
    reasons = []
    b = before.get("energy")
    a = after.get("energy")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return ["energy:missing_facts"]
    if not a.get("immutable") or a["immutable"] != b.get("immutable"):
        reasons.append("energy:immutable")
    for key in ("project", "rules"):
        if not a.get(key) or a.get(key) != b.get(key):
            reasons.append("energy:" + key + "_changed")
    for side, row in (("before", before), ("after", after)) if dependent_nets else ():
        if row.get("unqualified_pairs", 0):
            reasons.append("energy:unqualified_pairs:" + side)
        for name, skew in row.get("skews", {}).items():
            if not isinstance(skew, (int, float)) or not math.isfinite(skew):
                reasons.append("energy:missing_skew:" + name + ":" + side)
    if dependent_nets and (
        a.get("rf_si_requires_external_verification") is not False
        or b.get("rf_si_requires_external_verification") is not False
    ):
        reasons.append("energy:rf_si_verification_required")
    old = b.get("ir")
    new = a.get("ir")
    if not isinstance(old, dict) or not isinstance(new, dict) or set(old) != set(new):
        return reasons + ["energy:missing_ir"]
    for net in sorted(set(dependent_nets)):
        if net not in old or net not in new:
            reasons.append("energy:missing_ir_contract:" + net)
    for net, base in old.items():
        candidate = new[net]
        if base.get("status") != "pass" or candidate.get("status") != "pass":
            if net in dependent_nets or base != candidate:
                reasons.append("energy:ir_not_pass:" + net)
            continue
        r0, r1 = base.get("r_eff_mohm"), candidate.get("r_eff_mohm")
        if (
            not isinstance(r0, (int, float))
            or not isinstance(r1, (int, float))
            or not math.isfinite(r0)
            or not math.isfinite(r1)
        ):
            reasons.append("energy:ir_unknown:" + net)
        if candidate.get("opens"):
            reasons.append("energy:ir_open:" + net)
        budget = candidate.get("budget_mohm")
        if isinstance(r1, (int, float)) and (
            not isinstance(budget, (int, float)) or not math.isfinite(budget) or r1 > budget + 1e-9
        ):
            reasons.append("energy:ir_budget:" + net)
    return reasons
