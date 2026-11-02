"""Audit a candidate rule without activating optimizer state changes."""

from .local_trial import review_local_trial
from .operational_evidence import require_persist


def review_trial_for_diagnostics(
    objective, reference, candidate, reference_identity, candidate_identity, *, persist
):
    def wrapped(event, payload):
        return persist(
            "reviewed_rule_" + event,
            dict(
                scope="REVIEWED_ACCEPTANCE_ADAPTER_INACTIVE_NO_STATE_UPDATE",
                candidate_rule_evidence=payload,
            ),
        )

    proposal = review_local_trial(
        objective, reference, candidate, reference_identity, candidate_identity, persist=wrapped
    )
    decision = dict(
        schema="inactive-trial-diagnostic/1",
        candidate_rule_would_accept=proposal["search_candidate_accepted"],
        candidate_rule_decision=proposal,
        search_candidate_accepted=False,
        acceptance_adapter_reviewed=True,
        optimizer_activation_authorized=False,
        state_update_allowed=False,
        execution_authorized=False,
        physical_acceptance=False,
        delay_certified=False,
        reason="inactive_integration_no_optimizer_activation_authorized",
    )
    require_persist(persist, "inactive_trial_decision", decision)
    return decision
