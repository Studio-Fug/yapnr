# Measurement sessions

A session directory is the input of `python -m yapnr.rf.coupons extract` (design §7.6):

```text
<session>/
  session.yaml                      # yapnr-coupon-session/1 (session.json is accepted too)
  A-03_A05-P-L40_r1.s2p             # <board>-<serial>_<stick>-<family>-<what>_r<repeat>
  A-03_A05-P-L40_r2.s2p
  dc.csv                            # serial, stick, layer, width_mm, current_a, voltage_v, temp_c
  microsection.json                 # optional: {"pp1.h": [value_mm, sigma_mm], ...}
```

- Touchstone files are SOLT-corrected at the cable ends (tier 1), 50 Ω, two-port, all on the
  same linear grid starting at the step (10 MHz to 6 GHz in 10 MHz steps on a 6 GHz VNA).
- `<family>` drops the hyphen (`PMO` for P-MO); `<what>` is `THRU`, `L<ΔL>`, `REFL`, `VER<ΔL>`,
  `VAR`, `RING`, `STUB` or `CPL`.
- Repeat the thru and the 40 mm line with re-mated cables (`_r2`, `_r3`): the extraction takes the
  connection repeatability from them.
- `session.yaml` needs `schema: yapnr-coupon-session/1` and records the stackup, a lot alias (not
  the vendor's order number), the board serials, the VNA, the calibration kit, grid, IF bandwidth,
  power, temperature and humidity. An optional `noise:` block overrides the noise model of the fit
  (`yapnr.rf.coupons.fit.NoiseModel`).

`python -m yapnr.rf.coupons synthetic --stackup JLC04161H-7628 --out <dir>` writes a complete
synthetic session in this layout, with a `truth.json` that a real session does not have.

Whether measured sessions are published here is the owner's decision (design §14, question 7).
