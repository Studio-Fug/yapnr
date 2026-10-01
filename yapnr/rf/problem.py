"""A spec as a solver problem: grid, ports, calibration, design parameterization, evaluation.

`Problem(spec)` builds, at the spec's resolution (or `refine` times finer):

- the `Domain` (graded grid, line ports, feeds, design window) and the time step;
- the port calibrations Z_c(ω), k(ω) (cached on disk), and the port widths: a port without a
  width gets the whole number of cells whose calibrated Re Z_c at the band centre is closest to
  50 Ω, starting from Hammerstad–Jensen's width;
- the radiated-power box when a requirement needs it;
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
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.adjoint import gradient, wirtinger
from yapnr.rf.constants import ETA0
from yapnr.rf.design.filters import ConicFilter
from yapnr.rf.design.lengthscale import LengthScale, conic_radius
from yapnr.rf.design.material_grid import MaterialGrid
from yapnr.rf.design.parameterization import Parameterization
from yapnr.rf.domain import Domain, DomainSpec, PortSpec, hj_width_cells
from yapnr.rf.fdtd.dtft import decimation as dtft_decimation
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import sheet_conductance, sheet_conductance_derivative
from yapnr.rf.objectives import build_groups, group_values, violations
from yapnr.rf.ports import LineCalibration, calibrate_line
from yapnr.rf.spec import Spec

PAD_DEPTH = 2  # pixels of fixed feed copper inside the design window at each port (§5.1)


def default_cells(h: float, pitch: float) -> tuple[int, int]:
    """(meas_cells, src_cells) with the measurement plane ≥ 3h behind the reference plane and
    the source about 3h further (design §5.1: 9 and 17 cells on S1 at 0.3 mm)."""
    n = int(math.ceil(3.0 * h / pitch - 1e-9))
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
        log=None,
    ):
        self.spec = spec
        self.log = log or (lambda *_: None)
        sv = spec.solver
        self.exact = exact
        self.backend = backend or ("numpy" if exact else sv.backend)
        self.dtype = np.dtype(dtype or (np.float64 if exact else sv.dtype))
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

        widths = self._resolve_widths(n_sub)
        self.widths = widths
        self.domain = Domain(domain_spec(spec, widths, refine=refine, n_sub=n_sub))
        dom = self.domain
        self.grid = dom.grid
        self.dt = spec.grid.courant * self.grid.courant_dt()
        self.ports = {pg.port.number: pg for pg in dom.ports}
        self.cal = {n: self._calibration(widths[n]) for n in self.ports}
        self.box = None
        if spec.radiation is not None:
            rb = spec.radiation
            self.box = dom.radiation_box(
                (rb.offset_mm * 1e-3) if rb.offset_mm is not None else 0.5 * dom.spec.margin,
                (rb.height_mm * 1e-3) if rb.height_mm is not None else 0.6 * dom.spec.air,
                window_margin=None if rb.window_margin_mm is None else rb.window_margin_mm * 1e-3,
                window_height=None if rb.window_height_mm is None else rb.window_height_mm * 1e-3,
            )
        self.port_probes = [p for n in sorted(self.ports) for p in self.ports[n].probes]
        self.box_probes = self.box.probes if self.box is not None else []
        self.design_probes = dom.design_probes()
        self.watched = [p.name for p in self.port_probes + self.box_probes]
        self.groups = build_groups(spec, self.freqs)
        self.excitations = sorted({g.excitation for g in self.groups})
        self.g_min, self.g_max = self.stackup.g_min, self.stackup.g_max
        self.g_d = spec.optimizer.damping / ETA0
        self._fwd_cache: tuple[str, dict] = ("", {})
        self.sim = Simulation(
            self.grid,
            dom.structure(),
            dt=self.dt,
            backend=self.backend,
            dtype=self.dtype,
            threads=self.threads,
        )
        self.pitch = spec.grid.pitch_mm * 1e-3 / refine
        self.filter = ConicFilter(filter_radius(spec), self.pitch)
        self.material = self._material_grid(self.filter.ring_width)
        self.param = Parameterization(self.material, self.filter, spec.optimizer.eta)
        self.lengthscale = None
        if spec.rules.active:
            w = spec.rules.min_width_mm or spec.rules.min_space_mm
            s = spec.rules.min_space_mm or spec.rules.min_width_mm
            self.lengthscale = LengthScale.from_rules(w * 1e-3, s * 1e-3, self.pitch)

    # -- setup ----------------------------------------------------------------------------------

    def _calibration(self, width: int) -> LineCalibration:
        if self._given_cal is not None:
            return self._given_cal[width] if width in self._given_cal else self._given_cal["*"]
        if width not in self._cal_by_width:
            t0 = time.perf_counter()
            line = self._line_domain().line_spec(width, self.dt)
            self._cal_by_width[width] = calibrate_line(
                line,
                self.cal_omega,
                backend="torch" if self.backend == "torch" else "numpy",
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
        self.dt = spec.grid.courant * self._provisional.grid.courant_dt()
        if all(p.width_cells for p in spec.ports):
            return widths
        fc = 2.0 * np.pi * self.pulse.f_center

        def z_at_fc(w):
            return float(self._calibration(w).at(np.array([fc]))[0][0].real)

        z0 = z_at_fc(hj)
        other = hj - 1 if z0 < 50.0 else hj + 1
        cands = {hj: z0}
        if other >= 1:
            cands[other] = z_at_fc(other)
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
        for pg in dom.ports:
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
        for fr in spec.fixed:
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

    # -- design → solver ------------------------------------------------------------------------

    @property
    def design_shape(self) -> tuple[int, int]:
        return self.domain.design_shape

    def conductance(self, rho_bar):
        return sheet_conductance(rho_bar, self.g_min, self.g_max, self.g_d)

    def set_design(self, rho_bar: np.ndarray) -> None:
        rho_bar = np.asarray(rho_bar, dtype=np.float64)
        if rho_bar.shape != self.design_shape:
            raise ValueError(f"design must be {self.design_shape}, got {rho_bar.shape}")
        self.sim.structure.set_pixels(self.domain.pixels(self.conductance(rho_bar)))
        self.sim.update_materials()

    def _decimation(self) -> int:
        return 1 if self.exact else dtft_decimation(self.pulse.f_top, self.dt)

    def forward(self, port: int, *, omega=None, design: bool = True):
        """The forward run with port `port` excited; DTFTs of the ports, the box and (when
        `design`) the design plane."""
        omega = self.omega if omega is None else np.asarray(omega)
        probes = self.port_probes + self.box_probes + (self.design_probes if design else [])
        stop = StopRule(
            tol=self.tol,
            f_lo=float(omega.min()) / (2.0 * np.pi),
            max_steps=self.spec.solver.max_steps,
            probes=tuple(self.watched),
        )
        sources = self.ports[port].mode_sources(self.pulse, self.dt)
        return self.sim.run(sources, probes, omega, stop, decimation=self._decimation())

    def quantities(self, dft, port: int, omega=None) -> dict:
        """Waves of every port, S_ij for excitation j = `port`, and η_j (numpy or torch)."""
        omega = self.omega if omega is None else np.asarray(omega)
        waves = {n: sparams.port_waves(pg, self.cal[n], dft, omega) for n, pg in self.ports.items()}
        a_j = waves[port][0]
        out = {"waves": waves, "s": {}, "eta": {}}
        for n, (_, b) in waves.items():
            out["s"][(n, port)] = b / a_j
        if self.box is not None:
            p_inc = sparams.incident_power(a_j, self.cal[port], omega)
            out["eta"][port] = self.box.power(dft) / p_inc
        return out

    def evaluate(self, rho_bar: np.ndarray, *, gradients: bool = True) -> Evaluation:
        """Every f_{g,m} and (when `gradients`) ∂f_{g,m}/∂ρ̄ for the design ρ̄.

        The forward runs of the last design evaluated are kept (with the design-plane DTFTs),
        so evaluating the same ρ̄ again with gradients (the point a conservative MMA step
        accepted) runs only the adjoints."""
        import torch

        t0 = time.perf_counter()
        rho_bar = np.ascontiguousarray(rho_bar, dtype=np.float64)
        key = hashlib.sha256(rho_bar.tobytes()).hexdigest()
        cache = self._fwd_cache[1] if self._fwd_cache[0] == key else {}
        self._fwd_cache = (key, cache)
        self.set_design(rho_bar)
        dg = sheet_conductance_derivative(np.asarray(rho_bar), self.g_min, self.g_max, self.g_d)
        n_ports = len(self.ports)
        m = self.freqs.size
        s = np.full((m, n_ports, n_ports), np.nan + 0j)
        eta: dict = {}
        phis: dict = {}
        values, grads, keys = [], [], []
        steps: dict = {"forward": {}, "adjoint": {}}
        converged = True
        tau = self.spec.optimizer.tau
        stop_adj = StopRule(
            tol=self.adjoint_tol,
            f_lo=float(self.freqs.min()),
            max_steps=self.spec.solver.max_steps,
        )
        for j in self.excitations:
            if j in cache:
                fwd = cache[j]
                steps["forward"][j] = 0
            else:
                fwd = cache[j] = self.forward(j, design=True)
                steps["forward"][j] = fwd.steps
            converged &= fwd.converged
            q = {k: torch.as_tensor(v) for k, v in fwd.dft.items() if k in self.watched}
            qty = self.quantities(q, j)
            for (i, jj), v in qty["s"].items():
                s[:, i - 1, jj - 1] = v.detach().numpy()
            for jj, v in qty["eta"].items():
                eta[jj] = v.detach().numpy().astype(np.float64)
            phis.update(violations(self.spec, qty, self.freqs, excitation=j))
            for g in [g for g in self.groups if g.excitation == j]:

                def fn(dft, g=g, j=j):
                    return group_values(g, self.quantities(dft, j), self.freqs, tau)

                f, wg = wirtinger(fn, fwd.dft, self.watched)
                active = np.nonzero(g.frequencies_active)[0]
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
                    )
                    steps["adjoint"][g.name] = gr.adjoint.steps
                    converged &= gr.adjoint.converged
                    gp = self.domain.window_pixels(gr.pixels(self.grid)) * dg[None]
                for mm in active:
                    values.append(float(f[mm]))
                    keys.append((g.name, float(self.freqs[mm])))
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
        (or those in `ports`) excited; also Z_c (F, N) and η (port → (F,)) when there is a box.
        Internal e^{−iωt} convention; `sparams.to_engineering` converts."""
        freqs = self.sweep_freqs if freqs is None else np.asarray(freqs, dtype=np.float64)
        omega = 2.0 * np.pi * freqs
        self.set_design(rho_bar)
        n = len(self.ports)
        s = np.full((freqs.size, n, n), np.nan + 0j)
        eta = {}
        steps = {}
        for j in ports or sorted(self.ports):
            res = self.forward(j, omega=omega, design=False)
            steps[j] = res.steps
            qty = self.quantities(res.dft, j, omega)
            for (i, jj), v in qty["s"].items():
                s[:, i - 1, jj - 1] = v
            for jj, v in qty["eta"].items():
                eta[jj] = np.asarray(v, dtype=np.float64)
        zc = np.stack([self.cal[k].at(omega)[0] for k in sorted(self.ports)], axis=-1)
        return {"freqs": freqs, "s": s, "zc": zc, "eta": eta, "steps": steps}

    # -- reports --------------------------------------------------------------------------------

    def describe(self) -> dict:
        """Solver settings for the result's provenance."""
        g = self.grid
        fc = np.array([2.0 * np.pi * self.pulse.f_center])
        return {
            "grid_cells": list(g.n),
            "cells": int(g.cells),
            "pitch_mm": self.pitch * 1e3,
            "substrate_cells": int(self.domain.spec.n_sub),
            "dt_ps": self.dt * 1e12,
            "courant": self.spec.grid.courant,
            "cpml_cells": [g.pml.x_lo, g.pml.x_hi, g.pml.y_lo, g.pml.y_hi, g.pml.z_hi],
            "design_pixels": list(self.design_shape),
            "port_width_cells": {str(k): int(v) for k, v in sorted(self.widths.items())},
            "port_zc_at_fc_ohm": {
                str(k): [float(c.at(fc)[0][0].real), float(c.at(fc)[0][0].imag)]
                for k, c in sorted(self.cal.items())
            },
            "pulse_ghz": [self.pulse.f_center / 1e9, self.pulse.f_half_width / 1e9],
            "objective_ghz": [float(f) / 1e9 for f in self.freqs],
            "decimation": self._decimation(),
            "tol": self.tol,
            "adjoint_tol": self.adjoint_tol,
            "backend": self.backend,
            "dtype": str(self.dtype),
            "threads": self.threads,
            "filter_radius_mm": self.filter.radius * 1e3,
            "material_grid": self.material.to_json(),
            "groups": [
                {"name": g.name, "excitation": g.excitation, "requirements": len(g.requirements)}
                for g in self.groups
            ],
        }
