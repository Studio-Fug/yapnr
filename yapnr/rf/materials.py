"""Per-edge permittivity and conductivity of a microstrip board (design §4.3, §7.4).

`Structure` holds, for each E component, the permittivity and the conductivity of every edge:

- the substrate (εr, and σ_sub matching tan δ at f_ref) below the copper plane, air above, and
  the thickness-weighted mean on the plane itself (tangential to the interface);
- the copper sheet: a sheet conductance G_e (siemens) on each Ex and Ey edge of the copper
  plane, entering the update as σ_e = G_e / Δz_d(k_c);
- lumped resistors (extra σ on chosen edges) and hard PEC edges (held at zero).

The copper sheet is usually set per pixel (Yee face of the copper plane) and averaged onto the
edges as conductances in parallel: an edge shared by a copper and a void pixel conducts like
half the copper (`pixels_to_edges`); its transpose (`edges_to_pixels`) is the restriction of
edge gradients onto pixels (design §6.6).

**Inductive sheets** (`Structure.set_sheet` with an inductance): each pixel is then a sheet of
impedance R_p − iωL_p (time dependence e^{−iωt}), and each copper-plane edge carries one
branch per adjacent pixel, in parallel with the same weights. A branch's sheet current obeys
L dJ/dt + R J = E, integrated by the trapezoidal rule together with the Crank–Nicolson E
update (the engine keeps J per branch):

    J^{n+1} = k J^n + b (E^{n+1} + E^n)/2,   k = (2L − RΔt)/(2L + RΔt),  b = 2Δt/(2L + RΔt)

so the edge's update has the conductance Σ w b/(2Δz) and the explicit current
Σ w (1 + k) J^n/(2Δz). With L = 0 this is the resistive sheet (k = −1, b = 2/R). In the
frequency domain of the discrete scheme each branch is the admittance

    Y_d = c_ω G / (c_ω − iΩ L G)      (G = 1/R; Ω, c_ω as in `fdtd.dtft`)

entering like c_ω σ → c_ω Y_d/Δz. This is the RF analog of Hammond et al.'s interpolation of
dispersive (Drude–Lorentz) metals (§2.1, Eq. 7–10): with `sheet_inductance` gray pixels are
reactive (lossless) sheets instead of 377 Ω/sq absorbers, while ρ̄ = 1 is exactly the
resistive copper and ρ̄ = 0 a transparent sheet.
"""

from __future__ import annotations

import numpy as np

from yapnr.rf.constants import EPS0
from yapnr.rf.mesh import E_COMPONENTS, Grid
from yapnr.rf.stackup import Stackup


def sheet_conductance(rho_bar, g_min: float, g_max: float, g_d: float = 0.0):
    """G(ρ̄) = G_min (G_max/G_min)^ρ̄ + G_d ρ̄(1 − ρ̄) (log interpolation plus optional damping).

    Works on numpy arrays and torch tensors.
    """
    ratio = float(np.log(g_max / g_min))
    lib = np if isinstance(rho_bar, np.ndarray) or np.isscalar(rho_bar) else _torch()
    return g_min * lib.exp(ratio * rho_bar) + g_d * rho_bar * (1.0 - rho_bar)


def sheet_conductance_derivative(rho_bar, g_min: float, g_max: float, g_d: float = 0.0):
    """dG/dρ̄ = ln(G_max/G_min) G_min (G_max/G_min)^ρ̄ + G_d (1 − 2ρ̄)."""
    ratio = float(np.log(g_max / g_min))
    lib = np if isinstance(rho_bar, np.ndarray) or np.isscalar(rho_bar) else _torch()
    return ratio * g_min * lib.exp(ratio * rho_bar) + g_d * (1.0 - 2.0 * rho_bar)


def _torch():
    import torch

    return torch


def sheet_reactance(rho_bar, x0: float, x1: float):
    """X(ρ̄) = X0 (r^ρ̄ − r)/(1 − r), r = X1/X0: X0 at ρ̄ = 0 and exactly 0 at ρ̄ = 1, falling
    through √(X0 X1) near ρ̄ = ½ (log-like). numpy arrays or torch tensors."""
    r = x1 / x0
    lr = float(np.log(r))
    lib = np if isinstance(rho_bar, np.ndarray) or np.isscalar(rho_bar) else _torch()
    return x0 * (lib.exp(lr * rho_bar) - r) / (1.0 - r)


def sheet_reactance_derivative(rho_bar, x0: float, x1: float):
    """dX/dρ̄ = X0 ln(r) r^ρ̄/(1 − r)."""
    r = x1 / x0
    lr = float(np.log(r))
    lib = np if isinstance(rho_bar, np.ndarray) or np.isscalar(rho_bar) else _torch()
    return x0 * lr * lib.exp(lr * rho_bar) / (1.0 - r)


def reactive_sheet(rho_bar, g_max: float, omega_ref: float, damping: float):
    """(R, L) per pixel of the reactive interpolation: L = X(ρ̄)/ω_ref (`reactive_range`) and
    R = R_s + damping·ω_ref·L. The damping (Hammond et al. Eq. 11, here a loss tangent R/X of
    `damping` at ω_ref on gray pixels) bounds the time constant L/R of the gray sheets' current
    loops to 1/(damping·ω_ref), which otherwise is X/(ω R_s) ≈ 10⁴ periods and keeps the runs
    ringing; copper (ρ̄ = 1, L = 0) keeps R_s exactly."""
    x0, x1 = reactive_range(g_max)
    lp = np.maximum(sheet_reactance(rho_bar, x0, x1), 0.0) / omega_ref
    return 1.0 / g_max + damping * omega_ref * lp, lp


def reactive_range(g_max: float) -> tuple[float, float]:
    """(X0, X1) of the reactive interpolation for copper of sheet conductance G_max: X1 = R_s =
    1/G_max (the reactance falls below the resistance only for ρ̄ near 1) and X0 = η0²/X1, so
    the sheet passes η0 = 377 Ω near ρ̄ = ½ (as the resistive interpolation passes 377 Ω/sq)."""
    from yapnr.rf.constants import ETA0

    x1 = 1.0 / g_max
    return ETA0 * ETA0 / x1, x1


def branch_admittance(g, ind, omega, dt: float):
    """(Re, Im) of the discrete branch admittance Y_d = c G/(c − iΩ L G) per frequency (rows)
    and element of `g` and the inductance `ind` (flattened), in real arithmetic."""
    from yapnr.rf.fdtd.dtft import conductance_factor, numerical_omega

    c = conductance_factor(omega, dt)[:, None]
    big = numerical_omega(omega, dt)[:, None]
    g = np.asarray(g, dtype=np.float64).reshape(1, -1)
    ind = np.asarray(ind, dtype=np.float64).reshape(1, -1)
    # c G / (c − i q), q = Ω L G: = c G (c + i q)/(c² + q²).
    q = big * ind * g
    den = c * c + q * q
    return c * g * c / den, c * g * q / den


def branch_admittance_derivative(r, ind, dr, dl, omega, dt: float):
    """(Re, Im) of dY_d = (iΩ c dL − c² dR)/(c R − iΩ L)² of branches with resistance `r` and
    inductance `ind` (Y_d = c/(cR − iΩL)) for the parameter derivatives `dr`, `dl` (flattened,
    per frequency rows), in real arithmetic."""
    from yapnr.rf.fdtd.dtft import conductance_factor, numerical_omega

    c = conductance_factor(omega, dt)[:, None]
    big = numerical_omega(omega, dt)[:, None]
    r, ind, dr, dl = (np.asarray(v, dtype=np.float64).reshape(1, -1) for v in (r, ind, dr, dl))
    p, q = c * r, big * ind
    # 1/(p − iq)² = (p + iq)²/(p² + q²)² = (p² − q² + 2ipq)/(p² + q²)²
    den = (p * p + q * q) ** 2
    ur, ui = (p * p - q * q) / den, 2.0 * p * q / den
    nr, ni = -c * c * dr, big * c * dl
    return nr * ur - ni * ui, nr * ui + ni * ur


def _pair_weights(lengths_primary: np.ndarray, lengths_dual: np.ndarray):
    """Weights of the cell below and above each node in a dual-length average."""
    n = lengths_primary.size
    lo = np.zeros(n + 1)
    hi = np.zeros(n + 1)
    lo[1:] = lengths_primary / (2.0 * lengths_dual[1:])
    hi[:-1] = lengths_primary / (2.0 * lengths_dual[:-1])
    return lo, hi


def pixels_to_edges(grid: Grid, g_pix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Average per-pixel sheet conductances (Nx, Ny) onto the Ex (Nx, Ny+1) and Ey (Nx+1, Ny)
    edges of the copper plane, weighted by the part of each edge's dual width over each pixel."""
    nx, ny, _ = grid.n
    g_pix = np.asarray(g_pix, dtype=np.float64)
    if g_pix.shape != (nx, ny):
        raise ValueError(f"pixel array must be {(nx, ny)}, got {g_pix.shape}")
    ylo, yhi = _pair_weights(grid.y.primary, grid.y.dual)
    xlo, xhi = _pair_weights(grid.x.primary, grid.x.dual)
    gx = np.zeros((nx, ny + 1))
    gx[:, 1:] += ylo[None, 1:] * g_pix
    gx[:, :-1] += yhi[None, :-1] * g_pix
    gy = np.zeros((nx + 1, ny))
    gy[1:, :] += xlo[1:, None] * g_pix
    gy[:-1, :] += xhi[:-1, None] * g_pix
    return gx, gy


def edges_to_pixels(grid: Grid, d_gx: np.ndarray, d_gy: np.ndarray) -> np.ndarray:
    """Transpose of `pixels_to_edges`: edge gradients (any trailing leading dims) → pixels.

    `d_gx` has shape (..., Nx, Ny+1) and `d_gy` (..., Nx+1, Ny); the result (..., Nx, Ny).
    """
    ylo, yhi = _pair_weights(grid.y.primary, grid.y.dual)
    xlo, xhi = _pair_weights(grid.x.primary, grid.x.dual)
    d_gx = np.asarray(d_gx)
    d_gy = np.asarray(d_gy)
    out = d_gx[..., :, 1:] * ylo[1:] + d_gx[..., :, :-1] * yhi[:-1]
    out = out + d_gy[..., 1:, :] * xlo[1:, None] + d_gy[..., :-1, :] * xhi[:-1, None]
    return out


class Structure:
    """Materials of every E edge: permittivity, conductivity, the copper sheet, PEC edges."""

    def __init__(self, grid: Grid, stackup: Stackup):
        self.grid = grid
        self.stackup = stackup
        nx, ny, nz = grid.n
        kc = grid.k_c
        eps_sub = EPS0 * stackup.er
        sig_sub = stackup.sigma_sub
        self._eps: dict[str, np.ndarray] = {}
        self._sig: dict[str, np.ndarray] = {}
        # Ez edges lie inside one layer: substrate below the copper plane.
        ez_eps = np.full(nz, EPS0)
        ez_sig = np.zeros(nz)
        ez_eps[:kc] = eps_sub
        ez_sig[:kc] = sig_sub
        # Ex/Ey edges on node planes: substrate below, air above, thickness-weighted on k_c.
        dzp = grid.z.primary
        w_sub = dzp[kc - 1] / (dzp[kc - 1] + dzp[kc])
        t_eps = np.full(nz + 1, EPS0)
        t_sig = np.zeros(nz + 1)
        t_eps[:kc] = eps_sub
        t_sig[:kc] = sig_sub
        t_eps[kc] = w_sub * eps_sub + (1.0 - w_sub) * EPS0
        t_sig[kc] = w_sub * sig_sub
        for comp in E_COMPONENTS:
            shape = grid.shape(comp)
            prof_eps, prof_sig = (ez_eps, ez_sig) if comp == "ez" else (t_eps, t_sig)
            self._eps[comp] = np.broadcast_to(prof_eps, shape).copy()
            self._sig[comp] = np.broadcast_to(prof_sig, shape).copy()
        self.sheet = {"ex": np.zeros((nx, ny + 1)), "ey": np.zeros((nx + 1, ny))}
        self.pec = {comp: np.zeros(grid.shape(comp), dtype=bool) for comp in E_COMPONENTS}
        self._extra = {comp: np.zeros(grid.shape(comp)) for comp in E_COMPONENTS}
        # Inductive sheet: per-pixel conductance and inductance (None: resistive sheet).
        self.pixel_g: np.ndarray | None = None
        self.pixel_l: np.ndarray | None = None
        # Copper-edge correction (`edges`): the pixel copper fraction and the factor maps.
        self.copper: np.ndarray | None = None
        self.edge_maps: dict | None = None
        self._edge_k = None

    # -- copper edges ---------------------------------------------------------------------------

    @property
    def edge_correction(self) -> bool:
        return self.edge_maps is not None

    def edge_constants(self):
        from yapnr.rf.edges import EdgeConstants

        if self._edge_k is None:
            self._edge_k = EdgeConstants.build(self.grid, self.stackup.er)
        return self._edge_k

    def set_edge_correction(self, copper: np.ndarray | None) -> None:
        """Correct the copper's edges (`edges`) for the pixel copper fraction `copper` (Nx, Ny)
        in [0, 1] (None: no correction). Call `Simulation.update_materials` afterwards."""
        from yapnr.rf.edges import factor_maps

        if copper is None:
            self.copper = self.edge_maps = None
            return
        copper = np.asarray(copper, dtype=np.float64)
        if copper.shape != self.grid.n[:2]:
            raise ValueError(f"copper fraction must be {self.grid.n[:2]}, got {copper.shape}")
        self.copper = copper.copy()
        self.edge_maps = factor_maps(self.copper, self.edge_constants())

    def mu_factor(self, comp: str) -> np.ndarray | None:
        """1/μr of the corrected planes of H component `comp` (n1, n2, planes), or None."""
        if self.edge_maps is None:
            return None
        return self.edge_maps[comp]

    def eps_base(self, comp: str) -> np.ndarray:
        """Permittivity of each edge without the edge correction."""
        return self._eps[comp]

    # -- copper sheet ---------------------------------------------------------------------------

    @property
    def sheet_dz(self) -> float:
        """Dual height Δz_d(k_c) of the copper plane: σ_e = G_e / Δz_d."""
        return float(self.grid.z.dual[self.grid.k_c])

    def set_pixels(self, g_pix: np.ndarray) -> None:
        """Set the sheet from per-pixel conductances over the whole copper plane (Nx, Ny)."""
        self.set_sheet(g_pix)

    def set_sheet(self, g_pix: np.ndarray, l_pix: np.ndarray | None = None, dt: float = 0.0):
        """Set the sheet from per-pixel conductances G (S) and, optionally, inductances L (H per
        square; pixel impedance 1/G − iωL) over the whole copper plane (Nx, Ny). With an
        inductance the time step `dt` is needed (the branch coefficients depend on it)."""
        g_pix = np.asarray(g_pix, dtype=np.float64)
        if l_pix is None or not np.any(np.asarray(l_pix) > 0):
            self.pixel_g = self.pixel_l = None
            gx, gy = pixels_to_edges(self.grid, g_pix)
        else:
            if dt <= 0:
                raise ValueError("an inductive sheet needs the time step")
            l_pix = np.asarray(l_pix, dtype=np.float64)
            self.pixel_g, self.pixel_l, self.sheet_dt = g_pix.copy(), l_pix.copy(), float(dt)
            _, b = self.branch_coefficients()
            gx, gy = pixels_to_edges(self.grid, 0.5 * b)
        self.sheet["ex"][...] = gx
        self.sheet["ey"][...] = gy

    @property
    def inductive(self) -> bool:
        return self.pixel_l is not None

    def branch_coefficients(self) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel (k, b) of the inductive sheet's branches (see the module doc)."""
        g, l, dt = self.pixel_g, self.pixel_l, self.sheet_dt
        lg2 = 2.0 * l * g
        k = (lg2 - dt) / (lg2 + dt)
        b = 2.0 * dt * g / (lg2 + dt)
        return k, b

    def sheet_branches(self, comp: str) -> dict:
        """The engine's arrays for the interior copper-plane edges of `comp` ("ex": (Nx, Ny−1),
        "ey": (Nx−1, Ny)): per branch (lo, hi pixel) the factor c1 = w(1 + k)/(2Δz) of the
        explicit current, k and bh = b/2."""
        grid = self.grid
        k, b = self.branch_coefficients()
        dz = self.sheet_dz
        if comp == "ex":
            lo_w, hi_w = _pair_weights(grid.y.primary, grid.y.dual)
            w_lo, w_hi = lo_w[1:-1][None, :], hi_w[1:-1][None, :]
            k_lo, k_hi, b_lo, b_hi = k[:, :-1], k[:, 1:], b[:, :-1], b[:, 1:]
        else:
            lo_w, hi_w = _pair_weights(grid.x.primary, grid.x.dual)
            w_lo, w_hi = lo_w[1:-1][:, None], hi_w[1:-1][:, None]
            k_lo, k_hi, b_lo, b_hi = k[:-1, :], k[1:, :], b[:-1, :], b[1:, :]
        return {
            "c1": (w_lo * (1.0 + k_lo) / (2.0 * dz), w_hi * (1.0 + k_hi) / (2.0 * dz)),
            "k": (k_lo, k_hi),
            "bh": (0.5 * b_lo, 0.5 * b_hi),
        }

    def sheet_coefficient(self, omega, dt: float) -> dict:
        """(Re, Im) of the sheet's term in the discrete frequency-domain E equation per edge,
        (M, edges of `comp` on the copper plane): c_ω G_e/Δz for a resistive sheet,
        Σ w c_ω Y_d/Δz for an inductive one. Keyed by "ex", "ey"."""
        from yapnr.rf.fdtd.dtft import conductance_factor

        omega = np.atleast_1d(np.asarray(omega, dtype=np.float64))
        dz = self.sheet_dz
        out = {}
        if not self.inductive:
            c = conductance_factor(omega, dt)
            for comp in ("ex", "ey"):
                g = self.sheet[comp]
                out[comp] = (c[:, None, None] * g[None] / dz, np.zeros((omega.size,) + g.shape))
            return out
        c = conductance_factor(omega, dt)[:, None]
        yr, yi = branch_admittance(self.pixel_g, self.pixel_l, omega, dt)
        shape = self.pixel_g.shape
        for comp, sl in (("ex", 0), ("ey", 1)):
            parts = []
            for y in (yr, yi):
                pix = (c * y).reshape((omega.size,) + shape) / dz
                gx, gy = zip(*[pixels_to_edges(self.grid, pix[m]) for m in range(omega.size)])
                parts.append(np.array(gx if sl == 0 else gy))
            out[comp] = tuple(parts)
        return out

    def set_edges(self, gx: np.ndarray, gy: np.ndarray) -> None:
        """Set the sheet conductance of every Ex and Ey edge of the copper plane directly."""
        self.sheet["ex"][...] = gx
        self.sheet["ey"][...] = gy

    # -- elements -------------------------------------------------------------------------------

    def add_resistor(
        self, comp: str, flat_index, resistance: float, n_series: int, m_parallel: int
    ):
        """A lumped resistor on `flat_index` edges: n edges in series, m columns in parallel.

        σ_e = n L_e / (m R A_e) with A_e the dual area of each edge (design §4.3).
        """
        flat_index = np.asarray(flat_index).ravel()
        vol = self.grid.volume(comp).ravel()[flat_index]
        length = np.broadcast_to(self.grid.edge_length(comp), self.grid.shape(comp)).ravel()
        length = length[flat_index]
        area = vol / length
        self._extra[comp].reshape(-1)[flat_index] += (
            n_series * length / (m_parallel * resistance * area)
        )

    def set_pec(self, comp: str, flat_index) -> None:
        """Hold the given edges at zero (perfect conductor)."""
        self.pec[comp].reshape(-1)[np.asarray(flat_index).ravel()] = True

    # -- per-edge material arrays ---------------------------------------------------------------

    def eps(self, comp: str) -> np.ndarray:
        """Permittivity of each edge, with the edge correction's factors when it is on."""
        if self.edge_maps is None:
            return self._eps[comp]
        from yapnr.rf.edges import planes

        eps = self._eps[comp].copy()
        for n, k in enumerate(planes(self.grid, comp)):
            eps[:, :, k] *= self.edge_maps[comp][:, :, n]
        return eps

    def sigma(self, comp: str) -> np.ndarray:
        """Total conductivity of each edge: substrate + sheet (on the copper plane) + elements."""
        sig = self._sig[comp] + self._extra[comp]
        if comp in self.sheet:
            sig[:, :, self.grid.k_c] += self.sheet[comp] / self.sheet_dz
        return sig

    def coefficients(self, comp: str, dt: float) -> tuple[np.ndarray, np.ndarray]:
        """Crank–Nicolson update coefficients (Ca, Cb) of `comp` (design §4.4).

        E^{n+1} = Ca E^n + Cb (curl H − J),  a = σΔt/(2ε),  Ca = (1 − a)/(1 + a),
        Cb = (Δt/ε)/(1 + a). PEC edges get Ca = Cb = 0.
        """
        eps = self.eps(comp)
        a = self.sigma(comp) * dt / (2.0 * eps)
        ca = (1.0 - a) / (1.0 + a)
        cb = (dt / eps) / (1.0 + a)
        pec = self.pec[comp]
        ca[pec] = 0.0
        cb[pec] = 0.0
        return ca, cb
