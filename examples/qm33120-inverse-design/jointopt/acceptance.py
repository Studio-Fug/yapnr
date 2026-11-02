"""Inactive acceptance policy /3: Pareto progress on fixed observable support.

No new physical thresholds, scalar weights, branch certification or dispatch.
A proposed trial is not accepted progress; accepted progress is not qualification.
"""

import hashlib
import math

import numpy as np
import torch

from .observable_phase import PhaseContract, diagnostics

ACCEPTANCE_REVISION = "local-observable-acceptance/3"


def compare_values(reference, candidate):
    """Exact finite binary64 partial order; no hidden numerical tolerance."""
    a = np.asarray(reference, dtype=np.float64)
    b = np.asarray(candidate, dtype=np.float64)
    if a.shape != b.shape or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("Invalid comparable metric vector")
    return dict(
        compared=int(a.size),
        improved=int(np.count_nonzero(b < a)),
        regressed=int(np.count_nonzero(b > a)),
        reference_max=float(a.max()) if a.size else None,
        candidate_max=float(b.max()) if b.size else None,
        max_increase=float(np.maximum(b - a, 0).max()) if a.size else 0.0,
        reference_sha256=hashlib.sha256(a.tobytes()).hexdigest(),
        candidate_sha256=hashlib.sha256(b.tobytes()).hexdigest(),
    )


def band_evidence(reference_h, candidate_h, target, frequencies, angle_weights, policy, ports):
    """h[F,A,J,P]. All comparisons use the REFERENCE observable support.

    Pair phase and excess pair delay: absolute residual per observed sample.
    Path variation: every absolute difference of principal interval delays,
    separately for each Jones/path group. No fitted center or confidence weight.
    New support is reported but is not manufactured into comparable improvement.
    """
    a = torch.as_tensor(reference_h, dtype=torch.complex128).detach()
    b = torch.as_tensor(candidate_h, dtype=torch.complex128).detach()
    if a.shape != b.shape or a.ndim != 4 or a.shape[-1] != 2:
        raise ValueError("Matching F,A,J,2 responses required")
    if not torch.isfinite(a).all() or not torch.isfinite(b).all():
        raise ValueError("Nonfinite response")
    F, A, J, P = a.shape
    sw = (angle_weights[:, None].expand(A, J) / J).reshape(-1)
    contract = PhaseContract(policy.floor, policy.delay_goal_s, np.pi - 0.05)
    ad = diagnostics(a.reshape(F, A * J, P), target.reshape(F, A * J), frequencies, sw, contract)
    bd = diagnostics(b.reshape(F, A * J, P), target.reshape(F, A * J), frequencies, sw, contract)
    pa = a.abs().square()
    pb = b.abs().square()
    da = torch.relu((policy.floor - pa) / policy.floor)
    db = torch.relu((policy.floor - pb) / policy.floor)
    va = pa >= policy.floor
    vb = pb >= policy.floor
    floor = compare_values(da.numpy(), db.numpy())
    support_lost = int((va & ~vb).sum())
    support_new = int((~va & vb).sum())
    comparisons = {}
    metrics = {}
    unavailable = []

    # This helper deliberately returns no fabricated numbers for lost support.
    def metric(name, key, mask):
        x = np.asarray(ad[key], float)
        y = np.asarray(bd[key], float)
        if not np.isfinite(y[mask]).all():
            unavailable.append(name)
            return
        aa = np.abs(x[mask])
        bb = np.abs(y[mask])
        comparisons[name] = compare_values(aa, bb)
        metrics[name] = dict(reference=aa.tolist(), candidate=bb.tolist())

    metric(
        "absolute_pair_phase_residual_rad",
        "phase_residual_rad",
        np.asarray(ad["phase_valid_mask"], bool),
    )
    metric(
        "absolute_excess_pair_delay_s",
        "principal_interval_pair_delay_s",
        np.asarray(ad["pair_interval_valid_mask"], bool),
    )
    av = np.asarray(ad["path_interval_valid_mask"], bool).reshape(F - 1, A, J, P)
    at = np.asarray(ad["principal_interval_path_delay_s"], float).reshape(F - 1, A, J, P)
    bt = np.asarray(bd["principal_interval_path_delay_s"], float).reshape(F - 1, A, J, P)
    path = []
    for j in range(J):
        for port in range(P):
            mask = av[:, :, j, port]
            x = at[:, :, j, port][mask]
            y = bt[:, :, j, port][mask]
            name = f"path_variation_J{j}_P{ports[port]}"
            if not np.isfinite(y).all():
                unavailable.append(name)
                continue
            ii, jj = np.triu_indices(len(x), 1)
            d1 = np.abs(x[ii] - x[jj])
            d2 = np.abs(y[ii] - y[jj])
            comparisons[name] = compare_values(d1, d2)
            path.append(
                dict(
                    name=name,
                    reference_support_indices=np.argwhere(mask).tolist(),
                    reference_interval_delays_s=x.tolist(),
                    candidate_interval_delays_s=y.tolist(),
                    reference_peak_to_peak_s=float(np.ptp(x)) if len(x) > 1 else None,
                    candidate_peak_to_peak_s=float(np.ptp(y)) if len(y) > 1 else None,
                    pairwise_differences_comparison=comparisons[name],
                )
            )
    warning_ok = (
        bd.get("delay_alias_warning") is False and bd.get("path_delay_alias_warning") is False
    )
    count_ok = all(
        type(x) is int and x > 0
        for k in ("valid_phase_samples_by_frequency", "valid_delay_samples_by_interval")
        for x in bd[k]
    )
    phase_mask = np.asarray(bd["phase_valid_mask"], bool) & ~np.asarray(
        ad["phase_valid_mask"], bool
    )
    newly_observed_phase = np.asarray(bd["phase_residual_rad"], float)[phase_mask]
    return dict(
        reference_diagnostics=ad,
        candidate_diagnostics=bd,
        floor_comparison=floor,
        reference_floor_infeasible=bool((da > 0).any()),
        candidate_floor_infeasible=bool((db > 0).any()),
        reference_weighted_floor_penalty=float((da.square().mean((0, 2, 3)) * angle_weights).sum()),
        candidate_weighted_floor_penalty=float((db.square().mean((0, 2, 3)) * angle_weights).sum()),
        observable_endpoint_support_lost=support_lost,
        new_observable_endpoints=support_new,
        new_observable_pair_samples=int(phase_mask.sum()),
        new_observable_pair_phase_max_abs_rad=(
            float(np.max(abs(newly_observed_phase))) if len(newly_observed_phase) else None
        ),
        existing_support_comparisons=comparisons,
        existing_support_metrics=metrics,
        path_comparisons=path,
        unavailable_reference_comparisons=unavailable,
        candidate_warning_gate_passed=warning_ok,
        candidate_nonempty_support_gate_passed=count_ok,
        branch_certified=False,
    )


def decide(
    bands,
    hard_reference,
    hard_candidate,
    advice_reference,
    advice_candidate,
    *,
    context_matches,
    surrogate_reference,
    surrogate_candidate,
):
    """Return a record only. Persistence/identity binding belongs to local_trial.

    No compensation among floors, hard constraints, raw phase/delay, or retained
    advisory terms is inferred from a scalar objective. Mixed changes are
    proposals requiring an explicit priority decision; they do not update state.
    """
    if not all(math.isfinite(float(v)) for v in (surrogate_reference, surrogate_candidate)):
        raise ValueError("Nonfinite surrogate score")
    hard = compare_values(np.maximum(hard_reference, 0), np.maximum(hard_candidate, 0))
    advice = compare_values(advice_reference, advice_candidate)
    result = dict(
        acceptance_revision=ACCEPTANCE_REVISION,
        trial_status="proposed",
        accepted_optimization_progress=False,
        search_candidate_accepted=False,
        progress_kind=None,
        qualified_design=False,
        execution_authorized=False,
        physical_acceptance=False,
        delay_certified=False,
        full_band_delay_certified=False,
        hard_violation_comparison=hard,
        retained_advisory_comparison=advice,
        surrogate_reference=surrogate_reference,
        surrogate_candidate=surrogate_candidate,
        surrogate_improved=surrogate_candidate < surrogate_reference,
        candidate_floor_infeasible=any(b["candidate_floor_infeasible"] for b in bands),
        candidate_hard_infeasible=bool(np.any(np.asarray(hard_candidate) > 0)),
    )
    blockers = []
    tradeoffs = []
    if not context_matches:
        blockers.append("context_mismatch")
    for i, b in enumerate(bands):
        if b["floor_comparison"]["regressed"]:
            blockers.append(f"band_{i}:floor_violation_regression")
        if b["observable_endpoint_support_lost"]:
            blockers.append(f"band_{i}:observable_support_lost")
        if not b["candidate_warning_gate_passed"]:
            blockers.append(f"band_{i}:observable_alias_warning_or_missing")
        if not b["candidate_nonempty_support_gate_passed"]:
            blockers.append(f"band_{i}:insufficient_observable_support")
        if b["unavailable_reference_comparisons"]:
            blockers.append(f"band_{i}:reference_support_comparison_unavailable")
        tradeoffs.extend(
            f"band_{i}:{name}"
            for name, c in b["existing_support_comparisons"].items()
            if c["regressed"]
        )
    if hard["regressed"]:
        blockers.append("hard_violation_regression")
    if advice["regressed"]:
        tradeoffs.append("retained_advisory_regression")
    restoration = hard["improved"] > 0 or any(
        b["floor_comparison"]["improved"] > 0 or b["new_observable_endpoints"] > 0 for b in bands
    )
    phase_progress = any(
        c["improved"] > 0 for b in bands for c in b["existing_support_comparisons"].values()
    )
    advice_progress = advice["improved"] > 0
    result.update(
        blockers=blockers,
        tradeoffs_requiring_decision=tradeoffs,
        visibility_or_hard_restoration=restoration,
        some_sampled_phase_delay_metrics_improved=phase_progress,
        sampled_phase_delay_metrics_improved=bool(
            context_matches
            and phase_progress
            and not any(
                c["regressed"] for b in bands for c in b["existing_support_comparisons"].values()
            )
        ),
        some_retained_advisory_metrics_improved=advice_progress,
        retained_advisory_metrics_improved=bool(
            context_matches and advice_progress and not advice["regressed"]
        ),
    )
    if blockers:
        return dict(result, reason="protected_feasibility_or_operability_gate_failed")
    if tradeoffs:
        return dict(result, reason="unapproved_metric_tradeoff")
    if not (restoration or phase_progress or advice_progress):
        return dict(
            result,
            reason=(
                "surrogate_only_change"
                if surrogate_candidate != surrogate_reference
                else "no_progress"
            ),
        )
    kind = (
        "feasibility_restoration"
        if restoration
        else "sampled_phase_delay_progress" if phase_progress else "retained_advisory_progress"
    )
    return dict(
        result,
        trial_status="accepted_optimization_progress",
        accepted_optimization_progress=True,
        search_candidate_accepted=True,
        progress_kind=kind,
        reason=kind,
    )
