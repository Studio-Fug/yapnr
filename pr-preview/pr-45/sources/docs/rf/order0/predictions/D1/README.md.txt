# D1: the optimizer's thin divider (placeholder)

Region M (L1 over In1.Cu), window 12 × 15 mm, ports W / N / S, 4.25-5.75 GHz (Order 0 design §4.4).
Not optimized yet: its specs (one per formulation), criteria and substrate are in
[inputs/](../../inputs/) and explained in [demos](../../demos.md). When D1 passes its validation,
this directory receives its spec, `result.json`, `validation.json`, footprint and predicted
Touchstone files (thickness-equivalent and nominal substrate, FR408HR and EM528), and its copper
goes into the D1 window of upload O0-D, with the first 8 hex digits of the sha256 of `result.json`
in silkscreen.

What the board around the window is, for the spec (`divider-osh-m`):

- the window's west edge is P1's port plane; P2 and P3 sit on its north and south edges 6.0 mm
  from the west edge; each port is a 0.40 mm feed (4 cells of 0.10 mm, on grid nodes) running
  3.0 mm straight to its launch's reference plane;
- no L1 copper within 1.0 mm (5 h) of the window or the feeds, and the solder mask open over all
  of it; In1.Cu (the reference) solid under the window; the stick's east edge (where its tabs
  are) 4.0 mm (20 h) beyond the window;
- the window carries a placeholder footprint (`PHD1`): `yapnr fab check` stops on it
  (`FAB-PLACEHOLDER`) until the copper replaces it.
