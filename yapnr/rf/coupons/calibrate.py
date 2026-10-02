"""Second-tier multiline TRL and the TDR reference impedance (design §8.3, §8.4).

The input is SOLT-corrected data (tier 1, at the cable ends, 50 Ω). With the eight-term error
model M = X T Ȳ (T-parameters, `models.s_to_t`), the zero-length thru gives M0 = X Ȳ, so for
every line M_i M0⁻¹ = X L_i X⁻¹ with L_i = diag(e^{−γ l_i}, e^{+γ l_i}) [Engen79, Marks91]:

- the eigenvalues of every pair of standards (M_j M_i⁻¹, the thru included) give γ:
  −log λ = γ (l_j − l_i), unwrapped from the shortest difference up and combined by weighted
  least squares over the pairs;
- the eigenvectors give the error box X up to one scalar, X ∝ [[a, b], [κa, 1]] (κ from the
  e^{−γl} eigenvector, b from the e^{+γl} one), averaged over the pairs with the weights
  |λ1 − λ2|², the conditioning of each pair [Marks91, DeGroot02]; the reported conditioning is
  the best pair's |sin(β Δl)|, the figure of merit of design §5.1;
- the reflect, measured at both ports, gives a² (sign from the reflect's expected sign);
- a device is corrected by T = X⁻¹ M M0⁻¹ X, referenced to the line's own Zc at the reference
  planes (pseudo-waves).

This is the classic multiline idea with a simple weighting, not Marks' Gauss-Markov estimator
or Hatab's eigenvalue formulation [Hatab22]; scikit-rf's `NISTMultilineTRL` serves as the
cross-check when installed (`skrf_gamma`).

The TDR route [IPC-TM-650 2.5.5.7A]: a low-pass step response of the SOLT-corrected S11 of the
longest line, averaged over the middle of the line; the fit runs the same processing on the
model, so window and loss effects cancel to first order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import models

C_LIGHT = 299792458.0


@dataclass
class TRL:
    f: np.ndarray
    gamma: np.ndarray  # 1/m
    x: np.ndarray  # (F, 2, 2) normalized port-1 error box
    t0: np.ndarray  # (F, 2, 2) measured thru (T)
    conditioning: np.ndarray  # best |sin|-like metric per frequency
    gamma_lines: Dict[float, np.ndarray] = field(default_factory=dict)  # per-line estimates
    reflect: np.ndarray = None  # Γ of the reflect at the reference plane

    @property
    def eps_eff(self) -> np.ndarray:
        w = 2 * np.pi * self.f
        return np.real((self.gamma * C_LIGHT / (1j * w)) ** 2)

    @property
    def alpha_db_per_m(self) -> np.ndarray:
        return 20 / math.log(10) * self.gamma.real

    def correct(self, s_meas: np.ndarray) -> np.ndarray:
        """Corrected S (reference: the line's Zc at the reference planes)."""
        tm = models.s_to_t(s_meas)
        xi = np.linalg.inv(self.x)
        t = xi @ tm @ np.linalg.inv(self.t0) @ self.x
        return models.t_to_s(t)


def multiline_trl(
    f: np.ndarray,
    thru: np.ndarray,
    lines: Sequence[Tuple[float, np.ndarray]],
    reflect: np.ndarray,
    eps_guess: float,
    reflect_sign: float = -1.0,
) -> TRL:
    """thru, line S (F, 2, 2) in tier-1 (50 Ω) reference; lines as (ΔL mm, S); reflect: the
    reflect stick's two-port S (S11 and S22 are used); eps_guess: εeff for unwrapping."""
    f = np.asarray(f, float)
    nf = len(f)
    lines = sorted(lines, key=lambda x: x[0])
    t0 = models.s_to_t(thru)
    T = [t0] + [models.s_to_t(s) for _, s in lines]
    Ls = [0.0] + [dl * 1e-3 for dl, _ in lines]
    Ti = [np.linalg.inv(t) for t in T]
    # every pair of standards (thru included), shortest length difference first: M_j M_i⁻¹ =
    # X diag(e^{−γ Δl}, e^{+γ Δl}) X⁻¹ with Δl = l_j − l_i [Marks91]
    pairs = sorted(
        ((Ls[j] - Ls[i], i, j) for i in range(len(T)) for j in range(i + 1, len(T))),
        key=lambda p: p[0],
    )
    A = [(dl, T[j] @ Ti[i], i) for dl, i, j in pairs]
    gamma = np.zeros(nf, complex)
    x = np.zeros((nf, 2, 2), complex)
    cond = np.zeros(nf)
    per_line = {dl: np.zeros(nf, complex) for dl, _ in lines}
    eps_ref = eps_guess
    for k in range(nf):
        w = 2 * np.pi * f[k]
        # the reference for choosing and unwrapping the eigenvalues: forward propagation at the
        # εeff found so far (sign-free, so one noisy low-frequency point cannot flip γ)
        g_ref = 1j * w * math.sqrt(eps_ref) / C_LIGHT
        first = k == 0
        num, den = 0.0 + 0.0j, 0.0
        kap_num, b_num, wsum = 0.0 + 0.0j, 0.0 + 0.0j, 0.0
        g_est = g_ref
        for dl, a_, i in A:
            lam, vec = np.linalg.eig(a_[k])
            j1 = int(np.argmin(np.abs(lam - np.exp(-g_ref * dl))))
            j2 = 1 - j1
            phi = -np.log(lam[j1])
            n = np.round((g_ref * dl - phi).imag / (2 * np.pi))
            phi = phi + 2j * np.pi * n
            wt = float(np.abs(lam[j1] - lam[j2]) ** 2)
            if i == 0:
                per_line[round(dl * 1e3, 6)][k] = phi / dl
            num += wt * dl * phi
            den += wt * dl * dl
            if den > 0:
                g_est = num / den
                if first and g_est.imag > 0:
                    g_ref = g_est  # first frequency: unwrap the longer pairs with it
            v1, v2 = vec[:, j1], vec[:, j2]
            if abs(v1[0]) > 0 and abs(v2[1]) > 0:
                kap_num += wt * (v1[1] / v1[0])
                b_num += wt * (v2[0] / v2[1])
                wsum += wt
        gamma[k] = g_est
        e = ((g_est * C_LIGHT / (1j * w)) ** 2).real
        if g_est.imag > 0 and 0.5 * eps_guess < e < 2.0 * eps_guess:
            eps_ref = e
        cond[k] = max(abs(math.sin(g_est.imag * dl)) for dl, _, _ in A)
        kap = kap_num / wsum
        b = b_num / wsum
        x[k] = np.array([[1.0, b], [kap, 1.0]])  # with a = 1 for now
    # reflect: a^2 = N1 / N2
    g1, g2 = reflect[:, 0, 0], reflect[:, 1, 1]
    kap, b = x[:, 1, 0], x[:, 0, 1]
    M = t0
    n1 = (g1 - b) / (1 - kap * g1)
    n2 = (g2 * (M[:, 1, 1] - kap * M[:, 0, 1]) + (M[:, 1, 0] - kap * M[:, 0, 0])) / (
        (M[:, 0, 0] - b * M[:, 1, 0]) + g2 * (M[:, 0, 1] - b * M[:, 1, 1])
    )
    a = np.sqrt(n1 / n2)
    gr = n1 / a
    flipit = (gr * np.conj(reflect_sign)).real < 0
    a = np.where(flipit, -a, a)
    gr = n1 / a
    x[:, 0, 0] = a
    x[:, 1, 0] = kap * a
    return TRL(f, gamma, x, t0, cond, per_line, gr)


# --- TDR impedance ------------------------------------------------------------------------------


def step_response(f: np.ndarray, s11: np.ndarray, oversample: int = 8, beta: float = 6.0):
    """Low-pass step response of S11 on a harmonic grid f_k = k Δf (k ≥ 1). Returns (t, Γ(t)).

    The impulse response is integrated from t = −2 ns, so that the half of each windowed
    impulse that falls before its arrival time is included (summing from t = 0 would halve a
    reflection at the reference plane)."""
    f = np.asarray(f, float)
    df = f[1] - f[0]
    if abs(f[0] - df) > 1e-6 * df:
        # resample onto the harmonic grid
        fh = np.arange(1, int(f[-1] / df) + 1) * df
        s11 = np.interp(fh, f, s11.real) + 1j * np.interp(fh, f, s11.imag)
        f = fh
    # DC by quadratic extrapolation of the real part
    p = np.polyfit(f[:6], s11[:6].real, 2)
    spec = np.concatenate([[np.polyval(p, 0.0)], s11])
    n = len(spec)
    win = np.kaiser(2 * n - 1, beta)[n - 1 :]
    spec = spec * win
    nfft = oversample * 2 * n
    dt = 1.0 / (nfft * df)
    h = np.fft.irfft(spec, nfft)  # sums to spec[0]: the step settles to S11(DC)
    n_pre = min(int(round(2e-9 / dt)), nfft // 4)
    step = np.cumsum(np.roll(h, n_pre))
    t = (np.arange(nfft) - n_pre) * dt
    return t, step


def tdr_z0(
    f: np.ndarray,
    s11: np.ndarray,
    eps_eff: float,
    line_start_mm: float,
    line_mm: float,
    window: Tuple[float, float] = (0.3, 0.6),
    z_ref: float = 50.0,
    pre_delay_s: float = 0.0,
) -> float:
    """Average impedance over the middle of a line from its low-pass step response."""
    t, step = step_response(f, s11)
    v = C_LIGHT / math.sqrt(eps_eff)
    t0 = pre_delay_s + 2 * line_start_mm * 1e-3 / v
    t1 = t0 + 2 * line_mm * 1e-3 / v
    sel = (t >= t0 + window[0] * (t1 - t0)) & (t <= t0 + window[1] * (t1 - t0))
    g = float(np.mean(step[sel]))
    return z_ref * (1 + g) / (1 - g)


# --- quality checks (IEEE 370-style, simplified) -----------------------------------------------


PASSIVITY_TOL = 0.02  # SOLT-corrected data of a lossy line may exceed 1 by the residual errors


def quality(s: np.ndarray) -> dict:
    """Passivity (largest singular value of S over frequency) and reciprocity (max |S12 − S21|)
    of a two-port: the frequency-domain quality checks of IEEE 370 in their simplest form; the
    full metrics (scikit-rf `IEEEP370_FD_QM`) are a later addition."""
    sv = np.linalg.svd(s, compute_uv=False)
    return dict(
        max_singular=float(sv.max()),
        passive=bool(sv.max() <= 1.0 + PASSIVITY_TOL),
        reciprocity=float(np.max(np.abs(s[:, 0, 1] - s[:, 1, 0]))),
    )


# --- scikit-rf cross-check (optional) -----------------------------------------------------------


def skrf_gamma(f, thru, lines, reflect, eps_guess: float) -> np.ndarray:
    """γ from scikit-rf's NISTMultilineTRL (BSD-3); raises if scikit-rf is not installed."""
    try:
        import skrf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("scikit-rf is not installed (pip install scikit-rf)") from exc
    freq = skrf.Frequency.from_f(f, unit="Hz")
    nets = [skrf.Network(frequency=freq, s=thru, name="thru")]
    refl = skrf.Network(frequency=freq, s=reflect, name="reflect")
    ls = sorted(lines, key=lambda x: x[0])
    nets += [refl] + [skrf.Network(frequency=freq, s=s, name=f"l{dl}") for dl, s in ls]
    cal = skrf.NISTMultilineTRL(
        measured=nets,
        Grefls=[-1],
        l=[0.0] + [dl * 1e-3 for dl, _ in ls],
        er_est=eps_guess,
        refl_offset=0.0,
    )
    cal.run()
    return np.asarray(cal.gamma)
