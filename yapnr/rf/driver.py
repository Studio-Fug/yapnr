"""The optimization loop, checkpoints and the end-to-end `design()` (design §8.3, §10).

One iteration at β = β_epoch (design §8.3):

1. x → ρ̄ (`Parameterization`, torch float64);
2. `Problem.evaluate(ρ̄)`: one forward run per excitation, one adjoint run per group, every
   f_{g,m} and ∂f_{g,m}/∂ρ̄;
3. the pull-back to x (vector-Jacobian products) and, in the last epoch, the length-scale
   constraints and their gradients;
4. one MMA step of the epigraph problem (`optim.epigraph`);
5. the record (t, every f, S-parameters, η, φ, M_nd, run lengths, wall times) and a checkpoint.

An epoch ends at its cap or when converged (`optim.schedule`); MMA's state resets at each β.
The run stops after the last epoch or when the wall-clock budget is used up, then binarizes the
design (β = ∞), evaluates it and exports it.

Checkpoints: `checkpoint.npz` (x, the MMA state and the loop state as JSON text), written
atomically after `history.json` and `frames.npz`, so a resumed run continues from the last
completed iteration and reproduces an uninterrupted run bit for bit (same backend, dtype and
thread count).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.design.metrics import gray_measure
from yapnr.rf.optim.epigraph import Epigraph
from yapnr.rf.optim.mma import MMASettings, MMAState
from yapnr.rf.optim.schedule import BetaSchedule
from yapnr.rf.sparams import db

CHECKPOINT = "checkpoint.npz"
HISTORY = "history.json"
FRAMES = "frames.npz"
SPEC = "spec.json"


def schedule_for(spec) -> BetaSchedule:
    o = spec.optimizer
    return BetaSchedule(
        betas=tuple(float(b) for b in o.betas),
        iterations=o.iterations_per_beta,
        min_iterations=o.min_iterations,
        move=o.move,
        move_late=o.move_late,
    )


def _json_safe(v):
    """NaN/inf → None, numpy → Python, recursively."""
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, np.ndarray):
        return _json_safe(v.tolist())
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _atomic_write(path: str, write) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=os.path.splitext(path)[1])
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_json(path: str, obj) -> None:
    def w(tmp):
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_json_safe(obj), fh, indent=1, sort_keys=False, allow_nan=False)
            fh.write("\n")

    _atomic_write(path, w)


def sparam_record(s: np.ndarray) -> dict:
    """|S_ij| in dB (engineering names "S21") of the excited columns."""
    out = {}
    n = s.shape[-1]
    for j in range(n):
        if np.all(np.isnan(s[:, 0, j])):
            continue
        for i in range(n):
            out[f"S{i + 1}{j + 1}"] = db(s[:, i, j])
    return out


@dataclass
class LoopState:
    """Everything the loop needs to continue (checkpointed)."""

    x: np.ndarray
    epoch: int = 0
    epoch_iter: int = 0
    iteration: int = 0
    t_epoch: list = field(default_factory=list)
    mma: MMAState = field(default_factory=MMAState)
    wall_s: float = 0.0
    stop_reason: str | None = None

    def meta(self) -> dict:
        return {
            "epoch": self.epoch,
            "epoch_iter": self.epoch_iter,
            "iteration": self.iteration,
            "t_epoch": list(self.t_epoch),
            "wall_s": self.wall_s,
            "stop_reason": self.stop_reason,
        }


class Optimizer:
    """The β-continuation epigraph loop on a `Problem` (see the module doc).

    `out_dir` (optional) receives the checkpoint, the history and the frames; `budget_s` limits
    the wall-clock time of the loop (across resumes).
    """

    def __init__(
        self,
        problem,
        *,
        schedule: BetaSchedule | None = None,
        out_dir: str | None = None,
        budget_s: float | None = None,
        mma: MMASettings | None = None,
        log=None,
    ):
        self.problem = problem
        self.param = problem.param
        spec = problem.spec
        self.schedule = schedule or schedule_for(spec)
        self.out_dir = out_dir
        if budget_s is None and spec.optimizer.budget_min:
            budget_s = 60.0 * spec.optimizer.budget_min
        self.budget_s = budget_s
        self.mma_settings = mma or MMASettings()
        self.log = log or (lambda *_: None)
        self.history: list[dict] = []
        self.frames: list[np.ndarray] = []
        if spec.optimizer.seed:
            from yapnr.rf.seeds import initial_x

            x0 = initial_x(problem)
        else:
            x0 = self.param.grid.initial(spec.optimizer.init)
        self.state = LoopState(x=x0)
        self.spec_sha = spec.sha256()

    # -- checkpoints ----------------------------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.out_dir, name)

    def checkpoint(self) -> None:
        if not self.out_dir:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        write_json(self._path(HISTORY), {"spec_sha256": self.spec_sha, "iterations": self.history})
        frames = np.array(self.frames, dtype=np.uint8).reshape((-1,) + self.param.grid.shape)

        def wf(tmp):
            np.savez_compressed(tmp, rho_bar=frames)

        _atomic_write(self._path(FRAMES), wf)
        st = self.state
        arrays = {"x": st.x, **st.mma.to_arrays()}
        meta = dict(st.meta(), spec_sha256=self.spec_sha)
        arrays["state"] = np.array(json.dumps(meta, sort_keys=True))

        def wc(tmp):
            np.savez(tmp, **arrays)

        _atomic_write(self._path(CHECKPOINT), wc)

    def load_checkpoint(self) -> bool:
        """Restore the state from `out_dir` if a checkpoint of this spec is there."""
        if not self.out_dir or not os.path.exists(self._path(CHECKPOINT)):
            return False
        with np.load(self._path(CHECKPOINT), allow_pickle=False) as z:
            meta = json.loads(str(z["state"]))
            if meta.get("spec_sha256") != self.spec_sha:
                raise ValueError("the checkpoint belongs to a different spec")
            x = np.array(z["x"])
            mma = MMAState.from_arrays(z)
        with open(self._path(HISTORY), encoding="utf-8") as fh:
            hist = json.load(fh)["iterations"]
        with np.load(self._path(FRAMES), allow_pickle=False) as z:
            frames = list(np.array(z["rho_bar"]))
        n = int(meta["iteration"])
        self.history = hist[:n]
        self.frames = frames[:n]
        self.state = LoopState(
            x=x,
            epoch=int(meta["epoch"]),
            epoch_iter=int(meta["epoch_iter"]),
            iteration=n,
            t_epoch=[float(t) for t in meta["t_epoch"]],
            mma=mma,
            wall_s=float(meta["wall_s"]),
            stop_reason=meta["stop_reason"],
        )
        self.log(f"resumed at iteration {n} (epoch {self.state.epoch})")
        return True

    # -- the loop -------------------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self.state.stop_reason is not None

    def iterate(self) -> dict:
        """One iteration (design §8.3) at the current epoch's β; returns its record."""
        st, sch, prob, param = self.state, self.schedule, self.problem, self.param
        t0 = time.perf_counter()
        beta = sch.betas[st.epoch]
        rho_bar = param.rho_bar(st.x, beta)
        ev = prob.evaluate(rho_bar, gradients=True)
        df = param.vjp(st.x, beta, ev.grads)
        g = dg = None
        if prob.lengthscale is not None and sch.is_last(st.epoch):
            g, dg = param.lengthscale(st.x, beta, prob.lengthscale)
        epi = Epigraph(param.n_dof, settings=self.mma_settings)
        ls = prob.lengthscale if g is not None else None
        inner_steps = []

        def true_values(xh):
            evh = prob.evaluate(param.rho_bar(xh, beta), gradients=False)
            inner_steps.append(evh.steps["forward"])
            gh = param.lengthscale(xh, beta, ls)[0] if ls is not None else None
            return evh.values, gh

        opt = prob.spec.optimizer
        step = epi.step(
            st.x,
            ev.values,
            df,
            st.mma,
            g,
            dg,
            move=sch.move_for(st.epoch),
            evaluate=true_values if opt.conservative else None,
            max_inner=opt.max_inner,
        )
        change = float(np.max(np.abs(step.x - st.x))) if st.x.size else 0.0
        wall = time.perf_counter() - t0
        rec = {
            "iteration": st.iteration,
            "epoch": st.epoch,
            "beta": beta,
            "t": step.t,
            "f": ev.values,
            "keys": [[k, f / 1e9] for k, f in ev.keys],
            "lengthscale": g,
            "gray": gray_measure(rho_bar, self.param.grid.free),
            "change": change,
            "s_db": sparam_record(ev.s),
            "eta": {str(k): v for k, v in ev.eta.items()},
            "phi": ev.phi,
            "steps": ev.steps,
            "converged": ev.converged,
            "newton_steps": step.newton_steps,
            "inner": step.inner,
            "inner_forward_steps": inner_steps,
            "conservative": step.conservative,
            "t_next": step.t_new,
            "eval_s": ev.wall_s,
            "wall_s": wall,
        }
        self.history.append(rec)
        self.frames.append(np.round(255.0 * np.clip(rho_bar, 0.0, 1.0)).astype(np.uint8))
        st.x = step.x
        st.mma = step.state
        st.iteration += 1
        st.epoch_iter += 1
        st.t_epoch.append(step.t)
        st.wall_s += wall
        if sch.epoch_done(st.epoch, st.t_epoch, change):
            st.epoch += 1
            st.epoch_iter = 0
            st.t_epoch = []
            st.mma = MMAState()
            if st.epoch >= sch.epochs:
                st.stop_reason = "schedule"
        if self.budget_s is not None and st.wall_s >= self.budget_s and not self.done:
            st.stop_reason = "budget"
        self.log(
            f"it {rec['iteration']:4d}  β {beta:5.0f}  t {step.t:+.4f}  "
            f"gray {rec['gray']:.3f}  Δx {change:.3f}  {wall:.1f} s"
        )
        return rec

    def run(self, *, resume: bool = True, max_iterations: int | None = None) -> LoopState:
        """Iterate until the schedule ends or the budget is used up; `max_iterations` stops
        early (without finishing, so a later `run` continues)."""
        if resume:
            self.load_checkpoint()
        n = 0
        while not self.done and (max_iterations is None or n < max_iterations):
            self.iterate()
            self.checkpoint()
            n += 1
        return self.state

    # -- the end --------------------------------------------------------------------------------

    def finish(self) -> dict:
        """Binarize (β = ∞) and evaluate the final design."""
        st, sch = self.state, self.schedule
        beta = sch.betas[min(st.epoch, sch.epochs - 1)]
        rho_last = self.param.rho_bar(st.x, beta)
        rho_bin = self.param.rho_bar(st.x, math.inf)
        ev = self.problem.evaluate(rho_bin, gradients=False)
        return {
            "x": st.x,
            "beta_last": beta,
            "rho_bar": rho_last,
            "binary": rho_bin,
            "gray": gray_measure(rho_last, self.param.grid.free),
            "evaluation": ev,
        }


def design(
    spec,
    out_dir: str,
    *,
    problem=None,
    resume: bool = True,
    budget_s: float | None = None,
    sweep: bool = True,
    log=print,
    **problem_kwargs,
) -> dict:
    """Optimize `spec`, binarize, export and report into `out_dir` (design §10.4).

    Writes spec.json, checkpoint.npz, history.json, frames.npz, footprint.kicad_mod,
    coarse.sNp (the binary design on the optimization grid, every port excited, renormalized to
    50 Ω) and result.json. Returns the result dictionary.
    """
    from yapnr.rf.export import report
    from yapnr.rf.problem import Problem

    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, SPEC), spec.to_dict())
    t0 = time.perf_counter()
    if problem is None:
        problem = Problem(spec, cache_dir=os.path.join(out_dir, "cache"), log=log, **problem_kwargs)
    opt = Optimizer(problem, out_dir=out_dir, budget_s=budget_s, log=log)
    opt.run(resume=resume)
    final = opt.finish()
    result = report.export_design(problem, opt, final, out_dir, sweep=sweep, log=log)
    result["wall_s"]["total"] = time.perf_counter() - t0
    write_json(os.path.join(out_dir, report.RESULT), result)
    return result
