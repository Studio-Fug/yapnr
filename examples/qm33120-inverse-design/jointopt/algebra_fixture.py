"""Small complex linear field system for offline integration tests, NOT Maxwell.

Its derivative uses an actual transpose linear-system adjoint. No finite
differences or torch differentiation through the solve supplies the update.
"""

import hashlib

import numpy as np
import torch

from .bridge import Contract
from .evaluator import FieldRecord
from .objective import Response


class LinearFieldRunner:
    evidence_kind = "synthetic_complex_linear_system_NOT_FDTD"

    def __init__(self, shape):
        self.shape = shape
        self.forward_calls = 0
        self.adjoint_calls = 0
        n = np.prod(shape)
        grid = np.arange(n).reshape(shape)
        self.c = np.stack(
            [(0.25 + 0.04 * k) * (1 + 0.15 * np.sin(grid * (k + 1))) / n for k in range(6)]
        )
        self.off = np.eye(6, k=1) * (0.008 + 0.004j) + np.eye(6, k=-1) * (0.008 - 0.004j)

    def matrix(self, rho, f):
        diag = 1.0 + np.einsum("kab,ab->k", self.c, rho) + 1j * (0.03 + f * 0.001) * np.arange(6)
        return np.diag(diag) + self.off

    def forward(self, rho, port, contract, role):
        self.forward_calls += 1
        b = np.array([1.0, 0.8 + 0.15j, 0.7 - 0.2j, 0.4 + 0.1j, 0.3 - 0.1j, 0.2 + 0.2j]) * (
            1 + 0.005 * (port - 1)
        )
        fields = np.array([np.linalg.solve(self.matrix(rho, f), b) for f in range(6)])
        return FieldRecord(
            port, {"q": fields}, contract, True, ((10, 1e-13), (11, 1e-14)), 1, 1e-10, role=role
        )

    def pullback(self, rho, record, cotangents, contract, quantity):
        self.adjoint_calls += 1
        g = np.zeros(self.shape)
        for f in range(6):
            lam = np.linalg.solve(self.matrix(rho, f).T, cotangents["q"][f])
            g += np.einsum("k,kab->ab", -2 * np.real(lam * record.fields["q"][f]), self.c)
        return g


class LinearResponse:
    def __init__(self, sampling, active_excitations=(1, 2, 3, 4)):
        self.sm = sampling
        self.active_excitations = tuple(active_excitations)

    def __call__(self, fields):
        if set(fields) != set(self.active_excitations):
            raise ValueError("Missing actual excitation")
        q = torch.stack([fields[j]["q"] for j in self.active_excitations], -1)
        ff = torch.tensor(self.sm.policy.frequencies_hz, dtype=torch.float64)
        cols = [j - 1 for j in self.active_excitations]
        positions = torch.tensor(
            [[-0.0104, 0, 0], [0.0104, 0, 0], [0, -0.0084, 0], [0, 0.0084, 0]], dtype=torch.float64
        )[cols]
        n = torch.tensor(self.sm.directions)
        phase = (
            -(2 * np.pi * ff / 299792458.0)[:, None, None]
            * torch.einsum("dc,pc->dp", n, positions)[None]
        )
        # Deliberate spatial phase distortion makes both objective directional
        # derivatives informative; an ideal pair would have a zero phase gradient.
        phase = (
            phase
            + 0.7
            * q[:, 3, :].real[:, None, :]
            * (n[:, 0] + 0.3 * n[:, 2])[None, :, None]
            * torch.tensor([-1.0, 1.0, -1.0, 1.0])[cols]
        )
        beam = torch.exp(1j * phase)
        a = q[:, 1, :]
        e = torch.stack([beam * a[:, None, :], beam * (0.4 + 0.3j) * a[:, None, :]], -2)
        e = e.reshape(6, 5, len(self.sm.n), 2, len(cols))
        s = torch.stack(
            [
                torch.stack(
                    [q[:, 0, c] - 0.25 if i == j else 0.025 + 0.004 * q[:, 2, c] for i in range(4)],
                    -1,
                )
                for c, j in enumerate(cols)
            ],
            -1,
        )
        eta = 0.88 - 0.3 * q[:, 0, :].abs().square()
        accepted = 0.97 - 0.2 * q[:, 0, :].abs().square()
        return Response(s, e, eta, accepted, self.active_excitations)


def contract(policy):
    def h(s):
        return hashlib.sha256(s.encode()).hexdigest()

    return Contract(
        h("set_per_density"),
        h("synthetic-linear-field-v1"),
        tuple(policy.frequencies_hz),
        (0.0, 0.0, 0.0),
        h("synthetic-reference"),
        h("synthetic-normalization"),
        h("no-upstream-network"),
        "set_per_variant",
        0.13536742835376303e-12,
    )
