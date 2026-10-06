<!-- markdownlint-disable -->

# Palace validation (P4): Palace against openEMS and the 2D solver

Date 2026-10-04 (UTC). Owner request (2026-10-04): "let's get the palace integration stood up
alongside stage 3". This is phase P4 of `design.md` §4–5: the validation models of
`pipeline.md` run in the Palace image of `image.md` on GCP Batch, compared with yapnr's 2D
cross-section solver (`yapnr.rf.coupons.xsec`) and with openEMS, and the Palace configuration for
radar sign-off. Evidence labels: **[S]** solver prediction, **[D]** arithmetic or a closed form.
Nothing here is measured.

Data, scripts and per-run records are in `val/` (layout at the end) and, for the review fixes of
§10, in `val2/`. Figures are in `figs/` (`val-lines.png`, `val-patch.png`, `val-tx12.png`,
`val-fix-tx12.png`).

**Revised 2026-10-04 after two adversarial reviews (§10).** Verdicts that changed: the sheet-loss
"pass" is withdrawn (no converged quantity) and loss is validated on solid copper; the patch is
"explained", not "pass"; the TX1 sheet notch agrees within 0.4 % (not 0.07 %); the solid-copper
notch shift is confirmed by openEMS (thick PEC copper) and its cost to TX1 is a range.

## 1. Answer

**Palace is validated as the second solver for the radar sign-offs** [S]. On the case that
motivated it, the TX1 sliver, the two methods agree within 0.4 % on the notch with sheet copper
and within 0.2 % with real 35 µm copper (each solver's finest mesh), and within 0.2 dB on
transmission with sheet copper. Where they differ, openEMS is the one still moving more with its
mesh (in x/y and in z), and its trend points at Palace. The acceptance of design.md §4 is ≤ 1 % in
frequency and ≤ 0.5 dB, or an explained difference:

| Case              | Metric                                                           | Palace                                                  | openEMS (finest; trend)                                                   | 2D solver                        | Verdict                                                                               |
| ----------------- | ---------------------------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------- |
| (a) lines, 1 GHz  | εeff, Z (MSL / GCPW, shielded port box), sheet                   | 2.7327 / 2.7017; 52.65 / 52.31 Ω                        | –                                                                         | 2.7208 / 2.6897; 53.09 / 52.64 Ω | εeff **pass** (0.45 %); Z −0.6/−0.8 %, **explained** (still rising with order)        |
| (a) lines, 1 GHz  | εeff, Z, solid 35 µm (engineering review)                        | 2.5968 / 2.5349; 48.99 / 48.13 Ω                        | –                                                                         | 2.5889 / 2.5281; 49.31 / 48.41 Ω | **pass** (+0.3 %; −0.6 %)                                                             |
| (a) lines, 62 GHz | εeff (MSL / GCPW), sheet                                         | 2.861 / 2.814 (p3)                                      | 2.948 / 2.899 at 20 µm, falling 1.5 % per step; unchanged by z refinement | –                                | **explained**: openEMS +3.0 %, extrapolates to 0.7–1.8 % below Palace                 |
| (a) lines, 62 GHz | loss, sheet                                                      | 0.087 / 0.088                                           | 0.070–0.078 (z / x-y refinement move it ±10 %)                            | –                                | **no verdict**: not a converged quantity (§3, §10.3)                                  |
| (a) lines, 62 GHz | loss, solid copper (MSL / GCPW, dB/mm; port-face modes, order 3) | 0.0632 / 0.0653 (PEC floor); 0.0768 / 0.0786 (lossy L2) | –                                                                         | 0.0616 / 0.0630; 0.0730 / 0.0732 | **pass** (+2.6–7.4 %, ≤ 0.005 dB/mm; §10.3)                                           |
| (b) patch         | main \|S11\| dip                                                 | 63.30 GHz (p3)                                          | 62.54 GHz at 20 µm (4 z-cells), 62.68 (8 z-cells), still rising           | –                                | **explained** (0.98–1.2 %; not a pass, §4)                                            |
| (c) tx12          | TX1 sliver notch, sheet                                          | 59.73 → 59.81 GHz                                       | 59.75–60.03 GHz over 27–15 µm and 4–8 z-cells                             | –                                | **pass** (≤ 0.4 %)                                                                    |
| (c) tx12          | TX1 sliver notch, solid 35 µm copper                             | 61.13 → **61.30 GHz** (initial → refined, lossy floor)  | 61.02 (27 µm) → **61.42 GHz** (20 µm, 8 z-cells), PEC copper              | –                                | **pass** (0.2 %)                                                                      |
| (c) tx12          | \|S21\| A and B at 60.3 / 62.05 / 63.8 GHz, sheet                |                                                         |                                                                           | –                                | **pass** (≤ 0.18 dB)                                                                  |
| (c) tx12          | sliver effect A − B, sheet                                       | +28.5° / +9.1° / +4.2°, −2.8 dB at 60.3                 | +27.9° / +7.8° / +3.5°, −2.9 dB                                           | –                                | **pass** (≤ 0.12 dB, ≤ 1.3°)                                                          |
| (c) tx12          | sliver effect A − B at 62.05 GHz, solid copper                   | −1.8 / −2.6 dB; +37° / +40° (initial / refined)         | −0.3 / −1.3 dB; +35° / +41° (27 µm / 20 µm nz8)                           | –                                | phase **pass**; magnitude **a range** (0.3–2.6 dB, on the notch's flank)              |
| (c) tx12          | \|S11\| at −13 to −22 dB; absolute phase                         |                                                         |                                                                           | –                                | **explained** (0.7–4.4 dB; phase 21°: mostly openEMS's εeff, up to 6° Palace's order) |

What this tells the radar work (for review):

1. **The zero-thickness copper model is the larger error, in both solvers.** The 4-mil 50 Ω lines
   are 53 Ω with εeff 5 % high as sheets [S, 2D solver]; with real 35 µm copper both solvers put
   the TX1 sliver notch at **61.3–61.4 GHz instead of 59.8–60.0 GHz**: inside ANT-02, and over
   the stackup's Dk range (3.33–3.66) anywhere from about 60.6 to 63.0 GHz (§10.1).
   At the band centre it costs TX1 0.3–2.6 dB (the centre sits on the notch's flank) and about
   +40° against the sliver-free feed (§5). em-baseline's fix (remove the slivers) is therefore more
   important than the sheet runs suggested. Sign-off models use solid copper (Palace) and thick PEC
   copper with 8 z-cells (openEMS).
2. **The ground loss belongs in the model:** L2 carries about a quarter of the lines' conductor
   loss; both solvers had a lossless floor (§10.2). The TX1 feed dissipates 0.18 dB more with it.
3. **openEMS at 20 µm still has εeff 3 % high** (β 1.5 %, about 25° over a 13.7 mm feed); z
   refinement (8 cells across the core) does not change that, but moves Z 2 %, loss 10 %, small
   |S11| 2 dB, the patch dip +0.13 GHz and the thick-copper notch further (§10.4). Sign off
   openEMS feeds at ≤ 20 µm fill with 8 z-cells.
4. **The stage-2 patch calibration was made on a 40 µm mesh:** on finer meshes both solvers put
   the calibrated single patch's dip at 62.7–63.3 GHz instead of 61.9 GHz (1.5–2 % high with sheet
   copper, §4; 2–3 % with solid copper, §10.5).

**Recommended configuration** (§7, and now yapnr's default for sign-off models): solid copper as
`Impedance` by admittance; the lossy L2 floor; solder mask where the board has it; wave ports
ending in coplanar ground; order 2 plus one refinement swept separately (and/or order 3); adaptive
sweep at 1e-3; 8 ranks on c4d-standard-16 (62 GB, ≤ 4 M unknowns); about $0.12 and 45 minutes
for a feed's three stages on C4D. READY.md is the runbook.

**Palace at b797ea8 needed three workarounds** (§8, all in yapnr now): a `Conductivity` sheet
that crosses a port aborts any multi-rank run; a saved adapted mesh loses its sheets' crack
accounting (conductor loss halved on reload); and wave-port modes drift inside a long
refinement loop. **Spend:** validation about $1.42, review fixes about $0.51; the track stands at
about $2.7 of $8 (`gcp-spend.md`).

## 2. What ran

All on GCP Batch Spot through `tools/exp/palace_plan.py` / `openems_plan.py` and `yapnr exp`,
one model per 16-vCPU VM with 8 MPI ranks (Palace) or one model per c4d-highcpu-8 at 8 threads
(openEMS, READY.md). us-west4 was held by another track for most of the session (64 of 64 Spot
vCPUs), so after merging main's two-region placement most Palace runs went to C4 in Montreal.
Every submit was under $5 and is logged in `gcp-spend.md` and `../radar60/gcp-spend.md`.

| Campaign                                          | What                                                                                                                                                                         | Tasks           | Outcome                                                                              | Spot $ [D] |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- | ------------------------------------------------------------------------------------ | ---------- |
| palace-val-w1a / w1b (3648b8, c2fa17)             | modes, lines; tx12 and patch sweeps                                                                                                                                          | 4 of 11 started | cancelled: all aborted in < 1 s (§8 issue 1)                                         | 0.02       |
| palace-val-bisect (208fdf)                        | 12 small configs on line-msl-5mm                                                                                                                                             | 1               | isolated issue 1                                                                     | 0.01       |
| palace-val-w2a (3dc8c8)                           | 2D mode solves (port face, open wall; 1/30/62 GHz; p2, p3; `Impedance` against `Conductivity`); the 4 lines at p3 + refinement                                               | 7               | modes and p3 sweeps used; 3 refinement stages ran 32 GB VMs out of memory, 1 timeout | 0.19       |
| palace-val-w2b (a75871)                           | patch-w p2, tx12-A p2                                                                                                                                                        | 2 of 4          | tx12-A's TX1.P0 port picked a 24 Ω mode (§8); cancelled                              | 0.08       |
| palace-val-oems (06b84d)                          | openEMS: 4 lines × 40/27/20 µm, tx12-A/B 15 µm, tx12-B 20 µm, patch × 3                                                                                                      | 18              | all pass; the patch runs drew L 1.1126 (§8)                                          | 0.12       |
| palace-val-w3c (9e24d5)                           | tx12-A: p2, one refinement, sweep of the adapted mesh                                                                                                                        | 1               | pass; the last sweep's loss halved (issue 3)                                         | 0.15       |
| palace-val-w4e (618507)                           | tx12-A/B solid copper                                                                                                                                                        | 2               | ran with half the surface impedance (issue 2); superseded                            | 0.07       |
| palace-val-w4g (140f96)                           | patch-finite: p2, refinement, p3, 0.5 λ0 box, second-order walls                                                                                                             | 4               | pass                                                                                 | 0.32       |
| palace-val-w4a (faa374)                           | lines: p2, 3 refinements, sweep; 1 GHz PEC mode solves; 3D line with `Conductivity` on 1 rank                                                                                | 6               | pass (adapted-mesh losses not used, issue 3)                                         | 0.17       |
| palace-val-oems2 (81439d)                         | openEMS patch L 1.1506 at 40/27/20 µm                                                                                                                                        | 3               | pass                                                                                 | 0.04       |
| palace-val-w8e (266b0b)                           | tx12-A/B solid copper, Rs = 2 Re Z (w5e, stuck in Batch's queue, was cancelled and resubmitted)                                                                              | 2               | 2/2 pass                                                                             | 0.07       |
| palace-val-w7 (024507)                            | tx12-A/B: p2, one refinement, sweep of the adapted mesh (per-face sheets)                                                                                                    | 2               | 2/2 pass                                                                             | 0.18       |
| w3a, w3b, w3d, w3e, w3g, w4b, w5b, w5e, w6        | re-placed or superseded while queued                                                                                                                                         | 0 ran           | cancelled                                                                            | 0          |
| w3f                                               | patch with a 56 GB class                                                                                                                                                     | 0               | refused at submit (no highmem Spot template)                                         | 0          |
| palace-review-r1 (139235), engineering review     | committed stage runner on line-msl-5mm; 1 GHz and 62 GHz mode solves, sheet and solid                                                                                        | 4               | pass                                                                                 | 0.06       |
| palace-fix1a-b (a2c23e), §10                      | tx12-B-sg: solid copper, lossy floor; p2, one refinement, sweep of the saved mesh                                                                                            | 1               | pass                                                                                 | 0.13       |
| palace-fix1a-a (fee3a1), §10                      | tx12-A-sg, the same                                                                                                                                                          | 1               | pass                                                                                 | 0.13       |
| palace-fix1a-a (fee3a1) submission 4, §10         | tx12-A-sg at Dk 3.33 and 3.66, order-2 sweeps                                                                                                                                | 2               | pass                                                                                 | 0.09       |
| palace-fix1b (b89a65) submissions 7, 10, 11, §10  | solid-copper lines with the lossy floor (MSL p2/p3, GCPW p2/p3 5 mm), 2D mode solves 58/62/66 GHz, refinement loops (PEC and solid, issue 4), single patch with solid copper | 6               | pass                                                                                 | 0.17       |
| fix1a, fix1-oems, fix1-oems2, fix1a-a (on demand) | first plans of the above                                                                                                                                                     | 0 ran           | cancelled while queued, or refused at submit (on-demand VMs are not allowed)         | 0          |
| openEMS on the Mac (`val2/oems-local`), §10       | tx12-A/B thick PEC and sheet, MSL pair L2 sheet and PEC, single patch; 20 µm, 8 z-cells                                                                                      | 9               | pass                                                                                 | 0 (Mac)    |

**Validation spend: about $1.42** [D: task wall + 2 min start × Spot list price, boot disk; the
summary fetches were < 0.2 GiB of egress; the rows of `gcp-spend.md` were corrected to add up to
this in the review], plus about $0.16 of cross-region image pulls to Montreal and the registry's
$0.19 a month. With the P0 build ($0.56), the smoke ($0.005), the review's re-run ($0.06) and the
review fixes (about $0.51, all Spot in us-west4: 9 tasks, 2.7 VM-hours) the Palace track stands at **about $2.7 of its $8**. No job of this
track is left running (checked 15:50 UTC).

## 3. Case (a): the 4-mil lines

Microstrip w 0.200 and GCPW w 0.200 / g 0.200 on RO4835 0.1016 mm (Dk 3.56, Df 0.0037), L1 a
zero-thickness sheet (σ/K², K 1.806) in both solvers, lines of 5 and 10 mm between wave ports
(Palace) or MSL ports (openEMS, the same planar documents through `val/code/oems_lines.py`).
εeff and loss come from the 10 mm line minus the 5 mm line, so the port ends cancel. Figure:
`figs/val-lines.png`.

![lines](figs/val-lines.png)

**2D, quasi-static (1 GHz, PEC copper, the shielded port face against the 2D solver in the same
box) [S]:**

| Line | Palace p2 εeff / Z | Palace p3 εeff / Z | xsec (t 1 µm) εeff / Z | Δ εeff / Δ Z (p3) |
| ---- | ------------------ | ------------------ | ---------------------- | ----------------- |
| MSL  | 2.7372 / 51.92 Ω   | 2.7327 / 52.65 Ω   | 2.7208 / 53.09 Ω       | +0.44 % / −0.83 % |
| GCPW | 2.7049 / 51.67 Ω   | 2.7017 / 52.31 Ω   | 2.6897 / 52.64 Ω       | +0.45 % / −0.63 % |

εeff meets the design's 0.5 % at 1 GHz. Z is 0.6–0.8 % low and still rising with order (+1.4 %
from p2 to p3: the port face is meshed at 0.1 mm with 0.04 mm at the zero-thickness strip
edges, where the field is singular); xsec's 1 µm strip also sits slightly below a true sheet.
With the copper's skin impedance on (the `Impedance` stand-in), the 1 GHz mode has εeff 2.84 /
2.81: the internal inductance of 35 µm copper at 1 GHz adds about 4 % to L.

**3D, 62 GHz [S]:**

| Quantity at 62 GHz | Palace p2 / p3 / adapted (DOFs of the 10 mm line) | openEMS 40 / 27 / 20 µm fill | Palace 2D mode, p3 |
| ------------------ | ------------------------------------------------- | ---------------------------- | ------------------ |
| MSL εeff           | 2.881 / 2.861 / 2.853 (0.37 / 1.05 / 1.60 M)      | 3.066 / 2.991 / 2.948        | 2.852 (open wall)  |
| GCPW εeff          | 2.832 / 2.814 / 2.825 (1.04 / 2.93 / 1.43 M)      | 3.022 / 2.944 / 2.899        | 2.815 (port face)  |
| MSL loss (dB/mm)   | 0.089 / 0.087 / –                                 | 0.072 / 0.074 / 0.078        | 0.085              |
| GCPW loss (dB/mm)  | 0.090 / 0.088 / –                                 | 0.073 / 0.075 / 0.080        | 0.083              |

- **Palace is self-consistent:** the 3D lines and the 2D mode solves agree within 0.3 %; order 2
  → 3 moves εeff down 0.65–0.7 %, refining the order-2 mesh moves it down 0.3–1.0 %.
- **openEMS converges onto Palace.** Its εeff falls 2.5 % from 40 to 27 µm and 1.5 % from 27 to
  20 µm, first order in the fill size; extrapolating that to zero gives 2.82–2.84 (MSL) and 2.76–
  2.79 (GCPW), 0.7–1.8 % below Palace p3. At 20 µm openEMS is still +3.0 % (MSL and
  GCPW) above Palace p3, i.e. β 1.5 % high: about 25° over the 13.7 mm TX1 feed. This is the
  phase error em-baseline found (74° between the 40 and 27 µm meshes).
- **Loss of the sheet: no verdict (review finding 3).** Palace's sheet loses 0.008–0.010 dB/mm
  more than openEMS at 20 µm, but neither number is converged: a zero-thickness sheet's edge
  current is singular, openEMS's loss rises with in-plane refinement (0.072 → 0.078 dB/mm) and
  falls 10 % with z refinement (4 → 8 → 12 cells across the core: 0.078 / 0.072 / 0.070 dB/mm,
  the physics review's R1 [S]), and both sheet values lie 13–41 % above the 2D solver's 35 µm
  copper (0.062 dB/mm with L2 lossless). Loss is validated on solid copper instead (§10.3).
- **openEMS's z mesh.** The refinement above was in-plane only (4 cells, 25.4 µm, across the
  core). Refining z with x/y leaves εeff unchanged (2.948 / 2.946 / 2.946 for 4 / 8 / 12 cells at
  20 µm; this round's 8-cell run on the Mac: 2.9458) but moves Z by +2.1 %, loss by −10 % and
  |S11| by +2.4 dB [S] (§10.4). The εeff trend and its extrapolation stand; Z, loss and |S11|
  statements below are about in-plane refinement only.
- **The copper stand-in is exact where it is set:** on the same mesh, Palace with `Conductivity`
  on one rank and with the `Impedance` stand-in on eight gives the 5 mm line's S21 identically at
  62 GHz (−0.4994 dB, 87.19°); at 54 and 70 GHz the stand-in's conductor loss differs by
  −0.004 / +0.0035 dB/mm (as predicted, §8), phase ≤ 0.1°. Eight ranks took 39 s, one rank 292 s.
- **Sheet against real copper [S, 2D solver]:** 35 µm copper gives 50.05 Ω / εeff 2.634 (MSL) and
  48.85 Ω / 2.550 (GCPW) static; the zero-thickness sheet 53.7 Ω / 2.758 and 53.1 Ω / 2.713. Both
  solvers' sheet model is a 53 Ω line with εeff 5 % high. Palace with solid copper matches the 2D
  solver at 1 GHz within 0.3 % in εeff and 0.6 % in Z (the engineering review's run) and in loss
  at 62 GHz within 0.005 dB/mm (§10.3); §5 shows what the copper model does to the feed.

**Verdict (a):** εeff at 1 GHz **pass** (0.45 %); Z at 1 GHz 0.6–0.8 % low, **explained**
(port-face mesh, still converging); εeff at 62 GHz **explained difference**: openEMS is 3 % high
at its finest mesh and converges toward Palace (extrapolated within 2 %; z refinement does not
change it); loss of the zero-thickness sheet: **no verdict** (not a converged quantity in either
solver; the solid-copper loss of §10.3 replaces it). Z at 62 GHz is not compared across the
solvers: Palace's Z_PV of the shielded port face and openEMS's V/I of the open line are different
definitions in different boundaries (sheet: 56.6 Ω against 53.8–55.0 Ω over 4–12 z-cells); what
the feed comparison needs of it enters through `to_50`, checked by the agreement of |S21| and
|S11| on the feed (§5).

## 4. Case (b): the calibrated single patch

The stage-2 single patch (`patch-c-i30`: W 1.45, L 1.1506, inset 0.30, notch 0.10, on its L2
window, board 0.5 λ0 beyond the copper, 0.3 λ0 of air to the absorber). openEMS:
`column_sim.py --single` as stage 2 (MSL port with a 50 Ω source at the start of the 1.2 mm lead,
S11 at P1), now at 40, 27 and 20 µm fill with exact end criteria. Palace: `patch-finite`, the same
board with a 50 Ω lumped port at the lead's start, de-embedded to P1 with the lead's own Z_PV and
γ from the 2D mode solves; and `patch-w` (the board run into a wave port), a different geometry
kept as a port-type check. The −27 dB depth of the match is not a usable metric (design.md §4);
the comparison is the frequency of the main |S11| dip and the shape of the curve. Figure:
`figs/val-patch.png`.

![patch](figs/val-patch.png)

| Run [S]                                                           | Unknowns     | Main dip (GHz) | \|S11\| there | RL ≥ 10 dB band (GHz) | \|S11\| 60.3 / 62.05 / 63.8 (dB) |
| ----------------------------------------------------------------- | ------------ | -------------- | ------------- | --------------------- | -------------------------------- |
| openEMS 40 µm (stage 2, Mac)                                      | 0.72 M cells | 61.91          | −27.0         | 60.90–62.80           | −7.4 / −23.8 / −5.6              |
| openEMS 40 µm (GCP, exact end criteria)                           | 0.72 M       | 61.91          | −26.9         | 60.90–62.85           | −7.4 / −23.9 / −5.6              |
| openEMS 27 µm                                                     | 1.16 M       | 62.36          | −25.9         | (deepest dip at 64.6) | −6.1 / −19.0 / −6.7              |
| openEMS 20 µm                                                     | 1.73 M       | 62.54          | −26.3         | 61.50–63.40           | −5.8 / −15.7 / −7.3              |
| Palace p2                                                         | 1.30 M DOFs  | 63.14          | −40.5         | 62.05–63.85           | −4.1 / −10.2 / −11.3             |
| Palace p2, walls 0.5 λ0 away                                      | 1.33 M       | 63.12          | −33.4         | 61.95–63.90           | −4.5 / −10.7 / −11.5             |
| Palace p2, second-order absorbing walls                           | 1.30 M       | 63.18          | −34.3         | 62.10–63.90           | −4.0 / −9.8 / −11.7              |
| Palace p2, one refinement (its conductor loss halved, §8 issue 3) | 1.35 M       | 63.24          | −38.8         | 62.20–63.90           | −3.8 / −9.2 / −11.8              |
| Palace p3                                                         | 3.72 M       | 63.30          | −28.1         | 62.30–63.90           | −3.7 / −8.7 / −12.5              |
| Palace `patch-w` (board into a wave port), p2                     | 0.98 M       | 63.54          | −42.0         | 62.05–65.45           | −4.4 / −10.1 / −25.9             |

- GCP reproduces the Mac's stage-2 run to 0.01 GHz and 0.1 dB, so the image and the exact end
  criteria change nothing at 40 µm.
- **openEMS's dip moves up with the mesh** at a nearly constant rate per µm of fill (+0.45 GHz
  from 40 to 27 µm, 0.034 GHz/µm; +0.18 GHz from 27 to 20 µm, 0.028 GHz/µm). A power-law fit
  through the three meshes (order 1.5) gives 62.9 GHz at zero fill, a first-order one 63.1 GHz.
- **Palace's dip is stable to ±0.06 GHz** against the absorbing walls' distance and order, and
  moves up +0.10 GHz with one refinement and +0.16 GHz from order 2 to 3 (3.7 M unknowns, 71 min,
  30 GB).
- **Palace − openEMS:** Palace p3 is +0.76 GHz (+1.2 %) above openEMS at 20 µm and +0.2 to
  +0.4 GHz (+0.3 to +0.6 %) above openEMS extrapolated to zero fill. Both solvers move the same
  way with refinement, Palace by much less. The second dip (the one openEMS at 27 µm finds
  deepest) is at 64.65 GHz in openEMS at 20 µm and 65.0 GHz in Palace p2, +0.5 %: the whole
  response is shifted, not reshaped.
- **Consequence for stage 3 [S]:** the stage-2 calibration (L × 0.967, so that the single
  patch's dip sits at 61.9 GHz) was made on the 40 µm openEMS mesh. On finer meshes both solvers
  put that patch's dip at 62.7–63.3 GHz, 0.8–1.4 GHz higher: with sheet copper the patch is tuned
  about 1.5–2 % high, with solid copper another 0.4 % (§10.5). The stage-2 column runs used the same 40 µm
  mesh and were not re-run here. For the stage-3 owner to review; it does not touch
  em-baseline's conclusions.

- **z refinement (review finding 4):** openEMS at 20 µm with 8 cells across core and bondply
  (instead of 4; 2.26 M cells, 33 min on the Mac) puts the dip at **62.68 GHz** (−22.6 dB), +0.13
  GHz over the 4-cell run: towards Palace again. Against Palace order 3 (63.30 GHz) that is
  0.98 %, against order 2 (63.14 GHz) 0.73 %.
- **A resonance frequency that does not ride on the match (review finding 5):** the peak of Re Zin
  is plane-dependent here: at P1 it lies at 65–66 GHz, on the edge of Palace's 58–66 GHz band, and
  it moves several GHz as the reference plane moves along the lead (the patch has two coupled
  resonances, near 62.5 and 64.6 GHz). With the plane 0.3 mm past P1 for both solvers (the lead's
  Z0 and γ from Palace's mode solves) the peak is inside both bands [S]:

  | Run                                    | Re Zin peak, P1 + 0.3 mm (GHz) | Main \|S11\| dip at P1 (GHz) |
  | -------------------------------------- | ------------------------------ | ---------------------------- |
  | openEMS 40 / 27 / 20 µm, 4 z-cells     | 63.66 / 63.78 / 63.82          | 61.91 / 62.36 / 62.54        |
  | openEMS 20 µm, 8 z-cells               | 63.86                          | 62.68                        |
  | Palace p2 / refined / p3 (sheet)       | 63.92 / 63.93 / 63.96          | 63.14 / 63.24 / 63.30        |
  | Palace p2, walls 0.5 λ0 / second order | 63.91 / 63.94                  | 63.12 / 63.18                |

  On that plane the solvers are 0.10–0.17 % apart, and openEMS converges onto Palace (63.66 →
  63.86 GHz). The number supports the explanation; it is not used as the criterion, because it
  depends on the plane.

**Verdict (b): explained difference, not a pass** (review finding 5). The dip at openEMS's finest
meshes is 1.2 % (20 µm, 4 z-cells) and 0.98 % (20 µm, 8 z-cells) below Palace order 3; openEMS is
still rising with every refinement in x/y and in z, Palace by much less (+0.16 GHz from order 2 to
3); the extrapolation to zero fill (62.9–63.1 GHz, order unknown) is consistent with Palace but is
not a measurement of it. A different port model alone moves the dip by 0.24–0.40 GHz (`patch-w`,
the board run into a wave port, at order 2: 63.54 GHz, against `patch-finite` at order 3 / 2),
about the size of the remaining gap. The box-size check moved the dip by at most
0.06 GHz (0.1 %) but its depth by 6–7 dB.

## 5. Case (c): the TX1 feed with the GND sliver

The radar60 TX feeds as built (`tx12-A`, TX1's north finger with the 3.49 × 0.20 mm unstitched
GND tongue) and with the four slivers removed (`tx12-B`), excited at TX1.P0. Both solvers read
the same `prep.py` model of the zone-filled KiCad board (pipeline.md §2–3); Palace's box is the
openEMS 27 µm run's PML interior with the walls moved off the via barrels, and its four wave
ports are de-embedded to P0/P1. openEMS: em-baseline's runs at 40/27/20 µm (Mac) plus 15 µm for A
and B and 20 µm for B (GCP, exact end criteria). Palace: order 2 on the initial mesh, one
refinement at 59/60/61/63.8 GHz and the sweep of the saved mesh, all with zero-thickness copper;
and order 2 with solid 35 µm copper. All S-parameters as openEMS reports them (50 Ω port waves,
lines terminated in their own impedance; Palace's modal S renormalized with Z_PV). Figure:
`figs/val-tx12.png`.

![tx12](figs/val-tx12.png)

| Run [S]                                                                | Unknowns     | A: notch (GHz, depth) | A: \|S21\| 60.3 / 62.05 / 63.8 (dB) | B: \|S21\| 60.3 / 62.05 / 63.8 | A − B: \|S21\| (dB) / phase (°)                      |
| ---------------------------------------------------------------------- | ------------ | --------------------- | ----------------------------------- | ------------------------------ | ---------------------------------------------------- |
| openEMS 40 µm                                                          | 0.83 M cells | 56.94 (−7.8)          | −1.33 / −1.31 / −1.31               | −1.41 / −1.37 / −1.32          | +0.08 / +0.07 / 0.00; +4.5 / +2.4 / +1.0             |
| openEMS 27 µm                                                          | 1.70 M       | 59.75 (−9.2)          | −4.10 / −1.52 / −1.47               | −1.59 / −1.60 / −1.57          | −2.52 / +0.08 / +0.10; +29.8 / +8.8 / +4.3           |
| openEMS 20 µm                                                          | 2.77 M       | 59.94 (−7.9)          | −4.98 / −1.53 / −1.46               | −1.57 / −1.57 / −1.51          | −3.41 / +0.03 / +0.05; +25.7 / +7.3 / +3.1           |
| openEMS 15 µm                                                          | 4.71 M       | **59.85** (−8.5)      | −4.57 / −1.63 / −1.56               | −1.67 / −1.67 / −1.62          | −2.90 / +0.04 / +0.07; +27.9 / +7.8 / +3.5           |
| Palace p2                                                              | 1.71 M DOFs  | 59.73 (−9.2)          | −4.10 / −1.76 / −1.78               | −1.70 / −1.78 / −1.86          | −2.40 / +0.02 / +0.08; +28.1 / +9.7 / +4.8           |
| Palace p2, adapted mesh                                                | 1.82 M       | **59.81** (−9.1)      | −4.43 / −1.69 / −1.72               | −1.64 / −1.72 / −1.80          | −2.79 / +0.03 / +0.08; +28.5 / +9.1 / +4.2           |
| Palace p2, **solid 35 µm copper**                                      | 1.97 M       | **61.18** (−13.4)     | −5.51 / **−3.64** / −1.68           | −1.73 / −1.83 / −1.91          | −3.78 / **−1.81** / +0.23; −22.9 / **+39.4** / +14.8 |
| Palace p2, solid copper, **lossy L2 floor** (§10)                      | 1.97 M       | 61.13 (−12.7)         | −5.95 / −3.78 / −1.90               | −1.91 / −2.02 / −2.10          | −4.04 / −1.76 / +0.20; −22.0 / +37.4 / +14.5         |
| Palace p2, solid copper, lossy floor, **refined mesh** (§10)           | 2.14 M       | **61.30** (−12.7)     | −5.20 / −4.56 / −1.88               | −1.87 / −1.98 / −2.07          | −3.33 / −2.58 / +0.19; −21.9 / +39.7 / +14.0         |
| openEMS, L1 as 35 µm PEC (lossless), 27 µm, 4 z-cells (physics review) | 1.91 M cells | 61.02 (−18.1)         | −5.73 / −1.95 / −1.19               | –                              | −4.12 / −0.33 / +0.32; −26.1 / +35.2 / +12.3         |
| openEMS, L1 as 35 µm PEC (lossless), **20 µm, 8 z-cells** (§10)        | 3.57 M cells | **61.42** (−15.7)     | −3.58 / −2.86 / −1.17               | −1.62 / −1.56 / −1.40          | −1.96 / −1.30 / +0.23; −16.7 / +41.1 / +10.9         |

|S11| at TX1.P0, 62.05 / 63.8 GHz: A −17.9 / −14.4 (openEMS 15 µm), −22.3 / −16.0 (Palace
adapted); B −13.2 / −12.8 and −14.8 / −13.5. The absolute TX1 phase at 62.05 GHz (B) is −306°
(openEMS 15 µm; −301° to −318° over 27–15 µm) against −285° (Palace).

- **The sliver notch agrees within 0.4 %** (review finding 8: the 0.07 % of the first report was
  one pair of meshes): Palace 59.73 → 59.81 GHz from the initial to the refined mesh (0.13 %);
  openEMS 59.75 / 59.94 / 59.85 GHz at 27 / 20 / 15 µm with 4 z-cells, 59.65 GHz at 27 µm on the
  physics review's run, and 60.03 GHz at 20 µm with 8 z-cells (this round, §10.4). Palace refined
  against openEMS's finest in-plane mesh is 0.07 %, against its z-refined one 0.37 %. The 40 µm
  stage-2 mesh put it 5 % low (56.94 GHz), as em-baseline found. With this (sheet) copper model
  the notch is at 59.7–60.0 GHz.
- **Transmission agrees within 0.2 dB** wherever it was compared (A and B, 60.3/62.05/63.8 GHz;
  on the notch's flank at 60.3 GHz the notches coincide, so no detuning correction is needed).
- **The sliver's effect (A − B) agrees within 0.12 dB and 1.3°**: Palace +28.5 / +9.1 / +4.2°
  against openEMS +27.9 / +7.8 / +3.5° at 60.3/62.05/63.8 GHz, and −2.8 against −2.9 dB at
  60.3 GHz. em-baseline's numbers for the tongue (+30° / +9° / +4°) stand.
- **The absolute phase differs by 21°** over the feed (openEMS has more). Most of it is the line
  case's εeff difference (§3: 25° predicted over 13.7 mm at 20 µm), but not all of it is
  openEMS's: these Palace runs are order 2, and order 2 → 3 moved the lines' εeff down 0.65 %,
  about 6° over 13.7 mm (review finding 8).
- **|S11| differs by 0.7–4.4 dB** at the −13 to −22 dB level (the magnitudes within 0.05). Renormalizing
  Palace with openEMS's line impedance (53.2 Ω) instead of Z_PV moves it by ≤ 0.6 dB, so this is
  the port and launch modelling (openEMS's MSL port runs into the PML; Palace's wave port is a
  shielded face 0.8 mm before P0), and openEMS's own |S11| moves 2 dB between its meshes. The
  design's 0.5 dB criterion is not meaningful for reflections this small (as for the patch's
  −27 dB).
- **Real copper thickness moves the notch into the band [S, both solvers]** (corrected after the
  physics review: the first report said openEMS could not see it). With solid 35 µm copper Palace
  puts the notch at 61.18 GHz on the initial mesh (+2.4 % over the sheet at the same order and a
  similar mesh), 61.13 GHz with the lossy L2 floor and **61.30 GHz on the refined mesh**; openEMS
  with L1 drawn as 35 µm PEC puts it at 61.02 GHz (27 µm, 4 z-cells, the review's run) and
  **61.42 GHz** (20 µm, 8 z-cells): the two solvers' finest meshes are 0.2 % apart, and both moved
  up with refinement (Palace +0.28 %, openEMS +0.66 %). With the RO4835 Dk range of the stackup
  (3.33–3.66) the notch moves over 60.4–62.9 GHz at order 2, about 60.6–63.0 GHz refined (§10.1):
  inside ANT-02 throughout.
- **What it costs TX1 is a range, not a number** (review finding 1). The sliver's effect (A − B)
  at 62.05 GHz is −0.3, −1.3, −1.8 and −2.6 dB on the four solid-copper runs above (the band
  centre sits on the notch's steep flank, so |S21| there follows the notch's exact frequency and
  depth), +35° to +41° in phase on all of them; at 60.3 GHz −2.0 to −4.1 dB and −17° to −26°; at
  63.8 GHz +0.2 to +0.3 dB and +11° to +15°. The fix em-baseline recommends (remove every
  unstitched sliver: variant B) matters more than the sheet model suggested: as built, TX1 would
  be 0.3–2.6 dB and about 40° off in the middle of ANT-02.
- **Solid copper transmits slightly less than the sheet on B (review finding 1), explained by
  the power balance [S]:** at 62.05 GHz the dissipated and radiated power is 1.43 dB with solid
  copper against 1.57 dB with the sheet (both p2, PEC floor; 1 − Σ|S_i1|² over the four modal
  ports), i.e. the solid copper loses 0.14 dB less over the feed; its reflection at TX1.P0 is
  2 dB higher (−11.4 against −13.5 dB, modal), 0.03 of the incident power, which outweighs that.
  The lines' 0.020 dB/mm (0.27 dB over 13.7 mm) is not reached because part of the feed's loss is
  not in the plain 4-mil line (launch, bends, the pour). The lossy floor adds 0.18 dB of
  dissipation over the feed (1.60 dB), the refinement takes 0.03 dB off it.

**Verdict (c):** sheet-copper notch frequency **pass** (within 0.4 % over both solvers' meshes);
solid-copper notch frequency **pass** (61.30 against 61.42 GHz, 0.2 %, each solver's finest
mesh; both still moving up a few tenths of a percent); |S21| at 62.05 / 63.8 GHz and on the
flank (sheet) **pass** (≤ 0.18 dB); the A − B difference (sheet) **pass** (≤ 0.12 dB, ≤ 1.3°);
the A − B difference with solid copper: phase **pass** (+35° to +41°), magnitude at 62.05 GHz
**reported as a range** (0.3–2.6 dB: it rides on the notch's flank); |S11| 0.7–4.4 dB at −13 to
−22 dB, **explained** (port modelling and small-reflection sensitivity, and openEMS's |S11| moves
2.4 dB with z refinement); absolute phase 21°, **explained** (mostly openEMS's εeff, §3, up to
about 6° Palace's order 2).

## 6. Run time and cost per case

Spot list prices of 2026-10-04 (C4D us-west4, C4 Montreal) × (task wall + 2 min VM start), boot
disk included [D]; egress and storage excluded. Palace: 8 ranks on 16 vCPUs; openEMS: 8 threads
on a c4d-highcpu-8.

| Case    | Solver and run                                                                                                    | Unknowns                            | VM              | Wall                                    | Peak memory      | $ per run             |
| ------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------- | --------------- | --------------------------------------- | ---------------- | --------------------- |
| (a)     | Palace 2D mode solves, 18 per task (port face, open wall; p2, p3, `Conductivity` on 1 rank)                       | ≤ 0.05 M                            | c4-highcpu-16   | 10–19 s                                 | 0.4 GB           | 0.004                 |
| (a)     | Palace 3D line, p2 sweep, 5 / 10 mm (MSL)                                                                         | 0.21 / 0.37 M                       | c4-standard-16  | 39 / 108 s                              | 2.5 / 4.4 GB     | 0.005 / 0.007         |
| (a)     | Palace 3D line, p3 sweep, 5 / 10 mm (GCPW)                                                                        | 1.66 / 2.93 M                       | c4-highcpu-16   | 7.6 / 25 min                            | 12 / 22 GB       | 0.016 / 0.044         |
| (a)     | openEMS line, 20 µm, 5 / 10 mm (GCPW)                                                                             | 1.69 / 2.80 M cells                 | c4d-highcpu-8   | 62 / 137 s                              | –                | 0.004 / 0.005         |
| (b)     | Palace patch-finite, p2 sweep                                                                                     | 1.30 M                              | c4-standard-16  | 23 min                                  | 15 GB            | 0.05                  |
| (b)     | Palace patch-finite, p3 sweep                                                                                     | 3.72 M                              | c4-standard-16  | 71 min                                  | 30 GB            | 0.14                  |
| (b)     | openEMS patch, 40 / 27 / 20 µm                                                                                    | 0.72 / 1.16 / 1.73 M cells          | c4d-highcpu-8   | 3.3 / 6.4 / 15.9 min                    | –                | 0.006 / 0.010 / 0.022 |
| (c)     | Palace tx12, p2 sweep                                                                                             | 1.71 M                              | c4d-standard-16 | 13–15 min                               | 20 GB            | 0.04–0.05             |
| (c)     | Palace tx12, one refinement at 4 points + sweep of the saved mesh                                                 | 1.82 M                              | c4d-highmem-16  | 27 min                                  | 23 GB            | 0.095                 |
| (c)     | Palace tx12, solid copper, p2 sweep                                                                               | 1.97 M                              | c4-standard-16  | 17 min                                  | 23 GB            | 0.036                 |
| (c)     | openEMS tx12-B, 20 / 15 µm                                                                                        | 2.77 / 4.71 M cells                 | c4-highcpu-8    | 3.1 / 9.0 min                           | –                | 0.004 / 0.009         |
| (c)     | openEMS tx12-A (the ringing sliver), 15 µm                                                                        | 4.71 M cells                        | c4-highcpu-8    | 36 min                                  | –                | 0.03                  |
| (c) §10 | Palace tx12 solid copper, lossy floor: p2 sweep / one refinement at 4 points / sweep of the refined mesh          | 1.89–1.97 M / 2.04–2.14 M           | c4d-standard-16 | 14 / 11–13 / 17 min (41–44 min the job) | 23 / 26–27 GB    | 0.12–0.13 the job     |
| (c) §10 | Palace tx12-A solid, p2 sweep at Dk 3.33 / 3.66                                                                   | 1.97 M                              | c4d-standard-16 | 14–15 min                               | 23 GB            | 0.04–0.05             |
| (c) §10 | openEMS tx12-A / B, 20 µm, 8 z-cells, sheet or 35 µm PEC                                                          | 3.46–3.57 M cells                   | Mac, 4 threads  | 15–16 / 5–6 min                         | –                | 0                     |
| (a) §10 | Palace solid lines with lossy floor: MSL 5+10 mm p2+p3 (4 solves) / GCPW 5 mm p2+p3 and 10 mm p2 / 11 mode solves | 0.24–1.22 M / 0.64–1.8 M / < 0.05 M | c4d-standard-16 | 10 / 14 / 0.1 min                       | 10 / 12 / 0.4 GB | 0.03 / 0.05 / 0.006   |
| (b) §10 | Palace patch-finite solid copper, p2 sweep                                                                        | 1.32 M                              | c4d-standard-16 | 21 min                                  | 15 GB            | 0.07                  |
| (b) §10 | openEMS patch 20 µm, 8/8 z-cells                                                                                  | 2.26 M cells                        | Mac, 4 threads  | 33 min                                  | –                | 0                     |

- Palace's order-2 sweep of a 1.3–1.7 M unknown model costs about $0.05 and takes 13–25 minutes,
  half of it preconditioner setup (AMS, rebuilt per frequency); 10–13 full solves per sweep.
- **C4 (Intel, Montreal) is about 1.7× slower per unknown than C4D** for Palace (patch p2 on C4:
  106 s per solve at 1.3 M; tx12 p2 on C4D: 79 s at 1.7 M), against 1.3–1.4× for openEMS; at
  0.7× the price it costs about 20 % more per run. Prefer C4D when it has quota.
- openEMS is 5–20× cheaper per run at these sizes, which is why it stays the sweep workhorse.
- The whole validation, 53 tasks including the false starts (32 Palace, 21 openEMS), cost about $1.43 (§2).

## 7. Recommended Palace configuration for radar sign-off

What these runs support, for the radar sign-off models (the 60 GHz column, the BGA-to-GCPW
launch, the bank with finite board and radome). Since the review fixes this is what yapnr does
**by default**: `python -m yapnr.rf.palace case MODEL --out DIR --band ...` on any model not named
like a validation case writes it (`validation.signoff_settings`), and `palace_plan.py` runs it.
READY.md has the commands; §10 the review's changes.

| Item                | Setting                                                                                                                                                                                                                                                                                | Why (this report)                                                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Image               | `palace:b797ea8-x86-64-v3` (pinned digest `sha256:9d157377…`), schema 2-1-0                                                                                                                                                                                                            | image.md; every run here                                                                                                                                                        |
| Copper              | **solid 35 µm** (`case` applies it), written as `Impedance` by admittance at the band centre (the default `copper_bc`)                                                                                                                                                                 | the zero-thickness sheet makes the 50 Ω lines 53 Ω with εeff 5 % high [S, 2D solver] and moves the tx12 notch by 2.4 % (§5, §10.1); `Conductivity` aborts on > 1 rank (issue 1) |
| Ground              | the lossy `metal` floor (L2 with the LoPro foil's roughness factor), the default of the adapters                                                                                                                                                                                       | the ground carries about a quarter of the conductor loss (§10.2)                                                                                                                |
| Solder mask         | `stackups.solder_mask` (a `coat` of L1, conformal) where the board has mask: the BGA launch                                                                                                                                                                                            | §10.7                                                                                                                                                                           |
| Ports               | wave ports with `VoltagePath`; faces ±0.8 mm, 0.7 mm above L1, **ending inside coplanar ground**; `WavePortPEC` on the port walls; `Offset` back to the reference planes; S renormalized to 50 Ω in post with Z_PV (`yapnr.rf.palace.results.to_50`, the openEMS port-wave convention) | §5 (floating ground gave a 24 Ω port)                                                                                                                                           |
| Walls               | first-order absorbing, ≥ 0.3 λ0 from radiating copper, for feeds and single elements; for arrays, columns with neighbours and the radome: ≥ λ0, second order, and a box study on coupling, active S11 and pattern                                                                      | patch: 0.5 λ0 walls or second order moved the dip ≤ 0.06 GHz but its depth 6–7 dB (§4); a first-order wall reflects −15 dB at 45°, −9.5 dB at 60° [D] (§10.6)                   |
| Order and mesh      | order 2 on the gmsh mesh (0.04 mm at copper edges), then **one nonconforming refinement and/or order 3** as the convergence check                                                                                                                                                      | lines: p2 → p3 moved εeff 0.65 %; tx12 sheet: one refinement moved the notch 0.13 %, solid copper see §10.1; patch: p2 → p3 moved the dip 0.25 %                                |
| Refinement          | `Nonconformal`, `UpdateFraction` 0.7, **one** iteration at the feature frequencies (resonances, band edges), `MaxSize` 2 M (the default); then **sweep the saved mesh in a separate solve** (`stages` + `mesh_from`; `precracked` for sheets)                                          | issues 3–5                                                                                                                                                                      |
| Sweep               | adaptive, `AdaptiveTol` 1e-3, `AdaptiveMaxSamples` 30, 54–70 GHz written every 25 MHz                                                                                                                                                                                                  | 10–12 full solves per case; on tx12-A the reduced model matched separate full solves at 59/60/61/63.8 GHz to \|ΔS\| ≤ 2e-5                                                      |
| Linear solver       | Palace default (GMRES + AMS, 1e-6)                                                                                                                                                                                                                                                     | 35–90 GMRES iterations per solve, none failed                                                                                                                                   |
| Parallel            | 8 MPI ranks bound to cores, one model per 16-vCPU VM; **c4d-standard-16** (62 GB) in us-west4, c4-standard-16 in Montreal (about 1.7× slower for Palace, 0.7× the price)                                                                                                               | memory about 12 kB per unknown at order 2 (issue 6): ≤ 4 M unknowns per 62 GB VM                                                                                                |
| openEMS counterpart | 20 µm fill or finer with **8 cells across the core**; copper thickness as PEC (`feed_sim_x.py --thick`) for frequencies, read with own-line-impedance waves                                                                                                                            | §10.1, §10.4                                                                                                                                                                    |
| Cost                | tx12 feed with solid copper (1.9–2.0 M unknowns): order-2 sweep 14 min, refinement 11 min, sweep of the refined mesh 17 min: about $0.12 the three on C4D                                                                                                                              | §6, §10                                                                                                                                                                         |

For the column and the bank: the feed's 1.7–2.0 M unknowns grow to about 3–5 M for a column with its
divider and 15–30 M for the bank with radome (design.md §5). Up to about 4 M fits this VM; beyond
that use c4d-standard-32/-64 or highmem through an instance policy with 16–32 ranks (per-job
`ranks`/`memory_gb` now place correctly, §10; not measured here: a 4/8/16-rank scaling run on the
tx12 model is the first thing to do before the bank). openEMS stays the sweep workhorse at 20 µm
fill or finer for phase and resonances; Palace on solid copper is the second-method check on the
cases that decide sign-off.

## 8. Palace issues found, and what the tooling does about them

Each was found on these runs, traced in Palace's source at b797ea8, and handled in yapnr; none
is reported upstream yet.

| #   | What happens                                                                                                                                                                                                                                                                                                                                                           | Effect seen                                                                                                                                                                    | What yapnr does now                                                                                                                                       |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | A `Conductivity` boundary crossing a wave-port face or a BoundaryMode cross-section aborts the 2D mode solve on more than one MPI rank ("Dimension mismatch for MaterialPropertyCoefficient and libCEED integrator"): ranks that own none of its edges build an empty coefficient, which `ModeOperatorModel` adds without the empty check the other boundary types get | wave 1: every task with copper sheets died in < 1 s; a 12-config bisect isolated it (PEC copper runs; dropping the loss tangent, absorbing walls or WavePortPEC does not help) | copper is written as the `Impedance` with the same admittance at the band centre (`config.impedance_rl`); checked against `Conductivity` on one rank (§3) |
| 2   | `Impedance` is a parallel R ∥ L per square of the whole boundary (halved per face of a cracked interior sheet); `Conductivity` applies its Z on each face                                                                                                                                                                                                              | an interior sheet maps to Rs = ωLs = Re Z, the outside of solid copper to 2 Re Z; wave 4's solid runs used Re Z (half the surface impedance) and were redone                   | `impedance_rl(interior=...)` matches the admittance                                                                                                       |
| 3   | A saved adapted mesh has its interior sheets split already; a run that loads it no longer treats them as cracked (no "duplicate vertices" line) and gives each face the whole `Impedance`                                                                                                                                                                              | conductor loss of sweeps on saved meshes about half (GCPW line 0.063 against 0.088–0.090 dB/mm; tx12-A \|S21\| −1.31 against −1.76 dB at 62.05 GHz)                            | `precracked=True` for sweeps of saved meshes (`palace-sweep.json`); the affected runs were redone (tx12) or their loss is not used (lines, patch)         |
| 4   | Inside a nonconforming refinement loop, wave-port modes drift after a few refinements when a **zero-thickness** strip crosses the port face (reproduced with a PEC strip, so not the `Impedance`; not seen with solid copper, §10.3)                                                                                                                                   | line-msl-5mm at 62 GHz: Z_PV 55.9 → 62.4 Ω and \|S21\| −0.47 → −1.76 dB by the fifth mesh (sheet); PEC strip: 55.7 → 61.5 Ω, −0.23 → −0.95 dB on a lossless line               | refine once at a few points, then sweep the saved mesh in a separate solve (`stages` + `mesh_from`); solid copper for sign-off                            |
| 5   | Conformal (bisection) refinement grows the mesh fast                                                                                                                                                                                                                                                                                                                   | 0.21 → 0.76 → 1.94 → 4.5 M unknowns in three steps, then out of memory on 32 GB                                                                                                | nonconforming refinement, one step for the 1–2 M unknown models                                                                                           |
| 6   | Memory: about 12 kB per unknown at order 2 for a sweep with the reduced model (about 8 kB at order 3)                                                                                                                                                                                                                                                                  | 2.6 M unknowns ran a 32 GB VM out of memory; 1.7–2.0 M took 20–23 GB                                                                                                           | `memory_gb` 48 (64 GB VMs) and `MaxSize` caps in the validation settings                                                                                  |

Also from these runs (not Palace bugs):

- **Port faces must not hold a floating conductor.** In the port's 2D solve the face's edges are
  PEC, so a coplanar ground strip that does not reach one floats; tx12's TX1.P0 face ended 0.15 mm
  past the ground (2.90–3.25 mm at the wall, edge at 3.40) and the port picked a 24 Ω mode
  (n_eff 1.72 against 1.68). `model.port_geometry` now ends a side that has coplanar ground
  inside it (yapnr commit bad0e06).
- **The radar60 worktree changed `patch_l`'s meaning.** It now multiplies `patch_l` by
  `fullwave_l_scale` 0.967, so `column_sim.py --single --set patch_l=1.1506` draws L 1.1126.
  The first openEMS patch campaign ran that (dip at 64.9 GHz) and was redone with
  `--set fullwave_l_scale=1.0`, which reproduces stage 2 on GCP.

## 9. Code, files and open items

**yapnr `claude/palace`** (worktree `yapnr-wt/palace`; pushed, **PR #46**), on top of the P0–P2 commits:

| Commit                      | Content                                                                                                                                                                                                                                                    |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a3423e6`                   | merge of `origin/main` (#43 two-region placement, #44): needed to spill to C4 in Montreal while us-west4 was full. Conflict in `docs/cloud-experiments.md` only; `tools/exp/openems_plan.py` merged cleanly                                                |
| `c9b9c26`                   | `Impedance` copper (issue 1), open-wall and port-face mode solves, `SaveAdaptIterations`, `AdaptiveMaxSamples`, `python -m yapnr.rf.palace configs`                                                                                                        |
| `bad0e06`                   | port faces end inside coplanar ground; validation settings from these runs                                                                                                                                                                                 |
| `e66db5f`                   | `palace_job.py --stage CONFIG[@RANKS] --mesh-from`, `palace_plan.py` `stages`/`mesh_from`: refinement then a sweep in one task                                                                                                                             |
| `da93f83`, `806fd0c`        | `impedance_rl` by admittance (issue 2) and for pre-split meshes (issue 3)                                                                                                                                                                                  |
| `545cf8b`, `6c8e807`        | docs: `docs/rf-palace.md` §4, §6, §7 and the Palace part of `docs/cloud-experiments.md`                                                                                                                                                                    |
| `e82d2df`                   | merge of `origin/main` (#30)                                                                                                                                                                                                                               |
| `76b308f`                   | review fixes, planar: lossy `metal` floor (L2 with L1's roughness), solder-mask `coat` over solid copper (conformal or level), KiCad footprint pads                                                                                                        |
| `74cc5b7`                   | review fixes, Palace: Impedance copper and one-step refinement by default, `signoff_settings` and `case` defaults (with a test), `yapnr.rf.palace.results` (the analysis of `val/code/pal.py`, `lines.py`, `patch.py`, with tests), BUILD deps             |
| `e382bf1`                   | review fixes, campaigns: per-job ranks/memory placed (instance policy where a shape has no template; fewer ranks reserve the campaign's cores), stage outputs `stage-<k>-<stem>`, a stage listed twice refused, the job record written while the task runs |
| `d7c46dd`, `43dc2e6`, later | image labels (licences, revision) and reproducibility notes; docs (sign-off settings, sections 8–10 of `docs/rf-palace.md`), WORKLOG                                                                                                                       |

prek is clean on all files (including the privacy scan); the Palace unit tests (now 78 with the
gmsh/shapely ones, in `.venv/palace`) and `tests/unit/exp` (174) pass; Bazel ran locally on
`//tests/unit/rf_palace/...`, `//tests/unit/exp:test_palace_plan`, `test_openems_plan`,
`//tests/unit/repo/...` and `//tests/unit/rf_coupons/...`, and CI (`bazel test //...` on Linux
and macOS, lint, docs, wheels) is green on PR #46.

**Files** (`palace/val/`): `models/` (the meshed cases and every config, incl. `bisect/`,
`*-solid/`, `patch-finite-air05/`), `code/` (`chain.py`, the in-task stage runner used before
`--stage` existed; `derive.py`; `feed_sim_x.py` and `oems_lines.py` for the openEMS lines;
`mesh_solid.py`, `mesh_patch_box.py`; `xsec_ref.py`; the analysis `pal.py`, `lines.py`,
`patch.py`, `tx12.py`, `costs.py`, `fig_*.py`), `xsec_ref.json`, one directory per campaign
(`w1a` … `w8e`, `bisect`, `oems`, `oems2`: `JOBS.toml`, the planned campaign, plan logs; the
openEMS results collected in `oems*/runs/`). Palace results stay in the fetch store
(`~/yapnr-runs/fetched/<cid>/`; ids in `val/code/runs.py`).

**Open items.**

1. Report issues 1–4 upstream (awslabs/palace), with the bisect configs and the §10 refinement
   loops as reproducers.
2. ~~Solid-copper lines~~: done at 1 GHz by the engineering review (pass) and at 62 GHz in §10.3.
3. The job records should carry the VM shape (`costs.py` takes it from a table per campaign), and
   `openems_job.py` the CPU model (image.md finding 6). Spend is not reconciled with billing.
4. The full-band Palace loss uses a frozen surface impedance (±6–8 % conductor loss at the band
   edges); a two-pole `RationalImpedance` fit would remove that once issue 1 is fixed upstream.
5. A 4/8/16-rank scaling run of the tx12 model before the column and bank models (§7).

## 10. Review fixes (2026-10-04)

Two adversarial reviews (physics and engineering) of this report and of `claude/palace`. Every
finding was checked; none was rejected outright, one was corrected in detail (engineering 2).
What changed, and what the re-runs show [S]; nothing here is measured.

**Re-runs.** Palace on GCP Batch (C4D us-west4; both regions' 64 Spot vCPUs were held by other
tracks for most of the afternoon, see `gcp-spend.md`): `palace-fix1a-a` (fee3a1) and
`palace-fix1a-b` (a2c23e), the TX1 feed with solid copper and the lossy floor through the
committed `--stage`/`--mesh-from` runner; `palace-fix1b` (b89a65) and the Dk pair
(fee3a1 submission 4) ran later in the afternoon, after several re-submissions out of Batch's
queue (`gcp-spend.md`); every task passed. openEMS on the Mac (`val2/oems-local/run.sh`,
`run2.sh`, 4 threads through `gapfix/heavy.sh`, $0) instead of the planned GCP campaign: the
tx12 pair with L1 as 35 µm PEC and as the sheet, the MSL pair with L2 as a conducting sheet, the
single patch, all at 20 µm fill with 8 cells across the core. 2D references: `val2/xsec_ref2.json`
(35 µm copper in the port box, L2 lossy or not, Dk 3.33/3.56/3.66). Scripts: `val2/code/`
(`build_fix.py`, `analyze.py`, `patch2.py`, `fig_fix.py`, `xsec_ref2.py`); figure
`figs/val-fix-tx12.png`.

![fix](figs/val-fix-tx12.png)

### 10.1 Physics 1: the copper thickness finding, argued again

Accepted. "openEMS cannot see this" was wrong and is gone from §5, `val/code/mesh_solid.py` and
`docs/rf-palace.md` §7: openEMS draws L1 as 35 µm PEC (`feed_sim_x.py --thick 0.035 --zcu 0.035`,
the review's copy, now in `val/code`), lossless, read with each port's own line impedance (its
MSL port misreads the current on thick copper; `analyze.py: modal_oems`). Results in the §5
table: the notch is at **61.30 GHz** (Palace, solid copper, lossy floor, refined mesh; 61.13 on the
initial mesh) and **61.42 GHz** (openEMS, 20 µm, 8 z-cells; 61.02 at 27 µm with 4): 0.2 % apart
at each solver's finest mesh, both still rising slightly with refinement. The 1.8 dB / 39° at
62.05 GHz of the first report is replaced by ranges (§5): −0.3 to −2.6 dB, +35° to +41°. The odd
transmission of tx12-B with solid copper is explained by the power balance (§5). Dk range
(Physics 1, the Dk range of `stackups.py`, 3.33–3.66) [S]: Palace order 2 with solid copper and
the lossy floor puts the notch at **62.87 / 61.13 / 60.42 GHz** for Dk 3.33 / 3.56 / 3.66
(`palace-fix1a-a`, 14 min each); the √εeff scaling of the 2D solver's lines predicts 62.86 and
60.43 GHz [D], so the notch follows the line's εeff. With the refinement's +0.17 GHz the range is
about 60.6–63.0 GHz: inside ANT-02 (60.3–63.8 GHz) over the whole Dk range, near its lower edge
at Dk 3.66. What it costs TX1 at 62.05 GHz then spans −2.2 to −6.0 dB of |S21| (A, order 2) for
Dk 3.66 to 3.33: the slivers must go whatever the laminate's Dk turns out to be.

### 10.2 Physics 2: the L2 ground was lossless

Accepted. `domain.boundaries` has a `metal` kind (a lossy conductor wall); the line, feed and
KiCad adapters give the floor as L2 by default (`stackups.floor()`), and L2 gets L1's roughness
factor (K 1.806 at 62.05 GHz: it is the core's other LoPro foil, its treated side bonded to the
core and so facing L1; even a standard ED foil's treated side would give K close to 2, never 1).
Effect [S]: openEMS's MSL pair at 20 µm with 8 z-cells loses 0.0721 dB/mm with the PEC floor and
0.0792 with L2 as a conducting sheet (+0.0071); the 2D solver gives +0.0114 dB/mm for 35 µm
copper (0.0616 → 0.0730, MSL; GCPW 0.0630 → 0.0732); Palace's TX1 feed (B) dissipates 0.18 dB
more over 13.7 mm, and Palace's port-face mode solves (order 3, 62 GHz) put the floor's share at
+0.0136 / +0.0133 dB/mm (MSL / GCPW; 0.0632 → 0.0768 and 0.0653 → 0.0786 dB/mm), with εeff
+0.15 % and Z_PV +0.1 % from the ground's internal inductance. The three methods agree that the
ground adds 9–18 % to the line loss; Palace's increment is 1.2–1.3× the 2D solver's quasi-static
Wheeler estimate (openEMS's, with a zero-thickness strip, 0.6×).

### 10.3 Physics 3: the loss verdict compared numbers that do not converge

Accepted: the sheet-loss verdict is withdrawn (§3), loss is validated on solid copper.
Solid copper, 62 GHz, against the 2D solver in the same
port box [S]: with the PEC floor Palace's mode solves lose 0.0632 / 0.0653 dB/mm against
0.0616 / 0.0630 (+2.6 / +3.7 %, the engineering review's run); with the lossy floor 0.0768 /
0.0786 against 0.0730 / 0.0732 (+5.2 / +7.4 %, a difference of 0.004–0.005 dB/mm: **pass**
against the design's 0.010 dB/mm, and 0.07 dB over the 13.7 mm feed). The 3D microstrip pair
with the lossy floor (5 and 10 mm, `palace-fix1b`) gives εeff 2.768 / 2.743 and 0.0848 / 0.0823
dB/mm at orders 2 / 3, within 0.2 % and 2 % of the open-wall mode solve at order 3 (2.737,
0.0807): Palace is self-consistent on solid copper as it was on the sheet. The GCPW pair (order 2) gives 2.677 and 0.0834 dB/mm against
the port-face mode solve's 2.680 and 0.0814 at the same order. **Issue 4 (port modes drifting in a refinement loop), reproduced
as the review asked** (`palace-fix1b`, line-msl-5mm, five nonconforming refinements at 62 GHz,
`MaxSize` 1.2 M) [S]:

| Copper                          | Meshes (unknowns)                  | Z_PV of P1 / P2 (Ω)                                       | \|S21\| (dB), phase                      |
| ------------------------------- | ---------------------------------- | --------------------------------------------------------- | ---------------------------------------- |
| PEC strip, PEC floor (lossless) | 0.21 / 0.25 / 0.42 / 0.87 / 1.99 M | 55.7 / 55.8 → 56.4 / 56.2 → 58.8 / 56.7 → **61.5 / 59.0** | −0.23 → −0.39 → **−0.95**, 89.5° → 96.1° |
| solid 35 µm, lossy floor        | 0.24 / 0.30 / 0.63 / 1.54 M        | 53.4 / 53.4 → 53.8 / 53.8 → **53.9 / 53.9**               | −0.50 → −0.47, 100.2° → 104.0°           |

With a zero-thickness strip the drift is there without any `Impedance` (a lossless line losing
0.95 dB, its two identical ports parting by 2.5 Ω): it belongs to the refinement of a strip edge
that is singular where it crosses the port face, not to the surface impedance. With solid copper
the ports stay identical and Z_PV moves 1 % while the mesh converges. The upstream report
(open item 1) can say so; sign-off models use solid copper and one refinement anyway.

### 10.4 Physics 4: openEMS was refined in-plane only

Accepted. 8 cells across the core (and the bondply) at 20 µm fill [S]: the lines' εeff is
unchanged (2.946), their Z +2 %, loss −10 %, |S11| +2.4 dB (the physics review's 4/8/12-cell
runs, reproduced here at 8 cells); the sheet notch moves +0.09 GHz (59.94 → 60.03 GHz), the
thick-copper notch +0.40 GHz (61.02 at 27 µm/4 cells → 61.42 at 20 µm/8 cells), the patch dip
+0.13 GHz (62.54 → 62.68 GHz). The εeff extrapolation of §3 stands; Z, loss and |S11| statements
are about in-plane refinement only, and openEMS sign-off runs use 8 z-cells (READY.md).

### 10.5 Physics 5: the patch verdict

Accepted: **explained, not pass** (§4). openEMS with 8 z-cells is 0.98 % below Palace order 3; the
Re Zin peak is reference-plane dependent here and only supports the explanation (0.10–0.17 %
apart 0.3 mm past P1). Finding 3's patch number redone with solid copper [S]: Palace
order 2 (`patch-finite-sg`, 1.32 M unknowns, 21 min) puts the dip at the lumped port 0.27 GHz
above the sheet run (63.37 against 63.11 GHz, +0.4 %; de-embedded to P1 63.44 against 63.14 GHz).
So with real copper the stage-2 patch (calibrated to 61.9 GHz on the 40 µm openEMS mesh) sits at
about 63.0–63.6 GHz on converged meshes: tuned 2–3 % high, for the stage-3 owner.

### 10.6 Physics 6: no PML in Palace

Accepted, for arrays. `docs/rf-palace.md` §10 and READY.md: walls at least λ0 from radiating
copper and the radome, second order, and a box study (two wall distances) on coupling, active
S11 and pattern before a bank or column-with-neighbours result is trusted. A first-order wall
reflects (1 − cos θ)/(1 + cos θ) of a plane wave: −15 dB at 45°, −9.5 dB at 60° [D]. No bank
model exists yet, so the study itself is stage-3 work.

### 10.7 Physics 7: the launch runs under solder mask

Accepted. A dielectric with `coat: {layer, fill}` is a coating of that layer; over solid copper
"conformal" draws it `h` on the laminate and around the copper (shapely buffer), "level" fills
the gaps to the copper's top; the copper is cut out of it (`yapnr.rf.palace.mesh.coat_regions`).
`stackups.solder_mask(L1, outline=...)`: 17 µm, Dk 3.8, Df 0.025 (the stage-2 plan's 15–20 µm,
Dk 3.5–4; Df assumed). Tested by meshing a GCPW under both fills (mask volumes within 2–3e-4 mm³
of the closed forms). Not yet run on a launch: there is no launch model before stage 3.

### 10.8 Physics 8: headline numbers

Accepted: the sheet notch agreement is "within 0.4 %" (not 0.07 %), the absolute-phase gap is not
all openEMS's (Palace order 2 → 3 moves εeff 0.65 %, about 6°), and Z at 62 GHz is not compared
across the solvers (different definitions and boundaries, §3).

### 10.9 Engineering 1–8

| #      | Finding                                                                                                     | What changed                                                                                                                                                                                                                                                                                                                                                                                                |
| ------ | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 HIGH | `case` on a non-validation name wrote `Conductivity` copper, `AdaptiveTol` 1e-4 and 6 refinements up to 4 M | `Impedance` is the default everywhere (`--copper-bc` to change it); `config.REFINEMENT` (one nonconforming step, 2 M) is the default; `validation.signoff_settings` (solid copper, 1e-3, 30 samples, one refinement) for any model not named like a validation case; tests (`SignoffTest`).                                                                                                                 |
| 2 HIGH | per-job `ranks`/`memory_gb` accepted, placement per campaign                                                | Confirmed with a correction: a 16-rank job gets a 32-vCPU shape (`c4d-highcpu-32`), not 16 vCPUs, but no template exists for it, so it was refused at submit like w3f's 56 GB job; and a 4-rank job was packed two to a VM with both bound to the same cores. Now every job's shape is checked (instance policy when any lacks a template) and a job with fewer ranks reserves the campaign's cores; tests. |
| 3 MED  | analysis only in `val/code`                                                                                 | `yapnr.rf.palace.results` (`to_50`, `eeff_loss`, `notch`/`dip`/`re_peak`, `deembed_lumped`, `mode_results`, `stage_dirs`, `compare`) with tests; this round's analysis uses it.                                                                                                                                                                                                                             |
| 4 MED  | stages named alike share a directory                                                                        | outputs are `stage-<k>-<stem>`; a stage listed twice is refused; test reproducing the review's collision.                                                                                                                                                                                                                                                                                                   |
| 5 MED  | no stage-3 recipe; pads not read; FEA venv in a scratchpad; bank not planned                                | `docs/rf-palace.md` §9 (a sign-off run step by step) and §10 (sizes, memory, the bank), READY.md; `from_kicad` reads footprint pads (rect, roundrect, circle, oval, custom gr_poly/gr_rect/gr_circle; plated holes as vias; all 625 pads of radar60-reva read, none skipped); the FEA venv is `yapnr/.venv/palace` (main checkout) with its recipe in the docs.                                             |
| 6 LOW  | spend rows did not add up; egress and storage left out; records written last                                | `gcp-spend.md` rows corrected (w2b $0.08, w4 $0.56: P4 $1.42), cross-region image pulls (~$0.16) and registry storage (~$0.19/month) added; `palace_job.py` writes the record at the start, after each stage and at the end. Not reconciled with billing (no billing export): open.                                                                                                                         |
| 7 LOW  | image not reproducible, licence label incomplete                                                            | Dockerfile labels: SPDX expression of the image's licences incl. `LicenseRef-ParMETIS`, `org.opencontainers.image.revision` from `palace_plan.py image` (`_YAPNR_COMMIT`); README: what is pinned and what is not. The validated digest is kept (not rebuilt).                                                                                                                                              |
| 8 LOW  | one commit behind main; Bazel never run; BUILD deps                                                         | merged `origin/main` (#30); `//yapnr/rf:package` in the planar and palace BUILD files; Bazel run locally and in CI (green on PR #46).                                                                                                                                                                                                                                                                       |

## Sources (accessed 2026-10-04 UTC)

- Palace at b797ea8 (the image's pin): <https://github.com/awslabs/palace/tree/b797ea8060a52241cd9ab1176199f06af8585816>,
  in particular `docs/src/guide/boundaries.md` (Impedance is "computed from the parallel
  impedances" of Rs, Ls, Cs; wave ports, WavePortPEC), `docs/src/guide/problem.md`
  (BoundaryMode from a 3D mesh), `palace/models/modeoperatorassembly.cpp` (the conductivity
  term added without an empty check), `palace/fem/mesh.hpp` (`GetCeedBdrAttributes` skips
  attributes not local to the rank), `palace/fem/libceed/coefficient.cpp:74` (the abort),
  `palace/models/surfaceimpedanceoperator.cpp` (`attr_scaling` 2 on cracked attributes),
  `palace/models/surfaceconductivityoperator.cpp` (HFSS finite-thickness formula; External
  doubles the thickness), `palace/drivers/basesolver.cpp` (the AMR loop, `SaveIteration`),
  `palace/utils/geodata.cpp` (`<stem>.meshgz` of SaveAdaptMesh), `palace/drivers/drivensolver.cpp`
  (the adaptive sweep's greedy sampling).
- openEMS v0.37.0-rc3 (image `openems:0.37.0-rc3-x86-64`, `radar60/cloud-em/READY.md`),
  MSL port and conducting-sheet model: <https://www.openems.de> (accessed 2026-10-04).
- GCP Spot prices: the Cloud Billing Catalog as cached in yapnr `yapnr/exp/data/gcp-spot-prices.json`
  (accessed 2026-10-04): C4D us-west4 $0.00723 per vCPU-h + $0.000772 per GB-h; C4 Montreal
  $0.00483 + $0.000549.
- Internal: `palace/design.md`, `pipeline.md`, `image.md`; `radar60/rf-uniform/em-baseline.md`
  and its runs; `radar60/stage2-rf.md` §3 (line table) and §7 (patch calibration, `rf-runs/`);
  `radar60/cloud-em/READY.md`.
