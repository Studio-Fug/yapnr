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
   - D1, D2: the optimizer's validation runs on the thickness-equivalent and on the nominal
     substrate, with the spec, `result.json`, `validation.json` and the footprint.
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
   the fitted loss.
4. **The full optimizer history**: every D1 and D2 attempt with its validation, failures included,
   and the rule that picked the shipped run.
5. **The criteria** (below) and the analysis order.

D1 and D2 carry the first 8 hex digits of the sha256 of their `result.json` in silkscreen, which
ties the physical part to its prediction.

## Proof that the predictions came first

A commit date proves nothing (authors set it). Before upload:

- the release is created, and the sha256 of its bundle is stamped with
  [OpenTimestamps](https://opentimestamps.org/) (free); the `.ots` proof is added here;
- optionally, the release gets a Zenodo DOI.

## Acceptance criteria (draft)

| Item                                | Criteria                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Coupons, held out                   | Ring notches (n = 1 and 3) and the stub notch within 0.5 % of the **post-fit** model. A measured notch frequency comes from fitting the model's notch shape to the data around it, not from the lowest sample (the n = 3 notch lies 10-20 dB above the instrument floor). Verification line consistent with the mTRL uncertainty.                                                                                                                                                                                                                                                                                                                                                                                                                                |
| D1, D2: "meets spec"                | Measured (mTRL-corrected, renormalized to 50 Ω with the fitted line Z0 and its uncertainty) \|S11\| ≤ −20 dB and \|S21\|, \|S31\| ≥ −3.4 dB over 4.25-5.75 GHz (−3.4 dB confirmed by run 0b under D-O0-11 before D1 runs: the loss-corrected R1 reaches −3.285 dB; [demos][run0b])                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| D1, D2: "meets validation criteria" | \|S11\| ≤ −15 dB and \|S21\|, \|S31\| ≥ −3.6 dB                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| D1, D2: "agrees with prediction"    | Over 4.25-5.75 GHz, in the optimizer's port reference: the measured data, which mTRL gives in the fitted Zc of the region's line, are renormalized to the port impedance the solver calibrates for that run (`yapnr.rf.ports.calibrate_line`, recorded with the result) before comparing. On W the two differ by about 1.4 Ω (the TRL line 48.6 Ω with its L1 pour and inner-layer ring, the solver's port on open L1 about 50.0 Ω), \|Γ\| 0.014 per port. Then: max \|S11,meas − S11,pred\| ≤ 0.05; frequencies of the in-band \|S11\| minima within ±1.5 %; \|S21\| within ±0.25 dB; \|S21\| − \|S31\| within 0.3 dB. A difference inside the k = 2 measurement uncertainty, reported next to every number, also counts as agreement. The same for R1 and R1t. |
| D1 against R1, D2 against R1t       | The claim tested is "the optimizer automatically matches a textbook-class design", not that it beats one: both against the same criteria, with footprint area, worst in-band \|S11\| and \|S21\|, imbalance, compute hours and the number of optimizer attempts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

Every result is reported three ways: against the pre-registered nominal prediction, against the
post-fit prediction (the stackup fitted from this lot's coupons), and against the spec. The
instrument's floor is drawn on every transmission plot. Criteria are never changed after data
arrive; if one must be, results are reported against both versions.

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
