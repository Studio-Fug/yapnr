"""The tanh projection (design §7.3; Hammond et al. Eq. 4) and its Heaviside limit.

    ρ̄ = [tanh(βη) + tanh(β(ρ̃ − η))] / [tanh(βη) + tanh(β(1 − η))]

maps 0 → 0, η → η and 1 → 1 for every β > 0; β = ∞ is the Heaviside step ρ̄ = [ρ̃ > η]. β runs
through a schedule (8, 16, 32, 64, 128 by default, `optim.schedule`).
"""

from __future__ import annotations

import math

import numpy as np


def tanh_projection(x, beta: float, eta: float = 0.5):
    """ρ̄(ρ̃) for numpy arrays or torch tensors; β = inf gives the Heaviside step."""
    if isinstance(x, np.ndarray) or np.isscalar(x):
        x = np.asarray(x, dtype=np.float64)
        if math.isinf(beta):
            return (x > eta).astype(np.float64)
        num = np.tanh(beta * eta) + np.tanh(beta * (x - eta))
        return num / (np.tanh(beta * eta) + np.tanh(beta * (1.0 - eta)))
    import torch

    if math.isinf(beta):
        return (x > eta).to(x.dtype)
    den = math.tanh(beta * eta) + math.tanh(beta * (1.0 - eta))
    return (math.tanh(beta * eta) + torch.tanh(beta * (x - eta))) / den


def tanh_projection_derivative(x, beta: float, eta: float = 0.5) -> np.ndarray:
    """dρ̄/dρ̃ (numpy); zero for β = inf."""
    x = np.asarray(x, dtype=np.float64)
    if math.isinf(beta):
        return np.zeros_like(x)
    den = math.tanh(beta * eta) + math.tanh(beta * (1.0 - eta))
    return beta * (1.0 - np.tanh(beta * (x - eta)) ** 2) / den
