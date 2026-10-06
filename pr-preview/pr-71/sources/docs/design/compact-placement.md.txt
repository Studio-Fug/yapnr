# Design: compact placement (`PNR_COMPACT`) and shrink-to-fit (`PNR_SHRINK`)

Status: implemented on branch `claude/compact`, off by default; section 11 (legalizer spacing
and turns) on branch `claude/legalize`, off by default; section 12 (the `09-mcu-usb-31` lane:
`PAIRS`, `RELAX`, power-first placement, the router key) on branch `claude/lv2-compact`. Code: `hardware/pnr/pnr/compact_flags.py`
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

- `PNR_COMPACT=1` turns on `GP`, `RANK`, `LEGALIZE`, `COURTYARD` and `DROPS`, the three
  legalizer parts of section 11 (`WIRE`, `TURN`, `SATELLITES`) and the two parts of section 12
  (`PAIRS`, `RELAX`); `PNR_COMPACT_<PART>=0` drops one part (ablations).
- The router key (`pnr.feedback.signals.current_key`) carries a `compact` field, a digest of the
  active parts, `PNR_SHRINK` and the legalizer switches' effective values (section 12), so
  evaluations and block libraries of two placement configurations never mix.
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
  `_opposite_body_basins`, hierarchical block areas.
- **Power-first placement** (`PNR_POWER_FIRST=1`, `power_first.staged_place`): `GP` (spread and
  the cluster box of the staged placer's random starts), `LEGALIZE` (the courtyard gap in the staged
  overlap term and the legalizer, the slot grid, the copper margins), `COURTYARD`, `DROPS` and
  `RANK` apply; `WIRE` and `TURN` do not (the power-first legalizer and its retry judge every slot
  by the hot loops; section 11). Line groups (`SATELLITES`) are refused there with or without
  compact.

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
- The committed animations (`docs/animations/`, 2026-10-03) are rendered from traced runs with
  `--compact --gloss` (ladder: 8 of 8 pass; showcases: 5 of 5), with the gloss stage as a
  before/after (renderer 3, [Regression ladder](../regression-ladder.md#reading-an-animation));
  CI's traced runs take the same options.

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

Still open: the 0.01 mm courtyard gap (no DRC finding in any arm). The manual `09-mcu-usb-31`
lane is resolved in section 12.

## 11. Spacing and turns at legalization (`PNR_GP_POLISH`, `PNR_LEGALIZE_HPWL`, ...)

> Owner (2026-10-04): the legalizer moves a lot of components. How were the global placement
> distances tuned? Are there slight overlaps with the packed placement? Does legalization have
> the resolution not to treat the closer packing as overlaps? A component the legalizer moves
> should have its rotation re-evaluated to minimise wirelength: the line-constrained LED chaser
> in the README ends with extra track because its series resistors are not turned to lie flush
> with the row after legalization.

**Findings** (a GCP probe of 81 legalize calls per arm and paired Mac prototypes on 64 recorded
pool starts; the workflow's notes keep the data):

- **Nothing was tuned.** The global weights are the first placer's, unchanged; compact chose the
  0.01 mm gap, the 0.125 mm grid, the cluster box and spread 1.0 in its design and measured them
  only as a bundle.
- **Global placement does leave slight overlaps under compact** (6.8 true courtyard overlaps per
  result, median depth 0.07 mm), but removing them alone changes nothing measurable after routing.
- **Resolution is not why parts move.** Re-legalizing the same result on a 0.01 mm grid removes
  2.6 % of the displacement for 375 times the CPU; snapping is 4 % of the moved parts.
- **The legalizer moves parts mainly for its routing-channel cost** (`channels.py`, 70 % of the
  moves over 0.1 mm), which global placement never sees, then for the overlaps.
- **The legalizer never turned a part on these boards:** it tries `rot + 90` only when the global
  turn has no free slot at all, and its slot cost has no wirelength. The README resistors are not
  a turn-in-place case: the legalizer put them on the far side of the LED row (the nearest free
  slot), where every correctly polarised turn has the same wirelength. Choosing the slot and the
  turn together, with wirelength in the cost, puts them upright beside their LEDs.

Seven switches, each off by default and usable with or without `PNR_COMPACT`
(`pnr/legalize_flags.py`, stdlib only):

| Switch                               | Part | What it does                                                                                                                                                                                                          |
| ------------------------------------ | ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PNR_GP_POLISH=1`                    | A    | 200 more iterations of `global_place`'s own loop with turns and free sides frozen, the overlap, outline and keep-out terms on the legalizer's slots, the overlap weight ramped 1 to 100 and the step 0.3 to 0.005 mm. |
| `PNR_GP_CHANNELS=<lambda>`           | A    | The polish (implied) also carries the legalizer's channel cost, smooth: `lambda / 2 · Σ shortage²` over facing pad rows.                                                                                              |
| `PNR_POOL_SOURCE_CLAMP=1`            | A4   | The initial pool's source start begins with every movable part clamped into the outline (the cluster box under `GP`).                                                                                                 |
| `PNR_LEGALIZE_HPWL=<w>`              | C1   | The legalizer's slot cost gains `w` times the part's wirelength, and each part's slot is searched at all four turns: the cheapest wins.                                                                               |
| `PNR_LEGALIZE_REORIENT=1`            | C2   | After legalization, greedy in-place turns that shorten a part's wirelength, stay legal and do not raise its channel shortage; `=wire` drops the channel guard.                                                        |
| `PNR_LEGALIZE_CHANNEL_CLEARANCE=fab` | D    | The legalizer's channel model spaces unclassed nets at the fab clearance (the router's) instead of the board's `default_clearance_mm`.                                                                                |
| `PNR_LINE_SATELLITES=1`              | E    | A line group carries each member's satellite (a free two-pad part on a two-pin net to one member pad) flush beside the member, in line with it.                                                                       |

`run.py` takes `--gp-polish`, `--gp-channels L`, `--pool-source-clamp`, `--legalize-hpwl W`,
`--legalize-reorient [wire]`, `--legalize-channel-clearance fab` and `--line-satellites` (after
it strips the ambient `PNR_*` variables, so `provenance.json` records them); the `ladder-cell`
kind takes `gp_polish`, `gp_channels`, `pool_source_clamp`, `legalize_hpwl`,
`legalize_reorient`, `legalize_reorient_wire`, `legalize_channel_clearance_fab` and
`line_satellites`. A trace's header lists the active switches (`placement_switches`).

Three of them are `PNR_COMPACT` parts since the review-fix A/B below: `WIRE` (`PNR_LEGALIZE_HPWL`
at weight 4), `TURN` (`PNR_LEGALIZE_REORIENT=wire`) and `SATELLITES` (`PNR_LINE_SATELLITES`).
Under `PNR_COMPACT=1` each is on unless its variable is set (`0` included) or
`PNR_COMPACT_<PART>=0` drops it. The polish (A) and the fab channel clearance (D) stay opt-in.

### A. Global placement and the legalizer agree on spacing

`pnr/place/gp_polish.py`, run by `model.global_place(polish=...)` from the placer:

- **The legalizer's slot.** Per frozen turn and side, `ceil((w · s + clearance + 2m) / g) · g` per
  axis, centred where the legalizer centres it (the occupied box centre, offset courtyards
  included), with `s`, `clearance`, `m` and `g` exactly as `legalize()` gets them (compact: 1 or
  the feedback inflation, the gap, the margins, 0.125 mm; off: 1.3 or the inflation, the board
  clearance, none, 0.25 mm). Slots of whole cells that do not overlap stay disjoint after the
  nearest-cell snap, so a polished result legalizes by snapping alone unless the channel cost
  moves a part. With the pad-edge rule the slot centre is also held inside `pad_edge_box`.
- **The channel cost, soft.** The required gap of each ordered pair and facing direction is
  `ChannelModel.active_demand` over the facing rows' nets that reach a third part (pair-local
  nets reserve nothing), precomputed once for the frozen turns; the term is counted on facing
  envelopes only (gap at least 0, projections overlapping), `ChannelModel.penalty`'s definition.
  Enforcing the full demand as a hard spacing over-spaces (+5 to +11 % wirelength, +11 to +32 %
  bounding box in the prototype); the legalizer itself tolerates some shortage.
- **Everything else stays on** (wirelength, regions, aligns, groups, keep-outs, edge aligns,
  planes, matched lengths, side terms); a fresh positions-only Adam runs the fixed 200 steps.
  The trace shows them as more global-placement snapshots, so parts glide apart before
  legalization instead of jumping at it.
- Not with hull macros (ignored) or power-first placement (never asked).

### B. Resolution: no change

The 0.125 mm grid stays: a finer grid or an Abacus-style pass would buy 2.6 % of the
displacement at 375 times the CPU, and Abacus assumes equal-height rows and a separable cost
that the channel cost is not. Part A removes the grid's share instead.

### C. Turns

- **C1, the slot and the turn together** (`legalize.py`: `wire_cost`, `wire_turns`, the nested
  `search_scored`). The candidate cost (displacement², channel demand, soft rules) gains
  `w · wire_cost`: the half-perimeter wirelength of the part's nets with placed parts at their
  legal pins, unplaced parts at their global targets and plane nets left out. `search_scored`
  runs the existing `search()` once per turn of `[rot, rot + 90, rot + 180, rot + 270]` and keeps
  the cheapest outcome by `slot_cost` (which gains the same term); a tie keeps the earlier turn,
  hard rotations and `allow_rotation=False` keep the global turn, line and block macros turn as
  one body, the side retry compares both sides' best turns, and under `lookahead: regions` a turn
  whose every slot strands a part counts only when every turn does. The region and align reach
  then also covers the four turns.
  - The placer passes the weight to `legalize()` (`wire_weight`), which never reads the
    environment: power-first placement and the initial pool's basin fallback keep the plain
    legalizer, as they keep it without C2.
  - Parts on a `diff_pair` or `length_match` net keep the plain search (`wire_exempt`,
    `reorient.matched_refs`): no wirelength term and no four-turn choice, so the two legs of a
    pair are not turned apart; the matched-length pass evens them afterwards, as without C1.
  - A backtracking ban on a slot also bans the slot at the half turn, which covers the same
    cells (hull macros aside).
  - Cost: the channel cost dominates the search (85 % of the legalizer's time in a profile of
    `09-mcu-usb-31`; the wirelength term 2 %), and four turns score it four times. The search
    therefore ranks every free cell by `displacement² + w · wirelength`, a lower bound of its full
    cost (the channel and soft terms are never negative), scores the 64 lowest in full, and then
    only the cells whose bound does not exceed the best full cost (`_cheapest`). The sums are
    taken in the same order as the full scoring, so the chosen slot, ties included, is the same:
    96 of 96 recorded pool starts on 12 boards give byte-identical placements, and the
    legalizer's CPU under C1 falls from 3.7 times compact's to 1.0 times. Not used with cost
    capture (it records every candidate) or for a part with soft rules.
  - Cost capture records the wirelength term with the others, and the candidate fields gain a
    `wire_raw` column.
- **C2, in-place turns** (`pnr/place/reorient.py`), run in `placer.place` after `legalize`,
  `snap_aligns` and `refine_matched` and before `detail_moves`, so inside every pool start, line
  group macro graph and hierarchical block, and only where the placer may turn parts (`orient`).
  Greedy, best wirelength gain first (then the reference, then the turn), to a fixed point with
  at most four times as many turns in all as candidates (every accepted turn shortens the total,
  so the cap only bounds the work), each about the slot centre and checked by
  `metrics.pose_checker` with the legalizer's clearance, spreading factor, margins and pad-edge
  rule. Never turned: fixed, locked and hard-rotated parts, row and line-group members, macros,
  and parts on a diff-pair or length-match net. Ties keep the turn. With `=1` a turn must not
  raise the part's `ChannelModel.penalty` against the others.

### D. The channel model at the fab clearance (`PNR_LEGALIZE_CHANNEL_CLEARANCE=fab`)

The legalizer's channel model gives a net without a class the board's `default_clearance_mm`
(0.4 mm on the ladder) between tracks and pads, where the router and KiCad hold the fab clearance
(`fab.clearance_mm`, 0.2 mm): one escaping 0.25 mm track then asks 1.05 mm between facing pad
rows where 0.65 mm routes. Under compact placement that cost causes 70 % of the legalizer's moves
over 0.1 mm. With the switch the placer builds the legalizer's model (and C2's guard) at the fab
clearance; net classes and pairs with their own clearance keep it. Reports and every other model
are unchanged.

### E. Line satellites (`PNR_LINE_SATELLITES=1`)

The README's line-constrained chaser holds the LEDs in a rigid line, but their series resistors
are free parts: global placement leaves them anywhere along their nets, and wirelength alone
cannot prefer the pose beside the LED (a resistor anywhere on the straight path from its driver
pin to its LED has the same wirelength). With the switch a line group carries its members'
satellites (`line_group.satellites`): a free two-pad part (no constraint names it, not locked, on
the top side, on no diff-pair or length-match net) one of whose pads shares a two-pin net with one
member pad. Each sits flush beside its member, across the line on the side of that pad, turned so
its pads lie across the line with the shared pad facing the member's, the two shared pads in line
and the courtyards the line's clearance apart (`satellite_layout`); a member pad that lies along
the line takes none, nor does a satellite that would come too close to another. The satellites
are members of the line's rigid macro, so global placement turns the line knowing where the
resistors' other nets go, and the legalizer packs the row as one body. Not on a double-sided board
(a satellite there may want the other side).

### Determinism and tests

Fixed step counts, no new random draws, float64 in the legalizer, first-index ties, a fixed
turn order and `(gain, ref, turn)` order in C2: a seed reproduces its board on one platform.
Tests: `test_legalize_flags.py` (parsing, the runner's options, the trace header, and the flag-off
identity on 04-inverter-leds-8 and 07-chaser-20 against the compact goldens with every new
function patched to fail), `test_legalize_wire.py` (the README case: the near-side slot with the
term and the far one without; a reversed part half-turned; ties; hard rotations; offset courtyards
at the chosen turn; and a line-group chaser whose series resistors end upright beside their LEDs
only with the term), `test_reorient.py` (the best legal turn; neighbours, keep-outs and the spread
factor rule turns out; the channel guard; exclusions; idempotence; the placer only gets shorter)
and `test_gp_polish.py` (a tight toy packing legalizes by snapping alone after the polish, at most
half a cell diagonal per part, against 2.9 mm without; the channel term opens an escaping pair and
ignores a pair-local net; zero steps change nothing; the source clamp).

### Measurements

Wave 0 (2026-10-04, darwin-arm64, compact): the design's 64 recorded pool starts (8 ladder cases
and showcases, seed 0) replayed through the unchanged placer with each switch set, every
placement legal and routed once at the pool's finalist budget, paired against compact as on
`main` on the same global result. 56 starts without the source start (which begins up to 233 mm
off the board); base copper 191 mm per board:

| Switches                                            | Legalizer move | Moved > 0.5 mm | HPWL    | Bounding box | Copper per board [95 % CI] | Vias  |
| --------------------------------------------------- | -------------- | -------------- | ------- | ------------ | -------------------------- | ----- |
| none (compact as on `main`)                         | 2.31 mm        | 76 %           |         |              |                            |       |
| `GP_POLISH`                                         | 1.92 mm        | 70 %           | −1.8 %  | +1.4 %       | +0.1 [−4.1, +4.4]          | +0.36 |
| `GP_CHANNELS=1`                                     | 0.53 mm        | 30 %           | −3.1 %  | +6.7 %       | −1.8 [−6.8, +3.2]          | −1.88 |
| `GP_CHANNELS=0.5` (placement only)                  | 1.06 mm        | 50 %           | −4.5 %  | +1.9 %       |                            |       |
| `LEGALIZE_REORIENT=wire`                            | 2.31 mm        | 76 %           | −2.3 %  | −0.9 %       | −3.5 [−7.9, +0.9]          | −0.14 |
| `LEGALIZE_HPWL=4`                                   | 3.28 mm        | 95 %           | −13.4 % | −5.4 %       | −22.0 [−28.7, −15.3]       | −1.25 |
| `LEGALIZE_HPWL=4`, `REORIENT=1`                     | 3.28 mm        | 95 %           | −13.6 % | −5.4 %       | −23.3 [−29.5, −17.2]       | −1.20 |
| `LEGALIZE_HPWL=4`, `REORIENT=wire`                  | 3.28 mm        | 95 %           | −14.6 % | −6.0 %       | −26.6 [−32.5, −20.8]       | −1.50 |
| `LEGALIZE_HPWL=16`, `REORIENT=wire`                 | 4.20 mm        | 95 %           | −25.6 % | −11.1 %      | −46.5 [−54.9, −38.0]       | −2.29 |
| `GP_CHANNELS=1`, `LEGALIZE_HPWL=4`                  | 2.42 mm        | 89 %           | −13.4 % | −0.2 %       | −20.8 [−26.5, −15.1]       | −1.38 |
| `GP_CHANNELS=1`, `LEGALIZE_HPWL=4`, `REORIENT=wire` | 2.42 mm        | 89 %           | −14.1 % | −0.5 %       | −21.2 [−27.9, −14.4]       | −1.52 |

- Every figure lies inside the prototype's confidence interval; the wirelength term reproduces the
  prototype's placements start by start, and the engine without switches its copper on 60 of 64
  starts (the replay rounds the recorded targets to float32).
- `REORIENT=1` (the channel guard) turns little: −0.5 % HPWL alone and −1.3 mm after
  `LEGALIZE_HPWL=4`, against −2.3 % and −4.6 mm without the guard, whose turns raise the
  channel model's shortage score from 1.2 to 2.8 mm² per board. The A/B measures both.
- Legalization CPU per start rises from 0.21 s to 0.77 s with `LEGALIZE_HPWL` (four searches and
  the term) and the polish adds about 0.2 s; global placement is not counted here.
- README board (`line-chaser-20`, seed 0, start 00): 272.0 mm as on `main`; 295.7 mm with
  `LEGALIZE_HPWL=4` alone; 257.9 mm (21 vias) with `REORIENT=wire` too, the series resistors R4,
  R6 and R7 upright beside their LEDs; 277.2 mm (18 vias) with `GP_CHANNELS=1` as well.
- Switches off: the unit suites, and `run.py --compact` on 04-inverter-leds-8 and 05-timer-led-10,
  give the same placements and routes as `main`.

These are one platform, seed 0, one route per placement, without the pool's ranking, feedback
rounds or gloss. Adoption waits for the ladder A/B (8 cases and 4 showcases on two seeds, the
header rung, then 08-chaser-20-plane on ten seeds, the hard rungs, the manual `09-mcu-usb-31`
lane and `10-quad-bank-56`): a switch becomes a `PNR_COMPACT` part only if no cell loses
completion or gains DRC or skew findings, copper improves with its 95 % interval below zero on
the core cells and no hard family is more than 2 % worse, vias rise by at most 0.5 per board,
placement CPU by at most 30 % and the bounding box by at most 5 % (`GP_CHANNELS=1` is at +6.7 %
here, 0.5 the fallback). None of them targets the manual lane's failure (no room to tune the USB
pairs), so compact placement itself is expected to stay opt-in.

### Review-fix A/B (2026-10-04)

The switches after the review fixes above (the exact prune, matched parts exempt, half-turn bans),
paired against compact placement as on `main`. Core: the 8 ladder cases and 4 showcases at seeds
0 and 1 on darwin-arm64 (24 cells); hard cells on GCP x86. Copper is paired over cells both arms
pass, with a 95 % interval clustered by case.

| Arm (with `PNR_COMPACT`)    | Core copper per board | Vias  | Bbox   | Placement CPU | USB-pair lanes pass | Nightly 14 + 07-rel, 08 x 8, quad x 2 |
| --------------------------- | --------------------- | ----- | ------ | ------------- | ------------------- | ------------------------------------- |
| none (compact as on `main`) |                       |       |        |               | 22 / 36             | all pass                              |
| `WIRE` + `TURN`             | −27.4 [−47.4, −7.5]   | −1.75 | −6.4 % | ×1.10         | 23 / 36             | all pass; −52.0, −37.7, −184 mm       |
| `CHANNEL_CLEARANCE=fab`     | −12.1 [−35.3, +11.2]  | +0.50 | −8.2 % | ×1.01         | 23 / 36 (+4.3 vias) | all pass; −7.2, −19.2, −86.8 mm       |
| `WIRE` + `TURN` + fab       | −30.2 [−49.7, −10.7]  | −0.54 | −9.7 % | ×1.03         | 16 / 36             | 1 fail                                |

- **Cost.** The prune leaves every placement as it was (96 of 96 recorded pool starts, and the
  routed copper of all 33 re-run core cells is byte-identical) and brings placement CPU from
  1.92 times compact's to 1.10 on the core cells and 0.89 on the nightly rungs.
- **The USB-pair lanes** (`09-mcu-usb-31` x 7 at seeds 0 to 3, the header rung at 0 to 5 and the
  Monte Carlo driver at 0 and 1): with the matched parts exempt, `WIRE` + `TURN` passes 23 of 36
  against compact's 22 (9 cells pass only with it, 8 only without), where the version before the
  fixes passed only 18 of them. Boards without a matched net place exactly as before.
- **The fab clearance** shortens the legalizer's moves on the core (2.00 to 1.46 mm on average,
  parts moved over 0.5 mm 66 to 52 %) but adds vias on the hard cells and, with `WIRE`, packs the
  USB pairs too tightly to tune; it stays opt-in.
- **Line satellites**, on `line-chaser-20` at seeds 0 to 9: −82.6 [−117.0, −48.2] mm of copper and
  −11.5 vias per board against compact, 10 of 10 pass, the legalizer's mean move 2.28 to 1.15 mm;
  on `07-chaser-20-rel` (seeds 0 to 4) −67.6 mm and −8 vias, 5 of 5 pass; on `09-mcu-usb-31-rel`
  2 of 4 pass against compact's 1. The README board (seed 0): 205.5 mm and 8 vias against 268.7 mm
  and 20, every series resistor in line with its LED (none with `WIRE` + `TURN` alone).

The ladder's default stays as it was: compact placement, now with these parts, still fails the
manual `09-mcu-usb-31` lane more often than the default mode (which passes it on every seed), so
it stays opt-in.

### Conflicts and open points

- The legalizer options of `claude/s3a-legal` (scarcity order, region look-ahead, exact outline)
  are on `main` (#44); C1 adds one `only=` argument to `search()` and a flag where its stranded
  fallback is taken, so the look-ahead runs per turn. Their interaction is not measured.
- The channel model reads `default_clearance_mm` (0.4 on the ladder) where the router and KiCad
  use the fab clearance (0.2) for unclassed nets: one escaping track asks 1.05 mm between pads
  where 0.65 to 0.90 mm routes. `PNR_LEGALIZE_CHANNEL_CLEARANCE=fab` measures the change (above).
- Global placement's own weights were never tuned; with `WIRE` the legalizer re-places parts for
  wirelength (a mean move of 4.15 mm on the core), which a better global placement would leave
  less to do. The polish (A) brought the two closer by lengthening global placement's
  wirelength (+16 %) and is not adopted.

## 12. The `09-mcu-usb-31` lane: `PAIRS` and `RELAX` (2026-10-06)

Ladder v2 makes compact placement the default, which needs the manual `09-mcu-usb-31` lane (the
seven layout and constraint variants, `-header` and `-mc`) to pass with it wherever it passes
without. At `origin/main` (`82ec1a5`, GCP C4D x86-64, the lane's options: pool 8/1, 2 rounds,
packed maze; seeds 0 and 1) it passed 16 of 16 off and 11 of 16 compact (`-header` 0 of 2 off, 1
of 2 compact).

**Root cause.** All five failing cells (and every failure on seeds 2 and 3) are one of two kinds:

- **A pair out of skew** (`length_unmatched`, 1.5 to 12.6 mm). In every one, one leg of a USB pair
  carries a via pair and a detour that its partner does not: the two series resistors sit
  staggered, stacked across the legs or on either side of the connector, so one leg crosses the
  other or squeezes past a resistor through the bottom layer (a via pair alone is 2 x 1.51 mm on
  the 1.6 mm board). Without compact the same crossings happen, and the length tuner meanders the
  short leg back into the 1 mm budget (16 to 20 mm of meanders on some cells); with the courtyards
  packed at 0.01 mm there is no free cell beside the short leg for a bump, so the tuner adds
  nothing and the set is reverted. The place-route loop then stops anyway: it counted only
  unrouted nets, so a round with an unmatched pair was "converged".
- **A net unrouted** on the plane variants (`4L-SGPS`, `4L-SSGS`, `6L-SGSGPS`): every ground and
  supply pad drops a via to its plane, and the packed layout leaves the signal escapes no room.

**`PAIRS`.** The matched-length pass after legalization (`pnr.place.matched.refine_matched`) may
also turn a part on a pair or length group, or a line group's rigid macro (the `-rel` variant's
authored `usb-series` line), a quarter turn, so that the pads its legs leave from face where the
legs go. It never turns a part a hard rotation holds, nor without `orient`.

**`RELAX`: compact as far as the board routes.** In the place-route loop
(`pnr.route.feedback`) a round that leaves a signal net unrouted, or a declared pair or group
outside its budget, is not converged. Every later round runs with compact placement switched off
(`pnr.compact_flags.relaxed`, read by every part's call site) at the board's own spread, and the
first of them takes the initial pool again at the run's seed: it is the first round of the run
without compact, byte for byte (checked on `09-mcu-usb-31-rel` seed 0: `placed.json` and
`routes.json` identical). So a board that routes without compact in one round routes with it in
two, and keeps the compact layout wherever that routes. A board that does not place without
compact (the pin-1-origin header's envelope) keeps its best compact round
(`termination: relaxed_placement_failed`). `pnr-report.json` records the switch (`relaxed`:
the round, the spread, why).

**Rejected on the way** (A/B on GCP, the 8 rungs with the lane's options, seeds 0 to 3):

- the two series resistors placed as a rigid mirrored twin (an engine line group): 15 of 32
  against 21 of 32 without it, since a twin of 0805s puts its legs 2 mm apart and a twin turned
  across the legs mismatches them by that pitch;
- the declared pairs routed coupled (`board.route_pairs: coupled`): the staggered resistors give
  the coupled solver no channel (`no_coupled_channel`), both pairs fall back to legs;
- the short leg routed again, longer, through a waypoint or a via hop where meanders find no
  room: unit-tested, but on the lane's boards no such route cleared the other nets, so it never
  fired and was removed.

**Measured** (GCP C4D, x86-64; the 8 rungs of the lane run with the initial pool, seeds 0 to 3,
32 cells; `-mc` passes in every arm):

| Arm                                                     | Pass  | Rounds run without compact |
| ------------------------------------------------------- | ----- | -------------------------- |
| off (`main`)                                            | 26/32 | -                          |
| compact (`main`)                                        | 21/32 | -                          |
| compact, `RELAX` placing at the board's spread, no pool | 28/32 | 17                         |
| compact, `RELAX` (pool again), `PAIRS` off              | 31/32 | 16                         |
| compact, `RELAX` and `PAIRS`                            | 32/32 | 14                         |

Off fails `-header` on every seed (its envelope cannot be placed) and `-abs`, `-sidelock` on seed 3
(pairs out of skew). The one failure with `PAIRS` off is `-header` seed 0, whose round without
compact cannot place the header: the run stopped there, before the fallback kept its compact
round (fixed since).

The plane variants fall back on most seeds (their compact rounds leave a net unrouted); the
two-layer variants keep their compact layout on most.

**Validation** (`c5c1f5c` against `main` `586857d`, GCP C4D x86-64, seeds 0 and 1; the lane with
its options, the ladder and showcases with the pool 8/3, the 15 nightly hard rungs as the nightly
lane runs them):

| Cells                      | compact | off   | `RELAX` rounds | Placed bbox, compact vs off | Copper        | Vias      |
| -------------------------- | ------- | ----- | -------------- | --------------------------- | ------------- | --------- |
| 8 ladder cases (16)        | 16/16   | 16/16 | 0              | 6427 to 3795 mm² (-41 %)    | 2317 → 1633   | 134 → 128 |
| 4 showcases (8)            | 8/8     | 8/8   | 0              | 6641 to 3899 mm² (-41 %)    | 2460 → 1660   | 148 → 132 |
| 15 nightly hard rungs (30) | 30/30   | 30/30 | 0              | 26629 to 16935 mm² (-36 %)  | 8514 → 5787   | 821 → 775 |
| `09-mcu-usb-31` lane (18)  | 18/18   | 16/18 | 7              | 22412 to 19833 mm² (-12 %)  | 12012 → 10244 | 983 → 964 |

The bbox, copper (mm) and vias are summed over the cells that place in both arms (the lane
without `-header`, which off cannot place). Off on the branch is byte-identical to `main` on 70
of 72 cells (`placed.json`, `routes.json`, the routed board modulo UUIDs); the other two are
`-header`, which neither places (the same pool exhaustion). Compact costs 2 to 18 % more CPU
(the lane's fallback rounds).

**Power-first placement** (`PNR_POWER_FIRST=1`) no longer refuses compact (section 2).
