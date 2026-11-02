"""Exact scientific samples from the frozen yapnr GHz-band generator.

No relative/absolute tolerance is used. Historical rounded policy input has one
explicit migration function; saved field records are never relabeled by it.
"""

import hashlib
import inspect
from pathlib import Path

import numpy as np

SCHEMA = "qm33120-native-frequency-contract/1"
SPEC_MODULE_SHA256 = "d1d16ba4f08deaf63044e3e1fc204b1a7c677eb23ddbb670b59d46b09a73eb79"
INPUT_SPEC_SHA256 = "2b32ec8f81b32a3eddefee8d252d0dba303264e8ee85dda46f1a982de174c23d"
FREQUENCY_HEX = (
    "0x1.73eed80000000p+32",
    "0x1.82cf700000000p+32",
    "0x1.91b0080000000p+32",
    "0x1.cd32680000000p+32",
    "0x1.dc13000000000p+32",
    "0x1.eaf3980000001p+32",
)
FREQUENCIES_HZ = tuple(float.fromhex(v) for v in FREQUENCY_HEX)
LEGACY_POLICY_HZ = (
    6240000000.0,
    6489600000.0,
    6739200000.0,
    7737600000.0,
    7987200000.0,
    8236800000.0,
)


def _vector(values):
    a = np.asarray(values)
    if (
        a.shape != (6,)
        or a.dtype.kind not in "fiu"
        or not np.isfinite(a).all()
        or (a.dtype.kind == "f" and a.dtype.itemsize != 8)
    ):
        raise ValueError("Six finite real frequency samples required")
    return np.asarray(a, dtype=np.float64)


def vector_sha256(values):
    """Portable little-endian IEEE754 binary64 digest, not rounded text."""
    return hashlib.sha256(_vector(values).astype("<f8").tobytes()).hexdigest()


def require_canonical(values, label="frequency contract"):
    a = _vector(values)
    if tuple(v.hex() for v in a) != FREQUENCY_HEX:
        raise ValueError(
            label
            + ": exact pinned-runtime frequency vector required; no tolerance or record relabeling"
        )
    return FREQUENCIES_HZ


def descriptor():
    return dict(
        schema=SCHEMA,
        frequencies_hz=list(FREQUENCIES_HZ),
        binary64_hex=list(FREQUENCY_HEX),
        sha256_le_f64=vector_sha256(FREQUENCIES_HZ),
        spec_module_sha256=SPEC_MODULE_SHA256,
        original_input_spec_sha256=INPUT_SPEC_SHA256,
        construction="Band.frequencies: linspace(GHz endpoints, 3) * 1e9; Spec.objective_frequencies union",
    )


def migrate_legacy_policy_frequencies(values):
    """Explicitly migrate only the known r1/r2 six-literal POLICY vector.

    Never use this on saved field frequencies, a runtime, a worker request, or
    a Contract. Those require exact canonical identity and retain their hashes.
    """
    a = _vector(values)
    if not np.array_equal(a, np.asarray(LEGACY_POLICY_HZ)):
        raise ValueError("Not the exact known r1/r2 policy vector; migration refused")
    return FREQUENCIES_HZ, dict(
        schema="qm33120-policy-frequency-migration/1",
        source_sha256_le_f64=vector_sha256(a),
        target=descriptor(),
        source_binary64_hex=[v.hex() for v in a],
        changed_indices=[5],
        maximum_delta_hz=FREQUENCIES_HZ[-1] - LEGACY_POLICY_HZ[-1],
        applies_to="policy_configuration_only",
        saved_fields_rewritten=False,
    )


def verify_pinned_generation(spec):
    """Offline preflight. Read installed source and generate six numbers only."""
    from yapnr.rf import spec as spec_module

    source_sha = hashlib.sha256(Path(inspect.getfile(spec_module)).read_bytes()).hexdigest()
    if source_sha != SPEC_MODULE_SHA256 or np.__version__ != "1.26.4":
        raise ValueError("Pinned frequency generator source/numpy version differs")
    for name, ends in [("ch5", (6.24, 6.7392)), ("ch9", (7.7376, 8.2368))]:
        b = spec.bands[name]
        if (b.lo_ghz, b.hi_ghz, b.points, b.ghz_points) != (*ends, 3, None):
            raise ValueError("Frozen frequency band construction differs")
    require_canonical(spec.objective_frequencies(), "regenerated spec")
    return dict(**descriptor(), generator_verified=True, numpy_version=np.__version__)


def require_problem(problem, *, contract=None, policy=None):
    """Check before provider admission, field evaluation, or source preparation."""
    require_canonical(problem.freqs, "physical model")
    omega = 2.0 * np.pi * np.asarray(FREQUENCIES_HZ)
    if not np.array_equal(problem.omega, omega):
        raise ValueError("Physical angular frequencies differ from exact sample contract")
    if contract is not None:
        require_canonical(contract.frequencies_hz, "field/request record")
    if policy is not None:
        require_canonical(policy.frequencies_hz, "objective policy")
