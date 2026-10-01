# RF inverse design

`yapnr.rf` designs the copper of a single-layer microstrip footprint so that it meets a list of
transfer-function targets: |S_ij| limits and masks over frequency bands, phases, and a radiated
fraction for antennas. It follows the density-based topology optimization of Hammond et al.
(Meep's adjoint method): an FDTD solver with exact frequency-domain adjoint gradients, a filtered
and projected density on the copper plane, and an epigraph minimax solved by MMA. The result is
a KiCad footprint, a Touchstone file and a JSON report. The method and its choices are in the
[design](design/rf-topology-optimization.md) (issue #29).

Status: the solver, the optimizer and the end-to-end cases are implemented. Of the three cases,
the power divider meets its targets on the optimization grid and on a finer grid; the antenna
and the diplexer do not (the optimizer's local search stalls on resonant structures); the
results and the reasons are [below](#end-to-end-cases).

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
- **Optional sections:** `fixed` (rectangles of fixed copper or keepout), `lumped` (resistors
  across a gap, such as a Wilkinson divider's isolation resistor: the body rectangle stays void
  and carries the resistance between two copper pads), `radiation` (the box's offset and
  height), `solver` (backend, precision, tolerances) and `optimizer`: the β schedule, iteration
  caps, budget, move limits, `conservative` (CCSA steps that never raise t), `seed` (`star`,
  `stubs`, `patch`, `patch_edge`, or a uniform `init`), `binary_every` (how often the
  binarized design is evaluated, below), `eta_variants` (robust optimization over eroded and
  dilated designs, Hammond et al. §5.3) and `interpolation` (`resistive` or `reactive` gray
  copper, [below](#how-it-works)).

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

The exported design is the best binarized one of the run, not the last iterate: the loop
evaluates the binarized design (β = ∞ after the width and space repair; one forward run per
excitation) at the start, at every β change and every `binary_every` iterations (5), `finish`
evaluates the last one, and the lowest epigraph value wins; `result.json` records the iteration
it came from (`optimizer.export_iteration`). Plain MMA can raise t from one iteration to the next
and the value at finite β is not the binary design's, so the last iterate is not always the
best.

The density evolution renders as an animation (Pillow, like the place-and-route animations):

```sh
bazel run //yapnr/rf:animate -- runs/divider --out divider.webp
```

## End-to-end cases

Three cases exercise the whole method on the common tasks: a power divider (combiner), an
antenna and a filter bank (a diplexer). Each optimizes its spec from its starting point
([below](#starting-points-and-repairs)), binarizes, repairs the minimum width and space on the
pixel grid, exports the footprint, and is then re-validated from the exported `.kicad_mod` (not
from the optimizer's arrays) twice:

- **on the optimization grid ("coarse"):** the footprint must reproduce the exported design
  pixel for pixel and stay within 0.5 dB (transmissions) and 0.05 in |S| of the optimizer's
  binary design;
- **on a finer grid ("fine"):** half the pitch in-plane and 1.5 times the substrate cells, the
  feeds recalibrated from the pad widths, every port excited, the S-matrix renormalized to
  50 Ω.

The criteria are judged on dense in-band sweeps and are slightly looser on the fine grid than on
the coarse one (design §11). The presets, criteria and the runner are in `yapnr.rf.cases`; the
validator is `yapnr.rf.validate`.

```sh
# optimize, export and re-validate (4 threads; start it niced on a shared machine)
python -m yapnr.rf.cases run divider --out runs/divider
python -m yapnr.rf.cases validate divider --out runs/divider   # re-validate only
python -m yapnr.rf.animate runs/divider --out divider.webp --figure divider.png

# the same as Bazel tests: the full cases are manual, the smoke variants run in CI
nice -n 10 bazel test --config=lowmem --local_test_jobs=1 //tests/e2e/rf:test_divider
bazel test //tests/e2e/rf:test_divider_smoke
```

`YAPNR_RF_RUN_DIR` (passed with `--test_env`) keeps a full test's run directory; a finished run
there is only exported and re-validated again. The smoke variants run the same topologies on
tiny grids for four iterations and check the pipeline, not the RF targets.

All cases use a substrate of εr 3.55 and tan δ 0.0027 (a Rogers 4003C-like laminate) over a
solid ground, copper as a sheet with the surface resistance of smooth copper at 10 GHz, torch
float32 on 4 threads, and the β schedule 8 → 128 (8 → 64 for the antenna). Times are wall times
on the development Mac (Apple silicon), niced, next to other work.

| Case                        | Ports, substrate   | Optimization grid (cells, Δt, cells per λ_d at f_max) | Fine grid      | Window (pixels)  | Start       | Iterations, wall time | Coarse | Fine |
| --------------------------- | ------------------ | ----------------------------------------------------- | -------------- | ---------------- | ----------- | --------------------- | ------ | ---- |
| [divider](#a-power-divider) | 3, S1 (h 0.813 mm) | 92 × 74 × 22 = 150k, 0.465 ps, 46                     | 494k, 0.265 ps | 32 × 40 @ 0.3 mm | uniform 0.3 | 121, 13 min           | pass   | pass |
| [antenna](#b-antenna)       | 1, S2 (h 1.524 mm) | 99 × 81 × 27 = 217k, 0.599 ps, 39                     | 757k, 0.344 ps | 45 × 45 @ 0.4 mm | tuned patch | 16, 11 min            | fail   | fail |
| [diplexer](#c-diplexer)     | 3, S1              | 110 × 84 × 22 = 203k, 0.465 ps, 42                    | 731k, 0.265 ps | 50 × 50 @ 0.3 mm | junction    | 102, 17 min           | fail   | fail |

S1 and S2 are the same material (εr 3.55); the fine grids have half the pitch in-plane and 6 or
9 substrate cells. One iteration is one forward and one adjoint FDTD run (the antenna's
conservative steps add up to four forward runs); a run's re-validation takes 1–2 minutes.

The artifacts of each case are in [`docs/rf/`](rf/): the footprints form one KiCad library,
[`yapnr-rf-examples.pretty`](rf/yapnr-rf-examples.pretty/); each case directory holds
`spec.json`, `result.json` (`yapnr-rf-result/1`), `validation.json` (both re-simulations, with
the criteria and the |S| tables), `coarse.sNp` (the exported design on the optimization grid,
101 points over the source band) and `fine.sNp` (the fine re-simulation at the validation
frequencies), the animation of the optimization and the figure.

### (a) Power divider

<img src="rf/divider/divider.png" alt="The divider's copper and its |S_i1| on both grids" width="800">

Three ports on S1 (h 0.813 mm), a 9.6 × 12 mm window (32 × 40 pixels at 0.3 mm, mirror
symmetric about y = 0), 50 Ω feeds of 6 cells (Z_c 49.3 Ω calibrated), port 1 on the west edge
and ports 2 and 3 on the east edge 8.4 mm apart. Targets over 8.5–11.5 GHz (7 points):
|S11| ≤ −20 dB and |S21|, |S31| ≥ −3.28 dB. The optimizer met them from iteration 54 (β = 16)
and ended at t = −0.002 after 121 iterations (6.5 s each, 13 min). The β = 32 epoch ran to its
30-iteration cap with t flat while a few boundary pixels kept changing (the epoch's convergence
test also needs max |Δx| < 0.01).

| Check (dense sweep, 61 points) | Spec target | Coarse criterion | Coarse   | Fine criterion | Fine     |
| ------------------------------ | ----------- | ---------------- | -------- | -------------- | -------- |
| \|S11\| max, 8.5–11.5 GHz      | −20 dB      | ≤ −17 dB         | −19.3 dB | ≤ −15 dB       | −18.0 dB |
| \|S21\| = \|S31\| min          | −3.28 dB    | ≥ −3.45 dB       | −3.22 dB | ≥ −3.6 dB      | −3.27 dB |
| \|\|S21\| − \|S31\|\| max      | —           | —                | 0        | ≤ 0.25 dB      | 0        |
| passivity, min eig(I − SᴴS)    | —           | ≥ −0.01          | −0.005   | ≥ −0.01        | −0.006   |

The design is a junction with a small ring at the input, four short open stubs on the input
line and two 45° arms to the outputs: one copper island joining all three pads (a net tie),
manufacturable at 0.6 mm width and space after the repair (10 pixels changed; the transmission
moved by 0.02 dB, |S11| by 0.04). The output ports are not isolated or matched (|S22| ≈ −6 dB,
|S32| ≈ −6 to −8 dB), as for any lossless reciprocal three-port; the isolated (Wilkinson)
variant needs a lumped resistor, which specs cannot place yet. On the fine grid (0.15 mm,
6 substrate cells, 494k cells, Z_c 50.7 Ω) the deepest match moves from 9.55 to 10.0 GHz.
The imbalance is zero by construction (the design is mirror symmetric).

<img src="rf/divider/divider.webp" alt="The divider's optimization" width="800">

### (b) Antenna

<img src="rf/antenna/antenna.png" alt="The antenna's copper, |S11| and η on both grids" width="800">

**This case does not meet its targets.** One port on S2 (h 1.524 mm), an 18 × 18 mm window
(45 × 45 pixels at 0.4 mm, mirror symmetric), a 9-cell feed (3.6 mm, Z_c 52.3 Ω; 10 cells cannot
sit symmetrically on the window's centre line), and a radiated-power box 2.4 mm beyond the
window and 8 mm high with windows where the feed crosses it. Targets over 9.8–10.2 GHz
(4 points): |S11| ≤ −12 dB and a radiated fraction η ≥ 0.70 (the design's band was
9.7–10.3 GHz; see [starting points](#starting-points-and-repairs)). The tuned patch seed starts
at t = 0.35 (|S11| −8.6 dB and η 0.69 at 9.8 GHz); the first conservative step raised t to 1.07
and the run settled at t = 0.535 after 16 iterations (41 s each with the inner forward runs,
11 min): the slots got shallower and the resonance moved up, out of the band's low edge.

| Check (dense sweep)            | Spec target | Coarse criterion         | Coarse         | Fine criterion             | Fine           |
| ------------------------------ | ----------- | ------------------------ | -------------- | -------------------------- | -------------- |
| \|S11\| max                    | −12 dB      | ≤ −10 dB (9.8–10.2)      | −7.0 dB (fail) | ≤ −10 dB (9.85–10.15)      | −5.8 dB (fail) |
| η min at the check frequencies | 0.70        | ≥ 0.65 (9.8, 10.0, 10.2) | 0.650          | ≥ 0.60 (9.85, 10.0, 10.15) | 0.588 (fail)   |
| passivity                      | —           | ≥ −0.01                  | 0.74           | ≥ −0.01                    | 0.66           |

The copper is a plausible inset-fed patch (one island with the port's pad, no width or space
violation). On the coarse grid its match is best at 10.14 GHz (−10.6 dB) and η runs 0.65–0.77
over the band; the fine grid moves the match to about 10.3 GHz and η to 0.56–0.74, the same
1 % shift as the plain patch. The optimizer did not improve on its seed, in any of the variants
tried: plain MMA left the tuned seed on its first step and wandered at t 0.7–2.8, the
conservative variant with a 0.1 move rose to 0.5–0.8, and with a 0.05 move it settled at 0.535.
On this grid a pixel moves a resonance by about 5 %, and every boundary step through gray
copper first adds loss, which the first-order model does not see; the 4 % band also leaves no
margin for the coarse-to-fine shift. A finer optimization grid (0.2 mm, about eight times the
cost) and a robust formulation over eroded and dilated designs (design §14) are the next steps.

<img src="rf/antenna/antenna.webp" alt="The antenna's optimization" width="800">

### (c) Diplexer

<img src="rf/diplexer/diplexer.png" alt="The diplexer's copper and its |S_i1| on both grids" width="800">

**This case does not meet its targets.** Three ports on S1, a 15 × 15 mm window (50 × 50 pixels,
no symmetry), the common port 1 on the west edge, channel A (7.6–8.4 GHz) to port 2 and channel
B (11.6–12.4 GHz) to port 3 on the east edge 9 mm apart. Targets at 7.4–8.6 and 11.4–12.6 GHz
(4 points each, the channels widened by 0.2 GHz): in-channel |S| ≥ −1 dB, the other port
≤ −22 dB, |S11| ≤ −12 dB. From the junction seed the optimizer split the channels within ten
iterations (each branch passing its own band at about −2.5 dB) and then stalled: t = 1.83 after
102 iterations (9.8 s each, 17 min); a 60-iteration first epoch stayed at t = 1.8 ± 0.1 from
iteration 20 to 59.

| Check (0.05 GHz steps)     | Spec target | Coarse criterion | Coarse          | Fine criterion | Fine            |
| -------------------------- | ----------- | ---------------- | --------------- | -------------- | --------------- |
| A: \|S21\| min (7.6–8.4)   | −1 dB       | ≥ −1.5 dB        | −2.7 dB (fail)  | ≥ −2 dB        | −2.7 dB (fail)  |
| B: \|S31\| min (11.6–12.4) | −1 dB       | ≥ −1.5 dB        | −2.6 dB (fail)  | ≥ −2 dB        | −2.5 dB (fail)  |
| A: \|S31\| max (rejection) | −22 dB      | ≤ −18 dB         | −9.4 dB (fail)  | ≤ −15 dB       | −9.0 dB (fail)  |
| B: \|S21\| max (rejection) | −22 dB      | ≤ −18 dB         | −11.8 dB (fail) | ≤ −15 dB       | −10.0 dB (fail) |
| \|S11\| max, A / B         | −12 dB      | ≤ −10 dB         | −4.3 / −4.8 dB  | ≤ −8 dB        | −4.4 / −5.3 dB  |
| passivity                  | —           | ≥ −0.01          | 0.021           | ≥ −0.01        | 0.021           |

The result is a junction whose branches act as a low-pass (to port 2) and a high-pass split,
rolling off monotonically rather than notching the other channel: rejection is 9–12 dB and
the common port is mismatched (|S11| −4 to −8 dB), most of the in-channel loss. A diplexer that
meets the targets needs resonant stubs about a quarter wave long (3.8 mm at 12 GHz, 5.7 mm at
8 GHz) placed a quarter wave from the junction, and a stub only works once it is long enough:
the gradient from the junction does not lead there. The export and the re-validation work as
for the other cases (one island, no width or space violation, the fine grid agreeing with the
coarse one within 2 dB), so the case documents a limit of the optimizer's local search, not of the
pipeline. The three-channel bank (`filterbank3`, stretch) was not run for this reason.

<img src="rf/diplexer/diplexer.webp" alt="The diplexer's optimization" width="800">

### Starting points and repairs

The paper starts from a uniform ρ = 0.5. With copper that is a 377 Ω/sq absorber over the whole
window, and adding or removing conductance anywhere first changes how much it absorbs. The cases
therefore start differently (`optimizer.init`, `optimizer.seed`), each choice computed from the
spec alone:

| Case     | Start                                                                         | Uniform starts tried                                                                                                                                  |
| -------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| divider  | uniform x = 0.3 (ρ̄ ≈ 0.04 at β = 8: an almost transparent 3.3 kΩ/sq sheet)    | 0.5 grew into one radiating copper plate with holes: at β = 32 \|S21\| fell to −5.5 dB at 11.5 GHz and t rose from 1.1 to 2.5                         |
| diplexer | `seed: star`, every port's feed continued to the window's centre (a junction) | 0.3: the transmissions stayed below −30 dB for 15 iterations (the window absorbed); at iteration 30 a radiating copper mass, t = 8.5                  |
| antenna  | `seed: patch`, the closed-form inset-fed patch, tuned by whole pixels (below) | 0.3 and 0.5 fell back to the bare open-ended feed (η ≈ 0.15, \|S11\| ≈ −0.8 dB); 0.7 (a near-copper plate, also from β = 32) stayed a plate (η ≈ 0.4) |

For a radiated-power target the uniform starts are local optima: gray copper absorbs before it
radiates. The seeds are the textbook starting points (a junction, Balanis' patch); the
optimizer then reshapes them freely, with every pixel of the window a design variable.

The patch seed is W, L and the inset depth from the transmission-line model at the band
centre, then the best of its 27 whole-pixel neighbours (length ±1, width ±2, inset ±1) by the
spec's epigraph value on the grid, 27 forward runs (`yapnr.rf.seeds.tuned_patch`): one pixel of
length moves the resonance by about 5 %, so the closed form lands between pixels. Its copper
and void start at x = 0.9 and 0.1 with slots 1.5 times the minimum space, and its β schedule
starts at 16: at x 0.7/0.3, two-pixel slots and β = 8 the slots blurred into a lossy 32 Ω/sq and
the first step closed them, and at β = 32 every pixel saturated and nothing moved.

On the antenna's grid, the closed-form patch alone (W 10.0 mm, L 7.2 mm, inset 2.4 mm with
0.8 mm gaps, 4 mm from the port) reaches |S11| = −21 dB at 10.15 GHz and a radiated fraction
of 0.80, with a −10 dB band of 9.90–10.35 GHz; on the fine grid the same pixels resonate 1 %
higher (10.25 GHz). That sets the scale of what a single-layer patch on this substrate can do:
refining it for the design's 9.7–10.3 GHz (6 %) froze at t = 1.40 (η 0.56 and |S11| −5.3 dB at
9.7 GHz; MMA alternated between two designs, then stopped moving), so the antenna case asks for
9.8–10.2 GHz (4 %) at the same levels (|S11| ≤ −12 dB, η ≥ 0.7).

**Minimum width and space:** with the rules' two-pixel filter radius the Zhou constraints read as
met while the binary divider kept two one-pixel holes and two diagonal one-pixel nubs. The
export opens the copper and the void with a square of the minimum width (space) and bridges
corner contacts on the pixel grid (`yapnr.rf.export.repair`); for the divider that changed 10 of
1280 pixels and |S11| by at most 0.04 in magnitude. A four-pixel filter instead (with the
constraint thresholds derived from the filter radius) gave smoother shapes but stalled at
t ≈ 0.7 at iteration 70, where the two-pixel run was at −0.002.

## How it works

1. **Parameterization:** the design variables are the free pixels of the design window (half or
   a quarter of them with mirror symmetry); port pads and `fixed` regions are fixed; a ring
   around the window holds the feeds. A conic filter of radius R (from the minimum width and
   space) and a tanh projection with β growing 8 → 128 give the density ρ̄.
2. **Physics:** each pixel's sheet conductance is G_min (G_max/G_min)^ρ̄, from transparent to
   copper, averaged onto the solver's grid edges. With `interpolation: reactive` a gray pixel
   is instead an inductive sheet R_s + ω_d L − iωL(ρ̄) (the RF analog of the paper's
   interpolation of dispersive metals, Eq. 7–12: a branch current per pixel and edge, advanced
   with the E field by the trapezoidal rule; the damping ω_d keeps the gray sheets' current
   loops from ringing for 10⁴ periods), lossless where the resistive sheet is a 377 Ω/sq
   absorber; copper and void are the same. It is tested (the discrete frequency-domain identity
   and the pipeline gradient) but not used by the cases: on the antenna from a uniform start it
   fell back to an unfed plate, and on the diplexer its gray lines broke the junction.
3. **Objectives:** each requirement becomes a normalized violation φ ≤ 0 per frequency; the
   requirements of one excitation combine per frequency in a smooth maximum f, and the optimizer
   minimizes t subject to f ≤ t for every (excitation, frequency). One forward and one adjoint
   FDTD run per excitation give every f and its gradient.
4. **Optimizer:** MMA in its native min-max form (written from Svanberg's publications); in the
   last β epoch the minimum width and space enter as Zhou's indicator constraints. The
   conservative variant accepts a step only when its approximation was conservative at the new
   point, so t never rises; a step that does not get there within `max_inner` subproblems is
   rejected (x stays) and the next iteration continues from the raised curvature. Robust
   variants (`eta_variants`) add the objectives of eroded and dilated designs (projection
   thresholds above and below ½) to the epigraph.
5. **Export:** the design is binarized (β = ∞), traced into polygons by marching squares (pixel
   edges, half-pixel chamfers, holes joined by zero-width keyhole cuts), checked for minimum
   width and space on a raster at Δ/8, and written as a net-tie footprint: one SMD pad per port,
   the copper as `fp_poly` on F.Cu with `net_tie_pad_groups`, two pads per lumped part, and rule
   areas (keepouts) for the environment the design was simulated in: no copper pour, vias or
   other footprints within the simulated margin around the window, and no tracks there except in
   a corridor along each feed (checked with KiCad 10: a pour stops at the keepout, a track of
   another net across it is flagged, the feed track and the footprint's own copper are not). The
   footprint's description names the stackup it assumes (εr, tan δ, h, a solid ground on the
   next layer).

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

## Accuracy

What the reported numbers mean, measured on the S1 line (6 cells at 0.3 mm) and on the
divider's footprint:

- **Line loss.** The Poynting flux along a long matched line decays by 0.7–0.9 Np/m over
  2–12 GHz, close to the textbook 0.42 Np/m (dielectric) plus R_s/(Z0 w) = 0.29 Np/m
  (conductor): about 0.03 dB per centimetre of line. An earlier version of this guide said the
  sheet's loss rose with frequency to several times a thick strip's; that came from the
  calibration's Im k, which is not a loss measurement (next item).
- **De-embedding uses Re k only.** The two-plane calibration resolves β to about 1 % but not α:
  its Im k ranged from +4 to −1 Np/m with the planes' distance from the source. De-embedding
  the magnitude with it inflated every |S| by up to 0.15 dB and produced non-passive S-matrices
  (the earlier −0.01 passivity tolerance). The feed between the measurement and the reference
  plane (6h, about 5 mm on S1) is now de-embedded in phase only; its true loss (about 0.01 dB)
  makes the reported |S| that much low.
- **The excited port's incident wave reads high.** Within about 50 cells of the source the V/I
  samples see non-modal fields that the port's source launches, and the incident wave of the
  excited port reads 1.5–2 % high at 8–12 GHz (0 at 2–4 GHz): every |S_ij| of that column is
  about 0.1–0.2 dB low (a matched 15 mm line reads −0.26 dB at 10 GHz where the flux says
  −0.11 dB). The bias is conservative for transmission targets. The radiated fraction is not
  affected: the calibration measures the ratio of Poynting flux to V/I power at the ports' own
  distance from the source and P_inc uses it.
- **Reciprocity.** The discrete system is exactly reciprocal; the port-wave extraction is not.
  With the V/I plane 6h from the discontinuity and from the source (the default) and S = B A⁻¹
  from all excitations, |S21 − S12| on the divider is 0.003–0.007 (it was 0.009–0.013 at 3h)
  and the passivity margin min eig(I − SᴴS) is +0.03 to +0.05; the criteria require −1e-3.
  Taken together, transmissions are uncertain by about ±0.1 dB plus the low bias above.
- **Copper edges and resolution.** A zero-thickness sheet's edge field is under-resolved: on
  a 6-cell line the impedance is 6 % below Hammerstad–Jensen, and an open end's extension at
  10 GHz is 0.49 mm at 0.3 mm pitch and 0.44 mm at 0.15 mm against 0.35 mm (Kirschning–Jansen–
  Koster): the copper acts about 0.45 cell larger per edge, falling roughly as √Δ. A design
  exported pixel for pixel therefore resonates higher on finer grids and in hardware than on the
  optimization grid (the antenna about 1 % from 0.4 to 0.2 mm, about 3 % to the continuum by the
  √Δ extrapolation), and diagonal staircases add a resolution effect of their own. The
  validator re-simulates every case on the optimization grid, at half the pitch and at a third
  of it, judges the fine criteria on both finer grids and reports the trend of every check
  (`validation.json`, `convergence`): the finer grids are a check, not a converged answer, for
  resonant designs.

## Limitations

- One copper layer over a solid ground; no vias, no finite board or ground edges.
- The copper is a zero-thickness sheet: the strip impedance on coarse grids is a few per cent
  low and edges act about half a cell larger than their pixels ([accuracy](#accuracy)); the
  finer re-validation grids show the trend but are not converged for resonant designs.
- Minimum width and space are enforced in the last β epoch, repaired on the pixel grid at
  export and checked on the polygons; tiny nubs under about 0.6 pixel are not reported by the
  check.
- Gray copper is lossy (the log-interpolated sheet passes through 377 Ω/sq), so uniform starts
  can be local optima and boundary moves through gray first add loss. Resonant designs (the
  antenna, the diplexer's stubs) stalled in the local search; at the cases' pitch one pixel
  moves a resonance by about 5 %, and the fine grid shifts it by about 1 %.
- Lumped elements are resistors across a gap (`lumped`); no capacitors, inductors or vias. No
  external solver cross-check.
- The footprint's rule areas keep other copper out of the simulated margin (no pour, vias or
  other footprints within the margin; no tracks there except along the feeds); a solid ground
  on the next layer is assumed and named in the footprint, not enforced.
