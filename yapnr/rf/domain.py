"""A microstrip simulation domain: a design region, line ports on its edges, the grid around.

`Domain` turns a geometric description (stackup, pitch, design rectangle, ports) into the
solver objects: the graded grid (uniform over the design region and the feeds, graded outside,
CPML at the edges; design §4.2), the port geometries, the feed copper, the design-plane probes
and the pixel ↔ design-variable mapping. Problem builders (spec → domain) sit on top of it.

Along an axis with a port on one side, the feed runs at the uniform pitch from the design
region through `src_cells + pml_gap` cells and the CPML to the domain edge. A side without a
port gets a uniform `core_cells` margin, then cells growing by `ratio` up to `max_cell` until
`margin` metres beyond the design region, then the CPML.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.constants import C0
from yapnr.rf.fdtd.monitors import design_plane_probes
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Grid, PMLCells, graded_axis, substrate_z_axis
from yapnr.rf.ports import LinePort, PortGeometry
from yapnr.rf.stackup import Stackup


@dataclass(frozen=True)
class PortSpec:
    """A line port on an edge of the design region: side W/E/S/N, transverse centre (m)."""

    number: int
    side: str
    center: float
    width_cells: int


@dataclass(frozen=True)
class DomainSpec:
    """Geometry of a single-layer microstrip problem (SI units).

    Attributes:
      design: (x0, x1, y0, y1) of the design rectangle; its sides must be whole pitches long.
      f_max: highest frequency of interest; sets the largest cell (λ0/15) when not given.
      margin: extent of the domain beyond the design region on sides without a port
        (default 4h).
      air: air above the copper before the top CPML (default 7h).
    """

    stackup: Stackup
    pitch: float
    n_sub: int
    design: tuple[float, float, float, float]
    ports: tuple[PortSpec, ...] = field(default_factory=tuple)
    f_max: float = 0.0
    meas_cells: int = 9
    src_cells: int = 17
    pml_gap: int = 3
    core_cells: int = 2
    margin: float = 0.0
    air: float = 0.0
    max_cell: float = 0.0
    dz_max: float = 0.0
    ratio: float = 1.25
    n_pml: int = 10
    n_pml_top: int = 8

    def resolved(self) -> "DomainSpec":
        """Defaults filled in."""
        h = self.stackup.h
        f_max = self.f_max or 1.25 * self.stackup.f_ref
        lam = C0 / f_max
        return DomainSpec(
            **{
                **self.__dict__,
                "f_max": f_max,
                "margin": self.margin or 4.0 * h,
                "air": self.air or 7.0 * h,
                "max_cell": self.max_cell or lam / 15.0,
                "dz_max": self.dz_max or lam / 15.0,
            }
        )


class Domain:
    """Grid, ports and design window of a `DomainSpec`."""

    def __init__(self, spec: DomainSpec):
        spec = spec.resolved()
        self.spec = spec
        st, d = spec.stackup, spec.pitch
        x0, x1, y0, y1 = spec.design
        sides = {p.side for p in spec.ports}
        feed = (spec.src_cells + spec.pml_gap) * d
        axes = []
        pml = []
        for lo_side, hi_side, (c0, c1) in (("W", "E", (x0, x1)), ("S", "N", (y0, y1))):
            core_lo = c0 - (feed if lo_side in sides else spec.core_cells * d)
            core_hi = c1 + (feed if hi_side in sides else spec.core_cells * d)
            lo = core_lo if lo_side in sides else c0 - spec.margin
            hi = core_hi if hi_side in sides else c1 + spec.margin
            ax = graded_axis(
                core_lo,
                core_hi,
                d,
                lo,
                hi,
                max_cell=spec.max_cell,
                ratio=spec.ratio,
                n_pml_lo=spec.n_pml,
                n_pml_hi=spec.n_pml,
                grade_lo=lo_side not in sides,
                grade_hi=hi_side not in sides,
            )
            axes.append(ax)
            pml.append((spec.n_pml, spec.n_pml))
        z, kc = substrate_z_axis(
            st.h, spec.n_sub, spec.air, dz_max=spec.dz_max, ratio=spec.ratio, n_pml=spec.n_pml_top
        )
        cells = PMLCells(pml[0][0], pml[0][1], pml[1][0], pml[1][1], spec.n_pml_top)
        self.grid = Grid(axes[0], axes[1], z, kc, cells)
        g = self.grid
        self.window = (g.x.node(x0), g.x.node(x1), g.y.node(y0), g.y.node(y1))
        refs = {"W": x0, "E": x1, "S": y0, "N": y1}
        self.ports: list[PortGeometry] = [
            LinePort(
                p.number,
                p.side,
                p.center,
                p.width_cells,
                refs[p.side],
                spec.meas_cells,
                spec.src_cells,
            ).on(g)
            for p in spec.ports
        ]
        self.feed = np.zeros(g.n[:2], dtype=bool)
        for p in self.ports:
            self.feed[p.feed_pixels()] = True

    @property
    def design_shape(self) -> tuple[int, int]:
        i0, i1, j0, j1 = self.window
        return (i1 - i0, j1 - j0)

    def pixels(self, g_design: np.ndarray | None = None, g_feed: float | None = None):
        """Per-pixel sheet conductance of the whole copper plane: the feeds at G_max (or
        `g_feed`), the design window from `g_design`, zero elsewhere."""
        g = np.zeros(self.grid.n[:2])
        g[self.feed] = self.spec.stackup.g_max if g_feed is None else g_feed
        if g_design is not None:
            i0, i1, j0, j1 = self.window
            g[i0:i1, j0:j1] = g_design
        return g

    def structure(self, g_design: np.ndarray | None = None) -> Structure:
        s = Structure(self.grid, self.spec.stackup)
        s.set_pixels(self.pixels(g_design))
        return s

    def design_probes(self, prefix: str = "design"):
        i0, i1, j0, j1 = self.window
        return design_plane_probes(self.grid, i0, i1, j0, j1, prefix)

    def window_pixels(self, full: np.ndarray) -> np.ndarray:
        """The design-window part (…, ni, nj) of a whole-plane pixel array (…, Nx, Ny)."""
        i0, i1, j0, j1 = self.window
        return full[..., i0:i1, j0:j1]

    def line_spec(self, width_cells: int, dt: float):
        """The calibration spec of a feed of `width_cells` on this domain's cross-section."""
        from yapnr.rf.ports import LineSpec

        s = self.spec
        return LineSpec(
            stackup=s.stackup,
            pitch=s.pitch,
            n_sub=s.n_sub,
            width_cells=width_cells,
            air=s.air,
            dz_max=s.dz_max,
            dt=dt,
            n_pml=s.n_pml,
            n_pml_top=s.n_pml_top,
            ratio=s.ratio,
            lateral_max_cell=s.max_cell,
        )


def hj_width_cells(stackup: Stackup, pitch: float, z0: float = 50.0) -> int:
    """The whole number of cells whose Hammerstad–Jensen impedance is closest to z0."""
    from yapnr.rf.stackup import hammerstad_jensen

    best = min(
        range(1, 200),
        key=lambda n: abs(hammerstad_jensen(n * pitch, stackup.h, stackup.er)[0] - z0),
    )
    return int(best)


def default_dt(grid: Grid, courant: float = 0.95) -> float:
    return courant * grid.courant_dt()
