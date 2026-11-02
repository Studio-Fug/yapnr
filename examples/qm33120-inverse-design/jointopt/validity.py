"""Fail-closed numerical operability gate for phase/delay evaluation.

This establishes neither RF performance nor a minimum useful coverage fraction.
At least one jointly observable sample is needed at each sampled frequency and
adjacent interval. Low-signal samples remain in the objective/coverage denominator.
"""

import math

PHASE_GATE_SCHEMA = "joint-phase-operability/2"


def require_phase_operability(diagnostics, expected_bands):
    bands = diagnostics.get("bands")
    if not isinstance(bands, dict) or not expected_bands or set(bands) != set(expected_bands):
        raise RuntimeError(
            "Missing or inconsistent active-band phase diagnostics; preserve fields and stop"
        )
    for name in expected_bands:
        band = bands[name]
        if not isinstance(band, dict):
            raise RuntimeError(f"{name}: missing phase diagnostics; preserve fields and stop")
        for warning in ("delay_alias_warning", "path_delay_alias_warning"):
            # Missing/None/non-boolean flags cannot silently act like False.
            if band.get(warning) is not False:
                raise RuntimeError(f"{name}: {warning} unsafe or missing; preserve fields and stop")
        denominator = band.get("coverage_denominator")
        fraction = band.get("valid_fraction")
        if (
            not isinstance(denominator, int)
            or isinstance(denominator, bool)
            or denominator <= 0
            or denominator % 3
            or not isinstance(fraction, (int, float))
            or isinstance(fraction, bool)
            or not math.isfinite(fraction)
            or not 0 < fraction <= 1 + 1e-12
        ):
            raise RuntimeError(f"{name}: empty or invalid phase coverage; preserve fields and stop")
        count_per_frequency = denominator // 3
        for field, length, total_field in (
            ("valid_phase_samples_by_frequency", 3, "valid_phase_samples"),
            ("valid_delay_samples_by_interval", 2, "valid_delay_samples"),
        ):
            values = band.get(field)
            total = band.get(total_field)
            if (
                not isinstance(values, (list, tuple))
                or len(values) != length
                or any(
                    not isinstance(v, int)
                    or isinstance(v, bool)
                    or not 0 < v <= count_per_frequency
                    for v in values
                )
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total != sum(values)
            ):
                raise RuntimeError(f"{name}: empty or invalid {field}; preserve fields and stop")
