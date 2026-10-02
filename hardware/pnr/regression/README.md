# Native PnR regression ladder

A circuit manifest becomes an unrouted native KiCad board using real library
footprints. The ordinary `pnr.ingest`, `route_and_place`, `writeback` and `planes`
stages run, followed by a fresh native KiCad DRC on the exact saved result. No
pre-routed geometry, hand-selected final component locations, DRC exclusions,
expected failures or mocked routing are used. Only the supply connector is fixed.
This tests the PnR backend from native circuit inputs, not atopile compilation or
SPICE behavior. The fixtures are small engineering test circuits, not qualified
manufacturing designs.

| Case | Parts | Added difficulty |
| --- | ---: | --- |
| Connector + LED | 2 | Basic connection; supply must be externally current limited |
| Resistor + LED | 3 | Movable series current limiter |
| Two LEDs | 5 | Shared, branched supply and return |
| Inverter indicators | 8 | SOT-23-5 pin escapes, an intentionally unused pad, 3-pin connector |
| TLC555 blinker | 10 | 8-pin IC, RC timing and control, bypass and bulk capacitors |
| Two-stage chaser | 14 | TLC555 + CD4017B, cross-IC clock/reset, fanout |
| Five-stage chaser | 20 | Five LED/resistor outputs, shared rails and dense routing |
| Five-stage chaser with plane | 20 | Four copper layers, ground plane attachment/refill |

Signal width is 0.25 mm, supply/return width at least 0.4 mm, clearance 0.2 mm,
and vias 0.6/0.3 mm. Resolved supply policy includes a 0.1 A current budget.
These are the fixtures' own fabrication rules: the runner routes and judges every
stage under `PNR_FAB_PROFILE=legacy` (`--fab-profile`, default `legacy`), which
enforces exactly them. `--fab-profile jlc-pofv` routes and judges under the
engine's default JLCPCB profile instead (`pnr.fab_profile`: 0.127 mm clearance,
0.45/0.30 mm vias, vias kept 0.127 mm off SMD pads); `route_case.py` applies the
profile to the rules the router uses, as `writeback` does to the rules KiCad
checks.
Each manifest retains explicit intentionally unused pins. Pin mappings and timer
connections were checked against [TLC555](https://www.ti.com/lit/ds/symlink/tlc555.pdf),
[CD4017B](https://www.ti.com/lit/ds/symlink/cd4017b.pdf), and the KiCad library
footprints. The chaser uses a Johnson counter rather than requiring a separate
serial-data generator. Direct decoded-output reset is a test topology; power-up
phase/timing and analog performance require separate electrical qualification.

## Run

Use a numerical Python with PyTorch, NumPy and PyYAML, and a separate KiCad Python
with pcbnew. The Mac mini's durable local environment is
`output/pnr-regression-runtime/bin/python`. Its resolved dependencies are recorded
in `requirements-macos-py312.lock`; NumPy 1.26 is used with the repository's
PyTorch 2.3.1 because that Torch wheel predates the NumPy 2 ABI.

```sh
output/pnr-regression-runtime/bin/python hardware/pnr/regression/run.py \
  --out output/pnr-regression118/new-run --seed 0 --seed 1
```

The output must not exist. `--case NAME` selects a fixture; `--rounds` and
`--timeout` bound PnR search and individual stages. `--python`, `--kicad-python`,
`--kicad-cli` and `--library` make the runner portable to another configured host.
The Bazel entry point is `//hardware/pnr:native_regression` (manual host integration;
KiCad and its footprint installation are required). Contract and algorithm tests
are `regression_contract_test`, `joint_access_test`, `search_footprint_test`, and
`initial_pool_test`. These can also run directly with Python/unittest.

The runner exits nonzero for any failure, including unavailable dependencies.
Every run copies its numerical/native Python sources into `source-freeze`, records
hashes, versions, seeds, fixture/netlist/library fingerprints and individual stage
logs. It writes `summary.json`, per-case `result.json`, and CI-readable `junit.xml`.
Concurrent source editing cannot change the running frozen Python modules. Native
KiCad and source libraries must remain installed and unchanged during execution.

Acceptance requires legal placement, complete grid routing with no deferred nets,
exact original pin/net assignments, source-width tracks, qualified existing SMD
track contacts, zero native unconnected items, and zero native DRC findings
(including warnings). Zone-only contacts still rely on native DRC; the pad-entry
witness is a local track-contact check, not current/thermal qualification. Logs
retain opens, DRC types, copper length, vias, wall time and joint escape diagnostics.

Verify the installed native oracle itself using a passed two-part case:

```sh
# PNR_KICAD_PYTHON / PNR_KICAD_CLI: the headless (LSBackgroundOnly) KiCad copy, e.g.
# ~/Applications/KiCad-headless.app/Contents/{Frameworks/Python.framework/Versions/3.9/bin/python3,MacOS/kicad-cli};
# the /Applications/KiCad binaries register a Dock (Foreground) app on every call.
PYTHONPATH=hardware/pnr "$PNR_KICAD_PYTHON" \
  hardware/pnr/regression/check_native_oracle.py OUTPUT/01-connector-led-2-seed-0 \
  --kicad-cli "$PNR_KICAD_CLI"
```

It saves deliberately open and shorted copies in `negative-controls`; the passed
board is hash-checked unchanged. Both faults must be detected. KiCad workers use
separate processes and initialize wx to avoid invalid SWIG wrappers during board
reuse. Deliberately corrupted boards are diagnostic, never promoted or fabricated.

## Each hill-climb

1. Preserve an initial failing run, form a specific hypothesis, change the router,
   then rerun the complete ladder with the same budgets and fixed seed set.
2. Keep failed cases visible. Do not fix a test by deleting pins, lowering widths,
   expanding only that fixture's outline, hardcoding a placement, or ignoring DRC.
3. Export all enabled layers of the saved diagnostic/final boards using
   `hardware/tools/export_mini_review.py`; inspect actual page images, record only
   genuinely reviewed pages, and run `verify_mini_review.py`. Copper and annotated
   copper pages need individual inspection; technical pages can use contact sheets.
4. Every case gets a 5 mm via-proximity scan. Review changed clusters using actual
   contacts; proximity alone does not prove a redundant via.
5. Update the transfer-root HANDOFF-PROGRESS.md with run paths, exact native results,
   visual observations and remaining failures. Small-board success is a prerequisite
   for a new full Splanc trial, not evidence that Splanc itself is routed.

The paused full116 run and protected fresh28 board are not touched by this suite.

## Initial placement exploration

Use `--initial-pool --initial-starts 8 --initial-finalists 3` to generate and
legalize distinct whole-board starts, score their multilayer capacity, and route
three finalists under equal detail budgets. The legacy first legal placement and
a legal source incumbent are retained. Pool diagnostics and each candidate pose
live in `CASE/rounds/initial-pool/`. These are screening results; only the saved
final board through all native stages can pass this regression. Ambient pool
variables do not silently enable this mode in the runner.

See [RESULTS-118.md](RESULTS-118.md) for the frozen ladder, native comparisons,
actual PDF review evidence and remaining placement/route-quality findings.

## Traces and animations

`--trace` records a `pnr-trace-v1` trace per case in `CASE/trace` (`pnr.trace`):
the engine lane (global placement snapshots, the legalization order, every net
the detailed router adds, rips or commits, the pool's selections) and the native
lane (copies of the board after `writeback` and `planes` with their own KiCad
DRC, the saved board and the verdict). Tracing is observational: placements,
routes and reports are byte-identical with and without it (`trace_noop_test`).
`python -m pnr.animate CASE --out case.webp` (`//hardware/pnr:animate`) renders
the critical path from the unplaced board to KiCad's verdict, with the rejected
candidates of each selection as montages (`pnr.provenance`);
`//hardware/pnr:ladder_animations` runs the traced ladder with the initial pool
and renders every case (`--render-only RUN_DIR` skips the ladder) into
`docs/animations/`, with `manifest.json` and `ladder-results.json`. Case
directories in `result.json`, `summary.json` and `junit.xml` are relative to the
run directory. `provenance.json` records the fab profile, the checkout's commit
(and whether engine files were modified), the platform and `sources_sha256`, one
digest over the frozen sources that survives a rebase. The committed animations,
the current results of every case and how to regenerate them are on the docs page
[Regression ladder](../../../docs/regression-ladder.md). `python -m pnr.animate`
also takes a successive-halving run (coarse: its saved placements, rung
objectives and the winning rung's native phases).

CI (`.github/workflows/ladder.yaml`, informational) runs cases 01 to 06 on pull
requests that change engine inputs, and all cases with seeds 0 and 1 nightly,
inside the arm64 image, plus a traced pool run whose animations are uploaded as
an artifact.

## Showcases

`designs.showcases()` lists cases outside the ladder that show placement
constraints and hierarchy: `line-chaser-20` (the 07 chaser with LEDs D1 to D5 in
one `line_group`), `edge-io-12` and its free twin `edge-io-12-free` (connector,
button and LED held on the south edge with a hard `edge_align`) and
`hier-twin-bank-32` (a 555 clock and two identical CD4017B LED banks as three
blocks of two templates). `run.py --showcases` offers them to `--case`; without
the flag only the eight ladder cases exist. A design with `driver: hier` runs
`hier_case.py` instead of `route_case.py`: each block template is placed and
routed on its own board (trials over outlines and seeds, the best by rank), the
top level places the blocks as rigid macros and routes the nets between them
with the block copper held fixed, and it writes the same outputs, so writeback,
planes, refill, DRC and the gate run unchanged. A design with a `line_group` or
a hard `edge_align` also gets an independent audit of `placed.json`
(`constraint_audit` in `result.json`; a failure adds `constraint_violated`).
`--trace-placement-every N` makes global placement snapshots denser for the
animations. `//hardware/pnr:showcase_animations` (`animate_showcases.py`,
`--render-only RUN_DIR`) renders them side by side (free and constrained) and in
chapters (hierarchy) into `docs/animations/`; the page is
[Constraints and hierarchy](../../../docs/constraints-and-hierarchy.md). The
showcases never gate: the nightly lane runs them for information.

The detailed router's A\* runs on the packed kernel by default (integer cell
keys; the same predicates, prices and tie order as the reference search, so the
same routes). `--reference-maze` routes with the reference kernel instead, and
`--packed-maze` is accepted as a no-op for recorded configurations.
`--maze-kernel native` compiles the kernel's search loop in C from the frozen
sources with the host compiler and routes with it (the same routes again, about
six times faster than packed on a dense board); each case's `pnr-report.json`
records the kernel that actually ran (`maze_kernel`), which is packed when the
library cannot load. `--exact-separation recover|full|off` sets the detailed
router's separation model (`PNR_EXACT_SEPARATION`, `pnr/route/detail/exact_route.py`):
`full` routes with the exact pairwise copper separation instead of the halo
model (which keeps nets about twice as far apart as the rules ask), `recover`
routes again with it only when a detailed route leaves connections open and
keeps that route when it leaves fewer open, so every route that completes is
unchanged (the engine's default; it needs the packed or native kernel, so
`--reference-maze` keeps the halo model). Each case's `pnr-report.json` records
the mode (`exact_separation`), and `place-route.log` each recovery. The
performance opt-in `--batched-wirelength` can be tested explicitly. All of these
are recorded in provenance; ambient variables are still cleared, so a baseline
invocation keeps its original algorithms.
