# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-10-10 (PR #99 merge preparation). Previous status:
2026-10-04 (the radar60 stage-3b routing engine, pull request 49, on
`claude/radar-routing-engine-2`). Earlier the same day: RF round 2 with the native FDTD kernel
on `claude/rf-topopt`, merged with `main`; the hard rungs on `claude/ladder-hard-rungs` and the
engine's fixes for them on `claude/gap-fixes`, with `claude/gap-constraints` merged; the gloss
port's review fixes on `claude/gloss-port`. Before that, 2026-10-02: fab outputs and staged
ordering, F2, F1 and O1, on `claude/fab-order`. Before that, 2026-09-30: PR4, #10, the ladder
animations, the atopile toolchain and the privacy-scan trailer rule merged; line groups, hard
board edges, the hierarchical ladder driver and their animations on
`claude/animations-groups-hier`.

## In progress

- **PR #99 integration**: the native engineering workbench, evidence-gated workflow,
  traceability/artifact review, recorded attempts, circuit simulation and manufacturing
  handoff are ready for final hosted validation. Corrected the dock transaction expectation
  for Manufacturing, moved Git into the final container runtime for project initialization,
  and cleared hidden through-hole selection when inspecting qualified packets. JavaScript
  transaction/syntax checks pass; full regressions and hosted image smoke are being rerun.
  Consolidated feature evidence is linked from the PR; older session entries live in Git.

- **Assembly inventory**: quantity-aware catalog filtering and assembly-house checks
  publish replayable BOM/identity/quantity evidence. JLCPCB public inventory includes
  orderable stock and declared allowance/minimum screening; PCBWay explicitly needs
  its own supplier confirmation. Native Manufacturing exposes per-part shortages and
  historical checks. Network failure/unknown stock never passes sourcing.

- **Manufacturing GUI**: approved PCBWay packets have an explicit human-clicked
  official-plugin file upload and quote handoff, with exact artifact receipts.
  JLCPCB automatic attachment awaits approved API access; manual handoff remains.

- **Manufacturing package discovery**: published native/prototype packages are discovered automatically.
  Supplier, quantity, finish and optional through-hole service lead to local vendor-specific
  BOM/CPL preparation, previews, content-bound review and human downloads/quote handoff.
  Prototype vendor DFM/rotation and physical verification remain explicitly open.

- **On-demand parts**: authoring CLI builds fetch missing component queries, retain response
  digests and selected assets/locks in the article, and replay offline/frozen. Operator
  our rules_atopile-derived picker matches public supplier facts without hosted
  Atopile calls, accounts or tokens. No shared catalog uploads or prepopulated local
  library are required. Typed discovery covers resistors/capacitors and explicit
  LCSC/MPN picks; network/data failures remain explicit.

- **Workspace search and Performance**: Cmd/Ctrl-F opens a floating source/component/net
  search, including in embedded frames; repeated shortcuts preserve/clear queries.
  Inspect tab layouts migrate to the dialog. Performance holds native controls and
  validated project cloud settings with read-only SDK status and no job submission.

- **Recorded build attempts**: Experiments lists archived schematic-build results, including
  failures before PnR, and opens their result JSON in read-only source tabs. Restored active
  tabs initialize on first drawer open. Embedded styling and saved theme apply before
  renderer scripts load, avoiding the legacy-theme flash. Live discovery is read-only
  and retains failed attempts. The workspace server accepts cold-start asset bursts
  without dropping concurrent proxy connections.

- **Experiments embed**: workbench tabs own panel visibility; the native legacy drawer
  controller is bypassed in embeds. Experiment lane selections follow through to the live
  board/schematic/3D views while pinned comparisons remain independent.

- **Workbench follow-up**: drawer open/dismiss motion respects reduced motion; startup no longer
  waits for provider discovery. Source syntax, import navigation and hash-scoped selected-text Ask
  context pass browser checks. A project directory tree starts in the left drawer.

- **Prompt-to-PCB agent workflow**: owner-provided engineering workflow is now
  scoped into AGENTS.md; the installed wheel ships exact instructions and execution
  guide. `agent init` preserves contracts/checkpoints, `agent instructions` exposes
  guidance and `agent chat` opens an operator-owned interactive Codex/Claude terminal.
  The image includes checksum-pinned Codex and Git; no credentials or paid model
  sessions run in CI. Focused bootstrap/installed-wheel/container checks and hosted
  validation are in progress. Complete populated RF assembly acceptance remains #97,
  not satisfied by DUT-only packaging smoke.
  Review-first requirements/risk presentation, bounded speculative implementation,
  refinement-driven restarts and inline visual feedback are now explicit in both
  embedded guidance and the launcher prompt. `workflow init/query/next` implements
  a declarative, evidence-gated iteration controller with atomic state/history,
  immutable receipts, revision invalidation and acceptance/completion guards.
  Receipts remain assertions requiring genuine user decisions and authoritative
  validation reports; the controller does not authenticate them or sandbox agents.

- **Workspace-native browser UI**: replaced the OpenCode overlay with one layout
  for projects, focused/main conversations, artifacts, engineering views and a
  resizable chat pane. Background activity collapses into one spinner/details group;
  the footer follows controller stages, including review and rework. The journaled
  main-thread harness continues idle agents with their selected model, stopping at
  genuine review checkpoints and native questions/permissions. Turn duration,
  elapsed session time and continuation counts never stop the design loop.
  Stop and explicit resume are persistent. Runtime logs remain exportable but are
  excluded from Git snapshots to prevent recursive chat-summary growth.
  YAML requirements/risk/trace models now populate a native browser using pinned
  rules_requirements semantics. Report artifacts link to owned verification cases;
  stale or corrupt evidence remains open. Content-bound artifact review carries
  canonical feedback/questions, explicit approval and a canned revision cycle.
  Bundled package inspection is allowed without repeated external-directory prompts.
  Schematic capture opens live atopile sources alongside a netlist-backed schematic,
  retaining the last valid export through incomplete writes and failed builds.
  OpenCode remains the headless provider/tool/session runtime.
  Canonical notes carry immutable focused transcripts into the main conversation
  without starting a model turn; shared document edits use revision checks.
  Pinned sandboxed three.js choices, rendered artifact markup, full conversation
  journaling and integrity-checked workspace archives are implemented. Browser
  checks distinguish synthetic review fixtures from actual engineering views.
  Solver regeneration remains unverified until a pinned replay recipe passes;
  image-build toolchain warming remains outstanding.

- **External RF packaging (#96)**: collect every RF Bazel subpackage explicitly
  without numerical dependency closure in the wheel collector. Export/report,
  planar, Palace, coupons and order0 modules now ship, with the authoritative
  Palace schema and registered coupon copper/results. An isolated installed-wheel
  test runs design/export/native revalidation outside the checkout; both image
  architectures run the same external-project smoke. The installed-wheel smoke passes;
  full-suite and two-architecture CI validation are pending.
- **PyPI publishing**: the release workflow publishes the same two tested Bazel
  wheels through OIDC, with version/project/architecture checks and no dry-run uploads.
  First upload requires the owner to register the documented pending trusted publisher.

- **RF CI lanes and Bazel cache snapshots** (branch `codex/rf-nightly-ci-cache`): expensive RF
  solver/gradient/design and coupon fitting coverage moves to nightly/manual Linux and macOS;
  ordinary CI keeps inexpensive RF units and a deterministic native optimizer smoke. Expanded
  Bazel targets are checked against an exhaustive lane inventory. Explicit cache saves retain
  completed outputs after test failures with fork PRs restore-only. The existing CI dispatch can
  explicitly invoke the RF lane before merge. Native smoke, 14 focused policy regressions,
  changed-file hooks and actionlint pass; both hosted platforms verify the expanded 65-target
  inventory. Both RF lanes and same-head cache replays pass; detailed measurements and
  combined-head CI status after #91/#92 are tracked in PR #95.
  [Lane inventory and policy](docs/ci-lanes.md).

- **Hull nesting default (2026-10-07, PR #91).** Owner requested default-on nesting for hull
  macros with explicit opt-out. Legality/native gates and both routing fallbacks are unchanged.
  The integrated native dovetail example is clean with nonzero interlock; vias increase in
  that one pair. Final-head versus main dovetail and quad-bank campaigns remain pending under
  the approved total budget. See [validation status](docs/design/hull-nest-validation.md).

- **Route-then-compact and hull packing** (PR #86, branch `claude/route-compact`; design
  [docs/design/route-compact.md](docs/design/route-compact.md)). `PNR_ROUTE_COMPACT`
  (`TOP`, `BLOCK`, `FLAT`; default off): after routing, order-preserving 1D compaction (x then
  y) by the copper each gutter holds, rip-up and reroute, back-off on a worse route; the
  hierarchical driver measures block hulls from its in-memory routes (`PNR_MACRO_HULL`), GP
  packs hull bodies (`PNR_HULL_DOVETAIL`), new rung `13-dovetail-blocks-23`. Rebased on main
  (#80's viewer stage timing wraps the reroute step now). Review-fix round (2026-10-06):
  `not_worse` counts deferred nets and `port_debt_mm`, not just missing/unresolved; its vias/
  copper tolerance is a share of the pass's original route, not the previous accepted step
  (no more ratcheting); `feedback.py` refreshes `deferred_nets`/`converged` after FLAT instead
  of leaving them stamped from the pre-compaction route; the pass skips outright when the base
  route is itself incomplete; the deadline's first-reroute estimate no longer borrows from wall
  time spent before route-compact started; `compact_block` counts violations, not kinds, and
  gains the flat board's legacy-plane/coupled-pair guards; soft `edge_align`/`region` refs hold
  during compaction; new `pnr.place.hull.macro_overlap_area_mm2` metric and
  `hier["hull_rects"]` for renders to draw real hull geometry. Hierarchical A/B rerun at head
  in progress (campaign `20261007-ladder-01bca2`, adds a hulls-only-no-compaction arm). Next:
  rebase on #64/#70 and A/B again on top of detailed placement; hull support in `compact.py`
  and the keep legalizer (gp_polish still turns off over hull bodies); the push-and-shove
  router as the reroute.

- **Energy track optimization (#90, default off).** The witness-growth/reverse-cost proposer is
  wired into the native gloss transaction controller with exact native checks, joint-refill IR
  and reference evidence gates, protected metadata/copper guards, spatial quiet-chain invalidation,
  and phase-end rollback. Shapely/GEOS dependencies are hash-locked separately for controller and
  KiCad Python ABIs. Focused tests and actual clean-fixture native acceptance/refusal/byte-identity
  checks pass; a four-state recorded native animation documents the change. The KiCad dependency
  install now uses root's build cache and copy links, preserving runtime-home ownership; a focused
  regression checks its environment. Image build/smoke and Linux aggregate CI pass. The macOS
  ABI-path test now checks only the canonical path is added; all 16 integration tests and eight
  hard-edge tests pass locally, the latter within its unchanged 60-second limit. Next: fresh macOS
  CI, then review before merge.
  RF/SI-dependent plane edits without complete external evidence remain refused; no radar-board
  completion or joint pair solver claim.

- **Routing engine for radar60 Rev A, stage 3b** (#49, branch `claude/radar-routing-engine-2`;
  all opt-in, byte-identical where undeclared). Router: pad-local clearance and mask margin (data),
  `board.class_clearance: maze | repair`, `board.dru_routing` (a board's `.kicad_dru` where it
  constrains routing; the rest listed as unmodelled), `board.edge: exact`, and SMD lands on the
  far side of their footprint (`Pad.far_side`). Power: partial fanouts (the fanout's
  `partial`: bridge, retry), `plane_partition` (connected per-rail territories on a power
  layer; trees only where a zone of the minimum width fills, foreign copper at the pair's class
  clearance) and `ir_drop`
  (a resistive network per rail on the refilled board; `unsolved` when CG stops short; the solve
  runs in `PNR_PYTHON` where KiCad's Python has no numpy). New hard rungs `11-ufbga201-...-classes`,
  `-partial` (run.py excuses a spec's `designed_open` pads, held exact by its `unconnected`
  check) and `-rails`. Identity regression against `main` (GCP C4D, `--compact --gloss`, 78
  shared cells): placed, routes and rules identical in all; boards identical modulo segment
  direction in 77, the 78th (`-block` s1) differs after the gloss stage only (its pre-gloss
  board is identical; KiCad's dangling check on a 0.035 mm segment depends on the direction the
  gloss writes); 78/78 same verdict. Next: the radar trial's open items (the U2 power stage
  escapes, LVDS as coupled pairs (A8), 1V2/1V8 delivery, the PA corner with macro v2).

- **RF microstrip inverse design** (#29, branch `claude/rf-topopt`; design
  [docs/design/rf-topology-optimization.md](docs/design/rf-topology-optimization.md), guide
  [docs/rf-inverse-design.md](docs/rf-inverse-design.md), choices in
  [docs/decisions.md](docs/decisions.md)). Done: the solver (`yapnr.rf`: Yee FDTD, CPML, line ports,
  exact adjoint), the optimizer (material grid, conic filter, tanh projection, Zhou length scale,
  own MMA with the epigraph, robust variants, adaptive moves, specs with lumped resistors and their
  absorbed share, checkpoints, the best binarized design exported), net-tie `.kicad_mod` export,
  Touchstone, result JSON, the animation, and five end-to-end cases re-validated from the footprint
  on three grids. Round 2 (2026-10-02/03): a copper-edge correction and a modal port source, the
  antenna grown from the feed line alone, the cases re-run. Review fixes (2026-10-03, design §24):
  the footprint follows the pixel boundaries (the chamfered export had put other copper on the finer
  grids: most of round 2's 0.7–2 % grid shifts), the repair widens the one-pixel necks the exact
  copper shows, the time-step library holds the diagonal patterns, the `trust_reference` option, one
  thread per Bazel RF test with long timeouts (CI had timed out; it passes now), `seed_overlap` and
  the −10 dB band in `validation.json`, the antenna judged over 9.7–10.3 GHz at η ≥ 0.7. Results
  (same copper on every grid): divider, combiner (topology given by seed and keepouts) and diplexer
  (a closed-form stub filter refined, 77 % seed) pass on all three grids; the antenna meets |S11|
  and η on all three but fails the 4 % power balance at 9.7 GHz on the finer two (4.6, 4.7 %; cause
  not established, design §24.7); the bank fails (adjacent channels −9.8 and −11.4 dB against −12
  dB). A diplexer from the plain junction (robust, 25 iterations) only rolled off (rejection 13–16
  dB; stopped). Artifacts in `docs/rf/`. Native kernel (2026-10-03, `claude/rf-kernels` merged
  with `main`, [docs/rf-solver-backends.md](docs/rf-solver-backends.md)): the C stepper is the
  default backend wherever its library loads (`auto`, float64, bit-identical to numpy; numpy
  otherwise, said once), with every round-2 option (sha256 matrix in `test_native_identity`);
  Bazel builds it into `//yapnr/rf`, the wheel is per platform (manylinux_2_34 x86_64 and
  aarch64, macOS arm64) and the image loads it from the wheel (smoke-tested). Verified again
  independently (2026-10-04): the full-grid identity matrix on a C4D, end-to-end runs on the
  smoke grids, the divider and antenna re-validated on native float64 (round 2's verdicts, within
  2.5e-4 dB); one iteration on a C4D-16 takes 1.47 s (divider) and 16.1 s (D1 size) at 16
  threads, 8.4 and 6.3 times faster than torch float32. Fixed then: a case's run directory
  resumes with its own spec, a process solves each port mode profile once (the solve's last
  bits varied between processes on the Mac), and the library's first load is thread-safe
  (threads asking while it ran had fallen back to numpy). Next: the owner's
  decisions (balance criterion, the seeded filter banks, the native defaults), sub-pixel tuning
  of binary copper for resonant filters, a modal port extraction for radiators, a `yapnr rf`
  CLI, an external cross-check, footprints in PnR.

- **Compact placement (`PNR_COMPACT`, shrink-to-fit `PNR_SHRINK`, both off by default)**
  (branch `claude/compact` on `main`; design
  [docs/design/compact-placement.md](docs/design/compact-placement.md)): offset courtyards, the
  compact legalizer, spread 1.0 with clustered starts, a compactness tie-break after the vias,
  legacy plane drops planned before routing (`DROPS`) and the flat driver's outline search.
  Flag-off identity holds on x86 (placed and routed bytes of 30 cells against `main`). The A/B
  (GCP, seeds 0 and 1) passes the ladder, the showcases, the header rung, `08-chaser-20-plane`
  on ten seeds and the nightly hard rungs, but 5 of 16 manual `09-mcu-usb-31` cells regress (USB
  pair skew), so it stays opt-in. The docs and README animations (ladder and showcases) are
  regenerated with `--compact --gloss` (renderer 3: the gloss stage as a before/after; CI's
  traced runs take the same options). Next: reserve room for pair tuning under compact, then
  another A/B.

- **Hard-rung gap fixes** (branch `claude/gap-fixes` on `claude/ladder-hard-rungs`, local, not
  pushed). The hard rungs (`regression/hard_rungs.py`: stackups, via kinds, sides, absolute and
  relative constraints, Monte-Carlo search, a THT header) found capabilities the engine lacked.
  Five tracks are merged, each new behaviour following from the board's inputs (a board without
  them routes as before; the eight ladder cases and four showcases give identical boards):

  - declared stackups (`pnr/stack.py`): a KiCad stackup with a `power` or `mixed` layer, or a
    custom rule keeping tracks off a layer, routes every signal layer of any count; plane drops are
    planned with the pin escapes; every plane layer is formed (full outline by default);
  - blind, buried and micro vias (`pnr/via_policy.py`, the rules' `via_policy`): only spans the
    declared stack can build, one build per board, return ties between two planes of one net;
  - sides (`board.sides: double`, `pnr/place/sides.py`, `detail_moves.py`): placement chooses the
    side of free parts; writeback mirrors bottom parts on `B.*`;
  - length matching (`pnr/route/detail/tune.py`, `pnr/length_model.py`, `pnr/place/matched.py`):
    declared pairs and groups are tuned with meanders against KiCad's own length measure;
  - router speed: the native kernel (the packed search over dense per-net fields, in C; identical
    routes; the packed Python kernel where no library loads, `PNR_MAZE_KERNEL=packed` on request)
    is the default (2026-10-07: a 192-cell GCP A/B, every route identical, -54% task time), and an
    exact-separation recovery
    (`PNR_EXACT_SEPARATION=recover`, the default) for routes the halo model leaves open.

  Results (two seeds each): ladder and showcases 24 of 24, CPU 2,281 s to 442 s; hard rungs clean
  in 37 of 48 runs (7 before), CPU 11,537 s to 1,964 s on the 46 runs with a before. Still failing:
  07/09 `abs` and `rel` (`region`, `align`: on `claude/gap-constraints`, not merged), 09 `header`
  (no legal placement) and 09 4L-SSGS seed 0 (USB pair skew after a detour). Three "Ladder fix"
  commits came with the tracks (the plane check, the side check, the HDI microvia size).

- **Gloss, dekink and corridor coalescing (`PNR_GLOSS`, off by default)** (branch
  `claude/gloss-port`; design [docs/design/gloss.md](docs/design/gloss.md)): ported from Splanc's
  src18 with its tests, `board.Delete` instead of `Remove`, workers through `pnr.proc.run_status`;
  the `06g-gloss` and `07g-gloss` loop passes, the ladder's opt-in `--gloss` stage with
  `--gloss-flag` and `--gloss-measure`, functional groups derived from the rules
  (`PNR_GLOSS_CLASSES_FROM`, the most specific group wins) and the public example groups file for
  `07-chaser-20`. Review fixes: the ray metric no longer depends on which way a segment is drawn
  (patch handed back to the source), gloss settings in the router key and imports, the code key
  covers the pass only with the flag, the ladder's outer gate by violation keys, frozen and hashed
  groups file, per-stage CPU as on the hard-rungs branch, a nightly CI lane. Identity with the
  source (plus the same ray patch) is exact on three private replay points; flag-off identity with
  `main` (stubbed loop) holds; a real native loop with the flag ran both passes on a public board.
  Ladder A/B (24 runs per arm, no regression, bends -32 %, length -4 %, CPU 2.2 times): see
  [docs/regression-ladder.md](docs/regression-ladder.md#gloss-opt-in). Next: the larger paired
  native-loop A/B on the cloud lane decides default-on (owner); rebase `run.py` onto
  `claude/ladder-hard-rungs` when it lands.

- **Fab outputs and staged ordering** (branch `claude/fab-order`; design
  [docs/design/fab-and-ordering.md](docs/design/fab-and-ordering.md), guide
  [docs/fab-and-ordering.md](docs/fab-and-ordering.md)). Built: vendor profiles and stackups as
  data (`oshpark-2l/4l/6l`, `jlc-4l`; drafts `jlc-6l`, `pcbway-std`, `pcbway-hf-2l`), engine
  support for them (`PNR_FAB_PROFILE`, `run.py --fab-profile`; `legacy` and `jlc-pofv` unchanged,
  golden-pinned), `yapnr fab profiles|show|check|build|preview` and `yapnr order stage|vendors`
  (staging only, never uploads). Ladder cases 04 (`oshpark-2l`) and 08 (`oshpark-4l`, `jlc-4l`,
  `pcbway-std`) route clean under them and stage as dry runs. Not built: O2 and O3 (decisions D1
  and D3), JLC rotation corrections in the part cache.
- **Merged today:** PR3a (#9, code key scheme 2 and the engine format), the viewer (#12, PR4:
  `yapnr/viewer`, `bazel run //:viewer -- --root <live>`, paid features off by default, elkjs and
  three.js fetched pinned; guide [docs/viewer.md](docs/viewer.md), choices in
  [docs/decisions.md](docs/decisions.md)), and #13 (an explicit `started.json` stamp for the
  routing-feedback staleness check, fixing #10 on Linux). The format commits of #9 and #12 are in
  `.git-blame-ignore-revs` (#11, #14).
- **Merged since:** the regression ladder in CI and the PnR animations (#15: `pnr.trace`,
  `pnr.provenance`, `pnr.animate`, `ladder.yaml`, `docs/regression-ladder.md`) and two viewer
  test race fixes (#16, #17).
- **atopile toolchain and part cache** (merged as #18; A1 and A2 of the end-to-end plan):
  `yapnr atopile setup` (hashed per-platform locks, atopile 0.15.8 on Python 3.14.7),
  `yapnr atopile build` (offline, isolated, bounded; a hook in every atopile interpreter), the
  loopback picker (catalog schema v1), the part cache (store, HTTP server with tokens and takedowns,
  clients, importer, container), the Bazel toolchain and `yapnr_atopile_build`. The adversarial
  review's findings are fixed: a lock pins its part; client tokens go only to the named server (no
  redirects, https or loopback, the write token never with reads); the server checks tokens before
  bodies, bounds its connections, serves only files a part uses, collects orphans, checks file
  types, and a takedown blocks the part's files; local-only parts; no git clone offline and no
  contact with a running KiCad; ato.yaml's layout paths; the Bazel action copies only its declared
  inputs and needs an explicit cache; hashed sdist builds on linux-aarch64. Real atopile: 4
  end-to-end cases (7 builds) pass on darwin-arm64; `bazel test //...` 137 of 137. Not done: the
  image's `/opt/atopile` (A4) and a KiCad 9 against 10 A/B
  ([docs/frontends/atopile.md](docs/frontends/atopile.md#not-done-yet)).
- **Splanc's parts in a local part cache** (local-machine note; Splanc is not modified): the
  committed parts of `splanc`, `splanc_dev`, `splanc_max` and `splanc_eol_tester` (276
  directories, 248 versions of 163 parts) and the 103 entries of `picker_catalog.json`. They are
  in the development Mac's default cache, `<part-cache>` = `$XDG_DATA_HOME/yapnr/part-cache`.
  One lock per board is in `$XDG_DATA_HOME/yapnr/splanc-locks/`. Neither is in any repository.
  Each part records `splanc@<commit>:<path>`, its LCSC id, its generator (`easyeda:C<id>` or
  hand-authored) and a licence note. `splanc_max`'s `OUT` also has a version with the
  git-ignored DEGSON STEP model (`splanc_max.local-cad` lock), marked `local-only` (never
  uploaded; a public server refuses the cache). Every lock materializes the committed files
  byte-identically (rechecked after the review fixes, also over HTTP).
  Mini, built from a scratch copy with its parts directory emptied, has the same input id as a
  build from its committed parts, both from the directory and over HTTP, before and after the
  review fixes. Seeded with the same
  local layout, it equals rules_atopile's last Nix-built board in nets, footprints, pads,
  positions and outline. `splanc_eol_tester` fails on 0.15.8 with or without the cache: atopile
  rejects its `elec/footprints/` directory as deprecated.

- **Constraint and hierarchy showcases** (branch `claude/animations-groups-hier`, on top of
  `claude/ladder-animations`; design `docs/design/constraint-and-hier-animations.md`, §10 and §11
  "As built"). Engine, all opt-in: a HARD `line_group` constraint placed as one rigid macro
  inside `place()`, `edge_align` `hard`/`tolerance_mm`, own-net fixed copper in the detail
  router, and `regression/hier_case.py` (blocks placed and routed on their own boards, placed as
  macros, knitted with block copper held fixed). Traces gain `groups`, `group_members`,
  `header.constraints`, a `fixed` event and per-template block traces. `run.py --showcases` and
  `--trace-placement-every`; `designs.showcases()`: `line-chaser-20`, `edge-io-12-free`,
  `edge-io-12`, `hier-twin-bank-32`. Showcase run (pool seed 0, 8 starts, 3 finalists,
  snapshots every 5): all five cases pass the gate and the constraint audit, 0 opens, 0
  findings; vias 19 (07), 20 (line), 9 (edge free), 8 (edge), 38 (hier). A/B of the eight
  ladder cases: identical `placed.json`, `routes.json` and trace digests.
  Renderer: constraint highlighting (`pnr.animate.highlight`), rigid-body tweens, side-by-side
  comparisons (`python -m pnr.animate --compare A B`, `pnr.animate.compare`), the hierarchical
  chapters (`pnr.animate.hier`, `provenance.from_hier`), `--pacing showcase`, and
  `//hardware/pnr:showcase_animations`. Four files in `docs/animations/` (13.1 MB; folder
  budget 30 MB, the hierarchical WebP 3.5 MB), the page
  [docs/constraints-and-hierarchy.md](docs/constraints-and-hierarchy.md), a second README item
  (the side-by-side chaser GIF), and a nightly, informational showcase step in `ladder.yaml`
  (never run yet). Re-rendering the eight ladder cases reproduces every committed file's
  SHA-256; each showcase renders byte-identically twice.
  Review fixes (design §12): line-group macros reserve their members' sides (`line:<name>`) and
  refuse source-locked members; knit retries trace in their own scopes; `PAD_AXIS` is checked
  against the footprints; half-turns are drawn as flips, off-board poses widen the camera, the
  edge comparison replays its pool's eight starts; GIF palette and per-file renders. Rerun
  `.yapnr/ladder/showcase-2` (engine `c4db5fb`): the five cases' `placed.json`, `routes.json` and
  trace digests are identical to the first run; all pass. Next: the owner's review of the
  decisions in `docs/decisions.md` and the pull request.

- **RF fab-model coupons** (#32, branch `claude/rf-coupons`; design
  `docs/design/rf-fab-coupons.md`, guide `docs/rf-fab-coupons.md`): `yapnr/rf/coupons` generates
  boards A (JLC04161H-7628) and B (JLC06161H-7628), DRC-clean, with expected S-parameters and fab
  zips under `examples/rf-coupons/`, and extracts the fab parameters from a VNA session
  (`python -m yapnr.rf.coupons extract`): multiline TRL, a joint fit with connection nuisances
  and a parametric bootstrap. Synthetic recovery passes in CI. Next: the owner's decisions of
  design §14 (connector, VNA, scope), checking the SMA footprint and JLC's edge clearance, and
  the order (owner only).
- **RF coupons, adversarial review** (#32): the boards' TRL sticks carried no stick id (fixed, all
  labels now `A05 P L40`); mouse-bite tabs widened to JLC's 5 mm minimum; multiline TRL dropped
  nearly degenerate line pairs, whose swapped roots spiked the error boxes and failed a quarter of
  nominal verification lines; a fit stuck at the 2D-table edge is now a reported problem; board B
  alone does not split the core's and the 7628's εr (use board A's fit as `--prior`). Impedances
  re-checked with an independent finite-volume solver (within 0.1 Ω) and IPC-2141A (stripline
  50.5 Ω); recovery re-run with another seed, a wrong nominal stackup and a worse lab. Open: the
  SMA part, the panel fee, a mask-off stick on board B.
- **RF coupons, Order 0 on OSH Park** (branch `claude/order0`, from `claude/rf-coupons`; pages
  [docs/rf/order0/](docs/rf/order0/README.md)): the Cinch 142-0701-851 launch of regions M and W
  (2D-designed), board O as three uploads (O0-M coupons + R1 + A16 + A04R + tag/QR; O0-W light
  with the D2 window; O0-D with the D1 window), the OSH Park frameless panel, line tables for
  FR408HR (EM528 shares them), coupon predictions on both substrates; board B moved to
  JLC06161H-2116C with a mask-off tie line. All DRC-clean; `yapnr fab check` 0 errors. Next: D1/D2
  copper and the R1/R1t FDTD predictions (#29 round 2), the fab bundles, the pre-registration
  release, the capture tooling (WP7).
- **Order 0 part 2** (branch `claude/order0-p2`, main + `claude/order0`): RF stage plans
  require the native FDTD library and pass `YAPNR_RF_THREADS`, backend, dtype and `YAPNR_RF_*`
  per job, `--image-commit` refuses a bundle whose C sources are not the image's (PR #30 review
  follow-up); `yapnr.rf.coupons.equivalent` (2D thickness-equivalent substrates of regions M and
  W on FR408HR and EM528, matched to the coupon model's line at 5 GHz); `yapnr.rf.order0` (D1/D2
  specs in four formulations, the selection rule, criteria, forward-run directories of R1/R1t and
  of run 0b's loss lines, the loss correction and D-O0-11); [docs/rf/order0/demos.md](docs/rf/order0/demos.md)
  and the generated [inputs](docs/rf/order0/inputs/). Run 0b and the D1/D2 compute stage on GCP
  Batch (C4 Montreal, C4D us-west4); `d1-star` passes all three validation grids and is merged
  into O0-D's window (`catalog.o_optimized`, one real DRC clearance call left for the owner); no
  D2 formulation passes (closest miss 0.012 dB). openEMS-vs-yapnr.rf predictions collected
  (D-O0-13a); pre-registration bundle and manifest in `docs/rf/order0/`. Open: the D1 island/DRC
  and D2 near-miss decisions, the release/OpenTimestamps step (owner).
- **Palace, the second RF solver** (#46, branch `claude/palace`; guide
  [docs/rf-palace.md](docs/rf-palace.md), image `docker/palace`): planar documents
  (`yapnr.rf.planar`: lines, the radar60 patch and feed models, KiCad regions with footprint pads;
  lossy `metal` floor; solder mask over solid copper), the Gmsh builder, configurations checked
  against Palace's schema, the validated sign-off settings (`python -m yapnr.rf.palace case`),
  `yapnr.rf.palace.results`, and `palace_plan.py`/`palace_job.py` campaigns with stages. Validated
  against openEMS and the 2D cross-section solver on the radar60 lines, single patch and TX1
  feed; two reviews' fixes in. Open: Palace issues to report upstream, a rank-scaling run before
  the bank model, the image's ParMETIS licence (owner).

## Integration checks

- **PR integration review (2026-10-07).** PR #92 is rebased onto main after #88 and #90, retaining
  only its two new optimization commits. The existing 96-cell A/B receipts were rechecked: 96
  matching verdicts and 94 matching copper hashes, 85 passes per arm. See [validation
  evidence](docs/design/clearance-index-validation.md).

## Next

1. Owner (viewer, #12): review after the fact; decide the agent's default model (opus, $2 per
   turn, $20 per process) and file the issue for `pnr.capacitor_intent` (the cost replay reports
   contexts that need it as unavailable).
2. PR4 follow-ups: `--project` and the manifest's `[viewer]` table (with PR3d), the published
   `yapnr-live-event-v1` JSON Schema, browser e2e tests (Chrome DevTools), the viewer in the wheel
   and images once the engine is (PR3b), `examples/led555` as the smoke run (PR6b).
3. Before importing trials made by a yapnr checkout from before PR3a's format (legacy code keys,
   no `code_key_scheme`), keep a frozen copy of that pre-format tree (they re-key from it) or
   import with `--import-code-mismatch rebase` or `warn`; the default `error` refuses them.
   Splanc's frozen snapshot trees are not affected.
4. Owner: A/B Electrical221's two flags on the hierarchical engine before turning them on.
   Before the next experiment runs this engine, one rung-1 evaluation with it (`board.Delete`
   everywhere) when the Mac is free: the replay covered 5 of the 27 changed sites, and a full
   evaluation's nested workers exceed the two-KiCad-process budget kept while H7 runs.
5. Owner: push Splanc's `splanc-mini`, so the 9 newest `Imported-From` links of PR1 resolve (see the
   manifest).
6. atopile follow-up PRs (docs/frontends/atopile.md, "Not done yet"): A4 `/opt/atopile` in the
   `yapnr` image (the aarch64 wheel builder stage, notices, size), a KiCad 9 against 10 A/B and
   an end-to-end project that uses a stock `Library:Footprint`, then A5 to A8; after them the
   spec and checks (S1 to S4), the agents (R1 to R3, off by default) and fab outputs and ordering
   (F1, F2, O1 to O3; ordering stays staging-only: cart, quote or payment page).
7. Owner: move Splanc's builds to the part cache. Commit each board's `yapnr-parts.lock.json`
   (from the local locks), build with `yapnr atopile build` or `yapnr_atopile_build`, and only then
   remove the committed `elec/src/parts/`. Also commit or decide on `elec/layout/` (it seeds
   designators), and move `splanc_eol_tester`'s footprint into a part.
8. PR6a, then PR3b onwards: wire the 67 unwired engine test files with the glob macro (the two
   hygiene tests and PR3a's `test_feedback_signals` are wired; the two feedback generation tests
   skip without Splanc's Mini inputs) and replace the Splanc defaults and fixtures (manifest,
   "Known leftovers for PR3"). The KiCad-dependent tests run under the headless KiCad Python only
   (DEVELOPERS.md). PR3a landed before the KiCad lane: its format is checked by syntax-tree
   equivalence and fresh-interpreter imports, not by a KiCad-side test run. PR3b burns down the
   `.flake8` baseline and fixes the `via_coalesce` F821 with a test of its own.
9. PR-R is merged (#2). Its first `main` build pushes `yapnr-kicad:10.0.6-1-src`, then
   `yapnr-kicad:10.0.6-1` (with `10.0.6` and `10.0`), then `yapnr:edge`. Then make both GHCR
   packages **public** (package settings > Change visibility; irreversible, owner-approved). The
   organization must allow public packages first (Organization settings > Packages > Package
   creation: Public), or the option is missing. Check that an anonymous
   `docker pull ghcr.io/studio-fug/yapnr:edge` (and `yapnr-kicad:10.0.6-1-src` on arm64) works and
   that `gh attestation verify oci://ghcr.io/studio-fug/yapnr:edge -R Studio-Fug/yapnr` passes.
10. Create the release-note labels (`tools/release/create_labels.sh`), label open pull requests,
    and run the release dry run once (`gh workflow run release.yaml --ref main`); read the notes
    preview in its summary.
11. Uncomment the container and release badges in `README.md` once the image is public and v0.1.0
    exists; decide on immutable releases (docs/releases.md).
12. PR6a: the KiCad-side workers run under `/usr/bin/python3` (3.12), which cannot import yapnr
    (installed in the 3.11 venv only). Give them an import path with yapnr's pure-Python modules
    and none of the venv's compiled packages, and add a smoke check for it.
13. First release tag `v0.1.0` (owner) once the engine runs end to end inside the published image on
    both architectures (the PR6b example), after the `Image` run of that commit on `main` is green;
    make the `image` check required then.
14. Owner, fab and ordering (design §12): decisions D1 to D5. For the first OSH Park order, route
    the board under an OSH Park profile, run `yapnr order stage BOARD --vendor oshpark`, drop the
    zip on oshpark.com, check the preview and pay there; RF boards also pin `--stackup` (D4).
15. Owner decision (2026-10-07, `docs/decisions.md`): the `jlc-pofv` default flip. All 21 of
    `20261006-ladder-b0ec34`'s jlc-pofv failures pass on `claude/rails-settle` (the `-rails`
    cell was a plane-partition bug, fixed there), and the 45 gated cells pass 90 of 90 under
    `jlc-pofv` (88 under `legacy`), but the manual target rungs leave more open under it
    (`11-shove-channel-lm-14` 3 vs 2, `12-soc-bga-113` 51 vs 33 and 35 and one 3600 s
    timeout). Stays `legacy` until the owner says whether those count for "no rung worse".
16. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
    Dependabot `ubuntu` digest update (docs/releases.md, "Maintaining the images").
17. Showcases (`claude/animations-groups-hier`): the first nightly showcase step on the arm64
    runner (its runtime and its placements are unmeasured; `showcase-edge-io.webp` fits its
    2.5 MB budget at quality 70 by 450 bytes here, the encoder steps down where it must). The
    ladder's own animations still sweep single parts through half-turns (their timelines are
    pinned); move them to flips with the next deliberate refresh of `docs/animations/`.
18. Hard-rung gaps: merge `claude/gap-constraints` (region and align; it conflicts with the sides
    track in seven placement files) and rerun the `abs` and `rel` rungs; give the placement model
    an origin-to-courtyard offset (the THT header's origin is pin 1, so its courtyard is placed
    off by half its length and never legalizes); carry the three Ladder fixes (plane check, side
    check, HDI microvia) to `claude/ladder-hard-rungs`, or land the rungs and the fixes together.
19. Done (2026-10-07, owner: flip to native if no rung is worse and routes are equivalent): the C
    maze kernel is the default where its library loads (Bazel, the wheel and so the image, or a
    host compiler); CI runs `dense_maze_native_test` and `exact_route_native_test` in `//...`.
20. Boards with blind, buried or micro vias: the packed and native kernels hand them to the
    reference kernel (so no exact-separation recovery there), the length tuner only adds
    meanders, and the native KiCad repair loop and the hierarchical driver add through vias only.

## Blockers

- The Docker VM on the development Mac has a full disk (158 GB, 100 %), so the linux-aarch64
  container run of the atopile review fixes (wheel build, `setup` with the hashed sdist-build
  lock, end-to-end tests) and the part cache image smoke test could not run; the images there
  belong to other projects and were not pruned. The no-isolation sdist build was checked with
  the pinned CPython on macOS instead.

## Known issues

- `detail_route_test` (large, not in the PR lane) fails `test_drc_clean_by_construction` on the
  Mac at the branch base as well (a shared footprint cell on the `splanc_dev` fixture); not
  caused by this branch.

## Do not retry

- Exporting RF designs as marching-squares contours of the pixel centres (half-pixel chamfers):
  on finer grids that is other copper than the optimizer's, and the re-validation's resonances
  moved 1–2 % for it. Trace the pixel boundaries (`yapnr.rf.export.contour`).
- Widening a one-pixel neck at whichever facing void pixel comes first: next to other copper it
  leaves a one-pixel gap that the space pass closes, merging islands (the antenna's matched band
  fell from 19 to 4 %). `export.repair` checks the distance to other components.

- Letting a Bazel library under `yapnr/` go without `//yapnr:package`: rules_python then
  auto-creates empty `yapnr/__init__.py` files in its runfiles, and Bazel 7's sandbox reuse once
  wrote one through a symlink into the source tree (the file was truncated; six tests failed).

- Serving the viewer's static files after `Path.resolve()`: in Bazel runfiles every file is a
  symlink, so a resolved-path containment check rejects all of them. Check containment lexically.
- Pointing the atopile source index at Bazel runfiles: it skips symlinked `.ato` files on purpose;
  tests copy the fixture (`yapnr.viewer.testing.fixture_copy`).
- Resolving `requirements.lock` on linux-x86_64 with the default PyPI index: the torch wheel there
  needs CUDA libraries the lock does not carry. Resolve on darwin-arm64 or linux-aarch64 (PR6a
  adds a separate x86_64 lock).
- Changing imported code or adding allow markers so that the privacy scan passes: the owner chose
  to refine the scan's e-mail rule instead (`docs/decisions.md`).
- Running `git filter-repo` (or `gc`, or a branch switch) in the Splanc checkout: experiments run
  from it. Filter a scratch clone and remove its `origin` remote first (migration plan §7.5).
- Scrubbing imported machine paths in a later commit: the earlier commits would still publish them.
  Scrub each snapshot when it is staged, before its commit (PR2).
- Reproducing the `board.Remove` teardown SIGSEGV on synthetic boards: `Remove` and `Delete` both
  exit 0 there (zones, footprints, tracks, vias, any free order); the evidence is src12i's run.
  On H7's real boards `Remove` did not crash either (it leaks: SWIG's "no destructor found").
- Using SWIG's "memory leak ... no destructor found" messages in H7 logs to find the `Remove`
  sites a run reached: only some item types print it (shove's messages are `SHAPE_SEGMENT`,
  not `Remove`). Trace `BOARD.Delete`/`BOARD.Remove` calls instead.
- Making placement bitwise identical across macOS and Linux (thread count, deterministic
  algorithms, float64 `exp`/`log`): the thread count is already 1, deterministic mode changes
  nothing, and Adam's `addcmul` also rounds differently between the torch builds (#6).
- Comparing one seed's HPWL between placer variants, or across platforms: the per-seed spread
  (about 100 mm, 5 %) exceeds most effects; compare means over seeds on one platform (#6).
