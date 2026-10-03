"""The Yee stepper with CPML, Crank–Nicolson conductivity, sources and DTFT probes (design §4).

One step (n → n + 1):

    H^{n+½} = H^{n−½} − (Δt/μ0) (curl E^n + K^n)            (CPML ψ terms in the slabs)
    E^{n+1} = Ca E^n + Cb (curl H^{n+½} − J^{n+½})          (interior, non-PEC edges)

with Ca, Cb from `Structure.coefficients`. The copper-edge correction (`edges`) scales ε on a
few E planes (in Ca, Cb) and 1/μ on a few H planes (the H update of those planes is scaled
after the fact). An inductive copper sheet (`Structure.set_sheet`)
adds its branch currents: the explicit part Σ w (1 + k) J^n/(2Δz) is subtracted from the curl
before the update and the branch states advance after it (and after the sources), J^{n+1} =
k J^n + (b/2)(E^{n+1} + E^n). The outer boundary and the ground plane are PEC:
tangential E on them is never updated. H probes are accumulated at t = (n + ½)Δt after the H
update, E probes at t = (n + 1)Δt after the E update.

Backends: "numpy" (reference) and "torch" (CPU, intra-op threads), float64 or float32 fields;
their code path is shared and only a handful of array primitives differ (`_NumpyOps`,
`_TorchOps`). "native" (`native_kernel`, optional) runs blocks of steps in C with the numpy
reference's operations in the same order: float64 runs are bit-identical to numpy's.

A run advances in blocks of steps that end where the stop rule checks; the sources' values and
the DTFT phase factors of a block are tabulated once (`_BlockTables`) and every backend uses
the same tables.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.constants import MU0
from yapnr.rf.fdtd.cpml import CPMLParams, profile
from yapnr.rf.fdtd.dtft import Accumulator
from yapnr.rf.fdtd.monitors import Probe
from yapnr.rf.fdtd.sources import combine
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
    def diff_into(a, axis, out):
        hi = [slice(None)] * 3
        lo = [slice(None)] * 3
        hi[axis] = slice(1, None)
        lo[axis] = slice(None, -1)
        np.subtract(a[tuple(hi)], a[tuple(lo)], out=out)
        return out

    @staticmethod
    def mul_into(x, w, out):
        np.multiply(x, w, out=out)
        return out

    @staticmethod
    def slab_view(base, sl, axis, gap):
        v = base[sl]
        if not gap:
            return v
        return np.lib.stride_tricks.as_strided(
            v, shape=(2,) + v.shape, strides=(gap * base.strides[axis],) + v.strides
        )

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
    def copy_into(src, dst):
        np.copyto(dst, src)

    @staticmethod
    def blend_(x, old, m):
        """x ← old + m·(x − old)."""
        x -= old
        x *= m
        x += old

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

    def diff_into(self, a, axis, out):
        n = a.shape[axis]
        return self.torch.sub(a.narrow(axis, 1, n - 1), a.narrow(axis, 0, n - 1), out=out)

    def mul_into(self, x, w, out):
        return self.torch.mul(x, w, out=out)

    def slab_view(self, base, sl, axis, gap):
        v = base[sl]
        if not gap:
            return v
        return self.torch.as_strided(
            v,
            (2,) + tuple(v.shape),
            (gap * base.stride(axis),) + tuple(v.stride()),
            v.storage_offset(),
        )

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
    def copy_into(src, dst):
        dst.copy_(src)

    def blend_(self, x, old, m):
        """x ← old + m·(x − old) (one fused op)."""
        self.torch.lerp(old, x, m, out=x)

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
    """CPML slab(s) of one curl term along its axis.

    When the low and high slabs have equal length they are updated together through one
    strided view with a leading dimension of 2 (`gap` = start of high − start of low), which
    halves the number of small array operations per step.
    """

    sl: tuple
    b: object
    cd: object
    psi: object
    axis: int = 0
    gap: int = 0
    dview: object = None
    oview: object = None


@dataclass
class _Term:
    sign: int
    src: str
    axis: int
    dsl: tuple
    ik: object
    slabs: list
    shape: tuple = ()
    buf: object = None


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
    """A microstrip FDTD model ready to run: grid, materials, CPML and the time step.

    `backend` "numpy" (the reference), "torch" or "native" (`native_kernel`; falls back to
    numpy, saying so once, when its library is missing); None: ``$YAPNR_RF_BACKEND`` or numpy.
    `threads`: torch's intra-op threads (at most 4) or the native pool (``$YAPNR_RF_THREADS``
    overrides it).
    """

    def __init__(
        self,
        grid: Grid,
        structure: Structure,
        *,
        dt: float | None = None,
        courant: float = 0.95,
        cpml: CPMLParams = CPMLParams(),
        backend: str | None = None,
        dtype=np.float64,
        threads: int = 4,
    ):
        self.grid = grid
        self.structure = structure
        self.cpml = cpml
        self.dt = float(dt) if dt is not None else courant * grid.courant_dt()
        if backend is None:
            backend = os.environ.get("YAPNR_RF_BACKEND", "").strip().lower() or "numpy"
        self.requested_backend = backend
        kernel = None
        if backend == "native":
            from yapnr.rf.fdtd import native_kernel

            kernel = native_kernel.require_or_warn()
            if kernel is None:
                backend = "numpy"
        # The native backend builds its coefficients with the numpy primitives (same values).
        self.ops = make_ops("numpy" if backend == "native" else backend, dtype)
        self.backend = backend
        self.dtype = np.dtype(dtype)
        if backend == "torch":
            import torch

            torch.set_num_threads(max(1, min(4, int(threads))))
        self._interior = {
            c: tuple(slice(None) if staggered(c, a) else slice(1, -1) for a in range(3))
            for c in E_COMPONENTS
        }
        self._native = None
        self._build_terms(allocate=kernel is None)
        if kernel is not None:
            from yapnr.rf.fdtd.native_kernel import NativeStepper

            self._native = NativeStepper(self, kernel, threads)
        self.update_materials()
        self.reset()

    @property
    def threads(self) -> int | None:
        """Threads of the native stepper (None for the other backends)."""
        return self._native.threads if self._native is not None else None

    # -- setup ----------------------------------------------------------------------------------

    def _bshape(self, v: np.ndarray, axis: int):
        shape = [1, 1, 1]
        shape[axis] = v.size
        return self.ops.array(v.reshape(shape))

    def _build_terms(self, allocate: bool = True) -> None:
        """Curl terms and their CPML slabs; `allocate` the ψ arrays (the native stepper keeps
        its own)."""
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
                runs = prof.slabs()
                if len(runs) == 2 and runs[0][1] - runs[0][0] == runs[1][1] - runs[1][0]:
                    groups = [(runs[0], runs[1][0] - runs[0][0])]
                else:
                    groups = [(r, 0) for r in runs]
                for (s0, s1), gap in groups:
                    sl = tuple(slice(s0, s1) if a == u else slice(None) for a in range(3))
                    pshape = list(shape)
                    pshape[u] = s1 - s0
                    b = prof.b[s0:s1]
                    cd = scale * prof.c[s0:s1] / length[s0:s1]
                    if gap:
                        b = np.stack([b, prof.b[s0 + gap : s1 + gap]])
                        cd = np.stack(
                            [cd, scale * prof.c[s0 + gap : s1 + gap] / length[s0 + gap : s1 + gap]]
                        )
                        bs = [2, 1, 1, 1]
                        bs[u + 1] = s1 - s0
                        b = ops.array(b.reshape(bs))
                        cd = ops.array(cd.reshape(bs))
                        pshape = [2] + pshape
                    else:
                        b = self._bshape(b, u)
                        cd = self._bshape(cd, u)
                    psi = ops.zeros(tuple(pshape)) if allocate else None
                    slabs.append(_Slab(sl, b, cd, psi, u, gap))
                terms.append(
                    _Term(sign, src, u, dsl, self._bshape(ik, u), slabs, shape=tuple(shape))
                )
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
        # Copper-edge correction (`edges`): 1/μr on a few H planes around the copper plane.
        # The H update of those planes (with its sources) is scaled after the fact,
        # H ← H_old + (1/μr)(H − H_old), which is the update with μ = μ0 μr there.
        self._mu = []
        for comp in H_COMPONENTS:
            m = self.structure.mu_factor(comp)
            if m is None:
                continue
            from yapnr.rf.edges import planes

            ks = planes(self.grid, comp)
            self._mu.append((comp, ks[0], ks[-1] + 1, self.ops.array(m), self.ops.zeros(m.shape)))
        self._sheet = {}
        st = self.structure
        if st.inductive:
            if abs(st.sheet_dt - self.dt) > 1e-9 * self.dt:
                raise ValueError("the inductive sheet was set for another time step")
            for comp in ("ex", "ey"):
                br = st.sheet_branches(comp)
                self._sheet[comp] = {
                    name: tuple(self.ops.array(a) for a in arrs) for name, arrs in br.items()
                }
        if self._native is not None:
            self._native.set_materials(
                self._ca, self._cb, [(c, k0, k1, m) for c, k0, k1, m, _ in self._mu], self._sheet
            )
            # The stepper holds per-plane copies (scalars for uniform planes).
            self._ca, self._cb = {}, {}
        self._reset_sheet()

    def _reset_sheet(self) -> None:
        kc = self.grid.k_c
        self._sheet_state = {}
        for comp, sh in self._sheet.items():
            shape = tuple(sh["k"][0].shape)
            self._sheet_state[comp] = {
                "j": (self.ops.zeros(shape), self.ops.zeros(shape)),
                "buf": self.ops.zeros(shape),
                "esum": self.ops.zeros(shape),
                "plane": kc - 1,
            }

    def reset(self) -> None:
        """Zero all fields and CPML auxiliary arrays."""
        if self._native is not None:
            self._native.reset()
            # Views of the native buffers in the (x, y, z) shapes of the other backends.
            self.f = dict(self._native.views)
            self.n = 0
            return
        self.f = {c: self.ops.zeros(self.grid.shape(c)) for c in E_COMPONENTS + H_COMPONENTS}
        self._fint = {c: self.f[c][self._interior[c]] for c in E_COMPONENTS}
        self._curl = {}
        for comp, terms in self.terms.items():
            if comp[0] == "e":
                self._curl[comp] = self.ops.zeros(terms[0].shape)
            out = self._curl[comp] if comp[0] == "e" else self.f[comp]
            for t in terms:
                t.buf = self.ops.zeros(t.shape)
                for s in t.slabs:
                    s.psi = s.psi * 0
                    s.dview = self.ops.slab_view(t.buf, s.sl, s.axis, s.gap)
                    s.oview = self.ops.slab_view(out, s.sl, s.axis, s.gap)
        if hasattr(self, "_sheet"):
            self._reset_sheet()
        self.n = 0

    # -- stepping -------------------------------------------------------------------------------

    def _step_h(self) -> None:
        ops, f = self.ops, self.f
        for comp in H_COMPONENTS:
            out = f[comp]
            for t in self.terms[comp]:
                d = ops.diff_into(f[t.src], t.axis, t.buf)
                ops.addmul_(out, d, t.ik, -t.sign)
                for s in t.slabs:
                    ops.axpby_(s.psi, s.b, s.cd, s.dview)
                    ops.add_(s.oview, s.psi, -t.sign)

    def _step_e(self) -> None:
        ops, f = self.ops, self.f
        for comp in E_COMPONENTS:
            curl = self._curl[comp]
            for n, t in enumerate(self.terms[comp]):
                # The derivative along t.axis of the source restricted to the interior rows of
                # the other axes: diff(f)[dsl] without computing the unused rows.
                d = ops.diff_into(f[t.src][t.dsl], t.axis, t.buf)
                if n == 0:
                    ops.mul_into(d, t.ik, curl)
                else:
                    ops.addmul_(curl, d, t.ik, t.sign)
                for s in t.slabs:
                    ops.axpby_(s.psi, s.b, s.cd, s.dview)
                    ops.add_(s.oview, s.psi, t.sign)
            sh = self._sheet.get(comp)
            if sh is not None:
                state = self._sheet_state[comp]
                k = state["plane"]
                jlo, jhi = state["j"]
                buf = state["buf"]
                ops.mul_into(jlo, sh["c1"][0], buf)
                ops.addmul_(buf, jhi, sh["c1"][1], 1)
                ops.add_(curl[:, :, k], buf, -1)
                ops.copy_into(self._fint[comp][:, :, k], state["esum"])  # E^n
            ops.axpby_(self._fint[comp], self._ca[comp], self._cb[comp], curl)

    def _step_sheet(self) -> None:
        """Advance the inductive sheet's branch currents (after the E update and sources)."""
        ops = self.ops
        for comp, sh in self._sheet.items():
            state = self._sheet_state[comp]
            esum = state["esum"]
            ops.add_(esum, self._fint[comp][:, :, state["plane"]], 1)  # E^n + E^{n+1}
            jlo, jhi = state["j"]
            ops.axpby_(jlo, sh["k"][0], sh["bh"][0], esum)
            ops.axpby_(jhi, sh["k"][1], sh["bh"][1], esum)

    def advance(self, steps: int) -> None:
        """Step the current fields `steps` times without sources or probes (tests: the same
        kernels as `run`, from a state set through `f`)."""
        steps = int(steps)
        if steps <= 0:
            return
        omega = np.zeros(1)
        if self._native is not None:
            stepper = self._native.start([], [], omega.size, 1)
        else:
            stepper = _PythonRun(self, [], [], {}, 1)
        n = self.n
        while n < self.n + steps:
            n1 = min(n + _MAX_BLOCK, self.n + steps)
            stepper.block(n, n1, _BlockTables([], omega, self.dt, 1, n, n1))
            n = n1
        self.n = n

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

        `sources` have `comp`, `index`, `values(n)` (None once ended) and `end_step`, and
        optionally `separable()` (`sources`), which tabulates a block of steps at once.
        `callback(sim, n)` (optional) is called after every step, for time-domain tests.
        """
        dt = self.dt
        omega = np.atleast_1d(np.asarray(omega, dtype=np.float64))
        decimation = int(decimation)
        if decimation < 1:
            raise ValueError("decimation must be >= 1")
        self.reset()
        names = [p.name for p in probes]
        if len(set(names)) != len(names):
            raise ValueError("probe names must be unique")
        acc = {
            p.name: Accumulator(omega, p.index.size, dt, half_step=p.comp[0] == "h") for p in probes
        }
        plan = []
        for s in sources:
            idx = np.asarray(s.index, dtype=np.int64)
            if np.unique(idx).size != idx.size:
                raise ValueError("source edges must be unique")
            if s.comp[0] == "e":
                scale = self._cb_full[s.comp].reshape(-1)[idx]
            else:
                scale = np.full(idx.size, dt / MU0)
            plan.append(_SourcePlan.of(s, idx, scale))
        src_end = max([s.end_step for s in sources], default=0)
        check_names = list(stop.probes) if stop.probes else names
        period = stop.period(dt)
        prev = None
        good = 0
        converged = False
        history = []
        if self._native is not None:
            stepper = self._native.start(
                plan, [(p, acc[p.name]) for p in probes], omega.size, decimation
            )
        else:
            stepper = _PythonRun(self, plan, probes, acc, decimation)
        t0 = time.perf_counter()
        n = 0
        while n < stop.max_steps:
            # Steps up to the next stop check (or a callback after every step).
            n1 = min(_next_check(n, src_end, period, stop.min_steps), stop.max_steps)
            n1 = n + 1 if callback is not None else min(n1, n + _MAX_BLOCK)
            tables = _BlockTables(plan, omega, dt, decimation, n, n1)
            stepper.block(n, n1, tables)
            n = n1
            self.n = n
            if callback is not None:
                callback(self, n)
            if n > src_end and n >= stop.min_steps and (n - src_end) % period == 0:
                cur = {k: acc[k].value for k in check_names}
                if prev is not None:
                    change = relative_change(cur, prev, check_names)
                    if not math.isfinite(change):
                        raise FloatingPointError(
                            f"the run diverged at step {n} (time step above the stable limit?)"
                        )
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


# Longest block of steps between two looks at the stop rule (bounds the source tables).
_MAX_BLOCK = 512


def _next_check(n: int, src_end: int, period: int, min_steps: int) -> int:
    """The first step count m > n at which `Simulation.run` checks the stop rule: m > src_end,
    m >= min_steps and (m − src_end) a multiple of `period`."""
    lo = max(n + 1, src_end + 1, min_steps)
    return src_end + -(-(lo - src_end) // period) * period


@dataclass
class _SourcePlan:
    """A source as the run applies it: F[index] −= scale · v_n, with v_n = Σ_k amp[k] w_n[k]
    (`sources.combine`, k in order) for separable sources, else the source's own values(n)."""

    source: object
    comp: str
    index: np.ndarray
    scale: np.ndarray
    amp: np.ndarray | None
    weights: object
    handle: object = None

    @classmethod
    def of(cls, s, index, scale) -> "_SourcePlan":
        sep = getattr(s, "separable", None)
        if sep is not None:
            amp, weights = sep()
            amp = np.ascontiguousarray(amp, dtype=np.float64)
            if amp.shape != (amp.shape[0], index.size) or amp.shape[0] < 1:
                raise ValueError("separable source: amplitudes must be (K, edges)")
        else:
            amp = None

            def weights(n0, n1, s=s, size=index.size):
                table = np.zeros((n1 - n0, size))
                act = np.zeros(n1 - n0, dtype=bool)
                for b, n in enumerate(range(n0, n1)):
                    v = s.values(n)
                    if v is not None:
                        table[b] = v
                        act[b] = True
                return table, act

        return cls(s, s.comp, index, np.asarray(scale, dtype=np.float64), amp, weights)


def _phases(omega: np.ndarray, dt: float, decimation: int, samples, half: bool):
    """`Accumulator.add`'s weights w cos(ωt), w sin(ωt) (rows: samples), w = dΔt."""
    t = (np.asarray(samples, dtype=np.float64) + (0.5 if half else 0.0)) * dt
    w = decimation * dt
    ph = omega[None, :] * t[:, None]
    return w * np.cos(ph), w * np.sin(ph)


class _BlockTables:
    """Everything of steps n0 .. n1 − 1 that does not depend on the fields: per source the
    weights (B, K) (or values (B, P)) and whether it is on, and the DTFT phase factors of the
    H samples (steps n with n % d = 0, sample n) and E samples ((n + 1) % d = 0, sample n + 1).
    Every backend uses these tables, so they apply bit-identical values."""

    def __init__(self, plan, omega, dt, decimation, n0, n1):
        self.n0, self.n1 = n0, n1
        self.sources = [sp.weights(n0, n1) for sp in plan]
        steps = np.arange(n0, n1)
        h = steps[steps % decimation == 0]
        e = steps[(steps + 1) % decimation == 0] + 1
        self.phases = _phases(omega, dt, decimation, h, True) + _phases(
            omega, dt, decimation, e, False
        )


class _PythonRun:
    """The numpy/torch steps of a run (the reference)."""

    def __init__(self, sim: Simulation, plan, probes, acc, decimation: int):
        self.sim = sim
        self.dec = decimation
        ops = sim.ops
        for sp in plan:
            sp.handle = ops.index(sp.index)
        self.h_src = [(n, sp) for n, sp in enumerate(plan) if sp.comp[0] == "h"]
        self.e_src = [(n, sp) for n, sp in enumerate(plan) if sp.comp[0] == "e"]
        self.plan = plan
        self.h_probes = [(p, ops.index(p.index), acc[p.name]) for p in probes if p.comp[0] == "h"]
        self.e_probes = [(p, ops.index(p.index), acc[p.name]) for p in probes if p.comp[0] == "e"]

    def block(self, n0: int, n1: int, tables: _BlockTables) -> None:
        sim, ops, dec = self.sim, self.sim.ops, self.dec
        f = sim.f
        values = [
            combine(sp.amp, w) if (sp.amp is not None and act.any()) else w
            for sp, (w, act) in zip(self.plan, tables.sources)
        ]
        active = [act for _, act in tables.sources]
        hcos, hsin, ecos, esin = tables.phases
        hs = es = 0
        for n in range(n0, n1):
            b = n - n0
            for comp, k0, k1, _, old in sim._mu:
                ops.copy_into(f[comp][:, :, k0:k1], old)
            sim._step_h()
            for q, sp in self.h_src:
                if active[q][b]:
                    ops.sub_at(f[sp.comp], sp.handle, sp.scale * values[q][b])
            for comp, k0, k1, m, old in sim._mu:
                ops.blend_(f[comp][:, :, k0:k1], old, m)
            if n % dec == 0:
                for p, idx, a in self.h_probes:
                    v = ops.take(f[p.comp], idx)
                    a.re += np.outer(hcos[hs], v)
                    a.im += np.outer(hsin[hs], v)
                hs += 1
            sim._step_e()
            for q, sp in self.e_src:
                if active[q][b]:
                    ops.sub_at(f[sp.comp], sp.handle, sp.scale * values[q][b])
            if sim._sheet:
                sim._step_sheet()
            if (n + 1) % dec == 0:
                for p, idx, a in self.e_probes:
                    v = ops.take(f[p.comp], idx)
                    a.re += np.outer(ecos[es], v)
                    a.im += np.outer(esin[es], v)
                es += 1
