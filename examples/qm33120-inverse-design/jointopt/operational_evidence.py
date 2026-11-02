"""Local-only evidence boundary. No cloud transport or execution authority."""

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def exact_array(value):
    if isinstance(value, torch.Tensor):
        value = value.detach().cpu().numpy()
    a = np.ascontiguousarray(value)
    if a.dtype not in (np.float64, np.complex128) or not np.isfinite(a).all():
        raise ValueError("Finite binary64 normalization values required")
    v = a.ravel()
    return dict(
        dtype=a.dtype.str,
        shape=list(a.shape),
        sha256=hashlib.sha256(a.tobytes()).hexdigest(),
        values_hex=(
            [[float(x.real).hex(), float(x.imag).hex()] for x in v]
            if np.iscomplexobj(a)
            else [float(x).hex() for x in v]
        ),
    )


def normalization_receipt(response, q, contract):
    from yapnr.rf.farfield import intensity_factor

    from .frequencies import descriptor, require_problem

    p = response.problem
    require_problem(p, contract=contract, policy=response.sampling.policy)
    if set(q) != set(response.active_excitations):
        raise ValueError("Normalization excitation set differs")
    spectrum = exact_array(p._source_spectrum(p.omega))
    if spectrum["sha256"] != contract.source_normalization_sha256:
        raise ValueError("Actual source normalization differs from contract")
    values = {
        "source_spectrum": spectrum,
        "frequencies_hz": exact_array(np.asarray(p.freqs)),
        "omega_rad_s": exact_array(np.asarray(p.omega)),
        "farfield_scale": exact_array(np.sqrt(8 * np.pi * intensity_factor(p.omega))),
    }
    for j in response.active_excitations:
        a, _ = p.port_waves(j, q[j], p.omega, excitation=j)
        values[f"incident_wave_{j}"] = exact_array(a)
        values[f"incident_power_{j}"] = exact_array(p.wave_power(j, a, p.omega))
    physical = asdict(contract)
    physical.pop("design_sha256")
    physical.pop("morphology")
    algorithm = dict(
        schema="exact-native-response-normalization/1",
        source="problem._source_spectrum",
        incident="problem.port_waves(excitation=j)",
        realized_field="far vectors projected to bases * sqrt(8*pi*intensity_factor) / incident_wave",
        power="problem.wave_power",
        implementation_sha256=hashlib.sha256(
            Path(__file__).with_name("native.py").read_bytes()
        ).hexdigest(),
        receipt_implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    return dict(
        schema="native-normalization-values/1",
        scope="CURRENT_LOCAL_EXECUTION_NOT_RECONSTRUCTED_CLOUD_VALUES",
        complex_encoding="dtype and raw contiguous bytes hash; exact float.hex components",
        values=values,
        dt_s_hex=float(p.dt).hex(),
        frequency_contract=descriptor(),
        algorithm=algorithm,
        physical_context=physical,
        identity=dict(
            normalization_sha256=sha(dict(values=values, algorithm=algorithm)),
            physical_context_sha256=sha(physical),
        ),
        design_sha256=contract.design_sha256,
        execution_authorized=False,
        physical_acceptance=False,
        delay_certified=False,
    )


class DurableEvidence:
    """Exclusive immutable records; fsync before acknowledging. Never overwrites."""

    def __init__(self, directory):
        self.path = Path(directory)
        if self.path.is_symlink():
            raise ValueError("Evidence directory must not be a symlink")
        self.path.mkdir(parents=True, exist_ok=True)
        self.sequence = 0

    def __call__(self, event, payload):
        if not event or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in event):
            raise ValueError("Invalid evidence event name")
        raw = (
            json.dumps(dict(event=event, payload=payload), sort_keys=True, indent=2, allow_nan=True)
            + "\n"
        ).encode()
        if len(raw) > 1024 * 1024:
            raise ValueError("Evidence exceeds 1 MiB record cap")
        path = self.path / f"{self.sequence:05d}-{event}.json"
        with path.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        # Directory entry durability is part of acceptance, not just file data.
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.sequence += 1
        return True


def require_persist(persist, event, payload):
    if not callable(persist) or persist(event, payload) is not True:
        raise RuntimeError("Evidence not durably saved: " + event)


def validate_normalization(receipt, contract):
    """Reject relabeled/rounded normalization values; never infer absent values."""
    from .frequencies import descriptor

    if (
        receipt.get("schema") != "native-normalization-values/1"
        or receipt.get("frequency_contract") != descriptor()
    ):
        raise ValueError("Exact normalization receipt required")
    if receipt.get("design_sha256") != contract.design_sha256:
        raise ValueError("Normalization design differs")
    for record in receipt["values"].values():
        dtype = np.dtype(record["dtype"])
        if dtype.kind == "c":
            a = np.asarray(
                [complex(float.fromhex(r), float.fromhex(i)) for r, i in record["values_hex"]],
                dtype=dtype,
            )
        elif dtype.kind == "f":
            a = np.asarray([float.fromhex(x) for x in record["values_hex"]], dtype=dtype)
        else:
            raise ValueError("Unsupported normalization type")
        a = a.reshape(record["shape"])
        if exact_array(a) != record:
            raise ValueError("Normalization exact value/hash mismatch")
    if receipt["values"]["source_spectrum"]["sha256"] != contract.source_normalization_sha256:
        raise ValueError("Normalization source binding differs")
    physical = asdict(contract)
    physical.pop("design_sha256")
    physical.pop("morphology")
    if (
        sha(receipt["physical_context"]) != sha(physical)
        or receipt["dt_s_hex"] != float(contract.time_step_s).hex()
    ):
        raise ValueError("Normalization physical context differs")
    expected = dict(
        normalization_sha256=sha(dict(values=receipt["values"], algorithm=receipt["algorithm"])),
        physical_context_sha256=sha(physical),
    )
    if receipt["identity"] != expected:
        raise ValueError("Normalization identity differs")
    if (
        receipt["algorithm"]["implementation_sha256"]
        != hashlib.sha256(Path(__file__).with_name("native.py").read_bytes()).hexdigest()
        or receipt["algorithm"]["receipt_implementation_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise ValueError("Normalization implementation differs")
    return True
