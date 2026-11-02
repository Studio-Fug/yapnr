"""Exact uniform block map and transpose, with feasible endpoint test directions.

No smoothing, projection, morphology, copper redesign or optimizer is hidden here.
"""

import numpy as np


class BlockMap:
    def __init__(self, rho, fixed, ratio=2):
        self.ratio = ratio
        self.shape = rho.shape
        n, m = self.shape
        if n % ratio or m % ratio:
            raise ValueError("Nonintegral physical variable grid")
        q = rho.reshape(n // ratio, ratio, m // ratio, ratio)
        self.coarse = q[:, 0, :, 0].copy()
        if not np.array_equal(np.repeat(np.repeat(self.coarse, ratio, 0), ratio, 1), rho):
            raise ValueError("Variable grid changes existing copper")
        if not np.isin(rho, [0, 1]).all():
            raise ValueError("Expected preserved binary seed")
        self.fixed = (
            np.asarray(fixed, bool).reshape(n // ratio, ratio, m // ratio, ratio).any(axis=(1, 3))
        )
        self.free = ~self.fixed
        self.x = self.coarse[self.free].copy()

    def expand(self, x):
        x = np.asarray(x, float)
        if x.shape != self.x.shape or not np.isfinite(x).all() or np.any(x < 0) or np.any(x > 1):
            raise ValueError("Invalid feasible design")
        c = self.coarse.copy()
        c[self.free] = x
        return np.repeat(np.repeat(c, self.ratio, axis=0), self.ratio, axis=1)

    def jvp(self, v):
        if np.asarray(v).shape != self.x.shape:
            raise ValueError("Direction shape")
        c = np.zeros_like(self.coarse)
        c[self.free] = v
        return np.repeat(np.repeat(c, self.ratio, 0), self.ratio, 1)

    def vjp(self, g):
        g = np.asarray(g)
        n, m = self.shape
        if g.shape[-2:] != self.shape:
            raise ValueError("Cotangent shape")
        c = g.reshape(
            (*g.shape[:-2], n // self.ratio, self.ratio, m // self.ratio, self.ratio)
        ).sum(axis=(-3, -1))
        return c[..., self.free]

    def directions(self, seed):
        rng = np.random.default_rng(seed)
        sign = 1 - 2 * self.x
        # One global direction and one edge-neighborhood direction, fixed before fields.
        edges = np.zeros_like(self.coarse, dtype=bool)
        for axis in [0, 1]:
            delta = np.diff(self.coarse, axis=axis) != 0
            if axis == 0:
                edges[:-1] |= delta
                edges[1:] |= delta
            else:
                edges[:, :-1] |= delta
                edges[:, 1:] |= delta
        d = np.stack(
            [
                sign * rng.uniform(0.25, 1, len(sign)),
                sign * rng.uniform(0.25, 1, len(sign)) * edges[self.free],
            ]
        )
        if np.linalg.matrix_rank(d) < 2:
            raise ValueError("Uninformative duplicate direction preparation")
        return d / np.max(abs(d), axis=1)[:, None]


def one_sided(values, h):
    return (-3 * values[0] + 4 * values[h] - values[2 * h]) / (2 * h)


def assess(fd, adj, norm, direction, gate):
    scale = max(abs(fd), abs(adj), 1e-12)
    informative = abs(adj) > max(1e-12, 1e-8 * norm * np.linalg.norm(direction))
    return dict(
        finite_difference=float(fd),
        adjoint=float(adj),
        relative_error=float(abs(fd - adj) / scale),
        informative=bool(informative),
        passed=bool(informative and abs(fd - adj) / scale <= gate),
    )
