"""Opt-in preparation adapter for existing native adjoint sources; no FDTD calls.

Uses installed yapnr's Wirtinger/source APIs without modifying the engine.
Every coupled objective is scalar; recombination must sum frequency rows AND
excitations. This is not yet an optimizer/Problem.evaluate integration.
"""

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import torch

from yapnr.rf.adjoint import adjoint_sources, wirtinger


@dataclass(frozen=True)
class Contract:
    design_sha256: str
    model_sha256: str
    frequencies_hz: tuple[float, ...]
    common_origin_m: tuple[float, float, float]
    reference_planes_sha256: str
    source_normalization_sha256: str
    network_sha256: str
    morphology: str
    time_step_s: float

    def validate(self):
        for key in [
            "design_sha256",
            "model_sha256",
            "reference_planes_sha256",
            "source_normalization_sha256",
            "network_sha256",
        ]:
            value = getattr(self, key)
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Missing immutable " + key)
        f = np.asarray(self.frequencies_hz)
        if (
            f.ndim != 1
            or len(f) < 1
            or not np.isfinite(f).all()
            or (f <= 0).any()
            or (np.diff(f) <= 0).any()
        ):
            raise ValueError("Strict ordered positive frequency contract required")
        if (
            len(self.common_origin_m) != 3
            or not np.isfinite(self.common_origin_m).all()
            or not self.morphology
        ):
            raise ValueError("Origin and morphology identity required")
        if not np.isfinite(self.time_step_s) or self.time_step_s <= 0:
            raise ValueError("Physical timestep required")


@dataclass(frozen=True)
class Forward:
    excitation: int
    dft: Mapping[str, np.ndarray]
    contract: Contract
    converged: bool


@dataclass(frozen=True)
class JointResult:
    value: float
    cotangents: Mapping[int, Mapping[str, np.ndarray]]
    contract: Contract
    semantics: str = "scalar_coupled_across_excitations_and_frequencies"


def joint_wirtinger(objective, forwards, watched):
    """One scalar objective over independent leaves keyed (excitation,probe).

    Native conversion g=conj(torch_grad)/2 is reused unchanged: dL=2Re sum g*dq.
    Caller must substantiate contracts with real provenance outside this adapter.
    """
    if not forwards or len({f.excitation for f in forwards}) != len(forwards):
        raise ValueError("Unique nonempty excitation set required")
    reference = forwards[0].contract
    reference.validate()
    flat = {}
    keys = []
    expected = set(watched)
    if not expected or len(expected) != len(watched):
        raise ValueError("Unique watched probe names required")
    for f in forwards:
        if not f.converged or f.contract != reference:
            raise ValueError("Unconverged or mixed geometry/origin/frequency/variant/network")
        if f.excitation <= 0 or not expected.issubset(f.dft):
            raise ValueError("Excitation or probe contract mismatch")
        for name in watched:
            a = np.asarray(f.dft[name], dtype=np.complex128)
            if (
                a.ndim != 2
                or a.shape[0] != len(reference.frequencies_hz)
                or not np.isfinite(a).all()
            ):
                raise ValueError("Invalid probe array")
            key = (f.excitation, name)
            flat[key] = a
            keys.append(key)

    def wrapped(q):
        nested = {
            f.excitation: {name: q[(f.excitation, name)] for name in watched} for f in forwards
        }
        value = objective(nested)
        if (
            not isinstance(value, torch.Tensor)
            or value.ndim != 0
            or value.is_complex()
            or not torch.isfinite(value)
        ):
            raise ValueError(
                "Joint objective must return one finite real scalar; no per-frequency shortcut"
            )
        return value

    value, grads = wirtinger(wrapped, flat, keys)
    nested = {
        f.excitation: {name: grads[(f.excitation, name)] for name in watched} for f in forwards
    }
    return JointResult(float(value), nested, reference)


def sources_for_excitation(sim, probes, result, excitation):
    """Build native Nuttall-fit spectra only; never starts a field solve."""
    if excitation not in result.cotangents:
        raise ValueError("Missing excitation cotangents")
    if sim.dt != result.contract.time_step_s:
        raise ValueError("Native source timestep differs from forward contract")
    names = [p.name for p in probes]
    if len(names) != len(set(names)) or not set(result.cotangents[excitation]).issubset(names):
        raise ValueError("Missing or ambiguous native probes")
    omega = 2 * np.pi * np.asarray(result.contract.frequencies_hz)
    return adjoint_sources(sim, probes, result.cotangents[excitation], omega)


def sum_scalar_recombinations(result, gradients_by_excitation):
    """Consume design_gradient(gr_j,rho) for EACH j; sum all frequency rows.

    Actual calls to gradient/sim.run and the density/filter VJP remain integration
    work. Keeping this separate permits contract tests with zero FDTD steps.
    """
    if set(gradients_by_excitation) != set(result.cotangents):
        raise ValueError("Missing/extra adjoint contribution")
    shape = None
    total = None
    for excitation in sorted(gradients_by_excitation):
        g = np.asarray(gradients_by_excitation[excitation])
        if (
            g.ndim != 3
            or g.shape[0] != len(result.contract.frequencies_hz)
            or np.iscomplexobj(g)
            or not np.isfinite(g).all()
        ):
            raise ValueError("Expected real per-frequency pixel recombination")
        if shape is None:
            shape = g.shape
        if g.shape != shape:
            raise ValueError("Mixed design grids")
        part = g.sum(axis=0)
        total = part.copy() if total is None else total + part
    return total
