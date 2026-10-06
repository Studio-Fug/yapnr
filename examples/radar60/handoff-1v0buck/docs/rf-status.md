# RF status at handoff: ANT-02 and D14 (macro v2)

Source: `docs/stage3b-rf/freeze.md`, section "Freeze (macro v2): 2026-10-05". The macro v2 freeze is
on
`claude/radar60-rf3b` at `727df41`, merged into `claude/radar60-3d` and this branch. Evidence
labels:
[S] simulated, [D] derived.

## ANT-02 (radiation targets): three preregistered misses

The decision rule for column topology (D5) says: if no topology meets the target, freeze the best
point and preregister the misses. That is what was done. The freeze picked **cell-c1b-6**: K0
cavity,
L ×1.025, inset 0.325, w35 0.42, t_y 0.25, corporate feed.

| Quantity                             | Target   | c1b-6 [S]                                            | Status                              |
| ------------------------------------ | -------- | ---------------------------------------------------- | ----------------------------------- |
| Worst in-band \|S11\|, 60.3–63.8 GHz | ≤ −10 dB | −6.51 dB (openEMS, 20 µm), −7.2 dB (Palace-referred) | **miss, preregistered**             |
| Gain at 60.3 / 62.05 / 63.8 GHz      | ≥ 5 dBi  | 4.80 / 5.90 / 6.87 dBi                               | **miss at 60.3 GHz, preregistered** |
| Efficiency                           | ≥ 0.6    | 0.54 / 0.54 / 0.61                                   | **miss, preregistered**             |

The evidence that no change to the length L alone can close the return-loss miss:

- Even the isolated patch's −10 dB band is only 2.75 GHz wide in Palace, against the 3.5 GHz needed.
- K1, a solid L2 under the patches, never reaches −10 dB at L 1.00/1.015/1.03 [S]: −8.83, −8.27 and
  −7.80 dB. The K rule therefore rejects K1, and K0 stays, with the bondply cavity as a quantified
  warning.
- Palace and openEMS agree on the centring length to within 0.2 %: L ≈ 1.020 versus 1.022.

**Gain-clean alternative:** cell-c1b-2 (w35 0.30, t_y 0.49) reaches −6.34 dB in both frames and
meets the gain floor, but its band is centred 1.8 GHz high (63.81 GHz Palace-referred).

**Not checked:**

- the 15 µm mesh trend of c1b-6 (only the 20 µm result exists);
- the D12 frequency brackets (±1.8 % around L 1.025, the rfm1-m/p variants), which are generated but
  not simulated;
- radar-level checks (RAD-01…10) and measured return loss, which wait for Rev A hardware and 60 GHz
  metrology (board-design §1.3; RF-08 is the owner's booking, D8).

**What would close ANT-02's return loss:** a wider-band element (for example a stacked or
aperture-coupled
patch, a thicker L1–L2 dielectric or a different cavity treatment). That is a geometry change
outside
the current generator's parameter space, so it is a Rev B item unless the owner decides otherwise.

## D14 (PA-feed island coupling): TX1 fails, RX4 passes

D14 is the owner's decision that the macro owns the VOUT_PA feed. Its acceptance rule: island
coupling
≤ −40 dB into TX1 and RX4, and S21 changes ≤ 0.05 dB and 1°, with no dip over 0.1 dB.

| Port case at 62.05 GHz | Baseline (old board) | EM-test fence (model only) | **As built (4-via fence)**                                                            |
| ---------------------- | -------------------- | -------------------------- | ------------------------------------------------------------------------------------- |
| TX1                    | −34.9 dB             | −46.1 dB                   | **−33.5 dB** (−31.6 at 60.3 GHz, −35.3 at 63.8, −26.9 worst over 54–70 GHz) **fails** |
| RX4                    | −42.2 dB             | −45.6 dB                   | **−44.9 dB** (−44.6 to −45.1) passes                                                  |

**Why the as-built fence doesn't work:**

- The generator places its four fence vias at y = pocket top + 0.31 mm, on the bank side (U1 frame y
  7.31).
- The fence that worked in the model was a row at y 4.82, inside the U1 package outline (|x|, |y| <
  5.2) between the TX1 launch and the PA neck. The generator's `package` keepout forbids vias there.
- A south row placed 0 of 4 vias. An east column placed 1 of 4.

So **no fence the macro can build reaches the coupling path**. The remaining lever is under the BGA:
U1's GND ball vias and fanout, which is integration work. The port case is pessimistic, because the
real
island is shorted by capacitors.

**What would close D14:**

- integration places GND ball vias (or via-in-pad) under U1 between the TX1 launch and the PA neck,
  in the fanout plan;
- `merge_macro` keeps the PA-feed copper (today it accepts the `RFM1_PA_FEED` footprint but drops
  its copper);
- then one openEMS point on the routed copper, with the fence geometry in the model matching the
  board.

## Other checks still open

| Check                                               | Status                                                                                                         |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| C2: RF sign-off on routed copper (openEMS + Palace) | **not run**: the board isn't complete                                                                          |
| D14 on routed copper with U1 GND vias               | not run (see above)                                                                                            |
| C1 15 µm convergence of c1b-6                       | not run                                                                                                        |
| D15 (ground-stitching pour)                         | decided as A, with a quantified warning; isolation 36.7–37.4 dB across A/B/C against the RF-07 35 dB floor [S] |
| Dummy columns                                       | S1 ("outer"): isolation 38.2 dB against S2's 36.7 dB [S], TX1 feed 0.28 dB better                              |
| TX feed loss (RF-03)                                | 1.33–1.65 dB [S] for TX3 alone; the D6 waiver (≤ 2.7 dB) most likely stays                                     |
