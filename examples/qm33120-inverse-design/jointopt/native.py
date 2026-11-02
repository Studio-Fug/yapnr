"""Native response and bounded remote-provider interface. No provisioning code.

The provider owns timeouts, disk/resource guards, receipts, and saved-field replay.
execute(request, rho, forward=None, cotangents=None) returns an Envelope. It must
dispatch the existing native Problem.forward / adjoint.gradient path, not FD.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np
import torch

from yapnr.rf.farfield import intensity_factor

from .evaluator import FieldRecord, array_sha
from .frequencies import descriptor, require_problem
from .objective import Response


class NativeResponse:
    def __init__(self, problem, sampling, active_excitations=(1, 2, 3, 4)):
        self.problem, self.sampling = problem, sampling
        self.active_excitations = tuple(active_excitations)
        require_problem(problem, policy=sampling.policy)

    def __call__(self, q):
        p, sm = self.problem, self.sampling
        e, s, eta, accepted = [], [], [], []
        if set(q) != set(self.active_excitations):
            raise ValueError("Actual excitation set differs; no fabricated missing fields")
        for j in self.active_excitations:
            a, _ = p.port_waves(j, q[j], p.omega, excitation=j)
            f = p.far.vectors(q[j], p.omega, sm.directions)
            ev = torch.einsum("fdc,dqc->fdq", f, sm.bases)
            ev = (
                ev
                * torch.tensor(np.sqrt(8 * np.pi * intensity_factor(p.omega)))[:, None, None]
                / a[:, None, None]
            )
            e.append(ev.reshape(len(p.freqs), 5, len(sm.n), 2))
            quantities = p.quantities(q[j], j, far=False)
            s.append(torch.stack([quantities["s"][(i, j)] for i in (1, 2, 3, 4)], -1))
            eta.append(quantities["eta"][j])
            inc = p.wave_power(j, a, p.omega)
            # Includes residual incident waves at idle ports. Do not approximate
            # accepted power by 1-|Sjj|² or mislabel coupled power as material loss.
            net = sum(
                p.wave_power(i, ai, p.omega) - p.wave_power(i, bi, p.omega)
                for i, (ai, bi) in quantities["waves"].items()
            )
            accepted.append(net / inc)
        return Response(
            torch.stack(s, -1),
            torch.stack(e, -1),
            torch.stack(eta, -1),
            torch.stack(accepted, -1),
            self.active_excitations,
        )


@dataclass(frozen=True)
class Limits:
    max_native_jobs: int
    solver_wall_s: int
    max_steps: int = 250000
    decimation: int = 1
    tolerance: float = 1e-10
    free_disk_GiB: float = 4.0
    runtime_profile: str = "standard"

    def validate(self):
        if self.runtime_profile not in ("standard", "ch9_forward_runtime_pilot"):
            raise ValueError("Unknown runtime policy profile")
        ceiling = 13200 if self.runtime_profile == "ch9_forward_runtime_pilot" else 7200
        if self.runtime_profile == "ch9_forward_runtime_pilot" and self.max_native_jobs != 1:
            raise ValueError("Pilot profile is one independently admitted forward")
        if not 0 < self.max_native_jobs or not 0 < self.solver_wall_s <= ceiling:
            raise ValueError(
                "Explicit bounded job count/wall required; operator stricter limits still apply"
            )
        if (
            self.decimation != 1
            or self.max_steps != 250000
            or self.tolerance not in (1e-10, 1e-12)
            or self.free_disk_GiB < 4
        ):
            raise ValueError("Frozen derivative numerical/guard settings differ")


@dataclass
class Envelope:
    request_sha256: str
    exit_code: int
    guard_reason: object
    payload: object
    receipt_sha256: str


class NativeRunner:
    evidence_kind = "native_FDTD_unqualified_actual_array_adjoint"

    def __init__(self, problem, execute, admit, limits):
        limits.validate()
        self.p, self.execute, self.admit, self.limits = problem, execute, admit, limits
        self.job_count = 0
        self.receipts = []

    def _request(self, operation, rho, port, contract, role, cot=None):
        if self.job_count >= self.limits.max_native_jobs:
            raise RuntimeError("Native job budget exhausted")
        contract.validate()
        if self.limits.runtime_profile == "ch9_forward_runtime_pilot" and (
            operation != "forward" or port not in (3, 4) or role != "gradient" or cot is not None
        ):
            raise ValueError("Long profile is restricted to pilot baseline P3/P4 forwards")
        if contract.design_sha256 != array_sha(rho):
            raise ValueError("Density contract mismatch")
        require_problem(self.p, contract=contract)
        request = dict(
            schema="jointopt-native-request/2",
            operation=operation,
            port=port,
            role=role,
            contract=asdict(contract),
            limits=asdict(self.limits),
            frequency_contract=descriptor(),
            cotangent_sha256=None if cot is None else {k: array_sha(v) for k, v in cot.items()},
            ground_convergence_monitors_required=True,
            physical_edge_corrections_required=True,
        )
        request["request_sha256"] = hashlib.sha256(
            json.dumps(request, sort_keys=True).encode()
        ).hexdigest()
        # An operator's verified receipt/admission service must substantiate these
        # inputs. Constructing a request never grants execution permission.
        if self.admit(request) is not True:
            raise RuntimeError("Provider admission not granted")
        self.job_count += 1
        return request

    def _checked(self, request, envelope):
        if (
            envelope.request_sha256 != request["request_sha256"]
            or envelope.exit_code != 0
            or envelope.guard_reason is not None
        ):
            raise RuntimeError("Remote job failed, mixed, or stopped by a guard; preserve receipt")
        if len(envelope.receipt_sha256) != 64:
            raise RuntimeError("Missing immutable terminal receipt")
        self.receipts.append(
            dict(request=request["request_sha256"], receipt=envelope.receipt_sha256)
        )
        return envelope.payload

    def forward(self, rho, port, contract, role):
        req = self._request("forward", rho, port, contract, role)
        f = self._checked(req, self.execute(req, rho))
        rec = FieldRecord(
            port,
            f.dft,
            contract,
            bool(f.converged),
            tuple(f.history),
            f.decimation,
            self.limits.tolerance,
            native=f,
            role=role,
        )
        rec.validate(contract)
        if role == "gradient":
            required = [q.name for q in self.p.design_probes + self.p.edge_probes]
            if not set(required).issubset(f.dft):
                raise RuntimeError("Missing material/edge fields")
        return rec

    def pullback(self, rho, record, cotangents, contract, quantity):
        record.validate(contract)
        req = self._request("adjoint", rho, record.port, contract, "gradient", cotangents)
        gr = self._checked(
            req, self.execute(req, rho, forward=record.native, cotangents=cotangents)
        )
        a = gr.adjoint
        FieldRecord(
            record.port,
            a.dft,
            contract,
            bool(a.converged),
            tuple(a.history),
            a.decimation,
            self.limits.tolerance,
        ).validate(contract)
        self.p.set_design(rho)
        # Preserve native conductivity/reactive material AND edge derivatives.
        return self.p.design_gradient(gr, rho).sum(axis=0)


def provider_native_kernel(problem, request, rho, *, forward=None, cotangents=None):
    """Operator-worker entry point AFTER external admission/time/resource guards.

    No thread, process, network, or paid worker is started here. Caller must wrap
    this function in the admitted bounded worker and supply the receipt envelope.
    """
    from yapnr.rf.adjoint import gradient
    from yapnr.rf.fdtd.stop import StopRule

    limits = request["limits"]
    if (
        request.get("schema") != "jointopt-native-request/2"
        or request.get("frequency_contract") != descriptor()
    ):
        raise ValueError("Worker requires versioned exact frequency provenance")
    from types import SimpleNamespace

    require_problem(problem, contract=SimpleNamespace(**request["contract"]))
    if request["contract"]["design_sha256"] != array_sha(rho):
        raise ValueError("Worker density differs")
    if problem.dt != request["contract"]["time_step_s"] or tuple(problem.freqs) != tuple(
        request["contract"]["frequencies_hz"]
    ):
        raise ValueError("Worker physical time/frequency contract differs")
    if problem._decimation() != 1 or limits["decimation"] != 1:
        raise ValueError("Exact recording required")
    if problem.spec.solver.max_steps != limits["max_steps"]:
        raise ValueError("Worker step bound differs")
    problem.set_design(rho)
    problem.tol = limits["tolerance"]
    if request["operation"] == "forward":
        # All probes retained here. The operator's separately qualified role-aware
        # recording optimization can replace this worker without changing physics.
        return problem.forward(request["port"], design=True)
    if request["operation"] != "adjoint" or forward is None or cotangents is None:
        raise ValueError("Incomplete adjoint request")
    stop = StopRule(
        tol=limits["tolerance"], f_lo=float(problem.freqs.min()), max_steps=limits["max_steps"]
    )
    return gradient(
        problem.sim,
        forward,
        problem.port_probes + problem.box_probes,
        cotangents,
        problem.design_probes,
        stop,
        decimation=1,
        edge_probes=problem.edge_probes,
    )
