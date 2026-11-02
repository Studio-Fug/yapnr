"""Uniform block variables, native conic filter/projection and complete transpose."""

import hashlib
import math

import numpy as np
import torch

from yapnr.rf.design.material_grid import MaterialGrid
from yapnr.rf.design.parameterization import Parameterization


class Chain:
    def __init__(self, baseline, native_material, native_filter, ratio=2):
        self.baseline = np.asarray(baseline, dtype=float).copy()
        self.ratio = int(ratio)
        n, m = self.baseline.shape
        if n % ratio or m % ratio or not np.isin(self.baseline, [0, 1]).all():
            raise ValueError("Exact binary seed on integral uniform blocks required")
        b = self.baseline.reshape(n // ratio, ratio, m // ratio, ratio)
        coarse = b[:, 0, :, 0]
        if not np.array_equal(coarse.repeat(ratio, 0).repeat(ratio, 1), self.baseline):
            raise ValueError("Variable map would change existing seed")
        self.fixed = np.asarray(native_material.fixed, bool).copy()
        self.fixed_value = np.asarray(native_material.fixed_value, float).copy()
        if np.any(self.fixed & (self.fixed_value != self.baseline)):
            raise ValueError("Protected copper differs from frozen seed")
        self.coarse_fixed = self.fixed.reshape(n // ratio, ratio, m // ratio, ratio).any((1, 3))
        self.coarse = coarse.copy()
        self.free = ~self.coarse_fixed
        self.x0 = coarse[self.free].copy()
        self.material = MaterialGrid(
            self.baseline.shape,
            fixed=self.fixed,
            fixed_value=self.fixed_value,
            ring=native_material.ring,
            ring_width=native_material.ring_width,
        )
        self.native = Parameterization(self.material, native_filter)

    @property
    def n(self):
        return len(self.x0)

    def _expand(self, x):
        t = x if isinstance(x, torch.Tensor) else torch.tensor(x, dtype=torch.float64)
        if t.shape != (self.n,) or not torch.isfinite(t).all() or bool(((t < 0) | (t > 1)).any()):
            raise ValueError("Invalid design variables")
        c = torch.tensor(self.coarse, dtype=torch.float64)
        c = c.masked_scatter(torch.tensor(self.free), t)
        return c.repeat_interleave(self.ratio, 0).repeat_interleave(self.ratio, 1)

    def tensor(self, x, beta, eta=0.5):
        # Native parameterization sees a uniform fine material grid. Block map is
        # outside it; autograd includes both map transposes without averaging.
        fine = self._expand(x)
        return self.native.forward(fine[torch.tensor(self.material.free)], beta, eta)["rho_bar"]

    def density(self, x, beta, eta=0.5):
        with torch.no_grad():
            return self.tensor(x, beta, eta).numpy().copy()

    def vjp(self, x, beta, g, eta=0.5):
        cot = np.asarray(g, float)
        single = cot.ndim == 2
        if single:
            cot = cot[None]
        if cot.shape[1:] != self.baseline.shape or not np.isfinite(cot).all():
            raise ValueError("Invalid density cotangent")
        xt = torch.tensor(x, dtype=torch.float64, requires_grad=True)
        rho = self.tensor(xt, beta, eta)
        out = []
        for i, row in enumerate(cot):
            (grad,) = torch.autograd.grad(
                rho, xt, grad_outputs=torch.tensor(row), retain_graph=i < len(cot) - 1
            )
            out.append(grad.detach().numpy())
        return np.asarray(out)[0] if single else np.asarray(out)

    def initialization(self, beta, etas):
        return [
            dict(
                eta=e,
                changed_pixels=int(
                    np.count_nonzero(self.density(self.x0, beta, e) != self.baseline)
                ),
                max_density_change=float(
                    np.max(abs(self.density(self.x0, beta, e) - self.baseline))
                ),
                binary_changed_pixels=int(
                    np.count_nonzero(self.density(self.x0, math.inf, e) != self.baseline)
                ),
            )
            for e in etas
        ]

    def fingerprint(self):
        h = hashlib.sha256()
        for a in (
            self.baseline,
            self.fixed,
            self.fixed_value,
            self.material.ring,
            self.native.filter.kernel,
        ):
            h.update(np.ascontiguousarray(a).tobytes())
        h.update(str(self.ratio).encode())
        return h.hexdigest()
