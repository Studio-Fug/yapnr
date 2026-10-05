"""Graded Yee grids for single-layer microstrip (design §4.2).

An `Axis` is a strictly increasing node list. Primary lengths are the cell sizes; dual lengths
are the means of the two neighbouring cells (half a cell at the ends). A `Grid` combines three
axes, the index of the copper plane (the node plane z = h) and the number of CPML cells on each
side. The ground plane is z = 0; there is no CPML below it.

Field placement (i, j, k are node indices, i+½ a cell centre):

    Ex (i+½, j, k)    Hx (i, j+½, k+½)
    Ey (i, j+½, k)    Hy (i+½, j, k+½)
    Ez (i, j, k+½)    Hz (i+½, j+½, k)

A field component is *staggered* along an axis when it sits at cell centres along it: an E
component along its own axis, an H component along the two others. Every per-edge length,
area and volume follows from that rule: staggered → primary length, otherwise dual length.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

COMPONENTS = ("ex", "ey", "ez", "hx", "hy", "hz")
E_COMPONENTS = ("ex", "ey", "ez")
H_COMPONENTS = ("hx", "hy", "hz")
AXES = ("x", "y", "z")

_REL_TOL = 1e-6


def staggered(comp: str, axis: int) -> bool:
    """True if field `comp` sits at cell centres along `axis` (0, 1, 2)."""
    own = "xyz".index(comp[1])
    return (axis == own) if comp[0] == "e" else (axis != own)


@dataclass(frozen=True)
class Axis:
    """A graded axis: N + 1 strictly increasing nodes (metres)."""

    nodes: np.ndarray

    def __post_init__(self) -> None:
        nodes = np.asarray(self.nodes, dtype=np.float64)
        if nodes.ndim != 1 or nodes.size < 2 or not np.all(np.diff(nodes) > 0):
            raise ValueError("axis nodes must be a strictly increasing 1D list of >= 2 values")
        nodes.setflags(write=False)
        object.__setattr__(self, "nodes", nodes)

    @property
    def n(self) -> int:
        """Number of cells."""
        return self.nodes.size - 1

    @property
    def primary(self) -> np.ndarray:
        """Cell sizes Δ_{i+½} (N values)."""
        return np.diff(self.nodes)

    @property
    def dual(self) -> np.ndarray:
        """Dual lengths Δ_i at the nodes (N + 1 values, half cells at the ends)."""
        p = self.primary
        d = np.empty(self.n + 1)
        d[0] = 0.5 * p[0]
        d[-1] = 0.5 * p[-1]
        d[1:-1] = 0.5 * (p[:-1] + p[1:])
        return d

    @property
    def centers(self) -> np.ndarray:
        """Cell centres x_{i+½} (N values)."""
        return 0.5 * (self.nodes[:-1] + self.nodes[1:])

    @property
    def lo(self) -> float:
        return float(self.nodes[0])

    @property
    def hi(self) -> float:
        return float(self.nodes[-1])

    def node(self, coord: float) -> int:
        """Index of the node at `coord`; raises if `coord` is not on a node."""
        i = int(np.argmin(np.abs(self.nodes - coord)))
        if abs(self.nodes[i] - coord) > _REL_TOL * float(self.primary.min()):
            raise ValueError(f"{coord!r} is not a node (nearest {self.nodes[i]!r})")
        return i

    def nearest_node(self, coord: float) -> int:
        return int(np.argmin(np.abs(self.nodes - coord)))

    def cell(self, coord: float) -> int:
        """Index of the cell containing `coord` (clamped to the axis)."""
        i = int(np.searchsorted(self.nodes, coord, side="right")) - 1
        return min(max(i, 0), self.n - 1)

    def max_ratio(self) -> float:
        """Largest ratio between neighbouring cell sizes."""
        p = self.primary
        if p.size < 2:
            return 1.0
        r = p[1:] / p[:-1]
        return float(np.max(np.maximum(r, 1.0 / r)))


def _grow(first: float, distance: float, ratio: float, max_cell: float) -> list[float]:
    """Cells starting at `first` growing by `ratio` up to `max_cell` until `distance` is covered."""
    cells: list[float] = []
    c = first
    total = 0.0
    while total < distance * (1.0 - 1e-12):
        cells.append(c)
        total += c
        c = min(c * ratio, max_cell)
    return cells


def uniform_nodes(lo: float, hi: float, pitch: float) -> np.ndarray:
    """Nodes lo, lo + pitch, …, hi; (hi − lo) must be an integer multiple of `pitch`."""
    n = int(round((hi - lo) / pitch))
    if n < 1 or abs(n * pitch - (hi - lo)) > _REL_TOL * pitch:
        raise ValueError(f"[{lo}, {hi}] is not an integer number of {pitch} cells")
    return lo + pitch * np.arange(n + 1)


def graded_axis(
    core_lo: float,
    core_hi: float,
    pitch: float,
    lo: float,
    hi: float,
    *,
    max_cell: float,
    ratio: float = 1.25,
    n_pml_lo: int = 0,
    n_pml_hi: int = 0,
    grade_lo: bool = True,
    grade_hi: bool = True,
) -> Axis:
    """A uniform core [core_lo, core_hi] at `pitch`, extended to cover [lo, hi], plus CPML cells.

    Outside the core, cells grow by `ratio` per cell up to `max_cell` (or stay at `pitch` when
    `grade_*` is false, as for an axis that a feed runs along into the CPML). The extension may
    overshoot lo and hi by less than one cell. CPML cells equal the last interior cell.
    """
    if not ratio >= 1.0:
        raise ValueError("ratio must be >= 1")
    core = uniform_nodes(core_lo, core_hi, pitch)
    lo_cells = _grow(
        pitch * ratio if grade_lo else pitch,
        core_lo - lo,
        ratio if grade_lo else 1.0,
        max_cell if grade_lo else pitch,
    )
    hi_cells = _grow(
        pitch * ratio if grade_hi else pitch,
        hi - core_hi,
        ratio if grade_hi else 1.0,
        max_cell if grade_hi else pitch,
    )
    lo_cells += [lo_cells[-1] if lo_cells else pitch] * n_pml_lo
    hi_cells += [hi_cells[-1] if hi_cells else pitch] * n_pml_hi
    left = core_lo - np.cumsum(lo_cells)[::-1] if lo_cells else np.empty(0)
    right = core_hi + np.cumsum(hi_cells) if hi_cells else np.empty(0)
    return Axis(np.concatenate([left, core, right]))


def segment_cells(first: float, length: float, ratio: float, max_cell: float) -> list[float]:
    """Cells growing from `first` by `ratio` up to `max_cell` that cover `length` exactly (the
    grown cells scaled down to fit), so that the segment's far end is a node."""
    cells = _grow(min(first, max_cell), length, ratio, max_cell)
    total = sum(cells)
    return [c * length / total for c in cells]


def breakpoint_axis(
    core_lo: float,
    core_hi: float,
    pitch: float,
    breaks_lo,
    breaks_hi,
    lo: float,
    hi: float,
    *,
    max_cell,
    ratio: float = 1.25,
    n_pml_lo: int = 0,
    n_pml_hi: int = 0,
) -> Axis:
    """A uniform core [core_lo, core_hi] at `pitch`, then on each side cells growing by
    `ratio` up to `max_cell` with a node on every breakpoint (`breaks_lo` below the core,
    `breaks_hi` above it: a board's edges), out to `lo` and `hi` (overshot by less than a
    cell), plus the CPML cells. `max_cell` is a number or a function of the coordinate (the
    largest cell there: finer inside a substrate). Breakpoints closer than half a pitch to the
    core or to each other are an error (they would make tiny cells)."""
    core = uniform_nodes(core_lo, core_hi, pitch)

    def side(start: float, breaks, end: float, sign: float) -> list[float]:
        pts = sorted((abs(b - start) for b in breaks), key=float)
        cells: list[float] = []
        pos = 0.0
        last = pitch
        for d in pts + [None]:
            target = abs(end - start) if d is None else d
            if d is not None and target - pos < 0.5 * pitch - 1e-12:
                raise ValueError(
                    f"breakpoint {start + sign * d!r} lies within half a pitch of the uniform "
                    "core or of another breakpoint"
                )
            if target <= pos + 1e-12:
                continue
            mc = (
                max_cell(start + sign * (pos + 0.5 * (target - pos)))
                if callable(max_cell)
                else max_cell
            )
            mc = max(mc, pitch)
            if d is None:
                seg = _grow(min(last * ratio, mc), target - pos, ratio, mc)
            else:
                seg = segment_cells(last * ratio, target - pos, ratio, mc)
            cells += seg
            pos += sum(seg)
            last = seg[-1]
        return cells

    lo_cells = side(core_lo, [b for b in breaks_lo if b < core_lo - 1e-12], lo, -1.0)
    hi_cells = side(core_hi, [b for b in breaks_hi if b > core_hi + 1e-12], hi, 1.0)
    lo_cells += [lo_cells[-1] if lo_cells else pitch] * n_pml_lo
    hi_cells += [hi_cells[-1] if hi_cells else pitch] * n_pml_hi
    left = core_lo - np.cumsum(lo_cells)[::-1] if lo_cells else np.empty(0)
    right = core_hi + np.cumsum(hi_cells) if hi_cells else np.empty(0)
    return Axis(np.concatenate([left, core, right]))


def substrate_z_axis(
    h: float,
    n_sub: int,
    air: float,
    *,
    dz_max: float,
    ratio: float = 1.25,
    n_pml: int = 8,
) -> tuple[Axis, int]:
    """The vertical axis: `n_sub` uniform substrate cells, graded air, CPML on top.

    Returns the axis and the copper-plane node index k_c (= n_sub, at z = h). The first air
    cell equals the substrate cell; then cells grow by `ratio` up to `dz_max` until `air` metres
    above the copper are covered.
    """
    if n_sub < 1:
        raise ValueError("n_sub must be >= 1")
    dz = h / n_sub
    sub = np.linspace(0.0, h, n_sub + 1)
    air_cells = _grow(dz, air, ratio, max(dz_max, dz))
    air_cells += [air_cells[-1] if air_cells else dz] * n_pml
    nodes = np.concatenate([sub, h + np.cumsum(air_cells)])
    return Axis(nodes), n_sub


@dataclass(frozen=True)
class PMLCells:
    """Number of CPML cells on each side of the domain: none below the ground plane in the
    microstrip domains (`z_lo` = 0), `z_lo` below the air under a finite board (`board`)."""

    x_lo: int = 10
    x_hi: int = 10
    y_lo: int = 10
    y_hi: int = 10
    z_hi: int = 8
    z_lo: int = 0

    def along(self, axis: int) -> tuple[int, int]:
        return [(self.x_lo, self.x_hi), (self.y_lo, self.y_hi), (self.z_lo, self.z_hi)][axis]


@dataclass(frozen=True)
class Grid:
    """A Yee grid: three axes, the copper-plane node index k_c and the CPML cell counts."""

    x: Axis
    y: Axis
    z: Axis
    k_c: int
    pml: PMLCells = field(default_factory=PMLCells)

    def __post_init__(self) -> None:
        if not 0 < self.k_c < self.z.n:
            raise ValueError("the copper plane must be an interior z node")
        for a in range(3):
            lo, hi = self.pml.along(a)
            if lo + hi >= self.axis(a).n:
                raise ValueError(f"CPML fills axis {AXES[a]}")

    def axis(self, a: int) -> Axis:
        return (self.x, self.y, self.z)[a]

    @property
    def n(self) -> tuple[int, int, int]:
        """Cell counts (Nx, Ny, Nz)."""
        return (self.x.n, self.y.n, self.z.n)

    @property
    def cells(self) -> int:
        nx, ny, nz = self.n
        return nx * ny * nz

    def shape(self, comp: str) -> tuple[int, int, int]:
        return tuple(self.axis(a).n + (0 if staggered(comp, a) else 1) for a in range(3))

    def positions(self, comp: str, a: int) -> np.ndarray:
        """Coordinates of the samples of `comp` along axis `a`."""
        ax = self.axis(a)
        return ax.centers if staggered(comp, a) else ax.nodes

    def lengths(self, comp: str, a: int) -> np.ndarray:
        """Per-sample lengths of `comp` along axis `a`: primary if staggered, else dual."""
        ax = self.axis(a)
        return ax.primary if staggered(comp, a) else ax.dual

    def _bcast(self, v: np.ndarray, a: int) -> np.ndarray:
        shape = [1, 1, 1]
        shape[a] = v.size
        return v.reshape(shape)

    def volume(self, comp: str) -> np.ndarray:
        """Dual volume of each `comp` edge: V_e = L_p A_d (E), V_h = L_d A_p (H). Full 3D array."""
        v = np.ones(self.shape(comp))
        for a in range(3):
            v = v * self._bcast(self.lengths(comp, a), a)
        return v

    def edge_length(self, comp: str) -> np.ndarray:
        """Length of each `comp` edge along its own direction, broadcastable to the shape."""
        a = "xyz".index(comp[1])
        return self._bcast(self.lengths(comp, a), a)

    def flat_index(self, comp: str, i, j, k) -> np.ndarray:
        """Flat (C-order) indices of samples (i, j, k) of `comp` (broadcast index arrays)."""
        return np.ravel_multi_index(
            np.broadcast_arrays(np.asarray(i), np.asarray(j), np.asarray(k)), self.shape(comp)
        ).ravel()

    def unravel(self, comp: str, flat: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return np.unravel_index(np.asarray(flat), self.shape(comp))

    def min_spacing(self) -> tuple[float, float, float]:
        return tuple(float(self.axis(a).primary.min()) for a in range(3))

    def courant_dt(self) -> float:
        """The vacuum CFL limit with the smallest spacings: 1/(c √Σ 1/Δ_min²)."""
        from yapnr.rf.constants import C0

        dx, dy, dz = self.min_spacing()
        return 1.0 / (C0 * math.sqrt(1.0 / dx**2 + 1.0 / dy**2 + 1.0 / dz**2))

    def interior_box(self) -> tuple[tuple[float, float], ...]:
        """The domain without the CPML: ((x0, x1), (y0, y1), (z0, z1))."""
        out = []
        for a in range(3):
            ax = self.axis(a)
            lo, hi = self.pml.along(a)
            out.append((float(ax.nodes[lo]), float(ax.nodes[ax.n - hi])))
        return tuple(out)
