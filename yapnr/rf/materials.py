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

    # -- copper sheet ---------------------------------------------------------------------------

    @property
    def sheet_dz(self) -> float:
        """Dual height Δz_d(k_c) of the copper plane: σ_e = G_e / Δz_d."""
        return float(self.grid.z.dual[self.grid.k_c])

    def set_pixels(self, g_pix: np.ndarray) -> None:
        """Set the sheet from per-pixel conductances over the whole copper plane (Nx, Ny)."""
        gx, gy = pixels_to_edges(self.grid, g_pix)
        self.sheet["ex"][...] = gx
        self.sheet["ey"][...] = gy

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
        return self._eps[comp]

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
        eps = self._eps[comp]
        a = self.sigma(comp) * dt / (2.0 * eps)
        ca = (1.0 - a) / (1.0 + a)
        cb = (dt / eps) / (1.0 + a)
        pec = self.pec[comp]
        ca[pec] = 0.0
        cb[pec] = 0.0
        return ca, cb
