"""Reproducible complex arithmetic for numpy arrays.

numpy's complex multiply (and complex BLAS products such as `x @ w`) can round differently from
call to call: its vectorized and scalar loops (with and without fused multiply-add) are chosen
by the arrays' memory alignment, so the last bit of a product depends on where the allocator put
the operands (measured on arm64, numpy 1.26). Real multiply, add, divide, sum and the real
elementwise functions are reproducible. These helpers express the complex products of the
post-processing with real arithmetic for numpy arrays, so results repeat bit for bit; torch
tensors (whose CPU complex multiply is reproducible) pass through to ordinary operators.
"""

from __future__ import annotations

import numpy as np


def _np(x) -> bool:
    return isinstance(x, np.ndarray) or np.isscalar(x)


def make_complex(re, im) -> np.ndarray:
    """re + i·im without a complex multiply."""
    re, im = np.broadcast_arrays(np.asarray(re, np.float64), np.asarray(im, np.float64))
    out = np.empty(re.shape, dtype=np.complex128)
    out.real = re
    out.imag = im
    return out


def cmul(a, b):
    """a·b, reproducible for numpy operands (complex or real)."""
    if not (_np(a) and _np(b)):
        return a * b
    a, b = np.asarray(a), np.asarray(b)
    if not (np.iscomplexobj(a) and np.iscomplexobj(b)):
        if np.iscomplexobj(a):
            return make_complex(a.real * b, a.imag * b)
        if np.iscomplexobj(b):
            return make_complex(a * b.real, a * b.imag)
        return a * b
    return make_complex(a.real * b.real - a.imag * b.imag, a.real * b.imag + a.imag * b.real)


def wsum(x, w):
    """Σ_p x[..., p] w[p] for real weights w (numpy) or tensors (torch)."""
    if _np(x):
        x = np.asarray(x)
        w = np.asarray(w, dtype=np.float64)
        if np.iscomplexobj(x):
            return make_complex((x.real * w).sum(-1), (x.imag * w).sum(-1))
        return (x * w).sum(-1)
    import torch

    return (x * torch.as_tensor(np.asarray(w), dtype=torch.float64).to(x.dtype)).sum(-1)


def re_conj_product(e, h):
    """Re(e · conj(h))."""
    if _np(e) and _np(h):
        e, h = np.asarray(e), np.asarray(h)
        return e.real * h.real + e.imag * h.imag
    return (e * h.conj()).real
