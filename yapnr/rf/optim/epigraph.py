"""The epigraph minimax problem in MMA's native form (design §8.1, §8.2; Hammond et al. Eq. 2).

    min_{x, t}  t
    s.t.  f_k(x) ≤ t      every objective group and frequency k
          g_l(x) ≤ 0      length-scale constraints (last β epoch)
          0 ≤ x ≤ 1

maps onto Svanberg's form with f_0 = 0, a_0 = 1, the objectives shifted by C so that they are
positive (a_k = 1: f_k + C − z − y_k ≤ 0, so z is the epigraph variable and t = z − C) and the
length-scale constraints with a_l = 0. The elastic variables y carry a large linear cost
c = 1e3 and d = 1, so they are zero whenever the subproblem is feasible.

C defaults to 10 and grows when an objective falls below −C + 1 (met by a large margin), so
z ≥ 0 never binds. MMA solves for z afresh in every subproblem, so changing C between
iterations is harmless.

With `evaluate` (x → (f, g), the true values without gradients), the step is the conservative
variant (CCSA/GCMMA, `MMA.conservative_step`), as in NLopt's MMA that Hammond et al. use: inner
iterations raise the curvature of non-conservative approximations until the new point's true
values lie below them, so the epigraph value does not increase from one iteration to the next.
Each inner iteration costs one forward simulation per excitation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.optim.mma import MMA, MMASettings, MMAState


@dataclass
class EpigraphStep:
    """The result of one epigraph update."""

    x: np.ndarray
    t: float  # max_k f_k(x_k) (the epigraph value at the evaluated point)
    shift: float
    newton_steps: int
    state: MMAState
    inner: int = 0  # conservative variant: subproblems solved (and points evaluated)
    conservative: bool = True
    t_new: float | None = None  # conservative variant: max_k f_k(x_{k+1})


@dataclass
class Epigraph:
    """min_x max_k f_k(x) s.t. g_l(x) ≤ 0, x ∈ [0, 1]^n, by MMA."""

    n: int
    settings: MMASettings = field(default_factory=MMASettings)
    shift: float = 10.0
    c: float = 1e3
    d: float = 1.0

    def step(
        self,
        x: np.ndarray,
        f: np.ndarray,
        df: np.ndarray,
        state: MMAState,
        g: np.ndarray | None = None,
        dg: np.ndarray | None = None,
        *,
        move: float | None = None,
        xmin=0.0,
        xmax=1.0,
        evaluate=None,
        max_inner: int = 10,
    ) -> EpigraphStep:
        f = np.atleast_1d(np.asarray(f, np.float64))
        df = np.asarray(df, np.float64).reshape(f.size, self.n)
        g = np.zeros(0) if g is None else np.atleast_1d(np.asarray(g, np.float64))
        dg = np.zeros((0, self.n)) if dg is None else np.asarray(dg, np.float64).reshape(-1, self.n)
        shift = max(self.shift, 1.0 - float(f.min()))
        k, nl = f.size, g.size
        mma = MMA(
            n=self.n,
            m=k + nl,
            xmin=xmin,
            xmax=xmax,
            a0=1.0,
            a=np.concatenate([np.ones(k), np.zeros(nl)]),
            c=np.full(k + nl, self.c),
            d=np.full(k + nl, self.d),
            settings=self.settings,
        )
        fval = np.concatenate([f + shift, g])
        dfdx = np.concatenate([df, dg], axis=0)
        if evaluate is None:
            x_new, sol, new_state = mma.step(x, 0.0, np.zeros(self.n), fval, dfdx, state, move=move)
            return EpigraphStep(
                x=x_new,
                t=float(f.max()),
                shift=shift,
                newton_steps=sol.newton_steps,
                state=new_state,
            )

        def true_values(xh):
            fh, gh = evaluate(xh)
            gh = np.zeros(0) if nl == 0 else np.atleast_1d(np.asarray(gh, np.float64))
            return np.concatenate([np.asarray(fh, np.float64) + shift, gh])

        x_new, sol, new_state, true, inner, ok = mma.conservative_step(
            x, np.zeros(self.n), fval, dfdx, state, true_values, move=move, max_inner=max_inner
        )
        return EpigraphStep(
            x=x_new,
            t=float(f.max()),
            shift=shift,
            newton_steps=sol.newton_steps,
            state=new_state,
            inner=inner,
            conservative=ok,
            t_new=float(np.max(true[:k])) - shift,
        )
