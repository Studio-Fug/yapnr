# Design: compact placement (`PNR_COMPACT`) and shrink-to-fit (`PNR_SHRINK`)

Status: implemented on branch `claude/compact`, off by default. Code: `hardware/pnr/pnr/compact_flags.py`
(the switches, stdlib only) and `hardware/pnr/pnr/place/compact.py` (metrics, cluster box,
legalizer settings, shrink search); the call sites guard on the switches. Tests:
`hardware/pnr/tests/test_compact.py`.

> Owner request (2026-10-03): the PnR can achieve a much more compact result for the demoed
> blocks, since they are two layers and simple slow digital routing. The minimum distance between
> parts looks too conservative during global placement and legalization.

Measured before the change (seed 0): the ladder's courtyards cover only 11 to 22 % of their
outlines. Four causes, one switch part each, and a fifth part (`DROPS`) for a defect the denser
placement exposed:

| Part        | Cause                                                                                                                                                                        | Change                                                                                                                                                   |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GP`        | The ladder places at `spread=1.3`, which "makes parts fill the whole board"; the random starts are spread over the whole interior.                                           | Spread 1.0; the starts are drawn in a cluster box around the fixed parts. Space grows only where the router measured congestion.                         |
| `LEGALIZE`  | Slots are `ceil((size + 0.4) / 0.25 mm)`: the routing clearance (0.4 mm on the ladder) between courtyards that KiCad already draws about 0.25 mm outside the pads.           | The courtyard gap (0.01 mm, or `board.courtyard_clearance_mm`), a copper margin only where a box hugs its pads, a 0.125 mm slot grid, pads off the edge. |
| `COURTYARD` | Courtyards are modelled symmetric about the footprint origin: a 1x8 pin header whose origin is pin 1 reserves twice its length, and `09-mcu-usb-31-header` cannot be placed. | Each part occupies its ingested body box (`Component.body`), off its origin and turned with the part.                                                    |
| `RANK`      | Selections among equally complete candidates ignore compactness.                                                                                                             | A compactness tie-break after every completion key and the vias.                                                                                         |
| `DROPS`     | A `plane_layer` net without a declared stack gets its plane vias from writeback's dog-bones after routing, where routed copper can enclose a pad (`08-chaser-20-plane`).     | The router plans those drops with the signal escapes before routing, as it does for a declared stack.                                                    |

## 1. Switches

- `PNR_COMPACT=1` turns on `GP`, `RANK`, `LEGALIZE`, `COURTYARD` and `DROPS`;
  `PNR_COMPACT_<PART>=0` drops one part (ablations).
- `run.py`'s `--compact-off` choices and the `ladder-cell` kind's `compact_off` list the same
  parts as `pnr/compact_flags.py` (tests keep them equal).
- `PNR_SHRINK=1` is separate and never on by default: it changes the board outline.
- Unset, every caller takes its unchanged path and the engine's JSON gains no key. The flag-off
  identity is tested on 04-inverter-leds-8 and 07-chaser-20 against the parent commit (the
  legalizer, the initial pool's starts and fixed poses on every platform; the whole placer on the
  platform the golden was recorded on). On every platform a guard test runs the placer, the
  legalizer and the initial pool with the compact-only functions patched to fail and checks
  that no part has an offset body.
- The ladder: `run.py --compact [--compact-off PART ...] [--shrink]` sets the variables after
  the runner strips the ambient `PNR_*` ones, so `provenance.json` records them. The
  `ladder-cell` experiment kind takes `compact`, `compact_off`, `shrink`, `gloss`,
  `gloss_measure` and `hard`.

## 2. Offset courtyards (`COURTYARD`)

- **Data:** the ingest already records `Component.body`, the box the symmetric `courtyard` is
  built from (courtyard, or body, united with the pads and silk), as it lies about the origin in
  the unrotated frame of the current side, mirrored with the pads. No ingest or schema change.
- **Rectangle:** `geometry.courtyard_rect` returns `pos + regions.rotate_box(body, rot)` and
  `geometry.body_shift(comp, rot)` its centre's offset from `pos` (exact at quarter turns). Block
  and line macros keep their centred courtyard.
- **Correct through the rectangle:** hard violations, `pose_checker`, the capacity proxy,
  relocation, feedback moves and the hierarchical extent; regions and aligns already measured
  `body`.
- **Converted between pose and slot centre:** `legalize._place_part(..., shift)` compares the
  candidate slot centres less the shift with the target, the hard group discs, the edge box and
  the candidate cost, so all of them stay in pose space; callers set `pos = slot centre - shift`
  (the main loop, `available_pose`, the power-first look-ahead, `centre_limits`,
  `refine_channels`, `matched.refine_matched`). Landing reserves and region masks take the
  shift; a side retry recomputes it.
- **Global placement:** `model.global_place` and `cost_inspect.Objective` use the body half sizes
  and the body offset expected under the rotation probabilities, mixed over both sides for a
  side-free part like the pin offsets; the overlap, outline, edge and keep-out terms use
  `pos + offset`. The cost capture replays the same offsets (`effective_shift`).
- **Also:** edge-resolved fixed poses (the body flush with the edge, at the held rotation),
  ref-relative keep-outs (against the body's edge), the elastic mesh and its projection, hull
  macro boards (`hull.gp_bodies`), `regions.check_feasible`, and `plane_intent`, which widens
  the body to its symmetric via-array reservation.
- **Unchanged** (the symmetric box contains the body): `_fit_outline`, rows, line-group spacing,
  `_opposite_body_basins`, hierarchical block areas. `PNR_POWER_FIRST=1` refuses `PNR_COMPACT`.

## 3. Global placement (`GP`)

- `compact.spread()` returns 1.0 at the top of `placer.place`,
  `initial_pool.select_initial_placement` and `feedback.route_and_place`, overriding the ladder's
  `spread=1.3`; the feedback loop's per-part inflation (RePlAce, from routed congestion) is
  unchanged.
- `compact.cluster_box`: a rectangle of twice the summed body area with the board's aspect,
  centred on the fixed parts (else the board) and clamped inside it. `global_place(start_box=)`
  and the initial pool's stratified and Latin starts map their existing random draws into it (no
  new draws).
- The hierarchical regression driver's block trials also try the utilisations 0.5 and 0.6 (its
  own budget, `hier_case.budget_of`; a caller of `pnr.hier` sets its own).

## 4. Legalizer (`LEGALIZE`)

`compact.legalize_settings(graph, constraints, rules)` returns `(gap, grid_mm, margins)`, and
`compact.placement_clearance(constraints)` replaces `board.default_clearance_mm` wherever
placement reads it as a courtyard clearance: the placer (legalize, `snap_aligns`,
`refine_matched`, the side moves), the global objective and its inspector, the initial pool's
basin fallback and the hierarchical feedback boards. The copper margins go wherever that
clearance is checked after legalization: `snap_aligns`, the side moves (`pose_checker`) and
the feedback `MoveBoard` (both parts' margins added to the gap). Rows and line groups keep the
board's `default_clearance_mm`, not the gap, so they need none.

- **Gap:** `board.courtyard_clearance_mm` when authored, else 0.01 mm (KiCad courtyards carry
  about 0.25 mm around the pads; touching courtyards are legal; 10 µm guards nanometre rounding).
- **Copper margin:** `m = max(0, c / 2 - pad inset)`, with `c` the routing copper clearance and
  the inset the smallest distance from a pad to the part's box: zero for library courtyards, up
  to `c / 2` for a box that hugs its pads (a part without a courtyard layer). Two neighbours'
  pads keep at least `c`.
- **Slot:** `ceil((w · inflation + gap + 2m) / g)` cells with `g = 0.125 mm`; edge-band and
  align caps subtract the margin too.
- **Edges:** the pad and drill edge rule (`PNR_PAD_EDGE_CLEARANCE`) is on.
- Copper clearance stays with the router and native DRC; `channels.py` (escape demand) is
  unchanged.

## 5. Compactness metric and selection (`RANK`)

`compact.metrics(graph, W, H)` is measured on the body boxes of every part, fixed ones included,
whatever the switches (so every arm is measured alike): `bbox_mm2`, `utilization = ΣA / bbox`,
`occupancy = ΣA / WH` and `bucket = floor(20 · bbox / WH)`, rounded to 1e-3. The ladder records
the same measure in each case's `result.json` (`compactness`, every arm).

- `initial_pool.route_rank` (the pool's routed finalists, the halving screen, `mc_case`, the
  hierarchical top seed): `missing, unresolved, length_unmatched, vias, bucket, length`, then
  the id: on a fixed outline a smaller bounding box is free but a via is not. Under
  `PNR_SHRINK` (the outline follows the bounding box) the bucket ranks before the vias. A record
  without a bucket (the switch off) keeps the previous key.
- Halving native and deep stages: `bbox_mm2` after every completion key, before the id.
- The place-route loop's best round: `(overflow, unfinished, bbox_mm2)`.

## 6. Plane drops before routing (`DROPS`)

A net class with a `plane_layer` and no declared stack (the legacy plane path, e.g.
`08-chaser-20-plane`) routed its signals first and left every surface pad's via to the plane to
writeback's dog-bone search (`writeback.apply_planes`, eight directions and five distances). Where
routed copper enclosed a pad, the search failed (`planes: no clear fanout for ...`) and the pad
stayed off the plane: 1 seed in 6 with the switch off, 3 in 6 under compact. A placement-level
count (a dog-bone site clear of every other net's pad and of the edge, writeback's search
without the routed copper) found a free site for every plane pad in all 52 runs, so a placement
margin would not have helped; the routed copper closed them.

With `DROPS`, `route_board` treats those nets as a declared stack's plane nets are treated: each
surface pad gets a drop (stub plus through via, the pad's required width) planned jointly with the
signal exits and reserved before the maze runs. The via may cross its own net's plane region on
the plane layer (`RouteGrid.own_plane_cells`; blocked for tracks there, not for vias), which is
cleared after the planning so the maze kernels see the grid they model. The planes stage then
dog-bones only the pads still without a through contact (`skip_connected`).

## 7. Shrink-to-fit (`PNR_SHRINK`, flat driver)

`route_and_place` treats the outline as an envelope:

1. The loop runs on the envelope (the fallback); the search stops unless it converges legally.
2. The first probe is the envelope run's body bounding box plus `2 · (edge clearance + 0.5 mm)`
   per axis, as a scale of the outline.
3. Then bisection between the largest failed scale (at first the lower bound
   `max(√(ΣA / 0.7 WH), the part and fixed-part bounds)`) and the smallest converged one: at most
   four probes, sizes rounded to 0.5 mm.
4. Each probe is a same-seed `_place_route_loop` on `compact.scaled_constraints`. The outline
   keeps its origin and gives up its north and east: a fixed `at` keeps its absolute position,
   except one within 25 % of the north or east edge, which keeps its distance to that edge
   (`moved_fixed` in the record lists those). The lower bound keeps every fixed part's box
   inside the scaled outline.
5. The smallest probe that converges legally sets the constraints' outline and fixed poses and
   `placed.outline`, which write-back stamps; `pnr-report.json` records the search (`shrink`).

Skipped, and recorded so, with `auto_outline`, keep-outs or regions; the hierarchical and Monte
Carlo drivers never call it and record `shrink: {"skipped": "hier driver"}` (or `"mc driver"`),
and `run.py` exempts the hard rungs (their outline is part of the rung). Under `--shrink` the
runner's constraint audit judges hard edges against `placed.json`'s outline, otherwise against
the design's.

## 8. Trace and animation

- The trace header carries each part's `body` (µm) under `COURTYARD`; the renderer draws and
  measures it (courtyards, edge tethers, rigid line boxes).
- Shrink runs are trace scopes `shrink-NN`, the choice the selection `shrink`; the provenance
  model then follows only the chosen run and the header outline becomes its outline.
- `animate_ladder.py` and `animate_showcases.py` take `--runner-arg ARG` (repeatable); the
  options that changed a run's boards (`--compact`, `--compact-off`, `--shrink`, `--gloss`,
  `--gloss-flag`, read back from its provenance) are part of each animation's `config` in the
  manifest. `animate_ladder.py`'s baseline fallback reruns a failed case with the same
  arguments unless `--fallback-runner-arg ARG` names its own (`--fallback-runner-arg=--gloss`:
  a compact ladder's failed case falls back to the default mode).

## 9. Determinism

No new random streams (the cluster box maps the existing draws), integer buckets, rounded floats,
a fixed probe order and exact quarter-turn offsets: runs stay bitwise reproducible per platform,
as before.

## 10. Measurements and the default-on rule

Default-on (with `PNR_COMPACT=0` restoring the previous behaviour) only if, for each gloss
setting, every case that passes with the switch off also passes with it on, opens and DRC
findings are no worse, and `09-mcu-usb-31-header` passes. Shrink stays opt-in.

**Outcome (2026-10-03): both stay off by default.** Measured on GCP C4D (x86-64), every arm on
one image, seeds 0 and 1 unless noted:

| Cells                              | off            | compact                     | `COURTYARD` alone |
| ---------------------------------- | -------------- | --------------------------- | ----------------- |
| 8 ladder cases + 4 showcases (24)  | 24 pass        | 24 pass (also with gloss)   | 24 pass           |
| `09-mcu-usb-31-header` (2)         | 0 (pool fails) | 2 pass                      | 2 pass            |
| `08-chaser-20-plane`, seeds 0 to 9 | 7 pass         | 10 pass (7 without `DROPS`) | 7 pass            |
| nightly hard rungs, seed 0 (14)    | 14 pass        | 14 pass                     | 14 pass           |
| manual `09-mcu-usb-31` rungs (16)  | 16 pass        | 11 pass                     | 13 pass           |
| `10-quad-bank-56` (2)              | 1 pass         | 2 pass                      | 2 pass            |

On the 24 cells compact cuts the summed placed bounding box from 13068 to 8122 mm² (median
utilisation 0.36 to 0.55) and copper from 4777 to 4022 mm, for 28 more vias (282 to 310) and
21 % more CPU; with `RANK` after the vias, dropping `RANK` changes nothing there. The manual
`09-mcu-usb-31` rungs fail the rule: their USB pairs end out of skew (`skew_out_of_range`) or
with one leg or `VBUS` unrouted on 5 cells under compact and 3 under `COURTYARD` alone, all of
which pass with the switch off. Dense placement leaves no room for the pair tuning; reserving
it (inflating the parts on declared pairs and length groups) is the next step before another
A/B. Shrink passes 23 of 24: on `line-chaser-20` seed 1 a 0.4 mm GND track runs 0.175 mm from
the shrunk edge, through pad cells the router's edge inset leaves open.

The full tables are in the workflow's A/B notes; the ladder documentation
([compact placement](../regression-ladder.md#compact-placement-opt-in)) summarises them.

Still open: the 0.01 mm courtyard gap (no DRC finding in any arm).
