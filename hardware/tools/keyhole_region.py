#!/usr/bin/env python3
"""Transactional regional signal repair with native DRC gating.

Reopens only whole unlocked straight-track segments inside explicit bounds.
External pad and branch attachments remain fixed. Layer alternatives and
selected signal via relocation are opt-in; power/USB copper and footprints
remain immutable. A complete regional
solve is only accepted after native global connectivity/DRC improvement.
"""
import argparse
from collections import defaultdict
from dataclasses import asdict
import fnmatch
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pnr"))
import pcbnew
from pnr.profile import retain_native
from pnr.route.detail.regional import (
    Request,
    solve_region,
    preserves_connections,
    needs_connection,
)
from pnr.route.detail.keyhole import acceptable
from pnr.route.detail.layered import solve_layered_region
from pnr.route.detail.joint import solve_joint_region
from pnr.route.detail.portal_joint import solve_portal_region


print(
    "IMPLEMENTATION "
    + json.dumps(
        {
            name: dict(
                path=sys.modules[name].__file__,
                sha256=hashlib.sha256(Path(sys.modules[name].__file__).read_bytes()).hexdigest(),
            )
            for name in (
                "pnr.route.detail.keyhole",
                "pnr.route.detail.layered",
                "pnr.route.detail.joint",
            )
        }
    ),
    flush=True,
)


def pad_partition(board):
    """Native connected pad components, including actual filled-plane islands.

    GetConnectedItems walks native CN items (including zone islands). Do not
    traverse ZONE objects ourselves: one zone UUID may contain disconnected
    islands. Native continuous/split-zone controls exercise this distinction.
    """
    cn = board.GetConnectivity()
    seen = set()
    groups = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            identity = pad.m_Uuid.AsString()
            if identity in seen:
                continue
            group = {identity}
            group.update(
                t.m_Uuid.AsString()
                for t in cn.GetConnectedItems(pad)
                if isinstance(t, pcbnew.PAD) and t.GetNetCode() == pad.GetNetCode()
            )
            seen.update(group)
            groups.append(sorted(group))
    return groups


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--net", action="append", required=True)
    ap.add_argument("--source-pad", required=True, help="reference.pad")
    ap.add_argument("--target-pad", required=True, help="reference.pad")
    ap.add_argument("--source-pad-uuid")
    ap.add_argument("--target-pad-uuid")
    ap.add_argument(
        "--bounds", type=float, nargs=4, required=True, metavar=("X0", "Y0", "X1", "Y1")
    )
    ap.add_argument("--pitch", type=float, default=0.1)
    ap.add_argument(
        "--ground-leaf",
        action="store_true",
        help="add a same-package ground-pad connection; preserves all copper",
    )
    ap.add_argument("--max-orders", type=int, default=8)
    ap.add_argument(
        "--preserve-copper",
        action="store_true",
        help="add a layered connection without reopening existing copper",
    )
    ap.add_argument("--max-expansions", type=int, default=10000)
    ap.add_argument("--max-seconds", type=float, default=120.0)
    ap.add_argument(
        "--joint",
        action="store_true",
        help="negotiate conflicts between independently planned paths; requires --layers",
    )
    ap.add_argument("--kicad-cli", required=True)
    ap.add_argument(
        "--layers",
        action="store_true",
        help="allow checked through-via transitions to In2.Cu/B.Cu",
    )
    ap.add_argument(
        "--relocate-vias",
        action="store_true",
        help="reopen selected signal vias and their in-region tracks on routing layers",
    )
    ap.add_argument(
        "--source-via-window",
        action="append",
        nargs=5,
        default=[],
        metavar=("NET", "X0", "Y0", "X1", "Y1"),
        help="constrain the first escape via for a selected net",
    )
    ap.add_argument(
        "--rules", type=Path, help="Source-resolved routing policy for generic native loop"
    )
    ap.add_argument("--annotation-source", action="append", default=[], type=Path)
    ap.add_argument(
        "--portal-joint",
        action="store_true",
        help="coordinate surface escapes before joint trunk routing; requires --joint --layers",
    )
    args = ap.parse_args()
    if args.portal_joint and not (args.joint and args.layers):
        ap.error("portal joint requires layered joint routing")
    if args.joint and not args.layers:
        ap.error("joint search requires --layers")
    if not 0 < args.max_seconds < math.inf:
        ap.error("max-seconds must be positive and finite")
    via_windows = {v[0]: tuple(map(float, v[1:])) for v in args.source_via_window}
    if any(n not in args.net or w[0] >= w[2] or w[1] >= w[3] for n, w in via_windows.items()):
        ap.error("invalid source-via window")
    if args.relocate_vias and not args.layers:
        ap.error("via relocation requires --layers")
    x0, y0, x1, y1 = args.bounds
    if x0 >= x1 or y0 >= y1:
        ap.error("invalid bounds")
    if args.out_dir.exists():
        ap.error("new output directory required")
    settings = json.loads(args.board.with_suffix(".kicad_pro").read_text())["net_settings"]
    classes = {c["name"]: c for c in settings["classes"]}

    def policy(net):
        matches = [
            classes[p["netclass"]]
            for p in settings["netclass_patterns"]
            if fnmatch.fnmatchcase(net, p["pattern"])
        ]
        return min(matches, key=lambda c: c.get("priority", 999)) if matches else classes["Default"]

    if any(policy(n)["track_width"] > 0.2 for n in args.net):
        ap.error("regional width is below project requirement")
    # Reviewed small-signal/ESD auxiliary leaves; power trunks and USB D+/D-
    # remain excluded. Board.pd-1 is the PD controller's LDO_3V3 auxiliary net.
    allowed = {
        "scl",
        "sda",
        "MODE",
        "ILIM",
        "CC1",
        "CC2",
        "A5",
        "B5",
        "fault",
        "nFAULT_IN",
        "VBIAS",
        "board.pd-1",
    }
    if args.ground_leaf:
        if (
            not args.preserve_copper
            or args.net != ["lv"]
            or policy("lv")["name"] != "gnd"
            or args.source_pad.rsplit(".", 1)[0] != args.target_pad.rsplit(".", 1)[0]
        ):
            ap.error(
                "ground-leaf requires preserved copper and same-package lv pads in the gnd class"
            )
    elif not args.rules and any(
        policy(n)["name"] != "Default" or n not in allowed for n in args.net
    ):
        ap.error("regional policy supports only reviewed Default-class signals")
    from pnr.fab_profile import load_board  # custom rules in force for the final refill

    b = load_board(args.board)
    retain_native(b)
    b.BuildConnectivity()
    entry_rules = json.loads(args.rules.read_text()) if args.rules else None
    # Fab numbers: the routing policy's fab block, else the selected profile
    # (PNR_FAB_PROFILE). Legacy reproduces the former 0.6/0.3 via, 0.15 clearance.
    from pnr.fab_profile import active_geometry, geometry

    fg = geometry(entry_rules) if entry_rules is not None else active_geometry()
    before_entries = {}
    if args.rules:
        from pnr.via_coalesce import protected
        from pnr.pad_entry import snapshot

        before_entries = snapshot(b, entry_rules)
        excluded, _ = protected(b, entry_rules, args.annotation_source)
        if any(n in excluded or policy(n)["name"] != "Default" for n in args.net):
            ap.error("net protected by source/routing policy")
    before_connections = pad_partition(b)
    layer = pcbnew.F_Cu
    route_layers = [pcbnew.F_Cu, pcbnew.In2_Cu, pcbnew.B_Cu] if args.layers else [pcbnew.F_Cu]
    pads = [p for f in b.GetFootprints() for p in f.Pads()]
    tracks = list(b.GetTracks())
    uid = lambda t: t.m_Uuid.AsString()
    pt = lambda p: (p.x / 1e6, p.y / 1e6)
    vec = lambda p: pcbnew.VECTOR2I(round(p[0] * 1e6), round(p[1] * 1e6))
    inside = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1
    from pnr.pad_identity import resolve_pad

    source_pad = resolve_pad(b, args.source_pad, args.source_pad_uuid)
    target_pad = resolve_pad(b, args.target_pad, args.target_pad_uuid)
    net = source_pad.GetNetname()
    if target_pad.GetNetname() != net or net not in args.net:
        ap.error("source and target must share a selected signal net")

    if args.ground_leaf:
        fp = source_pad.GetParentFootprint()
        if math.dist(pt(source_pad.GetPosition()), pt(target_pad.GetPosition())) > 5:
            ap.error("ground leaf exceeds package-local distance")
        if not any(
            p.GetNetname() == "lv" and p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH for p in fp.Pads()
        ):
            ap.error("ground leaf requires an existing same-package ground through-hole pad")

    def eligible(t):
        if args.preserve_copper or t.IsLocked() or t.GetNetname() not in args.net:
            return False
        if isinstance(t, pcbnew.PCB_VIA):
            p = pt(t.GetPosition())
            return (
                args.relocate_vias
                and t.GetWidth(pcbnew.F_Cu) == round(fg.via_diameter * 1e6)
                and t.GetDrill() == round(fg.via_drill * 1e6)
                and x0 + 0.3 <= p[0] <= x1 - 0.3
                and y0 + 0.3 <= p[1] <= y1 - 0.3
            )
        return (
            type(t) == pcbnew.PCB_TRACK
            and t.GetWidth() == 200000
            and t.GetLayer() in (route_layers if args.relocate_vias else [layer])
            and inside(pt(t.GetStart()))
            and inside(pt(t.GetEnd()))
        )

    selected = [t for t in tracks if eligible(t)]
    anchor_layers_map = {}
    anchor_items = defaultdict(set)
    candidates = {uid(t): t for t in selected}
    cn = b.GetConnectivity()
    todo = set(candidates)
    requests, chains, selected = [], [], []
    retained_components = []
    while todo:
        root = min(todo)
        members, stack = {root}, [candidates[root]]
        anchors = set()
        missing_contacts = []
        while stack:
            t = stack.pop()
            for other in list(cn.GetConnectedTracks(t)) + list(cn.GetConnectedPads(t)):
                if uid(other) in candidates:
                    if uid(other) not in members:
                        members.add(uid(other))
                        stack.append(candidates[uid(other)])
                    continue
                common_layers = [
                    la for la in route_layers if t.IsOnLayer(la) and other.IsOnLayer(la)
                ]
                if not common_layers:
                    continue
                contact_layer = common_layers[0]
                # Find an unchanged attachment inside the shared copper, without
                # assuming KiCad tracks were already split at T junctions.
                points = [pt(t.GetStart()), pt(t.GetEnd())]
                points += (
                    [pt(other.GetPosition())]
                    if isinstance(other, (pcbnew.PAD, pcbnew.PCB_VIA))
                    else [pt(other.GetStart()), pt(other.GetEnd())]
                )
                # Track end centerlines can stop just outside a pad while their
                # finite-width copper overlaps it. Include overlap-box samples.
                tb, ob = t.GetBoundingBox(), other.GetBoundingBox()
                left = max(tb.GetLeft(), ob.GetLeft()) / 1e6
                right = min(tb.GetRight(), ob.GetRight()) / 1e6
                top = max(tb.GetTop(), ob.GetTop()) / 1e6
                bottom = min(tb.GetBottom(), ob.GetBottom()) / 1e6
                if left <= right and top <= bottom:
                    nx = max(1, math.ceil((right - left) / 0.025))
                    ny = max(1, math.ceil((bottom - top) / 0.025))
                    points += [
                        (
                            left + (right - left) * (i + 0.5) / nx,
                            top + (bottom - top) * (j + 0.5) / ny,
                        )
                        for i in range(nx)
                        for j in range(ny)
                    ]
                contacts = [
                    p
                    for p in points
                    if inside(p)
                    and all(
                        item.GetEffectiveShape(contact_layer).Collide(
                            pcbnew.SHAPE_CIRCLE(vec(p), 1), 0
                        )
                        for item in (t, other)
                    )
                ]
                if not contacts:
                    # Native finite-width contact can lie outside the requested
                    # search window even when the track centerline is inside.
                    # Retain the entire component instead of dropping a contact
                    # or aborting unrelated route requests.
                    missing_contacts.append(dict(item=uid(t), attachment=uid(other)))
                    continue
                anchor = min(
                    contacts,
                    key=lambda p: min(math.dist(p, pt(t.GetStart())), math.dist(p, pt(t.GetEnd()))),
                )
                anchors.add(anchor)
                anchor_items[t.GetNetname(), anchor].add(uid(other))
                anchor_layers_map.setdefault((t.GetNetname(), anchor), set()).update(
                    route_layers.index(la)
                    for la in common_layers
                    if other.GetEffectiveShape(la).Collide(pcbnew.SHAPE_CIRCLE(vec(anchor), 1), 0)
                )
        todo -= members
        if missing_contacts:
            retained_components.append(
                dict(
                    net=candidates[root].GetNetname(),
                    members=sorted(members),
                    reason="attachment_outside_search_or_unsampled",
                    contacts=missing_contacts,
                )
            )
            continue
        # Preserve isolated loops/stubs unchanged rather than silently deleting.
        if len(anchors) < 2:
            continue
        n = candidates[root].GetNetname()
        selected.extend(candidates[k] for k in sorted(members))
        chains.append(dict(net=n, anchors=sorted(anchors), removed=sorted(members)))
        # A connected copper component may be rerouted as a different tree, but
        # every one of its external contacts must still belong to that tree.
        tree = [min(anchors)]
        pending = anchors - set(tree)
        while pending:
            target = min(pending, key=lambda p: min(math.dist(p, q) for q in tree))
            requests.append(
                Request(
                    "restore-" + str(len(requests)),
                    n,
                    list(tree),
                    [target],
                    0.2,
                    max(fg.clearance_gap, policy(n).get("clearance", fg.clearance) + 0.001),
                )
            )
            tree.append(target)
            pending.remove(target)
    ids = {uid(t) for t in selected}
    remaining = [t for t in tracks if uid(t) not in ids]
    # The ripped copper is discarded: Delete, not Remove (a Removed item outlives
    # its board; see pnr.fanout_reserve.release). Only uuids and len(selected)
    # are read afterwards; tracks keeps invalid wrappers, so use remaining.
    for t in selected:
        b.Delete(t)
    b.BuildConnectivity()
    cn = b.GetConnectivity()
    # Multiple contacts on one unchanged via/track island do not need new
    # traces between them. Such edge-of-copper contacts may not even support a
    # fresh full-width centerline. Query native connectivity AFTER removal.
    unchanged = {uid(t): t for t in pads + remaining}
    component_cache = {}
    anchor_components = {}
    for key, items in anchor_items.items():
        connected = set()
        for identity in items:
            if identity not in component_cache:
                item = unchanged[identity]
                component_cache[identity] = {identity} | {
                    uid(other)
                    for other in cn.GetConnectedItems(item)
                    if uid(other) in unchanged and other.GetNetCode() == item.GetNetCode()
                }
            connected.update(component_cache[identity])
        anchor_components[key] = connected
    redundant_restorations = [
        r.name for r in requests if not needs_connection(r, anchor_components)
    ]
    requests = [r for r in requests if needs_connection(r, anchor_components)]

    def island(item):
        seen = {uid(item)}
        stack = [item]
        result = []
        while stack:
            t = stack.pop()
            result.append(t)
            for other in list(cn.GetConnectedPads(t)) + list(cn.GetConnectedTracks(t)):
                if uid(other) not in seen and other.GetNetCode() == item.GetNetCode():
                    seen.add(uid(other))
                    stack.append(other)
        return result

    def accesses(group):
        points = set()
        for t in group:
            access_layers = [index for index, la in enumerate(route_layers) if t.IsOnLayer(la)]
            if not access_layers:
                continue
            ps = (
                [t.GetPosition()]
                if isinstance(t, (pcbnew.PAD, pcbnew.PCB_VIA))
                else [t.GetStart(), t.GetEnd()]
            )
            for position in ps:
                p = pt(position)
                if inside(p):
                    points.add(p)
                    anchor_layers_map.setdefault((t.GetNetname(), p), set()).update(access_layers)
        return sorted(points)

    aa, zz = accesses(island(source_pad)), accesses(island(target_pad))
    if args.ground_leaf:
        # Ground leaves terminate at the selected same-package pad; do not
        # substitute a remote ground-island anchor or create a power trunk.
        aa, zz = [pt(source_pad.GetPosition())], [pt(target_pad.GetPosition())]
    if not aa or not zz:
        ap.error("both islands need access on a routing layer inside region")
    requests.insert(
        0,
        Request(
            "missing",
            net,
            aa,
            zz,
            0.2,
            max(fg.clearance_gap, policy(net).get("clearance", fg.clearance) + 0.001),
        ),
    )
    # Conservative static native copper oracle. Clearance includes project rules.
    obstacles = []
    retain_native(obstacles)
    buckets = defaultdict(set)

    def add(shape, box, gap, n, identity, la, smd=None):
        i = len(obstacles)
        obstacles.append((shape, gap, n, identity, la, smd))
        for x in range(math.floor(box.GetLeft() / 1e6) - 1, math.floor(box.GetRight() / 1e6) + 2):
            for y in range(
                math.floor(box.GetTop() / 1e6) - 1,
                math.floor(box.GetBottom() / 1e6) + 2,
            ):
                buckets[la, x, y].add(i)

    copper_layers = list(b.GetEnabledLayers().CuStack())
    from pnr.via_in_pad import hole_keepouts

    for t in pads + remaining:
        for la in copper_layers:
            if t.IsOnLayer(la):
                add(
                    t.GetEffectiveShape(la),
                    t.GetBoundingBox(),
                    max(
                        fg.clearance_gap,
                        policy(t.GetNetname()).get("clearance", fg.clearance) + 0.001,
                    ),
                    t.GetNetname(),
                    uid(t),
                    la,
                    # SMD pads carry the pad: pnr.via_in_pad judges vias against it.
                    (
                        t
                        if isinstance(t, pcbnew.PAD) and t.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
                        else None
                    ),
                )
            if isinstance(t, pcbnew.PAD) and t.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                add(
                    t.GetEffectiveHoleShape(),
                    t.GetBoundingBox(),
                    (
                        0.201
                        if fg.npth_hole_clearance is None
                        else round(fg.npth_hole_clearance + 0.001, 9)
                    ),
                    None,
                    uid(t),
                    la,
                )
            elif (
                fg.pth_hole_clearance is not None
                and isinstance(t, pcbnew.PAD)
                and t.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                and t.IsOnLayer(la)
                and max(t.GetDrillSize().x, t.GetDrillSize().y) > 0
            ):
                # Fab profile: plated drill wall to foreign copper (own net may
                # enter): component PTH 0.35, a footprint's via-class drill 0.20.
                kind = fg.hole_kind(False, True, max(t.GetDrillSize().x, t.GetDrillSize().y) / 1e6)
                add(
                    t.GetEffectiveHoleShape(),
                    t.GetBoundingBox(),
                    round(fg.pad_hole_clearance(kind) + 0.001, 9),
                    t.GetNetname(),
                    uid(t),
                    la,
                )
        # A via with removed unused pads (5B in-pad) is its bare hole where it has
        # no pad: foreign copper keeps the via hole clearance (0.20) from the wall.
        for la, hole, hole_gap in hole_keepouts(fg, t, copper_layers):
            add(hole, t.GetBoundingBox(), hole_gap, t.GetNetname(), uid(t), la)
    for z in b.Zones():
        for la in copper_layers:
            if z.GetIsRuleArea() and z.IsOnLayer(la) and z.GetDoNotAllowTracks():
                add(z.Outline(), z.GetBoundingBox(), 0.001, None, uid(z), la)
            elif not z.GetIsRuleArea() and z.IsOnLayer(la) and la in route_layers:
                ap.error(
                    "routing through filled surface/inner power zones requires an explicit plane policy"
                )
    outline = pcbnew.SHAPE_POLY_SET()
    b.GetBoardPolygonOutlines(outline, False)
    if outline.OutlineCount() != 1 or outline.VertexCount(0) != 4:
        ap.error("regional Mini adapter currently requires a rectangular outline")
    box = outline.BBox()
    probe = pcbnew.PCB_TRACK(b)
    probe.SetLayer(layer)
    static_hits = defaultdict(int)
    cache = {}
    obstacle_radius = max([o[1] for o in obstacles], default=0.201)

    def clear_layer(r, layer_index, a, z):
        la = route_layers[layer_index]
        key = (la, r.net, r.width, r.clearance, tuple(sorted((a, z))))
        if key in cache:
            return cache[key]
        edge = r.width / 2 + fg.edge_gap
        if any(
            not (
                box.GetLeft() / 1e6 + edge <= p[0] <= box.GetRight() / 1e6 - edge
                and box.GetTop() / 1e6 + edge <= p[1] <= box.GetBottom() / 1e6 - edge
            )
            for p in (a, z)
        ):
            return False
        probe.SetLayer(la)
        probe.SetWidth(round(r.width * 1e6))
        probe.SetStart(vec(a))
        probe.SetEnd(vec(z))
        shape = probe.GetEffectiveShape(la)
        found = set()
        radius = r.width / 2 + obstacle_radius
        for x in range(
            math.floor(min(a[0], z[0]) - radius),
            math.floor(max(a[0], z[0]) + radius) + 1,
        ):
            for y in range(
                math.floor(min(a[1], z[1]) - radius),
                math.floor(max(a[1], z[1]) + radius) + 1,
            ):
                found.update(buckets[la, x, y])
        for i in sorted(found):
            other, gap, n, identity, _, smd = obstacles[i]
            if n == r.net:
                continue
            if other.Collide(shape, round(max(gap, r.clearance if n is not None else gap) * 1e6)):
                static_hits[identity] += 1
                cache[key] = False
                return False
        cache[key] = True
        return True

    def clear(r, a, z):
        return clear_layer(r, 0, a, z)

    from pnr.reference_guard import ReferenceGuard

    reference_guard = ReferenceGuard(b, entry_rules or {})
    via_cache = {}
    drilled = [
        (t.GetEffectiveHoleShape(), t.GetBoundingBox(), t)
        for t in pads + remaining
        if isinstance(t, pcbnew.PCB_VIA)
        or isinstance(t, pcbnew.PAD)
        and max(t.GetDrillSize().x, t.GetDrillSize().y) > 0
    ]
    retain_native(drilled)

    # Drill gap (nm) a new via needs to each existing hole: one value (0.201)
    # before profiles; via / component PTH / NPTH differ under a fab profile, and
    # a footprint's via-class pad drill (under 0.30) counts as a via.
    def hole_kind(t):
        if isinstance(t, pcbnew.PCB_VIA):
            return fg.hole_kind(True)
        return fg.hole_kind(
            False,
            t.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH,
            max(t.GetDrillSize().x, t.GetDrillSize().y) / 1e6,
        )

    drilled_gaps = [
        (other, bb, t, round((fg.via_hole_gap(hole_kind(t)) + 0.001) * 1e6))
        for other, bb, t in drilled
    ]
    hole_reach = max(0.6, fg.max_via_hole_gap + 0.001 + fg.via_drill / 2)
    via_margin = fg.via_edge_margin()
    via_zones = [z for z in b.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
    # The board is immutable during search; resolve native metadata once.
    existing_vias_by_net = defaultdict(list)
    for t in remaining:
        if isinstance(t, pcbnew.PCB_VIA):
            existing_vias_by_net[t.GetNetname()].append((pt(t.GetPosition()), t))
    net_codes = {pad.GetNetname(): pad.GetNetCode() for pad in pads}

    from pnr.via_in_pad import smd_keepout_violated, in_pad_size, style_in_pad_via

    smd_pads = [t for t in pads if t.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
    # (net, x, y) -> (diameter, drill) of a checked 5B filled in-pad via (profile only).
    in_pad_vias = {}
    in_pad_key = lambda net, p: (net, round(p[0], 6), round(p[1], 6))

    def make_via(p, code, size=None):
        diameter, drill = size or (fg.via_diameter, fg.via_drill)
        via = pcbnew.PCB_VIA(b)
        via.SetPosition(vec(p))
        via.SetFrontWidth(round(diameter * 1e6))
        via.SetDrill(round(drill * 1e6))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(code)
        return via

    def via_fits(r, p, code, size=None):
        """Copper, keepout and drill checks of one via (default size unless ``size``)."""
        diameter, drill = size or (fg.via_diameter, fg.via_drill)
        via = make_via(p, code, size)
        for la in copper_layers:
            shape = via.GetEffectiveShape(la)
            found = set()
            for x in range(math.floor(p[0] - 0.8), math.floor(p[0] + 0.8) + 1):
                for y in range(math.floor(p[1] - 0.8), math.floor(p[1] + 0.8) + 1):
                    found.update(buckets[la, x, y])
            for i in found:
                other, gap, n, identity, _, smd = obstacles[i]
                # Legacy: no via within 0.05 mm of any SMD pad. Profile: 5A 0.127
                # unless a qualified 5B filled in-pad via of the pad's own net.
                if smd is not None and smd_keepout_violated(
                    fg, smd, other, shape, r.net, p, diameter, drill, la
                ):
                    return False
                if n != r.net and other.Collide(
                    shape, round(max(gap, r.clearance if n is not None else gap) * 1e6)
                ):
                    return False
            for zone in via_zones:
                if zone.IsOnLayer(la) and zone.Outline().Collide(shape, 1000):
                    return False
        hole = via.GetEffectiveHoleShape()
        for other, bb, t, gap in drilled_gaps:
            if (
                bb.GetLeft() / 1e6 - hole_reach <= p[0] <= bb.GetRight() / 1e6 + hole_reach
                and bb.GetTop() / 1e6 - hole_reach <= p[1] <= bb.GetBottom() / 1e6 + hole_reach
                and other.Collide(hole, gap)
            ):
                return False
        return True

    def via_clear(r, p):
        key = (r.net, p)
        if key in via_cache:
            return via_cache[key]
        via_cache[key] = False
        if not (
            box.GetLeft() / 1e6 + via_margin <= p[0] <= box.GetRight() / 1e6 - via_margin
            and box.GetTop() / 1e6 + via_margin <= p[1] <= box.GetBottom() / 1e6 - via_margin
        ):
            return False
        reused = next(
            (
                t
                for position, t in existing_vias_by_net.get(r.net, ())
                if math.dist(p, position) < 1e-8
            ),
            None,
        )
        if reused:
            via_cache[key] = True
            return True
        if not reference_guard.via_clear(r.net, p, fg.via_diameter):
            return False
        code = net_codes[r.net]
        if not via_fits(r, p, code):
            # Profile only: inside a same-net SMD pad the default via is refused;
            # a 5B filled in-pad class via (0.20/0.35, else 0.20/0.45) may fit.
            size = in_pad_size(fg, smd_pads, r.net, p)
            if size is None or not via_fits(r, p, code, size):
                return False
            in_pad_vias[in_pad_key(r.net, p)] = size
        via_cache[key] = True
        return True

    terminal_cache = {}

    def terminal_layers(r, p):
        key = (r.net, tuple(p))
        if key in terminal_cache:
            return terminal_cache[key]
        available = set(anchor_layers_map.get((r.net, tuple(p)), {0}))
        for t in pads + remaining:
            if t.GetNetname() != r.net:
                continue
            if not isinstance(t, (pcbnew.PAD, pcbnew.PCB_VIA)):
                continue
            if isinstance(t, pcbnew.PAD) and t.GetAttribute() != pcbnew.PAD_ATTRIB_PTH:
                continue
            for index, la in enumerate(route_layers):
                if t.IsOnLayer(la) and t.GetEffectiveShape(la).Collide(
                    pcbnew.SHAPE_CIRCLE(vec(p), 1), 0
                ):
                    available.add(index)
        terminal_cache[key] = sorted(available)
        return terminal_cache[key]

    args.out_dir.mkdir(parents=True)
    baseline = args.out_dir / "baseline.kicad_pcb"
    shutil.copyfile(args.board, baseline)
    shutil.copyfile(args.board.with_suffix(".kicad_pro"), baseline.with_suffix(".kicad_pro"))
    table = args.board.parent / "fp-lib-table"
    if table.exists():
        (args.out_dir / "fp-lib-table").write_text(
            table.read_text().replace("${KIPRJMOD}", str(args.board.parent.resolve()))
        )
    fixture = dict(
        source=str(args.board.resolve()),
        sha256=hashlib.sha256(args.board.read_bytes()).hexdigest(),
        bounds=args.bounds,
        layer="F.Cu",
        layers=args.layers,
        relocate_vias=args.relocate_vias,
        source_via_windows=via_windows,
        joint=args.joint,
        portal_joint=args.portal_joint,
        preserve_copper=args.preserve_copper,
        redundant_restorations=redundant_restorations,
        ground_leaf=args.ground_leaf,
        max_seconds=args.max_seconds,
        requests=[
            dict(
                **asdict(r),
                access_layers={str(p): terminal_layers(r, p) for p in r.sources + r.targets},
            )
            for r in requests
        ],
        chains=chains,
        retained_components=retained_components,
        pitch=args.pitch,
        max_orders=args.max_orders,
        max_expansions=args.max_expansions,
        source_pad=args.source_pad,
        target_pad=args.target_pad,
        nets=args.net,
    )
    (args.out_dir / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")
    print(
        "Regional requests:",
        len(requests),
        "reopened segments:",
        len(selected),
        flush=True,
    )
    from pnr.live import emit as live_emit

    if args.portal_joint:
        live_emit(
            "candidate_start",
            board=baseline,
            data=dict(phase="coordinated-portals", provisional=True),
        )
    if args.layers:

        def record_search_event(event):
            line = json.dumps(event)
            with (args.out_dir / "search-events.jsonl").open("a") as stream:
                stream.write(line + "\n")
            print(line, flush=True)
            if args.portal_joint:
                live_emit(
                    "search_progress",
                    data=dict(
                        phase="coordinated-portals",
                        provisional=True,
                        **{
                            k: v
                            for k, v in event.items()
                            if k not in ("partial_paths", "ports", "choices")
                        },
                    ),
                )
                for request_name, path in event.get("partial_paths", {}).items():
                    request = next(r for r in requests if r.name == request_name)
                    tracks = [
                        (
                            request.net,
                            b.GetLayerName(route_layers[pa[2]]),
                            pa[:2],
                            pb[:2],
                            request.width,
                        )
                        for pa, pb in zip(path, path[1:])
                        if pa[2] == pb[2]
                    ]
                    live_emit(
                        "signal_net_added",
                        data=dict(
                            net=request_name,
                            phase="coordinated-portals",
                            provisional=True,
                            tracks=tracks,
                        ),
                    )

        solver = (
            solve_portal_region
            if args.portal_joint
            else solve_joint_region if args.joint else solve_layered_region
        )
        result = solver(
            requests,
            args.bounds,
            clear_layer,
            via_clear,
            pitch=args.pitch,
            max_orders=args.max_orders,
            max_expansions=args.max_expansions,
            layers=len(route_layers),
            terminal_layers=terminal_layers,
            on_event=record_search_event,
            max_seconds=args.max_seconds,
            first_via_allowed=lambda r, p: r.net not in via_windows
            or (
                via_windows[r.net][0] <= p[0] <= via_windows[r.net][2]
                and via_windows[r.net][1] <= p[1] <= via_windows[r.net][3]
            ),
        )
    else:
        result = solve_region(
            requests,
            args.bounds,
            clear,
            pitch=args.pitch,
            max_orders=args.max_orders,
            max_expansions=args.max_expansions,
        )
    report = asdict(result)
    report["via_access"] = {
        n: dict(
            checked=sum(k[0] == n for k in via_cache),
            legal=sum(v for k, v in via_cache.items() if k[0] == n),
        )
        for n in args.net
    }
    report["static_blockers"] = dict(sorted(static_hits.items(), key=lambda kv: -kv[1])[:30])
    report["accepted"] = False
    if result.status == "routed":
        byname = {r.name: r for r in requests}
        added_vias = set()
        for name, path in result.paths.items():
            r = byname[name]
            code = next(p.GetNetCode() for p in pads if p.GetNetname() == r.net)
            path = path if args.layers else [(*p, 0) for p in path]
            for a, z in zip(path, path[1:]):
                if a == z:
                    continue
                if a[2] != z[2]:
                    key = (r.net, a[0], a[1])
                    if key not in added_vias and not any(
                        math.dist(a[:2], position) < 1e-8
                        for position, t in existing_vias_by_net.get(r.net, ())
                    ):
                        size = in_pad_vias.get(in_pad_key(r.net, a[:2]))
                        via = make_via(a[:2], code, size)
                        if size:
                            style_in_pad_via(fg, via)
                        b.Add(via)
                        added_vias.add(key)
                    continue
                t = pcbnew.PCB_TRACK(b)
                t.SetStart(vec(a[:2]))
                t.SetEnd(vec(z[:2]))
                t.SetLayer(route_layers[a[2]])
                t.SetNetCode(code)
                t.SetWidth(round(r.width * 1e6))
                b.Add(t)
        report["added_vias"] = len(added_vias)
        report["added_in_pad_vias"] = sum(
            in_pad_key(n, (x, y)) in in_pad_vias for n, x, y in added_vias
        )
        b.BuildConnectivity()
        entry_ok = True
        if entry_rules is not None:
            from pnr.pad_entry import repair_changed_entries

            report.update(repair_changed_entries(b, entry_rules, before_entries))
            entry_ok = not report["lost_pad_entries"] and not report["new_bad_entries"]
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        preserved = preserves_connections(before_connections, pad_partition(b))
        output = args.out_dir / "candidate.kicad_pcb"
        pcbnew.SaveBoard(str(output), b)
        shutil.copyfile(baseline.with_suffix(".kicad_pro"), output.with_suffix(".kicad_pro"))

        def drc(path):
            from pnr.native_drc import run_drc

            return run_drc(args.kicad_cli, path, path.with_suffix(".drc.json"))

        before, after = drc(baseline), drc(output)
        # A successful rip-up/restoration can strand an old signal via.
        # Only newly native-dangling vias are proposed, under source protection,
        # exact layer contacts and a fresh complete transaction quality gate.
        if (
            args.rules is not None
            and preserved
            and entry_ok
            and not acceptable(before, after)
            and len(after["unconnected_items"]) < len(before["unconnected_items"])
            and any(v["type"] == "via_dangling" for v in after["violations"])
        ):
            cleanup = args.out_dir / "transaction-cleanup"
            cleanup_cmd = [
                sys.executable,
                "-m",
                "pnr.transaction_cleanup",
                str(output),
                "--baseline",
                str(baseline),
                "--rules",
                str(args.rules),
                "--out-dir",
                str(cleanup),
                "--kicad-cli",
                args.kicad_cli,
                "--kicad-python",
                sys.executable,
            ]
            for net_name in args.net:
                cleanup_cmd += ["--net", net_name]
            for source_file in args.annotation_source:
                cleanup_cmd += ["--annotation-source", str(source_file)]
            # The cleanup runs bounded workers in sequence: PNR_PHASE_TIMEOUT
            # (pnr.proc) bounds it when this tool runs on its own. Under the native
            # loop and the repair adapters, the deadline of this whole worker
            # (PNR_WORKER_TIMEOUT or pnr.proc.worker_timeout) is shorter and binds
            # first. The cleanup stays in this worker's process group.
            from pnr.proc import phase_timeout, run_status

            with (args.out_dir / "transaction-cleanup.log").open("w") as log:
                cleanup_code, cleanup_timed_out = run_status(
                    cleanup_cmd,
                    timeout=phase_timeout(),
                    session=False,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            report["cleanup_returncode"] = cleanup_code
            if cleanup_timed_out:
                report["cleanup_timed_out"] = True
            if cleanup_code == 0 and (cleanup / "result.json").exists():
                cleanup_report = json.loads((cleanup / "result.json").read_text())
                report["transaction_cleanup"] = cleanup_report
                if cleanup_report.get("accepted") and cleanup_report.get("inputs_unchanged"):
                    shutil.copyfile(cleanup / "candidate.kicad_pcb", output)
                    after = drc(output)
        report.update(
            accepted=preserved and entry_ok and acceptable(before, after),
            preserved_pad_connectivity=preserved,
            before_opens=len(before["unconnected_items"]),
            after_opens=len(after["unconnected_items"]),
        )
    (args.out_dir / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"attempts", "paths", "static_blockers"}}
        ),
        flush=True,
    )


if __name__ == "__main__":
    from pnr.profile import run

    run("keyhole-region", main)
