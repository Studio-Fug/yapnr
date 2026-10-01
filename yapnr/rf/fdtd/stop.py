"""Run-length rule: stop once every monitored DTFT has converged (design §4.8)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StopRule:
    """Stop after `consecutive` checks with max|ΔX| / max|X| < tol for every checked probe.

    Checks start once every source has ended and repeat every `check_every` steps (default
    ⌈1/(f_lo Δt)⌉, one period of the lowest frequency of interest). `max_steps` is a hard cap;
    a run that reaches it is reported as not converged. `probes` limits the check to the named
    probes (default: all).
    """

    tol: float = 1e-3
    f_lo: float = 1e9
    max_steps: int = 200_000
    check_every: int | None = None
    consecutive: int = 2
    min_steps: int = 0
    probes: tuple[str, ...] | None = None

    def period(self, dt: float) -> int:
        if self.check_every:
            return int(self.check_every)
        return max(1, int(math.ceil(1.0 / (self.f_lo * dt))))


def relative_change(new: dict, old: dict, names) -> float:
    """max over probes of max|new − old| / max|new| (probes that are all zero are skipped)."""
    worst = 0.0
    scale_all = max((float(np.max(np.abs(new[n]))) if new[n].size else 0.0) for n in names)
    for n in names:
        if new[n].size == 0:
            continue
        scale = float(np.max(np.abs(new[n])))
        if scale <= 1e-300 or scale < 1e-14 * scale_all:
            continue
        worst = max(worst, float(np.max(np.abs(new[n] - old[n]))) / scale)
    return worst
