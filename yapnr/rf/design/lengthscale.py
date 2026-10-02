"""Minimum width and space: the indicator constraints of Zhou et al. (design §7.5).

Zhou, Lazarov, Wang and Sigmund (2015), as used in Hammond et al. §3.1 and in the photonics
design-rule work of Hammond et al. (2021), on the filtered field ρ̃ and the projected field ρ̄
of the design window, with physical gradients:

    I_s = ρ̄ exp(−c |∇ρ̃|²),          g_s = mean[ I_s · min(ρ̃ − η_e, 0)² ]
    I_v = (1 − ρ̄) exp(−c |∇ρ̃|²),    g_v = mean[ I_v · min(η_d − ρ̃, 0)² ]
    constraints:  g_s/ε − 1 ≤ 0,   g_v/ε − 1 ≤ 0

The indicators pick the skeleton of solid (void) features, where |∇ρ̃| is small; there ρ̃ must
reach η_e (fall below η_d). The conic filter radius R and the thresholds follow from the
minimum lengths through the relations of Qian and Sigmund (2013) for a line under the conic
filter: with η_e = 0.75 the radius for a minimum length b is R = b; different minimum width and
space share the larger R and derive η_e and η_d from each (`LengthScale.from_rules`).
c = 64 R² makes the exponent about −16 at a feature edge, where |∇ρ̃|² ≈ 1/(4R²).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from yapnr.rf.design.projection import tanh_projection


def conic_radius(length: float, eta_e: float = 0.75) -> float:
    """The conic-filter radius R whose eroded threshold η_e gives the minimum length b."""
    if 0.5 <= eta_e < 0.75:
        return length / (2.0 * math.sqrt(eta_e - 0.5))
    if 0.75 <= eta_e <= 1.0:
        return length / (2.0 - 2.0 * math.sqrt(1.0 - eta_e))
    raise ValueError("eta_e must lie in [0.5, 1]")


def eta_for_length(length: float, radius: float) -> float:
    """The threshold η_e at which a conic filter of radius R gives the minimum length b."""
    u = length / radius
    if u <= 0.0:
        return 0.5
    if u < 1.0:
        return 0.25 * u * u + 0.5
    if u < 2.0:
        return -0.25 * u * u + u
    return 1.0


@dataclass(frozen=True)
class LengthScale:
    """Thresholds and constants of the indicator constraints (lengths in metres)."""

    radius: float
    pitch: float
    eta_e: float = 0.75
    eta_d: float = 0.25
    c: float = 0.0
    eps: float = 1e-6

    @classmethod
    def from_rules(
        cls,
        min_width: float,
        min_space: float,
        pitch: float,
        eps: float = 1e-6,
        radius: float | None = None,
    ) -> "LengthScale":
        """R = the larger of the two radii at η = 0.75 (or the filter's `radius` when that is
        larger); η_e and η_d from each length under R."""
        radius = max(conic_radius(min_width), conic_radius(min_space), radius or 0.0)
        return cls(
            radius=radius,
            pitch=pitch,
            eta_e=eta_for_length(min_width, radius),
            eta_d=1.0 - eta_for_length(min_space, radius),
            c=64.0 * radius * radius,
            eps=eps,
        )

    @property
    def c_value(self) -> float:
        return self.c or 64.0 * self.radius * self.radius


def indicator_values(rho_tilde_m, beta: float, eta: float, ls: LengthScale, margin: int):
    """(g_s, g_v) as a torch tensor (2,) from the filtered field with `margin` ≥ 1 pixels
    around the window (central differences at every window pixel)."""
    import torch

    if margin < 1:
        raise ValueError("the filtered field needs a margin of at least one pixel")
    m = margin
    ni = rho_tilde_m.shape[0] - 2 * m
    nj = rho_tilde_m.shape[1] - 2 * m
    rt = rho_tilde_m[m : m + ni, m : m + nj]
    d = 2.0 * ls.pitch
    gx = (
        rho_tilde_m[m + 1 : m + 1 + ni, m : m + nj] - rho_tilde_m[m - 1 : m - 1 + ni, m : m + nj]
    ) / d
    gy = (
        rho_tilde_m[m : m + ni, m + 1 : m + 1 + nj] - rho_tilde_m[m : m + ni, m - 1 : m - 1 + nj]
    ) / d
    weight = torch.exp(-ls.c_value * (gx * gx + gy * gy))
    rb = tanh_projection(rt, beta, eta)
    solid = rb * weight * torch.clamp(rt - ls.eta_e, max=0.0) ** 2
    void = (1.0 - rb) * weight * torch.clamp(ls.eta_d - rt, max=0.0) ** 2
    return torch.stack([solid.mean(), void.mean()])


def indicator_constraints(rho_tilde_m, beta: float, eta: float, ls: LengthScale, margin: int):
    """The constraint values g/ε − 1 (≤ 0 when met), torch (2,): [width, space]."""
    return indicator_values(rho_tilde_m, beta, eta, ls, margin) / ls.eps - 1.0
