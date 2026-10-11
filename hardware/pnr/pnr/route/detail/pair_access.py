"""Bounded joint via-access choices for a differential pair.

Both polarities keep their identities. Clearance callbacks judge actual copper,
holes and all traversed layers; no candidate reserves anything until committed.
"""

import math

from . import coupled


def ports(
    terminals,
    width,
    gap,
    diameter,
    drill,
    clearance,
    hole_gap,
    cap,
    clear,
    via_clear,
    route_access=None,
):
    """Paired via sites and surface paths, ordered by total lead length.

    Each callback receives the net identity. Independently selectable orientations
    at the two ends allow the trunk to choose a different via embedding at each
    access. The caller must check the complete route's skew and uncoupled budget.
    """
    p, n = terminals
    a, b = terminals[p], terminals[n]
    distance = math.dist(a, b)
    if distance < 1e-8:
        return []
    axis = ((a[0] - b[0]) / distance, (a[1] - b[1]) / distance)
    tangent = (-axis[1], axis[0])
    mid = tuple((a[i] + b[i]) / 2 for i in (0, 1))
    separation = max(diameter + clearance, drill + hole_gap) + 0.002
    found, seen, legal_sites = [], set(), []
    runs = (0.4, 0.6, 0.8, 1.0, 1.2, 1.5) + ((2.0, 3.0, 4.0) if route_access else ())
    for run in runs:
        if run >= cap and route_access is None:
            continue
        for sign in (-1, 1):
            for angle in (0, 45, -45, 90, -90, 135, -135, 180):
                co, si = math.cos(math.radians(angle)), math.sin(math.radians(angle))
                direction = (axis[0] * co - axis[1] * si, axis[0] * si + axis[1] * co)
                for shift in (0, -0.25, 0.25):
                    center = tuple(
                        mid[i] + sign * tangent[i] * run + shift * axis[i] for i in (0, 1)
                    )
                    sites = {
                        net: tuple(center[i] + side * direction[i] * separation / 2 for i in (0, 1))
                        for net, side in ((p, 1), (n, -1))
                    }
                    if not all(via_clear(net, q) for net, q in sites.items()):
                        continue
                    legal_sites.append(dict(sites=sites, angle_deg=angle, run_mm=run))
                    choices = {
                        net: [
                            path
                            for path in coupled.elbows(terminals[net], sites[net])
                            if coupled.length(path) < cap
                            and all(clear(net, x, y, width) for x, y in zip(path, path[1:]))
                        ]
                        for net in (p, n)
                    }
                    for pp in choices[p]:
                        for nn in choices[n]:
                            paths = {p: pp, n: nn}
                            if not coupled.geometry_ok(paths, width, gap, clear):
                                continue
                            if any(
                                coupled.segment_distance(x, y, sites[other], sites[other])
                                < (diameter + width) / 2 + clearance - 1e-7
                                for net, other in ((p, n), (n, p))
                                for x, y in zip(paths[net], paths[net][1:])
                            ):
                                continue
                            key = tuple(tuple(round(v, 6) for v in sites[net]) for net in (p, n))
                            if key in seen:
                                continue
                            seen.add(key)
                            found.append(
                                dict(
                                    sites=sites,
                                    paths=paths,
                                    lengths={
                                        net: coupled.length(path) for net, path in paths.items()
                                    },
                                    angle_deg=angle,
                                )
                            )
    if route_access:
        # A coupled surface leg can reach an unobstructed via site farther than
        # the fanout budget: charge only its measured uncoupled copper, never
        # mistake the entire surface run for uncoupled lead length.
        legal_sites.sort(key=lambda row: row["run_mm"])
        diverse, angles = [], set()
        for row in legal_sites:
            if row["angle_deg"] not in angles:
                diverse.append(row)
                angles.add(row["angle_deg"])
        diverse += [row for row in legal_sites if row not in diverse]
        for row in diverse[:24]:
            sites = row["sites"]
            paths = route_access(sites)
            if paths is None or not coupled.geometry_ok(paths, width, gap, clear):
                continue
            if any(
                coupled.segment_distance(x, y, sites[other], sites[other])
                < (diameter + width) / 2 + clearance - 1e-7
                for net, other in ((p, n), (n, p))
                for x, y in zip(paths[net], paths[net][1:])
            ):
                continue
            runs = coupled.uncoupled_runs(
                {net: [(0, a, b) for a, b in zip(path, path[1:])] for net, path in paths.items()},
                width,
                gap,
            )
            unc = {net: sum(r["length_mm"] for r in runs[net]["runs"]) for net in (p, n)}
            if max(unc.values()) >= cap:
                continue
            found.append(
                dict(
                    row,
                    paths=paths,
                    lengths={net: coupled.length(path) for net, path in paths.items()},
                    uncoupled=unc,
                )
            )
    return sorted(
        found,
        key=lambda row: (
            sum(row.get("uncoupled", row["lengths"]).values()),
            sum(row["lengths"].values()),
            row["angle_deg"],
        ),
    )
