"""Port waves, S-parameters, de-embedding, renormalization and passivity (design §5.4).

Internally the time dependence is e^{−iωt} (forward waves e^{ikx}); everything reported
(Touchstone, JSON, phase targets) uses the engineering e^{+jωt} convention, the complex
conjugate (`to_engineering`).

    a = (V̂ + Z_c Î)/(2√R_c),   b = (V̂ − Z_c Î)/(2√R_c),   R_c = Re Z_c
    a_ref = a e^{+iβ d_m},     b_ref = b e^{−iβ d_m}       (reference plane d_m ahead)
    S_ij = b_ref,i / a_ref,j   (port j excited),  P_inc = |a_ref|²/2

The de-embedding shifts phases only (β = Re k, `port_waves`): the line loss over the d_m ≈ 3h of
feed between the measurement and the reference planes is about 0.002 dB per port on the cases'
lines (α ≈ 0.7 Np/m), while the calibration's Im k is not accurate to that level (a two-plane
extraction over a quarter wave resolves α only to about ±2 Np/m), and de-embedding with it
inflated |S| by up to 0.15 dB and produced non-passive S-matrices. Neglecting the feed loss
makes the reported |S| low by α(d_i + d_j), a conservative bias of under 0.01 dB.

The wave functions accept numpy arrays or torch tensors (complex128) so objectives can be
differentiated with autograd.
"""

from __future__ import annotations

import numpy as np

from yapnr.rf.numerics import cmul


def _as(x, ref):
    """`x` (numpy) as the array type of `ref`."""
    if isinstance(ref, np.ndarray) or np.isscalar(ref):
        return np.asarray(x)
    import torch

    return torch.as_tensor(np.asarray(x), dtype=torch.complex128)


def waves(v, i, zc, k, d_m: float):
    """Incident and reflected waves (a_ref, b_ref) at the reference plane of a line port."""
    zc_ = _as(np.asarray(zc, dtype=np.complex128), v)
    root = _as(2.0 * np.sqrt(np.asarray(zc).real), v)
    ph = _as(np.exp(1j * np.asarray(k) * d_m), v)
    zi = cmul(zc_, i)
    a = cmul((v + zi) / root, ph)
    b = (v - zi) / root / ph
    return a, b


def port_waves(geom, cal, dft, omega):
    """(a_ref, b_ref) of a `PortGeometry` with its `LineCalibration` from probe DTFTs; the
    de-embedding uses Re k (see the module doc)."""
    zc, k = cal.at(omega)
    return waves(geom.voltage(dft), geom.current(dft), zc, np.real(k), geom.d_m)


def incident_power(a, cal=None, omega=None):
    """P_inc = ½|a|², times the calibration's power factor when `cal` is given (to compare
    with Poynting fluxes, e.g. for the radiated fraction)."""
    p = 0.5 * abs(a) ** 2
    if cal is None:
        return p
    return p * _as(cal.power_at(omega if omega is not None else cal.omega), a).real


def to_engineering(s):
    """Convert from the internal e^{−iωt} convention to the engineering e^{+jωt} convention."""
    return np.conj(s)


def renormalize(s: np.ndarray, zc: np.ndarray, z_ref: float = 50.0) -> np.ndarray:
    """Renormalize pseudo-wave S-matrices (..., N, N) referenced to per-port real Z_c
    (..., N) to a common real z_ref through the impedance matrix.

    Z = √Z (I + S)(I − S)⁻¹ √Z,   S' = (Z − z_ref)(Z + z_ref)⁻¹.
    """
    s = np.asarray(s, dtype=np.complex128)
    zc = np.asarray(zc).real
    n = s.shape[-1]
    eye = np.eye(n)
    sq = np.sqrt(zc)[..., :, None] * eye
    z = sq @ (eye + s) @ np.linalg.inv(eye - s) @ sq
    return (z - z_ref * eye) @ np.linalg.inv(z + z_ref * eye)


def passivity_margin(s: np.ndarray) -> np.ndarray:
    """Smallest eigenvalue of I − SᴴS per frequency (≥ 0 for a passive network)."""
    s = np.asarray(s, dtype=np.complex128)
    m = np.eye(s.shape[-1]) - np.conj(np.swapaxes(s, -1, -2)) @ s
    return np.linalg.eigvalsh(m).min(axis=-1)


def db(x):
    """20 log10 |x|."""
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-300))
