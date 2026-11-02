"""Native conservative MMA proposals, independently evaluated before state changes."""

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np

from yapnr.rf.optim.mma import MMAState


class UpdateLoop:
    def __init__(
        self, evaluator, x, *, move=0.12, max_inner=2, geometry_check=None, evidence_persist=None
    ):
        if evaluator.objective.bands != ("ch5", "ch9"):
            raise ValueError(
                "Partial band blocks are source/runtime diagnostics, not the full optimizer"
            )
        from .observable_variation import REVISION

        if evaluator.objective.policy.objective_revision != REVISION or not callable(
            evidence_persist
        ):
            raise ValueError("Reviewed objective and durable trial/decision persistence required")
        self.persist = evidence_persist
        self.ev = evaluator
        self.x = np.asarray(x, float).copy()
        self.state = MMAState()
        self.move = move
        self.max_inner = max_inner
        self.geometry_check = geometry_check
        self.history = []

    def step(self):
        raise RuntimeError(
            "Optimizer activation blocked: inactive integration only; no state update authorized"
        )

    def evaluate_binary(self):
        """Separate explicit forward validation; never inherit gray-field results."""
        if self.geometry_check is None:
            raise RuntimeError("Binary export requires geometry validator")
        checks = [
            self.geometry_check(self.ev.chain.density(self.x, math.inf, e), e) for e in self.ev.etas
        ]
        if not all(r.get("passed") is True for r in checks):
            return dict(
                binary_geometry=checks,
                hard_screen_pass=False,
                new_forward_evaluation=False,
                RF_qualified=False,
            )
        previous = self.ev.beta
        try:
            self.ev.beta = math.inf
            result = self.ev.evaluate(self.x, False)
        finally:
            self.ev.beta = previous
        return dict(
            binary_geometry=checks,
            hard_screen_pass=bool(np.all(result.g <= 0)),
            objective=result.f.tolist(),
            hard=result.g.tolist(),
            diagnostics=result.diagnostics,
            density_hashes=result.density_hashes,
            new_forward_evaluation=True,
            independent_mesh_validation_pending=True,
            RF_qualified=False,
        )

    def save(self, directory):
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        np.save(d / "variables.npy", self.x, allow_pickle=False)
        for eta in self.ev.etas:
            np.save(
                d / f"density-eta-{eta}.npy",
                self.ev.chain.density(self.x, self.ev.beta, eta),
                allow_pickle=False,
            )
            np.save(
                d / f"binary-eta-{eta}.npy",
                self.ev.chain.density(self.x, math.inf, eta),
                allow_pickle=False,
            )
        state = {
            k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in vars(self.state).items()
        }
        metadata = dict(
            schema="joint-adjoint-update/1",
            policy=vars(self.ev.objective.policy),
            policy_sha256=self.ev.objective.policy.digest(),
            map_sha256=self.ev.chain.fingerprint(),
            field_contract=asdict(self.ev.contract),
            move=self.move,
            max_inner=self.max_inner,
            beta=self.ev.beta,
            etas=self.ev.etas,
            history=self.history,
            mma_state=state,
            RF_qualified=False,
            automatic_execution=False,
        )
        (d / "checkpoint.json").write_text(json.dumps(metadata, indent=2) + "\n")
        hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in d.iterdir()
            if p.is_file() and p.name != "hashes.json"
        }
        (d / "hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
        return metadata

    @classmethod
    def restore(cls, evaluator, directory, *, geometry_check=None, evidence_persist=None):
        d = Path(directory)
        hashes = json.loads((d / "hashes.json").read_text())
        for name, digest in hashes.items():
            if (
                Path(name).name != name
                or (d / name).is_symlink()
                or hashlib.sha256((d / name).read_bytes()).hexdigest() != digest
            ):
                raise ValueError("Checkpoint bytes differ")
        m = json.loads((d / "checkpoint.json").read_text())
        if (
            m["policy_sha256"] != evaluator.objective.policy.digest()
            or m["map_sha256"] != evaluator.chain.fingerprint()
        ):
            raise ValueError("Checkpoint objective or parameterization differs")
        if m["beta"] != evaluator.beta or tuple(m["etas"]) != evaluator.etas:
            raise ValueError("Checkpoint projection differs")

        def canonical(x):
            return json.dumps(x, sort_keys=True)

        if canonical(m["field_contract"]) != canonical(asdict(evaluator.contract)):
            raise ValueError("Checkpoint source/model/reference contract differs")
        obj = cls(
            evaluator,
            np.load(d / "variables.npy", allow_pickle=False),
            move=m["move"],
            max_inner=m["max_inner"],
            geometry_check=geometry_check,
            evidence_persist=evidence_persist,
        )
        state = m["mma_state"]
        for k, v in state.items():
            if isinstance(v, list):
                state[k] = np.asarray(v, dtype=float)
        obj.state = MMAState(**state)
        obj.history = m["history"]
        return obj
