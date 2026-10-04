"""Line ports: a feed strip into the CPML, a soft source, V/I sampling (design §5.1–§5.3).

A port is a strip of `width_cells` cells along the inward normal from a domain edge (W, E, S
or N) to its reference plane, the boundary of the design region. Behind the reference plane:

- the V/I measurement plane, (meas_cells + ½) cells from the reference plane: V from Ez
  columns under the strip centre at the two nodes around it (averaged, co-located with I),
  I from the dual-cell ring of H around the strip (discrete Ampère law);
- the source plane, src_cells cells from the reference plane: a uniform soft J_z on every
  substrate Ez edge under the strip.

V is the strip potential over ground and I flows inward (into the design region).

`calibrate_line` measures the line's characteristic impedance Z_c(ω) and complex wavenumber
k(ω) (forward wave e^{ikx}, time dependence e^{−iωt}) on a straight feed. It uses two V/I
planes and the transfer relation of a uniform line, which is exact for any mix of forward and
backward waves, so the residual CPML reflection does not bias it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass

import numpy as np

from yapnr.rf.fdtd.cpml import CPMLParams
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.monitors import Probe
from yapnr.rf.fdtd.sources import GaussianPulse, PulseSource
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Grid, PMLCells, graded_axis, substrate_z_axis
from yapnr.rf.numerics import wsum
from yapnr.rf.stackup import Stackup

_SIDES = {"W": (0, 1), "E": (0, -1), "S": (1, 1), "N": (1, -1)}


@dataclass(frozen=True)
class LinePort:
    """A line port specification (lengths in metres).

    Attributes:
      number: port number (1-based).
      side: the domain edge the feed enters from: "W" (feed along +x), "E" (−x), "S" (+y),
        "N" (−y).
      center: transverse coordinate of the strip centre.
      width_cells: strip width in cells of the (uniform) transverse pitch.
      ref: coordinate along the feed axis of the reference plane (a grid node).
      meas_cells: the measurement plane is (meas_cells + ½) cells behind the reference plane.
      src_cells: the source plane is src_cells cells behind the reference plane.
    """

    number: int
    side: str
    center: float
    width_cells: int
    ref: float
    meas_cells: int = 9
    src_cells: int = 17

    def on(self, grid: Grid) -> "PortGeometry":
        return PortGeometry(self, grid)


class PortGeometry:
    """A `LinePort` placed on a grid: probes, weights, source edges and feed pixels."""

    def __init__(self, port: LinePort, grid: Grid):
        if port.side not in _SIDES:
            raise ValueError(f"port side must be one of {sorted(_SIDES)}")
        if port.src_cells <= port.meas_cells + 1:
            raise ValueError("the source plane must lie behind the measurement plane")
        self.port = port
        self.grid = grid
        a, s = _SIDES[port.side]
        t = 1 - a
        self.axis, self.sign, self.taxis = a, s, t
        ax, tx = grid.axis(a), grid.axis(t)
        kc = grid.k_c
        i_ref = ax.node(port.ref)
        self.i_ref = i_ref
        # Measurement: V nodes iv0 < iv1, I at the cell between them.
        if s > 0:
            iv0 = i_ref - port.meas_cells - 1
            i_src = i_ref - port.src_cells
        else:
            iv0 = i_ref + port.meas_cells
            i_src = i_ref + port.src_cells
        iv1 = iv0 + 1
        self.i_cell = iv0
        self.i_src = i_src
        pitch_a = ax.primary[iv0]
        self.d_m = abs(port.ref - ax.centers[iv0])
        n_lo, n_hi = grid.pml.along(a)
        if not (n_lo + 2 <= min(i_src, iv0) and max(i_src, iv1) <= ax.n - n_hi - 2):
            raise ValueError(f"port {port.number}: source or measurement plane in the CPML")
        # Strip nodes ta..tb across the feed.
        width = port.width_cells
        # The pitch where the strip lies (not the axis median, which grading can change).
        local = float(tx.primary[tx.cell(port.center)])
        ta = tx.nearest_node(port.center - 0.5 * width * local)
        tb = ta + width
        pitches = tx.primary[ta:tb]
        if tb > tx.n or not np.allclose(pitches, pitches[0], rtol=1e-9):
            raise ValueError(f"port {port.number}: the strip must lie on a uniform pitch")
        self.ta, self.tb = ta, tb
        self.width = float(tx.nodes[tb] - tx.nodes[ta])
        if width % 2 == 0:
            centre = [ta + width // 2]
        else:
            centre = [ta + (width - 1) // 2, ta + (width + 1) // 2]
        self.centre_nodes = centre
        dzp = grid.z.primary
        # V probe: Ez at nodes (iv0, iv1) along a, centre node(s) across, every substrate layer.
        idx, w = [], []
        for iv in (iv0, iv1):
            for tc in centre:
                for k in range(kc):
                    ijk = [0, 0, k]
                    ijk[a], ijk[t] = iv, tc
                    idx.append(grid.flat_index("ez", *ijk)[0])
                    w.append(-dzp[k] / (2.0 * len(centre)))
        self.v_probe = Probe(f"p{port.number}_v", "ez", np.array(idx))
        self.v_weights = np.array(w)
        # I ring at the cell iv0 along a.
        hz_cells = [tb, ta - 1]
        dzd = grid.z.dual[kc]
        if a == 0:  # flow along x: Hy below/above the plane, Hz at the strip sides
            hcomp = "hy"
            t_w = tx.dual[ta : tb + 1]
            it_idx = [grid.flat_index("hy", iv0, j, kc - 1)[0] for j in range(ta, tb + 1)]
            it_idx += [grid.flat_index("hy", iv0, j, kc)[0] for j in range(ta, tb + 1)]
            it_w = np.concatenate([t_w, -t_w])
            iz_idx = [grid.flat_index("hz", iv0, j, kc)[0] for j in hz_cells]
            iz_w = np.array([dzd, -dzd])
        else:  # flow along y: Hx above/below the plane, Hz at the strip sides
            hcomp = "hx"
            t_w = tx.dual[ta : tb + 1]
            it_idx = [grid.flat_index("hx", i, iv0, kc)[0] for i in range(ta, tb + 1)]
            it_idx += [grid.flat_index("hx", i, iv0, kc - 1)[0] for i in range(ta, tb + 1)]
            it_w = np.concatenate([t_w, -t_w])
            iz_idx = [grid.flat_index("hz", i, iv0, kc)[0] for i in (ta - 1, tb)]
            iz_w = np.array([dzd, -dzd])
        self.it_probe = Probe(f"p{port.number}_i{hcomp[1]}", hcomp, np.array(it_idx))
        self.iz_probe = Probe(f"p{port.number}_iz", "hz", np.array(iz_idx))
        self.it_weights = s * it_w
        self.iz_weights = s * iz_w
        # Source: Ez at node i_src, strip nodes ta..tb, every substrate layer.
        src = []
        for tn in range(ta, tb + 1):
            for k in range(kc):
                ijk = [0, 0, k]
                ijk[a], ijk[t] = i_src, tn
                src.append(grid.flat_index("ez", *ijk)[0])
        self.src_index = np.array(src)
        self.pitch = float(pitch_a)

    @property
    def probes(self) -> list[Probe]:
        return [self.v_probe, self.it_probe, self.iz_probe]

    def voltage(self, dft):
        """V̂_m (M,) from the probe DTFTs (numpy arrays or torch tensors)."""
        x = dft[self.v_probe.name]
        return wsum(x, self.v_weights)

    def current(self, dft):
        """Î (M,) flowing inward, from the probe DTFTs."""
        xt = dft[self.it_probe.name]
        xz = dft[self.iz_probe.name]
        return wsum(xt, self.it_weights) + wsum(xz, self.iz_weights)

    def source(self, waveform: GaussianPulse, dt: float, amplitude: float = 1.0) -> PulseSource:
        """The soft J_z source of this port (uniform under the strip)."""
        return PulseSource("ez", self.src_index, amplitude, waveform, dt)

    def mode_sources(self, waveform: GaussianPulse, dt: float, amplitude: float = 1.0):
        """Soft J sources on the whole source cross-section shaped like the quasi-TEM mode.

        J_t ∝ H_0 × n̂ excites only the quasi-TEM mode (mode orthogonality); in the quasi-static
        limit H_0 is the field of the air-filled line, so J_t is the transverse electrostatic
        field of the strip in air (a 2D Laplace solve on the cross-section, `quasi_tem_air`).
        """
        ez, et = quasi_tem_air(self.grid, self.taxis, self.ta, self.tb)
        a, t = self.axis, self.taxis
        tcomp = "e" + "xyz"[t]
        out = []
        for comp, prof in (("ez", ez), (tcomp, et)):
            peak = np.max(np.abs(prof))
            jj, kk = np.nonzero(np.abs(prof) > 1e-4 * peak)
            ijk = [None, None, kk]
            ijk[a] = np.full(jj.shape, self.i_src)
            ijk[t] = jj
            idx = self.grid.flat_index(comp, *ijk)
            out.append(PulseSource(comp, idx, amplitude * prof[jj, kk] / peak, waveform, dt))
        return out

    def sources(
        self,
        waveform: GaussianPulse,
        dt: float,
        stackup: Stackup,
        *,
        kind: str = "static",
        edge_correction: bool = False,
        cpml: CPMLParams = CPMLParams(),
    ):
        """The port's sources: "static" (`mode_sources`) or "mode" (`modal_sources`)."""
        if kind == "static":
            return self.mode_sources(waveform, dt)
        if kind == "mode":
            return self.modal_sources(
                waveform, dt, stackup, edge_correction=edge_correction, cpml=cpml
            )
        raise ValueError(f"unknown port source {kind!r}")

    def modal_sources(
        self,
        waveform: GaussianPulse,
        dt: float,
        stackup: Stackup,
        *,
        edge_correction: bool = False,
        cpml: CPMLParams = CPMLParams(),
        amplitude: float = 1.0,
    ):
        """Soft J sources on the source cross-section shaped like the line's discrete mode
        (`modes`): J = n̂ × H_0 of the mode solved on this grid's cross-section, fitted as
        P0 + ω² P2 over the pulse's band and driven by s(t) and −s''(t)."""
        from yapnr.rf.fdtd.sources import ProfileSource
        from yapnr.rf.modes import cross_section, mode_profile

        key = (float(dt), waveform.f_center, waveform.f_half_width, edge_correction, cpml)
        cached = getattr(self, "_modal", None)
        if cached is None or cached[0] != key:
            cs = cross_section(
                self.grid, stackup, self.axis, self.ta, self.tb, edge_correction=edge_correction
            )
            lo = max(waveform.f_center - waveform.f_half_width, 0.3 * waveform.f_center)
            hi = waveform.f_center + waveform.f_half_width
            self._modal = (key, mode_profile(cs, stackup, lo, hi, dt, cpml))
        prof = self._modal[1]
        a, t = self.axis, self.taxis
        tcomp = "e" + "xyz"[t]
        w2 = (2.0 * math.pi * waveform.f_center) ** 2
        out = []
        for comp, p0, p2 in (("ez", prof.jz0, prof.jz2), (tcomp, prof.jt0, prof.jt2)):
            keep = (np.abs(p0) > 1e-6) | (np.abs(p2) * w2 > 1e-6)
            jj, kk = np.nonzero(keep)
            ijk = [None, None, kk]
            ijk[a] = np.full(jj.shape, self.i_src)
            ijk[t] = jj
            idx = self.grid.flat_index(comp, *ijk)
            out.append(
                ProfileSource(
                    comp, idx, amplitude * p0[jj, kk], amplitude * p2[jj, kk], waveform, dt
                )
            )
        return out

    def feed_pixels(self) -> tuple[slice, slice]:
        """Pixel slices (x, y) of the feed strip from the domain edge to the reference plane."""
        along = slice(0, self.i_ref) if self.sign > 0 else slice(self.i_ref, None)
        across = slice(self.ta, self.tb)
        return (along, across) if self.axis == 0 else (across, along)


# -- modal wave extraction (design §25.1) -----------------------------------------------------

_FACE_MODES: dict = {}


@dataclass
class FaceMode:
    """A line mode sampled on the E and H̄ samples of one `FluxFace` (`ModalPlane.on_face`):
    per pair of the face the mode's E and H at the samples (physical components, the forward
    mode along +axis), the pair's sign in (E × H)·â and the weights; `n_cross` = ∫e × h (the
    projection's normalization), `m_cross` = ∫e × h* and `c` the factor of the face's H averaging
    (cos KΔ/2)."""

    e: list
    h: list
    sign: list
    weights: list
    n_cross: complex
    m_cross: complex
    c: complex
    k: complex


class ModalPlane:
    """Modal waves of a line port (design §25.1): the full transverse plane at the measurement
    node projected on the feed's discrete line mode (`modes.line_mode`, the profile the modal
    source launches).

    With E on the node plane and H̄ the mean of the two half-cell H planes either side (the
    samples of a `FluxBox` face), a forward mode e^{iKa} gives H̄ = cos(KΔ/2) h, so

        a₊ + a₋ = ∫ E × h_m / N,   c (a₊ − a₋) = ∫ e_m × H̄ / N,   N = ∫ e_m × h_m

    (unconjugated cross products along the feed axis, the face's area weights, c = cos(KΔ/2)).
    The mode is scaled to unit power (½ Re(c* ∫ e_m × h_m*) = ½) with its voltage −∫E_z dz
    under the strip real and positive, so that P = ½|a|² and the phases follow the V/I
    convention. Mode orthogonality removes the radiated and surface-wave fields that reach the
    plane, which bias the V/I samples by several per cent next to a radiator (the audit,
    docs/design §25). The waves are de-embedded to the reference plane by the phase of Re K.

    The plane spans the interior (one node inside the CPML on every side, the ground to below
    the top CPML) across the feed, cut half way to the strips of other ports on the same side.
    """

    def __init__(
        self,
        pg: PortGeometry,
        stackup: Stackup,
        dt: float,
        *,
        edge_correction: bool = False,
        cpml: CPMLParams = CPMLParams(),
        neighbours=(),
    ):
        grid = pg.grid
        self.pg = pg
        self.grid = grid
        self.stackup = stackup
        self.dt = float(dt)
        self.edge_correction = bool(edge_correction)
        self.cpml = cpml
        a, t, s = pg.axis, pg.taxis, pg.sign
        meas = pg.port.meas_cells
        self.node = pg.i_ref - meas if s > 0 else pg.i_ref + meas
        ax = grid.axis(a)
        self.d = abs(float(ax.nodes[pg.i_ref] - ax.nodes[self.node]))
        tx = grid.axis(t)
        n_lo, n_hi = grid.pml.along(t)
        lo, hi = n_lo + 1, tx.n - n_hi - 1
        for other in neighbours:
            if other.axis != a or other.sign != s:
                continue
            if other.tb <= pg.ta:
                lo = max(lo, (other.tb + pg.ta + 1) // 2)
            elif other.ta >= pg.tb:
                hi = min(hi, (other.ta + pg.tb) // 2)
        z_lo, z_hi = grid.pml.along(2)
        self._t_range = (lo, hi)
        self._z_range = (z_lo + (1 if z_lo else 0), grid.z.n - z_hi - 1)
        self.box = self.plane(self.node, f"p{pg.port.number}_mode")
        self.face = self.box.faces[0]
        self._modes: dict = {}
        self._face_modes: dict = {}

    def plane(self, node: int, name: str):
        """A one-face `FluxBox` on the transverse plane of this port's feed at `node` along the
        feed axis (the same transverse extent as the measurement plane): the samples of a modal
        projection there, e.g. where the feed crosses the radiation box (design §25.2)."""
        from yapnr.rf.fdtd.monitors import FluxBox

        a, t = self.pg.axis, self.pg.taxis
        ranges = [None, None, None]
        ranges[a] = (int(node), int(node))
        ranges[t] = self._t_range
        ranges[2] = self._z_range
        return FluxBox(self.grid, name, tuple(ranges), faces=(("x+", "y+")[a],))

    @property
    def probes(self) -> list[Probe]:
        return self.box.probes

    def mode(self, omega: float):
        """The line mode at ω (solved once per frequency, `modes.line_mode`), with its phase
        and scale fixed (see the class doc)."""
        key = round(float(omega), 3)
        if key not in self._modes:
            from yapnr.rf.modes import cached_line_mode, cross_section

            pg = self.pg
            if not hasattr(self, "_cs"):
                self._cs = cross_section(
                    self.grid,
                    self.stackup,
                    pg.axis,
                    pg.ta,
                    pg.tb,
                    edge_correction=self.edge_correction,
                )
            self._modes[key] = cached_line_mode(
                self._cs, self.stackup, float(omega), self.dt, self.cpml
            )
        return self._modes[key]

    def on_face(self, face, omega: float) -> FaceMode:
        """The mode at ω on the samples of `face` (a `FluxFace` normal to the feed axis, on the
        feed's uniform pitch), unit-power scaled with the voltage phase of the class doc."""
        key = (id(face), round(float(omega), 3))
        if key in self._face_modes:
            return self._face_modes[key]
        pg, grid = self.pg, self.grid
        a, t = pg.axis, pg.taxis
        if face.axis != a:
            raise ValueError("the face must be normal to the feed axis")
        m = self.mode(omega)
        # Once per process for the same mode and samples (bit-identical projections in every
        # problem of the process, as `modes.cached_line_mode`).
        h = hashlib.sha256(m.key.encode())
        for pr in face.probes:
            h.update(pr.comp.encode())
            h.update(np.ascontiguousarray(pr.index).tobytes())
        for w in face.weights:
            h.update(np.ascontiguousarray(w, dtype=np.float64).tobytes())
        h.update(repr((tuple(pg.centre_nodes), face.node, a)).encode())
        gkey = h.hexdigest()
        if gkey in _FACE_MODES:
            self._face_modes[key] = _FACE_MODES[gkey]
            return _FACE_MODES[gkey]
        flip = 1.0 if a == 0 else -1.0  # the solver's frame for a line along y is left-handed
        ax = grid.axis(a)
        d_lo = float(ax.primary[face.node - 1])
        d_hi = float(ax.primary[face.node])
        if abs(d_lo - d_hi) > 1e-9 * d_hi:
            raise ValueError("a modal face needs the feed's uniform pitch on both sides")
        c = complex(np.cos(0.5 * m.k * d_hi))
        es, hs, signs, ws = [], [], [], []
        for pair in (0, 1):
            ep = face.probes[3 * pair]
            ii, jj, kk = grid.unravel(ep.comp, ep.index)
            tt = (ii, jj)[t]
            if ep.comp == "ez":
                e = m.ez[tt, kk]
                h = flip * m.ht[tt, kk]
            else:
                e = m.et[tt, kk]
                h = flip * m.hz[tt, kk]
            es.append(np.asarray(e, dtype=np.complex128))
            hs.append(np.asarray(h, dtype=np.complex128))
            signs.append(1.0 if pair == 0 else -1.0)
            ws.append(np.asarray(face.weights[pair], dtype=np.float64))
        n_cross = sum(s * np.sum(w * e * h) for e, h, s, w in zip(es, hs, signs, ws))
        m_cross = sum(s * np.sum(w * e * np.conj(h)) for e, h, s, w in zip(es, hs, signs, ws))
        # The voltage of the mode under the strip centre, ground to copper.
        kc = grid.k_c
        dz = grid.z.primary
        v = 0.0
        for tc in pg.centre_nodes:
            v = v - np.sum(m.ez[tc, :kc] * dz[:kc]) / len(pg.centre_nodes)
        power = float(np.real(np.conj(c) * m_cross))
        if not power > 0 or v == 0:
            raise ValueError(f"port {pg.port.number}: the mode carries no power on the face")
        alpha = (abs(v) / v) / np.sqrt(power)
        es = [alpha * e for e in es]
        hs = [alpha * h for h in hs]
        fm = FaceMode(
            e=es,
            h=hs,
            sign=signs,
            weights=ws,
            n_cross=complex(alpha * alpha * n_cross),
            m_cross=complex(abs(alpha) ** 2 * m_cross),
            c=c,
            k=complex(m.k),
        )
        self._face_modes[key] = fm
        _FACE_MODES[gkey] = fm
        while len(_FACE_MODES) > 4096:
            _FACE_MODES.pop(next(iter(_FACE_MODES)))
        return fm

    @staticmethod
    def amplitudes(face, fms, dft):
        """(a₊, a₋) (M,) along +axis of the fields on `face` (probe DTFTs, numpy or torch),
        `fms` the face's `FaceMode` per frequency."""
        e_num, h_num = 0.0, 0.0
        for pair in (0, 1):
            e_meas = dft[face.probes[3 * pair].name]
            h_meas = (dft[face.probes[3 * pair + 1].name] + dft[face.probes[3 * pair + 2].name]) / 2
            hm = np.stack([fm.h[pair] * fm.weights[pair] * fm.sign[pair] for fm in fms])
            em = np.stack([fm.e[pair] * fm.weights[pair] * fm.sign[pair] for fm in fms])
            if not isinstance(e_meas, np.ndarray):
                import torch

                hm = torch.as_tensor(hm)
                em = torch.as_tensor(em)
            e_num = e_num + (e_meas * hm).sum(-1)
            h_num = h_num + (em * h_meas).sum(-1)
        n = np.array([fm.n_cross for fm in fms])
        c = np.array([fm.c for fm in fms])
        if not isinstance(e_num, np.ndarray):
            import torch

            n = torch.as_tensor(n)
            c = torch.as_tensor(c)
        s = e_num / n
        d = h_num / (n * c)
        return (s + d) / 2, (s - d) / 2

    @staticmethod
    def net_power(fms, a_plus, a_minus):
        """The modal net power along +axis, ½ Re[(a₊ + a₋) c* (a₊ − a₋)* M] (M,)."""
        m = np.array([np.conj(fm.c) * fm.m_cross for fm in fms])
        s = a_plus + a_minus
        d = a_plus - a_minus
        if not isinstance(s, np.ndarray):
            import torch

            m = torch.as_tensor(m)
            return 0.5 * torch.real(s * torch.conj(d) * m)
        return 0.5 * np.real(s * np.conj(d) * m)

    def face_modes(self, face, omega) -> list:
        return [self.on_face(face, float(w)) for w in np.atleast_1d(omega)]

    def waves(self, dft, omega):
        """(a_ref, b_ref) (M,) at the reference plane, P = ½|a|² (numpy or torch)."""
        fms = self.face_modes(self.face, omega)
        ap, am = self.amplitudes(self.face, fms, dft)
        inc, out = (ap, am) if self.pg.sign > 0 else (am, ap)
        ph = np.exp(1j * np.array([fm.k.real for fm in fms]) * self.d)
        if not isinstance(inc, np.ndarray):
            import torch

            ph = torch.as_tensor(ph)
        return inc * ph, out / ph


def quasi_tem_air(grid: Grid, taxis: int, ta: int, tb: int, tol: float = 1e-10):
    """Electrostatic field of the strip (nodes ta..tb on the copper plane, potential 1) over
    the ground, with ε = ε0 everywhere, on the (t, z) cross-section of the grid.

    Returns (E_z, E_t): E_z on the vertical edges, shape (N_t + 1, N_z), and E_t on the
    transverse edges, shape (N_t, N_z + 1). The outer boundary is held at 0 (the PEC walls
    behind the CPML). Solved by conjugate gradients on the 5-point graded-grid Laplacian.
    """
    tx, zx = grid.axis(taxis), grid.z
    nt, nz = tx.n, zx.n
    kc = grid.k_c
    gz = tx.dual[:, None] / zx.primary[None, :]  # vertical edge conductance (Nt+1, Nz)
    gt = zx.dual[None, :] / tx.primary[:, None]  # transverse edge conductance (Nt, Nz+1)
    fixed = np.zeros((nt + 1, nz + 1), dtype=bool)
    fixed[0, :] = fixed[-1, :] = True
    fixed[:, 0] = fixed[:, -1] = True
    fixed[ta : tb + 1, kc] = True
    phi0 = np.zeros((nt + 1, nz + 1))
    phi0[ta : tb + 1, kc] = 1.0
    free = ~fixed

    def lap(phi):
        out = np.zeros_like(phi)
        d = gz * (phi[:, 1:] - phi[:, :-1])
        out[:, :-1] += d
        out[:, 1:] -= d
        d = gt * (phi[1:, :] - phi[:-1, :])
        out[:-1, :] += d
        out[1:, :] -= d
        return out  # = −(A φ): divergence of the flux

    # Solve A x = b on the free nodes, A = −lap (SPD), b = lap(φ0).
    b = np.where(free, lap(phi0), 0.0)
    x = np.zeros_like(phi0)
    r = b.copy()
    p = r.copy()
    rr = float(np.sum(r * r))
    b_norm = math.sqrt(rr)
    for _ in range(20 * (nt + nz) + 1000):
        ap = np.where(free, -lap(p), 0.0)
        alpha = rr / float(np.sum(p * ap))
        x += alpha * p
        r -= alpha * ap
        rr_new = float(np.sum(r * r))
        if math.sqrt(rr_new) <= tol * b_norm:
            break
        p = r + (rr_new / rr) * p
        rr = rr_new
    phi = phi0 + x
    ez = -(phi[:, 1:] - phi[:, :-1]) / zx.primary[None, :]
    et = -(phi[1:, :] - phi[:-1, :]) / tx.primary[:, None]
    return ez, et


# -- lumped ports ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LumpedPort:
    """A resistive port on Ez columns from the ground to the copper plane (design §5.5).

    `nodes` are the (x, y) coordinates of the columns (grid nodes); the port resistance R is
    split over the columns in parallel and the substrate edges of each column in series. A
    Thevenin source V_s(t) in series with R drives it; with V the column voltage (strip over
    ground) and I the current into the network,

        a = V̂_s / (2√R),   b = (2V̂ − V̂_s) / (2√R)

    with the reference plane at the port. The case ports of the infinite-substrate model are
    line ports; the board models (design §26) drive their ports this way. `ground` is the node
    plane of the ground (0 in the infinite-substrate model).
    """

    number: int
    nodes: tuple
    resistance: float = 50.0
    ground: int = 0

    def on(self, grid: Grid) -> "LumpedPortGeometry":
        return LumpedPortGeometry(self, grid)


class LumpedPortGeometry:
    """A `LumpedPort` placed on a grid."""

    def __init__(self, port: LumpedPort, grid: Grid):
        self.port = port
        self.grid = grid
        kc, kg = grid.k_c, int(port.ground)
        if not 0 <= kg < kc:
            raise ValueError(f"lumped port {port.number}: the ground must lie below the copper")
        cols = [(grid.x.node(x), grid.y.node(y)) for x, y in port.nodes]
        self.columns = cols
        self.n_series = kc - kg
        idx = [grid.flat_index("ez", i, j, k)[0] for i, j in cols for k in range(kg, kc)]
        self.index = np.array(idx)
        dz = grid.z.primary[kg:kc]
        self.v_probe = Probe(f"p{port.number}_vl", "ez", self.index)
        self.v_weights = np.tile(-dz, len(cols)) / len(cols)
        # σ_e = n L_e / (m R A_e) on every column edge (n edges in series, m columns).
        vol = grid.volume("ez").reshape(-1)[self.index]
        length = np.tile(dz, len(cols))
        area = vol / length
        n, m = self.n_series, len(cols)
        self.sigma = n * length / (m * port.resistance * area)
        # J = σ_e V_s / (n L_e) is the Norton current of the source across each edge.
        self.amplitude = self.sigma / (n * length)

    @property
    def probes(self) -> list[Probe]:
        return [self.v_probe]

    def apply(self, structure) -> None:
        """Add the port resistance to the structure (every port, excited or not)."""
        p = self.port
        structure.add_resistor("ez", self.index, p.resistance, self.n_series, len(self.columns))

    def source(self, waveform, dt: float) -> PulseSource:
        """The Thevenin source V_s(t) = waveform(t) (volts) as Norton currents."""
        return PulseSource("ez", self.index, self.amplitude, waveform, dt)

    def voltage(self, dft):
        x = dft[self.v_probe.name]
        return wsum(x, self.v_weights)

    def waves(self, dft, vs_hat):
        """(a, b) at the port; `vs_hat` is the source DTFT (zero for a passive port)."""
        root = 2.0 * math.sqrt(self.port.resistance)
        v = self.voltage(dft)
        return vs_hat / root, (2.0 * v - vs_hat) / root


def source_dtft(waveform, omega, dt: float, magnetic: bool = False, end_step: int | None = None):
    """Δt Σ_n w(t_n) e^{iω t_n} of a waveform sampled like a J (half steps) or K source."""
    from yapnr.rf.fdtd.sources import source_time

    n_end = end_step if end_step is not None else int(math.ceil(waveform.t_end / dt)) + 1
    t = source_time(np.arange(n_end + 1), dt, magnetic)
    return dt * np.exp(1j * np.outer(np.asarray(omega), t)) @ waveform(t)


# -- calibration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LineSpec:
    """Inputs of a feed-line calibration (SI units)."""

    stackup: Stackup
    pitch: float
    n_sub: int
    width_cells: int
    air: float
    dz_max: float
    dt: float
    lateral: float = 0.0  # half-width of the uniform region beside the strip (default 6h)
    lateral_max_cell: float = 0.0  # default 2·pitch
    n_pml: int = 10
    n_pml_top: int = 8
    ratio: float = 1.25
    cpml: CPMLParams = CPMLParams()
    # Cells from the source plane to the V/I current cell of the ports that use the calibration
    # (src_cells − meas_cells − 1, plus one half): the power factor is measured at that
    # distance, where the incident wave of an excited port is measured (0: not set).
    src_gap_cells: int = 0
    # The copper-edge correction (`edges`) on the strip's edges.
    edge_correction: bool = False
    # The port source, "static" or "mode" (`PortGeometry.sources`).
    port_source: str = "static"

    def key(self, omega) -> str:
        d = {"spec": _jsonable(asdict(self)), "omega": [float(w) for w in np.asarray(omega)]}
        if self.edge_correction:
            from yapnr.rf.edges import MODEL

            d["edge_model"] = MODEL
        blob = json.dumps(d, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.floating):
        return float(x)
    return x


@dataclass
class LineCalibration:
    """Z_c(ω) and k(ω) of a feed line, at the frequencies `omega`.

    `power` is the ratio of the Poynting flux through the line's cross-section to ½ Re(V Î*)
    for the forward wave: V/I pseudo-waves carry ½(|a|² − |b|²) = ½ Re(V Î*), which differs
    from the field power of the quasi-TEM mode by a few per cent as dispersion grows. Ratios of
    waves (S-parameters) do not need it; powers compared with Poynting fluxes (the radiated
    fraction) do: P_inc = power · |a|²/2.
    """

    omega: np.ndarray
    zc: np.ndarray
    k: np.ndarray
    dt: float
    steps: int = 0
    power: np.ndarray | None = None

    def __post_init__(self) -> None:
        self.omega = np.atleast_1d(np.asarray(self.omega, dtype=np.float64))
        if self.power is None:
            self.power = np.ones(self.omega.size)

    def eps_eff(self) -> np.ndarray:
        from yapnr.rf.constants import C0

        return (self.k.real * C0 / self.omega) ** 2

    def at(self, omega) -> tuple[np.ndarray, np.ndarray]:
        """(Z_c, k) interpolated linearly at `omega` (exact at the calibrated frequencies)."""
        omega = np.asarray(omega, dtype=np.float64)
        if self.omega.size == 1:
            if not np.allclose(omega, self.omega[0]):
                raise ValueError("single-frequency calibration")
            return np.full(omega.shape, self.zc[0]), np.full(omega.shape, self.k[0])

        def interp(v):
            return np.interp(omega, self.omega, v.real) + 1j * np.interp(omega, self.omega, v.imag)

        return interp(self.zc), interp(self.k)

    def power_at(self, omega) -> np.ndarray:
        """The power factor interpolated at `omega`."""
        omega = np.asarray(omega, dtype=np.float64)
        if self.omega.size == 1:
            return np.full(omega.shape, self.power[0])
        return np.interp(omega, self.omega, self.power)

    def to_json(self) -> dict:
        return {
            "omega": self.omega.tolist(),
            "zc": [[z.real, z.imag] for z in self.zc],
            "k": [[z.real, z.imag] for z in self.k],
            "dt": self.dt,
            "steps": self.steps,
            "power": [float(p) for p in self.power],
        }

    @classmethod
    def from_json(cls, d: dict) -> "LineCalibration":
        return cls(
            omega=np.array(d["omega"]),
            zc=np.array([complex(*z) for z in d["zc"]]),
            k=np.array([complex(*z) for z in d["k"]]),
            dt=float(d["dt"]),
            steps=int(d.get("steps", 0)),
            power=np.array(d["power"]) if "power" in d else None,
        )


def two_plane_line(v1, i1, v2, i2, length: float, k_guess):
    """(Z_c, k) of a uniform line from V and I at two planes `length` apart (forward = +).

    With V(x) = A e^{ikx} + B e^{−ikx} and Z I(x) = A e^{ikx} − B e^{−ikx}:
    cos kL = (V1 I1 + V2 I2)/(V1 I2 + V2 I1), Z sin kL = (V2 − V1 cos kL)/(i I1),
    sin kL / Z = (I2 − I1 cos kL)/(i V1). The branch of kL is the one nearest k_guess·L.
    """
    v1, i1, v2, i2 = (np.asarray(x, dtype=np.complex128) for x in (v1, i1, v2, i2))
    c = (v1 * i1 + v2 * i2) / (v1 * i2 + v2 * i1)
    u = (v2 - c * v1) / (1j * i1)
    w = (i2 - c * i1) / (1j * v1)
    zc = np.sqrt(u / w)
    zc = np.where(zc.real < 0, -zc, zc)
    s = u / zc
    kl = -1j * np.log(c + 1j * s)
    guess = np.asarray(k_guess) * length
    kl = kl + 2 * np.pi * np.round((guess.real - kl.real) / (2 * np.pi))
    return zc, kl / length


def calibration_grid(spec: LineSpec, length_cells: int) -> tuple[Grid, float]:
    """A straight-feed grid: x along the line (uniform, CPML at both ends), y graded."""
    h = spec.stackup.h
    pitch = spec.pitch
    lateral = spec.lateral or 6.0 * h
    half_w = 0.5 * spec.width_cells * pitch
    core = pitch * math.ceil((half_w + lateral) / pitch)
    nx = length_cells + 2 * spec.n_pml
    x = graded_axis(0.0, nx * pitch, pitch, 0.0, nx * pitch, max_cell=pitch)
    y = graded_axis(
        -core,
        core,
        pitch,
        -core - 4.0 * h,
        core + 4.0 * h,
        max_cell=spec.lateral_max_cell or 2.0 * pitch,
        ratio=spec.ratio,
        n_pml_lo=spec.n_pml,
        n_pml_hi=spec.n_pml,
    )
    z, kc = substrate_z_axis(
        h, spec.n_sub, spec.air, dz_max=spec.dz_max, ratio=spec.ratio, n_pml=spec.n_pml_top
    )
    pml = PMLCells(spec.n_pml, spec.n_pml, spec.n_pml, spec.n_pml, spec.n_pml_top)
    grid = Grid(x, y, z, kc, pml)
    # Strip centred on y = 0 (a node when width_cells is even, else half a cell off).
    centre = 0.0 if spec.width_cells % 2 == 0 else 0.5 * pitch
    return grid, centre


def calibrate_line(
    spec: LineSpec,
    omega,
    *,
    backend: str = "numpy",
    dtype=np.float64,
    tol: float = 1e-5,
    cache_dir: str | None = None,
    plane_gap_cells: int | None = None,
) -> LineCalibration:
    """Measure Z_c(ω) and k(ω) of the feed line described by `spec` (design §5.3)."""
    omega = np.atleast_1d(np.asarray(omega, dtype=np.float64))
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, f"line-{spec.key(omega)}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return LineCalibration.from_json(json.load(fh))
    from yapnr.rf.constants import C0
    from yapnr.rf.stackup import hammerstad_jensen

    _, e_eff = hammerstad_jensen(spec.width_cells * spec.pitch, spec.stackup.h, spec.stackup.er)
    k_guess = omega * math.sqrt(e_eff) / C0
    lam_min = 2 * math.pi / float(k_guess.max())
    gap = plane_gap_cells or max(2, int(round(lam_min / 4.0 / spec.pitch)))
    # Z_c and k from two planes at least 3h from the source (the near-source fields bias them),
    # the power factor at the ports' own distance from the source.
    use = spec.src_gap_cells or int(math.ceil(3 * spec.stackup.h / spec.pitch)) + 1
    settle = max(use, int(math.ceil(3 * spec.stackup.h / spec.pitch)) + 1)
    meas_cells = 2
    src_cells = settle + meas_cells + 1
    margin = settle + 3
    length = 3 + src_cells + gap + margin
    grid, centre = calibration_grid(spec, length)
    st = Structure(grid, spec.stackup)
    g = np.zeros(grid.n[:2])
    # Port 1 launches +x; plane 2 is the measurement plane of a second port `gap` cells on.
    i_ref1 = spec.n_pml + 3 + src_cells
    p1 = LinePort(1, "W", centre, spec.width_cells, grid.x.nodes[i_ref1], meas_cells, src_cells).on(
        grid
    )
    p2 = LinePort(
        2, "W", centre, spec.width_cells, grid.x.nodes[i_ref1 + gap], meas_cells, src_cells
    ).on(grid)
    i_src = p1.i_src
    pp = LinePort(
        3, "W", centre, spec.width_cells, grid.x.nodes[i_src + use + 3], meas_cells, use + 3
    ).on(grid)
    g[:, p1.ta : p1.tb] = spec.stackup.g_max
    st.set_pixels(g)
    if spec.edge_correction:
        st.set_edge_correction((g > 0).astype(np.float64))
    sim = Simulation(grid, st, dt=spec.dt, cpml=spec.cpml, backend=backend, dtype=dtype)
    f = omega / (2 * np.pi)
    pulse = GaussianPulse.for_band(float(f.min()), float(f.max()))
    stop = StopRule(tol=tol, f_lo=float(f.min()))
    # Cross-section flux on the nodes either side of plane 1's current ring.
    from yapnr.rf.fdtd.monitors import FluxBox

    ic = pp.i_cell
    yr = (grid.pml.y_lo + 1, grid.y.n - grid.pml.y_hi - 1)
    zr = (0, grid.z.n - grid.pml.z_hi - 1)
    sections = [
        FluxBox(grid, f"section{n}", ((n - 1, n), yr, zr), faces=("x+",)) for n in (ic, ic + 1)
    ]
    probes = p1.probes + p2.probes + [p for b in sections for p in b.probes]
    if pp.i_cell != p1.i_cell:
        probes += pp.probes
    res = sim.run(
        p1.sources(
            pulse,
            sim.dt,
            spec.stackup,
            kind=spec.port_source,
            edge_correction=spec.edge_correction,
            cpml=spec.cpml,
        ),
        probes,
        omega,
        stop,
    )
    v1, i1 = p1.voltage(res.dft), p1.current(res.dft)
    v2, i2 = p2.voltage(res.dft), p2.current(res.dft)
    zc, k = two_plane_line(v1, i1, v2, i2, gap * spec.pitch, k_guess)
    flux = 0.5 * (sections[0].power(res.dft) + sections[1].power(res.dft))
    if pp.i_cell != p1.i_cell:
        vp, ip = pp.voltage(res.dft), pp.current(res.dft)
    else:
        vp, ip = v1, i1
    power = flux / (0.5 * np.real(vp * np.conj(ip)))
    cal = LineCalibration(omega=omega, zc=zc, k=k, dt=sim.dt, steps=res.steps, power=power)
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cal.to_json(), fh)
    return cal
