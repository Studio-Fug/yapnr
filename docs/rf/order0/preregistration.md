# Order 0: pre-registration

The point of Order 0 is a public comparison of measured boards with simulations that were
published **before** the boards were made. This page says what will be frozen, how the freeze is
made verifiable, and how the data will be judged. It is a draft for the owner's review: the
criteria below become binding when the pre-registration release is published.

## What is frozen before upload

The release (a GitHub release of this directory, its tag `order0-prereg-<upload>`) holds, for
each upload:

1. **The boards**: the KiCad projects and the sha256 of the Gerber zip that is uploaded to
   OSH Park (from the fab bundle's manifest), the stick catalogue, the generator commit.
2. **Predicted Touchstone files for every stick**, at nominal FR408HR and at nominal EM528:
   - coupons (lines, width set, masked line, ring, stub, switch-term coupon, C-pads): the coupon
     forward model, a 2D quasi-static RLGC cascade ([predictions/](predictions/README.md));
   - R1, R1t: the RF optimizer's FDTD, forward only, on the same substrates as the demos;
   - D1, D2: the optimizer's validation runs on the thickness-equivalent and on the nominal
     substrate, with the spec, `result.json`, `validation.json` and the footprint.
3. **The solver's loss correction**: the predicted \|S21\| of the demos and references is
   published twice, as the solver gives it and corrected for the conductor loss the solver does
   not model (rough copper, the ground plane's loss, ENIG); the corrected one is the prediction.
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

| Item                                | Criteria                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Coupons, held out                   | Ring notches and stub notch within 0.5 % of the **post-fit** model. Verification line consistent with the mTRL uncertainty.                                                                                                                                                                                                                                        |
| D1, D2: "meets spec"                | Measured (mTRL-corrected, renormalized to 50 Ω with the fitted line Z0 and its uncertainty) \|S11\| ≤ −20 dB and \|S21\|, \|S31\| ≥ −3.4 dB over 4.25-5.75 GHz (the \|S21\| limit is set before D1 runs, after the loss check)                                                                                                                                     |
| D1, D2: "meets validation criteria" | \|S11\| ≤ −15 dB and \|S21\|, \|S31\| ≥ −3.6 dB                                                                                                                                                                                                                                                                                                                    |
| D1, D2: "agrees with prediction"    | Over 4.25-5.75 GHz, in the mTRL native reference (the line's own Zc, the optimizer's port): max \|S11,meas − S11,pred\| ≤ 0.05; frequencies of the in-band \|S11\| minima within ±1.5 %; \|S21\| within ±0.25 dB; \|S21\| − \|S31\| within 0.3 dB. A difference inside the k = 2 measurement uncertainty, reported next to every number, also counts as agreement. |
| D1 against R1, D2 against R1t       | The claim tested is "the optimizer automatically matches a textbook-class design", not that it beats one: both against the same criteria, with footprint area, worst in-band \|S11\| and \|S21\|, imbalance, compute hours and the number of optimizer attempts                                                                                                    |

Every result is reported three ways: against the pre-registered nominal prediction, against the
post-fit prediction (the stackup fitted from this lot's coupons), and against the spec. The
instrument's floor is drawn on every transmission plot. Criteria are never changed after data
arrive; if one must be, results are reported against both versions.

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
