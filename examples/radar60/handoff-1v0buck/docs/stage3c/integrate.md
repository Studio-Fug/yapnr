<!-- markdownlint-disable -->

# Stage 3c integrate — engine merge, PR, Wave 0

## Engine merge (`claude/s3c-engine`, off `origin/main` d3159ac)

Merged `claude/s3c-pour` (fast-forward, E1/E3/E4/E6), `claude/s3c-pairs` (two conflicts: both
tracks added an independent block right after the same `plane_partition`/pair-layer setup in
`router.py`/`hard_rungs.py` — kept both, in sequence) and `claude/s3c-block` (clean). Commits:
`00f25a5`, `f49af38`, `028b13a` (a real bug the merge's own KiCad run found: `hier_assemble_kicad`'s
duplicate-group fixture built an _empty_ `PCB_GROUP`, which KiCad drops on save/reload, so the
collision it meant to test never round-tripped; gave it a member — `assemble.py`'s own check was
already correct), `0c8be7e` (a second real bug, from CI: `test_stack.py`'s
`test_dedicated_planes_are_the_judges_no_track_layers` assumed every `plane_partition` entry names
a dedicated plane layer, but an outer pour (E1, `region`) sits on a signal layer that stays
general-purpose outside its region — the new `buck-vqfnhr-pour` rung now exercises that path and
exposed the stale assumption; fixed the test, not the engine).

**Tests:** 30 curated Bazel targets (every new/changed module plus `fixed_block`, `hier_case`,
`fanout`, `compact`, `legalize_*`, `gp_polish`, `region_align`, `sides_regions`,
`regression_contract`, `trace`, `line_group`, `keyhole`, `length_tune`, `fanout_route`,
`constraints`) pass locally, `--local_cpu_resources=2 --config=lowmem`, run in small batches (one
action-graph conflict between two targets sharing a pyc artifact, worked around by batch size, not
investigated further). The full `//hardware/pnr/...` suite was not run locally — it has exhausted
this Mac's disk before (per the prior pass); CI's own `test`/`test-macos` jobs cover it, and that is
where `stack_test` actually failed (not in the curated batch) and got fixed. All 5 real-KiCad tests
(`outer_pour`, `pour`, `plane_partition`, `library_table`, `hier_assemble`) now actually run against
KiCad 10.0.6 `pcbnew` (not just collected/skipped) and pass, 5/5.

**Identity regression** (Mac, `run.py`, both arms = `origin/main` vs `claude/s3c-engine`, no GCP —
disk was already tight; `PYTHONHASHSEED` fixed after discovering raw file SHA-256 is not a valid
identity check here, since KiCad writes random item UUIDs and even two runs of the _same_ commit
differ byte-for-byte; a canonicalizer that strips UUIDs and sorts each block kind as a multiset
confirmed two same-commit runs are then identical): the full gate (8 cases) + all 4 showcases, 2
seeds, both arms — **24/24 canonically identical**, confirming undeclared behavior is unchanged.
`11-ufbga201-fanout-6L-SGSGPS-rails` was also run both arms/seeds as a "one family" hard-rung check,
but turned out not to be a valid control: E4 itself edits that rung to add `protect_fanouts`, so
main's and candidate's specs for it differ on purpose (this is the documented main-vs-E4 fix, not a
regression) — excluded from the identity verdict rather than reported as a false alarm.

**PR #55**, pushed and opened. CI: `plan`/`lint`/`docs`/`image`/`ladder`/`run the ladder` green. The
full `test` job caught `stack_test` (above), fixed and pushed; `test`/`test-macos` re-running as this
is written. GCP: $0 (Mac throughout; the program ledger is unaffected).

## Wave 0 (`claude/radar60-3c`, merged `claude/s3c-engine` in — real conflicts: engine files PR #49

added both as a squashed `origin/main` commit and, independently, as 3b's own earlier unsquashed
snapshot of the same branch; resolved per-file by diff against each side's real history rather than
git's confused add/add, taking the strict superset — verified no radar-only fix was dropped)

Ran `integrate.py route` then `check` on the existing arm-A placement (stage-3b's own `work`/`board`,
copied to scratch, handoff tree untouched), through the merged engine, via `heavy.sh`:

- **route**: 874 s (above the ~494 s estimate — contended with the identity regression's two
  concurrent runs on the shared Mac). 48 nets unrouted (plan's baseline: ~46) — **within the ±5
  gate**. The internal validate rejected (expected: still-unrouted nets fail the baseline-vs-candidate
  DRC gate) and was recorded, not raised, exactly as `step_route` documents.
- **check**: R1 macro digest equal; QSPI max 30.5 mm (budget 25); LVDS TX0/TX1 skew ~22/24 mm
  (unrouted); IR: 1V0_BUCK 112 mΩ, 1V0_SH 70 mΩ against 0.5 mΩ (matches the known pre-R_SH-fix
  numbers, live and quantified); every 1.0 V/3.3 V/5 V rail besides those two still `open`.
- `unconnected: 187` in `measure.json` — **not** a true regression from 142: every one of the 114
  distinct part refs in `unconnected_items` is also a part in `lib_footprint_issues` (checked
  directly: 0 unconnected-only refs), i.e. the board's own `finish` snapshot predates `681913d`'s
  fp-lib-table fix, so KiCad can't resolve those footprints for connectivity either. A wave that
  re-runs `finish` (regenerating the table) is needed for a clean unconnected count; this is a
  measurement artifact of reusing the old `finish` output, not a Wave-0 engine regression.

## What remains

Drive `test`/`test-macos` fully green and watch for further CI-only findings (one has already
surfaced and been fixed this way). Re-run `finish` on `radar60-3c` to pick up `fp-lib-table` before
the next wave's `unconnected` count means anything. R1/R4/R5/R7/R8/R9 and the LVDS-coupled/pour
declarations on radar60 itself are all still open, per plan.
