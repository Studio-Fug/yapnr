# yapnr.rf multi-start with mechanical selection

Owner approval: multi-start in `yapnr.rf` — N starts with varied step sizes or seeds, each
validated from its footprint, the best kept mechanically, mirroring PnR's Monte Carlo selection
and successive halving (`pnr.mc.halving`).

This covers the schema (§1) and the halving pause primitive (§2) landed so far. Rung ranking,
final selection, per-start execution (local subprocesses or cloud waves) and a CLI entry point
are not implemented yet; a spec with no `starts` key is unaffected (one run, today's behavior).

## 0. Summary

- A **start** is the base spec with a few optimizer _path_ fields overridden: `move`,
  `move_late`, `init`, `seed`, and two new fields, `perturb_amplitude` and `perturb_seed`. Each
  start's run directory is an ordinary run directory, with its own `spec.json` and checkpoint.
  Start 0 is the base spec. Identity is the **design sha256**: the spec's sha256 with `name` and
  the execution fields (`backend`, `dtype`, `threads`, as in `cases.resume_spec`) set to the
  base's. A finished run with the same design sha256 can be imported as a start.
- **Halving is optional** and happens at epoch ends. Starts would be ranked there by a short
  validation, not by the epigraph t: the best binarized design so far, swept on the coarse grid
  and judged by the case's fine-level criteria. A within-case replication round (the same spec,
  several moves/seeds, several window sizes) is needed to measure whether that ranking agrees
  with the final result before halving is turned on by default — a single run is path-sensitive
  (a rounding-level kernel change can move a result 2–4 dB), so any such measurement needs
  replicates, and halving should report a confidence interval rather than a point estimate.
- **Final rule**, from the owner's order: pass all criteria on all validation grids first, then
  the worst fine/finer margin, then the fewest repaired pixels, then the start id.
- **Outputs:** every start is kept. `starts.json` would summarize them, and the winner's run is
  copied to the top of the directory, so it reads as a run directory does today.
- **Execution:** locally, each start is a time-bounded subprocess, P processes × T threads. In
  the cloud, each start is one `rf_stage_plan` line per wave, and halving runs on the Mac between
  waves. Multi-start is orthogonal to the spec's objectives (radiation box, NTFF, antenna
  objectives, ...): it only touches the optimizer path fields above, so it works with any spec.

## 1. Spec schema

`starts` is a new top-level key of the spec, next to `optimizer`, so it is not
`optimizer.starts`. A start's spec is the base minus `starts`, plus its optimizer overrides; one
run's optimizer never reads `starts`, and dropping it keeps start 0's hash equal to the single
run's.

```yaml
starts:
  vary: # optimizer path fields only
    move: [0.20, 0.18, 0.19, 0.21, 0.22]
    perturb_seed: [0] # a length-1 list broadcasts
  combine: zip # zip (lists of length n or 1) | product
  halving: # optional; omitted = every start runs to the end
    rungs: [{ after_epoch: 0, keep: 0.5 }, { after_epoch: 1, keep: 0.5 }]
    min_keep: 2
    control: 0 # seeded uniform sample of eliminated starts kept on as controls
    seed: 0
  select:
    criteria: case # case | spec | path of a criteria file (rf_job's format)
    quantum_db: 0.001
```

The spec is checked when it loads (`yapnr.rf.spec._validate_starts_structure`, and, per start,
`yapnr.rf.multistart.derive_starts`):

- `vary` keys must be on the allow-list (`STARTS_VARY_ALLOWED`). `betas`, the iteration caps,
  objectives, robust variants, the solver and the requirements are refused, because halving
  compares starts at the same epoch of the same problem.
- Start 0 must equal the base spec.
- Two starts may not have the same design sha256. The native f64 path is deterministic, so a
  duplicate would only recompute an identical run.
- n is at most 64.
- Rung `after_epoch` values must be strictly increasing, non-negative integers, and come before
  the last epoch of the spec's β schedule — a rung at or past the last epoch would be silently
  skipped by halving, so `derive_starts` refuses it once the spec (and so the schedule length)
  is known.
- `keep` must be a number in (0, 1).
- A nonzero `perturb_seed` with `perturb_amplitude` at 0 does nothing (`Optimizer.__init__` only
  perturbs x0 when the amplitude is nonzero), so `derive_starts` refuses that combination rather
  than silently spending N× the compute of one real run while exploring only one point.
- Every derived spec must pass `Spec.validate`.

`Spec.to_dict` leaves out `starts`, `perturb_amplitude` (default 0) and `perturb_seed` while they
are at their defaults, and `starts` is excluded from the dataclass's generated `__hash__` (it's a
plain dict); every preset's published hash is unchanged, and tests check both.

**Perturbation** (default off, `yapnr.rf.multistart.perturb`). After the seed or `init`:

- x0 += amplitude·(2u − 1), clipped to [0, 1].
- The noise is added to the design-variable vector, so symmetry and fixed pixels still hold.
- u comes from a counter-based SplitMix64 on `(perturb_seed, dof index)`, written in 64-bit
  integer arithmetic. It does not depend on numpy's `Generator` streams, so it is the same on
  any numpy version or machine.

**Start ids and records.** Starts are numbered `s00`, `s01`, and so on, in plan order. Each
start's spec keeps the base name; its `spec.json` alone reproduces it with the plain driver.

## 2. The halving pause primitive

`Optimizer.run(until_epoch=)` stops once `state.epoch >= until_epoch`, checked before the next
iteration, without touching `stop_reason` or anything else in `state` — a run can also end early
on convergence, so `until_epoch` is a rung on epoch boundaries, not on iteration counts. Resuming
with a later `until_epoch` (or `None`, to run to the end) continues exactly as an uninterrupted
run would: x, the history and the MMA state are bit-identical whether the run paused there or
not. This is the primitive successive halving's rung bookkeeping (binarize-and-judge, rank by
worst fine/finer margin, `keep = max(min_keep, ceil(keep · live))`) and final selection will be
built on; neither is implemented yet.
