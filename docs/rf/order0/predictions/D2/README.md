# D2: the optimizer's thick divider

**Compute stage, 2026-10-04 (corrected on review).** No formulation meets the −20 dB \|S11\|
production spec (8 formulations in two waves, [runs/](runs/), [selection.json](selection.json);
wave 2: [inputs/wave2/](../../inputs/wave2/)); the nearest, `d2-star-sched`, reaches worst
\|S11\| ≈ −19.3 to −19.4 dB (coarse/fine/finer), a 0.6–0.7 dB miss.

**\|S21\|/\|S31\| validation, corrected:** an earlier draft of this page checked `d2-star-sched`
against −3.40 dB (coarse) / −3.55 dB (fine, finer), figures with no recorded decision behind
them, and reported a 0.012 dB miss at the coarse grid. The design's §4.4 fixed validation limits
are actually **−3.45 dB (coarse) / −3.6 dB (fine, finer)**, unchanged by D-O0-11 (which sets only
the −3.4/−3.5 dB production spec, not this separate validation margin). Against the plan's own
limits, `d2-star-sched` **passes** \|S21\|/\|S31\| at every grid (coarse −3.412, fine −3.397,
finer −3.394 dB, all above −3.45/−3.6 respectively). So D2's only real miss is \|S11\|, not
\|S21\|; R1t reaches −3.549 dB on the same substrate. Whether O0-W carries `d2-star-sched`
(re-validated on W-eq, W-nom and W-eq-em528 in [variants/](variants/)) as a pre-registered
\|S11\|-missing design is the owner's decision; recorded here as `compute.md`'s recommendation
absent other owner direction.

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
- the window carries `d2-star-sched`'s exported copper (`catalog.o_optimized`; the board's
  copper equals `runs/d2-star-sched/footprint.kicad_mod`, 122.5 mm², polygon symmetric
  difference 0), with `03b7d938` on the back silkscreen.

**Against the spec, not only the validation limits:** the loss-corrected \|S21\| = \|S31\| that
"meets spec" uses is −3.43 to −3.45 dB on W-eq and W-nom (a 0.03-0.05 dB miss of −3.4 dB) and
−3.35 to −3.36 dB on W-eq-em528 ([corrected/](../corrected/summary.json)); the committed in-job
criteria ([d2.json](../../inputs/criteria/d2.json), −3.40 dB coarse) also fail at the coarse grid.
So on FR408HR the prediction misses the spec on \|S11\| and, narrowly, on \|S21\|; both versions
of the \|S21\| limit are reported in the [pre-registration](../../preregistration.md).
