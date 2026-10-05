# openEMS predictions (D-O0-13a), as found

The independent 3D prediction of D1 and R1 (43 µm copper on the nominal FR408HR stack, two
meshes; models and method in [../../openems/](../../openems/README.md)), loss-corrected like the
yapnr.rf arm, and its comparison with yapnr.rf ([comparison.json](comparison.json)). The launch
sticks A01 and A04 are in [../../openems/runs/](../../openems/runs/) and the `sticks` block of
`comparison.json`.

- `d1-openems-r05-loss-corrected.s3p`, `d1-openems-r025-loss-corrected.s3p`: D1 (`d1-star`,
  label `O0 D1 divider-osh-m ad20e643`) at 0.05 and 0.025 mm in the plane.
- `r1-openems-r05-loss-corrected.s3p`, `r1-openems-r025-loss-corrected.s3p`: R1, the same.

## What the two solvers predict (4.25-5.75 GHz, loss-corrected)

| Design | Solver            | Worst \|S11\| | Worst \|S21\|, \|S31\| | In-band \|S11\| minimum |
| ------ | ----------------- | ------------- | ---------------------- | ----------------------- |
| D1     | yapnr.rf (M-eq)   | −20.90 dB     | −3.306 dB              | 4.93 GHz                |
| D1     | openEMS, 0.05 mm  | −17.42 dB     | −3.322 dB              | 5.48 GHz                |
| D1     | openEMS, 0.025 mm | −18.17 dB     | −3.313 dB              | 5.40 GHz                |
| R1     | yapnr.rf (M-eq)   | −21.34 dB     | −3.285 dB              | 5.04 GHz                |
| R1     | openEMS, 0.05 mm  | −19.69 dB     | −3.295 dB              | 5.15 GHz                |
| R1     | openEMS, 0.025 mm | −20.47 dB     | −3.294 dB              | 5.11 GHz                |

The two agree on transmission (max \|ΔS21\| 0.083 dB on D1, 0.024 dB on R1) and disagree on
D1's match. **openEMS predicts that D1 does not meet the −20 dB \|S11\| spec** (a 2.7-3.5 dB miss)
and that its in-band \|S11\| minimum sits 9.5-11.1 % above yapnr.rf's, outside the ±1.5 % "agrees"
row; yapnr.rf predicts a pass. On R1 the gap is smaller (0.9-1.7 dB, minimum +1.3-2.0 %), and the
finer openEMS mesh moves both designs towards yapnr.rf. The leading suspect is the
thickness-equivalent substrate, fitted on an isolated line, which cannot represent the sidewall
coupling across D1's 14 floating islands (copper thickness / gap ≈ 0.22); this is not
established. Both are predictions made before fabrication; neither is preferred here. The
criteria compare the measurement with the yapnr.rf prediction ([pre-registration](../../preregistration.md));
the openEMS numbers are reported next to it as a registered alternative.
