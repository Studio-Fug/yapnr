# Order 0: pre-registration

The point of Order 0 is a public comparison of measured boards with simulations that were
published **before** the boards were made. This page says what will be frozen, how the freeze is
made verifiable, and how the data will be judged. It is a draft for the owner's review: the
criteria below become binding when the pre-registration release is published.

## What is frozen before upload

The release (a GitHub release of this directory, its tag `order0-prereg-<upload>`) holds, for
each upload:

1. **The boards**: the KiCad projects and the sha256 of the Gerber zip that is uploaded to
   OSH Park (from the fab bundle's manifest), the stick catalogue, the generator commit. The
   sha256 is taken from the bundle built from the committed board files: regenerating them with
   another Python or numpy can move a coordinate by 0.1 µm, so the release names the interpreter
   (`bazel run //yapnr/rf/coupons:cli`, the pinned `requirements.lock`). A board whose optimizer
   window is still empty fails `yapnr fab check` (`FAB-PLACEHOLDER`) and cannot be built.
2. **Predicted Touchstone files for every stick**, at nominal FR408HR and at nominal EM528:
   - coupons (lines, width set, masked line, ring, stub, switch-term coupon, C-pads): the coupon
     forward model, a 2D quasi-static RLGC cascade ([predictions/](predictions/README.md)), on a
     30 MHz grid; the numbers the criteria test are **pre-registered as scalars** besides
     (`scalars.json`: the ring's and the stub's notch frequencies to 10 kHz; `lines.csv`: every
     line family's εeff, α and Zc on the 6 MHz measurement grid);
   - R1, R1t: the RF optimizer's FDTD, forward only, on the same substrates as the demos, at the
     solver's second grid refinement (0.05 mm on M, 0.25 mm on W), on which every edge of their
     copper lies (R1's 0.70 mm arm is 14 cells about a node there; on the 0.10 mm grid it would be
     drawn 0.6 or 0.8 mm). Their geometry is frozen in this release as drawn: a quarter wave from
     Hammerstad's T-junction reference plane, a closed-form correction; it is not tuned after
     any D1 or D2 result;
   - the ring A11 and the stub A12 also in the optimizer's FDTD, forward only, with R1's settings
     (0.05 mm, the FR408HR and EM528 thickness-equivalent substrates): a second, independent
     prediction of the held-out notches next to the coupon model's;
   - D1, D2: the optimizer's validation runs on the thickness-equivalent and on the nominal
     substrate (and on EM528's thickness-equivalent one), with the spec, `result.json`,
     `validation.json` and the footprint;
   - the thru and the reflects (A01, A06, B01, B05) are calibration standards (the thru is the
     identity between its reference planes, the reflect is not assumed), A15 has no RF port; the
     tier-1 view of A01 and A04 (the launches included) is the openEMS prediction below.
     Every solver prediction names its source: yapnr commit `917222a` (the bundle the cloud tasks
     ran; the `result.json` provenance shows the image wheel's version string, `688fac3`) and the
     image `ghcr.io/studio-fug/yapnr@sha256:f41a4760…` with its native float64 kernel.
3. **The solver's loss correction**: the predicted \|S21\| of the demos and references is published
   twice, as the solver gives it and corrected for the conductor loss the solver does not model
   (rough copper, the ground plane's loss, ENIG); the corrected one is the prediction. The
   correction makes the solver's α(f) of a straight 0.40 mm (M) or 3.0 mm (W) line equal to the
   shipped coupon model's on the same nominal stackup (`lines.csv`; M 0.132 dB/cm at 5 GHz, with the
   roughness prior Rq 1.0 ± 0.5 µm). Run 0b measures the solver's α(f) from straight lines of two
   lengths on the references' grid (0.40 and 0.70 mm wide, 10 and 30 mm long); the difference Δα(f)
   is applied along R1's path (the 0.70 mm arm to the junction, then half the output line), and for
   any geometry (D1, D2) the fraction of the incident power the solver dissipates is scaled by
   α_coupon/α_solver of the port line ([demos][run0b]). Closed forms disagree on the smooth part
   (the coupon model's 2D Wheeler factor gives 0.037 dB/cm on M at 5 GHz, Hammerstad-Jensen 0.059),
   so the measured α(f) of the lines, not either model, settles it, and the post-fit prediction uses
   the fitted loss. Each substrate is corrected with its own lines (M-eq: run 0b's; M-nom, M-eq-em528,
   W-eq, W-nom, W-eq-em528: the same lines run on that substrate) against the coupon model of the
   board it stands for (FR408HR, or EM528 for the `-em528` substrates); R1t uses the 3.0 mm line's
   Δα along its path (region W has no 5.0 mm line run). The corrected files and the raw and
   corrected worst cases are in [predictions/corrected/](predictions/corrected/summary.json)
   (`python -m yapnr.rf.order0 correct`).
4. **The full optimizer history**: every D1 and D2 attempt with its validation, failures included,
   and the rule that picked the shipped run.
5. **An independent 3D prediction** (openEMS 0.37, [predictions/openems/](predictions/openems/README.md)):
   D1 and R1 with 43 µm copper on the nominal FR408HR stack at two meshes, the 0.40 mm line, and
   the sticks A01 and A04 with the Cinch launches as the tier-1 calibration sees them, with the
   comparison against the optimizer's FDTD stated as found: the two agree on \|S21\| within
   0.09 dB, but openEMS predicts D1's worst \|S11\| at −17.4 dB (0.05 mm mesh) and −18.2 dB
   (0.025 mm), a miss of the −20 dB spec, against yapnr.rf's −20.9 dB, with the in-band minimum
   about 10 % higher in frequency. That competing prediction is registered as it stands.
6. **The criteria** (below) and the analysis order.

D1 and D2 carry the first 8 hex digits of the sha256 of their `result.json` in silkscreen, which
ties the physical part to its prediction.

## Proof that the predictions came first

A commit date proves nothing (authors set it). Before upload:

- the release is created, and the sha256 of its bundle is stamped with
  [OpenTimestamps](https://opentimestamps.org/) (free); the `.ots` proof is added here;
- optionally, the release gets a Zenodo DOI.

## Acceptance criteria (draft)

The table is the Order 0 design's (§7), with its review's replacement of "agrees with
prediction" (the −15 dB band edges it first named lie above the LibreVNA's 6 GHz: R1's −15 dB
band is 3.3-6.7 GHz, 68 %). D3 and D4 (the antenna and the diplexer) are stretch items that do
not ship on Order 0; their rows are kept as written.

| Item                                 | Criteria                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Coupons, held out                    | Ring f_n and stub notch within 0.5 % of the **post-fit** model. Verification line consistent with the mTRL uncertainty.                                                                                                                                                                                                                                                                                                         |
| D1, D2: "meets spec"                 | Measured (mTRL-corrected, renormalized to 50 Ω) \|S11\| ≤ −20 dB and \|S21\|, \|S31\| ≥ −3.4 dB over 4.25-5.75 GHz                                                                                                                                                                                                                                                                                                              |
| D1, D2: "meets validation criteria"  | \|S11\| ≤ −15 dB and ≥ −3.6 dB                                                                                                                                                                                                                                                                                                                                                                                                  |
| D1, D2: "agrees with prediction"     | Max over 4.25-5.75 GHz of the vector difference \|S11,meas − S11,pred\| ≤ 0.05 (a −20 dB S11 is 0.10), in the mTRL native reference (line Z0, the same as the solver's modal port); frequencies of in-band \|S11\| minima within ±1.5 %; \|S21\| within ±0.25 dB; \|S21\| − \|S31\| within 0.3 dB. "Agrees" also counts a difference that lies inside the k = 2 measurement uncertainty, which is reported next to each number. |
| D1, D2, R1, R1t: reference impedance | "Meets spec" is evaluated after renormalizing to 50 Ω with the fitted line Z0, and that Z0 uncertainty is propagated. ±1.3 Ω without A16 moves a −20 dB \|S11\| by about ±1 dB; ±0.5 Ω with A16 moves it by ±0.4 dB.                                                                                                                                                                                                            |
| D1 against R1                        | The claim tested is **"the optimizer automatically matches a textbook-class design"**, not that it beats one. Report both against the same criteria, plus footprint area, worst in-band \|S11\| and \|S21\|, imbalance, compute hours and the number of optimizer attempts.                                                                                                                                                     |
| D3 (stretch, not on Order 0)         | Frequency of the \|S11\| minimum within ±1 %, and the −10 dB bandwidth within ±20 % (relative)                                                                                                                                                                                                                                                                                                                                  |
| D4 (stretch, not on Order 0)         | Channel centres within ±1 %, insertion loss within 0.3 dB, rejection within 3 dB of prediction while at least 10 dB above the floor                                                                                                                                                                                                                                                                                             |

Each result is reported three ways: against the pre-registered nominal prediction, against the
post-fit prediction (stackup fitted from the coupons), and against the spec. The instrument
floor is drawn on every transmission plot.

How the rows are applied (decided before any measurement; none of these changes a limit):

- **The prediction compared** is the loss-corrected one ([predictions/corrected/](predictions/corrected/summary.json)):
  the solver's \|S21\|, \|S31\| with the conductor loss it does not model added from the coupon
  model, per substrate. "Meets spec" uses −3.4 dB (run 0b, D-O0-11: the corrected R1 reaches
  −3.285 dB, [demos][run0b]); it applies to R1 and R1t as well, which are judged like D1 and D2.
- **The reference of "agrees"**: the solver's modal port is calibrated on the open-L1 line of the
  run (`yapnr.rf.ports.calibrate_line`, recorded with each result); the multiline TRL gives the
  measured data in the fitted Zc of the board's line, which on W has its L1 pour and the
  inner-layer ring (about 48.6 Ω against the solver's 50.0 Ω, \|Γ\| 0.014 per port). The
  measured data are therefore renormalized from the fitted TRL-line Zc to the run's port
  impedance before the comparison; on M the two agree within the fit's uncertainty.
- **Measured notch frequencies** (ring, stub) come from fitting the model's notch shape to the
  data around the notch, not from the lowest sample (the n = 3 notch lies 10-20 dB above the
  instrument floor).
- **D2** ships only as the owner decides (no D2 formulation meets the −20 dB \|S11\| spec; the
  nearest, `d2-star-sched`, reaches −19.3 to −19.4 dB and passes its \|S21\|, \|S31\| validation
  limits, [D2](predictions/D2/README.md)). If it ships, it is judged by the same rows, labelled a
  design that missed its \|S11\| spec.
- Criteria are never changed after data arrive; if one must be, results are reported against
  both versions.

### The k = 2 uncertainty model

Every comparison above carries a per-frequency k = 2 band (see [Uncertainty to publish](#uncertainty-to-publish)):
the linear propagation of the multiline TRL's inputs (Hatab's method and code,
[uncertainty-multiline-trl-calibration](https://github.com/ZiadHatab/uncertainty-multiline-trl-calibration),
BSD-3: standard definitions, line lengths, noise), the measured connection repeatability (three
connections), the copy-to-copy spread (two copies, one lot), the fitted line Z0's uncertainty and
the multiline-TRL against IEEE 370 2x-thru difference. Before the boards exist, the expected size
is the Monte Carlo of the coupon set with the LibreVNA's specified noise and a prototype
connection-repeatability model (solder variation σ_L 0.02 nH, σ_C 0.01 pF per launch): εeff
±0.37 % per point (±11 MHz at 5.8 GHz), α ±0.0044 dB/cm, Z0 ±1-1.5 Ω model-assisted (±0.5 Ω with
A16); twice the solder variation gives εeff ±0.65 %. The measured repeatability replaces the
prototype numbers.

## The C-pads (A16): how h is read

A16's two pads (6 and 12 mm square) give the prepreg height h, and with it the absolute Z0. The
launch and the 10 mm line before each pad are not negligible at low frequency (about 10° at
300 MHz, against 0.4° for 1 % of the 12 mm pad's capacitance), so the pad-pair difference does not
cancel them. The procedure:

1. De-embed each pad's 1-port with the launch half of the thru A01 (IEEE 370 NZC, the same tier-1
   data); the mTRL error box is the check above 0.5 GHz.
2. Fit 0.1-1 GHz with the distributed pad model (each pad an open-ended wide line with its open-end
   extension, `predictions/.../A16-*.s1p`), h and the prepreg's εr free, the line-to-pad step
   included as its closed-form shunt capacitance.
3. Uncertainty budget: besides the calibration, the dielectric's dispersion (Djordjevic-Sarkar
   puts εr at 100 MHz about 2 % above 5 GHz; with tan δ uncertain by 0.003 that is about ±0.7 % on
   h) and the step model (about 0.5 % of C12 − C6), next to the ±1.6 % the design expects.

## Analysis order (blind)

1. At capture, each demo's raw files are hashed and committed, not de-embedded or plotted.
2. The coupon fit (`yapnr-stackup-fit/1`) is committed first.
3. Only then are D1, R1, D2 and R1t de-embedded and compared. The post-fit prediction never sees
   demo data.

## Uncertainty to publish

Per-frequency k = 2 bands combining: the linear propagation of the calibration (standard
definitions, line lengths, noise), the measured connection repeatability (three connections of
A01, A03, A04 and each demo), the copy-to-copy spread (two copies, one lot), the line Z0
uncertainty, and the difference between multiline TRL and IEEE 370 2x-thru de-embedding from A01.
Not covered, and said so: one lot, two assembled copies, an instrument with preliminary
specifications.

[run0b]: demos.md#run-0b-the-loss-correction-and-the-s21-criterion
