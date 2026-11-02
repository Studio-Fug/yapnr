"""Saved-value derivative check; finite differences validate, never form updates."""

import numpy as np


def check_direction(
    base,
    gradient,
    tight_gradient,
    direction,
    values,
    steps=(1e-4, 5e-5),
    relative_gate=1e-3,
    stability_gate=2.5e-4,
):
    if relative_gate != 1e-3 or stability_gate != 2.5e-4 or tuple(steps) != (1e-4, 5e-5):
        raise ValueError("Version a protocol rather than relax the frozen actual-array gates")
    base = np.asarray(base, float)
    g = np.asarray(gradient, float)
    gt = np.asarray(tight_gradient, float)
    d = np.asarray(direction, float)
    if g.shape != gt.shape or g.shape != (len(base), len(d)):
        raise ValueError("Rows/variables differ")
    if not all(np.isfinite(a).all() for a in (base, g, gt, d)):
        raise ValueError("Nonfinite validation inputs")
    adj = g @ d
    scale_g = np.maximum.reduce(
        [np.linalg.norm(g, axis=1), np.linalg.norm(gt, axis=1), np.full(len(base), 1e-12)]
    )
    stable = np.linalg.norm(g - gt, axis=1) / scale_g
    fds = []
    for h in steps:
        for t in (h, 2 * h):
            if t not in values or np.asarray(values[t]).shape != base.shape:
                raise ValueError("Missing same-contract FD value")
        fds.append((-3 * base + 4 * np.asarray(values[h]) - np.asarray(values[2 * h])) / (2 * h))
    fds = np.asarray(fds)
    denominator = np.maximum(np.maximum(abs(fds), abs(adj)[None]), 1e-12)
    errors = abs(fds - adj[None]) / denominator
    plateau = abs(fds[-1] - fds[-2]) / np.maximum(np.maximum(abs(adj), abs(fds[-1])), 1e-12)
    informative = abs(adj) > np.maximum(1e-12, 1e-8 * np.linalg.norm(g, axis=1) * np.linalg.norm(d))
    passed = (
        informative
        & np.all(errors <= relative_gate, axis=0)
        & (plateau <= relative_gate)
        & (stable <= stability_gate)
    )
    return dict(
        passed=bool(passed.all()),
        row_passed=passed.tolist(),
        informative=informative.tolist(),
        relative_errors=errors.tolist(),
        plateau=plateau.tolist(),
        gradient_stability=stable.tolist(),
        finite_differences_supply_optimizer_gradients=False,
        actual_array_qualified=False,
        note=(
            "Requires independent source/record/replay evidence and both frozen directions "
            "before array derivative qualification"
        ),
    )
