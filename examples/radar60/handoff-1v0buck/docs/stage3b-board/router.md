<!-- markdownlint-disable -->

# radar60 stage 3b, track ROUTER: router clearances (A1-A4)

2026-10-04. Branch `claude/s3b-clear` (worktree `yapnr-wt/s3b-clear`), 12 commits on `origin/main`
21042fb, head **0d525ff**; local, not pushed. Design: `design.md` §1 A1-A4 and the `-classes` rung
(§1.9). Control branch for the rung A/B: `claude/s3b-clear-rungctl` (main + the 3 rung commits only;
worktree `yapnr-wt/s3b-clear-ctl`, local).

## Commits

| commit  | what                                                                                                            |
| ------- | --------------------------------------------------------------------------------------------------------------- |
| 6f49c9a | A2 pad-local clearance and solder mask margin (no flag: data-driven)                                            |
| f2c44f0 | A1 `board.class_clearance: maze`                                                                                |
| 58fd222 | A4 `board.edge: exact` (+ `board.keep_outline`)                                                                 |
| b826afa | A3 `board.dru_routing: true`                                                                                    |
| bf315ea | rung `11-ufbga201-fanout-6L-SGSGPS-classes`, `net_vias` check, rounded outline in native.py                     |
| 8eb39ef | `class_clearance: repair`; static offenders; exact SMD lands in the audit                                       |
| 570f222 | `edge: exact` keeps a wide net at its own edge inset                                                            |
| 02255b9 | class repair keeps the clean part of a ripped route and rejoins it                                              |
| b2ab1ef | rung: supply class 0.12 mm (0.15 left no legal interstitial VCC drop); native.py audit reads a width-less class |
| b78aef6 | checker test for `net_vias`                                                                                     |
| 4ceb5bc | `class_clearance`: joint escape options of two nets keep their class clearance                                  |
| 0d525ff | rung: 1.1 mm keep-outs in the rounded corners                                                                   |

## What the switches do (all opt-in; no rules key, no graph key, no change when undeclared)

- **A2**: ingest records `Pad.clearance_mm` (>0) and `mask_margin_mm` (>0.05 mm: the stock UFBGA's
  0.05 is not recorded, so no ladder graph changes). Pad halos grow to max(fab, local, margin + 1 µm);
  the joint, off-grid and fanout pad checks read `RouteGrid.pad_keepaways`.
- **A1** `class_clearance: maze`: per-net track halo and via keep-out from max(fab, class); every routed
  net judges static copper through tables at its width and clearance (`wide_pad_net`; new
  `wide_via_net` read by `via_passable` and the dense fields); escape options of two nets at their class
  clearance. After the route (`route/detail/class_check.py`): nets too close to the emitted escapes or
  pads, and the fewest that part every pair the exact rule (`Separation`, equality legal) finds too
  close, are ripped and rerouted exactly; failing that, the clean part of the old route is kept and
  rejoined; the rest stays open. An mm `copper_audit` of all emitted copper goes to
  `escape_diagnostics.class_clearance`. `repair`: the same checks on the fab-halo route.
- **A3** `dru_routing` (`pnr/dru_rules.py`, `route/detail/dru_apply.py`): no-via nets kept on their
  pads' layer; track layer bans; pair clearances (≤1 mm: raise the smaller side; larger: net keep-outs
  both ways around pads, escapes and fixed copper); length limits reported; hole-to-edge and edge
  limits fed to A4. Everything else is listed in `unmodelled` with a reason. Radar60's rules: 9 no-via
  nets (RF, XTAL), LVDS on F/B, In1/In4 track-free, SW–XTAL 8 mm, SW–RF 5 mm, QSPI ≤25 mm, hole to edge
  0.475 mm; 26 rule-constraints unmodelled (areas, sizes, pair geometry).
- **A4** `edge: exact` (`pnr/board_edge.py`): the drivers attach the source outline. Vias keep
  max(edge + r, hole_to_edge + ½drill, DRU limit + ½stroke + ½drill) + 1 µm from the contour (arcs,
  cut-outs); tracks keep edge + ½width, with a separate inset per width for wide nets. Writeback keeps the
  source outline when it frames the region.

## Radar trial-2 A/B (Mac via heavy.sh; trial-2 inputs written back and routed by `staged_signal`)

Same placement, rules and DRU as stage-3a trial 2, with the `pcbway-adv-6l-rf` profile copied from the
radar branch. Runs are in scratchpad `s3b/radar-ab/`. KiCad DRC of the routed candidate:

| arm (engine)    | switches              | clearance | mask bridge | not allowed | hole-edge | copper-edge | unconnected | validate |
| --------------- | --------------------- | --------- | ----------- | ----------- | --------- | ----------- | ----------- | -------- |
| trial 2 (s3a)   | –                     | 23        | 7           | 2           | 2         | 0           | 149         | exit 1   |
| A               | none (A2 only)        | 16        | 0           | 2           | 2         | 0           | 149         | exit 1   |
| B               | maze                  | 1         | 0           | 0           | 1         | 0           | 157         | exit 1   |
| E               | repair                | 1         | 0           | 4           | 2         | 0           | 154         | exit 1   |
| C               | maze + dru            | 1         | 0           | 0           | 1         | 24          | 150         | exit 1   |
| **J (0d525ff)** | **maze + dru + edge** | **1**     | 0           | 0           | **0**     | 0           | **152**     | **ok**   |
| K (0d525ff)     | repair + dru + edge   | 1         | 0           | 0           | 0         | 0           | 156         | ok       |

The one remaining clearance finding (pads of C54 and C60, 0.094 mm) and the 2 courtyard overlaps come
from the placement (bottom sites), not routing. C's copper-edge findings exposed a wide-net gap in the
rectangle edge model, which `edge: exact` now covers (570f222). Each route took 310-490 s; the switches
did not slow it. Class clearance costs 3 connections on this placement: 1V0_RF1, XTAL_N, QSPI_D2 and
CANH open; CANL routes. 1V0_RF1 cannot get past the GND fanout vias at 0.15 mm in either mode. That is
a capacity limit of this placement: the net is left open and reported, not emitted with a violation.
**Use all three together: `class_clearance: maze`, `dru_routing: true`, `edge: exact`.**

## Rung A/B: `11-ufbga201-fanout-6L-SGSGPS-classes`, `--compact --gloss` (Mac via heavy.sh)

| arm                           | seed 0                                                               | seed 1                                                       |
| ----------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------ |
| candidate 0d525ff             | **PASS**: open 0, DRC 0, checks 15/15, audit 0; place-route 51 s CPU | **PASS**: open 0, DRC 0, 15/15; 71 s                         |
| control (main + rung) 0d78ba3 | FAIL: 12 clearance + 9 mask bridges at FID1; 43 s                    | FAIL: 14 FID1 + 8 VCC-class clearance + 9 mask bridges; 42 s |

The earlier rounds found three problems in the rung itself, now fixed in b2ab1ef and 0d525ff:
supply 0.15 mm left no legal interstitial drop; native.py's audit crashed on a class with no width;
the placer put C8 in a rounded corner. GCP was not used for results: the us-west4 pair waited about
1 h for Spot capacity and was cancelled at $0, and the Montreal pair was cancelled minutes after it
started, on superseded code (< $0.01). Track total is under $0.02 (`gcp-spend.md`).

## Tests

- New targets: `pad_clearance_test`, `class_clearance_test`, `board_edge_test`, `dru_rules_test`.
- KiCad lane: `pad_clearance_kicad_test`, `board_edge_kicad_test` and `test_check_constraints`
  (`net_vias`), run by hand under KiCad 10's Python; all pass.
- Bazel, `--local_cpu_resources=2`. The output base was on a case-sensitive sparse image on the
  external disk, because the internal disk had 2.5 GB free. The image grew to 7.3 GB, so it was
  deleted after the runs, along with the worktree's `user.bazelrc`. At head, the new and changed
  targets pass.
- `//hardware/pnr/...`: 134 of 135 pass. The failure, `si_runner_test`, is an artefact of running Bazel
  under `nice`: the test expects 7 more steps of niceness than it can get.
- `test_detail_route` fails on main as well (pre-existing).
- prek is clean on every commit.

## For integration / limits

- `dense_maze.MODELLED_SOURCES` changed for `via_passable` and `_astar_reference`. A track that touches
  either must recompute the hashes after merging (`model_sources()`).
- The placer still models a rounded outline as its rectangle, so the rung keeps parts out of the
  corners with keep-outs. That gap belongs to the legalizer track.
- The copper audit does not cover fixed-block copper; the pair keep-outs do.
- A no-via net with pads on two layers is reported (`no_via_left`), not enforced.
- The fanout planner does not read the custom rules; the rung's CLK balls are ring 0 with surface exits.
- Mask margins of 0.05 mm or less are not ingested.
- `hard_rungs_test` and `check_constraints_test` cannot be built in one Bazel invocation: their
  precompile actions conflict (pre-existing).
