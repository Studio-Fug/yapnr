# Design: line groups, board edges and hierarchical PnR, animated

Status: proposed, branch `claude/animations-groups-hier` (based on `claude/ladder-animations`,
PR #15). This document extends [the animation design](animations.md); everything it does not
change (trace format rules, the critical path, encoding, determinism) stays as specified there.

## 0. Summary

The owner's request (2026-09-30), in three demos:

1. **Line group.** The five-stage chaser twice, side by side: LEDs unconstrained (the ladder's own
   `07-chaser-20`) and LEDs D1 to D5 held in one rigid line (`line-chaser-20`). The placer moves
   and rotates the whole line during optimization; the animation shows it as one rigid body.
2. **Board edges.** A small "front panel" board (`edge-io-12`: supply connector, pushbutton and LED
   on the south edge) next to its unconstrained twin (`edge-io-12-free`). The three edge parts
   slide along the edge and change order between starts.
3. **Hierarchical PnR.** A twin-bank chaser (`hier-twin-bank-31`, 31 parts, three blocks, two
   templates): each block template is placed and routed on its own board, the bank layout is
   reused for both banks, the top level places the blocks as rigid macros and then routes the few
   nets between them ("knitting"), and KiCad judges the result.

What gets built:

- **Engine (opt-in only):** a new HARD `line_group` constraint, implemented by collapsing each group
  into a rigid macro inside `place()` (reusing `pnr.hier.macro`); an opt-in `hard: true` for
  `edge_align` that keeps edge parts on their edge through legalization; an opt-in own-net mode for
  fixed copper in the detail router; a pure-Python hierarchical ladder driver
  (`regression/hier_case.py`). Designs that declare none of these run byte-identical code paths.
- **Ladder:** a `showcases()` list next to `designs()` (the eight cases and their contract stay
  as they are), `run.py --showcases`, `--trace-placement-every N`, and an independent constraint
  audit for showcase cases.
- **Traces (still `pnr-trace-v1`, additive):** member poses for rigid bodies, a `groups` field with
  the rigid bodies' own poses, an optional `constraints` list in the header, and a hierarchical
  bundle (per-template block traces plus a `blocks` event).
- **Renderer:** constraint highlighting, a rigid-body tween, a side-by-side comparison
  (`python -m pnr.animate --compare A B`), hierarchical chapters, showcase pacing.
- **Docs:** a new page `docs/constraints-and-hierarchy.md` with three animations; one more media
  item in the README (the side-by-side chaser as a GIF); manifest and results entries for
  showcases; the folder budget raised from 20 to 30 MB with the reason stated.

## 1. Scope and interpretation

- **"A human would place all of the LEDs in a row so that you can observe the chaser effect."**
  The LEDs of `07-chaser-20` (D1 to D5, one per counter output) in index order, evenly pitched,
  all turned the same way. Only the LEDs are constrained; their resistors stay free (a rigid
  LED-plus-resistor "companion" row is a follow-up, §9).
- **"How the PnR moves/rotates the group around during optimization."** The global placer's
  recorded snapshots of the group's centre and orientation, the legalizer's single step for the
  group, and the pool's montage of different group poses across starts.
- **"Side-by-side of unconstrained versus constrained."** Both halves from the same ladder run,
  engine commit, seed (0), pool configuration (8 starts, 3 finalists) and budgets; each half is
  its own critical path, synchronized by phase (§6.3).
- **"Place button/LED/connector along edge, show how they get moved around w.r.t. one another."**
  All three on the same (south) edge, so their order along the edge is the placer's choice. The
  motion within a start is sliding along the edge; the reordering shows across starts (the pool
  montage) and in a live "edge order" readout.
- **"Sub-blocks get individually PnRd, then global PnR integrates them and knits them together."**
  Per-template block synthesis (placement and detailed routing on the block's own board), then
  top-level macro placement, then top-level routing of the inter-block nets with the block copper
  held fixed, then KiCad writeback, DRC and the gate.
- **Out of scope:** changes to the eight ladder cases, their gate, budgets or animations; hard
  edge alignment of a whole line group; companion rows; flat-versus-hierarchical comparisons.

## 2. Line groups (`line_group`)

### 2.1 Schema

```yaml
line_group:
  - name: chaser_leds # required, unique among line groups
    members: [D1, D2, D3, D4, D5] # ordered literal refs (no globs), at least 2
    pitch_mm: 3.0 # centre-to-centre spacing along the line; or gap_mm, not both
    # gap_mm: 1.0      # courtyard-to-courtyard gap; default: board.default_clearance_mm
    rot: 90 # every member's rotation in the group frame (cardinal), default 0
    edge: none # none (default) | north | south | east | west: soft pull of the whole line
    reason: Chaser LEDs in one row, so the sequence reads as a line
```

The group frame: the line runs along +x in member order, centred on the group origin. The group
itself has one free position and one free cardinal rotation; members follow rigidly. A 180°
group rotation reverses the on-board direction of the sequence; that is allowed (a human would
accept either direction).

### 2.2 Validation (`pnr.constraints.compile_constraints`)

In the style of `row`; the section is parsed after `fixed`, `orientation`, `edge_align`,
`keepout`, `side`, `side_pref`, `row` and `group`, so the cross-checks see them all. Every failure
is a `ConstraintError` with the group name.

- `line_group` is a list of mappings; `name` is a non-empty unique string; `"line_group"` joins
  `known_keys`.
- `members`: at least two known, unique, literal refs; a ref belongs to at most one line group.
- A member may not be `fixed`, in a `row`, in `edge_align`, in `orientation`, in `side`, the ref of
  a ref-relative `keepout`, or a member or anchor of a HARD `group` (a soft `group` is allowed and
  pulls the whole line). These are the relations a rigid macro cannot carry faithfully
  (`pnr.hier.macro.collapse` already refuses the straddling ones at run time).
- Exactly one of `pitch_mm` and `gap_mm` (neither: `gap_mm` = `board.default_clearance_mm`); finite
  and positive; `gap_mm` at least the default clearance.
- `rot` finite and cardinal; `edge` one of `none`, `north`, `south`, `east`, `west`.
- Compiled to a HARD `Constraint` of kind `line_group` over the members, named after the group,
  with the parameters `pitch_mm`, `gap_mm`, `rot`, `edge` and `reason`.

Geometry is only known at placement time: `pnr.place.line_group.layout()` checks that
`pitch_mm >= (e_i + e_{i+1}) / 2 + clearance` for every adjacent pair, where `e` is a member's
courtyard extent along the line after `rot`, and raises a `ValueError` naming the pair otherwise.

`row` is unchanged (its `edge != "any"` still raises): extending it would collide with
`rows.violations`' edge test and with rows being interface parts.

### 2.3 Engine integration: a macro inside `place()`

**Decision: reuse the hierarchical macro mechanism, not a native rigid body in `model.py`.**

- A macro is an ordinary `Component` (courtyard = the group rectangle, pads = member pads in the
  group frame), so the unmodified global placer moves it and anneals its 4-way rotation, and the
  unmodified legalizer packs and rotates it as one rectangle. `MacroPlan.expand` poses the members
  rigidly. `hier/top.py` already runs exactly this sequence (collapse, `place()`, expand).
- A native rigid body would edit the placer's hot path for every design (bitwise reproducibility
  per platform, `model.py` notes), would understate the group's footprint while its rotation is
  uncertain (expected offsets fold toward the centre), and would still need a group-aware
  legalizer, because the per-part legalizer snaps and rotates parts one by one. More code, more
  risk, no gain.

New module `pnr/place/line_group.py` (about 250 lines):

- `layout(graph, con, clearance) -> (width, height, {ref: (x, y, rot)})`: the member poses in the
  group frame (origin bottom-left, line along +x, members centred on the line's axis), from the
  members' courtyards, `rot` and `pitch_mm` or `gap_mm`.
- `collapse(graph, constraints, rules) -> (mgraph, mcon, mrules, plan)`: builds one synthetic
  layout per group and calls `pnr.hier.macro.collapse(..., prefix="LG", margin=0.0)`. Then it
  drops the `line_group` constraints from `mcon` (otherwise the inner `place()` would collapse
  again) and adds a SOFT `edge_align` on the macro for a group with `edge` set.
- `map_starts(plan, positions, rotations)`: a group's start is the mean of its members' start
  positions and the first member's start rotation minus `rot`; `map_inflation(plan, inflation)`:
  the maximum over members; pair weights through `macro_pair_weights` (moved from `hier/top.py` to
  `hier/macro.py`, re-exported from `top.py`).
- `violations(graph, constraints) -> [refs]`: a group is broken unless its members sit at the
  layout offsets under one common pose (1e-5 mm, like `rows.violations`) with member rotations
  equal to `rot` plus the common angle, all on one side.

`pnr/place/placer.py`, at the top of `place()`, before row sampling:

```python
if any(c.kind == "line_group" for c in constraints.constraints):
    return _place_line_groups(graph, constraints, **same_keyword_arguments)
```

`_place_line_groups` computes the flat baseline HPWL, outline and pad-edge rule, collapses, maps
the start, inflation and pair weights, calls `place()` on the macro graph (which then samples rows,
applies sides, places and legalizes as today), expands with `plan.expand`, and returns
`_finish(flat, graph, constraints, ...)`, so the report checks the flat board against the original
constraints. It raises `ValueError` when `PNR_POWER_FIRST=1` (not supported) and when a member
carries a plane-access intent (the macro drops them; 2-layer showcases have none).

Adapters elsewhere (each a no-op unless a design declares a line group):

- `pnr/place/metrics.py`: `hard_violations` adds `line_group.violations` to `group_outside` (where
  rows already report), and `translation_checker`'s `legal()` checks it next to the row check. So
  every single-part move (local feedback moves, relocate, elastic, batch relocate) that would
  split a group is rejected, and the pool's rejection sees broken groups.
- `pnr/feedback/moves.py` `default_anchors` and `pnr/hier/blocks.py` (interface kinds and
  `sub_board`'s dropped kinds; `block_constraints_doc` pops `line_group`) add `line_group`.
- `pnr/place/initial_pool.py`: `_opposite_body_basins` skips line-group members.
- `pnr/hier/macro.py`: `collapse` gains `prefix="MB"` and `margin=None` (None keeps the edge
  clearance); `MacroPlan` gains `trace_rows()` (§2.5). Defaults reproduce today's behaviour.
- `hardware/pnr/BUILD.bazel`: `:pnr_place` depends on `:pnr_hier` (no cycle: `:pnr_hier` depends
  on `:pnr` only).

### 2.4 Reporting

Line-group violations are reported under the existing `group_outside` key, so `PlacementReport`,
its `summary()` string and every diagnostic record keep their shape for all designs.

### 2.5 What traces record

The placement tracer and the legalizer's `legal` event see the macro graph, so snapshots would
name `LG00`, which the flat header does not know (the renderer would drop the LEDs). Fix, in
`pnr.trace`, recording-only:

- `trace.pose_expansion(expander)`: a context manager that installs `expander` on the current
  recorder (no-op when tracing is off). While installed, `Recorder.poses()` and `Recorder.legal()`
  pass their rows through it: a macro row becomes its members' rows (member pose = macro pose plus
  the rotated member offset, member rotation plus the macro's arg-max rotation: exactly what
  `expand` produces), and the event gains `groups: [[ref, x_um, y_um, rot, side], ...]` with the
  macros' own rows. In `legal`, a macro in the accepted order becomes its members, in member order.
- `MacroPlan.trace_rows(rows)` is that expander (µm in, µm out).
- `place()` installs it around `global_place` and `legalize` for line groups; `hier/top.py`
  installs it around its `place(mgraph, ...)` call for block macros (§4.5).
- `board_header` writes an optional `constraints` list when the design has any `line_group`,
  `edge_align`, `group` or `row`: one entry per constraint with `kind`, `name`, `refs` and the
  fields that apply (`edge`, `hard`, `tolerance_um`, `pitch_um`, `gap_um`, `rot`, `anchor`,
  `radius_um`; no free-text `reason`). The eight ladder cases have none of these kinds, so their
  headers are byte-identical.
- The ladder's `--trace-placement-every N` (§5.2) makes snapshots denser (the showcases use 5; the
  default stays `ceil(iters / 24)`).

### 2.6 Tests

- `tests/test_line_group.py` (new `py_test` `line_group_test`, medium; deps `:pnr`,
  `:pnr_place`, `:pnr_hier`, `:pnr_route`, `:pnr_detail`), on synthetic graphs:
  - parser: every validation rule of §2.2, both accepted and rejected;
  - `layout`: pitch and gap geometry, `rot` 0 and 90, the too-small-pitch error;
  - `place()` on a 12-part board with a 4-member group, seeds 0 to 3: members rigid (1e-5 mm),
    group rotation cardinal, `report.legal`; a start with scattered members converges to a line;
  - `violations` and `translation_checker`: moving one member is illegal, moving none is legal;
  - `route_and_place` with the initial pool (2 starts): final placement satisfies the group;
  - `PNR_POWER_FIRST=1` and a fixed member raise.
- `tests/test_trace.py`: `pose_expansion` rows, `groups` rows, legal-order expansion, header
  `constraints` present only when declared.
- `tests/test_trace_noop.py`: a second board with a line group; results byte-identical with
  tracing unset, set, unset.
- `collapse` defaults: with default arguments it returns today's output for a two-block graph
  (in `line_group_test`; the existing `test_macro_hull.py` is not wired into Bazel).
- Ladder level: `run.py`'s constraint audit (§5.3) on `line-chaser-20`.

### 2.7 Unchanged behaviour

No ladder case declares `line_group`, hard `edge_align`, `group` or `row`, and every new branch is
guarded by the presence of such a constraint or by a new flag. Acceptance for the engine work:
an A/B run of all eight cases (§8.3) gives byte-identical `placed.json`, `routes.json` and trace
digests at the branch base and after the change.

## 3. Board edges (`edge_align`, opt-in `hard`)

Today `edge_align` is a soft quadratic pull toward the edge during global placement only. The
legalizer ignores it (a probe knocked a part off its edge in 4 of 8 starts), `hard_violations`
does not check it, and the "snaps orientation" claim in `docs/hardware/pnr-system.md` is not
implemented.

Additions (all opt-in):

```yaml
edge_align:
  SW1: { edge: south, hard: true, tolerance_mm: 1.0 }
orientation:
  SW1: 0 # the existing hard orientation constraint sets the facing; edge_align does not
```

- `hard` (bool, default false) makes the constraint HARD (the global pull is unchanged).
  `tolerance_mm` (default 1.0, at least 0.5) is the largest allowed distance from the part's
  courtyard to the named edge.
- `pnr.place.geometry.hard_edge_bands(constraints) -> {ref: (edge, tolerance)}`.
- `legalize(..., edge_bands=None)`: for a part in `edge_bands`, the candidate slots are
  intersected with the band (through the existing `box` bound of `_place_part`, per tried
  rotation: for `south`, `cy <= courtyard_h / 2 + tolerance`), and the part's inflation is capped
  at `1 + (2 * tolerance - clearance - grid) / extent_normal_to_edge` (at least 1) so the band is
  never empty because of spreading. `place()` and the pool's basin fallback pass `edge_bands`
  only when it is non-empty, so every existing call is unchanged.
- `hard_violations` and `translation_checker` report an edge part beyond its tolerance under
  `group_outside`. Feedback moves already treat `edge_align` parts as anchors.
- `docs/hardware/pnr-inputs.md` and `pnr-system.md`: document `hard` and `tolerance_mm`, and
  replace "snap orientation" with "set the facing with `orientation`".
- Tests (`tests/test_edge_hard.py`, `py_test` `edge_hard_test`): parser; for seeds 0 to 7 on the
  12-part showcase netlist (synthetic pads), every hard edge part within tolerance after
  `place()`; a soft `edge_align` behaves as before (the result equals a run without the new code
  path: `edge_bands` absent); the violation check.

## 4. Hierarchical ladder driver (`regression/hier_case.py`)

No end-to-end pure-Python hierarchical path exists today: block synthesis (`hier/synth.py`)
needs file inputs, top-level placement (`hier/top.py`) only places, copper assembly
(`hier/assemble.py`) runs only inside KiCad and leads into the native loop, and `route_and_place`
has no fixed-copper input. The driver below fills exactly these gaps with existing functions.

### 4.1 The design: `hier-twin-bank-31`

Ladder footprints only (SOIC-8, SOIC-16, 0805, the 1x02 header); 2 layers; board 56 x 40 mm;
the ladder's fab block and net classes. Each part carries an `address`; the driver copies it onto
the graph (`native.py` and the KiCad side stay untouched):

- `top.j1` J1 supply connector, `fixed` on the west edge as in every ladder case;
  `top.c_bulk` 10 µF (top-level part: a one-part module is not a block);
- `top.clock.{u, r1, r2, c1, c2, c3}`: the TLC555 astable of `timer_parts()` (RESET tied to VCC);
- `top.bank_a` and `top.bank_b`, each `{u, c, r0..r4, d0..d4}`: a CD4017B (pins as in `chaser(5)`,
  Q5 to RESET), a 100 nF bypass capacitor, five 2.2 kΩ resistors and five LEDs; both clocked by
  CLOCK. Internal nets are per bank (`QA0`, `LEDA0`, `RESETA`, ...); only VCC, GND and CLOCK cross
  block boundaries.

`extract_blocks` gives three blocks (`top.clock`, `top.bank_a`, `top.bank_b`); the banks share one
template key (same local names, footprints and internal net structure). The contract test checks
this with a pin-only graph.

### 4.2 Flow

Selected by `design["driver"] == "hier"` in `run.py` (the default `"flat"` runs `route_case.py`);
same arguments `(root, seed, rounds)`. Budgets are in `design["hier"]` and recorded in the report.

1. Load `design.json` and `source-graph.json`, set addresses, compile constraints and rules
   exactly as `route_case.py` does (including `apply_rules` for the fab profile).
2. **Block synthesis, per template.** Candidate outlines from
   `aspect_sizes(graph, rep, utilisations=(0.3, 0.4), aspects=(1.5, 1 / 1.5))`, seeds 0 and 1:
   8 trials per template. Each trial is `synth.run_trial(...)`, a new in-memory function factored
   out of `synth._trial` (whose file-based behaviour stays identical): `sub_board`, `place()`
   with 350 iterations, then per instance `instance_board` and
   `route_board(pitch=0.25, max_iters=8)`. The template's tier: the trials with the fewest
   missing connections, ordered by `synth.rank_key` (missing, port debt, area, vias, copper).
   `load_library` is not used (its rank key needs the native objective).
3. **Top level, K = 4 seeds.** `hierarchical_place()` with the in-memory tiers and 350
   iterations; illegal placements are skipped. Each macro's pose is recovered exactly from any
   member (the expansion is rigid).
4. **Knitting.** Each instance's routed copper is moved into board coordinates with the same rigid
   transform as `MacroPlan.expand`. The top-level routing graph is a copy of the flat graph in
   which (a) block-internal nets are removed (their copper is final), and (b) for every block and
   external net, one representative pad keeps the net and the block's other pads of that net are
   renamed `NET@block` and dropped from the net's pins (their block copper already joins them to
   the representative). The representative is the pad closest to the block outline (ties by ref
   and pad name), which the synthesis' port-debt term already pushes outward.
   `route_board(top_graph, ..., fixed_copper=block_copper, fixed_copper_own_net=True)` then routes
   VCC, GND and CLOCK between the representatives, J1 and `top.c_bulk`: about 10 connections.
5. **Selection.** The seed with the lexicographically smallest (missing connections, unresolved
   nets, vias, copper length) wins.
6. **Outputs**, in the shape `route_case.py` writes: `placed.json` (the flat graph, original
   nets), `routes.json` (block copper plus top-level copper, original net names, vias as
   `[net, x, y]`), `rules.json`, `pnr-report.json` (`converged` = no missing connection anywhere,
   `legal`, `unrouted`, `deferred`, `hier` = the chosen layouts, macros and seed scores). The
   runner's writeback, planes, refill, audit, DRC and via-scan stages run unchanged; KiCad's
   verdict is the gate.

### 4.3 Own-net fixed copper (`pnr/route/detail/fixed.py`)

Today fixed copper is an obstacle for every net, its own included, so a block's GND tree would
wall off its own representative pad. New keyword `own_net=False` on `reserve_fixed_copper` and
`fixed_copper_own_net=False` on `route_board`:

- with `own_net=True`, a fixed track's clearance cells are recorded in `grid.pad_net` and its
  via halo in `grid.via_halo`, owned by the track's net with the same conflict rule as
  `RouteGrid.add_pad` (foreign nets blocked, the own net passable; overlaps of two nets block
  both);
- fixed vias stay hard `via_blocked` for every net (hole-to-hole spacing applies within a net too)
  and own their track cells as above;
- with `own_net=False` the code path is today's, byte for byte.

Test (`tests/test_fixed_copper.py`, already wired as `fixed_copper_test`): a pad of net A behind a
fixed A track is reachable by A and not by B; a new A via never lands within hole spacing of a
fixed A via; the default mode's grid tables equal today's.

### 4.4 Trace bundle

The case trace (`CASE/trace`, set by `trace_native.environment()`) gains:

- `blocks/<template-id>/`: one ordinary `pnr-trace-v1` directory per template, written while
  `PNR_TRACE_DIR` points there (the recorder is keyed by directory and process; the driver
  finishes all templates before it records the top level, so each recorder is created once). Its
  header is the representative's sub-board; each trial is a `start-NN` scope of type `start` whose
  meta carries its own `outline` (µm); each instance route is a `route` scope `start-NN-<block>`;
  a `select` event `block-rank` names the chosen trial among all trials with their rank keys.
- In the case trace (header = the flat board, written by the driver's `begin_board`):
  - a scope `hier-blocks` with one `blocks` event listing, per instance, `block`, `template`,
    `trace` (the relative path `blocks/<template-id>`), the chosen `trial`, its `macro` ref,
    `size_um`, `members` (ref to `[x_um, y_um, rot, side]` in the block frame) and a `copper` blob
    (block frame);
  - one `start` scope `top-NN` per seed (macro placement, recorded with `pose_expansion`, so rows
    are member poses and `groups` holds the macro poses), then a `select` event `top-seed`;
  - a `route` scope for the winning seed that opens with a `fixed` event (the block copper in
    board coordinates and `connections_done`, the connections the block copper already
    completes, computed from the block routes), followed by the router's own events.
- `pnr/route/detail/trace_route.py`: when a route scope has a `fixed` event, its progress counts
  start at `connections_done` (absent: unchanged).

### 4.5 Engine touch points

`hier/synth.py` (`run_trial` factored out), `hier/top.py` (`pose_expansion` around its
`place()`), `hier/macro.py` (§2.3), `route/detail/fixed.py` and `router.py` (§4.3),
`route/detail/trace_route.py` (credit). All other hierarchical code is used as is.

### 4.6 Tests

`tests/test_hier_case.py` (`py_test` `hier_case_test`, medium): a synthetic 10-part design with two
identical 3-part blocks and a fixed connector, tiny budgets (2 trials, 1 seed, 80 iterations):

- the twins share a template and one chosen trial;
- block copper transformed into the board frame lands on the members' pads (rigid-transform
  check against `MacroPlan.expand`);
- representative choice is deterministic;
- union-find over pads, block copper and top-level copper connects every net's pins (the
  driver's own completeness check, which KiCad later confirms);
- the trace bundle loads (`pnr.animate` storyboard builds; §6.4).

## 5. Showcase cases and the runner

### 5.1 Cases (`designs.py`: `showcases()`, beside `designs()`)

| Case                | Parts | Board   | Driver | Constraints (besides the ladder's fab block and net classes)                           | Shown with        |
| ------------------- | ----: | ------- | ------ | -------------------------------------------------------------------------------------- | ----------------- |
| `07-chaser-20`      |    20 | 42 x 32 | flat   | the ladder case itself (J1 fixed), run in the same showcase run                        | left panel        |
| `line-chaser-20`    |    20 | 42 x 32 | flat   | `07-chaser-20` plus `line_group` D1 to D5, `pitch_mm: 3.0`, `rot: 90`                  | right panel       |
| `edge-io-12-free`   |    12 | 36 x 26 | flat   | none (nothing fixed)                                                                   | left panel        |
| `edge-io-12`        |    12 | 36 x 26 | flat   | `edge_align` south, `hard: true`, for J1, SW1, D1; `orientation` so each lies along it | right panel       |
| `hier-twin-bank-31` |    31 | 56 x 40 | hier   | J1 fixed west; addresses (§4.1)                                                        | single, chaptered |

- `line-chaser-20`: `rot: 90` turns every LED so its cathode faces one side of the line (a GND
  side) and its anode the other (the resistor side); 3.0 mm pitch leaves 0.96 mm between the 2.04
  mm courtyards. Everything else is `chaser(5)`, so the only difference to `07-chaser-20` is the
  group.
- `edge-io-12`, "hold to blink": the TLC555 astable of `timer_parts()` with RESET (pin 4) on its
  own net, SW1 from VCC to RESET, R4 100 kΩ from RESET to GND, R3 and D1 on CLOCK, C4 bulk. J1 is
  the ladder's 1x02 header (not fixed), SW1 `Button_Switch_SMD:SW_SPST_EVQPE1` (two uniquely
  numbered pads, 7.9 x 4.0 mm; footprints with repeated pad numbers are avoided because
  `refine_header` merges them). The orientations are chosen from the pad offsets so each part's
  long axis runs along the edge; the contract test checks that. The free twin drops `edge_align`
  and `orientation`.
- Names are not `NN-` prefixed, so they never sort into the ladder; `designs()` and the contract
  test of the eight cases are untouched. `designs.py` gains `LIB["button"]`.

### 5.2 Runner (`run.py`, `trace_native.py`)

- `--showcases`: case selection draws from `designs() + showcases()` (without it, only
  `designs()`, as today). A design's `driver` picks `route_case.py` or `hier_case.py`.
- `--trace-placement-every N` (with `--trace` only): `trace_native.environment()` adds
  `PNR_TRACE_PLACEMENT_EVERY=N`, and `run.json`'s config records it only when set, so default
  traced runs keep identical run documents.
- `--timeout 1200` for showcase runs (the hierarchical place-route stage takes minutes).

### 5.3 Gate and constraint audit

The gate (`acceptance()`) is unchanged and applies to every showcase case. In addition, for a
design that declares `line_group` or hard `edge_align`, `run.py` runs `constraint_reasons()`, a
stdlib check on `placed.json` independent of the engine's metrics: group members collinear at
the declared pitch in member order with the declared relative rotation (1e-3 mm);
hard edge parts' rotated courtyards within tolerance of their edge. A failure appends
`constraint_violated`; the details go to `result.json` under `constraint_audit`.

If a showcase fails the gate: the implementer may adjust only the knobs this document declares
(line group `pitch_mm` 3.0, 3.5 or 4.0 and `rot` 90 or 0; edge `tolerance_mm` 1.0 or 1.5; hier
board up to 64 x 44 mm, trials up to 3 utilisations, top seeds up to 6), never the engine, gate,
seed or pool budgets, and records every attempt in the commit body and `WORKLOG.md`. A showcase
that still fails is not committed as an animation; the docs page says so and why, with KiCad's
findings (§9 lists the fallbacks). `--allow-failed` renders are for review only.

### 5.4 CI

| Lane             | Showcases                                                                                                                                |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| pull request     | none in `ladder.yaml`; the unit tests (`line_group_test`, `edge_hard_test`, `hier_case_test`, contract, trace, animate) run in `ci.yaml` |
| nightly (`main`) | after the traced run: `--showcases --trace --trace-placement-every 5` for the four showcase cases, own 45-minute budget, informational   |
| manual           | new boolean input `showcases`                                                                                                            |

Showcases never gate: their results go to the step summary and the uploaded artifacts, and a
failure is a notice. The job timeout grows by the showcase budget only when showcases run. The
hierarchical case is estimated at 4 to 7 minutes on the Mac and 8 to 15 on the arm64 runner (never
measured there). The committed animations come from one Mac run (placement is reproducible per
platform only).

## 6. Renderer

### 6.1 Truthful animation

Every frame is recorded engine state or a labelled transition between two recorded states:

- positions between two placement snapshots: linear interpolation (existing, §5.3 of the
  animation design);
- **rigid tween (new):** a rigid body (line group or block macro, from `groups` rows) interpolates
  its centre linearly and its angle along the shorter arc, and its members are posed from that
  (never member by member, which would shrink the line mid-turn);
- **lift (new, hierarchical):** block tiles move from the block grid to their first recorded
  macro poses (a camera and layout transition, 0.8 s, captioned "blocks become macros");
- **phase holds (new, comparison):** the shorter panel holds its last frame of a phase.

Nothing else is invented: no easing of the engine's order, no reordering, no invented copper. The
docs page's "What is interpolated" note repeats this list. `docs/design/animations.md` §5.3 gains a
pointer to this section.

### 6.2 Constraint highlighting (header `constraints`)

New theme tokens (`CONSTRAINT`, `CONSTRAINT_OK`, `CONSTRAINT_BAD`, `BLOCK_OUTLINE`), used only when
the header has `constraints` (or a comparison passes a reference overlay):

- line group: a thin accent line through the member centres and a dashed rectangle around the
  rigid body; the group's name in the legend chip;
- edge alignment: the target edge drawn in the accent colour; the part's courtyard tinted; a short
  tether from the courtyard to the edge, mint within tolerance and red beyond it (live, from the
  recorded poses); an "edge order" readout (`south: J1 · D1 · SW1`, left to right from the poses);
- blocks (hierarchical): dashed block outlines posed with their macros, block copper drawn with
  the block;
- a legend chip in the caption strip lists what is highlighted.

For a trace without `constraints` the renderer's output is byte-identical to today's (the existing
`animate_test` goldens and hash checks must pass unchanged).

### 6.3 Side-by-side comparison (`pnr/animate/compare.py`)

- **Layout:** two panels of 480 px (960 px in all): one shared title strip, one caption strip per
  panel (label, constraint legend, live metric), the two boards, one footer per panel (phase,
  experiment, % routed). `Renderer.board(view, w, h)` draws each panel; `PairRenderer.frame()`
  takes a pair of views and composes them, so the encoder (duck-typed `renderer.frame(view)`) is
  reused.
- **Sync rule:** `Timeline` records scene marks (frame index and scene type) without changing its
  frames. Scenes map to phases: intro (title, source), placement (attempts, placement, montages
  and moves before the first route), routing (from the first route scene to the first native
  scene), native, end. Each phase is stretched to the longer of the two runs by holding the
  shorter run's last frame of that phase; both are resampled onto one 60 ms clock (frames are
  split into 60 ms slots, paired, and runs of identical pairs merged again). Pure and
  deterministic; unit-tested on synthetic timelines.
- **Captions:** panel labels from `--labels` or, by default, from the header: "Unconstrained" and
  the constraint legend (`line_group D1–D5 · 3.0 mm pitch`, `edge_align south (hard) J1 SW1 D1`).
  The free panel may draw the other panel's constraint as a neutral reference overlay, labelled
  "target (not constrained here)".
- **Metrics (all from recorded state):** HPWL from header pins and current poses; for the chaser,
  "LED line error": the largest distance of D1 to D5 from their least-squares line (0.00 mm for the
  group); for the edge demo, "on edge k/3" against the target edges; on the end cards, KiCad's
  verdict, vias and copper length from each run's `result` event, and HPWL.
- **Encoding:** `COMPARE_WEBP_STEPS` (960 px: quality 80, 70, 60, then 80 ms frames, then 880 and
  800 px) and `COMPARE_GIF_STEPS` (960 px: 128, 96, 64 colours, then 100 ms, then 880 px); the
  budgets stay 2.5 MB (WebP) and 5 MB (GIF).

### 6.4 Hierarchical storyboard

Triggered by a `blocks` event. `provenance.from_hier(trace)` builds the DAG (each template's trials
and its `block-rank` selection, the top seeds and `top-seed`, the route, the native stages);
`storyboard.build` emits, in order:

| Scene             | Content                                                                                                                                   | Duration |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| `title`, `source` | as today, subtitle "3 blocks, 2 templates"                                                                                                | 1.8 s    |
| `chapter`         | "1 · Blocks: each template placed and routed on its own board"                                                                            | 0.9 s    |
| `block-grid`      | one tile per template replaying its chosen trial (placement, legalization, route) from its sub-trace, synchronized by normalized progress | 6 s      |
| `montage`         | the bank template's trials with rank keys, the chosen one framed                                                                          | 2 s      |
| `reuse`           | the bank layout shown twice, "bank_a, bank_b: one layout, two instances" (from `blocks`)                                                  | 1.2 s    |
| `chapter`         | "2 · Top level: blocks become rigid macros"                                                                                               | 0.9 s    |
| `lift`            | tiles move onto the board at their first recorded macro poses (§6.1)                                                                      | 0.8 s    |
| `placement`       | the winning seed's macro placement: rigid bodies with outline and copper; legalization one macro per step                                 | 5 s      |
| `montage`         | the top seeds with their route scores                                                                                                     | 1.5 s    |
| `chapter`         | "3 · Knitting: route the nets between blocks"                                                                                             | 0.9 s    |
| `route`           | block copper drawn committed (dimmed), the top-level nets flash as they commit; the progress bar starts at `connections_done`             | 5 s      |
| `native`, `end`   | writeback, planes, refill, KiCad's verdict                                                                                                | 4.5 s    |

About 32 s (`--max-seconds 34`). Each tile has its own `Renderer` (its own header and per-scope
outline). `trace_digest` includes `blocks/**` when present (existing digests unchanged).

### 6.5 Pacing

`Timeline(..., pacing=None)`; `pacing="showcase"` sets global placement to 4 to 6 s, legalization
0.12 s per step (at most 2.4 s) and routing 3 to 6 s. The default reproduces today's frames.

### 6.6 Command line and Bazel

```text
python -m pnr.animate --compare LEFT RIGHT --out FILE [--labels A B] [--pacing showcase]
python -m pnr.animate HIER_CASE_DIR --out FILE --max-seconds 34 --pacing showcase
bazel run //hardware/pnr:ladder_animations -- --showcases [--render-only RUN_DIR [RUN_DIR ...]]
```

`animate_ladder.py --showcases` runs (or, with `--render-only`, reads) the showcase run and
renders `showcase-chaser-line.webp` and `.gif` (07 versus `line-chaser-20`),
`showcase-edge-io.webp` (free versus hard edges) and `showcase-hier-twin-bank.webp`; titles live
in `animate_ladder.py` (`SHOWCASE_TITLES`). No new Bazel targets besides the tests.

### 6.7 Determinism and budgets

Same rule as today: output bytes depend only on trace content, options and Pillow; tests render
twice and from a moved copy. Budgets: WebP 2.5 MB and GIF 5 MB per file (the hierarchical WebP may
use the encoder's steps down to 640 px; if it still exceeds 2.5 MB, a showcase-only WebP budget of
3.5 MB is added with that reason in `test_animations.py`). The folder total rises from 20 to 30 MB:
four showcase files (about 10 MB) join the 12 MB ladder set.

## 7. Documentation, README, manifest

- **New page `docs/constraints-and-hierarchy.md`** ("Constraints and hierarchy", in the Project
  toctree after the regression ladder; this design joins the design toctree): three sections
  (Line groups, Board edges, Hierarchical place and route), each with the animation, the YAML the
  case declares, what to watch for, a results row (routed, opens, findings, vias, copper, HPWL,
  time) generated from `ladder-results.json`, and the honest caveats (a 180° line is allowed;
  reordering across starts, sliding within one). A "What is interpolated" note (§6.1).
- **`docs/regression-ladder.md`**: a short "Showcases" section after the case table that links the
  new page and states that showcases are outside the gate and the PR lane.
- **README:** one more media item under the existing GIF: `showcase-chaser-line.gif` (960 px file,
  shown at `width="800"`), with a one-line caption ("Left: LEDs placed freely. Right: the same
  chaser with its LEDs held in a line group; the placer moves and turns the whole line.") linking
  the new page. Nothing else.
- **Manifest:** `animations` stays the eight ladder files plus the README GIF; a new `showcases`
  array holds, per file: `file`, `kind` (`compare` or `hier`), `cases`, `labels`, `title`,
  `format`, `bytes`, `sha256`, `width`, `height`, `frames`, `seconds`, `trace_sha256` (per case),
  `results` (per case), `settings`, `pillow` and `captions`; `readme_showcase` names the README
  file. `ladder-results.json` gains a `showcases` array (the `cases` array stays the eight).
- **`tests/unit/repo/test_animations.py`:** files must equal the union of both arrays; case
  equality and results checks stay on `animations`; showcase entries' results must match
  `ladder-results.json`'s `showcases`; widths 480 to 960 px; README must show `readme_showcase`;
  `TOTAL` 30 MB with the reason in the docstring.
- **Other docs:** `docs/hardware/pnr-inputs.md` (`line_group`, `edge_align.hard`), the orientation
  claim fix (§3), `hardware/pnr/regression/README.md` (showcases, `--showcases`, the hier
  driver), `docs/decisions.md` (the choices of §9), `WORKLOG.md`.

## 8. Implementation plan

Two implementers, sequential: A lands the engine and the runs; B builds on A's traces. Small
commits, each with its tests; messages end with the session's two trailer lines.

### 8.1 Implementer A: engine, cases, traces, runs

1. `line_group` parsing and validation (§2.2); parser tests.
2. `hier/macro.py`: `prefix`, `margin`, `trace_rows`, `macro_pair_weights` moved (re-exported).
3. `pnr/place/line_group.py`, the `place()` wrapper, metrics, interface kinds, pool basins, the
   Bazel dependency; `line_group_test`.
4. `pnr.trace`: `pose_expansion`, `groups`, header `constraints`; `trace_test`, `trace_noop_test`.
5. `edge_align.hard` (§3); `edge_hard_test`.
6. `fixed.py` own-net mode and `route_board(fixed_copper_own_net=...)`; `fixed_copper_test`.
7. `synth.run_trial`, `regression/hier_case.py`, `hier/top.py` expansion, `trace_route` credit;
   `hier_case_test`.
8. `showcases()`, `LIB["button"]`; `run.py --showcases`, `--trace-placement-every`, driver
   dispatch, `constraint_reasons`; `trace_native` config; contract tests (showcase names disjoint
   from `designs()`, pins consistent, line and edge refs exist, the twin banks share a template,
   the edge orientations align with the edge, `designs()` unchanged).
9. The A/B run (§8.3), then one showcase run (§8.3); knob iterations only as §5.3 allows.
10. `WORKLOG.md` (results, times, attempts).

A's acceptance: the unit tests above green; `prek` clean on every changed file; the A/B run shows
byte-identical `placed.json`, `routes.json` and trace digests for all eight cases; the showcase run
passes the gate and the constraint audit for all four showcase cases (or §5.3's reporting); the
run directory path handed to B.

### 8.2 Implementer B: renderer, animations, docs

1. Header `constraints` in the renderer, theme tokens, highlighting (§6.2); rigid tween and
   grouped legalization steps (§6.1); `pacing` (§6.5); `animate_test` additions (tween geometry,
   highlight off for traces without constraints, goldens unchanged).
2. `Timeline` scene marks; `compare.py` (§6.3); compare encoder steps; `--compare`, `--labels`,
   `--pacing`; tests (sync rule on synthetic timelines, determinism twice and from a moved trace,
   960 px width, metrics against hand-computed values).
3. Hierarchical: sub-trace loading, `provenance.from_hier`, the scenes of §6.4, per-tile
   renderers, `trace_digest` with `blocks/**`; tests on A's `hier_case_test` bundle.
4. `animate_ladder.py --showcases`, `SHOWCASE_TITLES`, manifest `showcases` and
   `readme_showcase`, `ladder-results.json` `showcases`.
5. Render from A's showcase run; check sizes; commit the files.
6. `test_animations.py` (§7); the docs page, ladder page section, README item, the other docs of
   §7; `docs/decisions.md`; `WORKLOG.md`.
7. `ladder.yaml`: the nightly showcase step, the `showcases` input, the timeout (§5.4).

B's acceptance: the unit and repo tests green; re-rendering any existing case whose A/B trace
digest equals the manifest's reproduces the committed file's SHA-256 (the renderer did not change
existing output); two renders of each showcase are byte-identical; all files within budget; the
docs build shows the new page; `prek run --all-files` clean.

### 8.3 Commands

Placeholders: `$PY` is the numerical Python with PyTorch the ladder already runs under (the
earlier traced run's `provenance.json`, `arguments.python`); `$K` is the headless KiCad bundle's
`Contents` directory (the regression README's example); `prek` is the repository's prek. Run from
the repository root; one ladder or KiCad run at a time, niced; never the KiCad GUI.

```sh
# Tests (Bazel capped at two CPUs on the shared machine)
nice -n 10 bazel test --config=lowmem --local_cpu_resources=2 \
  //hardware/pnr:constraints_test //hardware/pnr:line_group_test //hardware/pnr:edge_hard_test \
  //hardware/pnr:trace_test //hardware/pnr:trace_noop_test //hardware/pnr:fixed_copper_test \
  //hardware/pnr:hier_case_test //hardware/pnr:regression_contract_test \
  //hardware/pnr:initial_pool_test //hardware/pnr:animate_test //hardware/pnr:provenance_test \
  //tests/unit/repo/...

# Lint
prek run --files <changed files>
prek run --all-files          # before the last commit of each implementer

# Ladder: shared flags
KI="--python $PY --kicad-python $K/Frameworks/Python.framework/Versions/3.9/bin/python3 \
  --kicad-cli $K/MacOS/kicad-cli --library $K/SharedSupport/footprints"
POOL="--seed 0 --trace --initial-pool --initial-starts 8 --initial-finalists 3"

# A/B: the eight cases at the branch base and after the change (identical engine outputs)
# <design-commit>: the docs-only commit that adds this document (its engine is the branch base)
mkdir -p .yapnr/base-src && git archive <design-commit> hardware/pnr hardware/tools \
  | tar -x -C .yapnr/base-src
nice -n 10 $PY .yapnr/base-src/hardware/pnr/regression/run.py --repo .yapnr/base-src \
  --out .yapnr/ladder/ab-base $KI $POOL --timeout 900
nice -n 10 $PY hardware/pnr/regression/run.py --repo . --out .yapnr/ladder/ab-new \
  $KI $POOL --timeout 900
# per case: equal sha256 of placed.json and routes.json, equal trace digests (as the manifest)

# Showcases (one run)
nice -n 10 $PY hardware/pnr/regression/run.py --repo . --out .yapnr/ladder/showcase-1 \
  $KI $POOL --trace-placement-every 5 --timeout 1200 --showcases \
  --case 07-chaser-20 --case line-chaser-20 --case edge-io-12-free --case edge-io-12 \
  --case hier-twin-bank-31

# Render (B; no KiCad)
nice -n 10 bazel run --config=lowmem --local_cpu_resources=2 \
  //hardware/pnr:ladder_animations -- --showcases --render-only "$PWD/.yapnr/ladder/showcase-1"
```

## 9. Risks and fallbacks

1. **The LED line does not route on 42 x 32.** Knobs per §5.3 (pitch, `rot`); the board stays the
   same as `07-chaser-20`, or the comparison is no longer fair.
2. **Long lines over-reserve in the legalizer** (the 1.3 spread inflates the whole macro). Fine for
   five 0805 LEDs; a per-macro cap is a follow-up if a longer group needs it.
3. **Macro limits:** members top-side only, no plane-access intents, no `PNR_POWER_FIRST`, no hard
   edge for a group (soft `edge` only). Each raises a clear error; lifting them is follow-up work.
4. **Hard edge band infeasible** under feedback inflation: capped per §3; a residual
   `LegalizationError` rejects that start or round, and the report says so.
5. **Hierarchical top-level routing does not close (Option B).** Fallbacks, in order, each
   labelled in the title and on the page:
   1. retry the failing seed with the next representative pad for the failing block nets (at most
      3 candidates, deterministic order);
   2. Option A (`design["hier"]["knit"] = "full"`): blocks route internal nets only, the top level
      routes VCC, GND and CLOCK in full around the block copper; if 2 layers are too tight, a
      4-layer GND-plane variant `hier-twin-bank-31-plane`;
   3. the scoped version: hierarchical placement (blocks synthesized and placed as macros) with a
      flat top-level route of every net, titled "Hierarchical placement, flat routing".
6. **Runtime:** the hierarchical case stays out of the PR lane; nightly budget 45 minutes.
7. **Repository growth:** about 10 MB per showcase refresh; refresh deliberately, as for the ladder.
8. **Trace size:** denser snapshots and the block bundle stay well under `PNR_TRACE_MAX_MB` (64).
9. **Platform:** the committed showcase animations come from one Mac run; the manifest records it.

Owner decisions, recorded in `docs/decisions.md`: the `line_group` name and schema; `edge_align`
`hard`/`tolerance_mm`; the showcase list outside the gate; the 30 MB folder budget; the README's
second media item; that a failing showcase is not committed (default) rather than shown with a
red end card; companion rows (LED plus resistor) as a follow-up.
