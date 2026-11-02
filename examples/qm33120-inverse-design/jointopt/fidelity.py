"""Optional frozen-waveform frequency-domain fidelity evaluator.

This is not activated by fabricated QM33120 spectra. Caller supplies the actual
waveform spectrum, integration weights and allowed constant-delay grid. No
frequency-dependent equalizer or angular phase correction is fitted.
"""

import numpy as np
import torch


def pulse_fidelity(transfer, frequency_hz, spectrum, quadrature, delay_grid_s):
    f = torch.as_tensor(frequency_hz, dtype=torch.float64)
    x = torch.as_tensor(spectrum, dtype=torch.complex128)
    w = torch.as_tensor(quadrature, dtype=torch.float64)
    tau = torch.as_tensor(delay_grid_s, dtype=torch.float64)
    if len(f) < 8 or len(f) != len(x) or len(f) != len(w) or transfer.shape[0] != len(f):
        raise ValueError(
            "Frozen dense waveform/sweep required; six-point derivative fixture is insufficient"
        )
    if bool((torch.diff(f) <= 0).any()) or bool((w <= 0).any()) or tau.ndim != 1 or len(tau) < 1:
        raise ValueError("Invalid spectral quadrature or constant delay search")
    if not all(torch.isfinite(v).all() for v in (transfer, f, x, w, tau)):
        raise ValueError("Nonfinite waveform response")
    weight = w * x.abs().square()
    energy_in = weight.sum()
    if energy_in <= 0:
        raise ValueError("Zero-energy transmit waveform")
    flat = transfer.reshape(len(f), -1)
    energy_out = (weight[:, None] * flat.abs().square()).sum(0)
    corr = torch.einsum(
        "f,fp,ft->tp",
        weight.to(torch.complex128),
        flat,
        torch.exp(2j * np.pi * f[:, None] * tau[None]),
    )
    fidelity = corr.abs().amax(0) / torch.sqrt(energy_in * energy_out).clamp_min(1e-30)
    return fidelity.reshape(transfer.shape[1:]), (energy_out > 1e-20).reshape(transfer.shape[1:])
