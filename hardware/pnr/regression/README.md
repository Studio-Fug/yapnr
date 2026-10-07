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
checks. Under a profile the engine adapts to it: a track width finer than its
`min_track_width_mm` (a design default, a class or a pair width) is raised to it
(`rules.json` `fab_adaptations`), and a fanout via class it cannot drill takes its
filled in-pad via when that is no wider. A rung that needs a finer fab declares
it (`fab_profile`): the UFBGA-201 rungs' 0.35/0.15 mm plane drops and 0.10 mm tracks
run under `jlc-6l-hdi` (JLCPCB's multilayer 0.15/0.25 mm vias, 0.09 mm tracks,
filled vias) whenever the run selects a profile; `legacy` keeps every fixture's
own block.
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
`docs/animations/`, with `manifest.json` and `ladder-results.json`; the
committed ones were recorded with `--compact --gloss` (`--runner-arg`), and a
board saved after the gloss stage plays as a before/after. Case
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
inside the arm64 image, plus a traced pool run (with the committed animations'
`--compact --gloss`) whose animations are uploaded as an artifact. Nightly it also
runs the hard rungs of the `ladder` lane (`hard_rungs.LADDER_RUNGS`: the six hard rungs
promoted to the public ladder, rows 09 to 11 of the docs page) and of the `nightly` lane,
seed 0 (`run.py --lane ladder --lane nightly`).

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

The detailed router's A\* runs on the native kernel by default: the packed
search loop (integer cell keys over a dense per-net field; the same predicates,
prices and tie order as the reference search, so the same routes) in C,
`pnr/route/detail/native/maze.c`. The default lives in one place,
`pnr/route/detail/kernels.py` (`DEFAULT_MAZE_KERNEL`): the engine reads it when
`PNR_MAZE_KERNEL` is unset, `--maze-kernel` defaults to it, and the
`ladder-cell` kind of `yapnr exp` passes `--maze-kernel` only when a campaign
names one (`[config] maze_kernel = "packed"`). `--maze-kernel packed` routes with
the same search in Python, `--reference-maze` with the reference kernel, and
`--packed-maze` is accepted as a no-op for recorded configurations. For the
native kernel the runner uses a library that records the frozen `maze.c`'s
sha256 (the yapnr wheel's in the container image, `yapnr/native/`; Bazel's;
or one built beside the source with `python -m pnr.route.detail.native_maze
build`), else compiles one with the host compiler, else routes packed (the
same routes, slower) and says so; the loader also refuses a library whose
arithmetic fuses multiply-adds. Each case's `pnr-report.json` records the kernel
that actually ran (`maze_kernel`) and, under `maze_kernel.reference_fallback`,
why searches ran on the reference kernel instead (a grid the dense fields do
not model, such as a board with blind, buried or micro vias). The kernels
return identical routes on every machine: IEEE doubles in the same operation
order, no fused multiply-add, no libm, and the same `(f, tie)` heap order, so
ties break the same way (`tests/test_dense_maze.py`, `TieParityTest`).
`--exact-separation recover|full|off` sets the detailed router's separation
model (`PNR_EXACT_SEPARATION`, `pnr/route/detail/exact_route.py`): `full` routes
with the exact pairwise copper separation instead of the halo model (which
keeps nets about twice as far apart as the rules ask), `recover` routes again
with it only when a detailed route leaves connections open and keeps that route
when it leaves fewer open, so every route that completes is unchanged (the
engine's default). The recovery is skipped on a board whose later stages add
copper the route does not hold (plane drops placed after routing, deferred
native nets), since it cannot tell whether it took their room. It needs the
packed or native kernel, so `--reference-maze` also turns it off: a reference
run differs from a default one in the kernel *and* in the recovery. Each
case's `pnr-report.json` records the mode (`exact_separation`), and
`place-route.log` each recovery or skip. An unknown kernel or mode is an
error. The
performance opt-in `--batched-wirelength` can be tested explicitly. All of these
are recorded in provenance; ambient variables are still cleared, so a baseline
invocation keeps its original algorithms.

## BGA fanout rung

`11-ufbga201-fanout-6L-SGSGPS` (`hard_rungs.py`, manual lane) breaks an STM32F207 in
KiCad's stock 0.65 mm `UFBGA-201` footprint (UFBGA176+25; the ball map is KiCad's stock
symbol for the part) out to four fixed JST SH connectors: 47 GPIO balls from rings 0-3,
every ground and supply ball dropped to its plane. The board declares the escape as a
`fanout` (docs/hardware/pnr-inputs.md): 0.35/0.15 mm plane vias on interstitial sites,
0.40/0.20 mm dog-bones, no surface exit north, a reserved corridor over three east-edge
balls. Its `via_class` and `escape` checks (`check_constraints.py`) hold the drops to
their class and site and every listed ball to an escape.

`11-ufbga201-fanout-6L-SGSGPS-block` adds a fixed block (one new dimension, `parts`):
an RF launch from ball F15 to a U.FL connector east of the array, in one KiCad group
with two ground stitching vias on the array's interstitial lattice beside F15 (sites
the ground balls E15 and G15 may also choose: the plan joins them instead of drilling
again), a ground fence and an F.Cu-only rule area at the fanout's north-east edge. A
class keep-out over the launch lets only ground into F.Cu, In2.Cu and the supply plane
In4.Cu (the engine cuts its VCC plane out of it), and a class guard on F.Cu lets only
the plane nets pass beside it. Its `no_copper` checks judge zones too (`items: zones`),
so a plane poured into the launch fails the rung.

`11-ufbga201-fanout-6L-SGSGPS-classes` adds routing rules (one new dimension,
`constraints`): the supply plane class at 0.12 mm (its 0.35 mm interstitial drops leave
0.125 mm to the balls around them) and the ring-0 south balls R4 and R8
(PB1, PE7, to J2) in a CLK class at 0.20 mm; custom rules (`dru_rules`, written into
the judge's `.kicad_dru`) that bar vias on CLK, keep CLK 0.25 mm from every net without
a class and hold holes 0.425 mm from the outline's stroke edge; a 1 mm fiducial (KiCad's
`Fiducial_1mm_Mask2mm`: its own 0.6 mm clearance and 0.5 mm mask margin) fixed in the
south exits' corridor; and an outline with 1 mm corner radii drawn at 0.15 mm
(`outline_shape`, native.py). The engine declares `board.class_clearance: maze`,
`dru_routing` and `edge: exact`; KiCad's DRC judges clearance, mask bridges and hole to
edge, and the `net_vias` check (`check_constraints.py`) holds CLK to no via.

`11-ufbga201-fanout-6L-SGSGPS-pairs` adds a coupled differential pair (one new dimension,
`parts`): the unused adjacent ring-0 south balls R13 and R14 become `LVDS_N` and
`LVDS_P` and run to a 2-pin 1.0 mm JST SH header fixed on the south edge east of J2.
The engine declares `board.route_pairs: coupled` and the pair (0.10/0.15 mm, skew 0.1 mm)
with `layers: [F.Cu]` and `max_uncoupled_mm: 3`, so it is routed coupled from the
fanout's escape exits (`pnr.route.detail.pair_route`). The judge's custom rules hold its
skew, uncoupled length (3 mm) and gap (0.14-0.16 mm), and the `net_vias` check holds it to
no via (F.Cu only).

## Top rung

`12-soc-bga-113` (`soc_rung.py`, manual lane) is a purpose-built single-board computer
around an STM32F746 in the same 0.65 mm UFBGA176+25 as the BGA fanout rung, with the
buses of a Cortex-M7 board: a 16-bit SDRAM on the FMC (IS42S16400J, TSOP-II-54) whose
high data byte lane (DQ8-DQ15) is a `length_match` group (1.0 mm), a quad-SPI NOR flash,
an RMII Ethernet PHY (LAN8742A) with 22 ohm series terminations, 49.9 ohm MDI
terminations and two MDI differential pairs to a magnetics header, USB full speed (Micro-B,
ESD array; a differential pair), a microSD socket on SDMMC1, SWD and I/O connectors and
RGB/power LEDs: 113 parts. Four supply domains: USB 5 V, a buck to 3.3 V that owns the
supply plane (In4), and two LDOs for the MCU's analog supply (VDDA, VREF+) and the PHY's,
both routed on signal layers. Six layers (S G S G P S), the BGA rung's fine-pitch rules and
fanout (interstitial 0.35/0.15 mm plane drops: an HDI-capable profile's drill). The MCU,
the SDRAM, the USB receptacle, the magnetics header and four holes are fixed; the microSD
socket, the debug header and the I/O connector are locked to their edges; the rest is
placed by the engine, with hard proximity groups keeping each part's support parts
(decoupling, crystals, terminations, regulator capacitors) beside it. Ball functions come
from KiCad's `STM32F746IGKx` symbol. It is a target (`ci.target`): on 2026-10-06 the router
left about a third of its nets open, did not match the SDRAM lane, and crossed the keep-outs
that the microSD and QFN footprints carry (the router does not read footprint rule areas).

## Push-and-shove rungs

`11-shove-channel-14` and `11-shove-channel-lm-14` (`shove_rungs.py`, manual lane) are
small two-layer boards split by a copper wall (a track and via keep-out on both layers,
judged by `no_copper` checks) with one 4.25 mm channel: a 10-net bus runs between
connectors fixed on the west and east edges, and four late nets cross it from a connector
north-west of the wall to one south-east of it. Nine tracks fit per layer at the fab's
pitch (eight on a 0.25 mm routing grid) and fourteen nets need the channel, so a router
that commits early nets to the middle of the channel on both layers must push them aside to
finish. The `-lm` variant moves the east connector 7 mm north and matches the bus to 0.5 mm,
so the bus needs meander room where the late nets pass. Every part is fixed. Both carry a
`ci.target` (`push-and-shove`): the current router leaves nets open on them, and they wait
for the queued push-and-shove router.

## Dovetail rung

`13-dovetail-blocks-23` (`dovetail_rung.py`, manual lane) is a hierarchical board whose
blocks pack best interlocked: two port blocks of one template (a 12-pin JST SH side-entry
connector with four series resistors and a decoupling capacitor, the passives held by a hard
group within 8 mm of the connector's pin 1, so the routed block is an L) and a core of four
SOT-23-5 AND gates with their capacitors, each gate taking one input of each port. It is
the rung of the hull packing (`--macro-hull`: blocks enter the top level as their per-side
routed outlines, and `--hull-dovetail W` packs them in global placement) and of the
route-then-compact pass (`--route-compact`, [design](../../../docs/design/route-compact.md)),
whose `pnr-report.json` `route_compact` record carries the bounding box, the outline
shrink it frees and the gutters before and after.

## Length-matching scratch designs

`12-soc-bga-113` and `11-shove-channel-lm-14` declare length-match groups; a group with a
budget in mm gets a KiCad `skew` rule in the rung's custom rules (`dru_text`), so KiCad's
DRC judges it. For the router's length tuning alone,
`lenmatch_scratch.py write OUT.json` writes scratch designs for the router's
length tuning: an 8-net bus from a fixed SMD connector to an SOIC (a group, 0.5 mm)
and a differential pair whose pins swap between its connectors (1.0 mm skew), on two
layers and on 4L-SGPS with the bus's budget in ps (`lm-bus-pair`, whose bus reaches the
SOIC in reverse pin order, and `lm-bus-pair-4L-ps`), and the bus turning a corner to
an SOIC fixed above and to the right of the connector (`lm-bus-corner`). `run.py --design-json OUT.json` offers
any such list to `--case`. `lenmatch_scratch.py judge CASE_DIR --kicad-cli PATH`
then runs KiCad's DRC on a copy of the routed board with a `skew` rule per pair and
per group with a budget in mm and reports KiCad's lengths beside the engine's
`length_tuning` report. A budget in ps has no KiCad judge: KiCad 10.0.6's
`kicad-cli pcb drc` reads every delay as 0 ps (KiCad issue 23868), so those sets are
reported from the engine's audit only.
