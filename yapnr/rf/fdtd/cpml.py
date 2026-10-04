"""Convolutional PML (Roden and Gedney 2000) profiles on graded axes (design §4.4).

In a slab normal to u the derivative ∂_u becomes (1/κ_u) ∂_u + ψ_u with

    ψ_u^n = b_u ψ_u^{n−1} + c_u (∂_u F)^n
    b_u = exp(−(σ_u/κ_u + α_u) Δt/ε0),   c_u = σ_u (b_u − 1) / (κ_u (σ_u + κ_u α_u))
    σ_u(s) = σ_max (s/d)^m,   κ_u(s) = 1 + (κ_max − 1)(s/d)^m,   α_u(s) = α_max (1 − s/d)

where s is the depth into the PML of thickness d. The DTFT of the recursion is a coordinate
stretch 1/s_u(ω) = 1/κ_u + c_u/(1 − b_u e^{iωΔt}) that depends on the position along u only,
which keeps the discrete operator symmetric under the volume weighting outside the PML (§6.1).
Outside the slabs σ = 0, κ = 1 and c = 0 exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from yapnr.rf.constants import EPS0, ETA0
from yapnr.rf.mesh import Axis


@dataclass(frozen=True)
class CPMLParams:
    """CPML grading parameters.

    σ_max = sigma_factor · (order + 1) / (η0 Δ), with Δ the PML cell size of that side;
    α_max = 2π ε0 f_alpha (the design uses f_alpha = f_lo / 2).
    """

    order: float = 3.0
    sigma_factor: float = 0.8
    kappa_max: float = 5.0
    f_alpha: float = 1e9

    @property
    def alpha_max(self) -> float:
        return 2.0 * math.pi * EPS0 * self.f_alpha


@dataclass(frozen=True)
class Profile:
    """CPML coefficients at a set of positions along one axis (1D arrays of equal length)."""

    kappa: np.ndarray
    b: np.ndarray
    c: np.ndarray

    def slabs(self) -> list[tuple[int, int]]:
        """Contiguous index ranges [start, stop) where the PML is active (c ≠ 0 or κ ≠ 1)."""
        active = (self.c != 0.0) | (self.kappa != 1.0)
        runs = []
        i = 0
        n = active.size
        while i < n:
            if active[i]:
                j = i
                while j < n and active[j]:
                    j += 1
                runs.append((i, j))
                i = j
            else:
                i += 1
        return runs


def profile(
    axis: Axis,
    n_lo: int,
    n_hi: int,
    positions: np.ndarray,
    dt: float,
    params: CPMLParams,
) -> Profile:
    """CPML (κ, b, c) at `positions` for an axis with n_lo / n_hi PML cells at its ends."""
    pos = np.asarray(positions, dtype=np.float64)
    kappa = np.ones_like(pos)
    b = np.zeros_like(pos)
    c = np.zeros_like(pos)
    m = params.order
    nodes = axis.nodes
    sides = []
    if n_lo > 0:
        face = nodes[n_lo]
        depth = face - nodes[0]
        cell = nodes[1] - nodes[0]
        sides.append(((face - pos) / depth, cell))
    if n_hi > 0:
        face = nodes[axis.n - n_hi]
        depth = nodes[-1] - face
        cell = nodes[-1] - nodes[-2]
        sides.append(((pos - face) / depth, cell))
    for rho, cell in sides:
        inside = rho > 1e-12
        r = np.clip(rho[inside], 0.0, 1.0)
        sig = params.sigma_factor * (m + 1.0) / (ETA0 * cell) * r**m
        kap = 1.0 + (params.kappa_max - 1.0) * r**m
        alp = params.alpha_max * (1.0 - r)
        bb = np.exp(-(sig / kap + alp) * dt / EPS0)
        denom = kap * (sig + kap * alp)
        cc = np.where(denom > 0, sig * (bb - 1.0) / np.where(denom > 0, denom, 1.0), 0.0)
        kappa[inside] = kap
        b[inside] = bb
        c[inside] = cc
    return Profile(kappa=kappa, b=b, c=c)


def stretch(prof: Profile, omega: np.ndarray, dt: float) -> np.ndarray:
    """The frequency-domain factor 1/s(ω) = 1/κ + c/(1 − b e^{iωΔt}) of the recursion
    (rows: frequencies, columns: positions)."""
    z = np.exp(1j * np.asarray(omega)[:, None] * dt)
    return 1.0 / prof.kappa[None, :] + prof.c[None, :] / (1.0 - prof.b[None, :] * z)
