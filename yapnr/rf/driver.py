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
The run stops after the last epoch or when the wall-clock budget is used up. Each epoch
optimizes the spec's epigraph unless `optimizer.epoch_objectives` names another objective for
it ("radiation": the band-averaged log(1 + R̄) − log η̄ of a radiating port, which grows a
radiator from a gray start; `Problem.objective_units`) and may solve at its objective
frequencies scaled by `optimizer.epoch_frequency_scale` (frequency continuation).

**The exported design is the best binarized one, not the last iterate.** The epigraph value t at
finite β is not the value of the binary design (gray pixels interpolate), and plain MMA may
raise t, so the loop also evaluates the binarized design (β = ∞, after the width and space
repair, one forward run per excitation and no adjoint) at the first iteration, at the start of
every epoch and every `optimizer.binary_every` iterations, and `finish` once more at the end.
The design with the lowest binary t is exported; `result.json` records its iteration. Binary
designs are judged by the spec's epigraph in every epoch, whatever the epoch's objective.

A conservative step (`optimizer.conservative`) that does not reach a conservative approximation
within `max_inner` subproblems is rejected: x stays, the record says `accepted: false`, and
the next iteration reuses the same evaluation and continues from the raised curvature.

Checkpoints: `checkpoint.npz` (x, the best binarized design's x as `best_x`, the MMA state and
the loop state as JSON text), written atomically after `history.json` and `frames.npz`, so a
resumed run continues from the last completed iteration and reproduces an uninterrupted run bit
for bit (same backend, dtype and thread count). After `finish`, `export_x` is the exported
design's x (what the validator re-derives the binary design from).
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
    best_x: np.ndarray | None = None  # x of the best binarized design so far
    best_t: float | None = None  # its epigraph value (binary, repaired)
    best_iteration: int | None = None
    binary_checked: int = -1  # the last iteration whose binarized design was evaluated
    export_x: np.ndarray | None = None  # set by `finish`
    export_iteration: int | None = None
    move: float | None = None  # the adaptive move (`optimizer.adaptive_move`); None: schedule's

    def meta(self) -> dict:
        return {
            "epoch": self.epoch,
            "epoch_iter": self.epoch_iter,
            "iteration": self.iteration,
            "t_epoch": list(self.t_epoch),
            "wall_s": self.wall_s,
            "stop_reason": self.stop_reason,
            "best_t": self.best_t,
            "best_iteration": self.best_iteration,
            "binary_checked": self.binary_checked,
            "export_iteration": self.export_iteration,
            "move": self.move,
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
        # (key of x and β, evaluation, ∂f/∂x) of the last iteration: a rejected conservative step
        # leaves x unchanged and the next iteration reuses it.
        self._last_eval: tuple | None = None
        if spec.optimizer.seed:
            from yapnr.rf.seeds import initial_x

            x0 = initial_x(problem, cache_dir=out_dir, log=self.log)
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
        if st.best_x is not None:
            arrays["best_x"] = st.best_x
        if st.export_x is not None:
            arrays["export_x"] = st.export_x
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
            best_x = np.array(z["best_x"]) if "best_x" in z else None
            export_x = np.array(z["export_x"]) if "export_x" in z else None
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
            best_x=best_x,
            best_t=meta.get("best_t"),
            best_iteration=meta.get("best_iteration"),
            binary_checked=int(meta.get("binary_checked", -1)),
            export_x=export_x,
            export_iteration=meta.get("export_iteration"),
            move=meta.get("move"),
        )
        self.log(f"resumed at iteration {n} (epoch {self.state.epoch})")
        return True

    # -- the loop -------------------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self.state.stop_reason is not None

    def _binary_due(self) -> bool:
        st = self.state
        if st.binary_checked < 0 or st.epoch_iter == 0:
            return True  # the start, and the start of every epoch (the last epoch's end design)
        every = self.problem.spec.optimizer.binary_every
        return bool(every) and st.iteration - st.binary_checked >= every

    @property
    def variants(self) -> list:
        """Projection thresholds of the designs in the epigraph: None (nominal), then the
        robust variants (`optimizer.eta_variants`)."""
        return [None] + [float(e) for e in self.problem.spec.optimizer.eta_variants]

    def evaluate_binary(self, x: np.ndarray) -> tuple[float, object, np.ndarray, dict]:
        """(t, evaluation, exported pixels, repair info) of the binarized design of `x`: β = ∞,
        then the width and space repair of the export, one forward run per excitation. With
        robust variants, t is the largest over the nominal design and the variants at β = ∞."""
        from yapnr.rf.export.report import exported_binary

        exported, info = exported_binary(self.problem, self.param.rho_bar(x, math.inf))
        ev = self.problem.evaluate(exported, gradients=False)
        t = ev.t
        for e in self.variants[1:]:
            evv = self.problem.evaluate(self.param.rho_bar(x, math.inf, e), gradients=False)
            t = max(t, evv.t)
        return t, ev, exported, info

    def objective(self, epoch: int) -> tuple[str, float]:
        """The objective of `epoch` and its frequency scale (`optimizer.epoch_objectives`,
        default "spec"; `optimizer.epoch_frequency_scale`, default 1)."""
        o = self.problem.spec.optimizer
        eo, fs = o.epoch_objectives, o.epoch_frequency_scale
        e = min(epoch, len(o.betas) - 1)
        return (str(eo[e]) if eo else "spec", float(fs[e]) if fs else 1.0)

    def _evaluate(self, x: np.ndarray, beta: float, gradients: bool, objective=("spec", 1.0)):
        """The nominal evaluation, every variant's values (concatenated after the nominal ones)
        and, with `gradients`, ∂f/∂x of all of them."""
        prob, param = self.problem, self.param
        evs, dfs = [], []
        for e in self.variants:
            ev = prob.evaluate(
                param.rho_bar(x, beta, e),
                gradients=gradients,
                objective=objective[0],
                frequency_scale=objective[1],
            )
            evs.append(ev)
            if gradients:
                dfs.append(param.vjp(x, beta, ev.grads, e))
        values = np.concatenate([ev.values for ev in evs])
        df = np.concatenate(dfs, axis=0) if gradients else None
        return evs[0], values, df, [float(ev.t) for ev in evs]

    def _track_binary(self, rec: dict) -> None:
        st = self.state
        t_bin = self.evaluate_binary(st.x)[0]
        st.binary_checked = st.iteration
        rec["t_binary"] = t_bin
        if st.best_t is None or t_bin < st.best_t:
            st.best_t, st.best_iteration, st.best_x = t_bin, st.iteration, st.x.copy()

    def iterate(self) -> dict:
        """One iteration (design §8.3) at the current epoch's β; returns its record."""
        st, sch, prob, param = self.state, self.schedule, self.problem, self.param
        t0 = time.perf_counter()
        beta = sch.betas[st.epoch]
        objective = self.objective(st.epoch)
        rho_bar = param.rho_bar(st.x, beta)
        key = (st.x.tobytes(), beta, objective)
        if self._last_eval is not None and self._last_eval[0] == key:
            ev, values, df, t_var = self._last_eval[1]  # x unchanged (a rejected step)
            reused = True
        else:
            ev, values, df, t_var = self._evaluate(st.x, beta, True, objective)
            reused = False
        self._last_eval = (key, (ev, values, df, t_var))
        rec: dict = {"iteration": st.iteration, "epoch": st.epoch, "beta": beta}
        if objective[0] != "spec":
            rec["objective"] = objective[0]
        if objective[1] != 1.0:
            rec["frequency_scale"] = objective[1]
        if len(t_var) > 1:
            rec["t_variants"] = t_var
        if self._binary_due():
            self._track_binary(rec)
        g = dg = None
        if prob.lengthscale is not None and sch.is_last(st.epoch):
            g, dg = param.lengthscale(st.x, beta, prob.lengthscale)
        epi = Epigraph(param.n_dof, settings=self.mma_settings)
        ls = prob.lengthscale if g is not None else None
        inner_steps = []

        def true_values(xh):
            evh, vals, _, _ = self._evaluate(xh, beta, False, objective)
            inner_steps.append(evh.steps["forward"])
            gh = param.lengthscale(xh, beta, ls)[0] if ls is not None else None
            return vals, gh

        opt = prob.spec.optimizer
        trust = None
        if opt.adaptive_move and beta >= opt.adaptive_from_beta:
            base = sch.move_for(st.epoch)
            mv = base if st.move is None else min(st.move, base)
            trust = epi.trust_step(
                st.x,
                values,
                df,
                st.mma,
                g,
                dg,
                move=mv,
                evaluate=true_values,
                max_inner=opt.max_inner,
                slack=opt.trust_slack,
            )
            step = trust.step
            if not step.accepted:
                st.move = max(1e-3, 0.5 * trust.move)
            elif step.t_new < step.t:
                st.move = min(base, 1.5 * trust.move)
            else:
                st.move = trust.move
        else:
            step = epi.step(
                st.x,
                values,
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
        rec.update(
            {
                "t": step.t,
                "f": values,
                "keys": (
                    [[k, f / 1e9] for k, f in ev.keys]
                    if len(t_var) == 1
                    else [[k, f / 1e9, e] for e in self.variants for k, f in ev.keys]
                ),
                "lengthscale": g,
                "gray": gray_measure(rho_bar, self.param.grid.free),
                "change": change,
                "s_db": sparam_record(ev.s),
                "eta": {str(k): v for k, v in ev.eta.items()},
                "phi": ev.phi,
                "steps": ev.steps if not reused else {"forward": {}, "adjoint": {}},
                "converged": ev.converged,
                "reused_evaluation": reused,
                "newton_steps": step.newton_steps,
                "inner": step.inner,
                "inner_forward_steps": inner_steps,
                "accepted": step.accepted,
                "t_next": step.t_new,
                "move": trust.move if trust is not None else sch.move_for(st.epoch),
                "t_trials": trust.t_trials if trust is not None else [],
                "eval_s": 0.0 if reused else ev.wall_s,
                "wall_s": wall,
            }
        )
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
            st.move = None
            if st.epoch >= sch.epochs:
                st.stop_reason = "schedule"
        if self.budget_s is not None and st.wall_s >= self.budget_s and not self.done:
            st.stop_reason = "budget"
        tb = rec.get("t_binary")
        self.log(
            f"it {rec['iteration']:4d}  β {beta:5.0f}  t {step.t:+.4f}  "
            + (f"t_bin {tb:+.4f}  " if tb is not None else "")
            + ("" if step.accepted else "rejected  ")
            + f"gray {rec['gray']:.3f}  Δx {change:.3f}  {wall:.1f} s"
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
        """Binarize (β = ∞) the last design, compare it with the best binarized design of the
        loop and evaluate the better one (the exported design, after the width and space
        repair); records the choice in the state and the checkpoint."""
        st, sch = self.state, self.schedule
        beta = sch.betas[min(st.epoch, sch.epochs - 1)]
        t_last, ev_last, exp_last, info_last = self.evaluate_binary(st.x)
        x, t, ev, exported, info, it = st.x, t_last, ev_last, exp_last, info_last, st.iteration
        if st.best_x is not None and st.best_t is not None and st.best_t < t_last:
            t, ev, exported, info = self.evaluate_binary(st.best_x)
            x, it = st.best_x, st.best_iteration
            self.log(
                f"exporting the binarized design of iteration {it} (t {t:+.4f}); the last "
                f"design's is {t_last:+.4f}"
            )
        st.export_x, st.export_iteration = x.copy(), int(it)
        self.checkpoint()
        rho_last = self.param.rho_bar(x, beta)
        return {
            "x": x,
            "beta_last": beta,
            "rho_bar": rho_last,
            "binary": self.param.rho_bar(x, math.inf),
            "exported": exported,
            "repair": info,
            "gray": gray_measure(rho_last, self.param.grid.free),
            "evaluation": ev,
            "export_iteration": int(it),
            "t_binary_last": t_last,
            "t_binary_best_tracked": st.best_t,
            "best_iteration_tracked": st.best_iteration,
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
