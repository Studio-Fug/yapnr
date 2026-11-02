"""Differentiable requirements rows over all four independent embedded responses.

Power normalization is provided by the field model, never inferred from |E|.
Hard rows are engineering screening constraints, not mandatory silicon limits.
Recommended isolation and delay targets remain objective terms and diagnostics.
Local candidate surrogate; raw observable metrics stay separate from acceptance.
No candidate-dependent loss mask or chip-plane claim.
"""

import hashlib
import json
from dataclasses import dataclass, field

import numpy as np
import torch

from yapnr.rf.farfield import Frame, sphere_quadrature

from .frequencies import FREQUENCIES_HZ, require_canonical
from .observable_variation import REVISION, corrected_terms, observable_report
from .response import differential_phase_rate, target_phase


@dataclass(frozen=True)
class Policy:
    objective_revision: str = REVISION
    frequencies_hz: tuple = FREQUENCIES_HZ
    n_theta: int = 4
    tangent_step_rad: float = 0.001
    floor: float = 0.01
    match_db: float = -10.0
    isolation_screen_db: float = -20.0  # preserves the existing antenna screen
    isolation_recommendation_db: float = -25.0  # APH511 best-performance goal
    total_efficiency: float = 0.60
    radiation_efficiency: float = 0.60
    gain_goal_dbi: float = -6.0
    shape_goal_rms_db: float = 3.0
    delay_goal_s: float = 100e-12
    hard_smooth_tau: float = 0.02
    offset_rad: tuple = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    offset_mode: str = "fixed_diagnostic_zero"
    sign: int = -1
    baselines_m: tuple = ((0.0208, 0.0, 0.0), (0.0, 0.0168, 0.0))
    weights: tuple = (
        ("phase", 1.0),
        ("floor", 1.0),
        ("slope", 0.1),
        ("delay", 0.1),
        ("gain", 1.0),
        ("shape", 1.0),
        ("isolation_advice", 1.0),
        ("path_delay_advice", 0.1),
    )

    def __post_init__(self):
        if self.n_theta < 2 or self.sign != -1 or self.offset_mode != "fixed_diagnostic_zero":
            raise ValueError("Freeze a new reviewed convention before changing it")
        if len(self.offset_rad) != 6 or any(self.offset_rad):
            raise ValueError("This revision does not fit calibration offsets")
        require_canonical(self.frequencies_hz, "objective policy")
        if self.floor <= 0 or self.hard_smooth_tau <= 0:
            raise ValueError("Positive floor/aggregation scale required")

    def digest(self):
        return hashlib.sha256(json.dumps(vars(self), sort_keys=True).encode()).hexdigest()


class Sampling:
    def __init__(self, policy):
        self.policy = policy
        self.n, w, theta, phi = sphere_quadrature(policy.n_theta)
        self.w = torch.tensor(w / (4 * np.pi), dtype=torch.float64)
        et, ep = Frame().unit_vectors(theta, phi)
        self.tangents = np.stack([et, ep])
        basis = np.stack([et, ep], axis=1)
        dirs, bases = [self.n], [basis]
        for tangent in self.tangents:
            axis = np.cross(self.n, tangent)[:, None, :]
            for sign in (1, -1):
                a = sign * policy.tangent_step_rad
                dirs.append(self.n * np.cos(a) + tangent * np.sin(a))
                bases.append(
                    basis * np.cos(a)
                    + np.cross(axis, basis) * np.sin(a)
                    + axis * np.sum(axis * basis, axis=-1, keepdims=True) * (1 - np.cos(a))
                )
        self.directions = np.concatenate(dirs)
        self.bases = torch.tensor(np.concatenate(bases), dtype=torch.complex128)
        self.jones = torch.tensor(
            [[1, 0], [0, 1], [1 / np.sqrt(2), 1j / np.sqrt(2)], [1 / np.sqrt(2), -1j / np.sqrt(2)]],
            dtype=torch.complex128,
        )


@dataclass
class Response:
    s: torch.Tensor  # [frequency, receiving port, exciting port]
    e: torch.Tensor  # [frequency, 5 direction sets, angle, Jones component, port]
    total_efficiency: torch.Tensor  # flux / incident power [frequency, port]
    accepted_fraction: torch.Tensor  # net power into all device ports / incident power
    excitation_ids: tuple = (1, 2, 3, 4)

    def check(self, sampling):
        f, a = len(sampling.policy.frequencies_hz), len(sampling.n)
        n = len(self.excitation_ids)
        if len(set(self.excitation_ids)) != n or not set(self.excitation_ids).issubset(
            {1, 2, 3, 4}
        ):
            raise ValueError("Explicit unique excitation columns required")
        shapes = (
            (self.s, (f, 4, n)),
            (self.e, (f, 5, a, 2, n)),
            (self.total_efficiency, (f, n)),
            (self.accepted_fraction, (f, n)),
        )
        if any(tuple(t.shape) != sh or not torch.isfinite(t).all() for t, sh in shapes):
            raise ValueError("Nonfinite or inconsistent normalized response")


@dataclass
class Rows:
    objective: torch.Tensor
    hard: torch.Tensor
    objective_keys: tuple
    hard_keys: tuple
    raw_hard: dict
    diagnostics: dict = field(default_factory=dict)


def upper_max(x, tau):
    """Conservative smooth maximum; bound max <= result <= max+tau log(N)."""
    return tau * torch.logsumexp(x.reshape(-1) / tau, dim=0)


class JointObjective:
    def __init__(self, policy=None, bands=("ch5", "ch9")):
        self.policy = policy or Policy()
        self.sampling = Sampling(self.policy)
        self.bands = tuple(bands)
        if not self.bands or self.bands not in (("ch5",), ("ch9",), ("ch5", "ch9")):
            raise ValueError("Explicit ordered complete band blocks required")
        self.active_ports = tuple(
            j for band in self.bands for j in ((1, 2) if band == "ch5" else (3, 4))
        )

    def dependency_ports(self, kind, index):
        if kind == "objective" and index in range(len(self.bands)):
            return (1, 2) if self.bands[index] == "ch5" else (3, 4)
        if kind == "hard" and index in range(len(self.active_ports)):
            return (self.active_ports[index],)
        raise ValueError("Unknown objective/Jacobian row")

    def rows(self, response):
        p, sm = self.policy, self.sampling
        response.check(sm)
        freq = torch.tensor(p.frequencies_hz, dtype=torch.float64)
        objective, hard, raw, diag = [], [], {}, {}
        objective_keys, hard_keys = [], []
        for band, indices, pair in [("ch5", [0, 1, 2], [0, 1]), ("ch9", [3, 4, 5], [2, 3])]:
            if band not in self.bands:
                continue
            if not all(j + 1 in response.excitation_ids for j in pair):
                raise ValueError("Missing actual excitation fields for selected objective block")
            columns = [response.excitation_ids.index(j + 1) for j in pair]
            f = freq[indices]
            ef = response.e[indices][..., columns]
            h = torch.einsum("fgacp,lc->fgalp", ef, sm.jones.conj())
            hc = h[:, 0]
            baseline = torch.tensor(p.baselines_m[band == "ch9"], dtype=torch.float64).repeat(3, 1)
            target = target_phase(
                f, torch.tensor(sm.n), baseline, torch.tensor(p.offset_rad)[indices], sign=p.sign
            )
            tg = target[:, :, None].expand(hc.shape[:-1])
            smooth = corrected_terms(hc, tg, f, sm.w, p)
            phase, delay_loss, path_delay = (
                smooth[k] for k in ("phase", "delay", "path_delay_advice")
            )
            floor_loss = (
                torch.relu((p.floor - hc.abs().square()) / p.floor).square().mean((0, 2, 3)) * sm.w
            ).sum()
            k = 2 * np.pi * f / 299792458.0
            slope_terms = []
            for t in range(2):
                dh = (h[:, 1 + 2 * t] - h[:, 2 + 2 * t]) / (2 * p.tangent_step_rad)
                rate, _ = differential_phase_rate(hc, dh, p.floor)
                want = (
                    p.sign
                    * k[:, None]
                    * torch.einsum("fc,ac->fa", baseline, torch.tensor(sm.tangents[t]))
                )
                scale = (k * torch.linalg.norm(baseline, dim=-1))[:, None, None]
                slope_terms.append(
                    (((rate - want[:, :, None]) / scale).square().mean((0, 2)) * sm.w).sum()
                )
            slope = sum(slope_terms) / 2
            # Total gain/shape and polarized reception are deliberately distinct.
            gain_power = ef[:, 0].abs().square().sum(-2)
            gain_db = 10 * torch.log10(gain_power.clamp_min(1e-20))
            gain = upper_max(
                torch.relu((p.gain_goal_dbi - gain_db) / 10).square(), p.hard_smooth_tau
            )
            log_shape = gain_db - (gain_db * sm.w[None, :, None]).sum(1, keepdim=True)
            rms2 = (log_shape.square() * sm.w[None, :, None]).sum(1)
            shape = torch.relu(rms2 / p.shape_goal_rms_db**2 - 1).mean()
            rec = torch.stack(
                [response.s[indices, pair[1], columns[0]], response.s[indices, pair[0], columns[1]]]
            )
            iso_advice = torch.relu(
                rec.abs().square() / 10 ** (p.isolation_recommendation_db / 10) - 1
            ).mean()
            terms = dict(
                phase=phase,
                floor=floor_loss,
                slope=slope,
                delay=delay_loss,
                gain=gain,
                shape=shape,
                isolation_advice=iso_advice,
                path_delay_advice=path_delay,
            )
            objective.append(sum(dict(p.weights)[name] * value for name, value in terms.items()))
            objective_keys.append(band + ":phase_coverage_and_advisory_goals")
            diag[band] = observable_report(hc, tg, f, sm.w, p, tuple(j + 1 for j in pair))
            diag[band]["terms"] = {k: float(v.detach()) for k, v in terms.items()}
            diag[band]["minimum_power"] = float(hc.abs().square().min().detach())
            for port in pair:
                other = pair[1] if port == pair[0] else pair[0]
                column = response.excitation_ids.index(port + 1)
                eta = response.total_efficiency[indices, column]
                accepted = response.accepted_fraction[indices, column]
                intrinsic = eta / accepted.clamp_min(1e-12)
                terms_hard = dict(
                    match=response.s[indices, port, column].abs().square() / 10 ** (p.match_db / 10)
                    - 1,
                    isolation_screen=response.s[indices, other, column].abs().square()
                    / 10 ** (p.isolation_screen_db / 10)
                    - 1,
                    total_efficiency=(p.total_efficiency - eta) / p.total_efficiency,
                    radiation_efficiency=(p.radiation_efficiency - intrinsic)
                    / p.radiation_efficiency,
                )
                key = f"{band}:P{port+1}:engineering_screen"
                raw[key] = terms_hard
                hard.append(upper_max(torch.cat(list(terms_hard.values())), p.hard_smooth_tau))
                hard_keys.append(key)
                diag[band][f"P{port+1}_power_accounting"] = dict(
                    total_efficiency=eta.detach().tolist(),
                    accepted_fraction=accepted.detach().tolist(),
                    radiation_efficiency=intrinsic.detach().tolist(),
                    residual_accepted_minus_radiated=(accepted - eta).detach().tolist(),
                    residual_is_certified_material_loss=False,
                )
        return Rows(
            torch.stack(objective),
            torch.stack(hard),
            tuple(objective_keys),
            tuple(hard_keys),
            raw,
            dict(
                bands=diag,
                active_band_blocks=self.bands,
                reference_plane="antenna_internal",
                RF_qualified=False,
                isolation_25dB="recommendation_objective_not_hard",
                pulse_fidelity="pending_frozen_waveform_and_dense_frequency_response",
                arbitrary_peer_orientation="canonical_polarizations_are_incomplete_sampling",
            ),
        )
