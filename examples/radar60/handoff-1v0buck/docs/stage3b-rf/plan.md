<!-- markdownlint-disable -->

# radar60 stage 3b RF: plan

Date 2026-10-04. Branch `claude/radar60-rf3b`, created from `claude/radar60-rfuni` at 49be425 in the
worktree `yapnr-wt/radar60-rf3b`. Work stays in `examples/radar60/rf/` and its results. Nothing is
pushed, and every commit must pass prek. The example is public: no TI design files and no machine
paths. Labels: [D] means analytical or geometric, [S] means solver prediction. Nothing is measured.

**Inputs.**

- Owner decisions D14 and D15 (`board-design.md`, end of the file).
- rf-uniform `REPORT.md`, `em-after.md`, `design.md` and `fix/em/out/summary.json`.
- Stage-3a §5.3: A2/B2 have no legal via site under the current rules.

**Palace check (plan time).**

- `palace/READY.md` exists (06:43), but its status line is the unfilled placeholder `READY_STATUS`.
  Several review-fix campaigns are still pending.
- `yapnr.rf.palace.config` writes only `Driven` and `BoundaryMode`. The image does include ARPACK,
  so eigenmode can run.
- A bank cannot be one Palace run yet (READY §6.4).
- **Plan:** check READY.md again when the EM stage starts and again before freezing.
  - If it is READY: use Palace for eigenmodes (with a local Eigenmode writer in
    `rf/palace/eig.py`) and for driven cross-checks of the feed and the cell.
  - If it is not: use openEMS ring-down probes plus the analytical post-lattice model, and say so
    in the results.

## 1. Generator changes (`rfmacro`, all declared parameters with sources; every option builds, fills and passes DRC)

| #   | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Parameters                                                                                                                                                                                                                                                                                                                                                                                                                                                           | New/extended checks                                                                                                                                                                                             |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **D14 PA feed** owned by the macro. A2 and B2 are joined on L1; the copper leaves east of row A, between A1 and A3, keeping ≥ 0.10 mm from the VSSA lands and TX1's 0.70 mm launch ground. It runs north along the body edge into the pocket (U1 frame x 3.56–5.25, y 5.45–6.20 under S2). Vias in the pocket go L1→L6 and tie to the L4 1V0 pour; the bottom-side caps sit on them. L2, L3 and L5 get anti-pads. A2/B2 are exported in `skip_pads` for `merge_macro`                        | `pa_feed` on/off, `pa_i_peak` 2.5 A (whole 1.0 V rail [TI-DS]; no PA split is published, so the feed is sized for all of it), `pa_via` class (0.20/0.40 default or 0.30/0.55 power, both existing board rules), `pa_ir_max` 6 mV                                                                                                                                                                                                                                     | **G8:** clearances; IR drop; current per via ≤ 1.0 A peak [D]; the vias fit the pocket; L2 anti-pads ≥ 0.5 mm from feed centrelines; the GND round the island is stitched (G3); no lattice via lost (G5)        |
| 2   | **D15 open-pour stitching.** Fences stay at 0.45 mm pitch, and the stitch-or-remove rule stays for edges and fingers. Three pour variants: **A** today's 0.60 mm grid; **B** a 1.00 mm grid plus fill vias wherever the hard maximum is exceeded; **C** no L1 pour in the antenna area except the GCPW ground strips (gap edge to fence row + 0.1 mm), the ring and the isolation strip. The L2–L3 lattice becomes a separate parameter (through vias, placed as the union with the L1 vias) | `pour_mode` grid/sparse/strips, `stitch_grid` 0.60/1.00, **`stitch_dmax`** 0.45 (A) / 0.75 (B) for interior pour, `stitch_reach` 0.45 for edges (GND within 0.3 mm of a gap or cut-out), `l23_pitch` 0.60                                                                                                                                                                                                                                                            | **G3** gets two thresholds. A new metric `stitch_metrics` reports the geodesic dmax (10 µm raster), the area above 0.45/0.60/0.75 mm, vias per role, the largest L2–L3 lattice cell and the [D] lattice cut-off |
| 3   | **Dummies.** The open-end dummies RXD0 and TXD4 stay. **S1** = open-end only; **S2** = all four; optional **S1.5** = TXD0 kept and RXD5 dropped. Termination is the fitted 0201 by default, or `open`. `short` is refused                                                                                                                                                                                                                                                                    | `dummies` none/outer/outer+txd0/both, `dummy_term` load/open                                                                                                                                                                                                                                                                                                                                                                                                         | G7 unchanged. G2 open-end report per option. The pocket is re-derived (S1 removes RT2's zone, so the PA vias get more room)                                                                                     |
| 4   | **L2–L3 bondply cavity**                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | `l23_cavity`: **K0** ring only (today); **K2** posts: L2–L3 through vias round the window groups wherever L1 is free (bank ends, column gaps north of the top patch, the strip between the patch pair outside the divider corridor), L1 pads ≥ `post_clear` 0.60 mm from patch copper (stage 2 saw pads detune patches), at `post_pitch` 0.45–0.60; **K1** solid L2 under the patches (`windows=False`)                                                              | **G9:** L2–L3 cavity cells: the largest via-free span per bank, the posts' clearance to patches                                                                                                                 |
| 5   | **C1 column parameters**                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Patch `fullwave_l_scale` (0.967 → 0.98–1.04), `inset` 0.25–0.40, `w35` 0.30–0.45, `t_y` 0.25–0.55, a new `div_l_scale` (λ/4 and arm lengths). Also `column` corporate/series, which puts the RFS-4S cell from `coupons.py` into the bank                                                                                                                                                                                                                             | The cut-out stays tied to the cell frame, so G6 (D12 hash) holds. A series column has no in-gap input, so G2's open-end mask goes away                                                                          |
| 6   | **TX feed options**                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | `tx_eq`: **T0** fingers R 0.50 (today); **T1** accordion equalizer for TX2 (design §5: E lower by about 1.0 mm, TX path +3.9 mm instead of +4.86 mm); **T2** R 0.75 fingers. `tx_alloc_perm` tries other ball-to-column orders [D]. I expect only the nested order to be planar on L1, so the check should report "crossing". `tx_skew_budget_ps` is 0 by default; T4, partial equalization within the 7.9 ps limit, needs an owner waiver of the 2 ps design target | G1–G7 per option. Skew and length are reported                                                                                                                                                                  |
| 7   | Tests                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Unit tests for every option. Regression fixtures fail G3 (B without fill) and G8 (an undersized PA via set). Board-audit mirrors (A7/A8) go to the integration track, which is outside `rf/`                                                                                                                                                                                                                                                                         | —                                                                                                                                                                                                               |

**[D] numbers to record before any EM run:**

- dmax, area histogram and via count for each of A, B and C × S1/S2.
- Post-lattice parallel-plate cut-off: about 120 GHz at 0.60 mm and about 72 GHz at 1.00 mm, which
  is marginal for B (needs ≥ 70 GHz).
- PA feed: about 3.5 squares of 35 µm copper (~1.7 mΩ) plus 3–4 vias of 0.20/0.40 (~1.3 mΩ each),
  so IR drop ≤ 6 mV at 2.5 A. That counts against the 28 mV rail budget.
- Loop inductance of the PA vias.

## 2. EM experiment matrix

**openEMS (all runs):**

- Shape: one model × 8 threads per c4d-highcpu-8.
- `exact-endcriteria` at −40 dB, and frequency-domain NF2FF at 60.3/62.05/63.8 GHz only.
- No time-domain dumps. Bondply dumps only as one decimated plane at 3 frequencies. 16 GB disk.
- Column-periodic mesh anchored per bank, with one shared mesh template per comparison (the union
  of the variants' edges). Without it, two mesh lines moving inside a port lead shift Γ by 0.4.
- Each comparison stays on one machine family.

Costs: about $0.08 per VM-hour [D]. Base sizes are measured [S]: two-bank board 16.0 M cells at
40 µm (2.1–2.3 h); cell 1.5/2.7/4.1/6.3 M at 40/27/20/15 µm.

| ID          | Model (size)                                                                                                                                                                | Variants × drives                                                                    | Mesh                                | Solver / pool                         | Runs | Est. $ | Wall              |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ----------------------------------- | ------------------------------------- | ---- | ------ | ----------------- |
| **E1-BANK** | Two banks, dummies, loads, PA feed, one board (16 M; S1 ≈ 14.6 M)                                                                                                           | D15 A, B, C at S2/K0 × 7 drives; S1 (pour A) × 5 drives (TX1–3, RX3, RX4)            | 40 µm                               | openEMS, us-west4 C4D, 8 VMs          | 26   | 4.9    | ≈ 8–9 h (4 waves) |
| E1-C1a      | Triplet: centre column driven, neighbours terminated at Pg, run-in, entry, pour, ring (4.3 M)                                                                               | Corporate DOE: 15 L-scale × inset points, then 9 w35 × t_y points round the best     | 27 µm                               | openEMS, Montreal C4                  | 24   | 1.2    | ≈ 3 h             |
| E1-SER      | Same triplet, series-fed (RFS-4S)                                                                                                                                           | 8 points (L scale, link, inset)                                                      | 27 µm                               | C4                                    | 8    | 0.4    | ≈ 1.5 h           |
| E1-CAV      | Triplet with its own ring and real L2–L3 extent                                                                                                                             | K0, K2, K1 at 27 µm; K0 and K2 at 20/15 µm                                           | 27/20/15                            | C4                                    | 7    | 1.0    | ≈ 3.5 h           |
| E1-PP       | Plane-pair unit regions (4 × 4 mm stitched pour over L2/bondply/L3, weak probe, ring-down)                                                                                  | A, B, C                                                                              | 27/20/15                            | C4                                    | 9    | 0.2    | < 1 h             |
| E1-TX       | TX1 feed alone, P0→P1 (≈ 3 M)                                                                                                                                               | T0, T1, T2, S1-length, T4-length × lossy and lossless                                | 20 µm                               | C4                                    | 10   | 0.35   | ≈ 1 h             |
| E1-PA       | PA corner: TX1 and RX4 launches plus 3 mm of feed, PA island, vias through L2/L3 (≈ 4 M)                                                                                    | Via ends open and shorted (bracketing the bottom caps) × with and without the island | 20 µm                               | C4                                    | 4    | 0.2    | ≈ 1 h             |
| E1-EIG      | Palace Eigenmode, 55–70 GHz                                                                                                                                                 | A/B/C unit cells; the L2–L3 bank cavity at K0/K2 (1–2 M unknowns)                    | order 2 plus one refinement         | Palace, c4-standard-16 (2 slots)      | 5    | 0.5    | ≈ 1 h             |
| **E2-C1b**  | Triplet, top 2 corporate + top 1 series                                                                                                                                     | Mesh trend; Dk ±0.10 on the winner                                                   | 27/20/15 µm (15 µm ≈ 10 M, ≈ 2.2 h) | openEMS, us-west4 C4D (free after E1) | 11   | 1.25   | ≈ 3 h             |
| E2-TX       | TX1 feed, T0 vs the winner, lossy and lossless; Palace driven TX1 (2 M unknowns)                                                                                            | —                                                                                    | 15 µm / Palace                      | C4D + Palace                          | 5    | 0.45   | ≈ 1.5 h           |
| E2-PAL      | Palace driven, single cell, C1 winner (≤ 4 M unknowns)                                                                                                                      | —                                                                                    | order 2 plus refinement             | Palace                                | 1    | 0.3    | ≈ 1.5 h           |
| **E3**      | Final configuration: chosen D15/S/K, retuned column, TX option, PA feed. Full bank × 7 drives, plus an isolation sub-model (TXD0–TX2 + RX3–RXD5 + strip, ≈ 15 M) × 2 drives | —                                                                                    | 40 µm (bank); 27 µm (isolation)     | C4D                                   | 9    | 1.85   | ≈ 3 h             |

**Parallelism:**

- E1-BANK takes the whole us-west4 pool.
- All small E1 models run at the same time in Montreal: 6 C4 VMs plus one Palace VM.
- E2 starts when E1-C1a, E1-SER and E1-TX finish; E3 starts when E2 finishes.

**Total and wall time:**

- About $12.6 expected, ≈ $13.9 with a 10 % preemption reserve, against the $15 workflow cap.
- Every submit ceiling is under $5 (the bank is submitted per variant, ≈ $2 each). Each submit gets
  a row in `gcp-spend.md` first.
- **Kill rule:** stop submitting when actual spend plus open ceilings would pass $15. Drop work in
  this order: E2-PAL, the Dk corners, the second drive of the isolation sub-model.
- Wall time: about 9 h (E1), 3 h (E2) and 3 h (E3), plus the generator day and analysis. That is
  about 1.5–2 days elapsed, or more if other tracks hold the quota.

**Mesh limits:**

- A two-bank model at 27 µm (≈ 35 M cells, ≈ 6 h) exceeds the 4 h task limit.
- So bank-level isolation, active RL and patterns stay [S, 40 µm, shared mesh template], with a
  27 µm two-point check on the isolation sub-model.
- Fine-mesh (27/20/15 µm) trends cover the decisions that depend on resonances: the column, the
  cavity, the plane-pair modes and the feeds.

## 3. Decision rules (applied mechanically; ties go to the cheaper or simpler option)

- **D15 pour (A/B/C).**
  - Hard gates:
    - worst TX→RX isolation over 60.3–63.8 GHz ≥ 35 dB (required ≥ 27 dB);
    - no L1–L2 or L2–L3 mode with Q ≥ 20 in 57–70 GHz (Palace eigenmode, or the 27/20/15 µm
      ring-down);
    - G3 dmax holds.
  - Rank by worst isolation, then worst active RL (0/±15/±30/±45° codes, both banks), then the
    edge-to-interior ±45° pattern spread.
  - Ties: within 1 dB in isolation and 0.5 dB in RL and pattern (the resolution of a shared mesh
    template at 40 µm). Among tied variants, pick the fewest 0.15 mm drills.
  - E1 uses today's mistuned column, so E3 re-checks the winner with the retuned one.
- **S1/S2.**
  - Choose S1 unless S2 improves TX1's or RX4's worst active RL or ±45° pattern spread by ≥ 1 dB,
    or S1 leaves isolation under 35 dB.
  - S1 saves ≈ 0.24–0.3 dB of TX loss [D], two loads, and enlarges the PA pocket.
  - If only TX1 benefits (as rf-uniform saw: TX1 0.069→0.024, RX4 0.024 with and without), take
    S1.5 and confirm it in E3.
  - Loads stay fitted and are never shorted.
- **L2–L3 cavity.**
  - **K0** if E1 K0 passes the D15 gates and the bank cavity has no mode with Q ≥ 20 in 57–70 GHz.
  - Otherwise **K2**, if the triplet trend shows the bondply-mediated coupling falls ≥ 6 dB while
    the column's RL and gain move ≤ 0.5 dB.
  - **K1** only if C1 can still reach RL ≥ 10 dB on solid L2. I expect it cannot: about 2.1 % BW
    [D]. Otherwise the cavity is recorded as a quantified warning.
- **C1 retune target.** Embedded centre column, port at Pg, 15 µm:
  - |S11| ≤ −10 dB over 60.3–63.8 GHz;
  - RL-10 band centre at 62.05 ± 0.3 GHz, moving ≤ 0.5 % between 20 and 15 µm;
  - realized broadside gain ≥ 5 dBi at all three frequencies, and efficiency ≥ 0.6.
  - If the full band is out of reach, maximize the minimum in-band RL with the band centred.
  - D12 then brackets ±1.8 % around the new length. The Dk ±0.10 result is reported as a range.
  - The DOE ranks at 27 µm; selection is confirmed on the 27/20/15 trend.
- **TX feed.**
  - Lowest TX1 P0→P1 loss at 15 µm, with Palace agreeing within 0.2 dB or the difference explained.
  - Constraints: no dip > 0.1 dB in 54–70 GHz, skew ≤ 2 ps at 62.05 GHz, TX imbalance ≤ 0.75 dB,
    coupling to RX/RXD5 ≤ −40 dB, G1–G8 pass.
  - T3 counts only if the generator finds a planar order; T4 only with an owner waiver.
  - RF-03 is reported as is. Even TX3 with no meander is 1.33–1.65 dB P0→P1 [S], so the D6 waiver
    (≤ 2.7 dB) stays unless the numbers say otherwise.
- **D14 PA feed.**
  - Accept when G8 passes and, for both via-end brackets, the island couples ≤ −40 dB into TX1 and
    RX4 and changes their S21 by ≤ 0.05 dB and 1°, with no dip > 0.1 dB.
  - Otherwise fence the island with GND vias and re-run E1-PA.
- **D5.**
  - Recommend corporate if C1 meets its target, or misses only one band edge by ≤ 0.3 GHz.
  - Recommend series-fed if corporate misses and series meets the target, or beats corporate's
    in-band minimum RL by ≥ 3 dB at ≥ 5 dBi. Series-fed has side benefits: no in-gap input, so the
    open-end asymmetry and the need for dummies go away, and K2 posts become easier.
  - If both miss, keep corporate (it is the required topology) and preregister the ANT-02 RL miss.
  - **Current lean: corporate.** Its embedded RL-10 band is already ≥ 3.4 GHz wide (62.6–66.0 GHz
    in the bank [S, 40 µm]), against a 3.5 GHz band. Its problem is the centring, which C1 fixes.

## 4. Order of work

1. Generator changes and tests; build, fill and DRC every variant; record the [D] numbers; commit.
   Mac only (heavy.sh for KiCad fills).
2. Check READY.md again. Write the Palace eigen writer, or record the fallback. Submit E1, both
   pools at once.
3. Apply the D15, S, K, TX and PA rules. Run E2 (C1 trend, TX sign-off). Check Palace again before
   freezing.
4. E3 on the final configuration. Regenerate rfm1-m/n/p and the coupons; update `board_frame`.
5. Write the notes in `stage3b-rf/`. Archive the figures in progress-gallery.
6. Hand-offs to integration (`merge_macro`): the PA feed (A2/B2 `skip_pads`, bottom caps on the
   pocket vias, the L4 tie), the dummies and loads, and the mask islands.
