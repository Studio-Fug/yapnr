"""The conic filter (design §7.2; Hammond et al. Eq. 3).

    ρ̃ = w * ρ_ext,     w(r) ∝ max(0, 1 − r/R),     Σ w = 1

on a uniform pixel grid of pitch Δ. The kernel has (2n + 1)² taps with n = ⌈R/Δ⌉ − 1 (the
offsets with r < R). It is applied as a valid convolution of the extended grid (window plus
ring), so the output is the window plus a margin of r − n pixels: with the ring width
r = ⌈R/Δ⌉ + 1 = n + 2 of the design, a margin of 2, which the length-scale constraints use for
their gradients at the window's edge. The kernel is symmetric, so convolution and correlation
agree and the filter's transpose is the same convolution of the zero-padded cotangent
(torch autograd provides it).
"""

from __future__ import annotations

import math

import numpy as np


def kernel_half_width(radius: float, pitch: float) -> int:
    """n = ⌈R/Δ⌉ − 1 (the largest offset with a positive weight), 0 for R ≤ Δ."""
    if radius <= 0.0:
        return 0
    return max(0, int(math.ceil(radius / pitch - 1e-9)) - 1)


def conic_kernel(radius: float, pitch: float) -> np.ndarray:
    """The normalized conic kernel ((2n + 1), (2n + 1)); [[1]] for R = 0."""
    n = kernel_half_width(radius, pitch)
    if radius <= 0.0:
        return np.ones((1, 1))
    d = np.arange(-n, n + 1) * pitch
    r = np.hypot(d[:, None], d[None, :])
    w = np.maximum(0.0, 1.0 - r / radius)
    return w / w.sum()


def ring_width_for(radius: float, pitch: float) -> int:
    """The exterior ring of the design: ⌈R/Δ⌉ + 1 pixels (at least 2)."""
    return kernel_half_width(radius, pitch) + 2


class ConicFilter:
    """The conic filter of radius `radius` on pixels of `pitch` (both in metres)."""

    def __init__(self, radius: float, pitch: float):
        self.radius = float(radius)
        self.pitch = float(pitch)
        self.kernel = conic_kernel(self.radius, self.pitch)
        self.half = (self.kernel.shape[0] - 1) // 2

    @property
    def ring_width(self) -> int:
        return self.half + 2

    def __call__(self, ext):
        """Valid convolution of `ext` (A, B): (A − 2n, B − 2n). numpy or torch input."""
        if isinstance(ext, np.ndarray):
            return self._numpy(ext)
        import torch
        import torch.nn.functional as F

        k = torch.as_tensor(self.kernel, dtype=ext.dtype)[None, None]
        return F.conv2d(ext[None, None], k)[0, 0]

    def _numpy(self, ext: np.ndarray) -> np.ndarray:
        n = self.half
        a, b = ext.shape
        out = np.zeros((a - 2 * n, b - 2 * n))
        for di in range(2 * n + 1):
            for dj in range(2 * n + 1):
                w = self.kernel[di, dj]
                if w:
                    out += w * ext[di : di + a - 2 * n, dj : dj + b - 2 * n]
        return out
