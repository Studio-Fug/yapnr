"""Genuine channel-9 block of the complete objective for a runtime/source pilot.

Only P3/P4 excitation records are needed. This never supplies missing P1/P2
fields, never runs a solve, and cannot construct a partial-band optimizer.
"""

from .bridge import Forward, joint_wirtinger, sources_for_excitation
from .evaluator import array_sha
from .frequencies import descriptor, require_problem
from .native import NativeResponse
from .objective import JointObjective, Policy
from .validity import PHASE_GATE_SCHEMA, require_phase_operability


def prepare(problem, records, rho, *, policy=None, excitation=3):
    if set(records) != {3, 4} or excitation not in (3, 4):
        raise ValueError("Exactly P3/P4 records required")
    contract = records[3].contract
    for r in records.values():
        r.validate(contract)
    if array_sha(rho) != contract.design_sha256:
        raise ValueError("Source-pilot density does not match saved forwards")
    objective = JointObjective(policy or Policy(), bands=("ch9",))
    require_problem(problem, contract=contract, policy=objective.policy)
    problem.set_design(rho)
    response = NativeResponse(problem, objective.sampling, (3, 4))
    watched = [q.name for q in problem.port_probes + problem.box_probes]

    def fn(q):
        rows = objective.rows(response(q))
        require_phase_operability(rows.diagnostics, objective.bands)
        return rows.objective[0]

    joint = joint_wirtinger(
        fn,
        [Forward(j, r.fields, r.contract, r.converged) for j, r in sorted(records.items())],
        watched,
    )
    sources = sources_for_excitation(
        problem.sim, problem.port_probes + problem.box_probes, joint, excitation
    )
    return (
        joint,
        sources,
        dict(
            band="ch9",
            row="ch9:phase_coverage_and_advisory_goals",
            actual_excitation_records=[3, 4],
            source_excitation=excitation,
            objective_policy_sha256=objective.policy.digest(),
            frequency_contract=descriptor(),
            phase_gate_schema=PHASE_GATE_SCHEMA,
            purpose="runtime_and_source_diagnostic_only",
            new_FDTD_runs=0,
            gradient_qualified=False,
            full_optimizer_scope_unchanged=True,
        ),
    )
