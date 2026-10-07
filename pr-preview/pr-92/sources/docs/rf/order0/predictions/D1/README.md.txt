# D1: the optimizer's thin divider

> **Superseded 2026-10-05.** This export (label `ad20e643`) has two 0.100 mm corner gaps against
> OSH Park's 0.127 mm rule under PR #53's fixed DRC and does not ship. O0-D carries the
> re-optimized `d1-star`, label `7e070ca8`: see [D1-d1c](../D1-d1c/README.md). This page is kept
> otherwise unchanged as the record of part 2's run.

**Compute stage, 2026-10-04 (drafts until the pre-registration release).** The shipped run is
`d1-star` (the junction start): it passes every validation check on the coarse, fine and finer
grids of M-eq, worst-case fine/finer margin 0.299 dB (\|S11\| ≤ −20.70 dB, \|S21\| = \|S31\| ≥
−3.251 dB raw), `result.json` sha256 `ad20e643…` (label `O0 D1 divider-osh-m ad20e643`). Every
formulation's run is in [runs/](runs/) (spec, `result.json`, `validation.json`, footprint, the
re-validation Touchstone files, the optimizer history), the selection in
[selection.json](selection.json) (rule: [demos](../../demos.md#formulations)), and the shipped
footprint re-validated on M-eq with the 3-7 GHz pulse, M-nom and M-eq-em528 in
[variants/](variants/). Raw solver output: the loss-corrected prediction is stage 4's.

Region M (L1 over In1.Cu), window 12 × 15 mm, ports W / N / S, 4.25-5.75 GHz (Order 0 design §4.4).
Its specs (one per formulation), criteria and substrate are in [inputs/](../../inputs/) and
explained in [demos](../../demos.md). This directory receives its spec, `result.json`,
`validation.json`, footprint and predicted Touchstone files (thickness-equivalent and nominal
substrate, FR408HR and EM528), and its copper goes into the D1 window of upload O0-D, with the
first 8 hex digits of the sha256 of `result.json` in silkscreen.

What the board around the window is, for the spec (`divider-osh-m`):

- the window's west edge is P1's port plane; P2 and P3 sit on its north and south edges 6.0 mm
  from the west edge; each port is a 0.40 mm feed (4 cells of 0.10 mm, on grid nodes) running
  3.0 mm straight to its launch's reference plane;
- no L1 copper within 1.0 mm (5 h) of the window or the feeds, and the solder mask open over all
  of it; In1.Cu (the reference) solid under the window; the stick's east edge (where its tabs
  are) 4.0 mm (20 h) beyond the window;
- the window carries a placeholder footprint (`PHD1`): `yapnr fab check` stops on it
  (`FAB-PLACEHOLDER`) until the copper replaces it.
