"""Design metrics: the gray-level measure (design §7.6)."""

from __future__ import annotations

import numpy as np


def gray_measure(rho_bar, mask=None) -> float:
    """M_nd = 4/n Σ ρ̄(1 − ρ̄) over the pixels (of `mask`): 0 for a binary design, 1 for ρ̄ = ½."""
    r = np.asarray(rho_bar, dtype=np.float64)
    if mask is not None:
        r = r[np.asarray(mask, bool)]
    if r.size == 0:
        return 0.0
    return float(4.0 * np.mean(r * (1.0 - r)))
