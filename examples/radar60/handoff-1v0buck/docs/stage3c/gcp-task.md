<!-- markdownlint-disable -->

# radar60 as a GCP `yapnr exp` campaign

2026-10-05. Makes the radar flow (source/prepare/place/select/finish/route/check) runnable as a
`yapnr exp` campaign, generic machinery in yapnr and radar specifics in the example, proven with
a real GCP run.

## What landed (yapnr `claude/pnr-explore-gcp`, pushed; not merged)

- `tools/exp/pnr_explore_job.py` (generic): chains an `integrate.py`-shaped driver's steps
  (`DRIVER STEP --work W --out O ...` per step) inside one task, with `{work}`/`{cwd}`
  substitution for drivers that pin a step's subprocess `cwd` to `--engine` (see Finding 1),
  `--collect` globs into `out/<id>/files/` and `--embed KEY=PATH` folding a JSON file (a
  `check` step's report) straight into the record.
- `tools/exp/pnr_explore_plan.py` (generic): N seeds and/or candidate placements -> one
  `mc-eval` stage plan (`docs/cloud-experiments.md`, "Any command as a task"); git-archives the
  committed revision, copies the job runner and any `seed_from` snapshot, writes
  `stage.jsonl`/`campaign.toml`/`manifest.json`.
- `examples/radar60/board/explore_jobs.toml` (radar specifics): names `integrate.py`, the four
  paths to bundle, a local `source`+`prepare` snapshot (`reva/prepared`, not committed -- a
  Batch VM has no network for the atopile board), four seeds and one candidate
  (`reva/placement.json`, the committed stage3b arm-A winner), `route`+`check` as the shared
  post-steps. No power-stage library (`fixblocks`/`block`): those steps only exist on the
  still-unmerged `radar60-3c-power` branch.
- `examples/radar60/board/explore_rank.py` (radar specifics): the DRC/opens/IR/diff-pair/macro
  digest ranking table over a fetched campaign's `dataset.jsonl`.
- Tests: `tests/unit/exp/test_pnr_explore_{job,plan}.py` (stand-in drivers; step chaining and
  failure propagation, collect/embed, `{work}`/`{cwd}`, the produced campaign validated against
  `yapnr.exp.spec`/`kinds.get("mc-eval")` like the existing `rf_stage_plan` test), plus
  `tests/unit/radar60/test_explore_rank.py`. All green (`bazel test //tests/unit/exp/...
//tests/unit/radar60/...`).

## Commands

```sh
# once, locally (needs the atopile board; a Batch VM has no network for it):
python3 examples/radar60/board/integrate.py source --work examples/radar60/board/reva/prepared \
    --out /tmp/out --ato-board <rev-a.kicad_pcb> --engine . --python <numeric-python>
python3 examples/radar60/board/integrate.py prepare --work examples/radar60/board/reva/prepared \
    --out /tmp/out --engine . --python <numeric-python>

python3 tools/exp/pnr_explore_plan.py examples/radar60/board/explore_jobs.toml --repo . --out <dir>
yapnr exp plan <dir>/campaign.toml --backend gcp-batch --region us-west4
yapnr exp submit <plan> --yes
yapnr exp status <plan>            # poll to done
yapnr exp fetch <plan> --full
python3 examples/radar60/board/explore_rank.py <plan's fetched>/assembled
```

## Proof: two waves, one real bug found and fixed

**Wave 1** (`20261005-mceval-c6ca33`): all 5 tasks "succeeded" as Batch jobs but every task's
own `result.json` had `ok: false`, failing in 0.05-1.5 s -- far too fast to be a real
place/route failure. **Finding 1 (real, fixed):** `integrate.py` runs every step's subprocess
with its `cwd` pinned to `--engine`, and builds that subprocess's own `PYTHONPATH`/`sys.path`
from the same (relative) `--engine` string; with a relative `--engine` (`src`, matching the
bundle's layout), the subprocess's own path entries resolved against its _own_ pinned cwd and
doubled up (`src/src/hardware/pnr`), so every step failed at its first `import pnr...` --
`place`'s own `halving_seeded.py` with a plain `ModuleNotFoundError: No module named 'pnr'`,
`finish`'s `pnr.writeback` more confusingly (`yapnr.fab` failed to import the same way, so its
data-profile fallback for `PNR_FAB_PROFILE=pcbway-adv-6l-rf` raised `ValueError` instead of
finding it). The Mac validation runs had used an absolute `--engine` and never hit this; running
on a real GCP task is what surfaced it. Fixed by adding a `{cwd}` substitution next to the
existing `{work}` one (`pnr_explore_job.py`), reproduced locally first (a symlinked `src/`
layout mirroring the real bundle) before resubmitting.

**Wave 2** (`20261005-mceval-555fb9`, same jobs file, the fix): all 5 Batch jobs SUCCEEDED, with
real verdicts this time -- `cand-arm-a` passes (wall 730.8 s), `s0`-`s3` fail at `select` or
`place` (wall 292-338 s, a real placement search, not an import crash).

## Cost and wall time

|                           | Wave 1 (bug)                              | Wave 2 (fixed)                                                                                |
| ------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------- |
| `yapnr exp plan` estimate | expected $0.26, ceiling $0.96             | expected $0.26, ceiling $0.96                                                                 |
| Actual (billing estimate) | ~$0.01 (5 tasks, 0.05-1.5 s each)         | ~$0.06 [D: 3 VMs, each up to the longer of its 2 tasks' wall plus ~60 s boot, at $0.139/VM-h] |
| Wall per task             | 0.05-1.5 s (failed before doing anything) | 292-338 s (seeds), 730.8 s (candidate)                                                        |
| Region / shape            | us-west4, c4d-highcpu-16 Spot, 2 tasks/VM | same                                                                                          |

Both submits were logged in `../gcp-spend.md` before submitting, under this workflow's own $15
cap (program total before this task: ~$26.1 of $50; this task's own spend ~$0.07).

## Comparison: same seed, GCP vs. Mac

`s0` (seed 0, `--n0 8 --procs 2`, no power-stage library, wave 2's fixed code) ran on both:

- **Mac** (M4, this session): `place` tried all 8 starts and failed to legalize every one
  (`no legal placement`), 260.0 s wall; `dataset.jsonl` shows all 8 (`p000`-`p007`) failed, no
  `reason` recorded.
- **GCP** (c4d-highcpu-16 Spot, same seed/n0/procs): `place` itself succeeded -- at least one
  start (`p000`) _legalized_ -- and the chain reached `select`, which then failed: `p000`'s own
  audit rejected it ("pin distance: J1 VIN_5V: U5 (14.1 mm) before the protection D3 (17.9 mm)"),
  and `p001`-`p004` hit `LegalizationError`s during select's own refinement; 292.6 s wall.

**This is a real, unresolved finding, not explained here**: the same seed, n0, procs and
committed code reached a materially different placement-search outcome on the two platforms
(Apple M4 vs. AMD EPYC 9B45, both `procs=2`) -- GCP found a placement to audit-reject, the Mac
found none to even legalize. Candidates for the cause (not investigated further, per the
workflow's scope): floating-point or ordering differences between the platforms somewhere in
legalization's numeric optimization, or a `--procs`-worker-count sensitivity in how the 8 seeded
starts are actually drawn or scheduled. Flagged for review rather than resolved; `s1` did fail
at `place` on GCP too (closer to the Mac's pattern), so it is not a blanket platform failure.

Without the power-stage fixed blocks (U2/U5; `fixblocks`/`block`, still only on
`radar60-3c-power`), neither platform reaches a _passing_ placement for any of seeds 0-3 --
`stage3c/plan.md`'s own root-cause (U2 has 13/27 pads with 0 escape candidates) already said as
much, and this independently confirms it on both platforms, by two different failure paths.

The candidate task (`cand-arm-a`, the committed `reva/placement.json`, skips `place`/`select`
entirely) is the cleaner comparison, and it agrees closely: Mac 699.9 s / GCP 730.8 s wall;
**134 unconnected**, **DRC 7** (`isolated_copper` 1, `items_not_allowed` 3, `via_dangling` 3),
**diff pairs 0/8**, **R1 macro digest unchanged** (`equal: true`, same sha256) on both.

## What is not yet proven

- A power-stage library (`fixblocks`/`block` steps): not exercised here, since those steps are
  only on the unmerged `radar60-3c-power` branch. `explore_jobs.toml`'s `pre_steps`/`post_steps`
  already take arbitrary step lists, so a wave that needs them is a jobs-file edit, not a new
  tool.
- Top-K candidates from `select --top-n K`: only one candidate (the committed winner) was used
  here, for a cheap, real comparison point; the `candidates` list in `explore_jobs.toml` already
  takes any number of placement files, e.g. several ranks from one `select --top-n K` run.
- A wider seed sweep (many starts across many seeds in parallel): the machinery supports it (one
  task per seed, N tasks per `yapnr exp submit`); this proof stayed at 4 seeds + 1 candidate to
  stay under $1 and because every seed's placement-search failure is now a known, explained
  state rather than new information.

(<= 600 words.)
