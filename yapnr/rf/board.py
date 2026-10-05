"""Finite boards: a substrate block with its ground in free space, or on an infinite PEC ground
(design §26.1).

The infinite-substrate microstrip domain (`domain`) has no closed surface in homogeneous air,
so it has no far field. A board model puts the whole board inside the domain:

- **free** (`ground` an outline with keepouts): the substrate is a block of the board's outline
  and thickness, the ground a PEC sheet on the node plane z = 0 (the copper at z = h above it,
  `stackup.h`; the block reaches `thickness` − h below the ground), present under the ground
  outline less the keepouts (a ground edge is PEC where at least one of its two pixels is
  ground, openEMS's convention for sheets), air on every side and CPML on all six;
- **infinite** (`ground: "infinite"`): the substrate block stands on the PEC floor z = 0 (the
  domain's boundary), air beside and above it, CPML on five sides; its far field follows by
  image theory (`farfield`, upper half space).

The lateral axes are uniform at the design pitch over the design region, the ports and the
board's fixed copper, then graded (`mesh.breakpoint_axis`) to `max_cell` with nodes on the
board's, the ground's and the keepouts' edges, then `air` beyond the board to the CPML. The
z axis has the design's substrate cells between the ground and the copper, graded cells below
the ground (inside the block, at most λ_d/15) and in the air.

Ports are lumped (`ports.LumpedPort`, Ez columns from the ground to the copper on the grid
nodes inside the port's rectangle) and lie inside the Huygens box (`huygens_box`), which
encloses the whole board in air (six faces, or five from z = 0 over the infinite ground), so
its flux is exactly the radiated power. The copper is the design window (the optimizer's
sheet) and the board's fixed copper (`copper`, at G_max).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from yapnr.rf.constants import C0
from yapnr.rf.domain import check_inside_cpml
from yapnr.rf.fdtd.monitors import FluxBox, design_plane_probes
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Axis, Grid, PMLCells, breakpoint_axis, segment_cells
from yapnr.rf.ports import LumpedPort
from yapnr.rf.stackup import Stackup

Rect = tuple  # ((x0, x1), (y0, y1)) in metres


@dataclass(frozen=True)
class LumpedPortSpec:
    """A lumped port: number, rectangle (m; a degenerate side is a line of nodes) and R."""

    number: int
    x: tuple
    y: tuple
    ohms: float = 50.0


@dataclass(frozen=True)
class BoardGeometry:
    """A board model's geometry (SI units; see the module doc)."""

    stackup: Stackup
    pitch: float
    n_sub: int
    design: tuple  # (x0, x1, y0, y1)
    outline: Rect
    thickness: float
    ground: object  # "infinite" or a Rect (the ground's outline)
    keepouts: tuple = ()
    ports: tuple = ()
    copper: tuple = ()
    f_max: float = 0.0
    air: float = 0.0
    max_cell: float = 0.0
    core_cells: int = 2
    ratio: float = 1.25
    n_pml: int = 8
    clearance: int = 2
    # Ports must stand on the ground (the spec's rule); off for the free-space dipole tests.
    require_ground: bool = True

    @property
    def infinite(self) -> bool:
        return isinstance(self.ground, str)

    def resolved(self) -> "BoardGeometry":
        st = self.stackup
        f_max = self.f_max or 1.25 * st.f_ref
        lam0 = C0 / f_max
        lam_d = lam0 / math.sqrt(st.er)
        return BoardGeometry(
            **{
                **self.__dict__,
                "f_max": f_max,
                "air": self.air or lam0 / 4.0,
                "max_cell": self.max_cell or min(lam_d / 15.0, lam0 / 20.0),
            }
        )


def _inside(rect, x, y, tol: float = 1e-9) -> np.ndarray:
    """Points (pixel centres) inside or on a rectangle (m), to `tol` (rounding of the nodes)."""
    (x0, x1), (y0, y1) = (sorted(rect[0]), sorted(rect[1]))
    return (x >= x0 - tol) & (x <= x1 + tol) & (y >= y0 - tol) & (y <= y1 + tol)


class BoardDomain:
    """Grid, materials, ports and design window of a `BoardGeometry` (the interface of
    `domain.Domain`: `grid`, `window`, `ports`, `feed`, `design_shape`, `pixels`,
    `structure`, `copper`, `design_probes`, `window_pixels`)."""

    def __init__(self, geom: BoardGeometry):
        geom = geom.resolved()
        self.spec = self.geom = geom
        st, d = geom.stackup, geom.pitch
        x0, x1, y0, y1 = geom.design
        (bx0, bx1), (by0, by1) = (sorted(geom.outline[0]), sorted(geom.outline[1]))
        self.infinite = geom.infinite
        # The uniform core: the design region, the ports and the fixed copper, on whole
        # pitches from the design region's corner, `core_cells` around, inside the board.
        xs = (
            [x0, x1] + [v for p in geom.ports for v in p.x] + [v for r in geom.copper for v in r[0]]
        )
        ys = (
            [y0, y1] + [v for p in geom.ports for v in p.y] + [v for r in geom.copper for v in r[1]]
        )
        for v, lo, hi in [(v, bx0, bx1) for v in xs] + [(v, by0, by1) for v in ys]:
            if not lo - 1e-12 <= v <= hi + 1e-12:
                raise ValueError("the design region, ports and copper must lie on the board")

        def core(c0, c1, vals, lo, hi):
            a = c0 - d * math.ceil((c0 - min(vals)) / d - 1e-9) - geom.core_cells * d
            b = c1 + d * math.ceil((max(vals) - c1) / d - 1e-9) + geom.core_cells * d
            # Not beyond the board's edge (the edge then must be a node of the core).
            if a < lo:
                a = c0 - d * round((c0 - lo) / d)
                if abs(a - lo) > 1e-9 * d:
                    raise ValueError(
                        f"the board edge {lo * 1e3:g} mm is within {geom.core_cells} pitches of "
                        "the copper: put it on the design pitch grid or further out"
                    )
            if b > hi:
                b = c1 + d * round((hi - c1) / d)
                if abs(b - hi) > 1e-9 * d:
                    raise ValueError(
                        f"the board edge {hi * 1e3:g} mm is within {geom.core_cells} pitches of "
                        "the copper: put it on the design pitch grid or further out"
                    )
            return a, b

        cx = core(x0, x1, xs, bx0, bx1)
        cy = core(y0, y1, ys, by0, by1)
        breaks_x, breaks_y = [bx0, bx1], [by0, by1]
        if not self.infinite:
            (gx0, gx1), (gy0, gy1) = (sorted(geom.ground[0]), sorted(geom.ground[1]))
            breaks_x += [gx0, gx1]
            breaks_y += [gy0, gy1]
        for k in geom.keepouts:
            breaks_x += list(k[0])
            breaks_y += list(k[1])

        def outside(vals, c0, c1):
            """Breakpoints outside the core (inside it, the pixels decide by their centres)."""
            return sorted({round(v, 12) for v in vals if v < c0 - 1e-12 or v > c1 + 1e-12})

        lam_d = C0 / geom.f_max / math.sqrt(st.er)

        def max_cell_at(lo, hi):
            def f(v):  # finer inside the board's substrate
                return min(geom.max_cell, lam_d / 15.0) if lo <= v <= hi else geom.max_cell

            return f

        n = geom.n_pml
        ax_x = breakpoint_axis(
            cx[0], cx[1], d, outside(breaks_x, *cx), outside(breaks_x, *cx),
            bx0 - geom.air, bx1 + geom.air, max_cell=max_cell_at(bx0, bx1), ratio=geom.ratio,
            n_pml_lo=n, n_pml_hi=n,
        )  # fmt: skip
        ax_y = breakpoint_axis(
            cy[0], cy[1], d, outside(breaks_y, *cy), outside(breaks_y, *cy),
            by0 - geom.air, by1 + geom.air, max_cell=max_cell_at(by0, by1), ratio=geom.ratio,
            n_pml_lo=n, n_pml_hi=n,
        )  # fmt: skip
        z, kg, kc, kb = self._z_axis(geom)
        self.k_g, self.k_b = kg, kb
        pml = PMLCells(n, n, n, n, n, 0 if self.infinite else n)
        self.grid = Grid(ax_x, ax_y, z, kc, pml)
        g = self.grid
        self.window = (g.x.node(x0), g.x.node(x1), g.y.node(y0), g.y.node(y1))
        xc, yc = g.x.centers[:, None], g.y.centers[None, :]
        self.board_mask = _inside(geom.outline, xc, yc)
        if self.infinite:
            self.ground_mask = np.ones(g.n[:2], bool)
        else:
            self.ground_mask = _inside(geom.ground, xc, yc)
            for k in geom.keepouts:
                self.ground_mask &= ~_inside(k, xc, yc)
        self.feed = np.zeros(g.n[:2], bool)  # the fixed copper outside the design window
        for r in geom.copper:
            self.feed |= _inside(r, xc, yc)
        i0, i1, j0, j1 = self.window
        self.feed[i0:i1, j0:j1] = False
        self.ports = []
        for p in geom.ports:
            nodes = self._port_nodes(p)
            lp = LumpedPort(p.number, nodes, p.ohms, ground=kg).on(g)
            lp.number = p.number
            self.ports.append(lp)

    # -- grid -------------------------------------------------------------------------------

    def _z_axis(self, geom: BoardGeometry):
        """(axis, k_g, k_c, k_b): the ground at z = 0, the copper at h, the block's bottom."""
        st = geom.stackup
        h = st.h
        dz = h / geom.n_sub
        lam0 = C0 / geom.f_max
        dz_air = max(dz, min(geom.max_cell, lam0 / 20.0))
        dz_sub = max(dz, min(dz_air, lam0 / math.sqrt(st.er) / 15.0))
        n = geom.n_pml
        above = segment_cells(dz * geom.ratio, geom.air, geom.ratio, dz_air)
        # Grow above freely (the top need not be exact): extend to cover `air`.
        top = list(np.cumsum(above) + h)
        nodes_up = [h + 0.0] + top
        nodes_up += [nodes_up[-1] + above[-1] * (k + 1) for k in range(n)]
        core = list(np.linspace(0.0, h, geom.n_sub + 1))
        if geom.infinite:
            nodes = np.array(core[:-1] + nodes_up)
            return Axis(nodes), 0, geom.n_sub, 0
        below = []
        depth = geom.thickness - h
        if depth > 1e-12:
            below = segment_cells(dz * geom.ratio, depth, geom.ratio, dz_sub)
        last = below[-1] if below else dz
        air = segment_cells(last * geom.ratio, geom.air, geom.ratio, dz_air)
        cells = below + air + [air[-1]] * n
        down = -np.cumsum(cells)
        nodes = np.concatenate([down[::-1], core[:-1], nodes_up])
        kg = len(cells)
        kb = kg - len(below)
        return Axis(nodes), kg, kg + geom.n_sub, kb

    def _port_nodes(self, p: LumpedPortSpec) -> tuple:
        g = self.grid
        (x0, x1), (y0, y1) = sorted(p.x), sorted(p.y)
        tol = 1e-6 * self.geom.pitch
        ii = np.nonzero((g.x.nodes >= x0 - tol) & (g.x.nodes <= x1 + tol))[0]
        jj = np.nonzero((g.y.nodes >= y0 - tol) & (g.y.nodes <= y1 + tol))[0]
        if ii.size == 0 or jj.size == 0:
            raise ValueError(f"port {p.number}: its rectangle holds no grid node")
        for i in ii if self.geom.require_ground else ():
            for j in jj:
                if not self.ground_mask[max(i - 1, 0) : i + 1, max(j - 1, 0) : j + 1].any():
                    raise ValueError(f"port {p.number}: its column does not land on the ground")
        return tuple((float(g.x.nodes[i]), float(g.y.nodes[j])) for i in ii for j in jj)

    def port_pixels(self, port) -> list:
        """The copper-plane pixels around each node of a lumped port's columns."""
        out = []
        nx, ny = self.grid.n[:2]
        for i, j in port.columns:
            for a in (i - 1, i):
                for b in (j - 1, j):
                    if 0 <= a < nx and 0 <= b < ny:
                        out.append((a, b))
        return out

    # -- the Domain interface ---------------------------------------------------------------

    @property
    def design_shape(self) -> tuple[int, int]:
        i0, i1, j0, j1 = self.window
        return (i1 - i0, j1 - j0)

    def pixels(self, g_design: np.ndarray | None = None, g_feed: float | None = None):
        g = np.zeros(self.grid.n[:2])
        g[self.feed] = self.geom.stackup.g_max if g_feed is None else g_feed
        if g_design is not None:
            i0, i1, j0, j1 = self.window
            g[i0:i1, j0:j1] = g_design
        return g

    def copper(self, c_design: np.ndarray | None = None) -> np.ndarray:
        c = self.feed.astype(np.float64)
        if c_design is not None:
            i0, i1, j0, j1 = self.window
            c[i0:i1, j0:j1] = c_design
        return c

    def cells(self) -> tuple[np.ndarray, np.ndarray]:
        """(εr, σ) per cell: the substrate block, air elsewhere."""
        g, st = self.grid, self.geom.stackup
        nx, ny, nz = g.n
        er = np.ones((nx, ny, nz))
        sig = np.zeros((nx, ny, nz))
        block = self.board_mask[:, :, None] & (np.arange(nz) >= self.k_b)[None, None, :]
        block &= (np.arange(nz) < g.k_c)[None, None, :]
        er[block] = st.er
        sig[block] = st.sigma_sub
        return er, sig

    def structure(
        self, g_design: np.ndarray | None = None, copper: np.ndarray | None = None
    ) -> Structure:
        s = Structure(self.grid, self.geom.stackup, cells=self.cells())
        if not self.infinite:
            self.set_ground(s)
        s.set_pixels(self.pixels(g_design))
        if copper is not None:
            s.set_edge_correction(self.copper(copper))
        return s

    def set_ground(self, s: Structure) -> None:
        """PEC on the ground plane's Ex and Ey edges next to a ground pixel."""
        g, kg = self.grid, self.k_g
        m = self.ground_mask
        nx, ny = m.shape
        ex = np.zeros((nx, ny + 1), bool)
        ex[:, :-1] |= m
        ex[:, 1:] |= m
        ey = np.zeros((nx + 1, ny), bool)
        ey[:-1, :] |= m
        ey[1:, :] |= m
        for comp, mask in (("ex", ex), ("ey", ey)):
            i, j = np.nonzero(mask)
            s.set_pec(comp, g.flat_index(comp, i, j, np.full(i.size, kg)))

    def design_probes(self, prefix: str = "design"):
        i0, i1, j0, j1 = self.window
        return design_plane_probes(self.grid, i0, i1, j0, j1, prefix)

    def window_pixels(self, full: np.ndarray) -> np.ndarray:
        i0, i1, j0, j1 = self.window
        return full[..., i0:i1, j0:j1]

    def huygens_box(self, clearance: int | None = None, name: str = "nf") -> FluxBox:
        """The closed box in air around the whole board (`clearance` cells beyond its outline,
        below its block and above the copper); five faces from z = 0 over an infinite ground."""
        g = self.grid
        c = int(clearance if clearance is not None else self.geom.clearance)
        (bx0, bx1), (by0, by1) = (sorted(self.geom.outline[0]), sorted(self.geom.outline[1]))
        node_box = [
            (g.x.node(bx0) - c, g.x.node(bx1) + c),
            (g.y.node(by0) - c, g.y.node(by1) + c),
            (0 if self.infinite else self.k_b - c, g.k_c + c),
        ]
        check_inside_cpml(g, node_box)
        faces = ("x-", "x+", "y-", "y+", "z+") if self.infinite else None
        return FluxBox(g, name, tuple(node_box), faces=faces)

    def describe(self) -> dict:
        g = self.grid
        return {
            "model": "infinite ground" if self.infinite else "free board",
            "ground_node": int(self.k_g),
            "block_bottom_node": int(self.k_b),
            "domain_mm": [
                [float(a.nodes[0] * 1e3), float(a.nodes[-1] * 1e3)] for a in (g.x, g.y, g.z)
            ],
            "max_cell_mm": [float(a.primary.max() * 1e3) for a in (g.x, g.y, g.z)],
            "ground_pixels": int(self.ground_mask.sum()),
        }
