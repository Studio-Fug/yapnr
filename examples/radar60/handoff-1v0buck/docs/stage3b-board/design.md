<!-- markdownlint-disable -->

# radar60 stage 3b (board): design

2026-10-04. Inputs: `stage3a/integration.md` (the "What blocks a complete Rev A" list, §7 items
2–8), `board-design.md` D14/D15, `rf-uniform/REPORT.md` ("Numbers stage 3 must take"),
`stage3a/fixes/radar/` (trial 2: `trial2.py`, `work/`), yapnr `origin/main` a976f7a (PR #44 in),
branches `claude/radar60` ab57757, `claude/radar60-parts` 6c5743a and `claude/radar60-rfuni`
49be425. [D] means derived here; [E] means an estimate; [V] means checked in the code or files
while writing this design.

Scope: (A) public yapnr engine features, opt-in, with byte-identical output when they are not
declared. (B) the radar integration on the local branch `claude/radar60-3b`. The RF macro stays an
input (`fixed_block`), so stage 3c can swap in macro v2 (D14 PA feed, D15 stitching, column
retune) mechanically. Legalizer and GP code are out of scope: `claude/legalize` owns them.

---

## 0. Root causes found while designing (they change what must be built)

1. **Hole-to-edge (2 findings) is an outline-stroke mismatch, not a router error [V].** `pnr.writeback`
   (writeback.py:1144) replaces the board's rounded 0.05 mm Edge.Cuts outline with a rectangle
   drawn at a **0.15 mm** stroke. The board's DRU (`gen_board.py` line 723:
   `dru_text(..., 0.05)`) sets the hole-to-edge limit at 0.475 mm, which assumes a 0.05 mm stroke.
   The router keeps 0.525 mm from the outline centreline, and the physical rule (0.50 mm) holds.
   KiCad, however, measures to the stroke edge: 0.625 − 0.1 − 0.075 = 0.45 mm. The 1 mm corner
   radii are lost on the same step.
2. **Maze class clearance [V].** The halo router sizes `net_halo` and `via_keepout` from the fab
   clearance only (router.py:869–880). The exact-separation model is already class-aware
   (exact_route.py:73–102), but it runs only as recovery. That accounts for all 15 maze-level
   findings: PWR 0.15 mm, XTAL 0.15 mm.
3. **FID1 [V].** The pad's own `(clearance 0.6)` and `(solder_mask_margin 0.5)` are not ingested
   (`graph.Pad` has no field for them). One fix covers the 7 clearance findings and the 7 mask
   bridges: 0.5 + 0.6 = 1.1 mm reaches past the 1.0 mm aperture.
4. **A failed ball blocks its net [V].** route/detail/fanout.py adds the net to `blocked_nets` in
   three places: failed rows, no access cell, and an access cell lost to another fanout's tail.
   router.py:1084/1111 then drops the whole net.
5. **Bounding-box planes [V].** `stack.plane_regions` gives every non-largest net on a shared plane
   layer its pad bounding box plus 2 mm. U1's 1V2 balls (G15, H15, N11, P14, P15, R6) and 1V8 balls
   (B11, B12, D15, F5, K5, R9) span the whole package, so their boxes overlap and the smaller one
   wins. In trial 2, In3 was not a power layer, and the supplies were 0.25 mm traces
   (107 mΩ on 1V0_RF1).
6. **LVDS cannot pass the DRU on the route_board path [V].** `route_board` routes each leg as its
   own net and then tunes length (router.py:1226). The board's DRU checks `diff_pair_gap`
   0.16–0.22 mm and `diff_pair_uncoupled` ≤ 3 mm. The coupled solver
   (`route/detail/coupled.solve_pair`, pure geometry) runs only in the native loop, and neither
   `paired_bootstrap` nor `native_loop` knows declared fanouts, fixed blocks or v1 keepouts.
   **Feature A8 was added for this reason.**
7. **R46 (`radio.r_pa`, 0402 0 Ω jumper, C17168) carries the PA current.** Its annotation is 2.5 A
   peak. A thick-film 0402 jumper is typically specified at ≤ 50 mΩ and about 1 A [E]: 125 mV at
   2.5 A would break the 1.0 V window. This is for the owner (§6).

---

## 1. Engine features (track branches off `origin/main`, integrated on `claude/radar-routing-engine-2`)

Each feature is a declared input. Without it, the rules carry no new key, the graph JSON gets no
new field (`to_dict` pops None, as it already does for `body`/`stack`), and the output is
byte-identical.

### A1. Class clearance in the maze router, with an exact check (track `s3b-clear`)

- **Current paths:**
  - `router.route_board` computes `net_halo` with `_track_halo(w, track, clearance_fab, pitch)`
    and a global `via_keepout`.
  - `maze._footprint`/`_route_impl._h(net)` reserve cells.
  - `dense_maze` builds per-net fields.
  - `exact_route` (`PNR_EXACT_SEPARATION=recover`) is pairwise class-aware.
  - Stage 3a already made the fanout hand-over and the joint escape/drop checks class-aware.
- **Inputs:** `net_class[*].clearance_mm` (already compiled into `rules.net_classes`, then
  `grid.net_clearances`); a new `board.class_clearance: maze` gives `rules.class_clearance = "maze"`.
- **Algorithm:**
  - Let c_n = max(fab, class_n).
  - Track halo: h_n = ⌈(w_n/2 + c_n + w_sig/2)/p⌉ − 1.
  - Per-net via keepout: k_n = max(k_fab, ⌈(2 r_via + c_n)/p⌉ − 1). It enters `_footprint`, and the
    via cost window in `_astar`/`span_cost`. `_footprint` already takes a per-net halo, so this
    is a per-net radius in place of the global one.
  - Footprints of different nets stay disjoint, so a pair keeps max(c_a, c_b) as KiCad requires.
  - The remaining case is track against via of a wider class: (k_b + 1)p ≥ w_a/2 + max(c) + r_via.
    It is caught by the **exact check**: after `route()`, the exact model's pairwise kinds run
    over the committed routes (reusing `exact_route`'s zone builder). Offending nets are ripped up
    and rerouted by the existing exact-separation recovery.
  - The packed and native kernels get the halos through the per-net dense fields (no C ABI change).
- **Tests:** unit tests for the halo radius per class, via-to-via at PWR 0.15, track-to-via
  (reproduces the 0.118 mm 1V0_RF1/I2C_SCL case), the exact check flagging a halo-legal but
  class-illegal pair, and identity with the flag off. Rung `…-classes` (§1.9).

### A2. Pad-local clearance and mask margin (track `s3b-clear`)

- **Data:**
  - `graph.Pad.clearance_mm` and `Pad.mask_margin_mm` (Optional; only when the pad or its
    footprint sets them).
  - `ingest` reads `pad.GetLocalClearance()` (falling back to the footprint's) and the
    `solder_mask_margin`.
- **Algorithm:**
  - `grid.add_pad` (two-tier halo) uses max(c_fab, c_class(net), c_pad) for the pad's track and
    via halos.
  - Mask bridge: another net's copper must stay outside the aperture, so the keep-away is also at
    least the mask margin + 1 µm.
  - Netless pads such as fiducials are included. Escape and fanout checks (`_clear_of_pads`)
    take the same per-pad value.
- **Flag:** none. The field is absent on boards without local values, so the output is identical.
  Boards with local values change only where they were DRC-wrong.
- **Tests:** ingest of a fiducial (1 mm, 0.6, 0.5); grid halo; escape refusal; KiCad DRC on the
  rung (clearance and `solder_mask_bridge` both 0).

### A3. The board's custom DRC rules where they constrain routing (track `s3b-clear`)

- **Current paths:**
  - `ingest._no_track_layers` reads only one-layer `disallow track` rules.
  - `grid.layer_mask` exists, but only for current classes (`current_layer_mask`).
- **New `pnr/dru_rules.py` (stdlib):**
  - It parses `(rule …)` blocks and supports this subset of conditions: `A/B.Type`,
    `A/B.hasNetclass('X')`, `A/B.NetClass ==`, `A/B.NetName ==`, `A.Layer ==`, `&&`, `||`, `!`
    and parentheses.
  - It maps rules onto the existing mechanisms:
    - `disallow via` for a class or net becomes a single-layer `layer_mask` (no layer changes).
      This covers `radar60_xtal_no_vias`, the "crystal" rule.
    - `disallow track` on layers for a class becomes a `layer_mask` of the remaining layers.
      This covers `radar60_lvds_outer_layers`.
    - `clearance` with A/B class conditions becomes a class-pair table: SW–XTAL 8 mm and SW–RF 5 mm.
      Pairs ≤ 1 mm feed A1's exact check. Larger pairs become keep-away discs around the other
      class's pads, both ways, plus the exact check.
    - `length max` becomes a per-net bound reported by the route (QSPI 25 mm).
  - Every other rule is listed in `dru.unmodelled` with its name, never silently dropped. Area
    rules (`intersectsArea`, `intersectsCourtyard`) are covered by copper keepouts and via classes.
- **Flag:** `board.dru_routing: true`. `staged_signal` and `route_case` read the DRU beside the
  board and merge it into `rules["dru"]`; only `route_board` reads it.
- **Tests:** a parser fixture of radar60.kicad_dru (a public file), giving the expected masks,
  pairs and unmodelled list; unsupported syntax gives a warning and no effect; a maze test where
  a no-via net never changes layer; the rung's DRU.

### A4. Board edge as KiCad judges it; hole-to-edge for maze vias (track `s3b-clear`)

- **Current paths:**
  - `grid.block_edge_inset_split` uses insets on the bounding rectangle measured from the
    centreline.
  - writeback stamps a rectangle at 0.15 mm.
  - `fab_profile.dru_text` derives the KiCad limit from `edge_stroke_mm`.
- **Change:**
  1. A `board.keep_outline: true` writeback option keeps the source Edge.Cuts items (arcs and
     stroke) when they bound the placement region, instead of stamping a rectangle.
  2. The router's edge model is the board's own contour (the KiCad-side export already writes
     `fixed.json`; it gains `edges`: segments and arcs plus the stroke).
     - Vias are blocked where dist(cell, contour) − stroke/2 − drill/2 < limit_dru + 1 µm
       (the Oracle margin).
     - Tracks are blocked where the copper-to-edge test fails.
     - `limit_dru` comes from one function shared with `dru_text`.
  3. `outline_size` stays the bounding box.
- **Flag:** `board.edge: exact` (it implies `keep_outline`).
- **Tests:** a rounded corner (a via at a corner diagonal), a 0.15 mm stroke against the 0.05 mm
  DRU, a notch; KiCad DRC `hole_to_edge` = 0 on the rung.

### A5. Partial fanout (track `s3b-fanout`)

- **Data:** the fanout entry key `partial: true | {bridge: true}` (added to `fanout/spec.KEYS`).
- **Algorithm:**
  - In `plan_fanouts`, with `partial`, a failed ball no longer adds its net to `blocked_nets`
    (failed row, no access, or access lost to a tail).
  - It becomes a failure site, its copper is released (as stage 3a does), and the ball is taken
    out of `skip_pads` so `plan_escapes` gets a second try with the board's generic escapes.
  - **Bridge:** for a drop or plane ball whose plan failed, the planner tries a surface stub to an
    adjacent ball of the same net (0.65 mm orthogonal or 0.92 mm diagonal), judged exactly
    against foreign pads with class clearance. At 0.65 mm pitch a 0.10 mm track between
    0.32 mm lands needs 0.30 of the 0.33 mm gap.
    - Radar cases: 1V2 P14→P15 and G15/H15, 1V8 B11/B12; A2/B2 belong to D14.
  - The net routes among its remaining terminals. The report lists `partial: {ball: reason}`.
- **Tests:** unit tests (a ball with no site: off means the net is blocked, as on main; on means
  the rest is routed and one site is reported; bridge; an access cell lost to a tail). Rung
  `…-partial` (§1.9).

### A6. Per-rail inner-layer zones from the rail's balls and pads (track `s3b-power`)

- **Current paths:**
  - `stack.plane_regions`: the largest net takes the whole outline, the others take bounding
    boxes.
  - `PlaneAccess` keeps drops inside the regions.
  - `writeback.form_planes(regions=…)` draws them.
- **Inputs:** `plane_partition: [{layer: In3.Cu, nets: [...], order: current, split_gap_mm: 0.3,
min_width_mm: 1.0, fill: GND, core_no_vias: true}]`, a power-typed layer, the U1 fanout plan's
  drop vias, fixed copper, keepouts, and the `@pnr-current` currents (or class `current_a`).
- **Algorithm (new `pnr/plane_partition.py`, numpy):**
  1. Raster the layer at h = 0.1 mm inside the outline minus the edge clearance. Block antipads of
     foreign through copper: macro fence vias, fanout vias of other nets, PTH holes and keepouts on
     the layer.
  2. Terminals: each pad's drop disc (pad centre, reach 0.8 mm) or the planned drop via.
  3. Take nets in order: peak current, then terminal count, then name.
  4. For each net, build a Steiner tree (KMB, Dijkstra on free cells, a penalty inside other nets'
     terminal discs) and claim it dilated to w_net/2 + gap/2. The width is
     w_net = max(min_width, IPC-2221 internal width for the current (`electrical.current_width`),
     R□·L_tree/R_share), where R_share comes from A7's budget less the vias.
  5. An unreachable terminal becomes a partial: the pad falls back to a drop-less failure site, as
     with plane_fallback_drops.
  6. Grow the territories round-robin (multi-source BFS) into free cells, keeping the split gap;
     what is left becomes `fill` (GND) or stays empty.
  7. Polygonize: cell boundaries become polygons with holes, collinear points are merged, and the
     result is checked for one 4-connected component per net, terminals inside, and a trunk core
     ≥ w_net (distance transform).
  8. With `core_no_vias`, foreign maze vias may not land in a trunk core (`grid.via_blocked` on
     those cells), so a via line cannot cut a neck.
  9. The result is cached by input sha (as `fanout.cached_plan`). `plane_regions` returns it as
     drawn regions; the route result carries it to writeback.
- **Tests:** two interleaved rails both connected; a blocked terminal reported; the width honoured;
  determinism; KiCad zones fill with no overlap. Rung `…-rails`.

### A7. IR-drop report per rail (track `s3b-power`)

- **Inputs:** `ir_drop: [{net, sources: ["@pmic.fb_rf1:2"], sinks: {"@radio.u1": [G5,H5,J5]},
current_a (default: the source's @pnr-current peak), split: equal|area, budget_mohm |
budget_mv, temperature_c: 25}]`.
  - Copper thickness per layer comes from the stackup record. Via plating is 0.020 mm (fab
    profile key, IPC class 2 average). ρ = 1.72e-5 Ω·mm × (1 + 0.00393 (T − 20)).
- **Extract (KiCad Python, stdlib, `pnr/ir_extract.py`):** for each rail net, the filled zone
  polygons per layer, the tracks and arcs (with width), the vias (drill and span), the pads
  (shape, layers) and the stackup z positions, as JSON. Fixed-block copper is included, so 3c's
  macro PA feed is counted.
- **Solve (numpy, `pnr/ir_drop.py`):**
  - Zones and pads are rasterized at h = 0.05 mm per layer. Neighbouring copper cells are joined
    by G = t/ρ (one square); a partially covered edge is scaled.
  - Tracks are exact 1-D edges ρL/(wt), joined to the raster cell under each end point or to other
    track end points, including T joins (the trial-2 logic).
  - A via is a node chain over its span, each segment ρΔz/(π(d+t)t), tied to the cell under its
    pad on each layer.
  - The source pad cells are held at V = 0. Each sink injects its share spread over its pad cells.
  - Solve with Jacobi-preconditioned CG (stencil matvec, `bincount` for the sparse extras) to a
    relative residual of 1e-10. Union-find runs first, so an unconnected sink is an "open", not a
    number.
- **Output:** `ir.json` per rail, with:
  - the drop per sink in mV;
  - R_eff = worst drop / I;
  - the two-point R per sink, with other sinks open (comparable to trial 2's path measure);
  - the I²R loss;
  - the maximum current density per layer (A/mm), its location, and a neck flag against the
    IPC internal width;
  - pass or fail against the budget.
    The report also gives an optional In3 heat-map PNG (stdlib zlib writer) and warnings in the
    "quantified assumption" form (ERC assumptions flow).
- **Flag:** the `ir_drop` section. It is a report; it gates only with `hard: true`.
- **Tests (analytic):**
  - a track gives ρL/(wt) within 0.1 %;
  - a strip with bus bars gives R□L/W within 1 %;
  - a via barrel matches the formula;
  - two parallel paths give half the resistance;
  - an annulus gives R□ ln(b/a)/2π within 3 %;
  - a tree network equals the trial-2 Dijkstra;
  - an island is an open;
  - a KiCad extract of a small board with a filled zone.

### A8. Coupled pairs from declared fanout exits in route_board (track `s3b-fanout`, needed for LVDS)

- **Inputs:** `rules.diff_pairs` (from constraints or `@pnr-pair`), `rules.fanouts`, and
  `route_pairs: coupled`.
- **Algorithm:**
  - Run after `plan_fanouts` and before `plan_escapes`/maze.
  - For each declared pair, the end terminals are:
    - at a fanned-out ball, the fanout escape's exit point (layer, neck width), with the escape
      length counted as uncoupled;
    - at a plain pad, the pad.
  - `coupled.solve_pair` runs on the exit layer (F.Cu), with static obstacles from the grid's
    pads, the fixed copper, the escape copper, v1 keepouts and A1/A3 clearances. Its parameters
    are `max_uncoupled_head`, set to the DRU's 3 mm outside the courtyard minus the fanout escape
    outside the courtyard, and the annotation's 1.0 mm where it is stricter.
  - The result is committed as escape copper (its cells owned, like fanout copper), followed by
    the existing length tune for skew. A pair it cannot solve falls back to leg routing and is
    reported.
- **Tests:** a 2-ball pair from a BGA exit to a connector (gap and uncoupled checked exactly); the
  rung `09-mcu-usb` with the flag (A/B against today's skew failures).

### 1.9 Hard-rung extensions (`regression/hard_rungs.py`; all on `ufbga_fanout()` 6L-SGSGPS)

| Rung                                   | Adds                                                                                                                                                                                                                                                                                                                         | Checks                                                                                              |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `11-ufbga201-fanout-6L-SGSGPS-classes` | classes: `supply` 0.15 mm, `clk` 0.20 mm on two ring-0 GPIO nets with DRU `disallow via` and a CLK↔default 0.25 mm pair rule; FID1 (1 mm pad, 0.6 local, 0.5 mask) in an exit corridor; 1 mm corner radii at 0.15 mm stroke with J3/J4 within 1 mm of the edge; flags `class_clearance: maze`, `dru_routing`, `edge: exact` | KiCad DRC 0 (clearance, mask bridge, hole_to_edge); no via on CLK nets; escape and via_class checks |
| `…-partial`                            | `reserved` areas remove all sites of one VCC ball that has a same-net neighbour (bridged), one VCC ball without one, and the exit of one 3-terminal net's ball (NRST: U1, C10, a test pad); `partial: {bridge: true}`                                                                                                        | unconnected exactly at the 2 designed balls; the rest routed; DRC 0                                 |
| `…-rails`                              | VCC split into VDD / VDDA+VREF+ / VBAT, each from its own J5 pin; In4 (P) carries all three; `plane_partition` and `ir_drop` with budgets [D]                                                                                                                                                                                | one zone per rail, no overlap, every rail pad connected, IR within budget, DRC 0                    |

### 1.10 Engine regression plan

- **Identity (flags absent):**
  - Control: `origin/main` at the integration base. Candidate: the `claude/radar-routing-engine-2`
    head.
  - Cells: gate 8×2, showcases 4×2, every hard rung ×2, all with `--compact --gloss` (as stage 3a).
  - Pass: placed, routes and rules byte-identical; boards equal modulo gloss segment direction;
    pass/fail equal.
- **New rungs:** 3 × 2 seeds pass. Each feature commit carries an A/B on its rung (flag on vs off:
  DRC findings, opens, CPU), per AGENTS.md.
- **GCP:**
  - `yapnr exp`, both regions configured.
  - Each comparison is pinned to one family: c4d-highcpu-8 Spot in us-west4, same image digest for
    control and candidate. If C4D has no Spot quota, both arms re-run on C4 in Montreal; arms
    never mix.
  - Campaigns: control about $0.91 ceiling, candidate about $0.97, at most 3 re-checks of about
    $0.10. **Ceiling ≈ $2.2 of the $6 cap**, every submit < $5, each logged in `gcp-spend.md`
    before submit and after fetch. Fetch without `--full` unless a diff needs it.
- **Mac:** unit and KiCad tests directly. Radar runs only through `gapfix/heavy.sh`.
- **Bazel (before each push):**
  - `bazel test --local_cpu_resources=2 --config=lowmem` on the new and changed targets:
    `class_clearance_test`, `pad_clearance_test`, `dru_rules_test`, `edge_model_test`,
    `fanout_test` (partial, bridge), `pair_fanout_test`, `plane_partition_test`, `ir_drop_test`,
    `hard_rungs_test`, contract tests.
  - Modules are flat in `pnr/` (covered by the `:pnr` glob). `ir_extract.py` and `dru_rules.py`
    are added to `pnr_kicad_srcs`. Tests list `:pnr`, `:pnr_route`, `:pnr_place` and
    `requirement("numpy")` explicitly (the CI failures came from missing deps).
- prek is clean on every commit.

### 1.11 Tracks, merge order, conflicts

- **Worktrees:** `yapnr-wt/s3b-clear` (A1–A4), `s3b-fanout` (A5, A8) and `s3b-power` (A6, A7), all
  off `origin/main`.
- **Merge order** into `claude/radar-routing-engine-2`: clear, then fanout, then power.
- All three touch `route_board`. Each adds one-line hooks into its own module, as the stage-3a
  legal track did. `writeback.py` is touched by A4 (outline) and A6 (regions).
- **`claude/legalize`:** it touches `place/*`, `legalize*.py`, `BUILD.bazel`,
  `docs/hardware/pnr-inputs.md` and `regression/run.py`. None of the place code is ours. The
  `BUILD.bazel` and pnr-inputs edits are appends in separate sections; we do not touch `run.py`.
- One PR is opened from the integration branch; it is never merged by us.

---

## 2. Radar integration (`claude/radar60-3b`, local only)

### 2.1 Branch and merges

- **Merge order:**
  1. `claude/radar60` ab57757;
  2. merge `claude/radar60-parts` (3 commits: schematic, parts lock, J2 and flash lands);
  3. merge `claude/radar60-rfuni` (14 commits: macro generator, rfm1-m/n/p, floorplan 47.35, gen
     constraints, `rf_audit.py`, openEMS);
  4. merge `origin/main`.
     [V]: `git merge-tree` is clean at every step, and parts and rfuni share no file.
- **Trial engine:** a scratch detached worktree, `claude/radar-routing-engine-2` + `radar60-3b`.
- **Semantic conflicts to fix (none is textual):**
  1. `reva/placement.json` is for the 46.3 mm board with the old J2 land → re-place.
  2. `merge_macro` hard-codes 7 columns and rejects any other footprint → §2.2.
  3. `MACRO_NETS` lacks RXD0/RXD5/TXD0/TXD4.
  4. RT1–RT4 are in no schematic or BOM.
  5. `integrate.py` lacks trial 2's inputs: the v1 keepouts, `fixed_block rfm1` (group RFM1_MACRO,
     anchor @radio.u1, `solid_layers [In2.Cu]`), `plane_fallback_drops: false`, the fanout entry,
     and the PLANE_In1 cut out of the RF region (482.6 mm²). They move from `trial2.py` into
     `integrate.py compile`/`finish`.
  6. `lvds_header_fixed` (pose and courtyard) predates the parts track's J2 (rows ±3.086 mm).
  7. Flash EP (Macronix): no vias or traces under it, so an F.Cu track and via keepout over the EP.

### 2.2 `merge_macro` (kicad_ops.py)

- **Columns:** read from `record["columns"]` (11 in rfm1-n: 7 active and 4 `dummy: true`); no
  fixed 7.
  - Dummy column pads join RFM1 numbered `rxd0/rxd5/txd0/txd4`, on board-only nets
    `RF_RXD0/RF_RXD5/RF_TXD0/RF_TXD4` (class RF).
- **Loads:** each `R_0201_0603Metric_LOAD` becomes a real footprint RTn at the record's
  `loads[*].centre` in U1's frame, locked, in group RFM1_MACRO, and assembled (not
  `exclude_from_bom`).
  - The schematic gains `rf_macro.rt[0..3]` (a 50 Ω 0201 thin-film part with a verified LCSC
    number, under the parts-track rule).
  - `source()` skips them as it skips RFM1, so the macro owns the pose and land, and the
    schematic owns BOM and address.
  - S1 (open-end dummies only) still needs no code: the counts come from the record.
- **Mask islands:** the RFM1_MASK polygons are copied as they are (the islands are the gaps).
  `finish` checks that the islands contain the loads: x ±0.78 mm, y 33.968–35.738 (RX) and
  32.080–33.850 (TX).
- **R1 v2:** the digest covers tracks, arcs and vias as before, plus the RTn pads and the mask
  polygons, and `record.geometry_sha256` must equal the RFM1 field.
- **Tests:** a unit test on the rfm1-n fixture (counts 11/4, nets, digest equal, an unknown
  footprint still raises).

### 2.3 Floorplan numbers (from rfuni's `floorplan.yaml` and the record's `board_frame`)

- Board 60 × 47.35 mm, 1 mm corner radii; mounting holes at (3.5, 3.5), (56.5, 3.5), (3.5, 43.85)
  and (56.5, 43.85); radome lands at (3.5, 27) and (56.5, 27).
- RF region: x 12.599–51.469, y 26.7–47.35 (four rectangles), pocket 29.557–31.25 × 33.2–34.2; R4
  guard x 7.599–56.469.
- Cut-outs: RX 16.599–32.205 × 36.688–43.024, TX 34.205–47.469 × 34.800–41.136.
- Phase centres (R6 to 1 µm): RX y 39.988, x 20.666/23.008/25.350/27.692; TX y 38.100,
  x 38.718/41.060/43.402. Dummies: RXD0 18.324, RXD5 30.034, TXD0 36.376, TXD4 45.744.
- Loads: RT1 (19.495, 35.018), RT2 (31.205, 35.018), RT3 (35.205, 33.130), RT4 (44.573, 33.130).

### 2.4 Placement constraints (re-place; mechanical MC selection)

- **Fanout exit bands [D]:**
  - `integrate.py` plans U1's fanout (`pnr.fanout.cached_plan` on fixed U1 plus the macro copper)
    and turns every surface exit into a strip 0.5 mm wide, running 2.5 mm out from U1's courtyard.
  - Adjacent strips are merged into placement `keepout` polygons (`exit_south`, `exit_west`).
  - A strip may contain only a slot whose part sits on that ball's own net path (qspi_series at
    R12). Any other anchored slot that meets another net's strip is an input error, and
    `integrate.py` refuses it by name.
  - This clears the 7 no-access balls (E15, J14/J15, K14/K15, L14/L15).
- **South-edge order, east to west (U1 rot 270: row k at x = 30.55 − 0.65k, column c at
  y = 28 + 0.65(8 − c)) [D]:**

  | Balls               | x          |
  | ------------------- | ---------- |
  | XTAL B15/C15        | 29.9/29.25 |
  | SPIA D13, E13–E15   | 28.6/27.95 |
  | 1V2 G15/H15 (drops) | 26.65/26.0 |
  | LVDS J–M14/15       | 25.35–23.4 |

  - Y1 and its load caps move east of the SPIA strip: Y1 region **x 28.9–32.9**, y 15.75–21.55
    (8.1 mm to the switch region at x ≥ 41).
  - The caps' slots are re-derived beside B15/C15 at x ≥ 28.9. Today's `xtal_cap_n` slot
    (26.85–28.9) blocks E15's exit, which is the trial-2 no-access ball.

- **J2 and J3:**
  - J2 changes from fixed to `region` x 8.0–34.0, y 7.25–15.6. This bounds the 21.09 × 8.09 mm
    courtyard: the stage-2 pose ±2.6 mm in x, under the crystal region.
  - J3 gets the region x 0.6–8.3, y 8–24.3.
  - The main legalizer options `legalize: {outline: exact, order: scarcity, lookahead: regions}`
    gave 27/32 legal starts in stage 3a.
- **Pad-anchored groups (`anchor_pad`, in main):**
  - Damping: `r_damp_rf1` → `pmic.fb_rf1:2`, `r_damp_rf2` → `pmic.fb_rf2_a:2`, `r_damp_1v8` →
    `pmic.fb_1v8:2`, each within 3 mm; each `c_damp_*` → its R's pad 2, within 2 mm.
  - Snubbers: `r_snb*` → the inductor's SW pad, within 3.5 mm (3.0 gave 8/32).
  - Rail ferrites (from the IR budget, §2.7): `fb_rf1` → `U1:H5` within 12 mm; `fb_rf2_a/b` →
    `U1:D2` within 10 mm [D].
- **Bottom sites:**
  - The fanout's `bottom_sites` for C57, C61, C64, C63, C38 and the LDO caps (as in trial 2).
  - The PA corner parts (C54, R46, C60) keep their bottom regions until macro v2 puts its pocket
    vias in (3c re-derives their sites).

### 2.5 Power stage U2/U5: neck authorization (chosen)

- **What changes:** add `"neck_max_length_mm": 0.6` to the existing `@pnr-current` lines on
  `pmic.u2` (the SW and VIN pads) and on `power_in.efuse` pad 6 (5V_SYS). The engine path already
  exists: `electrical.neck_budget`, `pad_entry.neck_witness`, `pad_entry_neck.repair_neck`.
- **Numbers [D]:** a 0.25 × 0.6 mm neck on 0.035 mm outer copper (`neck_budget`'s 1 oz model) is
  1.2 mΩ: 3.3 mV at 2.8 A peak and 1.2 mW at 1 A rms, against the fab's `neck_loss_budget_w`
  and `neck_peak_drop_v`.
- **5V_SYS:** it becomes an In3 partition rail, so its drop leaves the eFuse pad through the
  authorized neck, not at the pad edge.
- **Fallback (3c):** a generated fixed copper block (U2 + L1–L4 + CIN) as a hierarchical subcell,
  only if the necked routes fail DRC or the hot-loop audit.

### 2.6 Routing plan (6 layers) and 8-layer criteria

| Layer     | Use                                                                                                                                                                                                                                 |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| L1 F.Cu   | fanout rings 0–1; **LVDS** (coupled 0.17/0.18 mm, 100 Ω over In1; layer mask F.Cu only, because B.Cu over the 0.20 mm FR-4 core is not 100 Ω at that geometry); **XTAL** (F.Cu only, no vias: A3); QSPI first choice; local signals |
| L2 In1.Cu | GND plane, no tracks (macro In1 inside the RF region)                                                                                                                                                                               |
| L3 In2.Cu | ring-2 dog-bone escapes, QSPI overflow, I2C/UART/CAN/SPIA/control; macro GND inside the RF region                                                                                                                                   |
| L4 In3.Cu | **power partition** (A6): 1V0_RF1, 1V0_RF2, 1V2, 1V8, 3V3_RADIO_IO, 3V3, 5V_SYS (1V0_PA from 3c); remainder GND                                                                                                                     |
| L5 In4.Cu | GND, solid                                                                                                                                                                                                                          |
| L6 B.Cu   | dog-bone landings, bottom decoupling (BGA shadow), signals, GND pour                                                                                                                                                                |

- **Flow:**
  1. finish (macro, group);
  2. compile (all §1 flags);
  3. sites;
  4. MC re-place (32 starts, stage 0);
  5. route the **top 4** candidates in `staged_signal`: fanout → A8 LVDS pairs → maze (QSPI first)
     → partition and drops → tune;
  6. refill, DRC, IR and audits;
  7. pick the winner mechanically: DRC errors, then opens, then IR passes, then length.
- **8 layers** if, on all 4 routed candidates, any of these holds:
  - (a) an LVDS pair cannot be coupled on F.Cu within gap 0.16–0.22 mm and ≤ 3 mm uncoupled;
  - (b) more than 2 signal nets stay open with their failure sites in the escape or south corridor
    (capacity, not input errors);
  - (c) a 1.0 V rail misses 4 mΩ with the partition at maximum territory;
  - (d) a QSPI net needs more than 25 mm.
- **8-layer caveat [D]:** an 8-layer stack must stay ≤ 1.2 mm thick, or the 0.15 mm drills
  (interstitial GND, RF fence) exceed aspect ratio 8. L1–L3 stay unchanged, so the macro does not
  move.

### 2.7 IR budget per rail (copper only, source pad → worst ball, A7 at the annotated peak, 60 °C)

The rule is 1 % of nominal [D]. For the 1.0 V branches it is the owner's 4 mΩ (10 mV at 2.5 A).

| Rail              | Source → sinks                      | I peak | Budget                                              |
| ----------------- | ----------------------------------- | ------ | --------------------------------------------------- |
| 1V0_BUCK + 1V0_SH | L_b2:2 → R_SH → FB inputs           | 2.5 A  | ≤ 1 mΩ together [D]                                 |
| 1V0_RF1           | FB3:2 (fb_rf1) → G5, H5, J5         | 2.5 A  | **≤ 4 mΩ**                                          |
| 1V0_RF2           | FB4:2 + FB5:2 → C2, D2, R46:1       | 2.5 A  | ≤ 4 mΩ                                              |
| 1V0_PA            | R46:2 → macro feed → A2, B2         | 2.5 A  | ≤ 4 mΩ with RF2 (3c; R46 reported separately, §0.7) |
| 1V2               | FB1:2 → G15, H15, N11, P14, P15, R6 | 1.0 A  | ≤ 12 mΩ                                             |
| 1V8               | FB2:2 → B11, B12, D15, F5, K5, R9   | 0.85 A | ≤ 21 mΩ                                             |
| 3V3_RADIO_IO      | U6:6 → F15, R10                     | 0.2 A  | ≤ 165 mΩ                                            |
| 3V3               | L_b0:2 → loads                      | 0.2 A  | ≤ 165 mΩ                                            |
| 5V_SYS            | U5:6 → U2 VIN pads, U6              | 1.5 A  | ≤ 33 mΩ                                             |

Feasibility for 4 mΩ [D]:

- Vias: 3 ball vias (0.20 mm drill, about 0.75 mm to In3, 0.93 mΩ each) plus 4 at FB3, about
  0.55 mΩ.
- Stubs: about 0.33 mΩ.
- In3 plane at 1 oz, R□ = 0.49 mΩ (0.57 at 60 °C): ≤ 5.5 squares, i.e. about 15 mm of path at
  ≥ 2.7 mm width.

The DC plan (review_calc §5) allows only 3 mV of copper. 4 mΩ meets that only if a branch carries
≤ 0.75 A, so the report gives mV both at the annotation and at a branch split (§6).

### 2.8 Pass marks (stage 3b)

- **R1:** macro digest v2 equal. **R2:** no foreign track, via or fill in the RF region on
  F.Cu–In2, judged on full shapes. **R4:** guard clean; SW ≥ 8 mm from XTAL and ≥ 5 mm from RF.
  **R6:** to 1 µm. **rf_audit A1–A6:** pass.
- **KiCad 10 headless DRC with the board's DRU:** 0 findings of clearance, hole*clearance,
  hole_to_edge, items_not_allowed, solder_mask_bridge, courtyards_overlap, track_width,
  via_diameter, diff_pair*\* or length. `lib_footprint_issues` may not exceed the baseline.
- **Unconnected:** only A2/B2 (1V0_PA, pending macro v2), so ≤ 2.
- **Fanout:** 37/37 Rev A signal balls; every supply ball connected except A2/B2.
- **LVDS:** 4 pairs to J2, intra-pair skew ≤ 0.1 mm, group skew ≤ 2.0 mm (KiCad lengths).
- **QSPI:** 7 nets, each ≤ 25 mm.
- **IR:** every rail within §2.7, no islands, CG residual ≤ 1e-10.
- **Placement:** legal; the audit passes (anchored radii, exit bands empty, sites).
- **Renders:** top, bottom and angled, plus an In3 partition map and IR heat maps. They are sent
  as they land and archived in `progress-gallery`.
- Anything short of the mark is reported ranked, with failure sites.

### 2.9 Stage 3c swap

1. Point `--macro` at macro v2.
2. Run `gen_board.py --macro` (floorplan from `board_frame`).
3. Run finish.
4. Re-derive the bottom sites from the pocket vias.
5. Re-route.

Placement re-runs only if the region or pocket changed.

---

## 3. Spend and runs

- Engine: §1.10 (ceiling about $2.2 of $6).
- Radar: Mac only (heavy.sh): 32-start placement about 15–20 min, 4 routes about 7–10 min each [E].

## 4. For the owner (recommendations in bold)

1. **R46 / `radio.r_pa`:** an 0402 0 Ω jumper carrying up to 2.5 A (≤ 50 mΩ, about 1 A rating [E]).
   **Replace it with the WFCP0612 1 mΩ (as R_SH's product fit), or make it a copper link in the
   macro v2 PA feed.**
2. **IR budget:** 4 mΩ (10 mV at 2.5 A) against the DC plan's 3 mV copper term. **Keep 4 mΩ per
   branch, and confirm the RF1 / RF2+PA current split at B4.**
3. **RT1–RT4 as schematic and BOM parts (LCSC to be verified).** The input-side dummy decision
   (S1) is still open; it is code-neutral.
4. **The 8-layer fallback must stay ≤ 1.2 mm** (0.15 mm drills).
