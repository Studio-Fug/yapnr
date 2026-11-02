"""Wire an already verified actual_array_derivative_r2 model into this adapter.

No construction of a large Problem, solver run, or remote admission on import.
The operator supplies its pinned, frozen model and its guarded job provider.
"""

import numpy as np

from .evaluator import JointEvaluator
from .frequencies import require_problem, verify_pinned_generation
from .geometry import BinaryGeometry
from .mapping import Chain
from .native import NativeResponse, NativeRunner
from .objective import JointObjective, Policy
from .optimizer import UpdateLoop


def build(
    prepared_model,
    execute,
    admit,
    limits,
    *,
    policy=None,
    beta=16.0,
    etas=(0.5, 0.4, 0.6),
    geometry_check=None
):
    p = prepared_model.p
    frequency_evidence = verify_pinned_generation(p.spec)
    require_problem(p, contract=prepared_model.contract, policy=policy or Policy())
    if p._decimation() != 1 or p.dt != prepared_model.contract.time_step_s:
        raise ValueError("Requires frozen exact-DTFT model")
    if set(p.ports) != {1, 2, 3, 4}:
        raise ValueError("All four antenna ports required")
    i0, i1, j0, j1 = p.domain.window
    for axis, lo, hi in ((p.grid.x, i0, i1), (p.grid.y, j0, j1)):
        if not np.allclose(axis.primary[lo:hi], 0.0001, rtol=0, atol=1e-15):
            raise ValueError("Nonuniform optimizer parameterization is not qualified")
    objective = JointObjective(policy or Policy())
    chain = Chain(prepared_model.rho, p.material, p.param.filter, ratio=2)
    runner = NativeRunner(p, execute, admit, limits)
    response = NativeResponse(p, objective.sampling)
    watched = [q.name for q in p.port_probes + p.box_probes]
    evaluator = JointEvaluator(
        objective, chain, runner, response, prepared_model.contract, watched, beta, etas
    )
    loop = UpdateLoop(
        evaluator, chain.x0, geometry_check=geometry_check or BinaryGeometry(p, prepared_model.rho)
    )
    # This report makes initialization changes explicit, before any fields exist.
    return loop, dict(
        initialization=chain.initialization(beta, etas),
        frequency_contract=frequency_evidence,
        policy_sha256=objective.policy.digest(),
        map_sha256=chain.fingerprint(),
        actual_array_gradient_qualified=False,
        execution_started=False,
        signal_reference="antenna_internal",
        missing_upstream_models_do_not_enter_objective=True,
    )
