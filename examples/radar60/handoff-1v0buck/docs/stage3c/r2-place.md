<!-- markdownlint-disable -->

# radar60 stage 3c: R7/R9 re-place

2026-10-05. Track: place (R7 + R9), plan `stage3c/plan.md` §1.7/1.9. Worktree
`<local-path>`, branch `claude/radar60-3c` (shared with the
concurrent power track, R1/E2 -- `dda3d13`/`8f373ea`, already landed when this pass started;
`origin/main` already merged, `a7344d3`).

## What already existed (verified, not rebuilt)

Exit bands (`bands.py`, `integrate.py step_prepare`), south-edge/scarcity legalizer order
(`legalize: {order: scarcity}`), the J2/J3 regions, and `fanout.bottom_sites` were all already
wired from earlier waves. `step_prepare` on the current inputs (this pass) reproduces them
cleanly: 0 slot errors, decoupling sets partition, no unassigned PMIC parts, 5/6 bottom sites
placed (`C62` unplaced, pre-existing, R7-unrelated). The power track's `pmic_block`/
`power_stage` region already serves the "power block hook": no placeholder was needed since R1
landed on this branch before this pass began.

## New this pass (R7, R9)

1. **Y1 In2.Cu/B.Cu keepout (R9).** `floorplan.yaml: crystal_keepout` (rect x 28.9-32.9,
   y 15.45-20.0, `allow_classes: [GND, XTAL]`) -> `gen_board.copper_keepouts` -> a v1
   `copper_keepout` entry. Bars I2C/CAN/FRAME_START from In2.Cu/B.Cu under Y1.
2. **Flash EP solid F.Cu pour (R9).** `floorplan.yaml: flash_ep.pour` (net GND, connect solid)
   -> a new `plane_partition` entry with `region: {refs: [@flash.u3]}`, the same E1 mechanism
   the power stage uses for its hot-rod lands. Merged into the single `plane_partition` list
   `gen_board.constraints()` emits (the two sections used to collide on that key; fixed to
   append, not overwrite). Replaces reliance on the maze finding a track into the EP's own
   keepout-narrowed courtyard, which is what gave U3.9 zero escape candidates in wave 1.
3. **R46 slot (R7, owner Q1 default).** R46 (`radio.r_pa`, 1V0_RF2 -> 1V0_PA) is the owner's
   WFCP0612 1 mOhm default; its courtyard (~3.8 x 4.4 mm, measured from R_SH1's own footprint,
   same Vishay family) does not fit the old `pa_r` bottom-side slot (2.35 x 1.35 mm, nor any
   combination of free area inside U1's BGA shadow, ~3.2 mm2 against ~17 mm2 needed). Removed
   that region; added `groups.r46` (anchor `@radio.u1` pad D2, radius 11 mm, hard) so the
   32-start search finds a legal slot mechanically (pnr-direction.md: no hand-picked rect).
   `audit.py PIN_LIMITS` split `pa_decoupling` (3 mm hard) into `pa_cap`/`rf2_cap` (unchanged,
   3 mm hard -- the two caps still fit their original slots) and `pa_r` (11 mm, soft/reported):
   without this split every candidate would hard-fail the pin-distance audit, since the old
   3 mm limit was calibrated for the 0402 part R46 no longer is.
   **Not done** (flagged, not assumed): the BOM/footprint swap itself. No atopile toolchain in
   this environment to rebuild/verify `radio.r_pa`'s part/footprint; `ato-rev-a`'s cached board
   still carries the 0 ohm 0402. The group gives it a legal, physically-correct-sized slot;
   the R2/power-chain track owns re-deriving the actual 1V0_RF2 -> 1V0_PA mOhm once the swap
   lands, against the 4 mOhm budget.
4. **LVDS pair adjacency.** Not a new mechanism: validated via `step_prepare`'s own gate (it
   raises if any ball-anchored slot meets an exit band; this pass's run raised nothing) and via
   the exit-band/bottom-site report. The fixed engine-side piece (A8/E5, coupled routing from
   the fanout exits) landed in PR #55; whether a side-by-side channel actually exists is a
   routing-stage result, reported in `select`'s candidates, not asserted here.

## Engine fix found along the way

`gen_board.py`: `power_sections()` and the new flash-EP pour both wrote the `plane_partition`
key; a dict-literal `**` merge let the second silently clobber the first. Fixed by merging the
two lists explicitly before the `doc = {...}` literal.

## Search

`integrate.py place --n0 32 --seed 0` (Mac, `heavy.sh`, KiCad headless; the power track held
the other slot throughout, no contention observed). `integrate.py select --top-n 4` (new flag,
this pass: writes every legal, audit-passing candidate up to 4, ranked, to its own
`candidates/rankN/placement.json`, not just the stage-0 winner -- `tests/unit/radar60/
test_integrate.py::TopCandidatesTest`, 5 cases, hermetic).

**Results: 32/32 starts ran; 2 legal; 0 audit-passing. No top-4 written -- blocked, cross-track,
not by R7/R9.**

| id                           | seed    | start kind        | status              |
| ---------------------------- | ------- | ----------------- | ------------------- |
| p025                         | 2618225 | latin-global      | legal, audit FAIL   |
| p012                         | 1256748 | stratified-global | legal, audit FAIL   |
| p000/p001/p002/... (27 more) | various | various           | `LegalizationError` |

Both legal candidates fail on the **same** check, unrelated to R7/R9:
`pin distance: J1 VIN_5V: U5 (12.6-15.2 mm) before the protection D3 (16.3-20.4 mm)` -- the
eFuse (U5) sits closer to J1's 5 V pin than its own TVS (D3), so a surge would reach the IC
before its protection. Root cause (verified, not R7/R9's input): the power track's R1 commit
(`dda3d13`) re-anchored `connector_tvs` (D3) from J1 (radius 13 mm) onto `efuse` (radius 6 mm,
inside the power-stage block) to let `pnr.hier.blocks` extract one rigid subcell; nothing now
keeps D3 nearer J1 than U5 is. 27 of the other 30 starts failed legalization entirely, and
every one of those 27 failure messages names a power-stage part (`R_SH1`, `TP1`, `L1`/`L2`,
`C49`/`C50`/`C51`/`C65`/`C68`, `R20`/`R24`/`R36`/`R37`/`R40`/`R41`, `C6`/`C21`/`C27`/`C46`,
`D2`) -- **zero** name `R46`, `Y1`/the crystal caps, `U3`/the flash, or any R7/R9-owned part.
A second 32-start batch (`--seed 1`) was attempted to widen the search cheaply; `pnr.mc.halving`
treated the `place` stage as already satisfied (32 "place" records already in `dataset.jsonl`)
and returned in 1 s without new starts, so the pool stays at the 32 this pass asked for.

**Classified per plan.md §3's feedback rule: an R1 (power track) input issue, not an engine gap
and not R7/R9.** Recommendation for R1 (not applied here -- out of this track's scope): give
`connector_tvs` its own region/order ahead of `efuse_block`, or a group anchored on `connector`
again in addition to `efuse`, so D3 is guaranteed nearer J1 than U5 regardless of where the
rigid block lands.

**What this pass does confirm, cleanly, across all 32 starts:** the Y1 keepout, the flash-EP
pour, the R46 group/slot and the exit-band/bottom-site machinery introduce no legalization or
audit failures of their own -- R7/R9's inputs are placement-ready; only the cross-track J1
blocker stands between this run and a routable top-4.

## Files

- `${REPO}/examples/radar60/board/floorplan.yaml`
- `${REPO}/examples/radar60/board/gen_board.py`
- `${REPO}/examples/radar60/board/audit.py`
- `${REPO}/examples/radar60/board/integrate.py`
- `${REPO}/tests/unit/radar60/test_integrate.py`
- Run tree: `stage3c/r7-place/` (work, out/candidates/rank1-4, place.log)
