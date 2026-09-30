"""Fine-pitch signal escape reservation ahead of the early power/plane phases.

Opt-in from native_loop (PNR_FANOUT_RESERVE=1). Every unrouted perimeter signal
pad of a package whose row pitch is <= --max-pitch gets a pad-aligned stub to
the first legal through-via site on a straight or 45-degree fan-out path, else
to a landing just outside the pad ring. The native Oracle checks each stub and
via against actual copper, keepouts, holes, edges and pair references. Escapes
are chosen jointly (bounded branch and bound over pairwise conflicts) and keep
the straight lanes of the package's unrouted power/pair pads free. They are
written as named rule areas (no tracks, no vias; pours, pads and footprints
allowed), so phases 03-05 route around them; --release deletes exactly those
areas. No copper is added here: native DRC and the loop's gate decide.
"""

import argparse, json, math, time
from collections import Counter
from pathlib import Path
from pnr.route.detail.regional import segment_distance

PREFIX = "PNR fanout:"
STEP = 0.05  # sample spacing along a candidate stub (mm)
MARGIN = 0.002  # legality/keepout margin beyond the reserved clearance (mm)
TURNS = (0.1, 0.25, 0.4)  # 45-degree fan-out turn depths beyond the land edge (mm)
REACH = 1.6  # longest stub beyond the land edge (mm)
VIAS = 3  # via sites kept per path: the first legal one plus staggers
LANDING_COST = 1.0  # a surface-only landing costs like 1 mm of extra via stub
RING_DEPTH = 0.05  # a perimeter land's outer edge lies at most this far inside the pad ring (mm)
TOL = 2e-3  # geometric tolerance: twice land()'s 1 um outline error (mm)


def unit(v):
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def ahead(a, v, t):
    return (a[0] + v[0] * t, a[1] + v[1] * t)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def row_pitch(lands):
    """Modal (then smallest) 0.01 mm-rounded distance to the nearest congruent land.

    lands=[(centre,(w,h))]; congruent means equal size in either orientation.
    """
    gaps = Counter()
    for i, (p, size) in enumerate(lands):
        near = [
            math.dist(p, q)
            for j, (q, other) in enumerate(lands)
            if j != i and all(abs(a - b) < 0.011 for a, b in zip(sorted(size), sorted(other)))
        ]
        if near:
            gaps[round(min(near), 2)] += 1
    return min(gaps, key=lambda gap: (-gaps[gap], gap)) if gaps else math.inf


def outward(center, axis, half_long, half_short, reference):
    """Pad-aligned outward unit vector; square lands use the snapped radial."""
    d = (center[0] - reference[0], center[1] - reference[1])
    if half_long >= 1.15 * half_short:
        s = dot(d, axis)
        return None if abs(s) < 1e-6 else (axis if s > 0 else (-axis[0], -axis[1]))
    if math.hypot(*d) < 1e-6:
        return None
    a = round(math.atan2(d[1], d[0]) / (math.pi / 4)) * math.pi / 4
    return (math.cos(a), math.sin(a))


def extent(u, axis, half_long, half_short):
    """Half extent of a land along u, and across it."""
    n = (-u[1], u[0])
    short = (-axis[1], axis[0])
    return (
        abs(dot(u, axis)) * half_long + abs(dot(u, short)) * half_short,
        abs(dot(n, axis)) * half_long + abs(dot(n, short)) * half_short,
    )


def exit_distance(p, u, box):
    """Distance along u from p to where the ray leaves box (0 outside)."""
    t = math.inf
    for i, (lo, hi) in enumerate(((box[0], box[2]), (box[1], box[3]))):
        if u[i] > 1e-9:
            t = min(t, (hi - p[i]) / u[i])
        elif u[i] < -1e-9:
            t = min(t, (lo - p[i]) / u[i])
    return max(0.0, t)


def on_ring(c, u, h, ring):
    """The land's outer edge (h along u from c) is on the pad ring, within TOL.

    Recessed outer-row lands (e.g. VQFN-HR side lands) sit exactly RING_DEPTH
    inside, so an exact comparison would flip with float rounding of the pose.
    """
    return exit_distance(ahead(c, u, h), u, ring) <= RING_DEPTH + TOL


def lane_clear(lanes, net, a, z, radius):
    """A stub (or via disc) stays clear of other nets' straight pad lanes."""
    return all(
        l["net"] == net or segment_distance(a, z, l["a"], l["z"]) >= radius + l["half"]
        for l in lanes
    )


def clash(a, b, g):
    """Two planned escapes would violate clearance (same net: via hole spacing only)."""
    holes = g["drill"] + g["hole"] + 2 * MARGIN
    if a["net"] == b["net"]:
        return bool(a["via"] and b["via"] and math.dist(a["via"], b["via"]) < holes)
    r = g["via"] + g["clearance"] + 2 * MARGIN
    if (
        a["box"][0] > b["box"][2] + r
        or b["box"][0] > a["box"][2] + r
        or a["box"][1] > b["box"][3] + r
        or b["box"][1] > a["box"][3] + r
    ):
        return False
    c = g["clearance"] + 2 * MARGIN
    sa, sb = list(zip(a["points"], a["points"][1:])), list(zip(b["points"], b["points"][1:]))
    if a["layer"] == b["layer"] and any(
        segment_distance(p, q, s, t) < (a["width"] + b["width"]) / 2 + c
        for p, q in sa
        for s, t in sb
    ):
        return True
    for x, segments in ((a, sb), (b, sa)):
        other = b if x is a else a
        if x["via"] and any(
            segment_distance(x["via"], x["via"], s, t) < (g["via"] + other["width"]) / 2 + c
            for s, t in segments
        ):
            return True
    return bool(
        a["via"]
        and b["via"]
        and math.dist(a["via"], b["via"]) < max(g["via"] + g["clearance"] + 2 * MARGIN, holes)
    )


def choose(options, g, node_limit=50000):
    """Most reserved pads, then least total cost, per conflict cluster.

    options maps pad label -> candidate list (cost ascending). Depth-first branch
    and bound, fewest-candidate pads first, stops at node_limit per cluster and
    keeps its best complete assignment (the first leaf is the greedy one).
    """
    labels = sorted(l for l in options if options[l])
    conflict = {(l, i): set() for l in labels for i in range(len(options[l]))}
    linked = {l: set() for l in labels}
    box = {
        l: [f(o["box"][k] for o in options[l]) for k, f in enumerate((min, min, max, max))]
        for l in labels
    }
    r = g["via"] + g["clearance"] + 2 * MARGIN
    for x, l in enumerate(labels):
        for m in labels[x + 1 :]:
            if (
                box[l][0] > box[m][2] + r
                or box[m][0] > box[l][2] + r
                or box[l][1] > box[m][3] + r
                or box[m][1] > box[l][3] + r
            ):
                continue
            for i, a in enumerate(options[l]):
                for j, b in enumerate(options[m]):
                    if clash(a, b, g):
                        conflict[l, i].add((m, j))
                        conflict[m, j].add((l, i))
                        linked[l].add(m)
                        linked[m].add(l)
    chosen = {}
    stats = dict(clusters=0, nodes=0, truncated=0)
    seen = set()
    for start in labels:
        if start in seen:
            continue
        cluster = []
        todo = [start]
        seen.add(start)
        while todo:
            l = todo.pop()
            cluster.append(l)
            for m in sorted(linked[l] - seen):
                seen.add(m)
                todo.append(m)
        order = sorted(cluster, key=lambda l: (len(options[l]), l))
        floor = [0.0] * (len(order) + 1)
        for k in range(len(order) - 1, -1, -1):
            floor[k] = floor[k + 1] + options[order[k]][0]["cost"]
        best = [-1, math.inf, {}]
        current = {}
        nodes = [0]

        def search(k, count, cost):
            nodes[0] += 1
            if nodes[0] > node_limit:
                return
            left = len(order) - k
            if count + left < best[0] or (
                count + left == best[0] and cost + floor[k] >= best[1] - 1e-9
            ):
                return
            if k == len(order):
                best[:] = [count, cost, dict(current)]
                return
            label = order[k]
            for i, option in enumerate(options[label]):
                if any(key in conflict[label, i] for key in current.items()):
                    continue
                current[label] = i
                search(k + 1, count + 1, cost + option["cost"])
                del current[label]
            search(k + 1, count, cost)

        search(0, 0, 0.0)
        stats["clusters"] += 1
        stats["nodes"] += min(nodes[0], node_limit)
        stats["truncated"] += nodes[0] > node_limit
        chosen.update({l: options[l][i] for l, i in best[2].items()})
    return chosen, stats


def land(pad, layer):
    """Actual land (custom copper by outline): centre, long axis, half sizes."""
    import pcbnew as k

    poly = k.SHAPE_POLY_SET()
    pad.TransformShapeToPolygon(poly, layer, 0, 1000, k.ERROR_INSIDE)
    points = [
        (poly.COutline(i).CPoint(j).x / 1e6, poly.COutline(i).CPoint(j).y / 1e6)
        for i in range(poly.OutlineCount())
        for j in range(poly.COutline(i).PointCount())
    ]
    best = None
    for angle in sorted(
        {
            round(pad.GetOrientation().AsDegrees() % 180, 6),
            round(-pad.GetOrientation().AsDegrees() % 180, 6),
        }
    ):
        r = math.radians(angle)
        ax = (math.cos(r), math.sin(r))
        ay = (-ax[1], ax[0])
        u = [dot(p, ax) for p in points]
        v = [dot(p, ay) for p in points]
        box = (
            (min(u) + max(u)) / 2,
            (min(v) + max(v)) / 2,
            (max(u) - min(u)) / 2,
            (max(v) - min(v)) / 2,
        )
        if best is None or box[2] * box[3] < best[2][2] * best[2][3] - 1e-12:
            best = (ax, ay, box)
    ax, ay, (cu, cv, hu, hv) = best
    center = (ax[0] * cu + ay[0] * cv, ax[1] * cu + ay[1] * cv)
    return (center, ax, hu, hv) if hu >= hv else (center, ay, hv, hu)


def zone_polygons(board, option, clearance):
    """Keepout outlines: the stub on its layer, the via disc on every copper layer."""
    import pcbnew as k
    from pnr.native_electrical import vec

    gap = round((clearance + MARGIN) * 1e6)
    stub = k.SHAPE_POLY_SET()
    result = []
    for a, z in zip(option["points"], option["points"][1:]):
        t = k.PCB_TRACK(board)
        t.SetLayer(option["layer"])
        t.SetStart(vec(a))
        t.SetEnd(vec(z))
        t.SetWidth(round(option["width"] * 1e6))
        t.TransformShapeToPolygon(stub, option["layer"], gap, 1000, k.ERROR_INSIDE)
    stub.Simplify()
    result.append((stub, [option["layer"]]))
    if option["via"]:
        v = k.PCB_VIA(board)
        v.SetPosition(vec(option["via"]))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        v.SetFrontWidth(round(option["via_diameter"] * 1e6))
        v.SetDrill(round(option["via_drill"] * 1e6))
        disc = k.SHAPE_POLY_SET()
        v.TransformShapeToPolygon(disc, k.F_Cu, gap, 1000, k.ERROR_INSIDE)
        result.append((disc, list(board.GetEnabledLayers().CuStack())))
    return result


def add_rule_area(board, name, poly, layers):
    import pcbnew as k

    added = []
    for i in range(poly.OutlineCount()):
        z = k.ZONE(board)
        z.SetIsRuleArea(True)
        z.SetZoneName(name)
        if len(layers) == 1:
            z.SetLayer(layers[0])
        else:
            z.SetLayerSet(k.LSET.AllCuMask(board.GetCopperLayerCount()))
        z.SetDoNotAllowTracks(True)
        z.SetDoNotAllowVias(True)
        z.SetDoNotAllowZoneFills(False)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        outline = z.Outline()
        outline.NewOutline()
        chain = poly.COutline(i)
        for j in range(chain.PointCount()):
            p = chain.CPoint(j)
            outline.Append(p.x, p.y)
        board.Add(z)
        added.append(z)
    return added


def reserve(board, rules, max_pitch=0.65, node_limit=50000):
    """Plan jointly legal escapes and add their rule areas to board."""
    import pcbnew as k
    from collections import defaultdict
    from pnr.native_electrical import Oracle, connected_items
    from pnr.electrical import net_policy
    from pnr.pad_entry import required_width

    started = time.monotonic()
    fab = rules.get("fab", {})
    clearance = max(
        [fab.get("clearance_mm", 0.15)]
        + [c["clearance_mm"] for c in rules.get("net_classes", []) if c.get("clearance_mm")]
    )
    g = dict(
        clearance=clearance,
        via=fab.get("via_diameter_mm", 0.6),
        drill=fab.get("via_drill_mm", 0.3),
        hole=fab.get("hole_to_hole_mm", fab.get("hole_clearance_mm", 0.2)),
    )
    board.BuildConnectivity()
    oracle = Oracle(board, rules)
    owners = defaultdict(set)
    for f in board.GetFootprints():
        for p in f.Pads():
            if p.GetNetname():
                owners[p.GetNetname()].add(f.GetReference())
    routed = lambda p: any(t.GetClass() != "PAD" for t in connected_items(board, p))
    report = dict(max_pitch_mm=max_pitch, clearance_mm=clearance, packages={}, pads={}, lanes=[])
    pads = []
    lanes = []
    for f in board.GetFootprints():
        smd = []
        for p in f.Pads():
            layer = k.F_Cu if p.IsOnLayer(k.F_Cu) else k.B_Cu if p.IsOnLayer(k.B_Cu) else None
            if p.GetAttribute() == k.PAD_ATTRIB_SMD and layer is not None:
                smd.append((p, layer, land(p, layer)))
        if len(smd) < 2:
            continue
        pitch = row_pitch([(c, (2 * hl, 2 * hs)) for _, _, (c, _, hl, hs) in smd])
        if pitch > max_pitch + 1e-6:
            continue
        boxes = [p.GetBoundingBox() for p in f.Pads()]
        reference = (
            (min(b.GetLeft() for b in boxes) + max(b.GetRight() for b in boxes)) / 2e6,
            (min(b.GetTop() for b in boxes) + max(b.GetBottom() for b in boxes)) / 2e6,
        )
        corners = []
        for _, _, (c, ax, hl, hs) in smd:
            ex, ey = abs(ax[0]) * hl + abs(ax[1]) * hs, abs(ax[1]) * hl + abs(ax[0]) * hs
            corners += [(c[0] - ex, c[1] - ey), (c[0] + ex, c[1] + ey)]
        ring = (
            min(p[0] for p in corners),
            min(p[1] for p in corners),
            max(p[0] for p in corners),
            max(p[1] for p in corners),
        )
        summary = report["packages"][f.GetReference()] = dict(
            pitch_mm=round(pitch, 4), signal_pads=0, eligible=0, reserved=0, via=0, landing=0
        )
        for p, layer, (c, ax, hl, hs) in smd:
            net = p.GetNetname()
            label = f.GetReference() + "." + p.GetNumber()
            if not net:
                continue
            mode = net_policy(net, rules)["mode"]
            u = outward(c, ax, hl, hs, reference)
            if u is None:
                if mode == "signal":
                    summary["signal_pads"] += 1
                    report["pads"][label] = dict(net=net, status="interior")
                continue
            h, half = extent(u, ax, hl, hs)
            perimeter = on_ring(c, u, h, ring)
            if mode in ("power", "pair") and perimeter and not routed(p):
                lanes.append(
                    dict(
                        pad=label, net=net, a=c, z=ahead(c, u, h + g["via"] + clearance), half=half
                    )
                )
            if mode != "signal":
                continue
            summary["signal_pads"] += 1
            status = (
                "not_perimeter"
                if not perimeter
                else (
                    "package_internal"
                    if owners[net] == {f.GetReference()}
                    else "routed" if routed(p) else None
                )
            )
            report["pads"][label] = dict(net=net, status=status or "unreserved")
            if status is None:
                summary["eligible"] += 1
                pads.append(
                    dict(
                        label=label,
                        ref=f.GetReference(),
                        net=net,
                        layer=layer,
                        center=c,
                        u=u,
                        h=h,
                        width=required_width(p, rules),
                    )
                )
    report["lanes"] = [dict(pad=l["pad"], net=l["net"]) for l in lanes]
    options = {}
    for pad in pads:
        options[pad["label"]] = candidates(oracle, pad, lanes, g)
        report["pads"][pad["label"]]["candidates"] = len(options[pad["label"]])
    chosen, stats = choose(options, g, node_limit)
    report["search"] = stats
    # Re-check the joint choice with native shapes: earlier escapes are reserved
    # copper for later ones, and keepouts must not touch any existing track or
    # via. A rejected pad falls back to its next option clashing with no choice.
    check = oracle.fork()
    items = list(board.GetTracks())
    areas = []
    rejected = Counter()

    def admissible(o):
        if not all(
            check.clear(o["net"], o["layer"], a, z, o["width"] + 2 * MARGIN)
            for a, z in zip(o["points"], o["points"][1:])
        ):
            rejected["stub"] += 1
            return None
        if o["via"] and not check.via(o["net"], o["via"], g["via"] + 2 * MARGIN, g["drill"]):
            rejected["via"] += 1
            return None
        polygons = zone_polygons(board, o, clearance)
        if any(
            poly.Collide(t.GetEffectiveShape(la), 0)
            for poly, layers in polygons
            for la in layers
            for t in items
            if t.IsOnLayer(la) and t.GetBoundingBox().Intersects(poly.BBox())
        ):
            rejected["keepout"] += 1
            return None
        return polygons

    for label in sorted(chosen, key=lambda l: (chosen[l]["cost"], l)):
        first = chosen.pop(label)
        tries = [first] + [
            o
            for o in options[label]
            if o is not first and not any(clash(o, x, g) for x in chosen.values())
        ]
        o, polygons = next(
            ((o, polygons) for o in tries for polygons in [admissible(o)] if polygons), (None, None)
        )
        if o is None:
            report["pads"][label]["status"] = "verify_failed"
            continue
        chosen[label] = o
        for a, z in zip(o["points"], o["points"][1:]):
            check.reserve_track(o["net"], o["layer"], a, z, o["width"])
        if o["via"]:
            check.reserve_via(o["net"], o["via"], g["via"], g["drill"])
        areas += [
            (PREFIX + label + (" via" if len(layers) > 1 else ""), poly, layers)
            for poly, layers in polygons
        ]
        summary = report["packages"][label.rsplit(".", 1)[0]]
        summary["reserved"] += 1
        summary["via" if o["via"] else "landing"] += 1
        report["pads"][label].update(
            status="reserved",
            terminal="via" if o["via"] else "landing",
            fallback=o is not first,
            points=o["points"],
            via=o["via"],
            cost=round(o["cost"], 4),
            layer=board.GetLayerName(o["layer"]),
        )
    # Oracle.via reads live board rule areas: add them only after every check.
    added = [z for name, poly, layers in areas for z in add_rule_area(board, name, poly, layers)]
    for label, row in report["pads"].items():
        if row["status"] == "unreserved":
            row["status"] = "no_candidate" if not options.get(label) else "conflict"
    report.update(
        verify_rejected=dict(rejected),
        rule_areas=len(added),
        reserved=sum(r["status"] == "reserved" for r in report["pads"].values()),
        eligible=len(pads),
        seconds=round(time.monotonic() - started, 3),
    )
    return report


def candidates(oracle, pad, lanes, g):
    """Straight and 45-degree fan-out stubs ending at via sites or a ring landing."""
    net, layer, w, c, u, h = (
        pad["net"],
        pad["layer"],
        pad["width"],
        pad["center"],
        pad["u"],
        pad["h"],
    )
    d, drill, clearance = g["via"], g["drill"], g["clearance"]
    near = [
        l
        for l in lanes
        if l["net"] != net
        and segment_distance(c, c, l["a"], l["z"]) < h + REACH + d + clearance + l["half"]
    ]
    free = lambda a, z, radius: lane_clear(near, net, a, z, radius + clearance + MARGIN)
    n = (-u[1], u[0])
    landing = h + w / 2 + clearance
    specs = [([c], u, h + STEP, h + REACH)]
    for s in TURNS:
        for sign in (1, -1):
            specs.append(
                (
                    [c, ahead(c, u, h + s)],
                    unit((u[0] + sign * n[0], u[1] + sign * n[1])),
                    STEP,
                    REACH - s,
                )
            )
    out = []
    for prefix, v, t, limit in specs:
        if len(prefix) > 1 and not (
            oracle.clear(net, layer, prefix[0], prefix[1], w + 2 * MARGIN)
            and free(prefix[0], prefix[1], w / 2)
        ):
            continue
        origin = prefix[-1]
        vias = []
        landed = False
        base = math.dist(*prefix) if len(prefix) > 1 else 0.0
        while t <= limit + 1e-9 and len(vias) < VIAS:
            e = ahead(origin, v, t)
            if not (oracle.clear(net, layer, origin, e, w + 2 * MARGIN) and free(origin, e, w / 2)):
                break
            points = prefix + [e]
            length = base + t - h
            box = (
                min(p[0] for p in points),
                min(p[1] for p in points),
                max(p[0] for p in points),
                max(p[1] for p in points),
            )
            option = dict(
                pad=pad["label"],
                net=net,
                layer=layer,
                width=w,
                points=points,
                box=box,
                via=None,
                via_diameter=d,
                via_drill=drill,
            )
            if (
                (not vias or math.dist(vias[-1], e) >= 0.25 - 1e-9)
                and free(e, e, d / 2)
                and oracle.via(net, e, d + 2 * MARGIN, drill)
            ):
                vias.append(e)
                r = d / 2
                out.append(
                    dict(
                        option,
                        via=e,
                        cost=length,
                        box=(box[0] - r, box[1] - r, box[2] + r, box[3] + r),
                    )
                )
            if not landed and dot((e[0] - c[0], e[1] - c[1]), u) >= landing - 1e-9:
                landed = True
                out.append(dict(option, cost=length + LANDING_COST))
            t += STEP
    return sorted(out, key=lambda o: (o["cost"], o["points"]))


def release(board):
    """Delete exactly the reservation rule areas."""
    zones = [z for z in board.Zones() if z.GetZoneName().startswith(PREFIX)]
    for z in zones:
        board.Delete(z)  # Delete, not Remove: detached zones crash KiCad-python teardown (SIGSEGV)
    return len(zones)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--rules", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--max-pitch", type=float, default=0.65)
    a = ap.parse_args(argv)
    import pcbnew as k

    board = k.LoadBoard(str(a.board))
    started = time.monotonic()
    if a.release:
        result = dict(released=release(board))
    else:
        if not a.rules:
            ap.error("reservation requires --rules")
        result = reserve(board, json.loads(a.rules.read_text()), a.max_pitch)
    result["seconds"] = round(time.monotonic() - started, 3)
    k.SaveBoard(str(a.out), board)
    a.report.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("released", "eligible", "reserved", "rule_areas", "seconds")
                if key in result
            }
        )
    )


if __name__ == "__main__":
    main()
