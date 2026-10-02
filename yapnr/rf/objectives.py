"""Requirements → normalized violations φ → objective groups (design §9).

Each requirement becomes a normalized violation φ(ω_m) at the objective frequencies inside its
band (φ ≤ 0 means met), with x = 10 log10(|S|² + 1e-10):

    max_db L (or an upper mask)      φ = (x − L)/s          s = 10 dB
    min_db L (or a lower mask)       φ = (L − x)/s          s = 1 dB
    phase θ0 ± tol                   φ = (1 − cos(∠S − θ0))/(1 − cos tol) − 1
    radiated fraction ≥ η_min        φ = (η_min − η)/s      s = 0.1   (≤ η_max likewise)
    absorbed fraction ≥ a_min        φ = (a_min − a)/s      s = 0.1   (≤ a_max likewise)

Requirements group by excitation (S_ij and the radiated and absorbed fractions of port j belong
to excitation j). Per group and frequency the violations combine into one smooth maximum,
f_{g,m} = τ log Σ_r exp(φ_r(ω_m)/τ), at most τ log K above the true maximum; one adjoint run
per group gives every f_{g,m} with its gradient. `aggregate="none"` makes one group per
requirement. The epigraph constrains f_{g,m} ≤ t for every (g, m) with an active requirement.

The functions take torch complex128 quantities (from the probe DTFTs) so that `adjoint.
wirtinger` differentiates them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from yapnr.rf.spec import Requirement, Spec


@dataclass
class Group:
    """Requirements sharing one excitation, aggregated per frequency into f_{g,m}."""

    name: str
    excitation: int
    requirements: tuple[Requirement, ...]
    active: np.ndarray  # (R, M) bool: requirement r constrains frequency m

    @property
    def frequencies_active(self) -> np.ndarray:
        """(M,) True where the group has at least one active requirement."""
        return self.active.any(axis=0)


def build_groups(spec: Spec, freqs: np.ndarray) -> list[Group]:
    """The objective groups of a spec at the objective frequencies `freqs` (Hz)."""
    reqs = spec.requirements
    if spec.optimizer.aggregate == "none":
        parts = [(f"r{k + 1}", (r,)) for k, r in enumerate(reqs)]
    else:
        parts = []
        for j in spec.excitations:
            parts.append((f"x{j}", tuple(r for r in reqs if r.excitation == j)))
    groups = []
    for name, rs in parts:
        active = np.array([spec.band_mask(r.band, freqs) for r in rs])
        groups.append(Group(name, rs[0].excitation, rs, active))
    return groups


def phi(req: Requirement, quantities: dict, freqs: np.ndarray):
    """φ_r at every objective frequency (torch (M,)); meaningful where the band is active.

    `quantities` holds "s": {(i, j): S_ij (M,)} (internal e^{−iωt} convention), "eta":
    {j: η_j (M,)} and "absorbed": {(element, j): the element's share of P_inc,j (M,)}.
    """
    import torch

    limit = torch.as_tensor(req.limit_at(freqs), dtype=torch.float64)
    s = req.scale_value
    if req.quantity == "radiated":
        eta = quantities["eta"][req.ports[0]]
        return (limit - eta) / s if req.bound == "min" else (eta - limit) / s
    if req.quantity == "absorbed":
        a = quantities["absorbed"][(req.element, req.ports[0])]
        return (limit - a) / s if req.bound == "min" else (a - limit) / s
    sij = quantities["s"][req.ports]
    if req.quantity == "phase":
        # Engineering phase: ∠conj(S); cos(∠conj(S) − θ0) = Re(S e^{iθ0}) / |S|.
        th = math.radians(req.limit)
        cos = torch.real(sij * complex(math.cos(th), math.sin(th))) / (torch.abs(sij) + 1e-300)
        return (1.0 - cos) / (1.0 - math.cos(math.radians(req.tol_deg))) - 1.0
    x = 10.0 * torch.log10(torch.abs(sij) ** 2 + 1e-10)
    return (x - limit) / s if req.bound == "max" else (limit - x) / s


def group_values(group: Group, quantities: dict, freqs: np.ndarray, tau: float):
    """f_{g,m} (torch (M,)): the smooth maximum of the active φ at each frequency. Entries
    without an active requirement are a large negative constant without gradient."""
    import torch

    phis = torch.stack([phi(r, quantities, freqs) for r in group.requirements])
    active = torch.as_tensor(group.active)
    masked = torch.where(active, phis / tau, torch.full_like(phis, -1e30))
    if len(group.requirements) == 1:
        return torch.where(active[0], phis[0], torch.full_like(phis[0], -1e30))
    return tau * torch.logsumexp(masked, dim=0)


def violations(spec: Spec, quantities: dict, freqs: np.ndarray, excitation: int) -> dict:
    """φ per requirement label ("k: label", numpy (M,), NaN where the band does not apply) of
    the requirements measured with `excitation`, for reports."""
    out = {}
    for k, r in enumerate(spec.requirements):
        if r.excitation != excitation:
            continue
        v = phi(r, quantities, freqs).detach().numpy().astype(np.float64)
        v = np.where(spec.band_mask(r.band, freqs), v, np.nan)
        out[f"{k + 1}: {r.label}"] = v
    return out
