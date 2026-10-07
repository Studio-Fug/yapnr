# Order 0: the openEMS models (D-O0-13a)

An independent 3D prediction next to the optimizer's own FDTD, made before fabrication. What
differs from yapnr.rf, on purpose:

|            | yapnr.rf (the predictions the criteria use)                                      | openEMS (these models)                                                                                                                                                        |
| ---------- | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Copper     | one zero-thickness sheet                                                         | 43.18 µm thick (L1), PEC                                                                                                                                                      |
| Substrate  | the thickness-equivalent one (M-eq: εr′ 3.4417, h′ 0.1795 mm)                    | nominal FR408HR as the coupon model has it at 5 GHz (prepreg εr 3.5767, 0.1999 mm; core 3.8343)                                                                               |
| Loss       | the substrate's (tan δ′) and the sheet's R_s, then the coupon model's correction | the substrate's (tan δ 0.00906, a conductivity fixed at 5 GHz, as yapnr.rf), copper lossless, then the same correction against openEMS's own line                             |
| Code, mesh | yapnr.rf's native kernel, its grid (0.10/0.05/0.033 mm in the plane)             | openEMS 0.37, its own mesh: 0.05 and 0.025 mm in the plane, 6 and 8 cells across the prepreg, 2 across the copper                                                             |
| Ports      | modal line ports into the CPML                                                   | 50 Ω-terminated feeds with voltage and current probes (the current loop around the whole thick strip); Zc and β from the telegrapher's equations (openEMS's MSLPort formulas) |

Neither models the L1 ground pour 1.0 mm from the copper, the solder mask (open on the boards)
or the launches; the sticks A01 and A04 are the exception: they model the launches.

## Files

- `models.py` writes `models/*.json` from yapnr's own data: D1 from the shipped footprint
  (`predictions/D1/runs/d1-star/footprint.kicad_mod`, label `O0 D1 divider-osh-m ad20e643`), R1
  as drawn, the 0.40 mm line at 10 and 30 mm, and A01/A04 from `yapnr.rf.coupons.launch` and the
  board file's vias.
- `order0_em.py` (with `order0_launch.py`) builds and runs one model with one port excited and
  writes `result.json` (the waves at the reference planes); `post.py` assembles the S-matrices
  (S = B A⁻¹, the missing excitations from the models' symmetry) and renormalizes them to 50 Ω;
  `compare.py` compares them with yapnr.rf and the coupon model.
- `jobs-r05.toml`, `jobs-r025.toml`, `jobs-launch.toml`: the cloud jobs (one model per
  c4d-highcpu-8 at 8 threads, openEMS's `exact-endcriteria`; `tools/exp/openems_plan.py`).

## The launch sticks

A01 (the thru) and A04 (ΔL 30 mm) with a Cinch 142-0701-851 on each end, as the tier-1 (coax)
calibration sees them: the flange against the milled edge with its PTFE coax bore (Ø4.20 mm,
centre conductor Ø1.27 mm), the 0.51 × 0.25 mm tab on the pin pad, the top and bottom legs on
their pads, the 4-layer stack with the In1.Cu cut-out under the pad and taper, the L1 pour's
channel and keep-away, and all of the stick's vias (72 on A01, 107 on A04). The ports are coax
TEM modes 3.35 mm behind the flange; the waves are referred to the flange's back face, so the
connector's own coax from its mating plane (which the tier-1 SOLT includes) adds phase and a
little loss, not reflection. The connector's inside (how the pin becomes the tab) is not on
Cinch's drawing and is modelled as the pin ending 0.3 mm inside the bore. The excitation is
openEMS's Gaussian less its DC content: the coax ports give the centre conductor no DC path,
and with the plain Gaussian a static field held the energy above the end criterion.

## Tried and not used

openEMS's conducting sheets on every face of the thick copper (and on the ground) added only
about 0.02 dB/cm to a 0.40 mm line at 5 GHz in a local test, about half the smooth-copper
conductor loss a 2D model gives (0.037-0.059 dB/cm); the predictions therefore keep the copper
lossless and add the conductor loss from the coupon model, as for yapnr.rf.
