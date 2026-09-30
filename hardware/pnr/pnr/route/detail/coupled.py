"""Coupled, single-layer differential routing with checked terminal fanouts.

Route a clearance envelope, offset BOTH traces, then validate their exact
geometry. No independently routed pair legs, silent gap changes or unmatched
vias. Length tuning inserts a bounded 45-degree trombone on the shorter leg;
every candidate is checked against its mate, itself and static obstacles.

src13 (PNR_PAIR_PER_RUN_UNCOUPLED / PNR_PAIR_STUB_MAX_MM in pnr.native_electrical):
solve_pair takes optional per-end fanout budgets (max_uncoupled_head/_tail; unset =
max_uncoupled, unchanged search); uncoupled_runs measures continuous uncoupled
runs along an ordered pair path and branch_lengths the copper length from a pad
to the endpoint path (stub). Nothing calls them unless those flags are set.
"""

import math

from .keyhole import elbows, length, route
from .regional import segment_distance


def offset_path(path, offset):
    if len(path) < 2:
        return []
    dirs = []
    normals = []
    for a, b in zip(path, path[1:]):
        d = math.dist(a, b)
        if d < 1e-9:
            raise ValueError("zero centerline segment")
        u = ((b[0] - a[0]) / d, (b[1] - a[1]) / d)
        dirs.append(u)
        normals.append((-u[1], u[0]))
    out = [(path[0][0] + offset * normals[0][0], path[0][1] + offset * normals[0][1])]
    for i, p in enumerate(path[1:-1], 1):
        a, b = normals[i - 1], normals[i]
        den = 1 + a[0] * b[0] + a[1] * b[1]
        if den < 0.5:
            raise ValueError("sharp/reversing pair bend")
        out.append((p[0] + offset * (a[0] + b[0]) / den, p[1] + offset * (a[1] + b[1]) / den))
    out.append((path[-1][0] + offset * normals[-1][0], path[-1][1] + offset * normals[-1][1]))
    # Offsets cannot consume/reverse a short centerline segment at a bend.
    if any(
        (b[0] - a[0]) * u[0] + (b[1] - a[1]) * u[1] <= 1e-8 for a, b, u in zip(out, out[1:], dirs)
    ):
        raise ValueError("pair bend too short")
    return out


def simplify(path):
    out = []
    for point in path:
        if out and math.dist(point, out[-1]) < 1e-8:
            continue
        while len(out) >= 2:
            a, b = out[-2:]
            u = (b[0] - a[0], b[1] - a[1])
            v = (point[0] - b[0], point[1] - b[1])
            if abs(u[0] * v[1] - u[1] * v[0]) > 1e-8 or u[0] * v[0] + u[1] * v[1] < 0:
                break
            out.pop()
        out.append(point)
    return out


def relaxed_centerlines(path, clear, pitch):
    """Remove grid hooks and bevel sharp corners under the same exact envelope.

    The callback retains endpoint heading/reference constraints. Final offset
    traces, fanouts and tuning are independently revalidated by solve_pair.
    """
    reduced = list(path)
    changed = False
    i = 0
    while i < len(reduced) - 2:
        for j in range(len(reduced) - 1, i + 1, -1):
            if j > i + 1 and clear(reduced[i], reduced[j]):
                reduced = reduced[: i + 1] + reduced[j:]
                changed = True
                break
        i += 1
    output = [reduced] if changed else []
    for distance in (pitch, pitch * 1.5, pitch * 2, pitch * 3):
        points = [reduced[0]]
        for index, point in enumerate(reduced[1:-1], 1):
            previous, following = reduced[index - 1], reduced[index + 1]
            left, right = math.dist(previous, point), math.dist(point, following)
            cut = min(distance, left / 3, right / 3)
            if cut < 1e-9:
                continue
            points.append(tuple(point[j] + (previous[j] - point[j]) * cut / left for j in (0, 1)))
            points.append(tuple(point[j] + (following[j] - point[j]) * cut / right for j in (0, 1)))
        points.append(reduced[-1])
        points = simplify(points)
        if len(points) > 1 and all(clear(a, z) for a, z in zip(points, points[1:])):
            output.append(points)
    return output


def geometry_ok(paths, width, gap, clear):
    for net, path in paths.items():
        if not all(clear(net, a, b, width) for a, b in zip(path, path[1:])):
            return False
        edges = list(zip(path, path[1:]))
        for i, (a, b) in enumerate(edges):
            for c, d in edges[i + 2 :]:
                if segment_distance(a, b, c, d) < width + gap - 1e-6:
                    return False
    ps = list(paths.values())
    return all(
        segment_distance(a, b, c, d) >= width + gap - 1e-6
        for a, b in zip(ps[0], ps[0][1:])
        for c, d in zip(ps[1], ps[1][1:])
    )


def tune(paths, width, gap, skew, clear, offsets=None, max_tuning_length=None):
    """Match full endpoint path lengths, including explicitly supplied lead lengths."""
    offsets = offsets or {}
    nets = list(paths)
    lengths = {n: length(p) + offsets.get(n, 0) for n, p in paths.items()}
    short = min(nets, key=lambda n: lengths[n])
    long = max(nets, key=lambda n: lengths[n])
    delta = lengths[long] - lengths[short]
    if delta <= skew + 1e-7:
        return paths
    # Two 45-degree ramps, flat middle: excess=2*h*(sqrt(2)-1).
    h = max(0.0, delta - skew * 0.95) / (2 * (math.sqrt(2) - 1))
    path = paths[short]
    # A compact local trombone must fit the source uncoupled-length allowance.
    # The former plateau grew with the whole host segment, losing coupling over
    # arbitrarily long distances even for a small endpoint skew correction.
    plateau = width + gap
    tuning_length = 2 * h * math.sqrt(2) + plateau
    if max_tuning_length is not None and tuning_length > max_tuning_length + 1e-7:
        return None
    for i, (a, b) in enumerate(zip(path, path[1:])):
        d = math.dist(a, b)
        if d < 2 * h + plateau + 2 * (width + gap):
            continue
        ux, uy = (b[0] - a[0]) / d, (b[1] - a[1]) / d
        for side in (-1, 1):
            vx, vy = -uy * side, ux * side
            pad = (d - 2 * h - plateau) / 2
            at = lambda x, y: (a[0] + x * ux + y * vx, a[1] + x * uy + y * vy)
            mid = [
                at(pad, 0),
                at(pad + h, h),
                at(pad + h + plateau, h),
                at(pad + 2 * h + plateau, 0),
            ]
            proposed = dict(paths)
            proposed[short] = path[: i + 1] + mid + path[i + 1 :]
            if geometry_ok(proposed, width, gap, clear):
                return proposed
    return None


def trim_path(path, start=0.0, end=0.0):
    """Trim only along the existing centerline; never cut an obstacle corner."""

    def trim(points, distance):
        points = list(points)
        while len(points) >= 2 and distance > 1e-9:
            a, b = points[:2]
            span = math.dist(a, b)
            if distance >= span - 1e-9:
                points.pop(0)
                distance -= span
            else:
                f = distance / span
                points[0] = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
                break
        return points

    if start < 0 or end < 0 or start + end >= length(path) - 1e-8:
        return []
    return list(reversed(trim(list(reversed(trim(path, start))), end)))


def route_directional(a, z, ds, de, bounds, clear, pitch, max_uncoupled, max_expansions):
    """Route from exact straight leads before joining the discrete maze."""
    from .keyhole import Repair

    run = max_uncoupled + 2 * pitch
    ap = tuple(a[i] + ds[i] * run for i in (0, 1))
    zp = tuple(z[i] - de[i] * run for i in (0, 1))
    if clear(a, z):
        return Repair([a, z], "routed", 0)
    if not clear(a, ap) or not clear(zp, z):
        return Repair([], "directional_lead_blocked", 0)
    result = route([ap], [zp], bounds, clear, pitch=pitch, max_expansions=max_expansions)
    if result.status == "routed":
        result.path = simplify([a] + result.path + [z])
    return result


def solve_pair(
    p,
    n,
    terminals,
    bounds,
    clear,
    envelope_clear,
    width,
    gap,
    skew,
    *,
    pitch=0.2,
    max_expansions=20000,
    max_uncoupled=2.0,
    offsets=None,
    max_attempts=32,
    signs=(-1, 1),
    accept_paths=None,
    max_tuning_length=None,
    max_uncoupled_head=None,
    max_uncoupled_tail=None
):
    """Terminals map net -> (exact source, exact target); same-layer geometry.

    envelope_clear checks a center trace of width 2*width+gap against every
    foreign obstacle. Terminal fanout is bounded and exact, not a snap shortcut.
    The adapter must native-check pad connectivity, layer/reference continuity
    and any retained external branches before acceptance.

    ``max_uncoupled_head`` / ``max_uncoupled_tail`` (default: ``max_uncoupled``)
    bound the source and target fanouts separately: each end is its own
    continuous uncoupled run (PNR_PAIR_PER_RUN_UNCOUPLED in the native adapter).
    They set the lead-point candidates and the pad-to-lane fanout bounds only;
    the lane-shape heuristics (length of the straight directional leads and of
    the straight endpoint approach) keep using ``max_uncoupled``, so a larger
    per-end budget never makes the lane search stricter. Unset, both equal
    ``max_uncoupled`` and the search is unchanged.
    """
    head_cap = max_uncoupled if max_uncoupled_head is None else max_uncoupled_head
    tail_cap = max_uncoupled if max_uncoupled_tail is None else max_uncoupled_tail
    if min(width, gap, skew, pitch, max_uncoupled, head_cap, tail_cap) <= 0:
        raise ValueError("positive pair rules required")
    start = tuple((terminals[p][0][i] + terminals[n][0][i]) / 2 for i in (0, 1))
    end = tuple((terminals[p][1][i] + terminals[n][1][i]) / 2 for i in (0, 1))
    # Escape endpoints may not admit the wide envelope. Try cardinal leadouts,
    # but validate each individual pad-to-lane lead below, with bounded length.
    leads = lambda cap: sorted(
        set((0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75))
        | {round(i * 0.1, 10) for i in range(1, 21) if i * 0.1 <= cap + 1e-9}
    )
    head_leads, tail_leads = leads(head_cap), leads(tail_cap)
    options = lambda q, cap, distances: [q] + [
        (q[0] + dx * d, q[1] + dy * d)
        for d in distances
        if d <= cap
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1))
    ]
    attempts = 0
    fanout_debug = []
    failures = {}

    def failed(reason):
        failures[reason] = failures.get(reason, 0) + 1

    candidates = [(start, end, None, None, None)]
    for sign in signs:
        headings = []
        for index in (0, 1):
            pp, nn = terminals[p][index], terminals[n][index]
            d = math.dist(pp, nn)
            if d < 1e-8:
                break
            headings.append((sign * (pp[1] - nn[1]) / d, sign * (nn[0] - pp[0]) / d))
        if len(headings) != 2:
            continue
        ds, de = headings
        for head, tail in sorted(
            ((x, y) for x in head_leads for y in tail_leads), key=lambda xy: sum(xy)
        ):
            a = tuple(start[i] + ds[i] * head for i in (0, 1))
            z = tuple(end[i] - de[i] * tail for i in (0, 1))
            candidates.append((a, z, sign, ds, de))
    # Interleave both polarities at each lead length; a bounded search must
    # not spend all attempts on the first lane orientation.
    candidates = candidates[:1] + sorted(
        candidates[1:],
        key=lambda v: (
            round(math.dist(start, v[0]) + math.dist(end, v[1]), 8),
            math.dist(v[0], v[1]),
        ),
    )
    candidates += [
        (a, z, None, None, None)
        for a, z in sorted(
            (
                (a, z)
                for a in options(start, head_cap, head_leads)
                for z in options(end, tail_cap, tail_leads)
            ),
            key=lambda az: math.dist(start, az[0]) + math.dist(end, az[1]),
        )
    ]
    for a, z, forced_sign, ds, de in candidates:
        if not envelope_clear(a, a, 2 * width + gap) or not envelope_clear(z, z, 2 * width + gap):
            continue
        if forced_sign is not None:
            viable = True
            for index, point, heading in ((0, a, ds), (1, z, de)):
                fanouts = {}
                end_cap = (head_cap, tail_cap)[index]
                for net, side in ((p, 1), (n, -1)):
                    portal = (
                        point[0] - heading[1] * forced_sign * side * (width + gap) / 2,
                        point[1] + heading[0] * forced_sign * side * (width + gap) / 2,
                    )
                    fanouts[net] = [
                        path
                        for path in elbows(terminals[net][index], portal)
                        if length(path) <= end_cap
                        and all(clear(net, x, y, width) for x, y in zip(path, path[1:]))
                    ]
                if not any(
                    geometry_ok({p: pp, n: nn}, width, gap, clear)
                    for pp in fanouts[p]
                    for nn in fanouts[n]
                ):
                    viable = False
                    break
            if not viable:
                failed("portal_fanout")
                continue

        def center_clear(x, y):
            if forced_sign is not None and math.dist(x, y) > 1e-9:

                def aligned(vector, heading):
                    return (
                        vector[0] * heading[0] + vector[1] * heading[1] > 0
                        and abs(vector[0] * heading[1] - vector[1] * heading[0]) < 1e-7
                    )

                for point, heading, entering in ((a, ds, False), (z, de, True)):
                    # Keep a straight approach longer than the lane offset;
                    # endpoint-only heading checks admit microscopic U-turns.
                    if segment_distance(x, y, point, point) < max_uncoupled:
                        outward = tuple(-v if entering else v for v in heading)
                        for q in (x, y):
                            v = (q[0] - point[0], q[1] - point[1])
                            if (
                                abs(v[0] * outward[1] - v[1] * outward[0]) > 1e-7
                                or v[0] * outward[0] + v[1] * outward[1] < -1e-7
                            ):
                                return False
                    if math.dist(x, point) < 1e-8:
                        vector = (
                            (x[0] - y[0], x[1] - y[1]) if entering else (y[0] - x[0], y[1] - x[1])
                        )
                        if not aligned(vector, heading):
                            return False
                    if math.dist(y, point) < 1e-8:
                        vector = (
                            (y[0] - x[0], y[1] - x[1]) if entering else (x[0] - y[0], x[1] - y[1])
                        )
                        if not aligned(vector, heading):
                            return False
            return envelope_clear(x, y, 2 * width + gap)

        if forced_sign is not None:
            routed = route_directional(
                a, z, ds, de, bounds, center_clear, pitch, max_uncoupled, max_expansions
            )
        else:
            routed = route(
                [a], [z], bounds, center_clear, pitch=pitch, max_expansions=max_expansions
            )
        attempts += 1
        if routed.status != "routed":
            failed("centerline_" + routed.status)
            if attempts >= max_attempts:
                break
            # Explicit straight approach candidates can still be viable.
        trial_paths = []
        if forced_sign is not None:
            for head in (1.0, 2.0, 3.0):
                for tail in (1.0, 2.0, 3.0):
                    ap = tuple(a[i] + ds[i] * head for i in (0, 1))
                    zp = tuple(z[i] - de[i] * tail for i in (0, 1))
                    for middle in elbows(ap, zp):
                        path = simplify([a] + middle + [z])
                        if all(
                            bounds[0] <= q[0] <= bounds[2] and bounds[1] <= q[1] <= bounds[3]
                            for q in path
                        ) and all(center_clear(x, y) for x, y in zip(path, path[1:])):
                            trial_paths.append(path)
        if routed.status == "routed":
            trial_paths.append(routed.path)
        smooth_trials = []
        for raw in trial_paths:
            smooth_trials.extend(relaxed_centerlines(raw, center_clear, width + gap))
            smooth_trials.append(raw)
        for raw_path in smooth_trials:
            # Grid lead-in corners can create tiny hooks when offset. Move the
            # coupled portion's ends along its existing envelope and re-check the
            # exact pad fanouts, retaining the source-defined uncoupled length cap.
            trims = sorted(
                ((a, z) for a in (0.0, 0.25, 0.5, 0.75) for z in (0.0, 0.25, 0.5, 0.75)),
                key=lambda az: sum(az),
            )
            for head_trim, tail_trim in trims:
                centerline = trim_path(raw_path, head_trim, tail_trim)
                if len(centerline) < 2:
                    continue
                for sign in (forced_sign,) if forced_sign is not None else signs:
                    try:
                        lanes = {
                            p: offset_path(centerline, sign * (width + gap) / 2),
                            n: offset_path(centerline, -sign * (width + gap) / 2),
                        }
                    except ValueError as exc:
                        if (
                            forced_sign is not None
                            and head_trim == tail_trim == 0
                            and len(fanout_debug) < 8
                        ):
                            fanout_debug.append(
                                dict(sign=sign, a=a, z=z, centerline=centerline, error=str(exc))
                            )
                        failed("offset_bend")
                        continue
                    fanouts = {}
                    for net in (p, n):
                        path = lanes[net]
                        s, t = terminals[net]
                        heads = [
                            x
                            for x in elbows(s, path[0])
                            if length(x) <= head_cap
                            and all(clear(net, a, b, width) for a, b in zip(x, x[1:]))
                        ]
                        tails = [
                            x
                            for x in elbows(path[-1], t)
                            if length(x) <= tail_cap
                            and all(clear(net, a, b, width) for a, b in zip(x, x[1:]))
                        ]
                        if (
                            forced_sign is not None
                            and head_trim == tail_trim == 0
                            and len(fanout_debug) < 8
                        ):
                            fanout_debug.append(
                                dict(
                                    net=net,
                                    sign=sign,
                                    a=a,
                                    z=z,
                                    centerline=centerline,
                                    heads=len(heads),
                                    tails=len(tails),
                                    lane=path,
                                )
                            )
                        fanouts[net] = [
                            simplify(h[:-1] + path + tail[1:]) for h in heads for tail in tails
                        ]
                    if not fanouts[p] or not fanouts[n]:
                        failed("terminal_fanout")
                    for pp in fanouts[p]:
                        for nn in fanouts[n]:
                            paths = {p: pp, n: nn}
                            if not geometry_ok(paths, width, gap, clear):
                                failed("pair_geometry")
                                continue
                            tuned = tune(
                                paths,
                                width,
                                gap,
                                skew,
                                clear,
                                offsets,
                                max_tuning_length=(
                                    max_uncoupled
                                    if max_tuning_length is None
                                    else max_tuning_length
                                ),
                            )
                            if not tuned:
                                failed("skew_tuning")
                                if len(fanout_debug) < 8:
                                    fanout_debug.append(
                                        dict(
                                            skew_tuning_lengths={
                                                net: length(path) + (offsets or {}).get(net, 0)
                                                for net, path in paths.items()
                                            },
                                            offsets=offsets,
                                            max_tuning_length=max_tuning_length,
                                            centerline=centerline,
                                        )
                                    )
                            if tuned and accept_paths is not None and not accept_paths(tuned):
                                failed("path_validation")
                                continue
                            if tuned:
                                return dict(
                                    status="routed",
                                    paths=tuned,
                                    lengths={
                                        net: length(path) + (offsets or {}).get(net, 0)
                                        for net, path in tuned.items()
                                    },
                                    centerline=centerline,
                                    attempts=attempts,
                                )
        if attempts >= max_attempts:
            break
    return dict(
        status="no_coupled_channel",
        paths={},
        attempts=attempts,
        failures=failures,
        fanout_debug=fanout_debug,
    )


def path_metrics(tracks, vias, source, target, *, layer_heights=None):
    """Connected endpoint length, not total copper on a branched net.

    Input tracks are (layer,a,b) centerlines. Split intersections, vias and exact
    terminals; include vertical travel only between used via layers. Reject
    alternative-path cycles because their timing is ambiguous. Dangling branches
    are reported separately instead of being included in the matched path.
    """
    import heapq
    from collections import defaultdict

    from pnr.track_graph import intersection, on_segment

    nm = lambda p: tuple(round(x * 1e6) for x in p)
    segments = [(la, nm(a), nm(b)) for la, a, b in tracks]
    points = defaultdict(set)
    for la, a, b in segments:
        points[la].update((a, b))
    for i, (la, a, b) in enumerate(segments):
        for lb, c, d in segments[i + 1 :]:
            if la == lb:
                q = intersection(a, b, c, d)
                if q is not None:
                    points[la].add(q)
    for p, layers in vias:
        for la in layers:
            points[la].add(nm(p))
    for p, la in (source, target):
        points[la].add(nm(p))
    graph = defaultdict(dict)

    def add(a, b, d):
        if a != b:
            graph[a][b] = min(graph[a].get(b, math.inf), d)
            graph[b][a] = graph[a][b]

    for la, a, b in segments:
        nodes = sorted(
            (p for p in points[la] if on_segment(p, a, b)), key=lambda p: math.dist(p, a)
        )
        for x, y in zip(nodes, nodes[1:]):
            add((la, *x), (la, *y), math.dist(x, y) / 1e6)
    for p, layers in vias:
        if layer_heights is None:
            return dict(connected=False, reason="missing_layer_heights")
        ordered = sorted(layers, key=lambda la: layer_heights[la])
        for a, b in zip(ordered, ordered[1:]):
            add((a, *nm(p)), (b, *nm(p)), abs(layer_heights[a] - layer_heights[b]))
    start = (source[1], *nm(source[0]))
    end = (target[1], *nm(target[0]))
    heap = [(0, start)]
    best = {start: 0}
    parent = {}
    while heap:
        d, a = heapq.heappop(heap)
        if d != best[a]:
            continue
        for b, w in graph[a].items():
            if d + w < best.get(b, math.inf):
                best[b] = d + w
                parent[b] = a
                heapq.heappush(heap, (d + w, b))
    if end not in best:
        return dict(connected=False, reason="disconnected_endpoints")
    reached = set(best)
    edges = sum(len(graph[a]) for a in reached) // 2
    if edges >= len(reached):
        return dict(connected=True, valid=False, reason="ambiguous_cycle")
    path = [end]
    while path[-1] != start:
        path.append(parent[path[-1]])
    return dict(
        connected=True,
        valid=True,
        length_mm=best[end],
        path=list(reversed(path)),
        branch_vertices=len(reached - set(path)),
    )


# --- continuous uncoupled runs and stub branches (PNR_PAIR_PER_RUN_UNCOUPLED /
# PNR_PAIR_STUB_MAX_MM in pnr.native_electrical). Pure geometry; nothing above
# calls these, so the default search is unchanged.

COUPLED_TOLERANCE = 0.1  # relative: a 45-degree outer lane corner is 8.2 % farther


def capsule_interval(a, b, c, d, r):
    """Parameters u in [0,1] where a+u(b-a) lies within r of segment c-d, or None.

    The capsule around c-d is convex, so the admitted set is one interval: the
    hull of the line's intersections with both end discs and the side slab.
    """
    vx, vy = b[0] - a[0], b[1] - a[1]
    pieces = []
    for q in (c, d):
        fx, fy = a[0] - q[0], a[1] - q[1]
        A = vx * vx + vy * vy
        B = 2 * (fx * vx + fy * vy)
        C = fx * fx + fy * fy - r * r
        if A < 1e-18:
            if C <= 0:
                pieces.append((0.0, 1.0))
            continue
        disc = B * B - 4 * A * C
        if disc < 0:
            continue
        root = math.sqrt(disc)
        pieces.append(((-B - root) / (2 * A), (-B + root) / (2 * A)))
    span = math.dist(c, d)
    if span > 1e-12:
        ex, ey = (d[0] - c[0]) / span, (d[1] - c[1]) / span
        lo, hi = -math.inf, math.inf
        for p0, dp, mn, mx in (
            ((a[0] - c[0]) * ex + (a[1] - c[1]) * ey, vx * ex + vy * ey, 0.0, span),
            (-(a[0] - c[0]) * ey + (a[1] - c[1]) * ex, -vx * ey + vy * ex, -r, r),
        ):
            if abs(dp) < 1e-15:
                if p0 < mn or p0 > mx:
                    lo, hi = 1.0, 0.0
                    break
                continue
            u1, u2 = (mn - p0) / dp, (mx - p0) / dp
            lo, hi = max(lo, min(u1, u2)), min(hi, max(u1, u2))
        if lo <= hi:
            pieces.append((lo, hi))
    if not pieces:
        return None
    lo, hi = max(0.0, min(x for x, _ in pieces)), min(1.0, max(y for _, y in pieces))
    return (lo, hi) if lo <= hi else None


def path_steps(node_path):
    """path_metrics' node path -> [(layer, a_mm, b_mm)]; a via hop has layer None."""
    steps = []
    for (la, ax, ay), (lb, bx, by) in zip(node_path, node_path[1:]):
        a, b = (ax / 1e6, ay / 1e6), (bx / 1e6, by / 1e6)
        steps.append((la if la == lb else None, a, b))
    return steps


def uncoupled_runs(
    steps,
    width,
    gap,
    *,
    exempt=None,
    tolerance=COUPLED_TOLERANCE,
    min_coupled=None,
    breaks=None,
    barrel=0.0
):
    """Continuous uncoupled runs along each net's ordered path (two nets).

    ``steps`` maps net -> [(layer, a, b)] in path order (layer None = via hop).
    A point of one net is coupled where its mate's path on the same layer lies
    within (width+gap)*(1+tolerance) of it (nominal pitch; the tolerance admits
    45-degree lane corners). A run continues through via hops (a hop adds
    ``barrel`` mm, default 0 = planar length only, as in the fanout budgets; a
    pair's via pair is never within the coupled pitch) and through coupled stretches
    shorter than ``min_coupled`` (default width+gap), which are counted into
    the adjacent run (also at either end of the path, e.g. pads at lane pitch).
    A coupled stretch of at least ``min_coupled`` ends it. Steps for
    which ``exempt(layer, a, b)`` is true (independently bounded copper, e.g.
    a separately budgeted connector branch) end a run and are not counted.
    ``breaks(layer, point)`` true at a step's start point ends the run there
    (an intermediate terminal pad treated as a run boundary).
    Returns net -> {max_mm, leading_mm, trailing_mm, runs:[{length_mm, start, end}]}.
    """
    nets = list(steps)
    if len(nets) != 2:
        raise ValueError("uncoupled_runs needs exactly two nets")
    r = (width + gap) * (1 + tolerance)
    min_coupled = width + gap if min_coupled is None else min_coupled
    out = {}
    for net in nets:
        mate = nets[1] if net == nets[0] else nets[0]
        layers = {}
        for la, c, d in steps[mate]:
            if la is not None and math.dist(c, d) > 1e-12:
                layers.setdefault(la, []).append((c, d))
        runs = []
        state = dict(current=0.0, start=None, coupled=0.0, coupled_from=None)

        def close(at):
            if state["current"] > 1e-9:
                runs.append(dict(length_mm=state["current"], start=state["start"], end=at))
            state.update(current=0.0, start=None)

        def sliver():
            # A pending coupled stretch shorter than min_coupled is part of the
            # adjacent run (also at the path start, a pad at lane pitch).
            if state["coupled"] and state["coupled"] < min_coupled:
                if state["start"] is None:
                    state["start"] = state["coupled_from"]
                state["current"] += state["coupled"]
            state.update(coupled=0.0, coupled_from=None)

        def uncoupled(length_mm, begin):
            if state["coupled"]:
                sliver()
            if state["start"] is None:
                state["start"] = begin
            state["current"] += length_mm

        def coupled(length_mm, begin):
            if not state["coupled"]:
                state["coupled_from"] = begin
            state["coupled"] += length_mm
            if state["coupled"] >= min_coupled and state["start"] is not None:
                close(state["coupled_from"])

        origin = next(((la, a[0], a[1]) for la, a, b in steps[net] if la is not None), None)
        end = None
        for la, a, b in steps[net]:
            if la is None:
                if barrel > 0:
                    uncoupled(barrel, (None, a[0], a[1]))
                continue
            span = math.dist(a, b)
            if span < 1e-12:
                continue
            if breaks is not None and breaks(la, a):
                if state["start"] is not None:
                    sliver()
                close((la, a[0], a[1]))
                state.update(coupled=0.0, coupled_from=None)
            if exempt is not None and exempt(la, a, b):
                if state["start"] is not None:
                    sliver()
                close((la, a[0], a[1]))
                state.update(coupled=0.0, coupled_from=None)
                end = (la, b[0], b[1])
                continue
            box = (
                min(a[0], b[0]) - r,
                min(a[1], b[1]) - r,
                max(a[0], b[0]) + r,
                max(a[1], b[1]) + r,
            )
            hits = []
            for c, d in layers.get(la, ()):
                if (
                    max(c[0], d[0]) < box[0]
                    or min(c[0], d[0]) > box[2]
                    or max(c[1], d[1]) < box[1]
                    or min(c[1], d[1]) > box[3]
                ):
                    continue
                q = capsule_interval(a, b, c, d, r)
                if q is not None:
                    hits.append(q)
            hits.sort()
            merged = []
            for lo, hi in hits:
                if merged and lo <= merged[-1][1] + 1e-12:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            at = lambda u: (la, a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1]))
            u = 0.0
            for lo, hi in merged + [[1.0, 1.0]]:
                lo = max(lo, u)
                if lo > u + 1e-12:
                    uncoupled((lo - u) * span, at(u))
                if hi > lo:
                    coupled((hi - lo) * span, at(lo))
                u = max(u, hi)
            end = at(1.0)
        trailing = 0.0
        if state["start"] is not None:
            sliver()
            trailing = state["current"]
        close(end)
        leading = runs[0]["length_mm"] if runs and runs[0]["start"] == origin else 0.0
        out[net] = dict(
            max_mm=max([x["length_mm"] for x in runs], default=0.0),
            leading_mm=leading,
            trailing_mm=trailing,
            runs=runs,
        )
    return out


def branch_lengths(tracks, vias, source, target, points, *, layer_heights, barrel=None):
    """Copper length from each point to the source->target endpoint path.

    Same exact graph as :func:`path_metrics` (intersections split, via hops by
    layer height). ``points`` are (xy, layer) nodes such as an intermediate
    terminal pad. Returns one dict per point: on_path, stub_mm (graph length to
    the nearest endpoint-path node: 0 in line, None if disconnected), the
    junction node and path (the nodes from the point to the junction; None if
    disconnected). Returns None when the endpoint graph itself is not valid.
    ``barrel`` (mm): length a via hop counts in the branch search (default: the
    layer-height difference, as the endpoint path).
    """
    import heapq
    from collections import defaultdict

    from pnr.track_graph import intersection, on_segment

    base = path_metrics(tracks, vias, source, target, layer_heights=layer_heights)
    if not base.get("valid"):
        return None
    nm = lambda p: tuple(round(x * 1e6) for x in p)
    segments = [(la, nm(a), nm(b)) for la, a, b in tracks]
    nodes = defaultdict(set)
    for la, a, b in segments:
        nodes[la].update((a, b))
    for i, (la, a, b) in enumerate(segments):
        for lb, c, d in segments[i + 1 :]:
            if la == lb:
                q = intersection(a, b, c, d)
                if q is not None:
                    nodes[la].add(q)
    for p, layers in vias:
        for la in layers:
            nodes[la].add(nm(p))
    for p, la in [source, target] + list(points):
        nodes[la].add(nm(p))
    graph = defaultdict(dict)

    def add(a, b, d):
        if a != b:
            graph[a][b] = min(graph[a].get(b, math.inf), d)
            graph[b][a] = graph[a][b]

    for la, a, b in segments:
        ordered = sorted(
            (p for p in nodes[la] if on_segment(p, a, b)), key=lambda p: math.dist(p, a)
        )
        for x, y in zip(ordered, ordered[1:]):
            add((la, *x), (la, *y), math.dist(x, y) / 1e6)
    for p, layers in vias:
        ordered = sorted(layers, key=lambda la: layer_heights[la])
        for a, b in zip(ordered, ordered[1:]):
            add(
                (a, *nm(p)),
                (b, *nm(p)),
                abs(layer_heights[a] - layer_heights[b]) if barrel is None else barrel,
            )
    main = set(base["path"])
    result = []
    for p, la in points:
        start = (la, *nm(p))
        if start in main:
            result.append(dict(on_path=True, stub_mm=0.0, junction=start, path=[start]))
            continue
        heap = [(0.0, start)]
        best = {start: 0.0}
        parent = {}
        found = None
        while heap:
            dist, a = heapq.heappop(heap)
            if dist != best[a]:
                continue
            if a in main:
                found = (dist, a)
                break
            for b, w in graph[a].items():
                if dist + w < best.get(b, math.inf):
                    best[b] = dist + w
                    parent[b] = a
                    heapq.heappush(heap, (dist + w, b))
        path = None
        if found:
            # Nodes (layer, x_nm, y_nm) from the point to the junction.
            path = [found[1]]
            while path[-1] != start:
                path.append(parent[path[-1]])
            path.reverse()
        result.append(
            dict(
                on_path=False,
                stub_mm=found[0] if found else None,
                junction=found[1] if found else None,
                path=path,
            )
        )
    return result
