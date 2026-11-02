"""Bounded C1 complex-response losses and separate observable diagnostics.

Local candidate uses the reviewed proposal primitives. No solver/provider calls.
Diagnostic threshold comparisons retain r3 abs().square() evaluation order.
Tensor shapes: h [frequency, sample, pair_port=2]; sample includes every fixed
direction/polarization. Fixed sample weights sum to one and are never remasked.
"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass

import numpy as np
import torch

SCHEMA = "qm33120-observable-complex-objective/1"


@dataclass(frozen=True)
class PhaseContract:
    power_floor: float
    delay_scale_s: float
    alias_limit_rad: float  # caller copies the existing frozen value, no default
    schema: str = SCHEMA

    def __post_init__(self):
        if (
            not math.isfinite(self.power_floor)
            or self.power_floor <= 0
            or not math.isfinite(self.delay_scale_s)
            or self.delay_scale_s <= 0
            or not 0 < self.alias_limit_rad < math.pi
            or self.schema != SCHEMA
        ):
            raise ValueError("Positive frozen scales and explicit existing alias guard required")

    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def _power(h):
    if h.dtype != torch.complex128 or not torch.isfinite(h).all():
        raise ValueError("Finite complex128 normalized response required")
    p = h.real.square() + h.imag.square()
    if not torch.isfinite(p).all():
        raise ValueError("Power overflow; no silent normalization")
    return p


def normalize(h, power_floor):
    """u=h/sqrt(|h|²+P0). Real Jacobian norm <= 1/sqrt(P0)."""
    if not math.isfinite(power_floor) or power_floor <= 0:
        raise ValueError("Positive power floor required")
    return h / torch.sqrt(_power(h) + power_floor)


def chord(z, target_rad):
    """|z-|z| exp(i target)|². C1 at z=0, with unique zero derivative.

    Equal to 2|z|²(1-cos(residual phase)) when z!=0, never evaluating arg.
    Bound: 0<=C<=4|z|²; ||grad_z C||<=8|z|. No phase fulfillment at z=0.
    """
    _power(z)
    target = torch.as_tensor(target_rad, dtype=torch.float64, device=z.device)
    if target.shape != z.shape or not torch.isfinite(target).all():
        raise ValueError("Fixed target shape/values required")
    if target.requires_grad:
        raise ValueError("Phase target is fixed; no angle-dependent fit")
    residual = z - z.abs() * torch.exp(1j * target)
    return residual.real.square() + residual.imag.square()


def product_loss(h, conjugated, target_rad, power_floor):
    """Last axis factors. Each normalized factor is bounded; no divide by h."""
    if (
        h.shape[-1] != len(conjugated)
        or not conjugated
        or any(type(v) is not bool for v in conjugated)
    ):
        raise ValueError("Explicit factor conjugations required")
    u = normalize(h, power_floor)
    z = torch.ones(h.shape[:-1], dtype=h.dtype, device=h.device)
    for j, conj in enumerate(conjugated):
        z = z * (u[..., j].conj() if conj else u[..., j])
    return chord(z, target_rad)


def floor_loss(h, power_floor):
    if not math.isfinite(power_floor) or power_floor <= 0:
        raise ValueError("Positive power floor required")
    return torch.relu(1 - _power(h) / power_floor).square()


def fixed_mean(values, weights):
    """Mean frequency/port axes, full fixed sample denominator. No valid mask."""
    if values.ndim < 2:
        raise ValueError("Expected frequency x sample axes")
    w = torch.as_tensor(weights, dtype=torch.float64, device=values.device)
    if (
        w.requires_grad
        or w.shape != (values.shape[1],)
        or not torch.isfinite(w).all()
        or (w <= 0).any()
    ):
        raise ValueError("Fixed strictly positive quadrature/polarization weights required")
    if abs(float(w.sum()) - 1) > 1e-12:
        raise ValueError("Weights must already sum to one; no candidate renormalization")
    expanded = w.reshape(1, len(w), *(1,) * (values.ndim - 2))
    return (values * expanded).sum(1).mean()


def band_terms(h, target_rad, frequencies_hz, weights, contract, *, path_step_target_rad):
    """Differentiable pair phase, pair delay, floor and explicit path-step loss.

    path_step_target_rad is REQUIRED, never fitted or silently set to zero.
    Do not wire this path loss over an existing variance/centering objective
    without reviewing that objective. It is an interval residual, not a claim
    of APH511 peak-to-peak group-delay qualification.
    """
    if h.ndim != 3 or h.shape[-1] != 2 or h.shape[0] < 2:
        raise ValueError("Expected frequency x sample x two paths")
    f = torch.as_tensor(frequencies_hz, dtype=torch.float64, device=h.device)
    if (
        f.requires_grad
        or f.shape != (h.shape[0],)
        or not torch.isfinite(f).all()
        or (torch.diff(f) <= 0).any()
    ):
        raise ValueError("Frozen ordered frequency vector required")
    target = torch.as_tensor(target_rad, dtype=torch.float64, device=h.device)
    if target.shape != h.shape[:-1] or target.requires_grad:
        raise ValueError("Frozen spatial pair target required")
    step_target = torch.as_tensor(path_step_target_rad, dtype=torch.float64, device=h.device)
    if step_target.shape != (h.shape[0] - 1, h.shape[1], 2) or step_target.requires_grad:
        raise ValueError("Explicit fixed per-path interval targets required")
    p0 = contract.power_floor
    phase = product_loss(h, (True, False), target, p0)
    factors = torch.stack((h[:-1, :, 0], h[:-1, :, 1], h[1:, :, 0], h[1:, :, 1]), -1)
    scale = (2 * torch.pi * torch.diff(f) * contract.delay_scale_s).square()
    pair_delay = (
        product_loss(factors, (False, True, True, False), torch.diff(target, dim=0), p0)
        / scale[:, None]
    )
    path_factors = torch.stack((h[:-1], h[1:]), -1)
    path_delay = product_loss(path_factors, (True, False), step_target, p0) / scale[:, None, None]
    floor = floor_loss(h, p0)
    raw = dict(phase=phase, pair_delay=pair_delay, path_step=path_delay, floor=floor)
    return {k: fixed_mean(v, weights) for k, v in raw.items()}, raw


def product_vjp_numpy(h, conjugated, target_rad, power_floor, cotangent=None):
    """Independent real-Euclidean analytic VJP; dL=Re(sum(conj(g)*dh)).

    Does not use autograd, angles, finite differences, or division by a factor.
    """
    h = np.asarray(h, np.complex128)
    p = h.real * h.real + h.imag * h.imag
    d = np.sqrt(p + power_floor)
    u = h / d
    factors = [np.conj(u[..., j]) if c else u[..., j] for j, c in enumerate(conjugated)]
    z = np.prod(np.stack(factors), axis=0)
    r = np.abs(z)
    t = np.exp(1j * np.asarray(target_rad))
    a = (z * np.conj(t)).real
    ratio = np.divide(a, r, out=np.zeros_like(a), where=r > 0)
    gz = 4 * z - 2 * (ratio * z + r * t)
    if cotangent is not None:
        gz = gz * np.asarray(cotangent)
    g = np.zeros_like(h)
    for j, c in enumerate(conjugated):
        other = np.ones_like(z)
        for k, factor in enumerate(factors):
            if k != j:
                other *= factor
        gu = gz * np.conj(other)
        if c:
            gu = np.conj(gu)
        g[..., j] = gu / d[..., j] - h[..., j] * (np.conj(gu) * h[..., j]).real / d[..., j] ** 3
    return g


def diagnostics(h, target_rad, frequencies_hz, weights, contract):
    """Raw observable-only angles/delays; never connected to the gradient graph.

    Invalid samples are NaN internally and None in the persisted report. Their
    count and fixed denominator remain explicit. Absence of a warning never
    proves absence of integer-turn aliases between the sparse samples.
    """
    h = h.detach()
    _power(h)
    p = h.abs().square()
    target = torch.as_tensor(target_rad, dtype=torch.float64, device=h.device).detach()
    if h.ndim != 3 or h.shape[-1] != 2 or target.shape != h.shape[:-1]:
        raise ValueError("Diagnostic shapes differ")
    f = torch.as_tensor(frequencies_hz, dtype=torch.float64, device=h.device).detach()
    df = torch.diff(f)
    if f.shape != (h.shape[0],) or not torch.isfinite(f).all() or (df <= 0).any():
        raise ValueError("Frequency diagnostic contract differs")
    fixed_mean(p, weights)  # validate weights without changing coverage denominator
    w = torch.as_tensor(weights, dtype=torch.float64, device=h.device)
    valid_path = p >= contract.power_floor
    valid_pair = valid_path.all(-1)
    u = normalize(h, contract.power_floor)
    q = u[..., 1] * u[..., 0].conj()
    phase_valid = valid_pair
    pair_valid = valid_pair[1:] & valid_pair[:-1]
    path_valid = valid_path[1:] & valid_path[:-1]

    def observed_angle(z, valid):
        out = torch.full(z.shape, float("nan"), dtype=torch.float64, device=z.device)
        out[valid] = torch.angle(z[valid])
        return out

    phase = observed_angle(q * torch.exp(-1j * target), phase_valid)
    pair_step = observed_angle(
        q[1:] * q[:-1].conj() * torch.exp(-1j * torch.diff(target, dim=0)), pair_valid
    )
    path_step = observed_angle(u[1:] * u[:-1].conj(), path_valid)

    def warning(step, valid):
        return bool((step[valid].abs() >= contract.alias_limit_rad).any())

    def nullable(values):
        def clean(v):
            if isinstance(v, list):
                return [clean(a) for a in v]
            return v if math.isfinite(v) else None

        return clean(values.cpu().tolist())

    counts = valid_pair.sum(1).cpu().tolist()
    intervals = pair_valid.sum(1).cpu().tolist()
    floor_shortfall = torch.relu(1 - p / contract.power_floor)
    result = dict(
        schema=SCHEMA,
        contract_sha256=contract.digest(),
        delay_alias_warning=warning(pair_step, pair_valid),
        path_delay_alias_warning=warning(path_step, path_valid),
        alias_limit_rad=contract.alias_limit_rad,
        alias_free_proven=False,
        warning_semantics="large_principal_increment_on_observable_endpoints_only",
        unwrapping_certified=False,
        continuous_band_observability_verified=False,
        frequency_count=h.shape[0],
        samples_per_frequency=h.shape[1],
        valid_phase_samples_by_frequency=counts,
        valid_delay_samples_by_interval=intervals,
        valid_path_samples_by_interval=path_valid.sum(1).cpu().tolist(),
        valid_phase_samples=int(valid_pair.sum()),
        valid_delay_samples=int(pair_valid.sum()),
        coverage_denominator=valid_pair.numel(),
        interval_coverage_denominator=pair_valid.numel(),
        valid_fraction=float(valid_pair.double().mean()),
        weighted_valid_fraction_by_frequency=(valid_pair.double() * w).sum(1).cpu().tolist(),
        missing_phase_samples=int((~valid_pair).sum()),
        missing_delay_samples=int((~pair_valid).sum()),
        floor_infeasible=bool((floor_shortfall > 0).any()),
        maximum_normalized_floor_violation=float(floor_shortfall.max()),
        all_requested_samples_observable=bool(valid_pair.all()),
        physical_phase_delay_fulfilled=False,
        physical_status=(
            "unobservable_samples_remain"
            if not bool(valid_pair.all())
            else "observable_samples_require_physical_validation"
        ),
        phase_residual_rad=nullable(phase),
        pair_phase_step_rad=nullable(pair_step),
        path_phase_step_rad=nullable(path_step),
        principal_interval_pair_delay_s=nullable(-pair_step / (2 * torch.pi * df[:, None])),
        principal_interval_path_delay_s=nullable(-path_step / (2 * torch.pi * df[:, None, None])),
        phase_valid_mask=valid_pair.cpu().tolist(),
        pair_interval_valid_mask=pair_valid.cpu().tolist(),
        path_interval_valid_mask=path_valid.cpu().tolist(),
        normalization_values_supplied=False,
    )
    # Keep losses and physical status separate from whether the sparse numerical
    # objective is eligible for evaluation. No new minimum coverage fraction.
    return result


class OperabilityError(RuntimeError):
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def require_evaluation_eligible(diagnostic, *, persist):
    """Persist BEFORE checking. Does not authorize execution or claim fulfillment.

    Caller must augment the record with actual incident/source normalization
    values, field/objective hashes and runtime provenance before this call.
    The required persistence callback must return True only after durable save.
    """
    if persist(diagnostic) is not True:
        raise OperabilityError("Pre-gate evidence was not durably saved", diagnostic)
    if diagnostic.get("schema") != SCHEMA:
        raise OperabilityError("Wrong diagnostic schema", diagnostic)
    nf, ns = diagnostic.get("frequency_count"), diagnostic.get("samples_per_frequency")
    if type(nf) is not int or nf < 2 or type(ns) is not int or ns < 1:
        raise OperabilityError("Missing frozen sample denominator", diagnostic)
    if (
        diagnostic.get("coverage_denominator") != nf * ns
        or diagnostic.get("interval_coverage_denominator") != (nf - 1) * ns
    ):
        raise OperabilityError("Coverage denominator changed", diagnostic)
    for key in ("delay_alias_warning", "path_delay_alias_warning"):
        if diagnostic.get(key) is not False:
            raise OperabilityError(key, diagnostic)
    for key, expected in [
        ("valid_phase_samples_by_frequency", nf),
        ("valid_delay_samples_by_interval", nf - 1),
    ]:
        counts = diagnostic.get(key)
        if (
            not isinstance(counts, list)
            or len(counts) != expected
            or any(type(c) is not int or not 0 < c <= ns for c in counts)
        ):
            raise OperabilityError("No observable support: " + key, diagnostic)
    return dict(
        numerical_evaluation_eligible=True,
        physical_phase_delay_fulfilled=False,
        floor_infeasible=diagnostic["floor_infeasible"],
        execution_authorized=False,
    )
