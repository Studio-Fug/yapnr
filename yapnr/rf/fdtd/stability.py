"""Time-step bounds on graded grids (design §4.7).

The leapfrog scheme is stable for Δt < 2/√λ_max, with λ_max the largest eigenvalue of
ε⁻¹ C_H μ⁻¹ C_E on the free E edges. `max_eigenvalue` estimates it by power iteration with the
Rayleigh quotient in the V ε inner product (in which the operator is symmetric positive
semidefinite), so the estimate approaches λ_max from below. Crank–Nicolson conductivity and the
CPML (κ ≥ 1) do not lower the bound.
"""

from __future__ import annotations

import math

import numpy as np

from yapnr.rf.constants import MU0
from yapnr.rf.mesh import E_COMPONENTS, Grid, staggered


def _free_masks(grid: Grid, structure) -> dict:
    masks = {}
    for c in E_COMPONENTS:
        m = np.ones(grid.shape(c), dtype=bool)
        for a in range(3):
            if not staggered(c, a):
                sl = [slice(None)] * 3
                sl[a] = 0
                m[tuple(sl)] = False
                sl[a] = -1
                m[tuple(sl)] = False
        if structure is not None:
            m &= ~structure.pec[c]
        masks[c] = m
    return masks


def _bc(v, a):
    s = [1, 1, 1]
    s[a] = v.size
    return v.reshape(s)


def apply_curl_curl(grid: Grid, e: dict, eps: dict, masks: dict, mu: dict | None = None) -> dict:
    """M e = ε⁻¹ C_H μ⁻¹ C_E e on the free edges (no CPML); `mu` maps an H component to its
    1/μr on the planes the copper-edge correction changes (`Structure.mu_factor`)."""
    x, y, z = grid.x, grid.y, grid.z

    def d(a, ax):
        return np.diff(a, axis=ax)

    hx = (d(e["ez"], 1) / _bc(y.primary, 1) - d(e["ey"], 2) / _bc(z.primary, 2)) / MU0
    hy = (d(e["ex"], 2) / _bc(z.primary, 2) - d(e["ez"], 0) / _bc(x.primary, 0)) / MU0
    hz = (d(e["ey"], 0) / _bc(x.primary, 0) - d(e["ex"], 1) / _bc(y.primary, 1)) / MU0
    if mu:
        from yapnr.rf.edges import planes

        for comp, h in (("hx", hx), ("hy", hy), ("hz", hz)):
            if mu.get(comp) is not None:
                ks = planes(grid, comp)
                h[:, :, ks[0] : ks[-1] + 1] *= mu[comp]
    out = {}
    cx = np.zeros(grid.shape("ex"))
    cx[:, 1:-1, :] += d(hz, 1) / _bc(y.dual[1:-1], 1)
    cx[:, :, 1:-1] -= d(hy, 2) / _bc(z.dual[1:-1], 2)
    cy = np.zeros(grid.shape("ey"))
    cy[:, :, 1:-1] += d(hx, 2) / _bc(z.dual[1:-1], 2)
    cy[1:-1, :, :] -= d(hz, 0) / _bc(x.dual[1:-1], 0)
    cz = np.zeros(grid.shape("ez"))
    cz[1:-1, :, :] += d(hy, 0) / _bc(x.dual[1:-1], 0)
    cz[:, 1:-1, :] -= d(hx, 1) / _bc(y.dual[1:-1], 1)
    for c, cc in (("ex", cx), ("ey", cy), ("ez", cz)):
        out[c] = np.where(masks[c], cc / eps[c], 0.0)
    return out


def max_eigenvalue(grid: Grid, structure=None, *, tol: float = 1e-6, max_iter: int = 500):
    """Power-iteration estimate of λ_max(ε⁻¹ C_H μ⁻¹ C_E); returns (λ, iterations)."""
    from yapnr.rf.constants import EPS0

    masks = _free_masks(grid, structure)
    eps = {
        c: (structure.eps(c) if structure is not None else np.full(grid.shape(c), EPS0))
        for c in E_COMPONENTS
    }
    mu = None
    if structure is not None and structure.edge_correction:
        mu = {c: structure.mu_factor(c) for c in ("hx", "hy", "hz")}
    w = {c: grid.volume(c) * eps[c] for c in E_COMPONENTS}
    # Deterministic start with a strong Nyquist component: (−1)^(i+j+k).
    e = {}
    for c in E_COMPONENTS:
        i, j, k = np.indices(grid.shape(c))
        e[c] = np.where(
            masks[c], (-1.0) ** (i + j + k) * (1.0 + 0.01 * ((i * 7 + j * 3 + k) % 5)), 0.0
        )
    lam_prev = 0.0
    lam = 0.0
    for it in range(1, max_iter + 1):
        me = apply_curl_curl(grid, e, eps, masks, mu)
        num = sum(float(np.sum(w[c] * e[c] * me[c])) for c in E_COMPONENTS)
        den = sum(float(np.sum(w[c] * e[c] * e[c])) for c in E_COMPONENTS)
        lam = num / den
        norm = math.sqrt(sum(float(np.sum(w[c] * me[c] ** 2)) for c in E_COMPONENTS))
        e = {c: me[c] / norm for c in E_COMPONENTS}
        if it > 5 and abs(lam - lam_prev) <= tol * lam:
            return lam, it
        lam_prev = lam
    return lam, max_iter


def stable_dt(grid: Grid, structure=None, courant: float = 0.95) -> float:
    """Δt = courant · min(2/√(1.01 λ_est), Δt_CFL) (design §4.7)."""
    lam, _ = max_eigenvalue(grid, structure)
    return courant * min(2.0 / math.sqrt(1.01 * lam), grid.courant_dt())
