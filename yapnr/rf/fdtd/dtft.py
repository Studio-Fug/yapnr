"""DTFT accumulators with the solver's phase conventions (design §4.6).

E fields are sampled at integer steps and H fields at half steps, so (time dependence e^{−iωt})

    Ê(ω) = Δt Σ_n E^n e^{iωnΔt},     Ĥ(ω) = Δt Σ_n H^{n+½} e^{iω(n+½)Δt}.

With these phases the DTFT of the update equations is exactly Maxwell's equations at the
numerical frequency Ω = (2/Δt) sin(ωΔt/2) with conductivity σ cos(ωΔt/2):

    (−iΩ ε + c_ω σ) Ê = C_H Ĥ − Ĵ,     −iΩ μ Ĥ = −C_E Ê − K̂.

Decimation accumulates every d-th sample with weight dΔt (design §4.6).
"""

from __future__ import annotations

import math

import numpy as np


def numerical_omega(omega, dt: float) -> np.ndarray:
    """Ω = (2/Δt) sin(ωΔt/2)."""
    return 2.0 / dt * np.sin(np.asarray(omega, dtype=np.float64) * dt / 2.0)


def conductance_factor(omega, dt: float) -> np.ndarray:
    """c_ω = cos(ωΔt/2), the factor on σ in the DTFT of the Crank–Nicolson update."""
    return np.cos(np.asarray(omega, dtype=np.float64) * dt / 2.0)


def decimation(f_top: float, dt: float) -> int:
    """d = max(1, ⌊1/(2.5 f_top Δt)⌋) for content below f_top."""
    return max(1, int(math.floor(1.0 / (2.5 * f_top * dt))))


class Accumulator:
    """Running DTFT of a set of real samples at frequencies ω (rows) and samples (columns)."""

    def __init__(self, omega: np.ndarray, n_samples: int, dt: float, half_step: bool):
        self.omega = np.asarray(omega, dtype=np.float64)
        self.dt = dt
        self.half_step = half_step
        self.re = np.zeros((self.omega.size, n_samples))
        self.im = np.zeros((self.omega.size, n_samples))

    def add(self, values: np.ndarray, n: int, weight_steps: int = 1) -> None:
        """Add the sample taken at step n (time nΔt, or (n + ½)Δt for half-step fields)."""
        t = (n + (0.5 if self.half_step else 0.0)) * self.dt
        w = weight_steps * self.dt
        ph = self.omega * t
        v = np.asarray(values, dtype=np.float64)
        self.re += np.outer(w * np.cos(ph), v)
        self.im += np.outer(w * np.sin(ph), v)

    @property
    def value(self) -> np.ndarray:
        return self.re + 1j * self.im
