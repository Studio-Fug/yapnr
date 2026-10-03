"""Integer-state A* over a dense search field (:mod:`.dense_maze`).

The same search as the reference :func:`pnr.route.detail.maze._astar_reference`:
the same moves in the same order, the same prices and arithmetic, the same heap
key ``(f, tie)`` and the same closed-set and relaxation rules, so it returns the
same path. Cells are flat ``[layer, j, i]`` keys; every predicate and price is a
list lookup in the field, built once per routed net. The native kernel
(:mod:`.native_maze`) runs this loop in C on the same field.
"""

from __future__ import annotations

import heapq

from .dense_maze import build_field, cell_of_key, keys_of

_SQRT2 = 2.0**0.5
_ORTHOGONAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
_DIAGONAL = ((1, 1), (1, -1), (-1, 1), (-1, -1))


def astar(
    grid,
    sources,
    targets,
    net,
    occ,
    history,
    via_cost,
    pres_fac,
    blocked=None,
    soft=None,
    diagonal=True,
    drill_sites=(),
    field=None,
):
    """:func:`pnr.route.detail.maze._astar` on a dense field (built here unless
    given). Grids outside the dense model use the reference search."""
    if not targets:
        return None
    if field is None:
        field = build_field(grid, net, occ, history, pres_fac, blocked, soft)
    if field is None:
        from .dense_maze import plain_soft
        from .maze import _astar_reference

        return _astar_reference(
            grid,
            sources,
            targets,
            net,
            occ,
            history,
            via_cost,
            pres_fac,
            blocked,
            plain_soft(soft),
            diagonal,
            drill_sites,
        )
    return search(field, sources, targets, via_cost, diagonal, drill_sites)


def endpoints(field, sources, targets):
    """``(starts, ends, target_xy)``: start keys in the reference's sorted order,
    target keys and the heuristic's target columns; None when either is empty."""
    nx, plane = field.nx, field.nx * field.ny
    ends = set(keys_of(field, targets))
    if not ends:
        return None
    starts = sorted(
        set(keys_of(field, sources)), key=lambda k: (k // plane, k % nx, (k % plane) // nx)
    )
    if not starts:
        return None
    target_xy = [(c.i, c.j) for c in set(targets) if _key(field, c) in ends]
    return starts, ends, target_xy


def _key(field, c):
    if 0 <= c.layer < field.nlayers and 0 <= c.i < field.nx and 0 <= c.j < field.ny:
        return c.layer * field.nx * field.ny + c.j * field.nx + c.i
    return None


def search(field, sources, targets, via_cost, diagonal=True, drill_sites=()):
    """A* from ``sources`` to the nearest of ``targets`` on ``field``."""
    found = endpoints(field, sources, targets)
    if found is None:
        return None
    starts, ends, target_xy = found
    kernel = _native()
    if kernel is not None:
        return kernel.search(field, starts, ends, via_cost, diagonal, drill_sites)
    return _search(field, starts, ends, target_xy, via_cost, diagonal, drill_sites)


def _native():
    from .native_maze import active

    return active()


def _search(field, starts, ends, target_xy, via_cost, diagonal, drill_sites):
    nx, ny, nlayers = field.nx, field.ny, field.nlayers
    plane = nx * ny
    ok, price, via_price, column, plated, corner, diag = field.lists()
    hole = field.tree_hole(drill_sites).reshape(-1).tolist()
    radius, mask = field.stencil
    conflict = {
        (di - radius, dj - radius)
        for dj in range(2 * radius + 1)
        for di in range(2 * radius + 1)
        if mask[dj, di]
    }

    heuristics = {}

    def heuristic(key):
        value = heuristics.get(key)
        if value is None:
            ij = key % plane
            j, i = divmod(ij, nx)
            best = None
            for ti, tj in target_xy:
                dx, dy = (abs(i - ti), abs(j - tj))
                distance = dx + dy - (2.0 - _SQRT2) * min(dx, dy)
                best = distance if best is None or distance < best else best
            value = heuristics[key] = best or 0.0
        return value

    queue = []
    distances = {}
    came = {}
    path_drills = {}
    tie = 0
    for key in starts:
        distances[key] = 0.0
        path_drills[key] = ()
        heapq.heappush(queue, (heuristic(key), tie, key))
        tie += 1
    closed = set()
    infinity = float("inf")
    heappop, heappush = heapq.heappop, heapq.heappush
    while queue:
        _, _, current = heappop(queue)
        if current in closed:
            continue
        closed.add(current)
        if current in ends:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            return [cell_of_key(field, key) for key in reversed(path)]
        layer, ij = divmod(current, plane)
        j, i = divmod(ij, nx)
        base = distances[current]
        layer_base = layer * plane
        drills = path_drills[current]
        for di, dj in _ORTHOGONAL:
            ni, nj = (i + di, j + dj)
            if not (0 <= ni < nx and 0 <= nj < ny):
                continue
            nxt = layer_base + nj * nx + ni
            if not ok[nxt]:
                continue
            new_distance = base + price[nxt]
            if new_distance < distances.get(nxt, infinity):
                distances[nxt] = new_distance
                came[nxt] = current
                path_drills[nxt] = drills
                heappush(queue, (new_distance + heuristic(nxt), tie, nxt))
                tie += 1
        if diagonal:
            for di, dj in _DIAGONAL:
                ni, nj = (i + di, j + dj)
                if not (0 <= ni < nx and 0 <= nj < ny):
                    continue
                nxt = layer_base + nj * nx + ni
                if not (
                    ok[nxt]
                    and corner[layer_base + j * nx + ni]
                    and corner[layer_base + nj * nx + i]
                ):
                    continue
                if diag is not None and not diag[layer_base + min(j, nj) * nx + min(i, ni)]:
                    continue
                new_distance = base + _SQRT2 * price[nxt]
                if new_distance < distances.get(nxt, infinity):
                    distances[nxt] = new_distance
                    came[nxt] = current
                    path_drills[nxt] = drills
                    heappush(queue, (new_distance + heuristic(nxt), tie, nxt))
                    tie += 1
        if not column[ij]:
            continue
        drop = not plated[ij]
        if drop:
            if not hole[ij]:
                continue
            clear = True
            for site in drills:
                sj, si = divmod(site, nx)
                if (i - si, j - sj) in conflict:
                    clear = False
                    break
            if not clear:
                continue
        for target_layer in range(nlayers):
            if target_layer == layer:
                continue
            nxt = target_layer * plane + ij
            if not ok[nxt]:
                continue
            # A through-via is priced over every layer; a plated pad as a cell.
            new_distance = (
                base + (via_price[ij] if drop else price[nxt]) + (via_cost if drop else 0.0)
            )
            if new_distance < distances.get(nxt, infinity):
                distances[nxt] = new_distance
                came[nxt] = current
                path_drills[nxt] = drills + (ij,) if drop else drills
                heappush(queue, (new_distance + heuristic(nxt), tie, nxt))
                tie += 1
    return None
