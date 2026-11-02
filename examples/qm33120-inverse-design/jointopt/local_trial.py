"""Inactive local acceptance /3. Reviews saved Responses; never updates a design."""

import hashlib

import numpy as np
import torch

from .acceptance import ACCEPTANCE_REVISION, band_evidence, decide
from .observable_variation import REVISION, context_digest
from .response import target_phase


def review_local_trial(
    objective, reference, candidate, reference_identity, candidate_identity, *, persist
):
    """Only entry point: compute -> persist evidence -> decide -> persist -> return.

    Source normalization and physical-context identities come from the loader.
    Context excludes the intentionally varied design, whose response hashes are
    recorded independently. This reviews acceptance, without dispatch or state.
    """
    p, sm = objective.policy, objective.sampling
    if p.objective_revision != REVISION:
        raise ValueError("Wrong candidate objective")
    a = objective.rows(reference)
    b = objective.rows(candidate)

    def digest(x):
        return hashlib.sha256(np.ascontiguousarray(x.detach().numpy()).tobytes()).hexdigest()

    bands = []
    contexts = []
    for band in objective.bands:
        ids = [0, 1, 2] if band == "ch5" else [3, 4, 5]
        ports = (1, 2) if band == "ch5" else (3, 4)
        f = torch.tensor(p.frequencies_hz, dtype=torch.float64)[ids]

        def hc(response):
            e = response.e[ids][..., [response.excitation_ids.index(q) for q in ports]]
            return torch.einsum("fgacp,lc->fgalp", e, sm.jones.conj())[:, 0]

        def ctx(identity):
            try:
                inner = context_digest(
                    p,
                    f.numpy(),
                    sm.directions,
                    sm.jones.numpy(),
                    identity["normalization_sha256"],
                    identity["physical_context_sha256"],
                )
                return hashlib.sha256((ACCEPTANCE_REVISION + ":" + inner).encode()).hexdigest()
            except (ValueError, KeyError, TypeError):
                # Keep evaluated evidence even when provenance validation fails.
                return None

        contexts.append(
            dict(band=band, reference=ctx(reference_identity), candidate=ctx(candidate_identity))
        )
        target = target_phase(
            f,
            torch.tensor(sm.n),
            torch.tensor(p.baselines_m[band == "ch9"], dtype=torch.float64).repeat(3, 1),
            torch.tensor(p.offset_rad, dtype=torch.float64)[ids],
            sign=p.sign,
        )[:, :, None].expand(3, len(sm.n), 4)
        bands.append(band_evidence(hc(reference), hc(candidate), target, f, sm.w, p, ports))

    def raw(rows):
        return np.concatenate(
            [t.detach().numpy().ravel() for v in rows.raw_hard.values() for t in v.values()]
        )

    raw_a, raw_b = raw(a), raw(b)
    # Preserve the existing conservative smooth engineering screens as well
    # as every raw constituent. No satisfied original screen may be lost.
    hard_a = np.concatenate((raw_a, a.hard.detach().numpy()))
    hard_b = np.concatenate((raw_b, b.hard.detach().numpy()))
    advice_keys = ("slope", "gain", "shape", "isolation_advice")

    def advice(rows):
        return np.asarray(
            [
                rows.diagnostics["bands"][band]["terms"][k]
                for band in objective.bands
                for k in advice_keys
            ],
            float,
        )

    advice_a, advice_b = advice(a), advice(b)
    evidence = dict(
        schema="local-response-trial/3",
        objective_revision=REVISION,
        acceptance_revision=ACCEPTANCE_REVISION,
        trial_status="proposed",
        reference_identity=reference_identity,
        candidate_identity=candidate_identity,
        contexts=contexts,
        reference_excitation_ids=reference.excitation_ids,
        candidate_excitation_ids=candidate.excitation_ids,
        reference_response_hashes={
            k: digest(getattr(reference, k))
            for k in ("s", "e", "total_efficiency", "accepted_fraction")
        },
        candidate_response_hashes={
            k: digest(getattr(candidate, k))
            for k in ("s", "e", "total_efficiency", "accepted_fraction")
        },
        reference_objective=a.objective.detach().tolist(),
        candidate_objective=b.objective.detach().tolist(),
        reference_raw_hard=raw_a.tolist(),
        candidate_raw_hard=raw_b.tolist(),
        reference_smooth_hard=a.hard.detach().tolist(),
        candidate_smooth_hard=b.hard.detach().tolist(),
        reference_protected_hard_vector=hard_a.tolist(),
        candidate_protected_hard_vector=hard_b.tolist(),
        retained_advisory_keys=[(band, k) for band in objective.bands for k in advice_keys],
        reference_retained_advice=advice_a.tolist(),
        candidate_retained_advice=advice_b.tolist(),
        reference_diagnostics=a.diagnostics,
        candidate_diagnostics=b.diagnostics,
        band_comparison_evidence=bands,
        qualified_design=False,
        execution_authorized=False,
        physical_acceptance=False,
        delay_certified=False,
    )
    if persist("evaluated_before_gate", evidence) is not True:
        raise RuntimeError("Pre-gate evidence not durably saved")
    decision = decide(
        bands,
        hard_a,
        hard_b,
        advice_a,
        advice_b,
        context_matches=all(
            c["reference"] is not None and c["reference"] == c["candidate"] for c in contexts
        ),
        surrogate_reference=float(a.objective.max()),
        surrogate_candidate=float(b.objective.max()),
    )
    if persist("decision", decision) is not True:
        raise RuntimeError("Decision not durably saved; no accepted progress may be used")
    return decision
