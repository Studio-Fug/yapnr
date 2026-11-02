"""LOCAL CANDIDATE ONLY: circular variation and explicit visibility-preserving search acceptance.

No solver, source release, provider action or physical certification lives here.
"""

import hashlib
import json
import math

import numpy as np
import torch

from .observable_phase import (
    PhaseContract,
    chord,
    diagnostics,
    fixed_mean,
    normalize,
    product_loss,
    product_vjp_numpy,
)

REVISION = "local-observable-variation-candidate/2"


def path_variation(h, frequencies_hz, angle_weights, power_floor, delay_scale_s):
    """h[F,angle,Jones,path]. Pairwise circular counterpart of centered variance.

    Each Jones/path group is separate. No fixed baseline center, arg, detached
    fitted center or zero-absolute-delay target is used. Every endpoint enters
    both sides of every pair, with its full derivative.
    At equal spacing, high visibility and small phase spread this tends to the
    original centered delay variance. Unequal exact spacings are retained in
    alpha_i*alpha_j; this is deliberately NOT a global equality of metrics.
    """
    if h.ndim != 4 or h.shape[0] < 2 or h.shape[-1] != 2:
        raise ValueError("F x angle x Jones x two paths required")
    f = torch.as_tensor(frequencies_hz, dtype=torch.float64, device=h.device)
    w = torch.as_tensor(angle_weights, dtype=torch.float64, device=h.device)
    if f.requires_grad or w.requires_grad or f.shape != (h.shape[0],) or w.shape != (h.shape[1],):
        raise ValueError("Fixed frequency and quadrature contract required")
    if (
        not torch.isfinite(f).all()
        or not torch.isfinite(w).all()
        or (torch.diff(f) <= 0).any()
        or (w <= 0).any()
        or abs(float(w.sum()) - 1) > 1e-12
    ):
        raise ValueError("Invalid fixed sampling")
    if not math.isfinite(delay_scale_s) or delay_scale_s <= 0:
        raise ValueError("Positive delay scale required")
    u = normalize(h, power_floor)
    v = (u[1:] * u[:-1].conj()).reshape(-1, h.shape[2], h.shape[3])
    # Eliminating a mean by all-pairs comparison: variance = half the mean
    # squared pairwise difference, in the equal-spacing small-angle limit.
    z = v[:, None] * v[None, :].conj()
    wm = (w[None, :].expand(h.shape[0] - 1, -1) / (h.shape[0] - 1)).reshape(-1)
    alpha = (
        (2 * torch.pi * torch.diff(f) * delay_scale_s)[:, None].expand(-1, h.shape[1]).reshape(-1)
    )
    coefficients = wm[:, None] * wm[None, :] / (2 * alpha[:, None] * alpha[None, :])
    return (chord(z, torch.zeros_like(z.real)) * coefficients[:, :, None, None]).sum((0, 1)).mean()


def path_variation_vjp_numpy(h, frequencies_hz, angle_weights, power_floor, delay_scale_s):
    """Independent full real-Euclidean VJP; includes both endpoints of every pair."""
    h = np.asarray(h, np.complex128)
    F, A, J, P = h.shape
    interval_count = F - 1
    M = interval_count * A
    wm = np.tile(np.asarray(angle_weights) / interval_count, interval_count)
    alpha = np.repeat(2 * np.pi * np.diff(frequencies_hz) * delay_scale_s, A)
    cot = wm[:, None] * wm[None, :] / (2 * alpha[:, None] * alpha[None, :] * J * P)
    result = np.zeros_like(h)
    for j in range(J):
        for p in range(P):
            lo = h[:-1, :, j, p].reshape(M)
            hi = h[1:, :, j, p].reshape(M)
            factors = np.stack(
                np.broadcast_arrays(lo[:, None], hi[:, None], lo[None, :], hi[None, :]), -1
            )
            g = product_vjp_numpy(
                factors, (True, False, False, True), np.zeros((M, M)), power_floor, cot
            )
            result[:-1, :, j, p] += (g[..., 0].sum(1) + g[..., 2].sum(0)).reshape(interval_count, A)
            result[1:, :, j, p] += (g[..., 1].sum(1) + g[..., 3].sum(0)).reshape(interval_count, A)
    return result


def corrected_terms(h, target, frequencies, angle_weights, policy):
    F, A, J, P = h.shape
    weights = (angle_weights[:, None].expand(A, J) / J).reshape(-1)
    flat = h.reshape(F, A * J, P)
    t = target.reshape(F, A * J)
    phase = fixed_mean(product_loss(flat, (True, False), t, policy.floor), weights)
    factors = torch.stack((flat[:-1, :, 0], flat[:-1, :, 1], flat[1:, :, 0], flat[1:, :, 1]), -1)
    scale = (2 * torch.pi * torch.diff(frequencies) * policy.delay_goal_s).square()
    pair = fixed_mean(
        product_loss(factors, (False, True, True, False), torch.diff(t, dim=0), policy.floor)
        / scale[:, None],
        weights,
    )
    path = path_variation(h, frequencies, angle_weights, policy.floor, policy.delay_goal_s)
    return dict(phase=phase, delay=pair, path_delay_advice=path)


def observable_report(h, target, frequencies, angle_weights, policy, physical_ports):
    """Raw principal-branch diagnostics only; missing observations remain explicit."""
    h = h.detach()
    F, A, J, P = h.shape
    w = (angle_weights[:, None].expand(A, J) / J).reshape(-1)
    report = diagnostics(
        h.reshape(F, A * J, P),
        target.reshape(F, A * J),
        frequencies,
        w,
        PhaseContract(policy.floor, policy.delay_goal_s, np.pi - 0.05),
    )
    report["valid_fraction"] = float(
        (((h.abs().square() >= policy.floor).all(-1)).double().mean((0, 2)) * angle_weights).sum()
    )
    valid = h.abs().square() >= policy.floor
    iv = valid[1:] & valid[:-1]
    steps = torch.full(iv.shape, float("nan"), dtype=torch.float64)
    product = h[1:] * h[:-1].conj()
    steps[iv] = torch.angle(product[iv])
    tau = -steps / (2 * np.pi * torch.diff(frequencies)[:, None, None, None])
    stats = []
    for j in range(J):
        for p in range(P):
            mask = iv[:, :, j, p]
            weights = angle_weights[None, :].expand(F - 1, -1) / (F - 1)
            count = int(mask.sum())
            selected = tau[:, :, j, p][mask]
            sw = weights[mask]
            if count:
                mu = (selected * sw).sum() / sw.sum()
                rms = torch.sqrt(((selected - mu).square() * sw).sum() / sw.sum())
                ptp = selected.max() - selected.min()
                vals = dict(mean_s=float(mu), rms_variation_s=float(rms), peak_to_peak_s=float(ptp))
            else:
                vals = dict(mean_s=None, rms_variation_s=None, peak_to_peak_s=None)
            # These are interval-average metrics on observed support, never a
            # reconstructed dense-frequency physical derivative certificate.
            per_angle = []
            for angle in range(A):
                values = tau[:, angle, j, p][mask[:, angle]]
                per_angle.append(
                    dict(
                        angle=angle,
                        observable_intervals=int(values.numel()),
                        peak_to_peak_s=(
                            float(values.max() - values.min()) if values.numel() > 1 else None
                        ),
                    )
                )
            per_interval = []
            for interval in range(F - 1):
                values = tau[interval, :, j, p][mask[interval]]
                per_interval.append(
                    dict(
                        interval=interval,
                        observable_angles=int(values.numel()),
                        peak_to_peak_s=(
                            float(values.max() - values.min()) if values.numel() > 1 else None
                        ),
                    )
                )
            stats.append(
                dict(
                    per_angle_frequency_interval_variation=per_angle,
                    per_interval_orientation_variation=per_interval,
                    jones=j,
                    path=physical_ports[p],
                    observable_intervals=count,
                    fixed_interval_count=(F - 1) * A,
                    observable_weight=float(sw.sum()),
                    **vals
                )
            )
    phases = [abs(v) for row in report["phase_residual_rad"] for v in row if v is not None]
    report.update(
        objective_revision=REVISION,
        raw_observable_path_variation=stats,
        raw_metric_scope=(
            "Principal-branch interval averages on explicitly reported observable support; "
            "support-weight normalization is diagnostic only, never used in losses."
        ),
        max_wrapped_phase_residual_deg=None if not phases else float(np.rad2deg(max(phases))),
        delay_certified=False,
        physical_phase_delay_fulfilled=False,
        execution_authorized=False,
    )
    return report


def context_digest(
    policy, frequencies, directions, jones, normalization_identity, physical_context_identity
):
    """Caller must retain the actual source/model/reference identity, not invent it."""
    if (
        not isinstance(normalization_identity, str)
        or len(normalization_identity) != 64
        or any(c not in "0123456789abcdef" for c in normalization_identity)
    ):
        raise ValueError("Normalization identity required")
    if (
        not isinstance(physical_context_identity, str)
        or len(physical_context_identity) != 64
        or any(c not in "0123456789abcdef" for c in physical_context_identity)
    ):
        raise ValueError("Frozen model/source/reference-plane identity required")

    def a(v):
        return hashlib.sha256(np.ascontiguousarray(v).tobytes()).hexdigest()

    return hashlib.sha256(
        json.dumps(
            dict(
                revision=REVISION,
                policy=policy.digest(),
                frequencies=a(frequencies),
                directions=a(directions),
                jones=a(jones),
                normalization=normalization_identity,
                physical_context=physical_context_identity,
            ),
            sort_keys=True,
        ).encode()
    ).hexdigest()


# Acceptance revision 3 lives in acceptance.py and local_trial.py.
