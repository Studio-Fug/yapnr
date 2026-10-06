<!-- markdownlint-disable -->

# radar60 stage 3c: consolidation

2026-10-05. Owner: "Consolidate: finish the half-done merge so one branch has the power
blocks, the placement work and the GCP job." Result: `claude/radar60-3d` (pushed, no PR --
only example/tooling files changed, no `hardware/pnr` engine code).

## What was merged, and conflicts

1. **`claude/radar60-3c`'s half-done merge** (worktree `yapnr-wt/radar60-3c`, stopped mid-way
   merging `claude/radar60-3c-power`). Conflicts were already resolved and staged by the
   stopped session; `floorplan.yaml`/`constraints.yaml`/`radar60.kicad_pcb` were hand-edited
   but unstaged. Read the staged diff (power_block.py's single-block R1 attempt replaced by
   3c-power's two-block `power_stage.blocks[]` schema -- the single block had no legal
   top-level slot in 24/24 starts), staged `floorplan.yaml`, regenerated the three derived
   files with `gen_board.py` instead of trusting the hand edits, committed
   (`claude/radar60-3c` `04443f5`, pushed).
2. New branch `claude/radar60-3d` off that commit, then: **origin/main** (PRs #56-58, clean),
   **`claude/radar60-rf3b`** (macro v2 freeze) -- conflicts in `floorplan.yaml` (PA pocket
   rect: took v2's `[29.563, 33.2, 31.25, 35.0]`, kept the place track's R46 relocation out of
   its part list) and `rf/README.md` (took rf3b's paragraph: true height 47.40, D14 PA-feed
   pads still unmerged). Regenerated the derived files again.
3. **`claude/pnr-explore-gcp`**, clean. Wired the power-stage library and v2 macro into the
   flow (see below); origin/main's PR #60 (live viewer) had to merge in first since
   `pnr_explore_plan.py` needed `yapnr.exp.spec`'s new `live` key to validate.

## Mechanical fallout, fixed against real data, not guesses

- `kicad_ops.merge_macro` refused every v2 macro board outright (new `radar60:RFM1_PA_FEED`
  footprint, unrecognized). Now recognized and counted, copper left for the PA-feed-vias step.
- The v2 freeze also dropped the RF-uniformity layout's RXD5/TXD0 dummy columns (9 columns, 2
  dummies on all three rfm1-m/n/p, confirmed against the real records).
  `kicad_ops.DEFAULT_COLUMNS` and `test_board_integration`'s hardcoded counts updated to match.
- `integrate.py`'s `block` post-step is for a _free-floating_ power-stage macro; ours are
  _in-situ_ (`site`), fixed by `fixblocks` before `prepare` even runs, so `block` raised ("the
  placement drew no block layouts") on every GCP task. Removed from `post_steps`.
- `_engine_id` (`write_placement`'s provenance) crashed with `FileNotFoundError('git')` on
  every GCP task that reached a legal, audit-passing candidate -- never hit before, since no
  seed had gotten that far in waves 1-3. Fixed to degrade to `""`; unit-tested, not yet
  re-verified on GCP (dedup-by-task-hash blocks a same-id resubmit; a renamed campaign would
  force it -- cheap follow-up, no further spend needed to call this proven).

## Test results

`gen_board.py --check --macro` passes (pocket covered, vias/copper inside the RF region, for
rfm1-m/n/p). 175 Bazel tests pass (`//hardware/pnr/...`, `//tests/unit/{radar60,exp,rf_coupons}/...`,
tag-filtered `-kicad,-slow,-manual`), including a new PA-feed-recognition case and a `git`-
missing regression test. `prek` clean on every commit.

## Mac validation (one placement, full flow)

`source` -> `fixblocks` (two block libraries synthesized locally, 16 seeds each, merged) ->
`prepare` -> `place` (seed 0, n0=8, 4/8 legal) -> `select` (4/4 audit-pass, winner p006) ->
`finish` -> `route` -> `check`, all exit 0. `check`'s own R1 macro-copper check: **equal =
true** (copper unchanged by routing). DRC 19 real violations (3 items*not_allowed, 15
via_dangling, 1 isolated_copper), 184 unconnected. Note: `finish`'s \_own* R1 pre-check compares
the whole board's copper against the bare macro, so it now reads `false` once the power
blocks' pours coexist with the macro pre-route -- a real gap in that check's scope, not a
routing regression (`check`'s post-route, properly-scoped check is the authoritative one).

## GCP smoke campaign (`radar60-smoke3d`, plan `20261006-mceval-033189`)

5/5 tasks SUCCEEDED as Batch jobs (c4d-highcpu-16 Spot us-west4, 2/VM). Ceiling $0.96, actual
~$0.03.

| Task                                           | Stage reached                                                                                                 | Result                                                                 |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| s0-s3 (n0=8 each)                              | place OK -> **select crashed** (the `git` bug above)                                                          | legalize+audit-pass for the first time on GCP with the power blocks in |
| cand-arm-a (stale pre-consolidation placement) | finish OK -> route -> **route's internal fixed_copper --validate failed** (exit 1, reproduced on the Mac too) | expected: predates the new fixed sites/frame                           |

Live viewer: `http://<tailnet-host>:8791/` (still running). Plan copied to
`stage3c/plans/20261006-mceval-033189/`; spend logged in `gcp-spend.md`.

## What remains

- Antenna v2 re-integration into the schematic/BOM (RT1/RT2 loads already match; nothing else
  checked here).
- PA-feed vias (D14): `RFM1_PA_FEED`'s pads, through vias, A2/B2 `skip_pads`, the In3 1V0 pour
  -- `kicad_ops.merge_macro` still drops the footprint rather than wiring it.
- Routing waves proper (seed/candidate search for a real winner): this step only proved the
  flow runs, with `n0=8`/one seed; the legalizer fix (another track) and a wider search are
  still needed to beat the 152-unconnected wave-4 board.
- `route`'s own `fixed_copper --validate` failure (both Mac and GCP) needs root-causing --
  route's exception handler swallows the subprocess's stderr, so the exact cause is unknown.
- `finish`'s whole-board-vs-bare-macro R1 pre-check should be rescoped now that power-stage
  copper legitimately coexists with the macro pre-route.
- Re-verify the `_engine_id` fix on GCP (rename the campaign, cheap).

## Review (2026-10-05, adversarial pass)

- **Ancestry:** `claude/radar60-3d` contains origin/main 586857d, radar60-3c 04443f5,
  radar60-3c-power af876cc, the place track f1d7c6b, radar60-rf3b 727df41, pnr-explore-gcp
  4f9d49d. A per-file "lines added on the source branch but absent in the result" pass found only
  intended supersessions: the v1 frame numbers (47.35, 51.469, 29.557, holes y 43.85) replaced by
  v2's, the RXD5/TXD0 dummies, the place track's single `power_stage` block replaced by the power
  track's two-block `blocks[]`, and rf3b's older copies of the schematic/board files that
  radar60-3c had already moved past. `rf/` equals rf3b's except README/BUILD; rfm1-m/n/p
  regenerate-check clean (`gen_board.py --check --compile --radome --macro` exit 0).
- **Tests:** 175/175 Bazel tests pass; privacy scan clean; engine diff vs main is only
  `tools/exp` + fab data, no machine paths.
- **Fixed:** board README floorplan table still gave the v1 frame (47.35 mm, x 51.469 east edge,
  pocket y 34.2, four loads RT1-RT4 at v1 positions) and docstrings said RT1-RT4; updated from
  floorplan.yaml/rfm1-n.json (`16a5450f`, pushed).
- **Smoke r2** (`20261006-mceval-d756cd`, viewer :8792): the `_engine_id` fix verified on GCP; all
  4 seeds pass place->select->finish->route->check. Unconnected 152/170/179/190, R1 equal on all.
  But **1V0_BUCK open (R_SH1.1) on all four**.
- **Root cause:** the consolidation re-synthesized the power-block library (2 ranked layouts per
  block); `power_block.py rank` orders hot-link opens before IR, so U2's winner df581a4262c0 has
  its own 1V0_BUCK path open (wave 4's 34c7ed35931c: 0.388 mOhm pass, 13 vs 11 open hot links).
  Reusing wave 4's library wholesale fails at `place` (`w4`, `20261006-mceval-b7d283`): its U5
  block d0ebd5a8e456 puts U5 at x 18.3, outside the place track's `efuse_ic` region (x 6.8-15.8)
  -- the cross-track reason a re-synth was needed for U5 (not for U2).
- **Hybrid** (wave 4's U2 block + the consolidation's U5 block de5bfabb679f; snapshot
  `review3d/work`, library `review3d/power-blocks-library.json`; `w4b`,
  `20261006-mceval-5818f8`, viewer :8794): 1V0_BUCK **0.383 mOhm PASS** on all four, unconnected
  152/148/162/153 (s1 **148** < wave 4's 152), DRC 14 each (s0: one `shorting_items`), R1 equal.
  **Use `review3d/work` (not `reva/prepared`) as `seed_from` for the next wave.**
- Open: rank a block whose own IR is `open` below every block whose IR is not (power_block.py),
  so a re-synth cannot silently pick an open buck path again.
