# Order 0: predictions

**Drafts.** These files are regenerated with the boards until the pre-registration release
([preregistration.md](../preregistration.md)); from then on they are frozen and never edited.

## Coupons

[coupons/](coupons/) holds the expected S-parameters of every modelled coupon stick of each upload,
at nominal parameters on FR408HR (`fr408hr/`) and on the EM528 alternate (`em528/`), with the
FR408HR geometry in both:

- two-port sticks: reference plane to reference plane (what the multiline-TRL-corrected
  measurement shows), 30 MHz-6 GHz in 30 MHz steps, renormalized to 50 Ω from the TRL line's Zc;
- the C-pads of A16: S11 at each pad's reference plane, 6 MHz-6 GHz in 6 MHz steps (the
  measurement grid; 0.1-1 GHz is the band that is fitted; the step from the line to the pad is
  left out, about 0.5 % of the pads' capacitance difference);
- `scalars.json`: the numbers the held-out criteria test, from the model's own |S21| minimum on a
  10 kHz grid, not the 30 MHz files: the ring A11's notches n = 1 and 3 (FR408HR 1.924 and
  5.793 GHz) and the stub A12's notch (5.500 GHz). The ring's arcs and the stub carry Hammerstad's
  T-junction reference planes;
- `lines.csv`: every line family's εeff, α (dB/cm) and Zc on the 6 MHz measurement grid.

The model is the coupon forward model: the 2D quasi-static RLGC of each line family (Djordjevic-
Sarkar dielectrics, Huray roughness, Wheeler's conductor loss, the shipped 2D tables) cascaded
with closed-form discontinuities. The launches are not modelled: the calibration removes them.
The thru and the reflect are calibration standards and have no file.

```sh
python -m yapnr.rf.coupons expected --stackup OSHPARK-4L-FR408HR --upload M \
    --out docs/rf/order0/predictions/coupons/O0-M
```

## Every stick and its prediction

Raw solver files are byte-for-byte copies of the cloud tasks' outputs (yapnr commit `917222a`,
image `ghcr.io/studio-fug/yapnr@sha256:f41a4760…`, native float64); the loss-corrected files
derived from them are in [corrected/](corrected/summary.json) and are the predictions the criteria
use ([pre-registration](../preregistration.md#acceptance-criteria-draft)). "FR408HR" is the
thickness-equivalent substrate of nominal FR408HR (M-eq, W-eq) for the solver and nominal FR408HR
for the coupon model; "EM528" likewise (M-eq-em528, W-eq-em528); the solver's raw nominal view
(zero-thickness copper on M-nom, W-nom) is published besides.

### O0-M

| Stick            | FR408HR                                                                                                                                                                                     | EM528                                                                               | Method                                          |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------- |
| A01 thru         | none between its reference planes (a calibration standard: the identity); the launches as tier-1 data see them: [openems/a01.s2p](openems/)                                                 | –                                                                                   | openEMS 3D (launch)                             |
| A02-A05, A04R    | [coupons/O0-M/fr408hr/](coupons/O0-M/fr408hr/) `A0x-M-line.s2p`                                                                                                                             | [coupons/O0-M/em528/](coupons/O0-M/em528/)                                          | coupon model                                    |
| A06 reflect      | none (a standard; its reflection is not assumed)                                                                                                                                            | –                                                                                   | –                                               |
| A07 verification | `A07-M-verify.s2p`                                                                                                                                                                          | the same                                                                            | coupon model                                    |
| A08, A09, A10    | `A08-M07-variant.s2p`, `A09-M14-variant.s2p`, `A10-MMK-variant.s2p`                                                                                                                         | the same                                                                            | coupon model                                    |
| A11 ring         | `A11-M-ring.s2p`, notches in `scalars.json`; [fdtd/a11-ring-m-eq/](fdtd/)                                                                                                                   | the same; [fdtd/a11-ring-m-eq-em528/](fdtd/)                                        | coupon model; our FDTD (forward, R1's settings) |
| A12 stub         | `A12-M-stub.s2p`, notch in `scalars.json`; [fdtd/a12-stub-m-eq/](fdtd/)                                                                                                                     | the same; [fdtd/a12-stub-m-eq-em528/](fdtd/)                                        | coupon model; our FDTD                          |
| A14 switch-term  | `A14-M-switch.s2p`                                                                                                                                                                          | the same                                                                            | coupon model (0402 as 100 Ω + 0.6 nH)           |
| A15 tag, DC      | no RF port                                                                                                                                                                                  | –                                                                                   | –                                               |
| A16 C-pads       | `A16-C6-pad1.s1p`, `A16-C12-pad2.s1p`                                                                                                                                                       | the same                                                                            | coupon model                                    |
| R1               | [run0b/r1-m-eq.s3p](run0b/r1-m-eq.s3p), corrected `r1-m-eq-loss-corrected.s3p`; raw nominal view [references/r1-m-nom/](references/r1-m-nom/), corrected `r1-m-nom-…`; [openems/](openems/) | [references/r1-m-eq-em528/](references/r1-m-eq-em528/), corrected `r1-m-eq-em528-…` | our FDTD, refine 2; openEMS 3D (FR408HR)        |

### O0-W light

| Stick       | FR408HR                                                                                                                                                                                                 | EM528                                                                          | Method       |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------ |
| B01 thru    | none (a standard)                                                                                                                                                                                       | –                                                                              | –            |
| B02-B04     | [coupons/O0-W/fr408hr/](coupons/O0-W/fr408hr/) `B0x-W-line.s2p`                                                                                                                                         | [coupons/O0-W/em528/](coupons/O0-W/em528/)                                     | coupon model |
| B05 reflect | none (a standard)                                                                                                                                                                                       | –                                                                              | –            |
| R1t         | [references/r1t-w-eq/](references/r1t-w-eq/), corrected `r1t-w-eq-…`; raw nominal view [references/r1t-w-nom/](references/r1t-w-nom/)                                                                   | [references/r1t-w-eq-em528/](references/r1t-w-eq-em528/)                       | our FDTD     |
| D2 window   | if the owner ships the nearest miss `d2-star-sched`: [D2/runs/d2-star-sched/](D2/runs/d2-star-sched/) (W-eq, three grids), [D2/variants/](D2/variants/) (W-eq wide, W-nom); corrected `d2-star-sched-…` | [D2/variants/d2-star-sched-w-eq-em528/](D2/variants/d2-star-sched-w-eq-em528/) | our FDTD     |

### O0-D

| Stick    | FR408HR                                                                                                                                                                                                                  | EM528                                                              | Method                         |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ | ------------------------------ |
| D1       | [D1/runs/d1-star/](D1/runs/d1-star/) (M-eq: coarse, fine, finer), [D1/variants/d1-star-m-eq/](D1/variants/d1-star-m-eq/) (3-7 GHz), [d1-star-m-nom/](D1/variants/d1-star-m-nom/); corrected `d1-…`; [openems/](openems/) | [D1/variants/d1-star-m-eq-em528/](D1/variants/d1-star-m-eq-em528/) | our FDTD; openEMS 3D (FR408HR) |
| R1 copy  | as O0-M's R1                                                                                                                                                                                                             | as O0-M's                                                          | –                              |
| A01      | as O0-M's A01                                                                                                                                                                                                            | –                                                                  | –                              |
| A20, A04 | [coupons/O0-D/fr408hr/](coupons/O0-D/fr408hr/) `A20-M-line.s2p`, `A04-M-line.s2p`; A04 with its launches: [openems/a04.s2p](openems/)                                                                                    | [coupons/O0-D/em528/](coupons/O0-D/em528/)                         | coupon model; openEMS 3D       |

## The loss correction

`python -m yapnr.rf.order0 correct --out docs/rf/order0/predictions/corrected` writes the
corrected file of every demo and reference prediction and
[corrected/summary.json](corrected/summary.json) (raw and corrected worst cases over
4.25-5.75 GHz, Δα and the ratio at 5 GHz per substrate). Each substrate uses its own loss lines:
M-eq run 0b's ([run0b/](run0b/)), W-eq the compute stage's ([references/line-w-\*](references/)),
the others the predictions stage's ([lines/](lines/)).

## Our FDTD on the held-out resonators

[fdtd/](fdtd/) holds the forward runs of the ring A11 and the stub A12 (the tuned O0-M geometry
from reference plane to reference plane, on the references' 0.05 mm grid, the stub's open end
snapped to the grid: 8.279 mm drawn, 8.30 mm simulated), and [fdtd/resonators.json](fdtd/resonators.json)
their notches next to the coupon model's. The criterion stays the coupon model's post-fit notch;
these are the optimizer's own solver predicting a held-out resonance before fabrication.

## openEMS (D-O0-13a)

[openems/](openems/README.md): D1, R1 and the 0.40 mm line with 43 µm copper on the nominal
FR408HR stack at two meshes, and A01/A04 with the Cinch launches; the models are
[../openems/](../openems/) (`order0_em.py`).
