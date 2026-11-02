"""Actual unchanged uniform-grid array and internal pair objective; no solve on import."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch
from bridge import Contract, Forward, joint_wirtinger
from parameterization import BlockMap
from response import differential_phase_rate, excess_pair_delay, pair_metrics, target_phase

from yapnr.rf.farfield import Frame, intensity_factor, sphere_quadrature
from yapnr.rf.fdtd.monitors import Probe, box_indices
from yapnr.rf.problem import Problem
from yapnr.rf.spec import Spec

ROOT = Path(__file__).resolve().parent


def read(n):
    return json.loads((ROOT / n).read_text())


def digest(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def tensor(a, complex=False):
    return torch.as_tensor(a, dtype=torch.complex128 if complex else torch.float64)


class ActualArray:
    def __init__(self, threads=16):
        self.cfg = c = read("config.json")
        raw = read("inputs/spec.json")
        raw["solver"]["max_steps"] = c["max_steps_per_derivative_solve"]
        spec = Spec.from_dict(raw)
        dt = c["dt_ps"] * 1e-12
        original = Problem._time_step

        def fixed(p, g):
            if dt > original(p, g) * (1 + 1e-12):
                raise ValueError("Frozen timestep exceeds bound")
            return dt

        with patch.object(Problem, "_time_step", fixed):
            self.p = p = Problem(spec, threads=threads, exact=True)
        audit = read("inputs/audit.json")
        assert list(p.grid.n) == audit["grid_cells"] and p._decimation() == 1 and p.dt == dt
        for k in range(3):
            assert digest(p.grid.axis(k).nodes) == audit["grid_axis_sha256"][k]
        i0, i1, j0, j1 = p.domain.window
        for a, lo, hi in [(p.grid.x, i0, i1), (p.grid.y, j0, j1)]:
            np.testing.assert_allclose(a.primary[lo:hi], 0.0001, atol=1e-15, rtol=0)
        self.rho = np.load(ROOT / "inputs/rho.npz", allow_pickle=False)["rho"]
        self.map = BlockMap(self.rho, p.material.fixed)
        self.x = self.map.x
        self.directions = self.map.directions(c["seed"])
        np.testing.assert_array_equal(self.map.expand(self.x), self.rho)
        if np.any(p.material.fixed & (p.material.fixed_value != self.rho)):
            raise ValueError("Frozen protected copper mismatch")
        p.set_design(self.rho)
        p.tol = c["forward_tol"]
        p.adjoint_tol = c["adjoint_tol"]
        # Keep the original ground H monitors for forward convergence. They are not objective leaves.
        g = p.grid
        kg = p.domain.k_g
        bx = spec.board.x_mm
        by = spec.board.y_mm
        for comp in ["hx", "hy"]:
            sh = g.shape(comp)
            idx = box_indices(
                g,
                comp,
                [
                    (g.x.node(bx[0] * 1e-3), min(g.x.node(bx[1] * 1e-3) + 1, sh[0])),
                    (g.y.node(by[0] * 1e-3), min(g.y.node(by[1] * 1e-3) + 1, sh[1])),
                    (kg - 1, kg + 1),
                ],
            )
            probe = Probe("ground_" + comp, comp, idx)
            p.design_probes.append(probe)
            p.watched.append(probe.name)
        self.watched = [q.name for q in p.port_probes + p.box_probes]
        n, w, theta, phi = sphere_quadrature(c["full_sphere_n_theta"])
        et, ep = Frame().unit_vectors(theta, phi)
        self.n = n
        self.w = tensor(w / (4 * np.pi))
        self.tangents = np.stack([et, ep])
        self.basis = np.stack([et, ep], axis=1)
        dirs = [n]
        bases = [self.basis]
        for t in self.tangents:
            ax = np.cross(n, t)
            for sign in [1, -1]:
                ang = sign * c["tangent_step_rad"]
                dirs.append(n * np.cos(ang) + t * np.sin(ang))
                v = self.basis
                a = ax[:, None, :]
                bases.append(
                    v * np.cos(ang)
                    + np.cross(a, v) * np.sin(ang)
                    + a * np.sum(a * v, axis=-1, keepdims=True) * (1 - np.cos(ang))
                )
        self.dirs = np.concatenate(dirs)
        self.bases = tensor(np.concatenate(bases))
        np.testing.assert_allclose(
            np.sum(self.dirs[:, None, :] * self.bases.numpy(), axis=-1), 0, atol=1e-14
        )
        self.indices = c["active_frequency_indices"]
        freq = p.freqs[self.indices]
        b = np.tile(c["target_baselines_m"][c["active_band"]], (len(freq), 1))
        self.baseline = tensor(b)
        self.target = target_phase(
            tensor(freq),
            tensor(n),
            self.baseline,
            torch.zeros(len(freq), dtype=torch.float64),
            sign=c["target_sign"],
        )
        k = tensor(2 * np.pi * freq / 299792458.0)
        self.slope_targets = (
            c["target_sign"]
            * k[:, None, None]
            * torch.einsum("fc,tac->fta", self.baseline, tensor(self.tangents))
        )
        self.slope_scale = (k * torch.linalg.norm(self.baseline, dim=-1))[:, None, None, None]
        self.jones = tensor(
            [[1, 0], [0, 1], [1 / np.sqrt(2), 1j / np.sqrt(2)], [1 / np.sqrt(2), -1j / np.sqrt(2)]],
            True,
        )
        self.contract = Contract(
            digest(self.rho),
            hashlib.sha256(json.dumps(p.describe(), sort_keys=True).encode()).hexdigest(),
            tuple(p.freqs),
            (0, 0, 0),
            hashlib.sha256(json.dumps(audit["port_contract"], sort_keys=True).encode()).hexdigest(),
            digest(p._source_spectrum(p.omega)),
            hashlib.sha256(b"NO_UPSTREAM_NETWORK_INTERNAL_PAIR_ONLY").hexdigest(),
            "binary_unchanged_uniform_0.2mm_block_variables",
            p.dt,
        )

    def response(self, q):
        p = self.p
        fields = []
        for j in self.cfg["active_pair"]:
            a, _ = p.port_waves(j, q[j], p.omega, excitation=j)
            F = p.far.vectors(q[j], p.omega, self.dirs)
            E = (
                torch.einsum("fdc,dqc->fdq", F, self.bases.to(torch.complex128))
                * tensor(np.sqrt(8 * np.pi * intensity_factor(p.omega)))[:, None, None]
                / torch.as_tensor(a)[:, None, None]
            )
            fields.append(torch.einsum("fdq,pq->fdp", E, self.jones.conj()))
        return torch.stack(fields, dim=-1)[self.indices].reshape(3, 5, len(self.n), 4, 2)

    def components(self, q):
        c = self.cfg
        h = self.response(q)
        hc = h[:, 0]
        target = self.target[:, :, None].expand(hc.shape[:-1])
        floor = c["power_floor"]
        pm = pair_metrics(hc, target, floor)
        product = hc[..., 1] * hc[..., 0].conj()
        res = (
            product / torch.sqrt(abs(product) ** 2 + (floor * 1e-6) ** 2) * torch.exp(-1j * target)
        )
        phase = ((abs(res - 1) ** 2).mean(dim=(0, 2)) * self.w).sum()
        floor_loss = (
            (torch.relu((floor - abs(hc) ** 2) / floor) ** 2).mean(dim=(0, 2, 3)) * self.w
        ).sum()
        slopes = []
        for t in [0, 1]:
            dh = (h[:, 1 + 2 * t] - h[:, 2 + 2 * t]) / (2 * c["tangent_step_rad"])
            rate, _ = differential_phase_rate(hc, dh, floor)
            slopes.append(rate)
        slope = torch.stack(slopes, dim=1)
        slope_loss = (
            (((slope - self.slope_targets[:, :, :, None]) / self.slope_scale) ** 2).mean(
                dim=(0, 1, 3)
            )
            * self.w
        ).sum()
        delay, valid, steps = excess_pair_delay(
            hc.reshape(3, -1, 2), target.reshape(3, -1), tensor(self.p.freqs[self.indices]), floor
        )
        delay_loss = (
            ((delay.reshape(2, len(self.n), 4) / c["delay_scale_s"]) ** 2).mean(dim=(0, 2)) * self.w
        ).sum()
        terms = dict(phase=phase, floor=floor_loss, slope=slope_loss, delay=delay_loss)
        value = sum(c["components_weights"][k] * v for k, v in terms.items())
        return value, dict(
            components={k: float(v.detach()) for k, v in terms.items()},
            valid_phase_samples=int(pm["valid"].sum()),
            total_phase_samples=pm["valid"].numel(),
            max_frequency_phase_step_rad=float(abs(steps).max().detach()),
            scope=(
                "Internal antenna pair, sparse full-sphere derivative exercise only; "
                "no chip/network/polarization-coverage acceptance"
            ),
        )

    def objective(self, q):
        return self.components(q)[0]

    def joint(self, forwards):
        return joint_wirtinger(
            self.objective,
            [
                Forward(j, f.dft, self.contract, bool(f.converged))
                for j, f in sorted(forwards.items())
            ],
            self.watched,
        )
