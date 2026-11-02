"""Analytic acceptance regressions. No solver dispatch or geometry update."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import torch
from jointopt.acceptance import band_evidence, decide
from jointopt.objective import Policy
from jointopt.observable_variation import corrected_terms


def test_acceptance_controls():
    with tempfile.TemporaryDirectory() as directory:
        R = Path(directory)
        p = Policy()
        f = torch.tensor(p.frequencies_hz, dtype=torch.float64)[3:]
        records = []
        cases = []

        def evidence(a, b):
            a = np.asarray(a, np.complex128)
            b = np.asarray(b, np.complex128)
            if a.ndim == 3:
                a = a[:, :, None, :]
                b = b[:, :, None, :]
            F, A, J, P = a.shape
            return band_evidence(
                a, b, torch.zeros((F, A, J)), f, torch.ones(A, dtype=torch.float64) / A, p, (3, 4)
            )

        def score(a):
            h = torch.tensor(np.asarray(a, np.complex128))
            if h.ndim == 3:
                h = h[:, :, None, :]
            F, A, J, P = h.shape
            t = corrected_terms(
                h, torch.zeros((F, A, J)), f, torch.ones(A, dtype=torch.float64) / A, p
            )
            return float(
                t["phase"]
                + 0.1 * t["delay"]
                + 0.1 * t["path_delay_advice"]
                + torch.relu((p.floor - h.abs().square()) / p.floor).square().mean()
            )

        def durable(name, data):
            path = R / name
            with path.open("w") as file:
                file.write(json.dumps(data, indent=2, allow_nan=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
            records.append(dict(path=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            return True

        def check(name, a, b, want, kind=None, reason=None, **kw):
            ev = evidence(a, b)
            va = score(a)
            vb = score(b)
            # Persist prior to invoking decision, including deliberately rejected controls.
            durable(
                f"case-{len(cases):02d}-before-gate.json",
                dict(
                    name=name,
                    band=ev,
                    reference_scalar=va,
                    candidate_scalar=vb,
                    scope="Analytic control, not antenna simulation",
                ),
            )
            opts = dict(context_matches=True, surrogate_reference=va, surrogate_candidate=vb)
            opts.update(kw)
            result = decide(
                [ev], np.array([-1.0]), np.array([-1.0]), np.zeros(4), np.zeros(4), **opts
            )
            assert result["accepted_optimization_progress"] == want, (name, result)
            if kind:
                assert result["progress_kind"] == kind, (name, result)
            if reason:
                assert result["reason"] == reason, (name, result)
            for key in (
                "qualified_design",
                "execution_authorized",
                "physical_acceptance",
                "delay_certified",
                "full_band_delay_certified",
            ):
                assert result[key] is False
            if not want:
                assert result["trial_status"] == "proposed"
            cases.append(dict(name=name, reference_scalar=va, candidate_scalar=vb, decision=result))
            return ev, result

        def powers(pp):
            return (
                np.broadcast_to(np.sqrt(pp)[None, :, None], (3, len(pp), 2))
                .astype(np.complex128)
                .copy()
            )

        # Exact prior redistribution counterexample: phase/delay zero, no floor worsens.
        a = powers([0.0025, 0.04])
        b = powers([0.005, 0.0375])
        ev, rs = check(
            "beneficial fixed-power redistribution", a, b, True, "feasibility_restoration"
        )
        assert rs["candidate_floor_infeasible"] and not rs["sampled_phase_delay_metrics_improved"]
        assert np.allclose(abs(a).sum(), abs(b).sum()) is False  # Fields actually differ.

        # Preserve the prior circular-worsening counterexample, now rejected.
        def steps(re, im, re2, im2):
            return np.stack(
                [
                    np.ones(2, complex),
                    [complex(re, im), complex(re, -im)],
                    [complex(re2, im2), complex(re2, -im2)],
                ]
            )[:, :, None].repeat(2, 2)

        a = steps(-0.6, 0.8, -0.28, -0.96)
        b = steps(-0.8, 0.6, 0.28, -0.96)
        ev, rs = check(
            "circular descent worsens observable delay variation",
            a,
            b,
            False,
            reason="unapproved_metric_tradeoff",
        )
        assert score(b) < score(a) and ev["candidate_warning_gate_passed"]
        assert any(
            c["regressed"]
            for n, c in ev["existing_support_comparisons"].items()
            if n.startswith("path_variation")
        )
        # Phase-only improvement at exactly preserved powers, not dependent on scalar.
        a = np.ones((3, 2, 2), complex)
        a[:, :, 1] = -1
        b = np.ones_like(a)
        check("phase improvement on fixed support", a, b, True, "sampled_phase_delay_progress")
        # Attenuation with lower surrogate alone never establishes progress.
        a = np.ones((3, 2, 2), complex)
        a[:, :, 1] = -1
        b = a * 0.5
        check(
            "above-floor attenuation cannot claim phase progress",
            a,
            b,
            False,
            reason="surrogate_only_change",
        )
        # Floor improvement is genuine even when confidence-weighted phase loss rises.
        a = powers([0.005625, 1.0])
        a[:, :, 1] *= -1
        b = powers([0.0081, 1.0])
        b[:, :, 1] *= -1
        _, rs = check(
            "restoration despite higher scalar with unchanged raw phases",
            a,
            b,
            True,
            "feasibility_restoration",
        )
        assert not rs["surrogate_improved"]
        # But a floor improvement cannot purchase a worsening observable phase.
        a = powers([0.0025, 0.04])
        b = powers([0.005, 0.0375])
        b[:, 1, 1] *= -1
        check(
            "restoration plus raw phase regression remains proposed",
            a,
            b,
            False,
            reason="unapproved_metric_tradeoff",
        )
        # Existing weaker sample may not worsen merely because aggregate metrics improve.
        a = powers([0.001, 0.005, 0.04])
        b = powers([0.002, 0.0045, 0.0395])
        check(
            "aggregate floor improvement with component regression",
            a,
            b,
            False,
            reason="protected_feasibility_or_operability_gate_failed",
        )
        # Threshold crossing: reference below P0, candidate just above; new pi residual
        # is newly measured, not compared to a fabricated prior phase or called improved.
        low = np.nextafter(0.1, 0)
        high = 0.1
        a = np.ones((3, 2, 2), complex)
        a[:, 0] = low
        b = a.copy()
        b[:, 0] = high
        b[:, 0, 1] *= -1
        be, rs = check(
            "upward threshold crossing with newly visible bad phase",
            a,
            b,
            True,
            "feasibility_restoration",
        )
        assert (
            be["new_observable_pair_samples"] == 3
            and be["new_observable_pair_phase_max_abs_rad"] == np.pi
        )
        assert not rs["sampled_phase_delay_metrics_improved"]
        assert (
            be["existing_support_comparisons"]["absolute_pair_phase_residual_rad"]["compared"] == 3
        )
        check(
            "downward threshold crossing cannot buy scalar phase gain",
            b,
            a,
            False,
            reason="protected_feasibility_or_operability_gate_failed",
        )
        # Same rounded floor observation ordering as r3, both sides at the boundary.
        a = np.ones((3, 2, 2), complex)
        a[:, 0] = complex(0.06, 0.08)
        check("boundary no-op", a, a, False, reason="no_progress")
        # All-dark to some observable support may restore, but there is no comparable phase.
        a = powers([0.0001, 0.0001])
        b = powers([0.04, 0.0001])
        be, rs = check(
            "all-dark reference to partial observable restoration",
            a,
            b,
            True,
            "feasibility_restoration",
        )
        assert not rs["sampled_phase_delay_metrics_improved"] and all(
            x["compared"] == 0 for x in be["existing_support_comparisons"].values()
        )
        check(
            "still no observable support after brightening",
            a,
            a * 1.1,
            False,
            reason="protected_feasibility_or_operability_gate_failed",
        )
        # Warn on an actual near-pi observed interval despite improving an independent floor.
        a = powers([0.0025, 0.04])
        b = powers([0.005, 0.04])
        b[1:, 1, 1] *= -1
        check(
            "restoration cannot bypass observed alias warning",
            a,
            b,
            False,
            reason="protected_feasibility_or_operability_gate_failed",
        )
        check(
            "context mismatch",
            a,
            a,
            False,
            reason="protected_feasibility_or_operability_gate_failed",
            context_matches=False,
        )
        # No compensation across bands or protected constraint/advisory vectors.
        zero = powers([1.0, 1.0])
        anti = zero.copy()
        anti[:, :, 1] *= -1
        good = evidence(anti, zero)
        bad = evidence(zero, anti)
        r = decide(
            [good, bad],
            [-1],
            [-1],
            [0],
            [0],
            context_matches=True,
            surrogate_reference=10.0,
            surrogate_candidate=1.0,
        )
        assert not r["accepted_optimization_progress"] and r["tradeoffs_requiring_decision"]
        cases.append(dict(name="no inter-band phase tradeoff", decision=r))
        r = decide(
            [evidence(zero, zero)],
            [1.0, 0.5],
            [0.8, 0.6],
            [0],
            [0],
            context_matches=True,
            surrogate_reference=1.0,
            surrogate_candidate=0.5,
        )
        assert (
            not r["accepted_optimization_progress"] and "hard_violation_regression" in r["blockers"]
        )
        cases.append(dict(name="hard violations cannot compensate each other", decision=r))
        r = decide(
            [evidence(zero, zero)],
            [1.0, 0.5],
            [0.8, 0.5],
            [0],
            [0],
            context_matches=True,
            surrogate_reference=1.0,
            surrogate_candidate=1.0,
        )
        assert (
            r["accepted_optimization_progress"] and r["progress_kind"] == "feasibility_restoration"
        )
        cases.append(dict(name="hard restoration with no other regression", decision=r))
        r = decide(
            [evidence(zero, zero)],
            [-1],
            [-1],
            [1.0, 1.0],
            [0.5, 1.1],
            context_matches=True,
            surrogate_reference=1.0,
            surrogate_candidate=0.5,
        )
        assert not r["accepted_optimization_progress"] and r["tradeoffs_requiring_decision"]
        cases.append(dict(name="retained advisory tradeoff not invented", decision=r))
        # Full saved Response entry point: actual prior field fixture, no solver.


if __name__ == "__main__":
    test_acceptance_controls()
    print("Analytic acceptance controls passed; no FDTD executed")
