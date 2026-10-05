"""A spec as a solver problem: grid, ports, calibration, design parameterization, evaluation.

`Problem(spec)` builds, at the spec's resolution (or `refine` times finer):

- the `Domain` (graded grid, line ports, feeds, design window) and the time step;
- the port calibrations Z_c(ω), k(ω) (cached on disk), and the port widths: a port without a
  width gets the whole number of cells whose calibrated Re Z_c at the band centre is closest to
  50 Ω, starting from Hammerstad–Jensen's width;
- the radiated-power box when a requirement needs it, and probes on the lumped resistors an
  `absorbed` requirement names (on every lumped resistor when a `loss` requirement needs their
  power);
- the objective groups (`objectives`) and the design chain x → ρ̄ (`design.parameterization`):
  the material grid with fixed port pads (the feed width, two pixels deep), fixed regions,
  mirror symmetry and the exterior ring (1 on the feeds), the conic filter and the length scale
  from the rules.

`evaluate(ρ̄)` runs one forward simulation per excitation and one adjoint per objective group
and returns every f_{g,m} with ∂f_{g,m}/∂ρ̄ (design §8.3, steps 2–5); `sweep(ρ̄)` returns the full
S-matrix over a frequency grid (every port excited, no adjoint).
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.adjoint import gradient, wirtinger
from yapnr.rf.constants import C0, ETA0
from yapnr.rf.design.filters import ConicFilter
from yapnr.rf.design.lengthscale import LengthScale, conic_radius
from yapnr.rf.design.material_grid import MaterialGrid
from yapnr.rf.design.parameterization import Parameterization
from yapnr.rf.domain import Domain, DomainSpec, PortSpec, hj_width_cells
from yapnr.rf.fdtd.dtft import conductance_factor
from yapnr.rf.fdtd.dtft import decimation as dtft_decimation
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.monitors import Probe
from yapnr.rf.fdtd.native_kernel import choose as choose_backend
from yapnr.rf.fdtd.native_kernel import provenance as native_status
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import (
    branch_admittance_derivative,
    reactive_range,
    reactive_sheet,
    sheet_conductance,
    sheet_conductance_derivative,
    sheet_reactance_derivative,
)
from yapnr.rf.numerics import re_conj_product
from yapnr.rf.objectives import build_groups, group_values, violations
from yapnr.rf.ports import LineCalibration, ModalPlane, calibrate_line
from yapnr.rf.spec import FixedRegion, Spec

PAD_DEPTH = 2  # pixels of fixed feed copper inside the design window at each port (§5.1)


def default_cells(h: float, pitch: float) -> tuple[int, int]:
    """(meas_cells, src_cells) with the measurement plane ≥ 6h behind the reference plane and
    the source about 6h further (17 and 33 cells on S1 at 0.3 mm).

    The design had 3h (9 and 17 cells): there the near fields of the discontinuity and of the
    source reach the V/I samples, and on the divider |S21 − S12| was 0.009–0.013 and |S21|
    0.04 dB lower than with 6h, where |S21 − S12| is 0.003–0.006 (docs/rf-inverse-design.md,
    "Accuracy"). The longer feeds cost about a third more cells on a three-port."""
    n = int(math.ceil(6.0 * h / pitch - 1e-9))
    return n, n + n - 1


def domain_spec(
    spec: Spec, widths: dict, *, refine: int = 1, n_sub: int | None = None
) -> DomainSpec:
    """The `DomainSpec` of a spec (SI units) with the given port widths (cells at this
    resolution); `refine` divides the pitch, `n_sub` overrides the substrate cells."""
    st = spec.stackup.to_stackup()
    g = spec.grid
    pitch = g.pitch_mm * 1e-3 / refine
    meas, src = default_cells(st.h, pitch)
    meas = g.meas_cells * refine if g.meas_cells else meas
    src = g.src_cells * refine if g.src_cells else src
    x0, x1, y0, y1 = (v * 1e-3 for v in spec.design_region)
    ports = tuple(PortSpec(p.n, p.side, p.at_mm * 1e-3, int(widths[p.n])) for p in spec.ports)
    f_max = g.f_max_ghz * 1e9 if g.f_max_ghz else source_pulse(spec).f_top
    return DomainSpec(
        stackup=st,
        pitch=pitch,
        n_sub=int(n_sub or g.substrate_cells * refine),
        design=(x0, x1, y0, y1),
        ports=ports,
        f_max=f_max,
        meas_cells=meas,
        src_cells=src,
        pml_gap=g.pml_gap * refine,
        core_cells=g.core_cells * refine,
        margin=(g.margin_mm or 0.0) * 1e-3,
        air=(g.air_mm or 0.0) * 1e-3,
        dz_max=(g.dz_max_mm or 0.0) * 1e-3,
        ratio=g.ratio,
        n_pml=g.pml_cells,
        n_pml_top=g.pml_top_cells,
    )


def board_geometry(spec: Spec, *, refine: int = 1, n_sub: int | None = None):
    """The `board.BoardGeometry` of a board spec (SI units)."""
    from yapnr.rf.board import BoardGeometry, LumpedPortSpec

    b = spec.board
    st = spec.stackup.to_stackup()
    g = spec.grid
    f_max = g.f_max_ghz * 1e9 if g.f_max_ghz else source_pulse(spec).f_top
    if b.infinite:
        ground, keepouts = "infinite", ()
    else:
        gx = b.ground.x_mm if b.ground.x_mm is not None else b.x_mm
        gy = b.ground.y_mm if b.ground.y_mm is not None else b.y_mm
        ground = (tuple(v * 1e-3 for v in gx), tuple(v * 1e-3 for v in gy))
        keepouts = tuple(k.si() for k in b.ground.keepout)
    return BoardGeometry(
        stackup=st,
        pitch=g.pitch_mm * 1e-3 / refine,
        n_sub=int(n_sub or g.substrate_cells * refine),
        design=tuple(v * 1e-3 for v in spec.design_region),
        outline=(tuple(v * 1e-3 for v in b.x_mm), tuple(v * 1e-3 for v in b.y_mm)),
        thickness=(b.thickness_mm or spec.stackup.h_mm) * 1e-3,
        ground=ground,
        keepouts=keepouts,
        ports=tuple(
            LumpedPortSpec(
                p.n,
                tuple(v * 1e-3 for v in p.x_mm),
                tuple(v * 1e-3 for v in p.y_mm),
                50.0 if p.ohms is None else p.ohms,
            )
            for p in spec.ports
        ),
        copper=tuple(r.si() for r in b.copper),
        f_max=f_max,
        air=(b.air_mm or 0.0) * 1e-3,
        max_cell=(b.max_cell_mm or 0.0) * 1e-3,
        core_cells=g.core_cells * refine,
        ratio=g.ratio,
        n_pml=b.pml_cells,
        clearance=(spec.radiation.clearance_cells if spec.radiation else 2) * refine,
    )


def _strip_on_nodes(spec: Spec, port, width: int, pitch_mm: float) -> bool:
    """True when a strip of `width` cells centred at the port lies between grid nodes (the
    grid's nodes are whole pitches from the design region's edges)."""
    x0, _, y0, _ = spec.design_region
    lo = y0 if port.side in ("W", "E") else x0
    edge = (port.at_mm - lo) / pitch_mm - 0.5 * width
    return abs(edge - round(edge)) < 1e-6


def source_pulse(spec: Spec) -> GaussianPulse:
    """The forward pulse covering every band a requirement uses (design §4.5)."""
    used = {r.band for r in spec.requirements}
    lo = min(spec.bands[b].frequencies().min() for b in used)
    hi = max(spec.bands[b].frequencies().max() for b in used)
    for b in used:
        lo = min(lo, spec.bands[b].lo_ghz * 1e9)
        hi = max(hi, spec.bands[b].hi_ghz * 1e9)
    return GaussianPulse.for_band(lo, hi)


def sweep_frequencies(pulse: GaussianPulse, points: int) -> np.ndarray:
    """`points` frequencies over the pulse's −20 dB band (Hz)."""
    lo = max(pulse.f_center - pulse.f_half_width, 0.05 * pulse.f_center)
    return np.linspace(lo, pulse.f_center + pulse.f_half_width, points)


def filter_radius(spec: Spec) -> float:
    """The conic radius (m): the optimizer's setting, else from the rules, else 1.5 pitches."""
    if spec.optimizer.filter_radius_mm is not None:
        return spec.optimizer.filter_radius_mm * 1e-3
    r = spec.rules
    if r.active:
        w = r.min_width_mm or r.min_space_mm
        s = r.min_space_mm or r.min_width_mm
        return max(conic_radius(w), conic_radius(s)) * 1e-3
    return 1.5 * spec.grid.pitch_mm * 1e-3


@dataclass
class Evaluation:
    """One evaluation of a design ρ̄ (design §8.3, steps 2–5)."""

    values: np.ndarray  # (K,) f_k for every active (group, frequency)
    grads: np.ndarray | None  # (K, ni, nj) ∂f_k/∂ρ̄
    keys: list  # (group name, frequency Hz) per k
    s: np.ndarray  # (M, N, N) S-parameters at the objective frequencies (internal convention)
    eta: dict  # port → (M,) radiated fraction
    phi: dict  # requirement label → (M,) violation (NaN outside its band)
    steps: dict = field(default_factory=dict)
    converged: bool = True
    wall_s: float = 0.0

    @property
    def t(self) -> float:
        return float(np.max(self.values))


class Problem:
    """The solver side of a spec (see the module doc)."""

    def __init__(
        self,
        spec: Spec,
        *,
        backend: str | None = None,
        dtype=None,
        threads: int | None = None,
        cache_dir: str | None = None,
        calibrations: dict | None = None,
        exact: bool = False,
        refine: int = 1,
        n_sub: int | None = None,
        interpolation: str | None = None,
        log=None,
    ):
        self.spec = spec
        self.log = log or (lambda *_: None)
        sv = spec.solver
        self.edge_correction = bool(sv.edge_correction)
        self.exact = exact
        # Explicit arguments, then $YAPNR_RF_BACKEND / $YAPNR_RF_DTYPE, then the spec
        # (`fdtd.native_kernel.choose`: "auto" is native where its library loads, else numpy;
        # exact problems run float64, native or numpy, the same bits).
        self.backend, self.dtype = choose_backend(backend, dtype, sv.backend, sv.dtype, exact=exact)
        # The spec's threads; each backend applies its own cap and $YAPNR_RF_THREADS
        # (`engine.thread_count` for torch, `native_kernel.thread_count` for the native pool).
        self.threads = int(threads or sv.threads)
        self.tol = 1e-12 if exact else sv.tol
        self.adjoint_tol = 1e-10 if exact else sv.adjoint_tol
        self.cache_dir = cache_dir
        self.refine = int(refine)
        self.stackup = spec.stackup.to_stackup()
        self.freqs = spec.objective_frequencies()
        self.omega = 2.0 * np.pi * self.freqs
        self.pulse = source_pulse(spec)
        self.sweep_freqs = sweep_frequencies(self.pulse, sv.sweep_points)
        cal_f = np.unique(np.concatenate([self.freqs, self.sweep_freqs]))
        self.cal_omega = 2.0 * np.pi * cal_f
        self._cal_by_width: dict[int, LineCalibration] = {}
        self._given_cal = calibrations

        self.board = spec.board is not None
        if self.board:
            from yapnr.rf.board import BoardDomain

            widths = {}
            self.domain = BoardDomain(board_geometry(spec, refine=refine, n_sub=n_sub))
        else:
            widths = self._resolve_widths(n_sub)
            self.domain = Domain(domain_spec(spec, widths, refine=refine, n_sub=n_sub))
        self.widths = widths
        dom = self.domain
        self.grid = dom.grid
        self.dt = self._time_step(self.grid)
        if self.board:
            self.ports = {pg.number: pg for pg in dom.ports}
            self.cal = {}
        else:
            self.ports = {pg.port.number: pg for pg in dom.ports}
            self.cal = {n: self._calibration(widths[n]) for n in self.ports}
        self.port_extraction = "lumped" if self.board else sv.port_extraction
        self.box = None
        self.far = None
        self.table = None
        self.targets: dict = {}
        self.warnings: list = []
        if self.board:
            from yapnr.rf.domain import ClosedBox
            from yapnr.rf.farfield import FarField

            clearance = spec.radiation.clearance_cells if spec.radiation is not None else 2
            huygens = dom.huygens_box(clearance * self.refine)
            self.box = ClosedBox(huygens)
            self.far = FarField(huygens, image=dom.infinite)
            self._pattern_setup()
        elif spec.radiation is not None:
            rb = spec.radiation
            self.box = dom.radiation_box(
                clearance=rb.clearance_cells * self.refine,
                offset=None if rb.offset_mm is None else rb.offset_mm * 1e-3,
                height=None if rb.height_mm is None else rb.height_mm * 1e-3,
            )
        # The modal planes of the line ports (design §25.1): the ports' waves with "modal"
        # extraction, and the modes that separate the guided waves on the box's feed faces.
        self.modal: dict = {}
        if not self.board and (self.port_extraction == "modal" or self.box is not None):
            for n, pg in self.ports.items():
                others = [q for m, q in self.ports.items() if m != n]
                self.modal[n] = ModalPlane(
                    pg,
                    self.stackup,
                    self.dt,
                    edge_correction=self.edge_correction,
                    neighbours=others,
                )
        if self.box is not None and not self.board:
            for n, face in self.box.feeds.items():
                self.box.planes[n] = self.modal[n].plane(face.node, f"rad_p{n}")
        self.port_probes = [p for n in sorted(self.ports) for p in self.ports[n].probes]
        if self.port_extraction == "modal":
            self.port_probes += [p for n in sorted(self.modal) for p in self.modal[n].probes]
        self.box_probes = self.box.probes if self.box is not None else []
        self.design_probes = dom.design_probes()
        self.watched = [p.name for p in self.port_probes + self.box_probes]
        self.groups = build_groups(spec, self.freqs)
        self.excitations = sorted({g.excitation for g in self.groups})
        self.g_min, self.g_max = self.stackup.g_min, self.stackup.g_max
        self.g_d = spec.optimizer.damping / ETA0
        self.interpolation = interpolation or spec.optimizer.interpolation
        if self.interpolation not in ("resistive", "reactive"):
            raise ValueError(f"unknown interpolation {self.interpolation!r}")
        self.x_range = reactive_range(self.g_max)
        self.omega_ref = 2.0 * np.pi * self.stackup.f_ref
        # The forward runs of the last few designs evaluated (key → {port: run}), most recent
        # last: the nominal design and every robust variant of the current x and of an
        # adaptive move's trial point, whose forward runs the next iteration reuses.
        self._fwd_cache: OrderedDict[str, dict] = OrderedDict()
        self.fwd_cache_size = 2 * (1 + len(spec.optimizer.eta_variants))
        self.edge_probes = []
        if self.edge_correction:
            from yapnr.rf.edges import design_probes as edge_design_probes

            self.edge_probes = edge_design_probes(self.grid, dom.window)
        self.sim = Simulation(
            self.grid,
            dom.structure(copper=np.zeros(dom.design_shape) if self.edge_correction else None),
            dt=self.dt,
            backend=self.backend,
            dtype=self.dtype,
            threads=self.threads,
        )
        self.backend = self.sim.backend  # "numpy" when the native library is missing
        self.log(f"FDTD backend: {self.solver_label()}")
        # Probes on the resistors an `absorbed` requirement names: P = ½ c_ω Σ_e σ_e V_e |Ê_e|²
        # over the part's edges (`dissipated_power` with the part's own σ only), name →
        # (probe, ½ σ_e V_e).
        self.lumped_probes: dict = {}
        absorbed = {r.element for r in spec.requirements if r.quantity == "absorbed"}
        self.loss_ports = {r.ports[0] for r in spec.requirements if r.quantity == "loss"}
        if self.loss_ports:
            absorbed = {el.name for el in spec.lumped}
        for el in spec.lumped:
            comp, idx, n_series, m_parallel = self.lumped_edges(el)
            self.sim.structure.add_resistor(comp, idx, el.ohms, n_series, m_parallel)
            if el.name in absorbed:
                vol = self.grid.volume(comp).ravel()[idx]
                length = np.broadcast_to(self.grid.edge_length(comp), self.grid.shape(comp))
                length = length.ravel()[idx]
                sigma = n_series * length / (m_parallel * el.ohms * (vol / length))
                probe = Probe(f"lumped_{el.name}", comp, idx)
                self.lumped_probes[el.name] = (probe, 0.5 * sigma * vol)
        if self.lumped_probes:
            extra = [pr for pr, _ in self.lumped_probes.values()]
            self.port_probes = self.port_probes + extra
            self.watched = self.watched + [pr.name for pr in extra]
        if self.board:
            for pg in self.ports.values():
                pg.apply(self.sim.structure)
        if spec.lumped or self.board:
            self.sim.update_materials()
        for a in self.assumptions():
            self.log(f"assumption ({a['code']}): {a['message']}")
        self.pitch = spec.grid.pitch_mm * 1e-3 / refine
        self.filter = ConicFilter(filter_radius(spec), self.pitch)
        self.material = self._material_grid(self.filter.ring_width)
        self.param = Parameterization(self.material, self.filter, spec.optimizer.eta)
        self.lengthscale = None
        if spec.rules.active:
            w = spec.rules.min_width_mm or spec.rules.min_space_mm
            s = spec.rules.min_space_mm or spec.rules.min_width_mm
            self.lengthscale = LengthScale.from_rules(
                w * 1e-3, s * 1e-3, self.pitch, radius=self.filter.radius
            )

    # -- setup ----------------------------------------------------------------------------------

    def _time_step(self, grid) -> float:
        """Δt = courant · Δt_CFL, or with the copper-edge correction its bound
        (`edges.stable_dt`)."""
        courant = self.spec.grid.courant
        if self.edge_correction:
            from yapnr.rf.edges import stable_dt

            return stable_dt(grid, self.stackup.er, courant)
        return courant * grid.courant_dt()

    def _calibration(self, width: int) -> LineCalibration:
        if self._given_cal is not None:
            return self._given_cal[width] if width in self._given_cal else self._given_cal["*"]
        if width not in self._cal_by_width:
            t0 = time.perf_counter()
            line = self._line_domain().line_spec(
                width, self.dt, self.edge_correction, self.spec.solver.port_source
            )
            self._cal_by_width[width] = calibrate_line(
                line,
                self.cal_omega,
                backend=self.backend if self.backend in ("torch", "native") else "numpy",
                dtype=np.float64,
                cache_dir=self.cache_dir,
            )
            self.log(f"calibrated a {width}-cell feed in {time.perf_counter() - t0:.1f} s")
        return self._cal_by_width[width]

    def _line_domain(self) -> Domain:
        return self._provisional

    def _resolve_widths(self, n_sub) -> dict:
        spec = self.spec
        pitch = spec.grid.pitch_mm * 1e-3 / self.refine
        hj = hj_width_cells(self.stackup, pitch)
        widths = {p.n: (p.width_cells * self.refine if p.width_cells else hj) for p in spec.ports}
        self._provisional = Domain(domain_spec(spec, widths, refine=self.refine, n_sub=n_sub))
        self.dt = self._time_step(self._provisional.grid)
        if all(p.width_cells for p in spec.ports):
            return widths
        fc = 2.0 * np.pi * self.pulse.f_center
        auto = [p for p in spec.ports if not p.width_cells]

        def z_at_fc(w):
            return float(self._calibration(w).at(np.array([fc]))[0][0].real)

        def fits(w):
            """The strip of w cells lies between grid nodes at every auto port (an odd width
            needs a centre on a cell centre, an even one a centre on a node)."""
            return all(_strip_on_nodes(spec, p, w, pitch * 1e3) for p in auto)

        fitting = sorted((w for w in range(1, 2 * hj + 3) if fits(w)), key=lambda w: abs(w - hj))
        if not fitting:
            raise ValueError("no feed width fits the port positions on the grid")
        first = fitting[0]
        z0 = z_at_fc(first)
        wider = z0 >= 50.0  # a wider strip lowers Z_c
        nxt = [w for w in fitting if (w > first if wider else w < first)]
        cands = {first: z0}
        if nxt:
            cands[nxt[0]] = z_at_fc(nxt[0])
        best = min(cands, key=lambda w: (abs(cands[w] - 50.0), w))
        self.log(f"feed width {best} cells (Z_c at f_c: {cands})")
        for p in spec.ports:
            if not p.width_cells:
                widths[p.n] = best
        return widths

    def _material_grid(self, ring_width: int) -> MaterialGrid:
        spec, dom = self.spec, self.domain
        i0, i1, j0, j1 = dom.window
        ni, nj = i1 - i0, j1 - j0
        r = ring_width
        nx, ny = dom.feed.shape
        ring = np.zeros((ni + 2 * r, nj + 2 * r))
        ilo, ihi = max(0, i0 - r), min(nx, i1 + r)
        jlo, jhi = max(0, j0 - r), min(ny, j1 + r)
        ring[ilo - (i0 - r) : ihi - (i0 - r), jlo - (j0 - r) : jhi - (j0 - r)] = dom.feed[
            ilo:ihi, jlo:jhi
        ]
        fixed = np.zeros((ni, nj), bool)
        value = np.zeros((ni, nj))
        depth = PAD_DEPTH * self.refine
        if self.board:
            # A lumped port inside the window: the pixels around its columns are copper.
            for pg in dom.ports:
                for a, b in dom.port_pixels(pg):
                    if i0 <= a < i1 and j0 <= b < j1:
                        fixed[a - i0, b - j0] = True
                        value[a - i0, b - j0] = 1.0
        for pg in [] if self.board else dom.ports:
            along = (
                slice(pg.i_ref, pg.i_ref + depth)
                if pg.sign > 0
                else slice(pg.i_ref - depth, pg.i_ref)
            )
            across = slice(pg.ta, pg.tb)
            sl = (along, across) if pg.axis == 0 else (across, along)
            sub = (
                slice(sl[0].start - i0, sl[0].stop - i0),
                slice(sl[1].start - j0, sl[1].stop - j0),
            )
            fixed[sub] = True
            value[sub] = 1.0
        xc = dom.grid.x.centers[i0:i1] * 1e3
        yc = dom.grid.y.centers[j0:j1] * 1e3
        regions = list(spec.fixed)
        for el in spec.lumped:
            # The part's body stays void, its pads copper.
            regions.append(FixedRegion(tuple(el.x_mm), tuple(el.y_mm), 0.0))
            for px, py in el.pads():
                regions.append(FixedRegion(px, py, 1.0))
        for fr in regions:
            inside = (
                (xc[:, None] >= min(fr.x_mm))
                & (xc[:, None] <= max(fr.x_mm))
                & (yc[None, :] >= min(fr.y_mm))
                & (yc[None, :] <= max(fr.y_mm))
            )
            fixed |= inside
            value = np.where(inside, fr.value, value)
        return MaterialGrid(
            (ni, nj),
            fixed=fixed,
            fixed_value=value,
            ring=ring,
            ring_width=r,
            symmetry=spec.symmetry,
        )

    def lumped_edges(self, el) -> tuple[str, np.ndarray, int, int]:
        """(component, flat indices, edges in series, columns in parallel) of a lumped
        resistor: the copper-plane edges along its axis inside its body (the pixels whose centre
        lies in the body), including the node lines on the body's sides."""
        g = self.grid
        xc, yc = g.x.centers * 1e3, g.y.centers * 1e3
        (x0, x1), (y0, y1) = sorted(el.x_mm), sorted(el.y_mm)
        ii = np.nonzero((xc >= x0) & (xc <= x1))[0]
        jj = np.nonzero((yc >= y0) & (yc <= y1))[0]
        if ii.size == 0 or jj.size == 0:
            raise ValueError(f"lumped {el.name}: the body covers no pixel")
        kc = g.k_c
        if el.axis == "y":
            nodes = np.arange(ii[0], ii[-1] + 2)  # x node lines across the body
            cells = jj
            i, j = np.meshgrid(nodes, cells, indexing="ij")
            idx = g.flat_index("ey", i.ravel(), j.ravel(), np.full(i.size, kc))
        else:
            nodes = np.arange(jj[0], jj[-1] + 2)
            cells = ii
            i, j = np.meshgrid(cells, nodes, indexing="ij")
            idx = g.flat_index("ex", i.ravel(), j.ravel(), np.full(i.size, kc))
        comp = "ey" if el.axis == "y" else "ex"
        return comp, np.asarray(idx), int(cells.size), int(nodes.size)

    # -- design → solver ------------------------------------------------------------------------

    @property
    def design_shape(self) -> tuple[int, int]:
        return self.domain.design_shape

    def conductance(self, rho_bar):
        return sheet_conductance(rho_bar, self.g_min, self.g_max, self.g_d)

    def reactive(self, rho_bar):
        """(R, L) per pixel of the reactive interpolation (Ω and H per square)."""
        return reactive_sheet(
            rho_bar, self.g_max, self.omega_ref, self.spec.optimizer.reactive_damping
        )

    def set_design(self, rho_bar: np.ndarray) -> None:
        rho_bar = np.asarray(rho_bar, dtype=np.float64)
        if rho_bar.shape != self.design_shape:
            raise ValueError(f"design must be {self.design_shape}, got {rho_bar.shape}")
        dom = self.domain
        if self.interpolation == "reactive":
            r, ind = self.reactive(rho_bar)
            g = dom.pixels(1.0 / r)
            lp = dom.pixels(ind, g_feed=0.0)
            self.sim.structure.set_sheet(g, lp, dt=self.dt)
        else:
            self.sim.structure.set_pixels(dom.pixels(self.conductance(rho_bar)))
        if self.edge_correction:
            self.sim.structure.set_edge_correction(dom.copper(rho_bar))
        self.sim.update_materials()

    def design_gradient(self, gr, rho_bar: np.ndarray) -> np.ndarray:
        """∂F_m/∂ρ̄ (M, ni, nj) of the design window from an adjoint `Gradient`."""
        dom = self.domain
        if self.interpolation == "reactive":
            kr, ki = (dom.window_pixels(k) for k in gr.pixel_kernel(self.grid))
            r, lp = self.reactive(rho_bar)
            dl = sheet_reactance_derivative(rho_bar, *self.x_range) / self.omega_ref
            dl = np.where(lp > 0.0, dl, 0.0)
            dr = self.spec.optimizer.reactive_damping * self.omega_ref * dl
            yr, yi = branch_admittance_derivative(r, lp, dr, dl, gr.omega, self.dt)
            shape = (gr.omega.size,) + lp.shape
            # ∂(Δz a)/∂ρ̄ = c_ω ∂Y_d/∂ρ̄ (a = c_ω Y_d/Δz on the sheet's edges).
            c = conductance_factor(gr.omega, self.dt)[:, None, None]
            return 2.0 * c * (kr * yr.reshape(shape) - ki * yi.reshape(shape)) + (
                self._edge_gradient(gr)
            )
        dg = sheet_conductance_derivative(rho_bar, self.g_min, self.g_max, self.g_d)
        return dom.window_pixels(gr.pixels(self.grid)) * dg[None] + self._edge_gradient(gr)

    def _edge_gradient(self, gr):
        """The copper-edge correction's part of ∂F_m/∂ρ̄ (the window's copper fraction is ρ̄)."""
        if not self.edge_correction:
            return 0.0
        return self.domain.window_pixels(gr.edge_pixels(self.sim.structure))

    def _decimation(self) -> int:
        return 1 if self.exact else dtft_decimation(self.pulse.f_top, self.dt)

    def forward(self, port: int, *, omega=None, design: bool = True):
        """The forward run with port `port` excited; DTFTs of the ports, the box and (when
        `design`) the design plane."""
        omega = self.omega if omega is None else np.asarray(omega)
        probes = self.port_probes + self.box_probes
        if design:
            probes = probes + self.design_probes + self.edge_probes
        stop = StopRule(
            tol=self.tol,
            f_lo=float(omega.min()) / (2.0 * np.pi),
            max_steps=self.spec.solver.max_steps,
            probes=tuple(self.watched),
        )
        return self.sim.run(
            self.port_sources(port), probes, omega, stop, decimation=self._decimation()
        )

    def port_sources(self, port: int) -> list:
        """The forward sources of port `port` (`spec.solver.port_source`; a lumped port's
        Thevenin source)."""
        if self.board:
            return [self.ports[port].source(self.pulse, self.dt)]
        return self.ports[port].sources(
            self.pulse,
            self.dt,
            self.stackup,
            kind=self.spec.solver.port_source,
            edge_correction=self.edge_correction,
        )

    def port_waves(self, n: int, dft, omega, excitation: int | None = None):
        """(a, b) of port `n` at its reference plane (`solver.port_extraction`; a lumped port's
        power waves, with its source's spectrum when it is the `excitation`)."""
        if self.board:
            vs = 0.0
            if n == excitation:
                vs = self._source_spectrum(omega)
                if not isinstance(dft[self.ports[n].v_probe.name], np.ndarray):
                    import torch

                    vs = torch.as_tensor(vs)
            return self.ports[n].waves(dft, vs)
        if self.port_extraction == "modal":
            return self.modal[n].waves(dft, omega)
        return sparams.port_waves(self.ports[n], self.cal[n], dft, omega)

    def _source_spectrum(self, omega) -> np.ndarray:
        from yapnr.rf.ports import source_dtft

        key = np.asarray(omega, dtype=np.float64).tobytes()
        cache = self.__dict__.setdefault("_vs_cache", {})
        if key not in cache:
            cache[key] = source_dtft(self.pulse, omega, self.dt)
        return cache[key]

    def wave_power(self, n: int, a, omega):
        """½|a|² as a power (W): exact for modal and lumped waves, times the calibration's
        power factor for V/I waves (`sparams.incident_power`)."""
        if self.port_extraction in ("modal", "lumped"):
            return 0.5 * abs(a) ** 2
        return sparams.incident_power(a, self.cal[n], omega)

    # -- board models: patterns -----------------------------------------------------------------

    def _pattern_setup(self) -> None:
        """The far field's direction table (every pattern requirement's directions, and the
        quadrature when a requirement needs it), the targets at the quadrature and the
        assumptions' warnings (design §26.3)."""
        from yapnr.rf.patterns import PatternRequirement, direction_table

        spec = self.spec
        reqs = [r for r in spec.requirements if isinstance(r, PatternRequirement)]
        self.pattern_ports = {r.excitation for r in reqs if r.quantity != "efficiency"}
        if not reqs:
            return
        quad = None
        needs_quad = any(
            r.quantity == "shape"
            or any(isinstance(r.spec.get(k), str) for k in ("directions", "front", "back"))
            for r in reqs
        )
        if needs_quad:
            quad = self.far.quadrature(self.omega, frame=spec.frame)
        self.table = direction_table(
            spec,
            None if quad is None else quad[0],
            None if quad is None else quad[1],
            spec.frame,
        )
        if self.far.image and np.any(self.table.dirs[:, 2] < -1e-9):
            raise ValueError(
                "over an infinite ground the far field exists above it only: every direction "
                "of a pattern requirement needs z ≥ 0"
            )
        if quad is not None:
            for name, t in spec.patterns.items():
                q, renorm = t.normalized(quad[0], quad[1], spec.frame)
                self.targets[name] = q
                if renorm > 0.01:
                    self.warnings.append(
                        {
                            "code": "target_renormalized",
                            "value": renorm,
                            "message": f"target {name!r} integrates to 4π·{1 + renorm:.3f} "
                            "before normalization (with its floor)",
                        }
                    )
                ka = float(np.max(self.omega)) / C0 * self.far.radius
                limit = ka * ka + 2 * ka
                peak = 4 * math.pi * float(np.max(q))
                if peak > limit:
                    self.warnings.append(
                        {
                            "code": "target_beyond_harrington",
                            "value": peak / limit,
                            "message": f"target {name!r} peaks at {10 * math.log10(peak):.1f} "
                            f"dBi, above Harrington's (ka)² + 2ka = {10 * math.log10(limit):.1f} "
                            f"dBi for the box radius {self.far.radius * 1e3:.1f} mm",
                        }
                    )
        floors = {}
        for r in reqs:
            j = r.excitation
            kinds = [
                q.spec.get("kind", "realized") if q.quantity != "efficiency" else "efficiency"
                for q in reqs
                if q.excitation == j and q.quantity in ("gain", "efficiency")
            ]
            if r.quantity == "shape" or (
                r.quantity == "gain" and r.spec.get("kind") == "directivity"
            ):
                floors[j] = any(k in ("realized", "gain", "efficiency") for k in kinds)
        for j, ok in floors.items():
            if not ok:
                self.warnings.append(
                    {
                        "code": "pattern_without_gain_floor",
                        "value": j,
                        "message": f"port {j}: a shape or directivity requirement without a "
                        "realized-gain or efficiency floor: lossy gray copper can buy the "
                        "pattern by absorbing (design §26.3)",
                    }
                )

    def far_quantities(self, dft, port: int, omega, waves) -> dict:
        """The far-field quantities of excitation `port` (torch): F (M, D, 3) at the direction
        table's directions, P_rad (the Huygens box's flux), P_inc, P_acc, U's factor and the
        targets (`patterns.phi_pattern`)."""
        import torch

        from yapnr.rf.farfield import intensity_factor

        a, b = waves[port]
        p_inc = 0.5 * abs(a) ** 2
        out = {
            "p_inc": p_inc,
            "p_acc": p_inc - 0.5 * abs(b) ** 2,
            "p_rad": self.box.power(dft),
            "u_factor": torch.as_tensor(intensity_factor(omega), dtype=torch.float64),
            "targets": self.targets,
            "floors": {n: t.floor_db for n, t in self.spec.patterns.items()},
        }
        if self.table is not None and self.table.dirs.shape[0]:
            out["F"] = self.far.vectors(dft, omega, self.table.dirs)
        return out

    def nonguided(self, dft, port: int, omega=None) -> tuple:
        """(η, P_inc) of excitation `port` from the closed box (design §25.2): the outward flux
        plus, where each feed crosses the box, the guided wave's net modal power into the box
        (`ModalPlane.net_power`, projected on the whole transverse plane at the face's node),
        over the incident modal power there:

            η = [Φ_out + Σ_p P_net,in,mode(face_p)] / P_inc,mode(face_j)

        The closed box's discrete Poynting identity (Φ_out = −P_diss inside) makes this
        1 − Σ_i |S_ij|² − P_diss/P_inc with the S-parameters at the faces, whatever the box's
        size: the part of the guided mode that passes beside or above the box is counted
        where it enters. On the infinite substrate η is the non-guided fraction: radiation and
        the substrate's surface wave together (design §25.4)."""
        omega = self.omega if omega is None else np.asarray(omega)
        total = self.box.power(dft)
        p_inc = None
        for n, plane in self.box.planes.items():
            mp = self.modal[n]
            face = plane.faces[0]
            fms = mp.face_modes(face, omega)
            ap, am = mp.amplitudes(face, fms, dft)
            sign = mp.pg.sign
            total = total + sign * ModalPlane.net_power(fms, ap, am)
            if n == port:
                inc = ap if sign > 0 else am
                p_inc = 0.5 * abs(inc) ** 2
        if p_inc is None:
            raise ValueError(f"port {port}: no feed face on the radiation box")
        return total / p_inc, p_inc

    def quantities(self, dft, port: int, omega=None, *, far: bool = True) -> dict:
        """Waves of every port, S_ij for excitation j = `port`, and η_j (numpy or torch); on a
        board model with pattern requirements (and `far`) the far-field quantities too."""
        omega = self.omega if omega is None else np.asarray(omega)
        waves = {n: self.port_waves(n, dft, omega, port) for n in self.ports}
        a_j = waves[port][0]
        out = {"waves": waves, "s": {}, "eta": {}}
        for n, (_, b) in waves.items():
            out["s"][(n, port)] = b / a_j
        if self.lumped_probes or port in self.loss_ports or self.board:
            p_inc = self.wave_power(port, a_j, omega)
        if self.board:
            # The Huygens box lies in air around the whole board: its flux is the radiated
            # power, η the total efficiency.
            out["eta"][port] = self.box.power(dft) / p_inc
            if (
                far
                and self.table is not None
                and port
                in self.pattern_ports
                | {r.excitation for r in self.spec.requirements if r.quantity == "efficiency"}
            ):
                out["far"] = {port: self.far_quantities(dft, port, omega, waves)}
                out["table"] = self.table
        elif self.box is not None:
            out["eta"][port] = self.nonguided(dft, port, omega)[0]
        out["absorbed"] = {
            (name, port): p / p_inc for name, p in self.lumped_power(dft, omega).items()
        }
        out["loss"] = {}
        if port in self.loss_ports:
            # The net power into the device, less the net power out of the other ports and into
            # every lumped resistor: −Σ_n (|b_n|² − |a_n|²) (V/I waves: times each port's power
            # factor; the idle ports' residual incident waves included), less the resistors'
            # shares.
            lost = 0.0
            for n, (a, b) in waves.items():
                net = self.wave_power(n, b, omega) - self.wave_power(n, a, omega)
                lost = lost - net / p_inc
            for v in out["absorbed"].values():
                lost = lost - v
            out["loss"][port] = lost
        return out

    def lumped_power(self, dft, omega=None) -> dict:
        """Power (W, (M,)) dissipated in each probed lumped resistor (`lumped_probes`)."""
        omega = self.omega if omega is None else np.asarray(omega)
        out = {}
        if not self.lumped_probes:
            return out
        c = conductance_factor(omega, self.dt)
        for name, (probe, w) in self.lumped_probes.items():
            e = dft[probe.name]
            p2 = re_conj_product(e, e)
            if isinstance(p2, np.ndarray):
                out[name] = c * (p2 * w).sum(-1)
            else:
                import torch

                ct = torch.as_tensor(c, dtype=torch.float64)
                out[name] = ct * (p2 * torch.as_tensor(w, dtype=torch.float64)).sum(-1)
        return out

    def objective_quantities(self, dft, port: int, omega=None) -> dict:
        """`quantities` as the objectives judge them: with `optimizer.reference_ohm` (one-port
        specs) the reflection renormalized from the feed's Z_c to that reference, Γ' = (Z − R)/
        (Z + R) with Z = Z_c (1 + Γ)/(1 − Γ), as `sparams.renormalize` does for the validator."""
        q = self.quantities(dft, port, omega)
        ref = self.spec.optimizer.reference_ohm
        if ref is None:
            return q
        omega = self.omega if omega is None else np.asarray(omega)
        g = q["s"][(port, port)]
        zc = np.asarray(self.cal[port].at(omega)[0]).real
        if not isinstance(g, np.ndarray):
            import torch

            zc = torch.as_tensor(zc, dtype=torch.float64)
        z = zc * (1.0 + g) / (1.0 - g)
        q["s"] = dict(q["s"])
        q["s"][(port, port)] = (z - float(ref)) / (z + float(ref))
        return q

    def objective_units(self, objective: str = "spec") -> list:
        """The objective's parts as (name, excitation, fn, rows): `fn(quantities)` → a real
        (M,) torch tensor; `rows` the frequency indices whose entries are values of the
        epigraph (one each), or None for a scalar (the sum of the entries, one value).

        "spec": one part per objective group, f_{g,m} at its active frequencies (design §9).
        "radiation": one scalar per lower bound on a radiated fraction, log(1 + R̄) − log η̄
        over its band (`spec.OptimizerSpec.epoch_objectives`)."""
        import torch

        if objective == "spec":
            tau = self.spec.optimizer.tau
            return [
                (
                    g.name,
                    g.excitation,
                    lambda q, g=g: group_values(g, q, self.freqs, tau),
                    np.nonzero(g.frequencies_active)[0],
                )
                for g in self.groups
            ]
        if objective != "radiation":
            raise ValueError(f"unknown objective {objective!r}")
        out = []
        for r in self.spec.requirements:
            if r.quantity != "radiated" or r.bound != "min":
                continue
            j = r.ports[0]
            band = torch.as_tensor(self.spec.band_mask(r.band, self.freqs))

            def fn(q, j=j, band=band):
                refl = torch.mean(torch.abs(q["s"][(j, j)][band]) ** 2)
                rad = torch.mean(q["eta"][j][band])
                f = torch.log(1.0 + refl) - torch.log(rad)
                return torch.cat([f.reshape(1), torch.zeros(self.freqs.size - 1, dtype=f.dtype)])

            out.append((f"radiation{j}", j, fn, None))
        return out

    def evaluate(
        self,
        rho_bar: np.ndarray,
        *,
        gradients: bool = True,
        objective: str = "spec",
        frequency_scale: float = 1.0,
    ) -> Evaluation:
        """Every f_{g,m} and (when `gradients`) ∂f_{g,m}/∂ρ̄ for the design ρ̄ (with
        `objective="radiation"`, the radiation objective's values instead, `objective_units`).

        `frequency_scale` (frequency continuation, `optimizer.epoch_frequency_scale`) solves at
        the objective frequencies times the factor and judges the results against the
        requirements of the nominal frequencies (the masks and limits are not moved).

        The forward runs of the last few designs evaluated are kept (with the design-plane
        DTFTs; `fwd_cache_size`, two per robust variant), so evaluating the same ρ̄ again with
        gradients (the point a conservative or adaptive MMA step accepted, with each of its
        variants) runs only the adjoints."""
        import torch

        t0 = time.perf_counter()
        rho_bar = np.ascontiguousarray(rho_bar, dtype=np.float64)
        key = hashlib.sha256(rho_bar.tobytes()).hexdigest() + f":{float(frequency_scale)!r}"
        cache = self._fwd_cache.pop(key, {})
        omega = self.omega * float(frequency_scale)
        self._fwd_cache[key] = cache
        while len(self._fwd_cache) > self.fwd_cache_size:
            self._fwd_cache.popitem(last=False)
        self.set_design(rho_bar)
        n_ports = len(self.ports)
        m = self.freqs.size
        s = np.full((m, n_ports, n_ports), np.nan + 0j)
        eta: dict = {}
        phis: dict = {}
        values, grads, keys = [], [], []
        steps: dict = {"forward": {}, "adjoint": {}}
        converged = True
        units = self.objective_units(objective)
        stop_adj = StopRule(
            tol=self.adjoint_tol,
            f_lo=float(self.freqs.min() * frequency_scale),
            max_steps=self.spec.solver.max_steps,
        )
        for j in sorted({u[1] for u in units}):
            if j in cache:
                fwd = cache[j]
                steps["forward"][j] = 0
            else:
                fwd = cache[j] = self.forward(j, omega=omega, design=True)
                steps["forward"][j] = fwd.steps
            converged &= fwd.converged
            q = {k: torch.as_tensor(v) for k, v in fwd.dft.items() if k in self.watched}
            qty = self.objective_quantities(q, j, omega)
            for (i, jj), v in qty["s"].items():
                s[:, i - 1, jj - 1] = v.detach().numpy()
            for jj, v in qty["eta"].items():
                eta[jj] = v.detach().numpy().astype(np.float64)
            phis.update(violations(self.spec, qty, self.freqs, excitation=j))
            for name, _, part, rows in [u for u in units if u[1] == j]:

                def fn(dft, part=part, j=j):
                    return part(self.objective_quantities(dft, j, omega))

                f, wg = wirtinger(fn, fwd.dft, self.watched)
                gp = None
                if gradients:
                    dec = 1 if self.exact else "auto"
                    gr = gradient(
                        self.sim,
                        fwd,
                        self.port_probes + self.box_probes,
                        wg,
                        self.design_probes,
                        stop_adj,
                        decimation=dec,
                        edge_probes=self.edge_probes,
                    )
                    steps["adjoint"][name] = gr.adjoint.steps
                    converged &= gr.adjoint.converged
                    gp = self.design_gradient(gr, rho_bar)
                if rows is None:
                    # A scalar: its Wirtinger rows are its derivatives at every frequency, so
                    # the gradient is the sum of the per-frequency recombinations.
                    values.append(float(np.sum(f)))
                    keys.append((name, float(np.mean(self.freqs))))
                    if gp is not None:
                        grads.append(gp.sum(axis=0))
                    continue
                for mm in rows:
                    values.append(float(f[mm]))
                    keys.append((name, float(self.freqs[mm])))
                    if gp is not None:
                        grads.append(gp[mm])
        phis = dict(sorted(phis.items(), key=lambda kv: int(kv[0].split(":")[0])))
        return Evaluation(
            values=np.array(values),
            grads=np.array(grads) if gradients else None,
            keys=keys,
            s=s,
            eta=eta,
            phi=phis,
            steps=steps,
            converged=bool(converged),
            wall_s=time.perf_counter() - t0,
        )

    def sweep(self, rho_bar: np.ndarray, freqs=None, ports=None) -> dict:
        """The full S-matrix (F, N, N) over `freqs` (default: the sweep grid) with every port
        (or those in `ports`) excited; also Z_c (F, N), η (port → (F,)) when there is a box and
        the probed resistors' shares of the incident power ("R1/P2" → (F,), `absorbed`) and the
        lost fractions ("loss2", for the ports a `loss` requirement names).
        Internal e^{−iωt} convention; `sparams.to_engineering` converts.

        With every port excited, S = B A⁻¹ from the wave matrices (A_nj, B_nj: the incident and
        outgoing waves at port n with port j excited), which does not assume that the idle
        ports see no incident wave (the residual reflection of their feeds); `s_naive` is
        b_i/a_j and `idle_incident` the largest |a_k/a_j| (k ≠ j) per frequency."""
        freqs = self.sweep_freqs if freqs is None else np.asarray(freqs, dtype=np.float64)
        omega = 2.0 * np.pi * freqs
        self.set_design(rho_bar)
        n = len(self.ports)
        s = np.full((freqs.size, n, n), np.nan + 0j)
        wa = np.full((freqs.size, n, n), np.nan + 0j)
        wb = np.full((freqs.size, n, n), np.nan + 0j)
        eta = {}
        absorbed = {}
        steps = {}
        excited = list(ports or sorted(self.ports))
        for j in excited:
            res = self.forward(j, omega=omega, design=False)
            steps[j] = res.steps
            qty = self.quantities(res.dft, j, omega, far=False)
            for (i, jj), v in qty["s"].items():
                s[:, i - 1, jj - 1] = v
            for k, (a, b) in qty["waves"].items():
                wa[:, k - 1, j - 1] = a
                wb[:, k - 1, j - 1] = b
            for jj, v in qty["eta"].items():
                eta[jj] = np.asarray(v, dtype=np.float64)
            for (name, jj), v in qty["absorbed"].items():
                absorbed[f"{name}/P{jj}"] = np.asarray(v, dtype=np.float64)
            for jj, v in qty["loss"].items():
                absorbed[f"loss{jj}"] = np.asarray(v, dtype=np.float64)
        if self.board:
            zc = np.stack(
                [
                    np.full(freqs.size, self.ports[k].port.resistance + 0j)
                    for k in sorted(self.ports)
                ],
                axis=-1,
            )
        else:
            zc = np.stack([self.cal[k].at(omega)[0] for k in sorted(self.ports)], axis=-1)
        out = {"freqs": freqs, "s": s, "zc": zc, "eta": eta, "steps": steps, "s_naive": s}
        out["absorbed"] = absorbed
        if sorted(excited) == sorted(self.ports) and n > 1:
            diag = np.abs(np.diagonal(wa, axis1=1, axis2=2))
            off = np.abs(wa - np.einsum("fii->fi", wa)[..., None] * np.eye(n))
            out["idle_incident"] = np.max(off / diag[:, None, :], axis=(1, 2))
            out["s"] = wb @ np.linalg.inv(wa)
        return out

    # -- reports --------------------------------------------------------------------------------

    def effective_threads(self) -> int:
        """The threads the stepper runs: the native pool's, torch's (`engine.thread_count`), or
        1 for numpy."""
        if self.sim.threads is not None:
            return int(self.sim.threads)
        if self.backend == "torch":
            from yapnr.rf.fdtd.engine import thread_count

            return thread_count(self.threads)
        return 1

    def solver_label(self) -> str:
        """The backend, precision and threads that run, and the native library (one line)."""
        out = f"{self.backend} {self.dtype.name}, {self.effective_threads()} thread(s)"
        native = native_status(self.sim)
        if native:
            out += f" ({native['library']}, {native['isa']})"
        return out

    def assumptions(self) -> list:
        """The model's assumptions that bear on the results, quantified where a closed form
        exists (design §25.4), for the result and validation reports: on the infinite substrate
        the radiated fraction is the non-guided fraction, which includes the TM0 surface wave."""
        out = list(self.warnings)
        if self.box is not None and not self.board:
            from yapnr.rf.stackup import surface_wave_share

            st = self.stackup
            share = surface_wave_share(st.er, st.h, st.f_ref)
            out.append(
                {
                    "code": "eta_nonguided",
                    "value": share,
                    "message": (
                        "the radiated fraction is the non-guided fraction of the infinite "
                        "substrate: it includes the TM0 surface wave, closed-form share "
                        f"P_sw/(P_sp + P_sw) = {share:.2f} for a half-wave patch on this "
                        f"stackup at f_ref = {st.f_ref / 1e9:g} GHz (Jackson and Alexopoulos); "
                        "a radiation efficiency, gain or pattern needs a `board` model"
                    ),
                }
            )
        return out

    def describe(self) -> dict:
        """Solver settings for the result's provenance."""
        g = self.grid
        fc = np.array([2.0 * np.pi * self.pulse.f_center])
        box = None
        if self.box is not None:
            box = {
                "mm": [
                    [float(ax.nodes[lo] * 1e3), float(ax.nodes[hi] * 1e3)]
                    for ax, (lo, hi) in zip((g.x, g.y, g.z), self.box.node_box)
                ],
                "nodes": [list(map(int, r)) for r in self.box.node_box],
                "eta": (
                    "total efficiency (radiated power over incident)"
                    if self.board
                    else "non-guided fraction (radiation and surface wave)"
                ),
            }
        if self.board:
            box["far_field_radius_mm"] = self.far.radius * 1e3
            box["image_ground"] = self.far.image
        return {
            "grid_cells": list(g.n),
            "cells": int(g.cells),
            "pitch_mm": self.pitch * 1e3,
            "substrate_cells": int(self.domain.spec.n_sub),
            "dt_ps": self.dt * 1e12,
            "courant": self.spec.grid.courant,
            "cpml_cells": [g.pml.x_lo, g.pml.x_hi, g.pml.y_lo, g.pml.y_hi, g.pml.z_hi]
            + ([g.pml.z_lo] if g.pml.z_lo else []),
            "board": self.domain.describe() if self.board else None,
            "design_pixels": list(self.design_shape),
            "port_width_cells": {str(k): int(v) for k, v in sorted(self.widths.items())},
            "port_zc_at_fc_ohm": (
                {
                    str(k): [float(c.at(fc)[0][0].real), float(c.at(fc)[0][0].imag)]
                    for k, c in sorted(self.cal.items())
                }
                if not self.board
                else {str(k): [pg.port.resistance, 0.0] for k, pg in sorted(self.ports.items())}
            ),
            "pulse_ghz": [self.pulse.f_center / 1e9, self.pulse.f_half_width / 1e9],
            "objective_ghz": [float(f) / 1e9 for f in self.freqs],
            "decimation": self._decimation(),
            "edge_correction": self.edge_correction,
            "port_source": self.spec.solver.port_source,
            "port_extraction": self.port_extraction,
            "radiation_box": box,
            "assumptions": self.assumptions(),
            "tol": self.tol,
            "adjoint_tol": self.adjoint_tol,
            "backend": self.backend,
            "dtype": str(self.dtype),
            "threads": self.effective_threads(),
            "native": native_status(self.sim),
            "filter_radius_mm": self.filter.radius * 1e3,
            "material_grid": self.material.to_json(),
            "groups": [
                {"name": g.name, "excitation": g.excitation, "requirements": len(g.requirements)}
                for g in self.groups
            ],
        }
