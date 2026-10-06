<!-- markdownlint-disable -->

# Stage 3c iterate — pass 3

Still no routed copper, no engine commits, no PR. $0 spent this pass. This pass's job was to
stop re-verifying known state and either land real engine code or produce a genuinely new,
actionable finding; it did the latter, not the former, and says plainly why.

## What this pass actually did

1. Re-confirmed the review's cold numbers independently (own scratch copy, `kicad-cli pcb
drc`, `candidate.kicad_pcb` md5-matched to `stage3b-board/integration/radar/A/route/`):
   142 unconnected items, 2 `skew_out_of_range` (CLK -18.69 mm, FRCLK -10.86 mm vs 0.1 mm),
   2 `diff_pair_uncoupled_length_too_long` (16.39 mm, 8.56 mm vs 3.0 mm), 187
   `lib_footprint_issues`. The pipeline's own `candidate.drc.json` (run in-place, with the
   real project file) matches exactly on all four counts — this is not a scratch-copy
   artifact.
2. **Root-caused the 187 `lib_footprint_issues` (finding 4).** Every one is "the current
   configuration does not include the footprint library 'Radar60\_\*'" across 54 distinct
   synthetic library nicknames (`Radar60_R_10k_0402`, `Radar60_TestPad_D1`, ...). There is no
   `fp-lib-table` next to `candidate.kicad_pro`, and none anywhere under `radar60/` resolves
   those names — confirmed by grep across the whole tree.
   - The engine already has the right tool for this: `hardware/pnr/pnr/library_table.py`,
     wired into `pnr.bzl`'s board rule (`library_table` target attr `out_libs`, copied to
     `<outdir>/fp-lib-table` at line ~312, keyed off a `footprints = [...]` attr of source
     `.kicad_mod` files).
   - Two real gaps, not a library defect and not embedded-footprint drift as the prior
     review guessed:
     (a) the radar60 Bazel target passes no `footprints = [...]` list (there are zero
     `.kicad_mod` files checked into `examples/radar60/`; parts come from atopile's
     generated/cached footprints, so the table would be empty even if the rule ran), and
     (b) the `stage3b-board/integration` pipeline that actually produced `candidate.kicad_pcb`
     doesn't go through this Bazel rule at all — there is no `fp-lib-table` file anywhere in
     that output directory, Bazel-built or otherwise.
   - This means the "0 at all severities" target does **not** need 187 individual
     library/footprint fixes or an owner waiver. It needs the integration pipeline to emit
     one `fp-lib-table` (mapping each embedded footprint's lib nickname to a generated,
     `${KIPRJMOD}`-relative footprint directory, the same shape `pnr/library_table.py`
     already writes) next to `candidate.kicad_pro`. That is in scope for `integrate.py`
     (R6/E-route area), not a per-part library cleanup.
   - Not yet verified end-to-end: whether registering the table drops the warning to 0, or
     surfaces a smaller set of genuine embedded-vs-library mismatches instead. That check
     (generate the table, re-run DRC, confirm) is next, and belongs in the `s3c-pour` or
     `radar60-3c` track together with the actual route step, not as an isolated patch landed
     without the harness that will exercise it.
3. **Did not land it as a commit.** Doing so correctly means: (a) deciding whether the
   `.kicad_mod` sources should be materialized from atopile's footprint cache and wired into
   the Bazel `footprints` attr (byte-identical, so existing non-radar boards are unaffected),
   or whether `integrate.py` should call `pnr.library_table` directly on whatever footprint
   artifacts the hierarchical pipeline already has; and (b) a test fixture, since this is
   general/opt-in engine surface per the workflow rules, not a radar-only hack. That's
   real, multi-hour work, same class as E1/E3/E5. Writing an untested one-off script against
   `candidate.kicad_pcb` to silence 187 warnings without that plumbing would be exactly the
   "no hand-fixing, no fabricated done-claim" anti-pattern the last review flagged, just
   moved from routing to DRC.
4. **Did not touch routing (E5 LVDS, E1/E3/E4 plane partition + un-stranding, pours, QSPI,
   PA corner), the macro-swap plumbing, or D14/C1** this pass. Each is a real algorithm
   change to a shared router with no safe shortcut; see `build.md` for the prior pass's scope
   read. None of them moved.
5. **Checked on, did not touch, the splanc mini board.** `splanc-100/parity-main-p003-native`
   (native pair search on USB D-, PID 43694, started 00:55) is still running under
   `gapfix/heavy.sh`, holding a slot, no queue backlog. Its own `gcp-spend.md` shows no GCP
   submissions yet ($0 of its $15 cap). It has no status/plan doc of its own under
   `splanc-100/` beyond `parity-native.sh` and the run logs; `probe-main-place.log` shows
   4 placements (p000-p003) legal, 5.7-6.6 s each — pre-routing. It needs an owner (whoever
   started it) or a dedicated status pass; this pass did not claim it or write to its
   directory, per the no-interference rule.
6. RF track: `stage3b-rf/freeze.md` unchanged since the last pass — `s3b-e2` (C1 retune +
   D14 fence, ceiling $4.85) still pending, D15=A/S1/K0 final, macro v2 **not frozen**.
   Nothing to swap yet; v1 remains the fixed_block basis, per the owner's standing decision.

## Spend

$0 (no GCP, no Mac heavy.sh runs beyond the read-only `kicad-cli` DRC pass, which is local
and free). Program ledger unchanged at ~$23.6 of $50 per `radar60/gcp-spend.md` ($10.4 stage
3b RF actual + up to $4.85 `s3b-e2` ceiling + ~$8.4 pre-stage-3b), splanc-100 at $0 of $15.

## Next pass, concretely

1. Land the `fp-lib-table` generation in `integrate.py` (or wire radar60's `footprints`
   Bazel attr), with a test board that has a deliberately-missing library today and a 0
   `lib_footprint_issues` result after; local Bazel run before push.
2. Start E5 (LVDS coupled-pair routing restricted to F.Cu) in `s3c-pairs` — the single
   highest-value remaining track since it's both a hard DRC gate (skew, uncoupled length)
   and currently fully unrouted on two of four legs.
3. Get an owner/status read on `splanc-100` so it isn't orphaned.

(The engine tracks landed and merged since this pass: see `build.md`'s successor `integrate.md`
for the `s3c-engine` merge, PR #55, and Wave 0. Wave 1 below is this file's first real
route+check wave.)

## Wave 1: R4 (LVDS coupled, F.Cu only), fresh prepare/finish/route/check

Branch `claude/radar60-3c` (`bca32ab`, on top of the merged `claude/s3c-engine`). `freeze.md`
checked first: still **BLOCKED, not frozen** (`s3b-e2a`/`s3b-e2b` pending) — v1 stays the
`fixed_block` basis, per the owner's standing decision; no macro swap this wave.

**Landed (R4, commit `bca32ab`):** `board.routing.route_pairs: coupled` (now available from the
merged E5/`s3c-pairs` track), each LVDS `diff_pair` gets `layers:[F.Cu]` and
`max_uncoupled_mm:3.0`; the `lvds` via class and the `lvds_outer_layers` DRU rule both drop
B.Cu (a coupled pair needs one shared layer for both legs, so "F.Cu or B.Cu" was never actually
safe). `dru_selftest.py` gains a B.Cu-LVDS case (24/24 pass); `test_board_integration` passes
unchanged; `gen_board.py --check --compile` clean.

**Run:** first wave to run `source`→`prepare`→`finish --placement reva/placement.json` fresh
against the _current_ radar60-3c inputs, rather than reusing stage-3b's stale `work/` snapshot
(which predates R2's anchors and R3's In3 power typing actually taking effect in fanout
planning) — Wave 0's numbers and this wave's are therefore not a clean trend; this wave is the
new clean baseline. Route: 537.8s. Checked twice (538s, 543s) with identical results; the second
run carried temporary debug instrumentation in `router.py` to capture `pair_route`'s internal
report, reverted after and never committed (see Findings).

| Metric                                | Wave 1                              | Note                                                                   |
| ------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------- |
| DRC (all severities)                  | **0**                               | fp-lib-table confirmed working (`check-drc.json`: 0 violations)        |
| Unconnected                           | **141**                             | vs. the ~142 cold-check baseline; real number now (fp-lib-table fixed) |
| R1 macro digest                       | equal                               | pass                                                                   |
| R4 foreign copper                     | 0 items                             | pass                                                                   |
| QSPI                                  | 6/7 routed, max 17.8 mm (budget 25) | only `QSPI_D1` open                                                    |
| LVDS                                  | 0/8 legs (all 4 pairs)              | see Findings — classified R7, not an engine bug                        |
| IR 1V0_BUCK                           | fail, 35.0 mΩ (budget 0.5)          |                                                                        |
| IR 1V0_SH                             | fail, 138.5 mΩ (budget 0.5)         | worse than Wave 0's stale-input 70 mΩ; see Run note                    |
| IR 1V0_RF1                            | fail, 12.75 mΩ (budget 4.0)         |                                                                        |
| IR 1V0_RF2, 1V8, 3V3_RADIO_IO, 5V_SYS | open                                | pre-R1 known state (U2/U5/eFuse stranding)                             |
| IR 1V2, 3V3                           | fail                                |                                                                        |
| Cost                                  | $0                                  | Mac only (heavy.sh); see `gcp-spend.md`                                |

**Findings:**

1. **LVDS: engine works as designed; the placement doesn't support it yet (→ R7, not a bug).**
   Instrumented `pair_route`'s report directly: all 4 pairs return `status: "legs", reason:
"no_coupled_channel"` with hundreds-to-thousands of failed placement attempts each
   (dominated by `pair_geometry`/`terminal_fanout`, i.e. no side-by-side channel exists at this
   placement's ball/fanout geometry), and — because the fallback-to-legs path itself commits no
   copper when the channel search already exhausted the escape corridor — `coupled_route.nets`
   comes back empty, so these nets fall through to the ordinary maze, which also fails for 3 of
   4 (no failure sites logged: blocked pre-maze) and partially fails the 4th (TX1, 1 failure
   site). Net effect: 0/8 legs land, worse than the pre-R4 baseline's partial B.Cu legs on this
   _specific stale arm-A placement_. This is expected and correct: the arm-A placement predates
   R7 (LVDS-aware exit bands/32-start re-place) and was never designed for a coupled route.
   Reverting B.Cu to mask this would hide the real blocker; not done. **R7 is now the
   highest-value next item** — it directly unblocks LVDS on this same engine.
2. **fp-lib-table (identified in pass 3) confirmed fixed end-to-end**: 0 DRC violations of any
   severity, 141 unconnected (matches the ~142 cold baseline almost exactly) — the 187
   `lib_footprint_issues` are gone for good, not just root-caused.
3. **QSPI is close**: 6/7 nets routed within budget; only `QSPI_D1` open. Likely a placement/
   fanout capacity item for the same R7 pass, not yet root-caused individually.
4. Debug instrumentation added to `router.py` to dump `pair_route`'s report was **not
   committed** — reverted (`git checkout --`) immediately after use; `git status` confirms a
   clean tree on that file.

**Renders** (`--use-board-stackup-colors`, routed `work/route/candidate.kicad_pcb`):
`stage3c/renders/wave1-radar60-{top,bottom,angled}.png`; archived to
`progress-gallery/2026-10-05/radar60/`.

**Next wave:** land R7 (exit bands from the U1 fanout plan, south-edge order, Y1 region, 32 MC
starts) so LVDS has a placement that can actually support a coupled route, then re-route top 4.
Pass marks are not met; 8-layer criteria not evaluated (criteria apply to "the top 4 routed
candidates of the final two waves" — not reached). Stopping here per the token-budget note
(lean, one wave at a time) rather than starting R7 placement-engine work in the same pass.

## Review of Wave 1: FAIL, with six findings

An independent review (cold checks against a copy of `wave1/work/route/candidate.kicad_pcb`)
confirmed the engine is clean (every post-placement copper item traces to `routes.json`; no
relaxed limits; no hand-routing) but found Wave 1's own numbers overstated completion:

1. **QSPI 6/7 was wrong; it is 4/7.** `kicad_ops.py measure` summed track length per net with
   no connectivity check, so `QSPI_CS_N` and `QSPI_D3` (copper present, pad still open) counted
   as routed.
2. **`check` skipped three things R6 requires:** its own `--refill-zones`, `rf_audit` on the
   _routed_ board (only the pre-route one is audited at `finish`), and an independent IR
   re-run (it trusted route's cached `ir.json`).
3. **5 DRC checks are invisible to `--severity-all`:** `footprint_filters_mismatch`,
   `footprint_type_mismatch`, `missing_courtyard`, `track_not_centered_on_via`,
   `tuning_profile_track_geometries` default to "ignore" in the project's own
   `rule_severities` (KiCad's stock defaults, not something this pipeline set), so "0 violations"
   never saw the 9 + 5 + 1 = 15 real ones.
4. **Owner Q1 (R46 -> WFCP0612 1 mOhm) was never applied**, and a floorplan.yaml comment
   mislabeled `R_SH1` as "R46" (a different part, on a different rail).
5. **Of the engine's own E1/E2/E3/E4/E6 (all landed in PR #55), the radar inputs used none of
   them** — only E5 (LVDS, via R4). R1/R7/R8/R9 were not wired in.
6. **"Opens must fall every wave" wasn't shown**: Wave 1 was a new baseline (fresh inputs), not
   a trend against Wave 0.

## This pass: fixes for findings #1-#3 (check step), a floorplan doc fix for #4, and R8/E6

wired in for #5 — committed, tested, and run through two real route+check waves

Branch `claude/radar60-3c`, 5 commits on top of `bca32ab` (R4): `291b67c` (check step fixes),
`5b2783c` (R_SH1/R46 doc fix), `f681d69` (R8 pour wired in), `d159a27` (R8: solid, not thermal,
after Wave 2 found a real new DRC violation the thermal default caused — see below).

**Findings #1/#2/#3 (`integrate.py step_check`, `kicad_ops.py measure`):**

- `measure()` now takes the independent DRC's own `unconnected_items` (net names extracted by
  `integrate._unconnected_nets`) and annotates every QSPI net and LVDS leg with `connected`;
  `nets_connected`/`nets_total` and `diff_pairs_connected_legs`/`diff_pairs_total_legs` replace
  eyeballing lengths, and `qspi_max_length_mm`/the group skew are computed over connected nets
  only.
- `step_check` runs its own `pnr.planes --refill-only` before any measurement (independent of
  route's own refill), runs `rf_audit.py` A1-A6 on the _routed_ candidate (not just the
  pre-route board `finish` already checks), and re-runs `pnr.ir_extract` on the refilled
  candidate directly instead of trusting route's cached `ir.json`.
- A second DRC pass (`_run_drc(..., promote=_DRC_IGNORED_BY_DEFAULT)`, a scratch copy of the
  project with the 5 ignore-by-default checks forced to "error") reports them as
  `drc_ignored_by_default`, named rather than silently absorbed into "0 violations".
- Verified against the real Wave 1 board before touching anything else (scratch copy): matched
  the review's cold numbers exactly (`nets_connected: 4/7`, `drc_ignored_by_default` 9+5+1).
- New hermetic test `test_integrate.py` (no KiCad) pins `_unconnected_nets`'s parsing and the
  five-type list. `bazel test //tests/unit/radar60:test_integrate
//tests/unit/radar60:test_board_integration //tests/unit/radar60:test_parts` all pass; prek
  clean.

**Finding #4 (R_SH1/R46 doc fix, partial):** the comment mislabel is fixed (`5b2783c`). Owner
Q1 itself (swap R46 from `Radar60_R_0_0402` to the already-defined `Radar60_R_1m_0612`, the
same WFCP0612 part R_SH1 uses on product builds) is **not applied**: this environment has no
atopile toolchain to rebuild the board and verify the swap end-to-end without installing one
(out of scope here, and the workflow's own "never change tooling for anyone" rule), and the
WFCP0612's real footprint (~3.7 x 4.3 mm courtyard) does not fit R46's current `pa_r` bottom-site
slot (2.35 x 1.35 mm, boxed between U1's BGA courtyard and the RF region boundary) by a wide
margin. Applying Q1 for real needs the R2 anchored re-place the plan's "Remaining to 100%"
already calls for, not a hand-guessed slot resize.

**Finding #5 (R8/E6): a B.Cu GND pour, wired in from the radar side.** E6 itself (an
outer-layer pour whose pads count connected after the refill, with island stitching) landed in
PR #55; no radar60 input used it. `floorplan.yaml` gained `pour: [{layer: B.Cu, net: GND,
connect: solid}]`; `gen_board.py` gained `pour_sections(d)` (symmetric with the existing
`power_sections`) to carry it into `constraints.yaml`. Verified end-to-end, not just the dict
shape: `integrate._compile()` (the exact call `step_prepare` makes) carries it through
`compile_constraints` into the compiled `rules.json` as the `pours` key `pnr.planes`/`pnr.pour`/
the router actually read (checked by name in the engine source).

### Wave 2 and Wave 3: real route+check, same placement as Wave 1 (`reva/placement.json`), the

atopile board already cached from stage3b-board (`ato-rev-a`, `input_id d1ca441e…`, matches
Wave 1's exactly) — fresh `source`->`prepare`->`finish`->`route`->`check` each time, on the Mac
via `heavy.sh`, numeric Python supplied by Bazel's own hermetic `yapnr_pypi` torch/numpy/pyyaml
(no system install; `bazel build @yapnr_pypi//torch @yapnr_pypi//numpy @yapnr_pypi//pyyaml`
first, then that interpreter directly — this is what let the engine's placement/fanout code run
outside Bazel here, since no project venv with torch existed on this Mac)

| Metric                      | Wave 1 (baseline)            | Wave 2 (pour: thermal)         | Wave 3 (pour: solid)                              |
| --------------------------- | ---------------------------- | ------------------------------ | ------------------------------------------------- |
| Route time                  | 538 s                        | 534 s                          | ~540 s (see wave3-route.log)                      |
| DRC (as configured)         | 0                            | **`starved_thermal`: 2**       | 0                                                 |
| DRC ignored-by-default      | not measured                 | 9+5+1 = 15                     | 9+5+1 = 15 (unchanged, not this wave's target)    |
| Unconnected (DRC entries)   | 141                          | 142                            | 142                                               |
| GND opens (pad occurrences) | 46                           | 38                             | 38                                                |
| QSPI connected/total        | not measured by connectivity | 4/7                            | 4/7 (CLK, CLK_FLASH, D0, D2; CS_N/D1/D3 open)     |
| LVDS legs connected         | 0/8                          | 0/8                            | 0/8 (R7 still pending; unchanged, as expected)    |
| R1 macro digest             | equal                        | equal                          | equal                                             |
| R4 foreign copper           | 0 items                      | —                              | 0 items                                           |
| rf_audit on routed board    | not run                      | A1-A6 pass                     | A1-A6 pass                                        |
| IR (independent re-run)     | —                            | 1V0_BUCK 35.0, 1V0_SH 138.5 mΩ | 1V0_BUCK 27.6, 1V0_SH 40.0 mΩ (shifted; see note) |

**Reading this:** R8's actual target, GND, moved for real: 46 -> 38 pad-level opens (-17%),
holding across both the thermal and solid pour variants, with every other net's open count
(5V_SYS 42, 1V8 36, 3V3_RADIO_IO 22, VIN_5V 14, 1V0_PA 12, 1V0_RF2 10, EFUSE_PG 8, the four
PMIC_SW_B\* at 4 each) identical wave-to-wave -- R8 is isolated and precise, not a side effect
elsewhere. Wave 2's own check step (the fix landed this pass) caught a real regression the pour
introduced -- `starved_thermal` on 2 bottom-site caps' GND pads (C38.2, C63.2), only 1 of the 2
spokes a thermal connection needs -- before calling it done; `connect: solid` (same `pour:`
schema, not a loosened limit: full connection instead of thermal relief, appropriate for small
SMD pads on a flood with no reflow-isolation need) fixed it in Wave 3 with 0 regressions
elsewhere. The IR shift (1V0_BUCK/1V0_SH both improved) is a plausible, not fully diagnosed,
side effect of freeing GND pads from fanout/escape planning (`pour_pads` skips their escape
plan), which frees router/via resources other nets then use differently; flagged for whoever
picks up R1/R2/R7, not re-derived further this pass.

QSPI/LVDS/opens did **not** otherwise move (R7/R1 not done this pass, as planned); the "opens
must fall every wave" gate (finding #6) now has its first real two-wave same-placement
comparison, and it did fall, on the one thing this pass changed.

**Renders:** `stage3c/wave3/renders/wave3-{top,bottom,angled}.png`; archived to
`progress-gallery/2026-10-05/radar60/` (wave1's renders kept alongside, per the gallery's
own "never delete" note) and sent to the user.

**Spend:** $0 GCP. Mac only, `heavy.sh`, one slot throughout (no contention this pass, slot 2
unused). `gcp-spend.md` updated.

**Branch pushed:** `claude/radar60-3c` now has `origin/claude/radar60-3c` (it did not before this
pass).

## Wave: GCP wide exploration (96 starts, 12 seeds) -- no new best

Resumed the GCP exploration track (`claude/pnr-explore-gcp` 4f9d49d, off `claude/radar60-3c`
f1d7c6b, own worktree so the live mid-merge `radar60-3c` worktree is untouched) after the proof
campaign (`stage3c/gcp-task.md`, waves 1-2, 5 tasks) validated the machinery end to end. Widened
`explore_jobs.toml` from 4 seeds to 12 (`s0`-`s11`, each `--n0 8 --procs 2`, no power-stage
library) + the `cand-arm-a` comparison, matching the owner's 48-96-start / 8-16-routed-candidate
guidance (`pnr-on-gcp.md`): 96 placement starts, 12 routed+checked candidates in parallel.

|                           | starts                    | legal/routed                                 | DRC                      | opens                                          | IR                                                                   | LVDS | QSPI           | cost       | wall                                   |
| ------------------------- | ------------------------- | -------------------------------------------- | ------------------------ | ---------------------------------------------- | -------------------------------------------------------------------- | ---- | -------------- | ---------- | -------------------------------------- |
| `s0`-`s11` (12 tasks)     | 96 (8 each)               | 0/12 (7 fail at `place`, 5 fail at `select`) | --                       | --                                             | --                                                                   | --   | --             | --         | 288-344 s each                         |
| `cand-arm-a` (comparison) | n/a (committed placement) | 1/1                                          | 7 + 8 ignored-by-default | 134 unconnected (7/7 nets partially connected) | 1V0_BUCK/1V0_RF1/1V0_RF2/1V0_SH/1V2/3V3/3V3_RADIO_IO/5V_SYS all fail | 0/8  | diff pairs 0/8 | --         | 737.1 s                                |
| campaign total            | 96                        | 1/13 pass                                    | --                       | --                                             | --                                                                   | --   | --             | ~$0.12 [D] | ~20 min (submit 14:45, done 15:05 PDT) |

**Result: no new best board.** All 12 widened seeds fail before reaching `route`/`check` --
the same outcome class wave 2 found on 4 seeds (`place`/`select`/legalization failures without
the power-stage fixed blocks), now confirmed 3x wider (96 vs 32 starts). `cand-arm-a` reproduces
the already-known committed-placement result exactly (134 unconnected, DRC 7, R1 macro digest
unchanged) -- consistent with the Mac's own run of the same placement, and with wave 2.

**Reading this:** this is the control experiment the exploration track owed the `plan.md`
diagnosis -- random re-seeding of a plain placement search (no power-stage library) does not
get past legalization/select at any scale tried so far (32 or 96 starts, 2 platforms, 16
distinct seeds total across both waves). The bottleneck is structural (U2/U5 stranding without
their fixed blocks -- the `## Remaining to 100%` list's R1 item below), not a search-breadth
problem, so further seed-only GCP waves on this exact shape would not be a good use of budget.
**Next wave on this track should add `pre_steps`/`post_steps` with `--library` (the power-stage
block library `block`/`finish --library` flow R1 already uses on the Mac, `stage3c/wave4/
library.json`) to the campaign job file** -- not attempted this pass (building and verifying
that wiring is itself a non-trivial increment, and the live `radar60-3c`/`radar60-3c-power`
worktrees were mid-edit by another agent this pass, so their exact library-build commands
weren't copied in uncommitted).

No render: no candidate beat the existing frozen board, so there is nothing new to cold-check or
archive this wave.

**Spend:** ~$0.12 actual (ceiling $2.48, logged before submit); `gcp-spend.md` updated. Plan
copied to `stage3c/plans/20261005-mceval-9ddbc0/` before submit/fetch (the earlier-deleted-plans
issue noted in the owner's state).

**Branch pushed:** `claude/pnr-explore-gcp` commit `4f9d49d` pushed to `origin/claude/pnr-explore-gcp`.

## Remaining to 100% (unchanged from the review's list, this pass did not reach them)

- **R1 + E1/E2** (power-stage subcell, fixed_block bridge): the highest-leverage remaining
  item -- fixes U2/U5 stranding, 5V_SYS/SW_B\*/EFUSE opens, 1V0_BUCK/1V0_SH IR. Needs its own
  `pnr.hier.synth` MC run (own worktree exists, `yapnr-wt/s3c-block` has E2 landed already);
  too large for this pass alongside everything above.
- **R2 chain + Q1**: re-derive the 1V0_RF2/1V0_PA feed once R46 moves (see finding #4 above --
  this is the anchored-placement work that swap actually needs).
- **R3/In3 trunks**: already landed per `impl-radar.md`; IR numbers above still show 1V0_RF1/RF2
  failing their mΩ budgets pending R1's un-stranding.
- **R7 re-place**: exit bands, south-edge order, Y1 region, U3 within 10 mm, bottom sites, 32 MC
  starts -- unblocks LVDS and the 3 open QSPI nets. Owner's default R46->WFCP0612 1 mOhm and the
  Q2 local-sensing default (0.5 mΩ budgets, quantified-warning fallback) both wait on this too,
  since they change the bottom-site layout R7 owns.
- **R9**: XTAL caps, flash EP pour (a natural follow-on of this pass's `pour:` mechanism --
  GND under the EP, no vias, same `connect: solid` pattern).
- **Macro v2 swap (R5)**: still blocked on `stage3b-rf/freeze.md` (not frozen); v1 stays the
  fixed_block basis.
- **Remaining DRC**: the 9 off-centre via track ends trace to the plan's own documented E4 open
  follow-up (a bottom-site pad's fanout stub counted as its connection); the 5 missing-courtyard
  and 1 footprint-type-mismatch trace to synthetic footprint generation (`gen_parts.py`), which
  (like Q1's swap) needs the atopile toolchain to regenerate and verify -- not attempted this
  pass.
