"""The whole design chain x → ρ̄ in torch (float64), with its vector-Jacobian products.

    ρ = embed(x)            material grid: free pixels from x, fixed pixels their values
    ρ_ext = extend(ρ)       the window inside the fixed exterior ring
    ρ̃_m = filter(ρ_ext)     conic filter; the window plus a margin of at least one pixel
    ρ̄ = reimpose(P_β(ρ̃))    tanh projection, then the fixed pixels set back

The solver's gradient with respect to ρ̄ (per objective and frequency) is pulled back to x by
`vjp`; the length-scale constraints are functions of ρ̃_m and ρ̄ (`lengthscale`).
"""

from __future__ import annotations

import numpy as np

from yapnr.rf.design.filters import ConicFilter
from yapnr.rf.design.lengthscale import LengthScale, indicator_constraints
from yapnr.rf.design.material_grid import MaterialGrid
from yapnr.rf.design.projection import tanh_projection


class Parameterization:
    """x (n_dof,) → ρ̄ (ni, nj) for a material grid and a conic filter."""

    def __init__(self, grid: MaterialGrid, filt: ConicFilter, eta: float = 0.5):
        if grid.ring_width < filt.half + 1:
            raise ValueError(
                f"the ring ({grid.ring_width} px) must be at least the filter half width "
                f"plus one ({filt.half + 1} px)"
            )
        self.grid = grid
        self.filter = filt
        self.eta = float(eta)
        self.margin = grid.ring_width - filt.half

    @property
    def n_dof(self) -> int:
        return self.grid.n_dof

    def forward(self, x, beta: float, eta: float | None = None) -> dict:
        """All stages as torch tensors: rho, rho_tilde_m (with margin), rho_tilde, rho_bar.
        `eta` overrides the projection threshold (the eroded and dilated designs of robust
        optimization: η above ½ erodes, below dilates)."""
        import torch

        if not isinstance(x, torch.Tensor):
            x = torch.as_tensor(np.asarray(x, dtype=np.float64))
        rho = self.grid.embed(x)
        rt_m = self.filter(self.grid.extend(rho))
        m = self.margin
        rt = rt_m[m:-m, m:-m]
        e = self.eta if eta is None else float(eta)
        rb = self.grid.reimpose(tanh_projection(rt, beta, e))
        return {"x": x, "rho": rho, "rho_tilde_m": rt_m, "rho_tilde": rt, "rho_bar": rb}

    def rho_bar(self, x, beta: float, eta: float | None = None) -> np.ndarray:
        """ρ̄ (ni, nj) as numpy."""
        import torch

        with torch.no_grad():
            st = self.forward(np.asarray(x, dtype=np.float64), beta, eta)
            return st["rho_bar"].numpy().copy()

    def rho_tilde(self, x) -> np.ndarray:
        """ρ̃ (ni, nj) as numpy."""
        import torch

        with torch.no_grad():
            return self.forward(np.asarray(x, dtype=np.float64), 1.0)["rho_tilde"].numpy().copy()

    def vjp(self, x, beta: float, cotangents: np.ndarray, eta: float | None = None) -> np.ndarray:
        """Σ_p g_kp ∂ρ̄_p/∂x for each row k of `cotangents` (K, ni, nj) → (K, n_dof)."""
        import torch

        g = np.asarray(cotangents, dtype=np.float64)
        squeeze = g.ndim == 2
        if squeeze:
            g = g[None]
        xt = torch.as_tensor(np.asarray(x, dtype=np.float64)).clone().requires_grad_(True)
        rb = self.forward(xt, beta, eta)["rho_bar"]
        out = np.zeros((g.shape[0], self.n_dof))
        for k in range(g.shape[0]):
            (gx,) = torch.autograd.grad(
                rb, xt, grad_outputs=torch.as_tensor(g[k]), retain_graph=k + 1 < g.shape[0]
            )
            out[k] = gx.numpy()
        return out[0] if squeeze else out

    def lengthscale(self, x, beta: float, ls: LengthScale) -> tuple[np.ndarray, np.ndarray]:
        """The width and space constraint values (2,) (≤ 0 when met) and their gradients
        (2, n_dof)."""
        import torch

        xt = torch.as_tensor(np.asarray(x, dtype=np.float64)).clone().requires_grad_(True)
        st = self.forward(xt, beta)
        vals = indicator_constraints(st["rho_tilde_m"], beta, self.eta, ls, self.margin)
        grads = np.zeros((2, self.n_dof))
        for k in range(2):
            (gx,) = torch.autograd.grad(vals[k], xt, retain_graph=k == 0)
            grads[k] = gx.numpy()
        return vals.detach().numpy(), grads
