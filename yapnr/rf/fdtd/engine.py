"""The Yee stepper with CPML, Crank–Nicolson conductivity, sources and DTFT probes (design §4).

One step (n → n + 1):

    H^{n+½} = H^{n−½} − (Δt/μ0) (curl E^n + K^n)            (CPML ψ terms in the slabs)
    E^{n+1} = Ca E^n + Cb (curl H^{n+½} − J^{n+½})          (interior, non-PEC edges)

with Ca, Cb from `Structure.coefficients`. The outer boundary and the ground plane are PEC:
tangential E on them is never updated. H probes are accumulated at t = (n + ½)Δt after the H
update, E probes at t = (n + 1)Δt after the E update.

Backends: "numpy" (reference) and "torch" (CPU, intra-op threads), float64 or float32 fields.
The code path is shared; only a handful of array primitives differ (`_NumpyOps`, `_TorchOps`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.constants import MU0
from yapnr.rf.fdtd.cpml import CPMLParams, profile
from yapnr.rf.fdtd.dtft import Accumulator
from yapnr.rf.fdtd.monitors import Probe
from yapnr.rf.fdtd.stop import StopRule, relative_change
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import E_COMPONENTS, H_COMPONENTS, Grid, staggered

# Curl terms: component → [(sign, source component, derivative axis)], first sign positive.
_E_TERMS = {
    "ex": [(1, "hz", 1), (-1, "hy", 2)],
    "ey": [(1, "hx", 2), (-1, "hz", 0)],
    "ez": [(1, "hy", 0), (-1, "hx", 1)],
}
_H_TERMS = {
    "hx": [(1, "ez", 1), (-1, "ey", 2)],
    "hy": [(1, "ex", 2), (-1, "ez", 0)],
    "hz": [(1, "ey", 0), (-1, "ex", 1)],
}


class _NumpyOps:
    name = "numpy"

    def __init__(self, dtype):
        self.dtype = np.dtype(dtype)

    def zeros(self, shape):
        return np.zeros(shape, self.dtype)

    def array(self, a):
        return np.ascontiguousarray(np.asarray(a, dtype=self.dtype))

    def index(self, idx):
        return np.asarray(idx, dtype=np.int64)

    def take(self, a, idx):
        return a.reshape(-1)[idx].astype(np.float64)

    def sub_at(self, a, idx, vals):
        flat = a.reshape(-1)
        flat[idx] -= vals.astype(self.dtype, copy=False)

    @staticmethod
    def diff(a, axis):
        if axis == 0:
            return a[1:] - a[:-1]
        if axis == 1:
            return a[:, 1:] - a[:, :-1]
        return a[:, :, 1:] - a[:, :, :-1]

    @staticmethod
    def mul(x, w):
        return x * w

    @staticmethod
    def addmul_(out, x, w, sign):
        if sign > 0:
            out += x * w
        else:
            out -= x * w

    @staticmethod
    def add_(out, x, sign):
        if sign > 0:
            out += x
        else:
            out -= x

    @staticmethod
    def axpby_(y, a, b, x):
        """y ← a·y + b·x."""
        y *= a
        y += b * x

    @staticmethod
    def to_numpy(a):
        return np.asarray(a)


class _TorchOps:
    name = "torch"

    def __init__(self, dtype):
        import torch

        self.torch = torch
        self.dtype = {np.dtype(np.float32): torch.float32, np.dtype(np.float64): torch.float64}[
            np.dtype(dtype)
        ]

    def zeros(self, shape):
        return self.torch.zeros(shape, dtype=self.dtype)

    def array(self, a):
        return self.torch.tensor(np.ascontiguousarray(a), dtype=self.dtype)

    def index(self, idx):
        return self.torch.as_tensor(np.asarray(idx, dtype=np.int64))

    def take(self, a, idx):
        return a.view(-1).index_select(0, idx).to(self.torch.float64).numpy()

    def sub_at(self, a, idx, vals):
        a.view(-1).index_add_(0, idx, self.torch.as_tensor(-np.asarray(vals), dtype=self.dtype))

    def diff(self, a, axis):
        return self.torch.diff(a, dim=axis)

    def mul(self, x, w):
        return self.torch.mul(x, w)

    @staticmethod
    def addmul_(out, x, w, sign):
        out.addcmul_(x, w, value=float(sign))

    @staticmethod
    def add_(out, x, sign):
        out.add_(x, alpha=float(sign))

    @staticmethod
    def axpby_(y, a, b, x):
        y.mul_(a).addcmul_(b, x)

    @staticmethod
    def to_numpy(a):
        return a.detach().cpu().numpy()


def make_ops(backend: str, dtype):
    if backend == "numpy":
        return _NumpyOps(dtype)
    if backend == "torch":
        return _TorchOps(dtype)
    raise ValueError(f"unknown backend {backend!r}")


@dataclass
class _Slab:
    sl: tuple
    b: object
    cd: object
    psi: object


@dataclass
class _Term:
    sign: int
    src: str
    axis: int
    dsl: tuple
    ik: object
    slabs: list


@dataclass
class RunResult:
    """DTFTs of every probe (name → complex (M, P)) and run statistics."""

    dft: dict
    omega: np.ndarray
    dt: float
    steps: int
    converged: bool
    wall_s: float
    decimation: int
    history: list = field(default_factory=list)


class Simulation:
    """A microstrip FDTD model ready to run: grid, materials, CPML and the time step."""

    def __init__(
        self,
        grid: Grid,
        structure: Structure,
        *,
        dt: float | None = None,
        courant: float = 0.95,
        cpml: CPMLParams = CPMLParams(),
        backend: str = "numpy",
        dtype=np.float64,
        threads: int = 4,
    ):
        self.grid = grid
        self.structure = structure
        self.cpml = cpml
        self.dt = float(dt) if dt is not None else courant * grid.courant_dt()
        self.ops = make_ops(backend, dtype)
        self.backend = backend
        self.dtype = np.dtype(dtype)
        if backend == "torch":
            import torch

            torch.set_num_threads(max(1, min(4, int(threads))))
        self._interior = {
            c: tuple(slice(None) if staggered(c, a) else slice(1, -1) for a in range(3))
            for c in E_COMPONENTS
        }
        self._build_terms()
        self.update_materials()
        self.reset()

    # -- setup ----------------------------------------------------------------------------------

    def _bshape(self, v: np.ndarray, axis: int):
        shape = [1, 1, 1]
        shape[axis] = v.size
        return self.ops.array(v.reshape(shape))

    def _build_terms(self) -> None:
        grid, dt, ops = self.grid, self.dt, self.ops
        self.terms: dict[str, list[_Term]] = {}
        for comp, spec in list(_E_TERMS.items()) + list(_H_TERMS.items()):
            electric = comp[0] == "e"
            terms = []
            full_shape = grid.shape(comp)
            for sign, src, u in spec:
                ax = grid.axis(u)
                n_lo, n_hi = grid.pml.along(u)
                if electric:
                    # Derivative at interior nodes 1..N−1 along u, dual lengths.
                    pos = ax.nodes[1:-1]
                    length = ax.dual[1:-1]
                    scale = 1.0
                    dsl = tuple(
                        slice(None) if a == u else self._interior[comp][a] for a in range(3)
                    )
                    shape = [
                        len(range(*self._interior[comp][a].indices(full_shape[a])))
                        for a in range(3)
                    ]
                else:
                    pos = ax.centers
                    length = ax.primary
                    scale = dt / MU0
                    dsl = (slice(None),) * 3
                    shape = list(full_shape)
                prof = profile(ax, n_lo, n_hi, pos, dt, self.cpml)
                ik = scale / (prof.kappa * length)
                slabs = []
                for s0, s1 in prof.slabs():
                    sl = tuple(slice(s0, s1) if a == u else slice(None) for a in range(3))
                    pshape = list(shape)
                    pshape[u] = s1 - s0
                    slabs.append(
                        _Slab(
                            sl=sl,
                            b=self._bshape(prof.b[s0:s1], u),
                            cd=self._bshape(scale * prof.c[s0:s1] / length[s0:s1], u),
                            psi=ops.zeros(tuple(pshape)),
                        )
                    )
                terms.append(_Term(sign, src, u, dsl, self._bshape(ik, u), slabs))
            self.terms[comp] = terms

    def update_materials(self) -> None:
        """Recompute the update coefficients from `structure` (after changing the sheet)."""
        self._ca = {}
        self._cb = {}
        self._cb_full = {}
        for comp in E_COMPONENTS:
            ca, cb = self.structure.coefficients(comp, self.dt)
            sl = self._interior[comp]
            self._ca[comp] = self.ops.array(ca[sl])
            self._cb[comp] = self.ops.array(cb[sl])
            self._cb_full[comp] = cb  # float64, for source scaling

    def reset(self) -> None:
        """Zero all fields and CPML auxiliary arrays."""
        self.f = {c: self.ops.zeros(self.grid.shape(c)) for c in E_COMPONENTS + H_COMPONENTS}
        self._fint = {c: self.f[c][self._interior[c]] for c in E_COMPONENTS}
        for terms in self.terms.values():
            for t in terms:
                for s in t.slabs:
                    s.psi = s.psi * 0
        self.n = 0

    # -- stepping -------------------------------------------------------------------------------

    def _step_h(self) -> None:
        ops, f = self.ops, self.f
        for comp in H_COMPONENTS:
            out = f[comp]
            for t in self.terms[comp]:
                d = ops.diff(f[t.src], t.axis)
                ops.addmul_(out, d, t.ik, -t.sign)
                for s in t.slabs:
                    ops.axpby_(s.psi, s.b, s.cd, d[s.sl])
                    ops.add_(out[s.sl], s.psi, -t.sign)

    def _step_e(self) -> None:
        ops, f = self.ops, self.f
        for comp in E_COMPONENTS:
            curl = None
            for t in self.terms[comp]:
                d = ops.diff(f[t.src], t.axis)[t.dsl]
                if curl is None:
                    curl = ops.mul(d, t.ik)
                else:
                    ops.addmul_(curl, d, t.ik, t.sign)
                for s in t.slabs:
                    ops.axpby_(s.psi, s.b, s.cd, d[s.sl])
                    ops.add_(curl[s.sl], s.psi, t.sign)
            ops.axpby_(self._fint[comp], self._ca[comp], self._cb[comp], curl)

    def field(self, comp: str) -> np.ndarray:
        """A float64 numpy copy of the current values of `comp`."""
        return np.array(self.ops.to_numpy(self.f[comp]), dtype=np.float64)

    # -- running --------------------------------------------------------------------------------

    def run(
        self,
        sources,
        probes: list[Probe],
        omega,
        stop: StopRule,
        *,
        decimation: int = 1,
        callback=None,
    ) -> RunResult:
        """Run from zero fields until `stop` is met; return the probes' DTFTs at `omega`.

        `sources` have `comp`, `index`, `values(n)` (None once ended) and `end_step`.
        `callback(sim, n)` (optional) is called after every step, for time-domain tests.
        """
        ops, dt = self.ops, self.dt
        omega = np.atleast_1d(np.asarray(omega, dtype=np.float64))
        self.reset()
        names = [p.name for p in probes]
        if len(set(names)) != len(names):
            raise ValueError("probe names must be unique")
        acc = {
            p.name: Accumulator(omega, p.index.size, dt, half_step=p.comp[0] == "h") for p in probes
        }
        pidx = {p.name: ops.index(p.index) for p in probes}
        e_probes = [p for p in probes if p.comp[0] == "e"]
        h_probes = [p for p in probes if p.comp[0] == "h"]
        e_src, h_src = [], []
        for s in sources:
            idx = np.asarray(s.index, dtype=np.int64)
            if np.unique(idx).size != idx.size:
                raise ValueError("source edges must be unique")
            if s.comp[0] == "e":
                e_src.append((s, ops.index(idx), self._cb_full[s.comp].reshape(-1)[idx]))
            else:
                h_src.append((s, ops.index(idx), np.full(idx.size, dt / MU0)))
        src_end = max([s.end_step for s in sources], default=0)
        check_names = list(stop.probes) if stop.probes else names
        period = stop.period(dt)
        prev = None
        good = 0
        converged = False
        history = []
        t0 = time.perf_counter()
        n = 0
        while n < stop.max_steps:
            self._step_h()
            for s, idx, scale in h_src:
                v = s.values(n)
                if v is not None:
                    ops.sub_at(self.f[s.comp], idx, scale * v)
            if n % decimation == 0:
                for p in h_probes:
                    acc[p.name].add(ops.take(self.f[p.comp], pidx[p.name]), n, decimation)
            self._step_e()
            for s, idx, scale in e_src:
                v = s.values(n)
                if v is not None:
                    ops.sub_at(self.f[s.comp], idx, scale * v)
            if (n + 1) % decimation == 0:
                for p in e_probes:
                    acc[p.name].add(ops.take(self.f[p.comp], pidx[p.name]), n + 1, decimation)
            n += 1
            self.n = n
            if callback is not None:
                callback(self, n)
            if n > src_end and n >= stop.min_steps and (n - src_end) % period == 0:
                cur = {k: acc[k].value for k in check_names}
                if prev is not None:
                    change = relative_change(cur, prev, check_names)
                    history.append((n, change))
                    good = good + 1 if change < stop.tol else 0
                    if good >= stop.consecutive:
                        converged = True
                        break
                prev = cur
        return RunResult(
            dft={k: a.value for k, a in acc.items()},
            omega=omega,
            dt=dt,
            steps=n,
            converged=converged,
            wall_s=time.perf_counter() - t0,
            decimation=decimation,
            history=history,
        )
