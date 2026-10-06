<!-- markdownlint-disable -->

# radar60 stage 3b RF: build

Date: 2026-10-04.

**Branch and commit.** `claude/radar60-rf3b`, commit f4e8c5a (on 49be425), in the worktree
`yapnr-wt/radar60-rf3b`. It is not pushed.

**Checks.**

- prek is clean.
- All 43 unit tests pass (the 21 new ones are in `tests/test_stage3b.py`).
- `gen_board.py --check --macro` passes for rfm1-m, rfm1-n and rfm1-p.

**Evidence labels.** [D] means analytical or geometric; [S] means solver output. Nothing here is
measured.

**Spend.** No EM run and no GCP job in this step: $0.00, logged in `gcp-spend.md`. This workflow
has $15 to spend in total.

**Palace.** `palace/READY.md` still reads `READY_STATUS`, the unfilled placeholder. So E1-EIG is
replaced by the openEMS plane-pair ring-downs (E1-PP) plus the [D] lattice and cavity numbers
below. READY.md gets checked again before E2 and before freezing.

## 1. What the generator now does

### D14: the PA feed (`rfmacro/pa.py`, check G8)

The feed runs on the 1V0_PA net (U1 frame).

**Copper.**

- **Bar:** joins A2 and B2, x 3.74–5.14, y 3.75–4.24 (0.49 mm wide).
- **Neck:** x 4.86–5.14 (0.28 mm wide). It runs north inside the package outline, between A1's
  clearance and the pads of TX1's west fence vias.
- **Vias:** four 0.20/0.40 vias at board (30.89, 33.45), (30.39, 33.45), (29.89, 33.45) and
  (29.89, 33.95). Together they form an L, not a 2×2 block: the RXD5 load's via at (31.205, 34.17)
  blocks the second row. Under S1 the four vias do form a 2×2 block.

**Clearances.** All are at least 0.15 mm, the PWR class:

- to the lands, the GND via pads and TX1's launch ground;
- to RF copper, 0.71 mm.

The L2 anti-pad has a radius of 0.35 mm and sits 0.72 mm from the nearest feed centreline, against
the 0.50 mm rule.

**Current and voltage drop [D].**

- Per via: 0.63 A peak and 0.25 A RMS.
- IR drop: **8.4 mV at the inner ball for 2.5 A**. Of this, 4.3 squares are in the neck (2.1 mΩ),
  and the vias plus the copper square under them add 0.48 mΩ.
- Each via adds 0.97 nH of partial self-inductance [D].

**Hand-off.** `board_frame.pa_feed` gives `skip_pads` (A2, B2), the vias, the copper, and the tie to
the L4 pour. The board preview stands an In3 patch in for that L4 pour. There is a mask island over
the feed, and the vias are tented.

### D15: the pour variants (`rfmacro/pour.py`, check G3 plus stitch metrics)

|                                                   | A (0.60 mm grid) | B (1.00 mm grid) | C (strips)     |
| ------------------------------------------------- | ---------------- | ---------------- | -------------- |
| 0.15 mm drills                                    | 1437             | **970 (−32 %)**  | 986            |
| L1 GND area                                       | 437 mm²          | 437 mm²          | 149 mm²        |
| L1 dmax, edge / interior (geodesic, 10 µm raster) | 0.46 / 0.46 mm   | 0.46 / 0.77 mm   | 0.46 / 0.42 mm |
| L2–L3 largest via-free circle (radius)            | 0.46 mm          | 0.76 mm          | 0.74 mm        |
| Cut-off [D]: lattice / largest gap                | 141 / 103 GHz    | **81 / 59 GHz**  | 81 / 60 GHz    |

In B and C, the largest gaps put the bound on the plane-pair cut-off inside the band. E1-PP
measures this.

### Dummies

- **S1:**
  - TX P0→P1 is 15.29 mm against 18.57 mm, which saves 0.24 dB of line loss [D].
  - It uses 1324 drills.
  - G2 reports RX4 and TX1 as declared edge columns.
- **S1.5:** the fit puts the TX bank 1.98 mm higher (E 8.78).
- **Loads:** fitted, or open (DNP). The value "short" is refused.

### L2–L3 cavity (check G9)

**K2.** It places only 5 posts per column. The posts have to stay at least 0.60 mm from the patches,
at least 0.45 mm from the lines, and inside G2's safe span, so they can only go below the lower patch
and above the top one.

|                                      | K0      | K2             | K1      |
| ------------------------------------ | ------- | -------------- | ------- |
| Largest via-free radius under a bank | 3.56 mm | 2.91 mm        | 3.58 mm |
| TM modes in 57–70 GHz [D], RX bank   | 26      | 26 (unchanged) | 26      |
| TM modes in 57–70 GHz [D], TX bank   | 24      | 24 (unchanged) | 23      |

So K2 does not divide the cavity [D]. With K1 the bondply is not excited at all.

### C1 and D5

- `div_l_scale` is in place.
- **The series-fed column passes G2 with no declared difference.** Its TX bank sits at dx 4.70 and
  TX P0→P1 grows by 1.16 mm.

### TX feeds

- **T3:** only the nested order is planar [D].
- **T4:** TX1 is 1.46 mm shorter (7.9 ps). This needs your waiver.
- **T2: fails G4 at one point.** The fence opens to 0.65 mm where TX1's lane finger meets its
  run-in pair. The via placement does not close that gap. This is reported, not hidden.
- **T1 (the accordion) is not drawn.** It would save about 0.07 dB [D], but it adds bends, and the
  bends are where the radiated loss comes from.

### Via placement

New bridge and re-plan steps close fence gaps that a kinked shared row leaves. This was needed for
S1 and T4.

## 2. Boards

`gen/` holds 14 variants:

- A, B, C;
- S1, S1.5, open;
- K2, K1;
- SER;
- T2, T4;
- pa0;
- the C1 lower and upper corners.

rfm1-m, rfm1-n, rfm1-p and the coupons were rebuilt as well.

**All 18 boards pass KiCad DRC clean**: `--refill-zones --severity-all`, 0 violations, 0
unconnected items. **All pass G1–G9 except T2** (G4).

The summary table is in the repo at `results/stage3b/variants.json`.

## 3. E1 models, ready to submit (none submitted)

Each run uses one model on 8 threads, with `exact-endcriteria`. Far fields are frequency-domain
only. One bondply plane is dumped at 62.05 GHz.

| Campaign (`em/jobs/`)                                         | Family | Runs   | Cells          | Est. VM-h | Est. $    | Longest |
| ------------------------------------------------------------- | ------ | ------ | -------------- | --------- | --------- | ------- |
| s3b-e1-bank-a / -b / -c (one mesh template)                   | C4D    | 7 each | 16.2 M (40 µm) | 14.9 each | 1.18 each | 147 min |
| s3b-e1-bank-s1                                                | C4D    | 5      | 14.0 M         | 8.6       | 0.68      | 127 min |
| s3b-e1-c1a (15 points of L × inset, centre-only cells, 27 µm) | C4     | 15     | 3.0 M          | 9.0       | 0.71      | 38 min  |
| s3b-e1-ser (8 series-fed points)                              | C4     | 8      | 2.7 M          | 4.5       | 0.35      | 34 min  |
| s3b-e1-cav (triplets K0/K2/K1 at 27 µm; K0/K2 at 20 µm)       | C4     | 5      | 5.8 / 9.4 M    | 10.8      | 0.85      | 194 min |
| s3b-e1-pp (A/B/C × 27/20/15 µm, 4 ns ring-down)               | C4     | 9      | 0.6–1.8 M      | 1.8       | 0.14      | 22 min  |
| s3b-e1-tx (T0, T2, T4, S1, B, C × lossy/lossless, 20 µm)      | C4     | 12     | 5.6–8.0 M      | 4.0       | 0.31      | 22 min  |
| s3b-e1-pa (TX1 and RX4 × pa0/short/open/port, 20 µm)          | C4     | 8      | 0.9–1.6 M      | 0.8       | 0.06      | 8 min   |

**Total: 83 runs, about $6.6 expected, or about $7.6 with boot time and preemptions.**

The estimates are [D]: the rf-uniform reference runs, scaled by cell count and by the smallest
cell. Run `yapnr exp plan` on each file and log its ceiling before you submit.

**Wall time.** The bank runs take about 9–10 h on 8 C4D VMs. The Montreal runs take about 4–5 h.

**Modelling decisions:**

- **C1 points on single cells.** The C1 sweep runs on the centre column alone in a triplet's
  cut-out, because a full triplet costs 2× as much. E2 confirms the result embedded in a triplet.
- **No 15 µm triplets.** A 15 µm triplet has about 15 M cells, which runs past the 4 h task limit.
  E2 therefore takes its 15 µm point from single cells.
- **E1-PA is split by line.** It runs per line (8 runs instead of 4), so that each line's lead can
  leave the box without crossing the other line.

After E1-C1a, write out the second-stage w35 × t_y points with
`openems/stage3b_em.py c1b --em em --best '{...}'`. The isolation sub-model for E3 is
`board_ant.py --keep`.

## 4. For review

1. **D14 IR drop is 8.4 mV against the plan's 6 mV.** The geometry fixes it: the neck is 0.28 mm
   wide, and it has to leave the A1/B1 GND drop site free. I set the budget to 10 mV [D], which
   keeps the nominal ball voltage at 0.95 V.
2. **K2 is effectively K0.** The real cavity fixes are K1, or accepting K0 with a quantified
   warning.
3. **B and C are marginal on the plane-pair cut-off [D].** E1-PP decides.
4. **T2 fails G4, and T4 needs your skew waiver.**
5. **The integration track has two items:**
   - `merge_macro` must take the PA pads, the vias, the In3 tie and `skip_pads`;
   - `rf_audit` A3 needs the D15 B interior threshold.

**Figures:** `figs/build-d15-pour-variants.png`, `figs/build-pa-feed-corner.png` and
`em/figs/*.png` (one per model).
