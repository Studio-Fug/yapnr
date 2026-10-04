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

## Demos and references

| Item               | Prediction                                                                                                                                    | Status                                                                                                                             |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| [D1](D1/README.md) | the optimizer's validation runs: thickness-equivalent (M-eq), nominal (M-nom) and EM528 (M-eq-em528) substrates                               | `d1-star` passes (coarse, fine, finer); every run in `D1/runs/`, the shipped one on the other substrates in `D1/variants/`         |
| [D2](D2/README.md) | the same, region W                                                                                                                            | no formulation passes (8 runs, `D2/runs/`); the nearest miss on the other substrates in `D2/variants/`                             |
| R1, R1t            | the optimizer's FDTD, forward only (`python -m yapnr.rf.order0 write`: forward-run directories), loss-corrected, at refine 2 (0.05 / 0.25 mm) | R1 on M-eq: run 0b; R1 on M-nom and M-eq-em528, R1t on W-eq, W-nom and W-eq-em528 and the W loss lines: [references/](references/) |
