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
from yapnr.rf.fdtd.monitors import FluxBox, design_plane_probes
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Grid, PMLCells, graded_axis, substrate_z_axis
from yapnr.rf.ports import LinePort, PortGeometry
from yapnr.rf.stackup import Stackup


def check_inside_cpml(grid: Grid, node_box, cells: int = 2) -> None:
    """Raise unless every face of `node_box` lies at least `cells` cells from the CPML (the
    faces on the domain's outer boundary, z = 0 on a PEC ground, excepted)."""
    for a, (lo, hi) in enumerate(node_box):
        n_lo, n_hi = grid.pml.along(a)
        n = grid.axis(a).n
        if (lo > 0 or n_lo > 0) and lo < n_lo + cells:
            raise ValueError(
                f"the radiation box comes within {lo - n_lo} cells of the {'xyz'[a]}-low CPML "
                f"(at least {cells}): more margin or air"
            )
        if hi > n - n_hi - cells:
            raise ValueError(
                f"the radiation box comes within {n - n_hi - hi} cells of the {'xyz'[a]}-high "
                f"CPML (at least {cells}): more margin or air"
            )


@dataclass
class ClosedBox:
    """A closed flux box and, per line port, the face its feed crosses (design §25.2), and the
    full transverse planes at those faces' nodes (`planes`, port → one-face `FluxBox`, set by
    `Problem`: the guided wave's modal projection needs the whole cross-section of the mode,
    which extends beyond the box)."""

    box: FluxBox
    feeds: dict = field(default_factory=dict)
    planes: dict = field(default_factory=dict)

    @property
    def probes(self):
        return self.box.probes + [p for n in sorted(self.planes) for p in self.planes[n].probes]

    @property
    def node_box(self):
        return self.box.node_box

    def power(self, dft):
        """Outward Poynting flux through the closed box (M,)."""
        return self.box.power(dft)


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

    def structure(
        self, g_design: np.ndarray | None = None, copper: np.ndarray | None = None
    ) -> Structure:
        """The structure with the design window's conductances `g_design`; with `copper` (the
        window's copper fraction) the copper-edge correction is on (`edges`)."""
        s = Structure(self.grid, self.spec.stackup)
        s.set_pixels(self.pixels(g_design))
        if copper is not None:
            s.set_edge_correction(self.copper(copper))
        return s

    def copper(self, c_design: np.ndarray | None = None) -> np.ndarray:
        """The copper fraction of every pixel of the plane: 1 on the feeds, the design window
        from `c_design`, 0 elsewhere."""
        c = self.feed.astype(np.float64)
        if c_design is not None:
            i0, i1, j0, j1 = self.window
            c[i0:i1, j0:j1] = c_design
        return c

    def design_probes(self, prefix: str = "design"):
        i0, i1, j0, j1 = self.window
        return design_plane_probes(self.grid, i0, i1, j0, j1, prefix)

    def radiation_box(
        self,
        *,
        clearance: int = 2,
        offset: float | None = None,
        height: float | None = None,
        name: str = "rad",
    ) -> "ClosedBox":
        """The radiated-power box (design §25.2): closed by the ground (faces x−, x+, y−, y+ and
        z+ from z = 0; the PEC ground carries no flux), without feed windows, enclosing the
        design region by `clearance` cells on every side and above the copper; `offset` (m)
        beyond the design region and `height` (m) above the copper move the faces out further
        (snapped to the nearest node). Every face lies at least two cells from the CPML. The
        faces the feeds cross are listed per port (`ClosedBox.feeds`): there the guided wave
        is separated modally (`ports.ModalPlane`), and everything else that leaves the box is
        the non-guided power (radiation and, on the infinite substrate, the surface wave)."""
        g = self.grid
        c = int(clearance)
        if c < 1:
            raise ValueError("the radiation box needs a clearance of at least one cell")
        i0, i1, j0, j1 = self.window
        x0, x1, y0, y1 = self.spec.design
        kc = g.k_c
        h = self.spec.stackup.h
        lo_x, hi_x, lo_y, hi_y, top = i0 - c, i1 + c, j0 - c, j1 + c, kc + c
        if offset is not None:
            lo_x = min(lo_x, g.x.nearest_node(x0 - offset))
            hi_x = max(hi_x, g.x.nearest_node(x1 + offset))
            lo_y = min(lo_y, g.y.nearest_node(y0 - offset))
            hi_y = max(hi_y, g.y.nearest_node(y1 + offset))
        if height is not None:
            top = max(top, g.z.nearest_node(h + height))
        node_box = ((lo_x, hi_x), (lo_y, hi_y), (0, top))
        check_inside_cpml(g, node_box)
        box = FluxBox(g, name, node_box, faces=("x-", "x+", "y-", "y+", "z+"))
        tags = {"W": "x-", "E": "x+", "S": "y-", "N": "y+"}
        feeds = {}
        for p in self.ports:
            tag = tags[p.port.side]
            face = box.faces[["x-", "x+", "y-", "y+", "z+"].index(tag)]
            outside = face.node > p.i_src + 1 if p.sign > 0 else face.node < p.i_src - 1
            if not outside:
                raise ValueError(
                    f"port {p.port.number}: the radiation box must leave the port's source "
                    "outside (a smaller offset or longer feeds)"
                )
            feeds[p.port.number] = face
        return ClosedBox(box, feeds)

    def window_pixels(self, full: np.ndarray) -> np.ndarray:
        """The design-window part (…, ni, nj) of a whole-plane pixel array (…, Nx, Ny)."""
        i0, i1, j0, j1 = self.window
        return full[..., i0:i1, j0:j1]

    def line_spec(
        self,
        width_cells: int,
        dt: float,
        edge_correction: bool = False,
        port_source: str = "static",
    ):
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
            src_gap_cells=max(1, s.src_cells - s.meas_cells - 1),
            edge_correction=edge_correction,
            port_source=port_source,
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
