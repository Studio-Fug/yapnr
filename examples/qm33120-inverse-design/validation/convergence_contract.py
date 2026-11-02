"""Frozen P3 convergence protocol, shared by producer and independent consumers."""

import hashlib
import json
import math

MAX_STEPS = 196608
SOURCE_END = 29598
PERIOD = 1184
TOL = 1e-10
CONSECUTIVE = 2
PROTOCOL = dict(
    schema="P3-postsource-convergence/2",
    audit="captured-native-max-scalars/1",
    max_steps=MAX_STEPS,
    source_end=SOURCE_END,
    period=PERIOD,
    tolerance=TOL,
    comparison="strict_less_than",
    consecutive=CONSECUTIVE,
    first_snapshot_step=30782,
    first_residual_step=31966,
    earliest_converged_step=33150,
    decimation=1,
    all_declared_probes=True,
    relative_rule=(
        "native max across per-probe maxabs delta/maxabs current; "
        "skip scale<=1e-300 or scale<1e-14*globalmax"
    ),
    gradient_qualified=False,
)
PROTOCOL_SHA = hashlib.sha256(
    json.dumps(PROTOCOL, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def request(binding):
    if (
        binding.get("convergence_protocol_sha256") != PROTOCOL_SHA
        or binding.get("max_steps") != MAX_STEPS
        or binding.get("convergence_tolerance") != TOL
        or binding.get("decimation") != 1
    ):
        raise ValueError("Frozen convergence request differs")
    if binding.get("scope") not in (
        "actual_P3_convergence_only",
        "tiny_remapped_saved_P3_convergence_only",
    ):
        raise ValueError("Convergence scope differs")


def number(x):
    return type(x) in (int, float) and math.isfinite(x) and x >= 0


def captured_audit(names, sizes, reductions, value):
    """Replay only the exact scalar reductions captured inside original relative_change.

    No complex magnitudes are recomputed. Preserve original two-pass scaling and
    skip decisions, division/max order, and strict equality to the native return.
    """
    if (
        not isinstance(sizes, list)
        or len(sizes) != len(names)
        or any(type(n) is not int or n < 0 for n in sizes)
    ):
        raise ValueError("Invalid probe sizes")
    if (
        not isinstance(reductions, list)
        or len(reductions) > 3 * len(names)
        or not all(number(x) for x in reductions)
        or not number(value)
    ):
        raise ValueError("Invalid captured scalar reductions")
    i = 0

    def take():
        nonlocal i
        if i >= len(reductions):
            raise ValueError("Missing captured scalar reduction")
        x = reductions[i]
        i += 1
        return x

    first = {n: (take() if size else 0.0) for n, size in zip(names, sizes)}
    globalmax = max(first.values(), default=0.0)
    worst = 0.0
    probes = {}
    active = 0
    for n, size in zip(names, sizes):
        scale = take() if size else 0.0
        skip = not size or scale <= 1e-300 or scale < 1e-14 * globalmax
        delta = None if skip else take()
        relative = None if skip else delta / scale
        if not skip:
            worst = max(worst, relative)
            active += 1
        probes[n] = dict(
            max_abs_global_pass=first[n],
            max_abs_current=scale,
            max_abs_delta=delta,
            relative_change=relative,
            skipped=bool(skip),
        )
    if i != len(reductions):
        raise ValueError("Extra captured scalar reductions")
    if worst != value:
        raise ValueError("Captured native scalar result differs from unchanged delegate")
    return dict(
        audit_schema="captured-native-max-scalars/1",
        probe_sizes=sizes,
        raw_reductions=reductions,
        global_scale=globalmax,
        active_probes=active,
        probes=probes,
    )


def validate(record, binding):
    request(binding)
    if (
        record.get("convergence_protocol_sha256") != PROTOCOL_SHA
        or record.get("source_end") != SOURCE_END
        or record.get("source_injection_complete") is not True
    ):
        raise ValueError("Injection/convergence protocol differs")
    n = record.get("steps")
    converged = record.get("converged")
    history = record.get("history")
    audit = record.get("convergence_audit")
    if (
        type(n) is not int
        or not SOURCE_END < n <= MAX_STEPS
        or type(converged) is not bool
        or not isinstance(history, list)
        or not isinstance(audit, list)
    ):
        raise ValueError("Invalid convergence evidence shape")
    expected = list(range(SOURCE_END + 2 * PERIOD, n + 1, PERIOD))
    names = [p["name"] for p in binding["probes"]]
    if len(history) != len(expected) or len(audit) != len(expected):
        raise ValueError("Missing postsource residual history")
    good = 0
    first_pass = None
    for row, proof, step in zip(history, audit, expected):
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 2
            or type(row[0]) is not int
            or row[0] != step
            or not number(row[1])
        ):
            raise ValueError("Invalid residual step/value")
        if (
            proof.get("step") != step
            or proof.get("change") != row[1]
            or list(proof.get("probes", {})) != names
        ):
            raise ValueError("Residual audit binding")
        sizes = [6 * p["points"] for p in binding["probes"]]
        if proof.get("probe_sizes") != sizes:
            raise ValueError("Captured probe sizes differ from authenticated request")
        decoded = captured_audit(names, sizes, proof.get("raw_reductions"), row[1])
        if proof != {"step": step, "change": row[1], **decoded}:
            raise ValueError("Captured scalar audit fields differ")
        if decoded["active_probes"] < 1 or decoded["global_scale"] <= 0:
            raise ValueError("Empty/nonfinite convergence support")
        good = good + 1 if row[1] < TOL else 0
        if good >= CONSECUTIVE and first_pass is None:
            first_pass = step
    if converged:
        if first_pass != n or n >= MAX_STEPS or n < PROTOCOL["earliest_converged_step"]:
            raise ValueError("Invalid first convergence termination")
    elif n != MAX_STEPS or first_pass is not None:
        raise ValueError("Incomplete/noncanonical step-cap termination")
    return dict(
        converged=converged,
        status="CONVERGED_P3_ONLY" if converged else "INCONCLUSIVE_STEP_CAP",
        last_two_changes=[x[1] for x in history[-2:]],
        checks=len(history),
        source_complete=True,
        gradient_qualified=False,
    )
