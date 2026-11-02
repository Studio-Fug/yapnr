"""Joint objective -> native cotangents -> field adjoint -> material -> design VJP.

runner is injected by the bounded operator. There is no cloud scheduler or
unbounded solver entry point in this module. Every runner record must carry the
exact design/source/model contract and convergence evidence.
"""

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, replace

import numpy as np
import torch

from .bridge import Forward, joint_wirtinger
from .frequencies import require_canonical
from .validity import PHASE_GATE_SCHEMA, require_phase_operability


def array_sha(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


@dataclass
class FieldRecord:
    port: int
    fields: dict
    contract: object
    converged: bool
    history: tuple
    decimation: int
    tolerance: float
    native: object = None
    role: str = "gradient"

    def validate(self, expected):
        if self.contract != expected or not self.converged or self.decimation != 1:
            raise RuntimeError("Mixed or unconverged exact-DTFT field record")
        if self.tolerance not in (1e-10, 1e-12):
            raise RuntimeError("Derivative tolerance changed")
        if len(self.history) < 2 or any(
            not np.isfinite(v) or v > self.tolerance for _, v in self.history[-2:]
        ):
            raise RuntimeError("Two terminal convergence checks required")


@dataclass
class Evaluation:
    f: np.ndarray
    g: np.ndarray
    df: object
    dg: object
    keys_f: tuple
    keys_g: tuple
    diagnostics: list
    density_hashes: tuple
    backend_evidence: str
    trial_responses: tuple = ()
    trial_identities: tuple = ()


class JointEvaluator:
    def __init__(
        self,
        objective,
        chain,
        runner,
        response_builder,
        contract,
        watched,
        beta=16.0,
        etas=(0.5, 0.4, 0.6),
        *,
        evidence_persist,
    ):
        if len(set(etas)) != len(etas) or 0.5 not in etas:
            raise ValueError("Nominal and unique morphology variants required")
        contract.validate()
        require_canonical(contract.frequencies_hz, "evaluator field contract")
        require_canonical(objective.policy.frequencies_hz, "evaluator objective")
        self.objective, self.chain, self.runner, self.response_builder = (
            objective,
            chain,
            runner,
            response_builder,
        )
        self.contract, self.watched = contract, tuple(watched)
        self.beta, self.etas = float(beta), tuple(etas)
        if not callable(evidence_persist):
            raise ValueError("Durable pre-gate evidence callback required")
        self.evidence_persist = evidence_persist
        self.cache = OrderedDict()
        self.evidence_kind = runner.evidence_kind

    def _contract(self, rho, eta):
        return replace(
            self.contract,
            design_sha256=array_sha(rho),
            morphology=(
                f"jointopt-r1:beta={self.beta}:eta={eta}:map={self.chain.fingerprint()}:"
                f"objective={self.objective.policy.digest()}"
            ),
        )

    def evaluate(self, x, gradients=True):
        fs, gs, dfs, dgs, kf, kg, diags, hashes = [], [], [], [], [], [], [], []
        responses, identities = [], []
        for eta in self.etas:
            rho = self.chain.density(x, self.beta, eta)
            contract = self._contract(rho, eta)
            contract.validate()
            key = (contract.design_sha256, contract.morphology)
            records = self.cache.get(key)
            # Value-only records may omit design/edge data. Never reuse them as
            # gradient records without an explicit provider-side promotion.
            if records is None or (
                gradients and any(r.role != "gradient" for r in records.values())
            ):
                records = {
                    j: self.runner.forward(
                        rho, j, contract, role="gradient" if gradients else "values"
                    )
                    for j in self.objective.active_ports
                }
                for r in records.values():
                    r.validate(contract)
                self.cache[key] = records
                # Keep at most one four-port geometry in RAM. A remote provider
                # may reuse older checksummed results on disk without holding
                # every line-search and morphology field in this process.
                while len(self.cache) > 1:
                    self.cache.popitem(last=False)
            nested = {
                j: {
                    name: torch.tensor(r.fields[name], dtype=torch.complex128)
                    for name in self.watched
                }
                for j, r in records.items()
            }
            from .objective import Response
            from .operational_evidence import normalization_receipt, require_persist

            norm = normalization_receipt(self.response_builder, nested, contract)
            require_persist(self.evidence_persist, "normalization", dict(eta=eta, receipt=norm))
            response = self.response_builder(nested)
            rows = self.objective.rows(response)
            require_persist(
                self.evidence_persist,
                "objective_before_gate",
                dict(
                    eta=eta,
                    design_sha256=contract.design_sha256,
                    policy_sha256=self.objective.policy.digest(),
                    diagnostics=rows.diagnostics,
                ),
            )
            responses.append(
                Response(
                    *(
                        getattr(response, k).detach().clone()
                        for k in ("s", "e", "total_efficiency", "accepted_fraction")
                    ),
                    response.excitation_ids,
                )
            )
            identities.append(norm["identity"])
            require_phase_operability(rows.diagnostics, self.objective.bands)
            f, g = rows.objective.detach().numpy(), rows.hard.detach().numpy()
            fs.extend(f)
            gs.extend(g)
            kf.extend((eta, k) for k in rows.objective_keys)
            kg.extend((eta, k) for k in rows.hard_keys)
            diags.append(
                dict(
                    eta=eta,
                    phase_gate_schema=PHASE_GATE_SCHEMA,
                    **rows.diagnostics,
                    raw_hard={
                        k: {n: v.detach().numpy().tolist() for n, v in terms.items()}
                        for k, terms in rows.raw_hard.items()
                    },
                )
            )
            hashes.append(contract.design_sha256)
            if gradients:
                ff = [Forward(j, r.fields, r.contract, r.converged) for j, r in records.items()]
                for kind, nrows, out in [("objective", len(f), dfs), ("hard", len(g), dgs)]:
                    for i in range(nrows):

                        def scalar(q, kind=kind, i=i):
                            scalar_rows = self.objective.rows(self.response_builder(q))
                            require_phase_operability(scalar_rows.diagnostics, self.objective.bands)
                            return getattr(scalar_rows, kind)[i]

                        joint = joint_wirtinger(scalar, ff, self.watched)
                        expected = (f if kind == "objective" else g)[i]
                        np.testing.assert_allclose(joint.value, expected, rtol=1e-12, atol=1e-12)
                        density = np.zeros_like(rho)
                        for j in records:
                            cot = joint.cotangents[j]
                            if any(np.any(v) for v in cot.values()):
                                if j not in self.objective.dependency_ports(kind, i):
                                    raise RuntimeError(
                                        "Undeclared cross-excitation dependency changes job budget"
                                    )
                                result = self.runner.pullback(
                                    rho, records[j], cot, contract, quantity=(kind, i)
                                )
                                result = np.asarray(result, float)
                                if result.shape != rho.shape or not np.isfinite(result).all():
                                    raise RuntimeError(
                                        "Wrong-shape or nonfinite native density derivative"
                                    )
                                density += result
                        if not np.isfinite(density).all():
                            raise RuntimeError("Nonfinite field-adjoint gradient")
                        out.append(self.chain.vjp(x, self.beta, density, eta))
        return Evaluation(
            np.asarray(fs),
            np.asarray(gs),
            np.asarray(dfs) if gradients else None,
            np.asarray(dgs) if gradients else None,
            tuple(kf),
            tuple(kg),
            diags,
            tuple(hashes),
            self.evidence_kind,
            tuple(responses),
            tuple(identities),
        )

    def work_plan(self, max_forward_trials=2):
        v = len(self.etas)
        n = len(self.objective.active_ports)
        return dict(
            variants=v,
            active_ports=self.objective.active_ports,
            forward_per_gradient=n * v,
            adjoint_per_gradient=2 * n * v,
            worst_native_jobs_one_step=(3 * n + n * max_forward_trials) * v,
            native_jobs_per_trial=n * v,
            cache_reuse_not_assumed=True,
            physical_RF_acceptance=False,
            execution_admitted=False,
        )
