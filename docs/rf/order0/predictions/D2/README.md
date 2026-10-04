# D2: the optimizer's thick divider

**Compute stage, 2026-10-04 (drafts).** No formulation passes (8 in two waves,
[runs/](runs/), [selection.json](selection.json); wave 2: [inputs/wave2/](../../inputs/wave2/)).
The nearest misses fail only the coarse-grid \|S21\| limit (−3.40 dB), by 0.012 dB at 5.75 GHz,
and pass fine and finer; R1t reaches −3.549 dB on the same substrate. Whether O0-W carries the
nearest miss (`d2-star-sched`, re-validated on W-eq, W-nom and W-eq-em528 in
[variants/](variants/)) as a pre-registered failing design is the owner's decision.

Region W (L1 over B.Cu, inner layers removed), window 20 × 24 mm, ports W / N / S, 4.25-5.75 GHz
(Order 0 design §4.4). Its specs (one per formulation), criteria and substrate are in
[inputs/](../../inputs/) and explained in [demos](../../demos.md). If D2 ships, this directory
receives its spec, `result.json`, `validation.json`, footprint and
predicted Touchstone files (FR408HR and EM528), and its copper goes into the D2 window of upload
O0-W.

What the board around the window is, for the spec (`divider-osh-w`), which the solver's open L1
over an infinite ground does not see:

- the window's west edge is P1's port plane; P2 and P3 sit on its north and south edges 10.0 mm
  from the west edge (the spec's ports); each port is a 3.0 mm feed (6 cells of
  0.50 mm) running 4.0 mm straight to its launch's reference plane;
- the L1 ground pour stays 4.2 mm (3 h) from the window and the feeds; In1.Cu and In2.Cu are
  removed under the window, its keep-away and the feeds, and remain as a stitched ring beyond;
- the reference plane (B.Cu, L4) ends at the stick's east edge, 6.0 mm (4.3 h) beyond the window;
  to the west, north and south it extends 14 mm (the feeds and launches);
- the TRL line of region W (B01-B05) is 48.6 Ω with that pour and ring (2D), about 1.4 Ω under
  the solver's open-L1 port: the comparison renormalizes the measurement to the solver's port
  impedance ([pre-registration](../../preregistration.md)). The same holds for R1t;
- the window carries a placeholder footprint (`PHD2`): `yapnr fab check` stops on it
  (`FAB-PLACEHOLDER`) until the copper replaces it.
