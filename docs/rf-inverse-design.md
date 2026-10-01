# RF inverse design

`yapnr.rf` designs the copper of a single-layer microstrip footprint so that it meets a list of
transfer-function targets: |S_ij| limits and masks over frequency bands, phases, and a radiated
fraction for antennas. It follows the density-based topology optimization of Hammond et al.
(Meep's adjoint method): an FDTD solver with exact frequency-domain adjoint gradients, a filtered
and projected density on the copper plane, and an epigraph minimax solved by MMA. The result is
a KiCad footprint, a Touchstone file and a JSON report. The method and its choices are in the
[design](design/rf-topology-optimization.md) (issue #29).

Status: the solver and the optimizer are implemented and unit-tested. The end-to-end cases
(power divider, patch antenna, diplexer) and their re-validation on a finer grid are the next
step; this page gets their measured results then.

## A spec

A spec is YAML or JSON (`yapnr-rf-spec/1`, lengths in mm, frequencies in GHz):

```yaml
schema: yapnr-rf-spec/1
name: divider-x10
stackup: { er: 3.55, tan_delta: 0.0027, h_mm: 0.813, f_ref_ghz: 10 }
grid: { pitch_mm: 0.3, substrate_cells: 4 }
design_region: { x_mm: [0, 9.6], y_mm: [-6.0, 6.0] }
symmetry: mirror_y
rules: { min_width_mm: 0.6, min_space_mm: 0.6 }
ports:
  - { n: 1, side: W, at_mm: 0.0, width_cells: auto }
  - { n: 2, side: E, at_mm: 4.2, width_cells: auto }
  - { n: 3, side: E, at_mm: -4.2, width_cells: auto }
bands:
  pass: { ghz: [8.5, 11.5], points: 7 }
requirements:
  - { s: [1, 1], max_db: -20, band: pass }
  - { s: [2, 1], min_db: -3.28, band: pass }
  - { s: [3, 1], min_db: -3.28, band: pass }
optimizer: { betas: [8, 16, 32, 64, 128], iterations_per_beta: 30, budget_min: 45 }
```

- **Ports** are line ports: a feed strip enters the design region on side `W`, `E`, `S` or `N`
  at `at_mm` (the transverse position of its centre) and runs into the absorbing boundary.
  `width_cells: auto` picks the whole number of cells whose calibrated impedance is closest to
  50 Ω.
- **Requirements:** `max_db`, `min_db`, `between_db: [lo, hi]`, piecewise-linear masks
  `mask_db: [[f, dB], ...]` (upper) and `min_mask_db` (lower), `phase_deg` with `tol_deg`
  (engineering e^{+jωt} convention), and `{radiated: j, min: η}` for the fraction of the power
  incident at port j that leaves through a box above the board. Every requirement names a band
  and may set its `scale` (the normalization of its violation).
- **Optional sections:** `fixed` (rectangles of fixed copper or keepout), `radiation` (the box's
  offset and height), `solver` (backend, precision, tolerances) and `optimizer` (β schedule,
  iteration caps, budget, move limits, `conservative`, `aggregate`).

The Python API mirrors the file:

```python
from yapnr.rf.spec import S, RadiatedFraction, Spec

spec = Spec.load("divider.yaml")
extra = S(2, 1).phase_deg(-90, tol=10, band="pass")
antenna_target = RadiatedFraction(1).at_least(0.7, band="main")
```

## Running a design

```python
from yapnr.rf.driver import design
from yapnr.rf.spec import Spec

result = design(Spec.load("divider.yaml"), "runs/divider")
```

The run directory holds `spec.json`, `checkpoint.npz`, `history.json` (t, every objective, the
S-parameters, the radiated fraction and the gray level per iteration), `frames.npz` (ρ̄ per
iteration), `footprint.kicad_mod`, `coarse.sNp` (the binary design on the optimization grid,
every port excited, renormalized to 50 Ω) and `result.json` (`yapnr-rf-result/1`: solver
settings, optimizer summary, achieved values, the width and space check and provenance). A run
that stops resumes from its checkpoint and reproduces the uninterrupted run bit for bit. Runs use
at most 4 threads; on a shared machine start them with `nice -n 10`.

The density evolution renders as an animation (Pillow, like the place-and-route animations):

```sh
bazel run //yapnr/rf:animate -- runs/divider --out divider.webp
```

## How it works

1. **Parameterization:** the design variables are the free pixels of the design window (half or
   a quarter of them with mirror symmetry); port pads and `fixed` regions are fixed; a ring
   around the window holds the feeds. A conic filter of radius R (from the minimum width and
   space) and a tanh projection with β growing 8 → 128 give the density ρ̄.
2. **Physics:** each pixel's sheet conductance is G_min (G_max/G_min)^ρ̄, from transparent to
   copper, averaged onto the solver's grid edges.
3. **Objectives:** each requirement becomes a normalized violation φ ≤ 0 per frequency; the
   requirements of one excitation combine per frequency in a smooth maximum f, and the optimizer
   minimizes t subject to f ≤ t for every (excitation, frequency). One forward and one adjoint
   FDTD run per excitation give every f and its gradient.
4. **Optimizer:** MMA in its native min-max form (written from Svanberg's publications); in the
   last β epoch the minimum width and space enter as Zhou's indicator constraints.
5. **Export:** the design is binarized (β = ∞), traced into polygons by marching squares (pixel
   edges, half-pixel chamfers, holes joined by zero-width keyhole cuts), checked for minimum
   width and space on a raster at Δ/8, and written as a net-tie footprint: one SMD pad per port,
   the copper as `fp_poly` on F.Cu with `net_tie_pad_groups`. The footprint's description names
   the stackup it assumes (εr, tan δ, h, a solid ground on the next layer).

## Measured

Solver (unit tests; design Δ = 0.3 mm on εr 3.55, h 0.813 mm):

| Check                                         | Measured                                       |
| --------------------------------------------- | ---------------------------------------------- |
| adjoint gradient against finite differences   | 2.4e-9 to 1.5e-8 relative                      |
| CPML reflection, 10 cells                     | −66.6 dB (substrate), −79.6 dB (air)           |
| Z_c against Hammerstad–Jensen, 6 cells/width  | −6.4 % (2 GHz) to −5.8 % (4 GHz); −3.3 % at 12 |
| ε_eff against Kirschning–Jansen, 2–12 GHz     | within 0.5 %                                   |
| matched line through a 9.6 mm region          | \|S11\| ≤ −41.8 dB, \|S21\| ≥ −0.13 dB         |
| throughput, torch float32, 4 threads          | 263 M cell-updates/s (157k cells)              |
| divider iteration (forward + adjoint, 7 freq) | 6.7 s                                          |

Optimizer (unit tests):

| Check                                                                          | Measured                                 |
| ------------------------------------------------------------------------------ | ---------------------------------------- |
| whole-pipeline gradient (x → ρ̄ → FDTD → objectives) against finite differences | 1e-10 relative                           |
| MMA on Svanberg's toy problem                                                  | KKT residual < 1e-6, x within 1e-5       |
| tiny two-port (6 × 6 pixels): t from the uniform start                         | 0.31 → −0.28 (the matched straight line) |
| contour → polygons → pixels, 200 random masks                                  | exact                                    |
| KiCad 10 (`kicad-cli fp upgrade`, `fp export svg`)                             | loads and plots, every polygon kept      |

On the tiny problem the conservative MMA variant (`optimizer.conservative: true`, NLopt's
CCSA as used by Hammond et al.) never lets t rise, but it needed 96 forward runs against 18 for
plain MMA and ended at t = −0.10 against −0.28, so plain MMA is the default.

## Limitations

- One copper layer over a solid ground; no vias, no finite board or ground edges.
- The copper is a zero-thickness sheet: its loss rises with frequency (edge crowding) to several
  times a thick strip's, and the strip impedance on coarse grids is a few per cent low; the
  coarse-to-fine re-validation is the judge.
- Minimum width and space are enforced in the last β epoch and checked on export; tiny nubs
  under about 0.6 pixel are not reported by the check.
- Clearance to copper outside the footprint is KiCad's job.
