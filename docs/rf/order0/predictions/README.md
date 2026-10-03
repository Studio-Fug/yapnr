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
  measurement grid; 30-300 MHz is the band that matters).

The model is the coupon forward model: the 2D quasi-static RLGC of each line family (Djordjevic-
Sarkar dielectrics, Huray roughness, Wheeler's conductor loss, the shipped 2D tables) cascaded
with closed-form discontinuities. The launches are not modelled: the calibration removes them.
The thru and the reflect are calibration standards and have no file.

```sh
python -m yapnr.rf.coupons expected --stackup OSHPARK-4L-FR408HR --upload M \
    --out docs/rf/order0/predictions/coupons/O0-M
```

## Demos and references

| Item               | Prediction                                                                   | Status                                                         |
| ------------------ | ---------------------------------------------------------------------------- | -------------------------------------------------------------- |
| [D1](D1/README.md) | the optimizer's validation runs (thickness-equivalent and nominal substrate) | not yet: D1 has not been optimized for Order 0                 |
| [D2](D2/README.md) | the same, region W                                                           | not yet                                                        |
| R1, R1t            | the optimizer's FDTD, forward only, loss-corrected                           | not yet: the forward-simulation command is still to be written |
