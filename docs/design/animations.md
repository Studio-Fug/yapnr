# Design: the regression ladder in CI, PnR traces and critical-path animations

Status: implemented on branch `claude/ladder-animations`, based on `main` at 2b8e52d; the result
is the [Regression ladder](../regression-ladder.md) page. Where the implementation differs from
this design:

- **The fabrication profile.** On `main` at 2b8e52d the ladder left `PNR_FAB_PROFILE` unset, so
  writeback stamped the engine's default `jlc-pofv` rules into each project while the router used
  the fixtures' own rules; cases 04 to 08 then failed KiCad's gate on the via-to-SMD-pad rule
  alone. The runner now sets one profile for every stage, `legacy` by default (the fixtures' own
  rules, which the ladder README documents), and `route_case.py` applies the selected profile to
  the router's rules; all eight cases pass, and §8.4 and §9 hold. `--fab-profile jlc-pofv` runs
  the ladder under the JLCPCB profile.
- **Montage placement.** A montage follows the winner's own replay once it has reached the state
  its tiles show (the shortlist after the chosen start's legalization, the routed finalists after
  its route), not before the replay (§4.4, §4.5), so the zoom lands on the frame on screen and the
  timeline never shows routed copper at 0 %. Montages cross-fade with the board.
- **Progress bar.** The number and the bar count committed connections; negotiation fills a
  lighter bar behind them (§5.2 drew the provisional count in the bar itself, which reached 100 %
  and then dropped when commits began). The title card's backdrop is the unplaced board.
- **The gloss stage (renderer 3, 2026-10-03).** A board saved after `run.py --gloss`'s stage
  (a `native` board event with `stage: gloss`) plays as a before/after: the board before it
  with the copper the stage replaced drawn as ripped (dashed red), a cross-fade, and the board
  after it with its new copper flashed (green); the footer gives the copper length before and
  after. The changed copper is the geometric difference of the two boards
  (`pnr.animate.gloss`: track pieces no collinear track of the same layer and width covers,
  sampled every 0.1 mm), so the pass's merging of collinear pieces is not marked. A stage that
  changed no copper holds for 0.4 s ("Gloss · no copper changed"). Traces without the stage
  render as before (the timeline golden holds).
- **Draw order.** Vias are drawn after pads, as KiCad draws them (§5.2 had them before, which hid
  a via on a pad); native frames ring each KiCad DRC finding.
- **Results.** `docs/animations/ladder-results.json` (every case's gate result, with KiCad's
  findings by rule; since 2026-10-03 also the placed parts' `compactness` and the gloss stage's
  bends and length) sits next to the manifest. The manifest records the ladder run's own
  provenance (commit and dirty flag, `sources_sha256`, profile, platform, image, KiCad) apart from
  the render's, and frame counts as encoded.
- **README case.** The README shows `05-timer-led-10`, the TLC555 blinker: the request's "555
  flasher" literally (§0, §1 chose `07-chaser-20`); the chasers are the fallbacks.
- **Halving and synthesis (§4.3, §4.4).** A successive-halving run is animated in coarse mode
  (`pnr.provenance.halving_trace`): the winner's placement as one move from the source board, the
  promotion montages (placements with their rung objective, no copper ahead of the timeline), the
  winning rung's native phases and a montage of that rung's final boards. Not done: the per-block
  montages of the assembled synthesis layouts and the generation-lineage strip; a synthesis
  library alone has no board, so `--storyboard` writes its critical path, competitors and
  montages as data.
- **Encoding.** WebP uses method 4 (about 20 times faster than 6, and smaller here); the GIF
  palette keeps the theme's signal and copper-layer colours besides the median-cut entries.
- **Trace.** The legalizer's result is one `legal` event (the accepted order); a `board` event's
  DRC summary also counts findings by rule (`by_rule`) and records their positions (`findings`),
  and the ladder's `result` event has `rules`. The native snapshots' DRC uses the board's
  `.kicad_dru` too, and the copied library table is removed after it (no absolute paths).

## 0. Summary

The request: add the ramping-complexity test suite (up to the 555 flasher); add a feature that
generates animations of the place-and-route process in a straight line from zero to 100 % routed,
including all experiments along the critical path through the graph to the result; generate
animations of all ladder boards and commit them in the docs; show the 555 flasher in the README.

- **The suite.** The 8-case native ladder already in `hardware/pnr/regression/` (imported with PR2)
  is "added" by making it run anywhere (Bazel, the container image), running it in CI and
  documenting it with one animation per case. No fixture edits.
- **README sizzle.** `07-chaser-20` (Five-stage chaser: TLC555 + CD4017B, 20 parts, 2 layers) as a
  GIF near the top. Fallback, if its GIF misses the budget: `06-chaser-14`, then `05-timer-led-10`.
  The caption names the case.
- **Recorder.** `pnr.trace`: stdlib only, opt-in through `PNR_TRACE_DIR`. A trace is a directory in
  format `pnr-trace-v1`: JSON header, JSONL event streams, content-addressed geometry blobs; integer
  micrometres in the engine frame.
- **No side effect.** Unset: every hook is one hoisted environment lookup. Set or unset, results are
  identical; a test compares outputs byte for byte.
- **Native stages.** The ladder runner parses each saved `.kicad_pcb` with a stdlib S-expression
  reader (`pnr.trace_board`), no KiCad process; opens come from KiCad DRC on a copy.
- **Critical path.** `pnr.provenance`: a DAG of experiments and artifacts with _derive_ and _select_
  edges. The path is the ancestry of the final board, following only the winner through each
  selection; the losers become montage scenes.
- **Renderer.** `pnr.animate`: pure Python + Pillow. Animated WebP for the docs (800 px, at most 2.5
  MB), GIF for the README (640 px, at most 5 MB), MP4 only when `ffmpeg` exists. Viewer palette.
  Deterministic.
- **CLI and Bazel.** `python -m pnr.animate SOURCE --out ...`, `//hardware/pnr:animate`,
  `regression/run.py --trace`, `//hardware/pnr:ladder_animations`.
- **CI.** New `ladder.yaml` on `ubuntu-24.04-arm`: the ladder inside `ghcr.io/studio-fug/yapnr:edge`
  (resolved to a digest). PR: cases 01 to 06, seed 0. Nightly: all 8 cases, seeds 0 and 1, plus a
  traced run whose animations are uploaded.
- **Docs.** `docs/regression-ladder.md`; animations in `docs/animations/` (not `_static`); a
  manifest with hashes and provenance; the large-file hook excludes the folder and a repository test
  enforces a size budget instead.

## 1. Scope and interpretation

- **The suite.** "The ramping complexity test suite that we built" is the native ladder of
  `hardware/pnr/regression/` (`designs.py`, `run.py`, `RESULTS-118.md`): 2, 3, 5, 8, 10, 14, 20
  and 20 parts, the last one on four layers with a ground plane. It is in `main` already, but it
  only runs by hand on the development Mac: its defaults point at the macOS application bundle,
  and `//hardware/pnr:native_regression` is a manual target. Adding it means: runnable in the
  container image, a CI lane, a docs page, and animations. The fixtures, budgets and the
  acceptance gate stay as they are.
- **"Up to the 555 flasher."** The ladder tops out with the TLC555-clocked cases: the TLC555
  blinker (05) is the literal 555 flasher; the chasers (06 to 08) are a 555 driving a CD4017B LED
  sequence. All 8 cases get an animation. The README shows `07-chaser-20`, the densest 555 board
  on two layers; on `08-chaser-20-plane` the ground pour hides the inner-layer copper at 640 px.
- **"Straight line from zero to 100 % routed."** One linear timeline per board: it starts on the
  unplaced board (every connection open, ratsnest drawn) and ends on the saved board with KiCad's
  own DRC verdict. The timeline never branches; alternatives appear as short montages at the point
  where the engine chose between them.
- **"All experiments along the critical path."** Every experiment the final board derives from
  (the chosen start's placement, the feedback rounds up to the best round, the winner's rungs, the
  block layouts it assembled), plus, at each selection, the candidates it was chosen from (shown,
  not replayed). The formal definition is in §4.
- **Out of scope:** any change to the engine's decisions, fixtures or acceptance; the viewer
  (PR4, `yapnr/viewer`); moving code out of `hardware/` (PR3b).

## 2. What exists today

**The ladder runner** (`regression/run.py`) runs per case and seed: `generate` (KiCad Python,
`native.py make`: library footprints in a naive row, no copper) → `place-route` (numerical Python,
`route_case.py`: `route_and_place(iters=350, max_rounds=--rounds, detail_pitch_mm=.25,
detail_iters=8, spread=1.3)` with `PNR_ROUND_DIAGNOSTICS=<case>/rounds`) → `writeback` → `planes`
→ `refill` → `audit` → `drc` (`kicad-cli`) → `via-scan`. It freezes the sources, strips ambient
`PNR_*` variables, and writes `provenance.json`, `summary.json`, per-case `result.json` and
`junit.xml`. `--initial-pool` switches on the bounded initial-placement pool.

**Telemetry and diagnostics the engine already writes:**

- `pnr.live.emit` (`PNR_LIVE_DIR`): viewer events, optionally with a board copy or a layout. The
  maze router's `live_net` emits `signal_net_added`/`signal_net_removed` with provisional grid
  tracks (no vias, no pad escapes). The initial pool emits `candidate_start`/`candidate_complete`.
- `pnr.place.cost_capture` (`PNR_COST_CAPTURE_DIR`): the optimizer's last step and every
  legalizer decision.
- `pnr.phase_capture`: board snapshots with DRC in `pnr.full_iteration` (`phases/NN-name/`).
- Round diagnostics: `rounds/round-NN/` (`placed.json`, `routes.json`, `result.json`,
  `congestion.json`) and `rounds/initial-pool/<start-id>/` (`placed.json`,
  `placement-result.json`, `capacity-proxy.json`, `routes.json`, `routing-result.json`) plus
  `report.json` (shortlist, finalists, selection).

**Search provenance of the bigger runs:**

- Successive halving (`pnr.mc.halving`): `dataset.jsonl` (one record per candidate and stage:
  `place`, `screen`, `native`, `deep`, `gen-place` with `parent`, `gen`, `arm`; `seed-from`
  imports with `imported_from`), `cand/<id>/{placed.json,blocks.json,screen.json}` and a
  `pnr.full_iteration` round directory per native rung (`phases/*/diagnostic.kicad_pcb`,
  `phase.json`, `evaluation.json`, `electrical/native-loop/progress.json`).
- Hierarchical synthesis (`pnr.hier.synth_native`): `<library>/<template>/library.json` (ranked
  layouts; each instance names its native directory) and `native/<tag>/` per evaluation. The
  halving place record `hier` names, per block, the drawn layout (`seed`, size, `tier_rank`,
  `native_dir`) that `pnr.hier.assemble` copies into the top-level board.

**What is missing:** nothing records the global-placement iterations, the legalization order, the
committed routes with vias and escapes in commit order, rip-ups as they happen, or which of these
experiments lead to the final board. That is what this design adds.

**Gaps that block CI** (found while reading `run.py` against the image):

1. `run.py` writes `python-version.txt` with `python -m pip freeze`; the image's `/opt/venv` is
   built by `uv` and has no pip, so the runner fails before the first case. Fall back to
   `importlib.metadata` when pip is absent.
2. The defaults of `--python`, `--kicad-python`, `--kicad-cli` and `--library` point at the macOS
   bundle; CI passes all four explicitly (PR6a replaces the defaults with toolchain discovery).
3. `result.json`, `summary.json` and `junit.xml` hold absolute case directories; public CI
   artifacts should carry run-relative ones (`export_reviews.py` then resolves them against the
   run directory).

## 3. (a) Trace format `pnr-trace-v1`

### 3.1 Principles

- **Observational.** The engine never reads a trace; no decision depends on one.
- **Opt-in.** `PNR_TRACE_DIR` unset: each hook costs one environment lookup, hoisted out of loops
  (a `tracer` object that is `None`); the recorder module is not even imported. No hook uses a
  random number generator (Python, numpy or torch), adds a torch operation, or mutates engine
  state; ids are counters.
- **Stdlib only and 3.9-parseable,** like `pnr.live`, so future KiCad-side hooks can use it
  (listed in `pnr_kicad_srcs` then).
- **Never fails the engine.** The first recorder error disables recording for the process and
  writes `errors.json` (the same stance as cost capture: diagnostics are "unavailable, never a
  score").
- **Bounded** (§3.8), **path-free** (names and run-relative paths only, never absolute ones) and
  **deterministic** (no wall-clock value in any field the renderer reads; sorted keys; integer
  micrometres).

### 3.2 Layout

```text
<trace>/
  run.json               written by the orchestrator (ladder runner): subject, stage order, config
  header.json            board, stack-up, components with pads, nets, total connection count
  streams/<lane>.jsonl   one writer per file; events in emission order
  blobs/<sha256>.json    geometry, content-addressed (a re-added identical route costs nothing)
  errors.json            only if the recorder disabled itself
```

A lane is a writer process: `engine` (the place-route process) and `native` (the runner's native
stages). A child process gets its lane from `PNR_TRACE_LANE`; a second writer on an existing lane
file gets `<lane>.<n>.jsonl` (exclusive create), so no two processes ever append to one file.
Halving and synthesis give each evaluation its own trace directory inside its candidate or native
directory (the parent sets `PNR_TRACE_DIR` for the child); `pnr.provenance` links them (§4).

### 3.3 Frame and units

The engine frame of `pnr.graph`: millimetres, y up, origin at the outline's bottom-left corner,
stored as **integer micrometres**. Angles are degrees counter-clockwise (engine convention).
Layers are indices into `header.copper_layers`, outer to outer (`F.Cu`, `In1.Cu`, `In2.Cu`,
`B.Cu`).

### 3.4 `header.json`

Written once, by the first `trace.begin_board(graph, constraints, rules)` (called at the start of
`route_and_place`), with exclusive create; a later call checks that its component set matches.

```json
{
  "schema": "pnr-trace-v1",
  "recorder": 1,
  "outline": { "w": 30000, "h": 24000, "polygon": null },
  "copper_layers": ["F.Cu", "B.Cu"],
  "plane_layers": {},
  "components": [
    {
      "ref": "U1",
      "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
      "value": "TLC555",
      "courtyard": [7400, 5400],
      "fixed": false,
      "pads": [
        {
          "name": "1",
          "net": "GND",
          "offset": [-2475, 1905],
          "size": [1950, 600],
          "shape": "roundrect",
          "corner": 150,
          "drill": null
        }
      ]
    }
  ],
  "nets": [
    {
      "name": "GND",
      "pins": [
        ["U1", "1"],
        ["C1", "2"]
      ],
      "width": 400,
      "plane": null
    }
  ],
  "connections_total": 17
}
```

Pad shapes come from the graph (`size`, `land_corner`, `through_hole`, `drill_size`); the ladder
runner refines them (shape name, round-rect ratio, pad angle) from `source.kicad_pcb` with the
reader of §3.11. `connections_total` is the sum over nets of (pads − 1): the ratsnest edge count
of the unrouted board, the same quantity KiCad's `unconnected_items` counts down.

### 3.5 Events

Every event carries `seq` (per-stream counter), `kind`, `scope` (a path-like id such as
`initial-pool/start-05` or `round-02/route`, never a file path), `phase` (from a fixed list the
renderer maps to display text) and, where known, `progress: {done, total, source}`.

| Kind          | Fields                                                                                                                        | Emitted by                                                 |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `scope_begin` | `type` (round, attempt, pool, start, finalist, route, stage, block, candidate), `parent`, `meta`                              | hooks                                                      |
| `scope_end`   | `status` (ok, illegal, failed, dropped), `metrics`                                                                            | hooks                                                      |
| `poses`       | `stage` (source, global, legal, relocate, elastic, local-move), `iter`, `iters`, `poses` (inline up to 64 parts, else a blob) | `pnr.place.model`, `pnr.route.feedback`, initial pool      |
| `legal_step`  | `ref`, `pose`, `order`; `legal_restore` with the restored poses when the legalizer backtracks                                 | `pnr.place.legalize`                                       |
| `select`      | `among` (scope ids), `chosen`, `criterion`, `scores`                                                                          | initial pool (shortlist, finalists, chosen), feedback loop |
| `route_begin` | `pitch`, `layers`, `nets`, `plane_nets`, `deferred`, `max_iters`                                                              | `pnr.route.detail.router`                                  |
| `net`         | `net`, `op` (add, rip, commit, drop, recover), `pass`, `provisional`, `copper` (blob), `groups` (pad groups), `progress`      | `pnr.route.detail.maze` through the router context         |
| `route_end`   | `copper` (blob: the final `BoardRoute`, escapes included), `unrouted`, `deferred`, `progress`                                 | `pnr.route.detail.router`                                  |
| `congestion`  | `pitch`, `nx`, `ny`, `cells` (quantized 0 to 255), `inflation` (ref to factor)                                                | `pnr.route.feedback`                                       |
| `board`       | `stage`, `copper` (blob with zones), `poses` (from footprints), `drc` (`unconnected`, `violations`, `by_type`, `open_pairs`)  | ladder runner, `pnr.phase_capture`                         |
| `result`      | `passed`, `reasons`, `opens`, `violations`, `vias`, `copper_length_mm`                                                        | ladder runner                                              |
| `truncated`   | `bytes`, `dropped_kinds`                                                                                                      | recorder                                                   |

The sequence of `net` events with `op` in (commit, recover) and `provisional: false` is the
per-connection commit order; `add`/`rip` with `provisional: true` are the negotiation passes
(overlaps allowed), and `rip` in the final rip-up-and-reroute pass is a real rip-up.

### 3.6 Blobs

Canonical JSON (sorted keys, no spaces), named by its SHA-256:

```text
copper: {"tracks": [[layer, x0, y0, x1, y1, width]],
         "vias":   [[x, y, diameter, drill]],
         "zones":  [[layer, net, [outer ring, hole rings...]]]}
poses:  [[ref, x, y, rot, side]]
```

A net's copper blob is complete for that net at that moment: grid segments, vias (plated pad
transitions excluded exactly as `route_board` excludes them) and the pad escapes of the escape
plan (`_emit_escape` into a scratch object), so what is drawn is what the router would hand to
writeback.

### 3.7 Progress: "% routed"

`done / total` connections, with `total = connections_total` (§3.4) throughout:

- **engine steps** (`source: router`, or `router-provisional` during negotiation): per net,
  `pads − groups`, where the groups are the net's pads joined by the route's own edges (a
  union-find over the route edges, projected to pads through the escape plan's access cells).
  Nets the grid router does not route (plane nets, deferred electrical nets) count as fully open.
  This matches the router's `remaining_connections` bookkeeping.
- **native steps** (`source: kicad`): `total − len(unconnected_items)` from KiCad DRC on the
  saved board.
- **placement** (`source: none`): 0.

The bar is honest, not monotone: rip-ups make it dip. The final value is KiCad's.

### 3.8 Bounds

- Global placement: a snapshot every `ceil(iters / 24)` optimizer steps and at the last step
  (`PNR_TRACE_PLACEMENT_EVERY` overrides).
- Router detail is recorded only inside a `route` scope opened by a caller (rounds, pool
  finalists, halving screens). `route_board` calls elsewhere (relocation probes, holdout routing)
  record only a `route_end` summary, so probe storms cannot flood a trace.
- Size: `PNR_TRACE_MAX_MB` (default 64). Past half of it, negotiation passes are written as one
  keyframe per pass instead of per-net events; past 90 %, only scopes, selections, `board` and
  `result` events and one `truncated` event (10 % is reserved for them). Expected size of a
  ladder case: well under 5 MB (to be measured).

### 3.9 Hook points (all small, guarded, at stable anchors)

| File                         | Hook                                                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `pnr/route/feedback.py`      | header at entry; scope per round and placement attempt; `poses`, `congestion`, `select` of the best round  |
| `pnr/place/model.py`         | `tracer = trace.placement_tracer(...)` before the optimizer loop; `tracer.snapshot(step, pos, p)` when due |
| `pnr/place/legalize.py`      | `legal_step` after each part is placed; `legal_restore` at the backtrack restore                           |
| `pnr/place/initial_pool.py`  | scope per start and finalist; shortlist, finalist and chosen `select` events                               |
| `pnr/route/detail/router.py` | router context (grid, escape plan, net widths) around `route(...)`; `route_begin`/`route_end`              |
| `pnr/route/detail/maze.py`   | `live_net` also calls the trace hook (today it returns early unless `PNR_LIVE_DIR` is set)                 |
| `pnr/phase_capture.py`       | a `board` event from the captured board and its DRC (halving and synthesis runs)                           |
| `regression/run.py`          | `--trace` (§3.10)                                                                                          |

The displayed rotation during global placement is the arg-max of the rotation probabilities, as
cost capture does; positions are the optimizer's current `pos` (read with `detach()`).

### 3.10 Native stages in the ladder

With `--trace`, the runner sets `PNR_TRACE_DIR=<case>/trace` for the `place-route` stage only
(explicitly, like the pool flags; ambient `PNR_*` variables stay stripped), writes `run.json`, and
after `writeback`, `planes` and `refill` it:

1. copies the board, its `.kicad_pro` and `fp-lib-table` to `<case>/trace/native/<stage>/`;
2. runs `kicad-cli pcb drc` on the copy (bounded by `--timeout`), except after `refill`, where the
   runner's own final DRC report is used;
3. parses the board (§3.11) and appends a `board` event to the `native` lane.

That is two extra `kicad-cli` runs per case, sequential, never on the saved board itself. The
final `result` event repeats the acceptance fields of `result.json`. `provenance.json` records
`trace: true`.

### 3.11 The `.kicad_pcb` reader (`pnr.trace_board`)

A stdlib S-expression tokenizer that extracts: the frame (the `Edge.Cuts` extent, as
`pnr.ingest._board_frame` uses the physical contour), the net table, `segment`, `arc`
(tessellated), `via`, the `filled_polygon`s of zones, and footprints (position, side, pads with
local position, size, shape, round-rect ratio, drill). It accepts nets written as codes or as
names. It needs no KiCad process, so it runs in the controller and in CI outside the image.
Tests use small hand-written snippets; a manual `requires-kicad` test checks it against `pcbnew`
on one ladder board (coordinates within 1 micrometre, same counts, same poses as `placed.json`).

### 3.12 The no-behaviour-change guarantee

- `trace_noop_test` runs a synthetic 5-part board through `route_and_place` with detailed
  routing, with and without the initial pool, three times: tracing unset, set, unset again, and
  requires byte-identical `placed` JSON and routes (timing fields removed).
- On the ladder (development Mac and the container), a traced and an untraced run of the same
  seed must give byte-identical `placed.json`, `routes.json` and `pnr-report.json` (minus elapsed
  seconds) and equal audit and DRC metrics. Board files are compared by metrics only: writeback
  gives new items fresh UUIDs. The measured overhead goes into the commit body (AGENTS.md: new
  behaviour is default-off, with a measured A/B).

## 4. (b) Provenance and the critical path

### 4.1 Model (`pnr-provenance-v1`)

- **Nodes:** `artifact` (a pose set, a route result, a board, a DRC report), `experiment` (a
  global placement, a legalization, a detailed route, a native stage, a block evaluation) and
  `selection` (a ranking that picks one candidate). Each has a stable, path-free id (for example
  `ladder:round-02/route`, `pool:start-05/place`, `halving:p017/deep`,
  `block:<template>/<layout-tag>`), a label, a status, metrics, an order key, and either a trace
  scope or run-relative artifact paths.
- **Edges:** `derive` (input artifact to experiment, experiment to output artifact) and `select`
  (candidate to selection, selection to chosen).

### 4.2 The critical path

The critical path of a final artifact is its derivation ancestry, where a selection contributes
only its chosen input; the other inputs of a selection are that selection's _competitors_.

```python
def critical_path(dag, final):
    order, competitors, seen = [], {}, set()

    def visit(node):
        if node in seen:
            return
        seen.add(node)
        if node.kind == "selection":
            visit(node.chosen)
            competitors[node] = [c for c in node.candidates if c is not node.chosen]
        for source in sorted(dag.derive_inputs(node), key=lambda n: n.order):
            visit(source)
        order.append(node)  # post-order: inputs before their consumer

    visit(final)
    return order, competitors
```

Post-order keeps each input's whole ancestry contiguous, so independent inputs (block layouts
that an assembly consumes) become consecutive sub-sequences before their consumer, in time order.

Consequences for the ladder: round _k+1_'s placement derives from the congestion history of
rounds 1 to _k_, so every round up to the best round is on the path; rounds after the best round
are competitors of the "best round" selection (named on the end card, not replayed). Illegal
placement attempts are competitors of the round's "first legal attempt". With the pool, the
chosen start's global placement, legalization and finalist route are on the path (the winner's
finalist route is reused as round 1's route); the other starts and finalists are competitors.

### 4.3 Adapters

| Source                 | Detected by                             | Nodes come from                                                                             |
| ---------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------- |
| trace directory        | `header.json`                           | scopes, `select` and `board` events                                                         |
| ladder case            | `design.json` + `result.json`           | its `trace/`, else the round diagnostics and the saved boards (coarse mode)                 |
| ladder run             | `summary.json` + `provenance.json`      | one case (`--case`, `--seed`, default seed 0), or all cases in a batch                      |
| halving run            | `dataset.jsonl` + `cand/`               | stage records (rungs), `gen-place` parents, `seed-from` imports, the winner's `hier` choice |
| hierarchical synthesis | `library.json` per template + `native/` | ranked layouts (a selection per template), each native directory's phases                   |

**Coarse mode** animates an untraced directory from what it saved: `placed.json` poses, a round's
`routes.json` drawn net by net in file order, `phases/*/diagnostic.kicad_pcb` as native
keyframes, `evaluation.json` and DRC. The overlay then says "reconstructed from saved results".
The animator only reads its sources and refuses an output path inside one (AGENTS.md: never write
into experiment directories).

### 4.4 Storyboard (`pnr-storyboard-v1`)

`critical_path` becomes an ordered list of scenes; `pnr.animate --storyboard FILE` writes it for
inspection and golden tests.

**Ladder, baseline configuration:**

1. Title card: case title, circuit description, parts, nets, layers.
2. Source: the generated board (parts in the generator's naive row, partly outside the outline),
   full ratsnest, 0 %.
3. Round 1: failed placement attempts flash (if any); global placement (parts fly in and spread);
   legalization (parts snap to their slots one by one).
4. Round 1 routing: negotiation (provisional routes, dashed, overlaps allowed), commit passes
   (copper settles into layer colours), rip-up and reroute (red fades), recovery.
5. Per further round on the path: congestion heat overlay with the inflated parts named, the new
   placement, its routing.
6. Native: writeback (engine copper cross-fades to the KiCad board), planes (zone pour revealed
   by a wipe), refill, DRC.
7. End card: the saved board, KiCad's verdict and the metrics.

**Ladder with the initial pool** replaces step 3's first half with two montages (the 8 legal
starts with their proxy scores, the shortlisted 3 lit; then the 3 routed finalists with their
objectives, the winner lit), a zoom into the winner, and the winner's own global placement and
legalization replayed in full.

**Halving and hierarchical runs:** per assembled block, a montage of its template's ranked
layouts (chosen one lit, with its rank) and the chosen layout's native sequence on its sub-board
(at most 3 s per block; beyond 6 blocks, one combined montage); then the rung montages (place,
screen, native, deep; promoted candidates lit at each rung), the winner's generation lineage as a
strip of its ancestors' final boards, the macro placement, the assembly (block copper moves from
the sub-board into the macro pose), and the winner's native phases to its DRC verdict.

### 4.5 How competitors are shown

- A selection with at least two candidates on the path becomes a montage placed before the
  winner's replay; consecutive selections (shortlist, then finalists) form one montage group in
  stage order.
- At most 8 tiles (4 × 2): up to 7 competitors (the best by the selection's own criterion) plus
  the winner; more candidates show as a "+N more" tile.
- Each tile is the candidate's end state at that stage (legal placement, or routed copper) with
  its score in the selection's criterion; losers are dimmed to 35 %, the winner is outlined in
  the accent colour and labelled "chosen". Timing: 0.4 s in, 1.0 s hold, 0.6 s zoom into the
  winner.

## 5. (c) Renderer (`pnr.animate`)

### 5.1 Pipeline

`sources → provenance DAG → critical path → storyboard → timeline (frames with durations) →
raster frames (Pillow) → encoders`. Pure Python and Pillow (no numpy, no torch, no KiCad), so it
runs on any host through Bazel. Modules: `storyboard.py`, `timeline.py`, `render.py`,
`encode.py`, `theme.py`, `__main__.py`.

### 5.2 Visual specification

Colours are the viewer's (`yapnr/viewer/static/app.js` in PR4), copied into `theme.py` with a
comment naming the source; two new tokens are marked.

| Element                               | Colour                                         |
| ------------------------------------- | ---------------------------------------------- |
| background                            | `#10191e`                                      |
| board substrate (new)                 | `#15232a`                                      |
| outline                               | `#acc7ce`                                      |
| `F.Cu` / `In1.Cu` / `In2.Cu` / `B.Cu` | `#e89a73` / `#aa9de9` / `#dfbd62` / `#6eb6e5`  |
| zones                                 | layer colour at 15 % (the viewer's `25` alpha) |
| via ring / hole                       | `#b3c1c9` / `#15252c`                          |
| pads                                  | `#cdd2be`                                      |
| courtyards (new)                      | `#3b5561`, 1 px outline                        |
| ratsnest                              | `#8da4ba` at 33 %                              |
| new copper (2 frames)                 | `#62ffad`                                      |
| ripped copper (fade)                  | `#ff566b`, dashed                              |
| provisional (negotiation)             | `#d39af6`, dashed                              |
| chosen, accent                        | `#9ee6d1`                                      |
| congestion heat                       | `#61dbac` to `#f07769`, alpha by value         |
| text / muted text                     | `#dce5e9` / `#8a9ea7`                          |

- **Draw order:** background, substrate, zones (bottom layer first), tracks (`B.Cu`, `In2.Cu`,
  `In1.Cu`, `F.Cu`), vias, pads, courtyards, reference labels (only when at least 7 px tall),
  ratsnest, highlights, outline, overlay.
- **Ratsnest:** per net, a minimum spanning tree between its pad groups (§3.7), nearest pad pair
  per group pair. Native frames draw KiCad's `open_pairs` instead.
- **Camera:** the first scenes frame the source row and the outline together; the view eases to
  the outline (plus 4 % margin) as global placement starts. The y axis is flipped for the image.
- **Overlay:** a header with the case title (left) and "10 parts · 9 nets · 2 layers" (right); a
  footer with the phase text (left), the "% routed" bar with its number, and a small step counter
  "step 412/980" (right). The end card adds "KiCad DRC: 0 unconnected · 0 violations",
  "7 vias · 120.1 mm copper · seed 0" and the engine revision. The bar is drawn lighter during
  provisional negotiation.
- **Text is whitelisted:** titles and descriptions from the ladder's title map, reference
  designators, net names, counts, and phase strings from a fixed table. A validator rejects any
  overlay string that looks like a path (`/` between word characters, `~`, a drive letter) or an
  e-mail address; the manifest lists every overlay string, so the privacy scan sees them as text.
- **Font:** Pillow's embedded default TrueType font at a fixed size (`ImageFont.load_default(size)`,
  Aileron, CC0), so no system font is involved.

### 5.3 Timing

Frames carry their own durations, so holds cost one frame. WebP frames are 60 ms (16.7 fps); GIF
frames 80 ms (GIF delays are in 10 ms units). Motion uses ease-in-out cubic.

| Scene                            | Duration                                                                |
| -------------------------------- | ----------------------------------------------------------------------- |
| title card                       | 1.0 s                                                                   |
| source board                     | 0.8 s                                                                   |
| montage (per group)              | 1.4 s, plus 0.6 s zoom into the winner                                  |
| failed placement attempt         | 0.3 s each, at most 3                                                   |
| global placement                 | 2.0 to 3.0 s, positions interpolated between snapshots                  |
| legalization                     | 0.08 s per part, at most 1.6 s                                          |
| routing (one route scope)        | 3 to 8 s, log-scaled in its event count; negotiation at most 40 % of it |
| congestion between rounds        | 0.8 s                                                                   |
| native writeback, planes, refill | 0.6 s, 1.2 s, 0.4 s                                                     |
| DRC verdict and end hold         | 2.5 s                                                                   |

Routing events are batched per frame (`k = ceil(events / frames)`); a committed net flashes for
two frames, a ripped net fades over three. Expected lengths: about 8 to 10 s for the small cases
and 18 to 22 s for the chasers; `--max-seconds` (default 22) scales scenes down proportionally.
Rigid bodies (line groups, block macros), the hierarchical lift and the holds of a side-by-side
comparison are specified in [the constraint and hierarchy design](constraint-and-hier-animations.md),
section 6.1, which lists everything a frame may interpolate.

### 5.4 Outputs and budgets

| Output         | Width  | Budget | Encoder settings (first try)                                        |
| -------------- | ------ | ------ | ------------------------------------------------------------------- |
| docs WebP      | 800 px | 2.5 MB | lossy, quality 80, method 6, `minimize_size`, loop 0                |
| README GIF     | 640 px | 5 MB   | one fixed 128-colour palette for all frames, no dithering, loop 0   |
| MP4 (optional) | 800 px | none   | only if `ffmpeg` is on `PATH`: H.264, yuv420p, CRF 23, `+faststart` |

Frames are drawn at twice the size and reduced with a Lanczos filter (Pillow draws without
anti-aliasing). If an output exceeds its budget, the encoder steps down deterministically: WebP
quality 80, 70, 60, then a longer frame interval, then width 720, 640; GIF 128, 96, 64 colours,
then 100 ms frames, then width 560. The chosen settings are recorded in the manifest; an output
still over budget is an error. `ffmpeg` runs with a deadline (`pnr.proc`).

### 5.5 Determinism

The output bytes are a function of the trace content, the options and the Pillow version: no
time, no environment, no path, iteration always in sorted order, a fixed palette and font. Tests
render twice, and from a copy of the trace at another path, and compare hashes. Across platforms
the output is expected, not promised, to be identical (the Pillow wheels bundle their own
FreeType, zlib and libwebp); the manifest records the Pillow version. The engine results
themselves are reproducible per platform only (docs/decisions.md), which is why the manifest
records the platform and image the ladder ran on.

### 5.6 Performance

A static layer (substrate, outline, courtyards of fixed parts) is cached; the copper layer is
redrawn only when copper changed; placement frames redraw parts only. Target: all 8 ladder cases
rendered in a few minutes on one core (to be measured).

## 6. (d) Command line and Bazel

```text
python -m pnr.animate SOURCE [SOURCE ...] --out PATH
  SOURCE              trace dir, ladder case or run dir, halving out dir, or synthesis library dir
  --out PATH          a file (.webp, .gif, .mp4) or a directory (<label>.<ext> per --format)
  --format LIST       webp,gif (default webp); mp4 only with ffmpeg, else a warning
  --case NAME --seed N   pick a case of a ladder run
  --width, --max-seconds, --budget-mb, --gif-budget-mb
  --title, --subtitle validated overlay text (no paths)
  --storyboard FILE   write the storyboard JSON and stop
  --manifest FILE     add or update this animation's entry
  --coarse            allow reconstruction from saved results when there is no trace
```

Bazel (`hardware/pnr/BUILD.bazel`):

- `pnr/trace.py`, `pnr/trace_board.py` and `pnr/provenance.py` join `:pnr` through its existing
  `pnr/*.py` glob;
- `py_library(name = "pnr_animate")` over `pnr/animate/*.py` (without `__main__.py`), depending
  on `:pnr` and `requirement("pillow")` only (no torch, so building it fetches no torch wheel);
- `py_binary(name = "animate")`;
- `py_binary(name = "ladder_animations")` for `regression/animate_ladder.py`, tagged `manual` and
  `requires-kicad`: it runs `run.py` with `--trace`, `--initial-pool`, `--initial-starts 8`,
  `--initial-finalists 3` and `--seed 0` into `<repo>/.yapnr/ladder/<run-id>` (git-ignored),
  renders every case into `docs/animations/<case>.webp`, the README GIF, and `manifest.json`.
  With `--render-only RUN_DIR` it skips the ladder, so no KiCad is needed. The case titles
  ("TLC555 blinker", ...) live in this script, so `designs.py` and the fixtures stay unchanged.

**The animations use the initial pool** (`--initial-pool`): the pool is where the ladder's search
happens (the montages of §4.4), and RESULTS-118 measured it as a quality gain on two cases. The
baseline ladder stays the regression gate. A case that fails in pool mode is rendered from the
baseline configuration instead, and the manifest records the configuration per case.

`regression/run.py` gains `--trace` (§3.10), the pip-less version listing, run-relative case
directories, and nothing else. Pillow joins `requirements.in` under a new "Tooling" section with
a major-version ceiling (for example `pillow>=12,<13`, whatever major is current at
implementation), locked with `bazel run //:requirements.update` (`MODULE.bazel.lock` committed),
and pinned in `docs/decisions.md`. It does **not** join `requirements-runtime.in`: the renderer
is a contributor and docs tool, so the image and its runtime locks do not change (when a
`yapnr animate` command ships in the image, Pillow moves to the runtime set).

New files under `hardware/` are outside prek's scope until PR3a; they are written black, isort
and flake8 clean at 100 columns from the start (checked by hand with those tools), so PR3a's
reformatting is a no-op for them.

## 7. (e) CI

### 7.1 Workflow `ladder.yaml`

- **Triggers:** every pull request (a plan step skips the ladder unless engine inputs changed:
  `hardware/pnr/`, `hardware/tools/scan_via_proximity.py`, `requirements*`, `docker/`, the
  workflow), a nightly `schedule` on `main`, and `workflow_dispatch` (inputs: cases, seeds,
  animate). A final `ladder` job always reports, as `image.yaml` does, so the check can become
  required later without a path filter leaving it pending.
- **Permissions:** `contents: read`, `packages: read`. Concurrency per PR, cancelling superseded
  PR runs.
- **Runner:** `ubuntu-24.04-arm` (the image's arm64 build; the same architecture as the `test`
  job).

### 7.2 The image and the runtime guard

1. `docker/login-action` (pinned by commit, as in `image.yaml`) with `GITHUB_TOKEN`. While the
   package is private, the repository needs read access to it (packages pushed by this
   repository's workflows are linked to it; otherwise Package settings, Manage Actions access).
   Once the owner makes the packages public, the login stays harmless. Fork PRs skip the ladder
   until then.
2. Resolve `ghcr.io/studio-fug/yapnr:edge` to a digest and use the digest (recorded in the step
   summary and the manifest).
3. **Runtime guard.** `edge` is built from `main`; a PR can change the runtime pins. A step
   compares the image's `/opt/venv` distributions with the checkout's
   `docker/yapnr/runtime-arm64.lock`. If they differ, it creates an overlay venv inside the
   container from the image's own CPython (`/opt/python`) and installs the checkout's lock with
   `pip --require-hashes --no-deps`: the same packages the PR's image would get, in a minute or
   two, without building an image. This is the simplest reliable option; building the app image
   in the job (Bazel wheel plus `docker buildx`) was rejected as slow for no gain, since the
   ladder runs the checkout's engine sources, not the image's wheel.
4. If `docker/yapnr-kicad/` changed (a new KiCad base that is not published yet), the ladder is
   skipped with a notice: `image.yaml` smoke-tests the new base on the PR, and the nightly run on
   `main` covers the ladder after the merge.

### 7.3 Running the ladder in the container

```sh
timeout --kill-after=60 50m docker run --rm --init \
  -u "$(id -u):$(id -g)" -v "$PWD":/work -w /work --shm-size 1g \
  --entrypoint /usr/local/bin/yapnr-kicad-env "$IMAGE" \
  "$PY" hardware/pnr/regression/run.py --repo /work --out /work/out/ladder \
    --python "$PY" --kicad-python /usr/bin/python3 --kicad-cli /usr/bin/kicad-cli \
    --library /usr/share/kicad/footprints --timeout 600 $CASES $SEEDS
```

`$PY` is `/opt/venv/bin/python` or the overlay venv's. `yapnr-kicad-env` gives the runner's UID a
writable home with KiCad's library tables. The KiCad-side stages run under the image's KiCad
Python (3.12) from the frozen sources on `PYTHONPATH`, as the runner already arranges.

### 7.4 What runs when

| Event            | Cases and seeds                                                              | Job timeout |
| ---------------- | ---------------------------------------------------------------------------- | ----------- |
| pull request     | 01 to 06, seed 0, baseline (about 1 min on the Mac; estimated 3 to 6 min)    | 45 min      |
| nightly (`main`) | all 8, seeds 0 and 1, baseline; then all 8, seed 0, `--initial-pool --trace` | 150 min     |
| manual           | as given; `animate: true` adds the traced run and the animations             | 150 min     |

The PR subset is revisited after the first CI timings (all 8 cases at seed 0 took about 3 min on
the Mac, so the full seed-0 ladder may fit a PR too).

### 7.5 Reporting and artifacts

- A stdlib script (`tools/ci/ladder_summary.py`) turns `summary.json` into the step summary: case,
  seed, pass, opens, violations, vias, copper, seconds, and the failure reasons. `junit.xml` is
  uploaded as is (no third-party reporter action needed).
- `if: always()` uploads `ladder-results` (summary, junit, per-case `result.json`, `drc.json`,
  stage logs; boards of failed cases) for 14 days. Case directories in these files are
  run-relative (§2).
- Timeouts at three levels: `run.py --timeout` per stage, `timeout` around `docker run` (so the
  upload still runs after a hang), and the job's `timeout-minutes`.
- **Animations** (nightly and `animate: true`): a second job on `ubuntu-24.04-arm` sets up Bazel
  with the same caches as `ci.yaml`, downloads the traced run, runs
  `bazel run //hardware/pnr:ladder_animations -- --render-only ...`, uploads `ladder-animations`,
  and compares the trace hashes with `docs/animations/manifest.json`; a drift is a notice, never
  a failure. Refreshing the committed animations stays a deliberate change (§8.4).
- The lane is informational at first; after about a week of green nightly runs the owner can make
  `ladder` a required check.

## 8. (f) Documentation

### 8.1 Files

```text
docs/animations/<case>.webp     one per ladder case (8)
docs/animations/07-chaser-20.gif README sizzle
docs/animations/manifest.json   provenance, hashes, sizes, settings, overlay strings
docs/regression-ladder.md       the "Regression ladder" page
```

The folder is `docs/animations/`, not `docs/_static/animations/`: `docs/build_docs.py` stages only
Markdown from `docs/`, and its GitHub Pages step rewrites every `_static` reference in the HTML,
which would break raw `<img>` paths. `build_docs.py` gets one more step: copy `docs/animations/`
to `_extra/docs/animations/`, which `html_extra_path` places at `docs/animations/` in the site.
Then the same relative paths work on GitHub and on the site, for `README.md` (staged at the site
root) and for `docs/regression-ladder.md`. Both use raw HTML `<img>` inside a `<p>` or `<figure>`
(not a standalone `<img>`, which MyST's `html_image` would turn into a Sphinx image node).

### 8.2 The page and the README

- `docs/regression-ladder.md`: what the ladder tests and its gate (from the regression README),
  how to run it (Mac with the headless KiCad copy, container, Bazel), the CI lanes, how to read
  an animation (colours, bar, montages), then one section per case: its animation, a caption
  from the manifest (parts, nets, layers, vias, copper, KiCad result, seed, configuration, engine
  revision, platform). Linked from `docs/index.md` ("Start here") and the "Project" toctree, and
  from `hardware/pnr/regression/README.md`.
- `README.md`, right after the first paragraph under the title:

```text
<p align="center">
  <img src="docs/animations/07-chaser-20.gif" width="640"
    alt="yapnr placing and routing a TLC555 + CD4017B LED chaser, from an unplaced board
    to a KiCad-DRC-clean result">
</p>
<p align="center"><sub>TLC555 + CD4017B five-stage LED chaser (20 parts), from the
  <a href="docs/regression-ladder.md">regression ladder</a>: initial placement pool,
  placement, routing and KiCad DRC.</sub></p>
```

### 8.3 Size budget instead of the large-file hook

- `.pre-commit-config.yaml`: `check-added-large-files` gets
  `exclude: ^(third_party/|docs/animations/)`.
- `tests/unit/repo/test_animations.py` (a repository check, stdlib only, tagged like the others)
  enforces: every file in `docs/animations/` is in the manifest with a matching SHA-256 and size;
  extensions are `.webp`, `.gif` and `.json` only; WebP at most 2.5 MB, GIF at most 5 MB, folder
  total at most 20 MB; widths between 480 and 960 px (read from the file headers); no EXIF, XMP or
  GIF comment chunks; every ladder case (from `designs.py`) has an animation; the README and the
  page reference only files that exist.

Manifest shape:

```json
{
  "schema": "yapnr-animations-v1",
  "generated": {
    "date": "2026-10-01",
    "engine_revision": "<commit>",
    "renderer": 1,
    "pillow": "<version>",
    "platform": "linux-aarch64",
    "image": "ghcr.io/studio-fug/yapnr@sha256:<digest>",
    "kicad": "10.0.6"
  },
  "animations": [
    {
      "case": "05-timer-led-10",
      "title": "TLC555 blinker",
      "seed": 0,
      "config": ["--initial-pool", "--initial-starts", "8", "--initial-finalists", "3"],
      "file": "05-timer-led-10.webp",
      "bytes": 812345,
      "sha256": "<hash>",
      "width": 800,
      "frames": 240,
      "seconds": 16.2,
      "trace_sha256": "<hash>",
      "result": { "passed": true, "opens": 0, "violations": 0, "vias": 7, "copper_mm": 120.1 },
      "settings": { "quality": 80, "frame_ms": 60 },
      "captions": ["TLC555 blinker", "10 parts · 9 nets · 2 layers", "Global placement"]
    }
  ],
  "readme": { "case": "07-chaser-20", "file": "07-chaser-20.gif" }
}
```

### 8.4 Where the committed animations come from, and how often they change

- Preferred: the ladder in the published arm64 image (locally with colima, or the nightly
  artifact), so CI can reproduce them; the manifest records the image digest and platform.
  Fallback: the development Mac (headless KiCad copy, the experiment runtime), recorded as
  `darwin-arm64`.
- Only boards that pass KiCad's gate are committed; the renderer refuses to write into
  `docs/animations/` for a failed case unless `--allow-failed`, and then the end card says FAIL.
- Each refresh adds about 10 to 16 MB to the history. Refresh deliberately (a notable engine
  change, or a release), never on every PR; the total budget bounds each refresh. Git LFS and
  Pages-only hosting were considered and rejected for now: the request is to commit them, LFS
  complicates the Pages checkout and the README.

## 9. Tests and acceptance

| Test                                  | Tier and size           | Checks                                                                                                                |
| ------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `trace_test`                          | unit, small             | format, bounds and truncation, self-disable on error, no absolute paths, determinism                                  |
| `trace_noop_test`                     | unit, medium            | byte-identical engine outputs with tracing unset, set, unset (§3.12)                                                  |
| `trace_board_test`                    | unit, small             | reader on snippets: frame, segments, arcs, vias, zones, both net syntaxes                                             |
| `trace_board_kicad_test`              | manual, requires-kicad  | parity with `pcbnew` on a ladder board                                                                                |
| `provenance_test`                     | unit, small             | critical path and competitors on synthetic ladder, pool, halving and synthesis dirs                                   |
| `animate_test`                        | unit, medium            | storyboard golden, frame count, identical bytes twice and from a moved trace, budgets, no metadata, overlay validator |
| `regression_contract_test` (extended) | unit, small             | `--trace` parsing, run-relative directories, pip-less version listing                                                 |
| `tests/unit/repo/test_animations.py`  | repo check              | §8.3                                                                                                                  |
| ladder A/B, traced against untraced   | manual (Mac, container) | §3.12; result and overhead in the commit body                                                                         |

Acceptance: all 8 cases pass the unchanged gate in the traced run; 8 WebP files and the README
GIF within budget; `bazel test //...` and `prek run --all-files` green; the docs build shows the
page and the README animation; the CI lane green on a PR and on a manual nightly run.

## 10. Implementation order and coordination

Small commits, each with its tests:

1. `pnr.trace` and the engine hooks (§3.9); `trace_test`, `trace_noop_test`; A/B in the body.
2. `pnr.trace_board`; `run.py --trace`, the pip-less listing, run-relative directories.
3. `pnr.provenance` and the storyboard; `provenance_test`.
4. Pillow in `requirements.in` and the lock; `pnr.animate`; `:animate`; `animate_test`;
   `docs/decisions.md` pin.
5. `regression/animate_ladder.py` and `:ladder_animations`.
6. `ladder.yaml`, `tools/ci/ladder_summary.py`, the CI section of `DEVELOPERS.md`.
7. Docs: `build_docs.py` step, `docs/regression-ladder.md`, index and toctree (this design
   document too), README sizzle, hook exclusion, `test_animations.py`, the animations and the
   manifest.
8. `WORKLOG.md` and `docs/decisions.md` (Pillow pin, animations policy, the CI lane).

- **PR3a** (format and lint of `hardware/pnr`): engine edits are insertions of two to six lines at
  stable anchors in six files (`feedback.py`, `model.py`, `legalize.py`, `initial_pool.py`,
  `router.py`, `maze.py`) plus `phase_capture.py` and `run.py`; rebasing onto PR3a means
  re-inserting them into the reformatted files. Landing after PR3a is preferred.
- **PR4** (viewer): nothing under `yapnr/viewer` changes; the palette is copied. A later viewer
  could replay `pnr-trace-v1`.
- **PR6a** replaces `run.py`'s macOS defaults with toolchain discovery; this change keeps passing
  explicit paths. **PR3b** moves the new modules with the rest of `pnr`.
- **On the shared Mac:** the ladder runs niced and sequentially (one KiCad process at a time,
  within the two-process budget), only through the headless KiCad copy; Bazel with
  `--config=lowmem`; the image, if used through colima, needs about 2.3 GB of disk.

## 11. Risks and open questions for the owner

1. **Pool configuration for the animations** (§6): they show the search, but not the exact
   regression configuration. The alternative is baseline-only animations with no montages for
   most cases.
2. **Repository growth** from committed binaries (§8.4), about 10 to 16 MB per refresh.
3. **Platform of record:** the arm64 image (reproducible by CI) or the Mac. Boards differ between
   platforms by design (decisions: placement is reproducible per platform only).
4. **README format:** GIF, because GitHub autoplays it everywhere; WebP only on the docs site.
5. **Pad fidelity:** shapes from the graph refined from the source board, not KiCad's exact pad
   polygons (the viewer's `pcbnew` extraction), to keep KiCad processes out of rendering.
6. **Halving and synthesis adapters** are built and tested on synthetic directories. Real
   Splanc-shaped runs depend on Splanc paths in `pnr.mc.halving` and `pnr.hier.native_block`
   (PR3c); coarse mode can animate them read-only in the meantime.
7. **Private image:** fork PRs and anyone without package access cannot run the lane until the
   packages are public (WORKLOG, next step 5).
