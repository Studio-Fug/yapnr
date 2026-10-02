"""Sources: the forward Gaussian pulse and the Nuttall-basis adjoint sources (design §4.5, §6.5).

Electric currents J sit at half steps (J^{n+½} enters the E update of step n) and magnetic
currents K at integer steps (K^n enters the H update of step n). A source is a set of edges of
one component with a time series per edge; `values(n)` returns the values used in step n.

Adjoint sources (Hammond et al. §5.2, Eq. 20–24): with the Nuttall window w[n], 0 ≤ n ≤ N,

    W_m[n] = w[n] e^{−iω_m t_n},   s[n] = 2 Re Σ_m β_m W_m[n]

and the β_m solved so that the DTFT of the real sequence s, with the same phase convention as
the monitors, equals the requested spectral values at every ω_m exactly (a real-linear system
that includes the negative-frequency image). Three more basis sequences (the window times
1, u, u², u the centred time) zero the source's time moments of order 0–2, that is its
spectrum and two derivatives at ω = 0. Without them an adjoint current leaves static charge
behind and drives the slow eddy currents of the copper sheet (magnetic diffusion time
μ0 G_max Δ, tens of thousands of steps), which stalls the convergence of the run; the forward
pulse has no low-frequency content to begin with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

NUTTALL = (0.355768, 0.487396, 0.144232, 0.012604)


def is_magnetic(comp: str) -> bool:
    return comp[0] == "h"


def source_time(n, dt: float, magnetic: bool):
    """Time of step n's source sample: nΔt for K (magnetic), (n + ½)Δt for J (electric)."""
    return (np.asarray(n, dtype=np.float64) + (0.0 if magnetic else 0.5)) * dt


@dataclass(frozen=True)
class GaussianPulse:
    """J(t) = sin(ω_c (t − t0)) exp(−((t − t0)/τ)²), −20 dB at ω_c ± Δω_h, t0 = 5τ."""

    f_center: float
    f_half_width: float
    delay_taus: float = 5.0

    @property
    def tau(self) -> float:
        return 2.0 * math.sqrt(math.log(10.0)) / (2.0 * math.pi * self.f_half_width)

    @property
    def t0(self) -> float:
        return self.delay_taus * self.tau

    @property
    def t_end(self) -> float:
        return 2.0 * self.t0

    @property
    def f_top(self) -> float:
        """Frequency above which the spectrum is below −100 dB of its peak (for decimation)."""
        return self.f_center + math.sqrt(5.0) * self.f_half_width

    def __call__(self, t):
        t = np.asarray(t, dtype=np.float64)
        u = (t - self.t0) / self.tau
        return np.sin(2.0 * math.pi * self.f_center * (t - self.t0)) * np.exp(-u * u)

    @classmethod
    def for_band(cls, f_lo: float, f_hi: float, margin: float = 0.15, min_rel: float = 0.2):
        """A pulse covering [f_lo, f_hi] plus `margin` on each side and at least ±min_rel·f_c."""
        span = f_hi - f_lo
        lo = f_lo - margin * span
        hi = f_hi + margin * span
        fc = 0.5 * (lo + hi)
        hw = max(0.5 * (hi - lo), min_rel * fc)
        return cls(fc, hw)


@dataclass(frozen=True)
class PulseSecondDerivative:
    """s''(t) of a `GaussianPulse` s (analytic), for sources whose spectrum carries a factor
    ω² (ω² S(ω) is the transform of −s'' with the e^{−iωt} convention)."""

    pulse: GaussianPulse

    @property
    def t_end(self) -> float:
        return self.pulse.t_end

    @property
    def f_top(self) -> float:
        return self.pulse.f_top

    def __call__(self, t):
        p = self.pulse
        t = np.asarray(t, dtype=np.float64) - p.t0
        w = 2.0 * math.pi * p.f_center
        tau2 = p.tau * p.tau
        g = np.exp(-t * t / tau2)
        g1 = -2.0 * t / tau2 * g
        g2 = (4.0 * t * t / (tau2 * tau2) - 2.0 / tau2) * g
        sn, cs = np.sin(w * t), np.cos(w * t)
        return -w * w * sn * g + 2.0 * w * cs * g1 + sn * g2


@dataclass
class ProfileSource:
    """A source with two per-edge profiles: values(n) = amp0 · s(t_n) − amp2 · s''(t_n) for a
    `GaussianPulse` s (a spectrum (amp0 + ω² amp2) S(ω); the modal port source)."""

    comp: str
    index: np.ndarray
    amp0: np.ndarray
    amp2: np.ndarray
    waveform: GaussianPulse
    dt: float

    def __post_init__(self) -> None:
        self.index = np.asarray(self.index, dtype=np.int64).ravel()
        self.amp0 = np.asarray(self.amp0, dtype=np.float64).ravel()
        self.amp2 = np.asarray(self.amp2, dtype=np.float64).ravel()
        self._d2 = PulseSecondDerivative(self.waveform)

    @property
    def end_step(self) -> int:
        return int(math.ceil(self.waveform.t_end / self.dt)) + 1

    def values(self, n: int):
        if n > self.end_step:
            return None
        t = source_time(n, self.dt, is_magnetic(self.comp))
        return self.amp0 * float(self.waveform(t)) - self.amp2 * float(self._d2(t))


@dataclass
class PulseSource:
    """A source with one waveform scaled per edge: values(n) = amplitude · waveform(t_n)."""

    comp: str
    index: np.ndarray
    amplitude: np.ndarray
    waveform: GaussianPulse
    dt: float

    def __post_init__(self) -> None:
        self.index = np.asarray(self.index, dtype=np.int64).ravel()
        self.amplitude = np.broadcast_to(
            np.asarray(self.amplitude, dtype=np.float64), self.index.shape
        ).copy()

    @property
    def end_step(self) -> int:
        return int(math.ceil(self.waveform.t_end / self.dt)) + 1

    def values(self, n: int):
        if n > self.end_step:
            return None
        return self.amplitude * float(
            self.waveform(source_time(n, self.dt, is_magnetic(self.comp)))
        )


def nuttall(n_window: int) -> np.ndarray:
    """The 4-term Nuttall window w[n], 0 ≤ n ≤ N (N + 1 samples)."""
    n = np.arange(n_window + 1, dtype=np.float64)
    x = 2.0 * math.pi * n / n_window
    a = NUTTALL
    return a[0] - a[1] * np.cos(x) + a[2] * np.cos(2 * x) - a[3] * np.cos(3 * x)


def window_length(omega: np.ndarray, dt: float) -> int:
    """N = ⌈2π / (Δω_min Δt)⌉ with Δω_min the smallest spacing of the objective frequencies.

    One frequency: four periods of it.
    """
    w = np.sort(np.asarray(omega, dtype=np.float64))
    if w.size > 1:
        dw = float(np.min(np.diff(w)))
        if dw <= 0:
            raise ValueError("objective frequencies must be distinct")
    else:
        dw = float(w[0]) / 4.0
    return int(math.ceil(2.0 * math.pi / (dw * dt)))


@dataclass
class NuttallFit:
    """The real-linear map from requested DTFT values at ω_m to Nuttall coefficients β_m."""

    omega: np.ndarray
    dt: float
    magnetic: bool
    n_window: int
    moments: int = 3
    cond: float = 0.0
    _inv: np.ndarray = field(default=None, repr=False)
    _w: np.ndarray = field(default=None, repr=False)
    _basis: np.ndarray = field(default=None, repr=False)

    @classmethod
    def build(
        cls,
        omega,
        dt: float,
        magnetic: bool,
        n_window: int | None = None,
        max_cond: float = 1e6,
        max_doublings: int = 6,
    ) -> "NuttallFit":
        omega = np.asarray(omega, dtype=np.float64)
        n_win = n_window or window_length(omega, dt)
        for _ in range(max_doublings + 1):
            fit = cls(omega=omega, dt=dt, magnetic=magnetic, n_window=n_win)
            fit._factor()
            if fit.cond <= max_cond:
                return fit
            n_win *= 2
        raise RuntimeError(f"Nuttall fit ill-conditioned (cond {fit.cond:.3g})")

    def _factor(self) -> None:
        w = nuttall(self.n_window)
        n = np.arange(self.n_window + 1, dtype=np.float64)
        t = source_time(n, self.dt, self.magnetic)
        m = self.omega.size
        # Real basis sequences: 2 Re W_m (coefficient p_m), −2 Im W_m (q_m), and the window
        # times powers of the centred step index, whose coefficients zero the low-order time
        # moments (DC, first, ...) of the source (see `moments`).
        wm = w[None, :] * np.exp(-1j * np.outer(self.omega, t))  # W_m[n] = w e^{-iω_m t}
        u = (n - 0.5 * self.n_window) / self.n_window
        low = np.array([w * u**p for p in range(self.moments)]).reshape(self.moments, n.size)
        basis = np.concatenate([2.0 * wm.real, -2.0 * wm.imag, low], axis=0)
        ek = np.exp(1j * np.outer(self.omega, t))  # (M, T)
        dtft = self.dt * ek @ basis.T  # (M, 2M+P): DTFT of each basis sequence at ω_k
        size = 2 * m + self.moments
        r = np.zeros((size, size))
        r[:m] = dtft.real
        r[m : 2 * m] = dtft.imag
        for p in range(self.moments):
            r[2 * m + p] = self.dt * basis @ u**p  # p-th time moment
        # The conditioning that matters is that of the frequency block (closely spaced ω_m
        # under a short window); the moment rows only add a fixed scaling.
        self.cond = float(np.linalg.cond(r[: 2 * m, : 2 * m]))
        self._inv = np.linalg.inv(r)
        self._w = w
        self._basis = basis

    @property
    def f_top(self) -> float:
        """Highest objective frequency plus the window's main-lobe half width (4 bins); above
        it the spectrum is at the Nuttall sidelobe level (about −90 dB)."""
        return float(self.omega.max()) / (2.0 * math.pi) + 4.0 / (self.n_window * self.dt)

    def coefficients(self, requested: np.ndarray) -> np.ndarray:
        """Real basis coefficients (P, 2M + moments) for requested DTFT values (P, M) at the
        objective frequencies, with the low-order time moments of the source zero."""
        req = np.asarray(requested, dtype=np.complex128)
        zeros = np.zeros(req.shape[:-1] + (self.moments,))
        rhs = np.concatenate([req.real, req.imag, zeros], axis=-1)
        return rhs @ self._inv.T

    def series(self, coef: np.ndarray, n: int) -> np.ndarray:
        """s[n] for every edge (zero outside 0..N)."""
        if n < 0 or n > self.n_window:
            return np.zeros(coef.shape[0])
        return coef @ self._basis[:, n]

    def realized(self, coef: np.ndarray, omega: np.ndarray) -> np.ndarray:
        """DTFT of the realized series at `omega` (P, len(omega)); for tests."""
        t = source_time(np.arange(self.n_window + 1), self.dt, self.magnetic)
        s = coef @ self._basis  # (P, T)
        return self.dt * s @ np.exp(1j * np.outer(t, np.asarray(omega)))


@dataclass
class SpectralSource:
    """An adjoint source: per-edge basis coefficients (P, 2M + moments) under one fit."""

    comp: str
    index: np.ndarray
    coef: np.ndarray
    fit: NuttallFit

    @property
    def end_step(self) -> int:
        return self.fit.n_window + 1

    def values(self, n: int):
        if n > self.fit.n_window:
            return None
        return self.fit.series(self.coef, n)
