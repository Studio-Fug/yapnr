"""The method of moving asymptotes (MMA), written from Svanberg's publications.

K. Svanberg, "The method of moving asymptotes — a new method for structural optimization"
(1987); "A class of globally convergent optimization methods based on conservative convex
separable approximations" (2002); "MMA and GCMMA — two methods for nonlinear optimization"
(technical note, 2007). The reference implementations are GPL; this module follows the
equations of the publications and copies no code.

The problem, in Svanberg's general form:

    min  f_0(x) + a_0 z + Σ_i (c_i y_i + ½ d_i y_i²)
    s.t. f_i(x) − a_i z − y_i ≤ 0   (i = 1..m),   x_min ≤ x ≤ x_max,   y ≥ 0,   z ≥ 0

With f_0 = 0, a_0 = 1, a_i = 1 and large c_i it is the min-max problem min_x max_i f_i(x)
(when every f_i ≥ 0), which `epigraph` uses. Each iteration replaces f_i by the convex separable
approximation at x_k

    f̃_i(x) = Σ_j [ p_ij/(u_j − x_j) + q_ij/(x_j − l_j) ] + r_i,
    p_ij = (u_j − x_j)² (1.001 (∂f_i/∂x_j)⁺ + 0.001 (∂f_i/∂x_j)⁻ + raa0/(x_max − x_min)_j),
    q_ij = (x_j − l_j)² (0.001 (∂f_i/∂x_j)⁺ + 1.001 (∂f_i/∂x_j)⁻ + raa0/(x_max − x_min)_j),

with asymptotes l < x_k < u that move apart while x_j oscillates monotonically and contract
when it oscillates, and solves the convex subproblem within the move limits

    α_j = max(x_min, l_j + albefa (x_j − l_j), x_j − move (x_max − x_min)_j),
    β_j = min(x_max, u_j − albefa (u_j − x_j), x_j + move (x_max − x_min)_j)

by a primal-dual interior-point method (`solve_subproblem`): Newton steps on the perturbed KKT
conditions, reduced to an (m + 1) × (m + 1) linear system in (Δλ, Δz), with ε decreasing from 1
to `epsimin` by factors of ten.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class MMASettings:
    """Svanberg's parameters (defaults from the 2007 note; `move` per design §8.2)."""

    asyinit: float = 0.5
    asyincr: float = 1.2
    asydecr: float = 0.7
    albefa: float = 0.1
    raa0: float = 1e-5
    move: float = 0.2
    epsimin: float = 1e-7
    max_newton: int = 200


@dataclass
class MMAState:
    """What MMA carries from one iteration to the next (checkpointed by the driver)."""

    k: int = 0
    xold1: np.ndarray | None = None
    xold2: np.ndarray | None = None
    low: np.ndarray | None = None
    upp: np.ndarray | None = None
    # Conservative variant: the curvature parameters reached by a rejected step, from which the
    # next call at the same x_k continues (None after an accepted step).
    rho: np.ndarray | None = None

    def to_arrays(self, prefix: str = "mma_") -> dict:
        out = {f"{prefix}k": np.array(self.k)}
        for name in ("xold1", "xold2", "low", "upp", "rho"):
            v = getattr(self, name)
            if v is not None:
                out[prefix + name] = np.asarray(v)
        return out

    @classmethod
    def from_arrays(cls, arrays, prefix: str = "mma_") -> "MMAState":
        def get(name):
            key = prefix + name
            return np.array(arrays[key]) if key in arrays else None

        return cls(
            k=int(np.asarray(arrays[prefix + "k"])),
            xold1=get("xold1"),
            xold2=get("xold2"),
            low=get("low"),
            upp=get("upp"),
            rho=get("rho"),
        )


@dataclass
class Subsolution:
    """The primal and dual variables of a solved subproblem."""

    x: np.ndarray
    y: np.ndarray
    z: float
    lam: np.ndarray
    xsi: np.ndarray
    eta: np.ndarray
    mu: np.ndarray
    zet: float
    s: np.ndarray
    newton_steps: int = 0


@dataclass
class _Approx:
    """The data of one subproblem."""

    low: np.ndarray
    upp: np.ndarray
    alpha: np.ndarray
    beta: np.ndarray
    p0: np.ndarray
    q0: np.ndarray
    P: np.ndarray
    Q: np.ndarray
    b: np.ndarray
    a0: float
    a: np.ndarray
    c: np.ndarray
    d: np.ndarray


def _residual(ap: _Approx, v: dict, eps: float) -> tuple[np.ndarray, dict]:
    """The perturbed KKT residuals of the subproblem at the point `v`."""
    x, y, z, lam = v["x"], v["y"], v["z"], v["lam"]
    xsi, eta, mu, zet, s = v["xsi"], v["eta"], v["mu"], v["zet"], v["s"]
    ux = ap.upp - x
    xl = x - ap.low
    plam = ap.p0 + ap.P.T @ lam
    qlam = ap.q0 + ap.Q.T @ lam
    g = ap.P @ (1.0 / ux) + ap.Q @ (1.0 / xl)
    r = {
        "x": plam / ux**2 - qlam / xl**2 - xsi + eta,
        "y": ap.c + ap.d * y - lam - mu,
        "z": np.array([ap.a0 - zet - ap.a @ lam]),
        "lam": g - ap.a * z - y + s - ap.b,
        "xsi": xsi * (x - ap.alpha) - eps,
        "eta": eta * (ap.beta - x) - eps,
        "mu": mu * y - eps,
        "zet": np.array([zet * z - eps]),
        "s": lam * s - eps,
    }
    return np.concatenate(list(r.values())), r


def _newton_direction(ap: _Approx, v: dict, r: dict) -> dict:
    """The Newton step of the perturbed KKT system, reduced to (Δλ, Δz)."""
    x, y, z, lam = v["x"], v["y"], v["z"], v["lam"]
    xsi, eta, mu, zet, s = v["xsi"], v["eta"], v["mu"], v["zet"], v["s"]
    ux = ap.upp - x
    xl = x - ap.low
    xa = x - ap.alpha
    bx = ap.beta - x
    plam = ap.p0 + ap.P.T @ lam
    qlam = ap.q0 + ap.Q.T @ lam
    G = ap.P / ux**2 - ap.Q / xl**2  # ∂g_i/∂x_j, (m, n)
    d_x = 2.0 * plam / ux**3 + 2.0 * qlam / xl**3 + xsi / xa + eta / bx
    del_x = -r["x"] - r["xsi"] / xa + r["eta"] / bx
    d_y = ap.d + mu / y
    del_y = -r["y"] - r["mu"] / y
    del_z = -r["z"][0] - r["zet"][0] / z
    del_lam = -r["lam"] + r["s"] / lam
    gd = G / d_x
    m = lam.size
    mat = np.empty((m + 1, m + 1))
    mat[:m, :m] = gd @ G.T + np.diag(s / lam + 1.0 / d_y)
    mat[:m, m] = ap.a
    mat[m, :m] = ap.a
    mat[m, m] = -zet / z
    rhs = np.concatenate([-del_lam + gd @ del_x - del_y / d_y, [-del_z]])
    sol = np.linalg.solve(mat, rhs)
    dlam, dz = sol[:m], sol[m]
    dx = (del_x - G.T @ dlam) / d_x
    dy = (del_y + dlam) / d_y
    return {
        "x": dx,
        "y": dy,
        "z": dz,
        "lam": dlam,
        "xsi": (-r["xsi"] - xsi * dx) / xa,
        "eta": (-r["eta"] + eta * dx) / bx,
        "mu": (-r["mu"] - mu * dy) / y,
        "zet": (-r["zet"][0] - zet * dz) / z,
        "s": (-r["s"] - s * dlam) / lam,
    }


_POSITIVE = ("y", "z", "lam", "xsi", "eta", "mu", "zet", "s")


def _max_step(ap: _Approx, v: dict, dv: dict) -> float:
    """The largest step (fraction 1/1.01 to the boundary) keeping every variable interior."""
    worst = 1.0
    for k in _POSITIVE:
        ratio = -1.01 * np.atleast_1d(dv[k]) / np.atleast_1d(v[k])
        worst = max(worst, float(np.max(ratio)))
    worst = max(worst, float(np.max(-1.01 * dv["x"] / (v["x"] - ap.alpha))))
    worst = max(worst, float(np.max(1.01 * dv["x"] / (ap.beta - v["x"]))))
    return 1.0 / worst


def solve_subproblem(ap: _Approx, epsimin: float = 1e-7, max_newton: int = 200) -> Subsolution:
    """Solve the MMA subproblem by the primal-dual interior-point method."""
    m = ap.b.size
    x = 0.5 * (ap.alpha + ap.beta)
    v = {
        "x": x,
        "y": np.ones(m),
        "z": 1.0,
        "lam": np.ones(m),
        "xsi": np.maximum(1.0, 1.0 / (x - ap.alpha)),
        "eta": np.maximum(1.0, 1.0 / (ap.beta - x)),
        "mu": np.maximum(1.0, 0.5 * ap.c),
        "zet": 1.0,
        "s": np.ones(m),
    }
    eps = 1.0
    steps = 0
    while eps > 0.999 * epsimin:
        res, r = _residual(ap, v, eps)
        norm = float(np.linalg.norm(res))
        inner = 0
        while float(np.max(np.abs(res))) > 0.9 * eps and inner < max_newton:
            inner += 1
            steps += 1
            dv = _newton_direction(ap, v, r)
            step = _max_step(ap, v, dv)
            for _ in range(60):
                trial = {k: v[k] + step * dv[k] for k in v}
                res_t, r_t = _residual(ap, trial, eps)
                norm_t = float(np.linalg.norm(res_t))
                if norm_t <= norm:
                    break
                step *= 0.5
            v, res, r, norm = trial, res_t, r_t, norm_t
        eps *= 0.1
    return Subsolution(
        x=v["x"],
        y=v["y"],
        z=float(v["z"]),
        lam=v["lam"],
        xsi=v["xsi"],
        eta=v["eta"],
        mu=v["mu"],
        zet=float(v["zet"]),
        s=v["s"],
        newton_steps=steps,
    )


@dataclass
class MMA:
    """MMA for n variables in [xmin, xmax] and m constraints (Svanberg's general form)."""

    n: int
    m: int
    xmin: np.ndarray
    xmax: np.ndarray
    a0: float = 1.0
    a: np.ndarray | None = None
    c: np.ndarray | None = None
    d: np.ndarray | None = None
    settings: MMASettings = field(default_factory=MMASettings)

    def __post_init__(self) -> None:
        self.xmin = np.broadcast_to(np.asarray(self.xmin, np.float64), (self.n,)).copy()
        self.xmax = np.broadcast_to(np.asarray(self.xmax, np.float64), (self.n,)).copy()
        self.a = np.zeros(self.m) if self.a is None else np.asarray(self.a, np.float64)
        self.c = np.full(self.m, 1e3) if self.c is None else np.asarray(self.c, np.float64)
        self.d = np.ones(self.m) if self.d is None else np.asarray(self.d, np.float64)

    def _asymptotes(self, x: np.ndarray, state: MMAState):
        st = self.settings
        span = self.xmax - self.xmin
        if state.k < 2 or state.low is None or state.xold2 is None:
            return x - st.asyinit * span, x + st.asyinit * span
        trend = (x - state.xold1) * (state.xold1 - state.xold2)
        factor = np.ones(self.n)
        factor[trend > 0] = st.asyincr
        factor[trend < 0] = st.asydecr
        low = x - factor * (state.xold1 - state.low)
        upp = x + factor * (state.upp - state.xold1)
        low = np.clip(low, x - 10.0 * span, x - 0.01 * span)
        upp = np.clip(upp, x + 0.01 * span, x + 10.0 * span)
        return low, upp

    def approximation(
        self, x, df0, fval, dfdx, state: MMAState, move: float | None = None, rho=None
    ):
        """The subproblem data at x_k. `rho` ((m + 1,), optional) replaces raa0 per function
        (row 0: f_0) — the curvature parameters of the conservative variant."""
        st = self.settings
        mv = st.move if move is None else move
        span = self.xmax - self.xmin
        low, upp = self._asymptotes(x, state)
        alpha = np.maximum.reduce([self.xmin, low + st.albefa * (x - low), x - mv * span])
        beta = np.minimum.reduce([self.xmax, upp - st.albefa * (upp - x), x + mv * span])
        ux2 = (upp - x) ** 2
        xl2 = (x - low) ** 2
        if rho is None:
            rho = np.full(self.m + 1, st.raa0)
        rho = np.asarray(rho, np.float64)
        span_eps = np.maximum(span, 1e-5)
        reg0 = rho[0] / span_eps
        reg = rho[1:, None] / span_eps[None, :]
        df0 = np.asarray(df0, np.float64)
        dfdx = np.asarray(dfdx, np.float64).reshape(self.m, self.n)
        pos0, neg0 = np.maximum(df0, 0.0), np.maximum(-df0, 0.0)
        pos, neg = np.maximum(dfdx, 0.0), np.maximum(-dfdx, 0.0)
        p0 = (1.001 * pos0 + 0.001 * neg0 + reg0) * ux2
        q0 = (0.001 * pos0 + 1.001 * neg0 + reg0) * xl2
        P = (1.001 * pos + 0.001 * neg + reg) * ux2
        Q = (0.001 * pos + 1.001 * neg + reg) * xl2
        b = P @ (1.0 / (upp - x)) + Q @ (1.0 / (x - low)) - np.asarray(fval, np.float64)
        return _Approx(low, upp, alpha, beta, p0, q0, P, Q, b, self.a0, self.a, self.c, self.d)

    @staticmethod
    def approx_values(ap: _Approx, x: np.ndarray) -> np.ndarray:
        """f̃_i(x) for the constraints i = 1..m (f̃_i(x_k) = f_i(x_k))."""
        return ap.P @ (1.0 / (ap.upp - x)) + ap.Q @ (1.0 / (x - ap.low)) - ap.b

    def step(self, x, f0, df0, fval, dfdx, state: MMAState, move: float | None = None):
        """One MMA iteration from x_k with objective and constraint values and gradients.

        Returns (x_{k+1}, the subproblem solution, the new state). `f0` is unused by the
        update (only its gradient enters) and accepted for symmetry with the papers.
        """
        del f0
        x = np.asarray(x, np.float64)
        ap = self.approximation(x, df0, fval, dfdx, state, move)
        sol = solve_subproblem(ap, self.settings.epsimin, self.settings.max_newton)
        new = MMAState(
            k=state.k + 1,
            xold1=x.copy(),
            xold2=None if state.xold1 is None else state.xold1.copy(),
            low=ap.low,
            upp=ap.upp,
        )
        return sol.x, sol, new

    def conservative_step(
        self,
        x,
        df0,
        fval,
        dfdx,
        state: MMAState,
        evaluate,
        move: float | None = None,
        max_inner: int = 10,
    ):
        """One outer iteration of the globally convergent variant (CCSA/GCMMA, Svanberg 2002,
        2007) for f_0 with zero gradient (the epigraph form, where f̃_0 is conservative by
        construction).

        The curvature parameters start at ρ_i = max(1e-6, 0.1/n Σ_j |∂f_i/∂x_j| (x_max −
        x_min)_j). After each subproblem solve, `evaluate(x̂)` returns the true constraint
        values; every constraint whose approximation is not conservative, f_i(x̂) > f̃_i(x̂),
        gets ρ_i ← min(1.1 (ρ_i + δ_i), 10 ρ_i) with δ_i = (f_i(x̂) − f̃_i(x̂)) / d(x̂) and
        d(x) = Σ_j (u_j − l_j)(x_j − x_kj)² / ((u_j − x_j)(x_j − l_j)(x_max − x_min)_j),
        and the subproblem is solved again.

        Only a conservative point is accepted, so the maximum constraint (the epigraph value)
        never increases up to the elastic variables. When `max_inner` subproblems all fail, the
        step is rejected: x_k is returned unchanged with its own values, and the returned state
        keeps x_k's asymptotes and the raised ρ, so the next call at x_k (with the same values
        and gradients) continues the inner iterations where this one stopped.

        Returns (x_{k+1}, the solution, the new state, the true values at x_{k+1}, the number
        of inner iterations, whether the step was accepted).
        """
        x = np.asarray(x, np.float64)
        fval = np.asarray(fval, np.float64)
        dfdx = np.asarray(dfdx, np.float64).reshape(self.m, self.n)
        span = self.xmax - self.xmin
        rho = np.empty(self.m + 1)
        rho[0] = 1e-6
        rho[1:] = np.maximum(1e-6, 0.1 / self.n * np.abs(dfdx) @ span)
        if state.rho is not None and np.asarray(state.rho).shape == rho.shape:
            rho = np.maximum(rho, np.asarray(state.rho, np.float64))
        conservative = False
        inner = 0
        for inner in range(1, max_inner + 1):
            ap = self.approximation(x, df0, fval, dfdx, state, move, rho)
            sol = solve_subproblem(ap, self.settings.epsimin, self.settings.max_newton)
            true = np.asarray(evaluate(sol.x), np.float64)
            approx = self.approx_values(ap, sol.x)
            gap = true - approx
            bad = gap > 1e-10 * (1.0 + np.abs(true))
            if not bad.any():
                conservative = True
                break
            dx2 = (sol.x - x) ** 2
            d = float(
                np.sum((ap.upp - ap.low) * dx2 / ((ap.upp - sol.x) * (sol.x - ap.low) * span))
            )
            d = max(d, 1e-300)
            delta = gap / d
            grown = np.minimum(1.1 * (rho[1:] + delta), 10.0 * rho[1:])
            rho[1:] = np.where(bad, grown, rho[1:])
        if not conservative:
            kept = MMAState(
                k=state.k,
                xold1=state.xold1,
                xold2=state.xold2,
                low=state.low,
                upp=state.upp,
                rho=rho.copy(),
            )
            return x.copy(), sol, kept, fval.copy(), inner, False
        new = MMAState(
            k=state.k + 1,
            xold1=x.copy(),
            xold2=None if state.xold1 is None else state.xold1.copy(),
            low=ap.low,
            upp=ap.upp,
        )
        return sol.x, sol, new, true, inner, True

    def kkt_residual(self, sol: Subsolution, df0, fval, dfdx) -> float:
        """Norm of the KKT residual of the original problem at sol.x, with the subproblem's
        multipliers and with f and its gradients evaluated at sol.x (Svanberg 2007, §6)."""
        x = sol.x
        dfdx = np.asarray(dfdx, np.float64).reshape(self.m, self.n)
        r = [
            np.asarray(df0) + dfdx.T @ sol.lam - sol.xsi + sol.eta,
            self.c + self.d * sol.y - sol.mu - sol.lam,
            [self.a0 - sol.zet - self.a @ sol.lam],
            np.asarray(fval) - self.a * sol.z - sol.y + sol.s,
            sol.xsi * (x - self.xmin),
            sol.eta * (self.xmax - x),
            sol.mu * sol.y,
            [sol.zet * sol.z],
            sol.lam * sol.s,
        ]
        return float(np.linalg.norm(np.concatenate([np.atleast_1d(v) for v in r])))
