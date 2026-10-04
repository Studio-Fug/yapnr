"""DC IR drop of a supply rail: a resistive network over its copper, solved exactly.

The rail's copper (:func:`pnr.ir_extract.extract`, or any dict of the same form) is

* **zones and pads**: rasterized per copper layer at ``h`` (default 0.1 mm); a cell
  is copper when its centre is. Neighbouring copper cells of one layer are joined by
  the conductance of one square, ``t / rho``;
* **tracks and arcs** (arcs as chords): exact one-dimensional resistors
  ``rho L / (w t)``, joined to the copper cell under each end point, to other track
  end points, and to the interior of another track an end point lies on (a T join);
* **vias and plated holes**: a barrel node chain over the span, each segment
  ``rho dz / (pi (d + t) t)`` (``d`` the finished hole, ``t`` the plating); the land
  on every spanned layer is one node (its cells are merged).

The ``sources`` pads are held at 0 V; each sink draws its share of ``current_a``
spread over its pad cells. The network is solved by Jacobi-preconditioned conjugate
gradients to a relative residual of 1e-10 (``tol``); a union-find runs first, so a
sink that no copper joins to a source is reported **open**, never as a number.

:func:`solve` returns the report of one rail: the drop at each sink, the effective
resistance (worst drop / current), the two-point resistance per sink with the other
sinks open (comparable to a path measure), the I^2 R loss, the largest current density
per layer (A per mm of width) with its location and a neck flag against IPC-2221, and
pass or fail against ``budget_mohm`` or ``budget_mv``. Warnings take the quantified
assumption form (statement, consequence). Copper only: the DC resistance of parts in
the path (ferrites, sense resistors) is not modelled.

numpy only; no KiCad. Units: mm, A, V, ohm (reports in mV and mOhm).
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

RHO_20C = 1.72e-5  # copper resistivity at 20 C, ohm mm
ALPHA = 0.00393  # its temperature coefficient, 1/K
PLATING_MM = 0.020  # via barrel plating (IPC-6012 class 2 average)
DEFAULT_H = 0.1


def resistivity(temperature_c: float = 25.0) -> float:
    """Copper resistivity (ohm mm) at ``temperature_c``."""
    return RHO_20C * (1.0 + ALPHA * (float(temperature_c) - 20.0))


def barrel_ohm(length_mm, drill_mm, plating_mm=PLATING_MM, rho=RHO_20C):
    """A via barrel of finished hole ``drill_mm`` over ``length_mm``."""
    return rho * length_mm / (math.pi * (drill_mm + plating_mm) * plating_mm)


# ------------------------------------------------------------------ raster


class _Raster:
    """A common cell grid over the rail's copper (cell centres at x0 + (i + 0.5) h)."""

    def __init__(self, bounds, h):
        x0, y0, x1, y1 = bounds
        self.h = float(h)
        self.x0 = math.floor(x0 / h) * h - h
        self.y0 = math.floor(y0 / h) * h - h
        self.nx = int(math.ceil((x1 - self.x0) / h)) + 2
        self.ny = int(math.ceil((y1 - self.y0) / h)) + 2

    def centres(self):
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.h
        ys = self.y0 + (np.arange(self.ny) + 0.5) * self.h
        return xs, ys

    def cell(self, p):
        i = int(math.floor((p[0] - self.x0) / self.h))
        j = int(math.floor((p[1] - self.y0) / self.h))
        return min(max(i, 0), self.nx - 1), min(max(j, 0), self.ny - 1)

    def polygon(self, rings) -> np.ndarray:
        """``[ny, nx]`` bool: cells whose centre is inside ``rings`` (even-odd, so a
        polygon's holes are rings too)."""
        out = np.zeros((self.ny, self.nx), dtype=bool)
        edges = []
        for ring in rings:
            r = np.asarray(ring, dtype=float)
            if len(r) < 3:
                continue
            nxt = np.roll(r, -1, axis=0)
            edges.append(np.hstack([r, nxt]))
        if not edges:
            return out
        e = np.vstack(edges)
        e = e[e[:, 1] != e[:, 3]]
        if not len(e):
            return out
        xs, ys = self.centres()
        lo = max(0, int(math.floor((e[:, [1, 3]].min() - self.y0) / self.h)) - 1)
        hi = min(self.ny - 1, int(math.ceil((e[:, [1, 3]].max() - self.y0) / self.h)) + 1)
        for j in range(lo, hi + 1):
            y = ys[j]
            hit = (e[:, 1] > y) != (e[:, 3] > y)
            if not hit.any():
                continue
            a = e[hit]
            xc = a[:, 0] + (y - a[:, 1]) * (a[:, 2] - a[:, 0]) / (a[:, 3] - a[:, 1])
            xc.sort()
            for k in range(0, len(xc) - 1, 2):
                i0 = int(math.ceil((xc[k] - self.x0) / self.h - 0.5))
                i1 = int(math.floor((xc[k + 1] - self.x0) / self.h - 0.5))
                if i1 >= i0:
                    out[j, max(i0, 0) : min(i1, self.nx - 1) + 1] = True
        return out

    def disc(self, centre, radius) -> np.ndarray:
        """Cells whose centre is within ``radius`` of ``centre`` (at least its own)."""
        out = np.zeros((self.ny, self.nx), dtype=bool)
        xs, ys = self.centres()
        i0 = max(0, int((centre[0] - radius - self.x0) / self.h) - 1)
        i1 = min(self.nx - 1, int((centre[0] + radius - self.x0) / self.h) + 1)
        j0 = max(0, int((centre[1] - radius - self.y0) / self.h) - 1)
        j1 = min(self.ny - 1, int((centre[1] + radius - self.y0) / self.h) + 1)
        X, Y = np.meshgrid(xs[i0 : i1 + 1], ys[j0 : j1 + 1])
        out[j0 : j1 + 1, i0 : i1 + 1] = (X - centre[0]) ** 2 + (Y - centre[1]) ** 2 <= radius**2
        i, j = self.cell(centre)
        out[j, i] = True
        return out


def _bounds(copper) -> Tuple[float, float, float, float]:
    pts = []
    for z in copper.get("zones", []):
        for poly in z["polygons"]:
            pts.extend(poly["outline"])
    for p in copper.get("pads", []):
        pts.extend(p["polygon"])
    for t in copper.get("tracks", []):
        w = t["width_mm"] / 2
        for q in (t["a"], t["b"]):
            pts.extend([(q[0] - w, q[1] - w), (q[0] + w, q[1] + w)])
    for a in copper.get("arcs", []):
        for q in (a["start"], a["mid"], a["end"]):
            pts.append(q)
    for v in copper.get("vias", []):
        r = v["diameter_mm"] / 2
        pts.extend([(v["at"][0] - r, v["at"][1] - r), (v["at"][0] + r, v["at"][1] + r)])
    if not pts:
        raise ValueError("the rail has no copper")
    a = np.asarray(pts, dtype=float)
    return float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), float(a[:, 1].max())


def arc_chords(start, mid, end, max_sagitta=0.001):
    """Points along the circular arc start-mid-end (chords within ``max_sagitta``)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [tuple(start), tuple(end)]
    ux = (
        (x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)
    ) / d
    uy = (
        (x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)
    ) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (start, mid, end))

    def ccw(a, b):
        return (b - a) % (2 * math.pi)

    sweep = ccw(a1, a3)
    if ccw(a1, a2) > sweep:  # clockwise through mid
        sweep -= 2 * math.pi
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - max_sagitta / r))) if r > max_sagitta else math.pi
    n = max(1, int(math.ceil(abs(sweep) / max(step, 1e-6))))
    return [
        (ux + r * math.cos(a1 + sweep * k / n), uy + r * math.sin(a1 + sweep * k / n))
        for k in range(n + 1)
    ]


# ------------------------------------------------------------------ network


class _Network:
    def __init__(self):
        self.n = 0
        self.i: List[np.ndarray] = []
        self.j: List[np.ndarray] = []
        self.g: List[np.ndarray] = []
        self.kind: List[np.ndarray] = []  # 0 raster, 1 track, 2 barrel

    def nodes(self, count) -> np.ndarray:
        out = np.arange(self.n, self.n + count)
        self.n += count
        return out

    def add(self, i, j, g, kind):
        i, j, g = np.atleast_1d(i), np.atleast_1d(j), np.atleast_1d(g).astype(float)
        keep = i != j
        self.i.append(i[keep])
        self.j.append(j[keep])
        self.g.append(np.broadcast_to(g, i.shape)[keep] if g.size == 1 else g[keep])
        self.kind.append(np.full(int(keep.sum()), kind, dtype=np.int8))

    def arrays(self):
        if not self.i:
            e = np.zeros(0, dtype=np.int64)
            return e, e, np.zeros(0), np.zeros(0, dtype=np.int8)
        return (
            np.concatenate(self.i).astype(np.int64),
            np.concatenate(self.j).astype(np.int64),
            np.concatenate(self.g),
            np.concatenate(self.kind),
        )


def _find(parent, x):
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _components(n, i, j) -> np.ndarray:
    """Connected component label per node (vectorized label propagation)."""
    label = np.arange(n)
    if not len(i):
        return label
    while True:
        m = np.minimum(label[i], label[j])
        new = label.copy()
        np.minimum.at(new, i, m)
        np.minimum.at(new, j, m)
        new = new[new]  # pointer jumping
        if np.array_equal(new, label):
            return label
        label = new


def _pcg(i, j, g, diag, b, tol, max_iter):
    """Solve the reduced Laplacian system (``diag`` includes the conductance to the
    held nodes) by Jacobi-preconditioned CG. Returns (x, relative residual, iterations)."""
    n = len(b)

    def matvec(x):
        y = diag * x
        y -= np.bincount(i, weights=g * x[j], minlength=n)
        y -= np.bincount(j, weights=g * x[i], minlength=n)
        return y

    def dot(a, b):  # no BLAS: Accelerate builds of numpy raise spurious FP flags
        return float(np.sum(a * b))

    inv = 1.0 / diag
    x = np.zeros(n)
    r = b.copy()
    norm = math.sqrt(dot(b, b)) or 1.0
    z = inv * r
    p = z.copy()
    rz = dot(r, z)
    it = 0
    res = math.sqrt(dot(r, r)) / norm
    while res > tol and it < max_iter:
        q = matvec(p)
        alpha = rz / dot(p, q)
        x += alpha * p
        r -= alpha * q
        it += 1
        if it % 50 == 0:  # refresh the true residual against drift
            r = b - matvec(x)
        res = math.sqrt(dot(r, r)) / norm
        z = inv * r
        rz_new = dot(r, z)
        p = z + (rz_new / rz) * p
        rz = rz_new
    return x, res, it


def _key(p):
    return (round(p[0], 6), round(p[1], 6))


def build(
    copper: Dict, h: float = DEFAULT_H, temperature_c: float = 25.0, plating_mm: float = PLATING_MM
):
    """The rail's resistive network: a dict with the raster, node maps and edges."""
    rho = resistivity(temperature_c)
    layers = [x["name"] for x in copper["layers"]]
    thick = {x["name"]: float(x["copper_mm"]) for x in copper["layers"]}
    zpos = {x["name"]: float(x["z_mm"]) for x in copper["layers"]}
    raster = _Raster(_bounds(copper), h)
    masks = {la: np.zeros((raster.ny, raster.nx), dtype=bool) for la in layers}
    for z in copper.get("zones", []):
        rings = []
        for poly in z["polygons"]:
            rings.append(poly["outline"])
            rings.extend(poly.get("holes", []))
        masks[z["layer"]] |= raster.polygon(rings)
    pad_cells = []
    for p in copper.get("pads", []):
        shape = raster.polygon([p["polygon"]] + list(p.get("holes") or []))
        if not shape.any():
            shape = raster.disc(p["at"], 0.0)
        cells = {}
        for la in p["layers"]:
            if la in masks:
                masks[la] |= shape
                cells[la] = shape
        pad_cells.append(cells)
    via_lands = []
    for v in copper.get("vias", []):
        span = layers[layers.index(v["top"]) : layers.index(v["bottom"]) + 1]
        land = raster.disc(v["at"], v["diameter_mm"] / 2)
        for la in span:
            masks[la] |= land
        via_lands.append((span, land))
    net = _Network()
    node = {}
    for la in layers:
        ids = np.full(masks[la].shape, -1, dtype=np.int64)
        count = int(masks[la].sum())
        ids[masks[la]] = net.nodes(count)
        node[la] = ids
    # Merge each via land (one node per spanned layer) and each pad's cells? Pads keep
    # their cells; a via land is equipotential (the barrel meets it all round).
    parent = np.arange(net.n)
    for span, land in via_lands:
        for la in span:
            ids = node[la][land]
            ids = ids[ids >= 0]
            for k in ids[1:]:
                a, b = _find(parent, int(ids[0])), _find(parent, int(k))
                if a != b:
                    parent[max(a, b)] = min(a, b)
    for la in layers:
        ids = node[la]
        g = thick[la] / rho
        right = (ids[:, :-1] >= 0) & (ids[:, 1:] >= 0)
        net.add(ids[:, :-1][right], ids[:, 1:][right], g, 0)
        up = (ids[:-1, :] >= 0) & (ids[1:, :] >= 0)
        net.add(ids[:-1, :][up], ids[1:, :][up], g, 0)

    def cell_node(la, p):
        i, j = raster.cell(p)
        k = node[la][j, i] if la in node else -1
        return int(k) if k >= 0 else None

    # Tracks and arcs: exact 1-D resistors between end nodes, split at T joins.
    segs = []
    for t in copper.get("tracks", []):
        segs.append((t["layer"], tuple(t["a"]), tuple(t["b"]), float(t["width_mm"])))
    for a in copper.get("arcs", []):
        pts = arc_chords(a["start"], a["mid"], a["end"])
        for p, q in zip(pts, pts[1:]):
            segs.append((a["layer"], p, q, float(a["width_mm"])))
    ends: Dict[Tuple[str, Tuple[float, float]], int] = {}
    extra = []

    def end_node(la, p):
        key = (la, _key(p))
        if key not in ends:
            k = cell_node(la, p)
            if k is None:
                k = int(net.nodes(1)[0])
                extra.append(k)
            ends[key] = k
        return ends[key]

    by_layer: Dict[str, List[Tuple[float, float]]] = {}
    for la, p, q, _w in segs:
        by_layer.setdefault(la, []).extend([p, q])
    track_edges = []
    for la, p, q, w in segs:
        dx, dy = q[0] - p[0], q[1] - p[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        cuts = [0.0, 1.0]
        for e in by_layer[la]:
            t = ((e[0] - p[0]) * dx + (e[1] - p[1]) * dy) / (length * length)
            if 1e-6 < t < 1 - 1e-6:
                off = abs((e[0] - p[0]) * dy - (e[1] - p[1]) * dx) / length
                if off < 1e-4:
                    cuts.append(t)
        cuts = sorted(set(round(c, 9) for c in cuts))
        for t0, t1 in zip(cuts, cuts[1:]):
            a = (p[0] + dx * t0, p[1] + dy * t0)
            b = (p[0] + dx * t1, p[1] + dy * t1)
            seg_len = length * (t1 - t0)
            g = thick[la] * w / (rho * seg_len)
            track_edges.append((end_node(la, a), end_node(la, b), g, la, a, b, w))
    if track_edges:
        net.add(
            np.array([e[0] for e in track_edges]),
            np.array([e[1] for e in track_edges]),
            np.array([e[2] for e in track_edges]),
            1,
        )
    # Barrels: vias, then plated pad holes.
    barrels = []
    for v, (span, _land) in zip(copper.get("vias", []), via_lands):
        barrels.append((v["at"], span, float(v["drill_mm"])))
    for p in copper.get("pads", []):
        if p.get("drill_mm") and len(p["layers"]) > 1:
            span = [la for la in layers if la in p["layers"]]
            barrels.append((p["at"], span, float(p["drill_mm"])))
    for at, span, drill in barrels:
        nodes = [cell_node(la, at) for la in span]
        for (la, a), (lb, b) in zip(zip(span, nodes), list(zip(span, nodes))[1:]):
            if a is None or b is None:
                continue
            dz = abs(zpos[lb] - zpos[la])
            net.add(np.array([a]), np.array([b]), 1.0 / barrel_ohm(dz, drill, plating_mm, rho), 2)
    i, j, g, kind = net.arrays()
    # Apply the via-land merges: every node maps to its representative.
    rep = np.array([_find(parent, k) for k in range(len(parent))] + extra, dtype=np.int64)
    if len(rep) < net.n:
        rep = np.concatenate([rep, np.arange(len(rep), net.n)])
    # An edge whose two ends merged into one node (inside a via land) carries no
    # current; kept, it would only inflate the Jacobi preconditioner's diagonal.
    ri, rj = rep[i], rep[j]
    keep = ri != rj
    return dict(
        raster=raster,
        layers=layers,
        node=node,
        rep=rep,
        n=net.n,
        edges=(ri[keep], rj[keep], g[keep], kind[keep]),
        pad_cells=pad_cells,
        track_edges=track_edges,
        thick=thick,
        rho=rho,
    )


def _pad_nodes(model, index) -> np.ndarray:
    out = []
    for la, cells in model["pad_cells"][index].items():
        ids = model["node"][la][cells]
        out.append(ids[ids >= 0])
    if not out:
        return np.zeros(0, dtype=np.int64)
    return np.unique(model["rep"][np.concatenate(out)])


def _solve_network(model, held, loads, tol, max_iter):
    """Potentials (V) with ``held`` nodes at 0 and ``loads`` {node: A drawn}."""
    i, j, g, _kind = model["edges"]
    n = model["n"]
    label = _components(n, i, j)
    source_labels = set(label[held].tolist())
    active = np.isin(label, list(source_labels))
    is_held = np.zeros(n, dtype=bool)
    is_held[held] = True
    unknown = active & ~is_held
    index = -np.ones(n, dtype=np.int64)
    index[unknown] = np.arange(int(unknown.sum()))
    diag = np.zeros(int(unknown.sum()))
    both = unknown[i] & unknown[j]
    ii, jj, gg = index[i[both]], index[j[both]], g[both]
    np.add.at(diag, ii, gg)
    np.add.at(diag, jj, gg)
    for a, b in ((i, j), (j, i)):  # an unknown tied to a held node
        m = unknown[a] & is_held[b]
        np.add.at(diag, index[a[m]], g[m])
    rhs = np.zeros(len(diag))
    for k, amps in loads.items():
        if unknown[k]:
            rhs[index[k]] += amps
    v = np.zeros(n)
    if len(rhs):
        x, res, it = _pcg(ii, jj, gg, diag, rhs, tol, max_iter)
        v[unknown] = x
    else:
        res, it = 0.0, 0
    return v, active, res, it


def _png(path, image):
    """Write an RGB uint8 image (rows top to bottom) as a PNG (stdlib zlib)."""
    h, w, _ = image.shape
    raw = b"".join(b"\x00" + image[r].tobytes() for r in range(h))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


def _heat(v_layer, mask):
    """A blue-to-red map of ``v_layer`` over ``mask`` (rows flipped: y up)."""
    img = np.full(mask.shape + (3,), 255, dtype=np.uint8)
    if mask.any():
        vals = v_layer[mask]
        lo, hi = float(vals.min()), float(vals.max())
        t = np.zeros(mask.shape)
        t[mask] = (v_layer[mask] - lo) / (hi - lo) if hi > lo else 0.0
        img[..., 0] = np.where(mask, (255 * t).astype(np.uint8), 255)
        img[..., 1] = np.where(mask, (255 * (1 - abs(2 * t - 1))).astype(np.uint8), 255)
        img[..., 2] = np.where(mask, (255 * (1 - t)).astype(np.uint8), 255)
    return img[::-1]


def solve(
    copper: Dict,
    *,
    sources: Sequence[int],
    sinks: Sequence[int],
    current_a: float,
    split: str = "equal",
    weights: Optional[Sequence[float]] = None,
    budget_mohm: Optional[float] = None,
    budget_mv: Optional[float] = None,
    temperature_c: float = 25.0,
    h: float = DEFAULT_H,
    plating_mm: float = PLATING_MM,
    tol: float = 1e-10,
    max_iter: int = 200000,
    two_point: bool = True,
    heatmap: Optional[str] = None,
    copper_oz_delta_t_c: float = 10.0,
) -> Dict:
    """The IR-drop report of one rail. ``sources``/``sinks`` index ``copper["pads"]``;
    ``split`` is ``equal`` (each sink draws I/n), ``area`` (by pad area) or
    ``weights`` (explicit, normalized)."""
    model = build(copper, h=h, temperature_c=temperature_c, plating_mm=plating_mm)
    pads = copper["pads"]
    held = np.unique(np.concatenate([_pad_nodes(model, k) for k in sources]))
    if not len(held):
        raise ValueError("the source pads have no copper")
    if weights is not None:
        w = np.asarray(weights, dtype=float)
    elif split == "area":
        w = np.array([max(_poly_area(pads[k]["polygon"]), 1e-9) for k in sinks])
    elif split == "equal":
        w = np.ones(len(sinks))
    else:
        raise ValueError("split must be equal or area")
    w = w / w.sum()
    i, j, g, kind = model["edges"]
    label = _components(model["n"], i, j)
    source_labels = set(label[held].tolist())
    rows = []
    loads = {}
    opens = []
    sink_nodes = []
    for k, share in zip(sinks, w):
        nodes = _pad_nodes(model, k)
        name = "%s.%s" % (pads[k].get("ref"), pads[k].get("pad"))
        if not len(nodes) or not (set(label[nodes].tolist()) & source_labels):
            opens.append(name)
            sink_nodes.append((name, nodes, share, False))
            continue
        for nd in nodes:
            loads[int(nd)] = loads.get(int(nd), 0.0) + current_a * share / len(nodes)
        sink_nodes.append((name, nodes, share, True))
    v, active, res, it = _solve_network(model, held, loads, tol, max_iter)
    worst = 0.0
    for name, nodes, share, ok in sink_nodes:
        row = dict(sink=name, share=round(float(share), 6))
        if not ok:
            row["open"] = True
        else:
            drop = float(v[nodes].mean())
            worst = max(worst, drop)
            row["drop_mv"] = round(drop * 1e3, 6)
        rows.append(row)
    loss = float((g * (v[i] - v[j]) ** 2).sum())
    report = dict(
        current_a=current_a,
        split=split if weights is None else "weights",
        temperature_c=temperature_c,
        resistivity_ohm_mm=model["rho"],
        h_mm=h,
        plating_mm=plating_mm,
        nodes=int(model["n"]),
        edges=int(len(i)),
        residual=res,
        iterations=it,
        sinks=rows,
        opens=opens,
        loss_w=round(loss, 9),
    )
    if not opens:
        report["worst_drop_mv"] = round(worst * 1e3, 6)
        report["r_eff_mohm"] = round(worst / current_a * 1e3, 6) if current_a else None
    if two_point and not opens:
        tp = []
        for name, nodes, _share, ok in sink_nodes:
            vk, _a, r2, _it = _solve_network(
                model, held, {int(n): 1.0 / len(nodes) for n in nodes}, tol, max_iter
            )
            tp.append(dict(sink=name, r_mohm=round(float(vk[nodes].mean()) * 1e3, 6)))
            report["residual"] = max(report["residual"], r2)
        report["two_point"] = tp
    report["density"] = _density(model, v, current_a, copper_oz_delta_t_c)
    status = "open" if opens else "pass"
    if not opens and not (math.isfinite(report["residual"]) and report["residual"] <= tol):
        # CG stopped short of its tolerance: no number of this solve is a result.
        status = "unsolved"
        report["worst_drop_mv"] = report["r_eff_mohm"] = report["loss_w"] = None
        report["density"] = {}
        report.pop("two_point", None)
    if status == "pass" and budget_mohm is not None and report["r_eff_mohm"] > budget_mohm + 1e-12:
        status = "fail"
    if status == "pass" and budget_mv is not None and report["worst_drop_mv"] > budget_mv + 1e-12:
        status = "fail"
    if budget_mohm is not None:
        report["budget_mohm"] = budget_mohm
    if budget_mv is not None:
        report["budget_mv"] = budget_mv
    report["status"] = status
    report["warnings"] = _warnings(report, budget_mohm, budget_mv)
    if heatmap:
        layer_maps = []
        for la in model["layers"]:
            ids = model["node"][la]
            mask = ids >= 0
            if not mask.any():
                continue
            vl = np.zeros(ids.shape)
            vl[mask] = v[model["rep"][ids[mask]]]
            layer_maps.append((la, vl, mask))
        for la, vl, mask in layer_maps:
            _png("%s-%s.png" % (heatmap, la.replace(".", "_")), _heat(vl, mask))
        report["heatmaps"] = [
            "%s-%s.png" % (heatmap, la.replace(".", "_")) for la, _v, _m in layer_maps
        ]
    return report


def _poly_area(poly):
    return 0.5 * abs(
        sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(poly, list(poly[1:]) + list(poly[:1])))
    )


def _density(model, v, current_a, delta_t_c):
    """The largest current per mm of width on each layer (raster cells: from the
    face currents; tracks: I / w), where, and a neck flag where it exceeds what an
    IPC-2221 trace carrying the rail's whole current would carry per mm."""
    from pnr.electrical import current_width

    raster = model["raster"]
    out = {}
    rep = model["rep"]
    xs, ys = raster.centres()
    for index, la in enumerate(model["layers"]):
        ids = model["node"][la]
        mask = ids >= 0
        if not mask.any():
            continue
        g = model["thick"][la] / model["rho"]
        vl = np.zeros(ids.shape)
        vl[mask] = v[rep[ids[mask]]]
        jx = np.zeros(ids.shape)
        jy = np.zeros(ids.shape)
        fx = (mask[:, :-1] & mask[:, 1:]) * (vl[:, :-1] - vl[:, 1:]) * g / raster.h
        fy = (mask[:-1, :] & mask[1:, :]) * (vl[:-1, :] - vl[1:, :]) * g / raster.h
        jx[:, :-1] += fx / 2
        jx[:, 1:] += fx / 2
        jy[:-1, :] += fy / 2
        jy[1:, :] += fy / 2
        mag = np.hypot(jx, jy) * mask
        k = int(np.argmax(mag))
        jj, ii = divmod(k, ids.shape[1])
        best = dict(
            a_per_mm=round(float(mag.flat[k]), 6),
            at=[round(float(xs[ii]), 4), round(float(ys[jj]), 4)],
            kind="plane",
        )
        for a, b, gt, tla, pa, pb, w in model["track_edges"]:
            if tla != la:
                continue
            amps = abs(float(v[rep[a]] - v[rep[b]])) * gt
            if amps / w > best["a_per_mm"]:
                best = dict(
                    a_per_mm=round(amps / w, 6),
                    at=[round((pa[0] + pb[0]) / 2, 4), round((pa[1] + pb[1]) / 2, 4)],
                    kind="track",
                )
        outer = index in (0, len(model["layers"]) - 1)
        oz = model["thick"][la] / 0.035
        if current_a and current_a > 0:
            w_ipc = current_width(current_a, oz, delta_t_c, external=outer)
            best["ipc_a_per_mm"] = round(current_a / w_ipc, 6)
            best["neck"] = best["a_per_mm"] > current_a / w_ipc + 1e-12
        out[la] = best
    return out


def _warnings(report, budget_mohm, budget_mv) -> List[Dict]:
    """Quantified assumptions (statement, consequence) for the review."""
    out = []
    if report.get("opens"):
        out.append(
            dict(
                statement="sinks %s are not joined to the source by copper"
                % ", ".join(report["opens"]),
                consequence="no DC path: the rail does not reach them",
            )
        )
        return out
    if report.get("status") == "unsolved":
        out.append(
            dict(
                statement="the solve stopped at a relative residual of %.3g after %d iterations"
                % (report["residual"], report["iterations"]),
                consequence="no drop or resistance is reported for this rail",
            )
        )
        return out
    tp = report.get("two_point") or []
    if len(tp) > 1:
        worst = max(tp, key=lambda r: r["r_mohm"])
        all_in = worst["r_mohm"] * report["current_a"]
        out.append(
            dict(
                statement="the load current splits %s among %d sinks" % (report["split"], len(tp)),
                consequence="if all %.3g A flows into %s, its drop is %.3f mV (%.3f mOhm "
                "two-point)" % (report["current_a"], worst["sink"], all_in, worst["r_mohm"]),
            )
        )
    for la, d in sorted((report.get("density") or {}).items()):
        if d.get("neck"):
            out.append(
                dict(
                    statement="%s carries %.3f A/mm at (%.3f, %.3f) (%s)"
                    % (la, d["a_per_mm"], d["at"][0], d["at"][1], d["kind"]),
                    consequence="above the %.3f A/mm of an IPC-2221 trace carrying the whole "
                    "%.3g A (10 C rise): a local neck" % (d["ipc_a_per_mm"], report["current_a"]),
                )
            )
    if report["status"] == "fail":
        out.append(
            dict(
                statement="the rail's copper resistance is %.3f mOhm (%.3f mV at %.3g A)"
                % (report["r_eff_mohm"], report["worst_drop_mv"], report["current_a"]),
                consequence="over its budget (%s)"
                % ("%.3f mOhm" % budget_mohm if budget_mohm is not None else "%.3f mV" % budget_mv),
            )
        )
    return out


def resolve_pads(copper: Dict, refs: Sequence[Tuple[str, str]]) -> List[int]:
    """Indices of ``copper["pads"]`` for ``[(ref, pad)]`` (every match of a pad name
    a footprint repeats); raises on one that is not on the rail."""
    out = []
    for ref, pad in refs:
        hits = [
            k for k, p in enumerate(copper["pads"]) if p.get("ref") == ref and p.get("pad") == pad
        ]
        if not hits:
            raise ValueError("pad %s.%s is not on net %s" % (ref, pad, copper.get("net")))
        out.extend(hits)
    return out


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("copper", nargs="?", help="a rail's copper JSON (pnr.ir_extract --copper)")
    ap.add_argument("--jobs", help="a list of {copper, kwargs} (pnr.ir_extract without numpy)")
    ap.add_argument("--source", action="append", help="REF:PAD")
    ap.add_argument("--sink", action="append", help="REF:PAD")
    ap.add_argument("--current", type=float)
    ap.add_argument("--budget-mohm", type=float)
    ap.add_argument("--h", type=float, default=DEFAULT_H)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.jobs:
        with open(args.jobs, encoding="utf-8") as fh:
            jobs = json.load(fh)
        reports = [solve(job["copper"], **job["kwargs"]) for job in jobs]
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(reports, fh)
        return 0
    if not (args.copper and args.source and args.sink and args.current is not None):
        ap.error("a copper file, --source, --sink and --current (or --jobs)")
    with open(args.copper, encoding="utf-8") as fh:
        copper = json.load(fh)

    def pairs(items):
        return [tuple(s.rsplit(":", 1)) for s in items]

    report = solve(
        copper,
        sources=resolve_pads(copper, pairs(args.source)),
        sinks=resolve_pads(copper, pairs(args.sink)),
        current_a=args.current,
        budget_mohm=args.budget_mohm,
        h=args.h,
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print("%s: %s" % (copper.get("net"), report["status"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
