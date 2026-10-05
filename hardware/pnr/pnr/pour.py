"""Outer-layer pours whose pads count as connected after the refill (``pour:``).

    pour:
      - layer: B.Cu        # an outer layer
        net: GND
        stitch: true       # stitch islands to the net's other zones (default true)
        connect: thermal   # the zone's pad connection (solid or thermal, default)
        clearance_mm: 0.2  # default: the fab clearance
        min_width_mm: 0.15 # default: the fab track width

The router (:func:`pads`, ``route_board``) plans no escape and no plane drop for the
net's surface pads on the layer: the pour is their connection. After routing,
``pnr.planes`` (KiCad's Python) draws the pour over the outline where the board has
none (:func:`draw`, priority 0: every other zone of the layer fills first), fills it,
and judges it (:func:`stitch`): each filled island of the pour that holds a pad of the
net but no via or plated hole of it gets a through via where one fits (exact
clearance to every other net's copper on every layer, the via's land inside the
island) and joins another zone of its net there (its plane); an island with no such
site is reported. The board is refilled after any stitch. KiCad's DRC remains the
judge of what is connected.
"""

from __future__ import annotations

import math
from typing import Dict, List, Set, Tuple


def pads(grid, graph, entries) -> Set[Tuple[str, str]]:
    """The (ref, pad) pairs a declared pour joins: surface pads of its net on its
    layer (router side, no KiCad)."""
    from pnr.place.geometry import pad_rects
    from pnr.route.detail.grid import pad_layer

    want = {(e["net"], e["layer"]) for e in entries or []}
    out = set()
    for comp in graph.components:
        for (name, net, _r), pad in zip(pad_rects(comp), comp.pads):
            if pad.through_hole or not net:
                continue
            if (net, grid.layers[pad_layer(grid, comp, pad)]) in want:
                out.add((comp.ref, name))
    return out


def _zone_name(entry) -> str:
    return "pour %s %s" % (entry["net"], entry["layer"])


def draw(board, rules) -> List:
    """Draw each declared pour over the board outline where none is drawn yet (by its
    name); returns the zones drawn."""
    import pcbnew

    from pnr.writeback import _fab, _nm, copper_layer, outline_bounds

    fab = _fab(rules)
    have = {z.GetZoneName() for z in board.Zones()}
    box = outline_bounds(board)
    corners = [
        (box.GetLeft(), box.GetTop()),
        (box.GetRight(), box.GetTop()),
        (box.GetRight(), box.GetBottom()),
        (box.GetLeft(), box.GetBottom()),
    ]
    made = []
    for entry in (rules or {}).get("pours") or []:
        if _zone_name(entry) in have:
            continue
        net = board.FindNet(entry["net"])
        if net is None or net.GetNetCode() <= 0:
            continue
        z = pcbnew.ZONE(board)
        z.SetLayer(copper_layer(board, entry["layer"]))
        z.SetNetCode(net.GetNetCode())
        z.SetZoneName(_zone_name(entry))
        z.SetLocalClearance(_nm(entry.get("clearance_mm", fab["clearance_mm"])))
        z.SetMinThickness(_nm(entry.get("min_width_mm", fab["track_width_mm"])))
        z.SetAssignedPriority(0)
        z.SetPadConnection(
            pcbnew.ZONE_CONNECTION_FULL
            if entry.get("connect") == "solid"
            else pcbnew.ZONE_CONNECTION_THERMAL
        )
        outline = z.Outline()
        outline.NewOutline()
        for x, y in corners:
            outline.Append(pcbnew.VECTOR2I(int(x), int(y)))
        board.Add(z)
        made.append(z)
    return made


def _island(polys, k):
    import pcbnew

    out = pcbnew.SHAPE_POLY_SET()
    out.AddOutline(polys.Outline(k))
    for h in range(polys.HoleCount(k)):
        out.AddHole(polys.CHole(k, h))
    return out


def _site(island, centre, radius, step, ok):
    """The candidate nearest ``centre`` inside ``island`` (nm), at least ``radius``
    from its edge, that ``ok`` admits; None without one."""
    import pcbnew

    box = island.BBox()
    found = []
    x = box.GetLeft() + radius
    while x <= box.GetRight() - radius:
        y = box.GetTop() + radius
        while y <= box.GetBottom() - radius:
            p = pcbnew.VECTOR2I(int(x), int(y))
            # The via's land inside the island: its centre and 16 points of its rim.
            rim = [
                pcbnew.VECTOR2I(
                    int(x + radius * math.cos(a * math.pi / 8)),
                    int(y + radius * math.sin(a * math.pi / 8)),
                )
                for a in range(16)
            ]
            if island.Contains(p) and all(island.Contains(q) for q in rim):
                found.append((math.dist((x, y), centre), int(x), int(y)))
            y += step
        x += step
    for _d, x, y in sorted(found):
        if ok((x, y)):
            return (x, y)
    return None


def stitch(board, rules, step_mm: float = 0.1) -> Dict[str, dict]:
    """Stitch each filled island of a declared pour (``stitch: true``) that holds a
    pad of its net but no via or plated hole of it to another zone of the net (see
    the module doc). The board must be filled. Returns ``{"NET LAYER": {islands,
    stitched: [[x_mm, y_mm]], unstitched: [[x_mm, y_mm]]}}`` (board nm / 1e6)."""
    import pcbnew

    from pnr.writeback import _clear_segment, _collect_obstacles, _fab, _nm, copper_layer

    fab = _fab(rules)
    via_d = _nm(fab.get("via_diameter_mm", 0.6))
    via_h = _nm(fab.get("via_drill_mm", 0.3))
    report: Dict[str, dict] = {}
    entries = [e for e in (rules or {}).get("pours") or [] if e.get("stitch", True)]
    if not entries:
        return report
    obstacles = _collect_obstacles(board)
    for entry in entries:
        lid = copper_layer(board, entry["layer"])
        zones = [
            z
            for z in board.Zones()
            if not z.GetIsRuleArea() and z.GetZoneName() == _zone_name(entry) and z.IsOnLayer(lid)
        ]
        if not zones:
            continue
        code = zones[0].GetNetCode()
        clearance = _nm(entry.get("clearance_mm", fab["clearance_mm"]))
        others = [
            z
            for z in board.Zones()
            if not z.GetIsRuleArea() and z.GetNetCode() == code and not z.IsOnLayer(lid)
        ]
        through = []  # the net's vias and plated holes: they reach every layer
        for t in board.GetTracks():
            if t.GetClass() == "PCB_VIA" and t.GetNetCode() == code:
                through.append(t.GetPosition())
        pad_points = []
        for fp in board.GetFootprints():
            for p in fp.Pads():
                if p.GetNetCode() != code:
                    continue
                if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                    through.append(p.GetPosition())
                elif p.IsOnLayer(lid):
                    pad_points.append(p.GetPosition())
        row = report.setdefault(
            "%s %s" % (entry["net"], entry["layer"]), dict(islands=0, stitched=[], unstitched=[])
        )

        def joins(xy):
            q = pcbnew.VECTOR2I(*xy)
            for z in others:
                for la in z.GetLayerSet().Seq():
                    if z.GetFilledPolysList(la).Contains(q):
                        return True
            return False

        for zone in zones:
            polys = zone.GetFilledPolysList(lid)
            for k in range(polys.OutlineCount()):
                row["islands"] += 1
                island = _island(polys, k)
                if any(island.Contains(q) for q in through):
                    continue
                mine = [q for q in pad_points if island.Contains(q)]
                if not mine:
                    continue
                cx = sum(q.x for q in mine) / len(mine)
                cy = sum(q.y for q in mine) / len(mine)

                def ok(xy):
                    return joins(xy) and _clear_segment(
                        xy, xy, via_d / 2 + clearance, code, obstacles
                    )

                site = _site(island, (cx, cy), via_d / 2, _nm(step_mm), ok)
                at = [round(cx / 1e6, 4), round(cy / 1e6, 4)]
                if site is None:
                    row["unstitched"].append(at)
                    continue
                via = pcbnew.PCB_VIA(board)
                via.SetPosition(pcbnew.VECTOR2I(*site))
                via.SetWidth(via_d)
                via.SetDrill(via_h)
                via.SetViaType(pcbnew.VIATYPE_THROUGH)
                via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                via.SetNetCode(code)
                board.Add(via)
                obstacles.append((site, site, via_d / 2, code))
                obstacles.append((site, site, via_h / 2, -1))
                through.append(via.GetPosition())
                row["stitched"].append([round(site[0] / 1e6, 4), round(site[1] / 1e6, 4)])
    if any(r["stitched"] for r in report.values()):
        board.BuildConnectivity()
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return report
