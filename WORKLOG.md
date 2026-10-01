# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-10-01 (the RF review fixes and re-run cases on `claude/rf-topopt`; before
that PR4, #10, the ladder animations, the atopile toolchain and the privacy-scan trailer rule
merged; line groups, hard board edges, the hierarchical ladder driver and their animations on
`claude/animations-groups-hier`).

## In progress

- **RF microstrip inverse design** (#29, branch `claude/rf-topopt`; design
  [docs/design/rf-topology-optimization.md](docs/design/rf-topology-optimization.md), guide
  [docs/rf-inverse-design.md](docs/rf-inverse-design.md), choices in
  [docs/decisions.md](docs/decisions.md)). Done: the solver (`yapnr.rf`: Yee FDTD, CPML, line
  ports, exact adjoint; gradients to 1.5e-8), the optimizer (material grid, conic filter, tanh
  projection, Zhou length scale, own MMA with the epigraph, robust variants, a reactive
  interpolation, specs with lumped resistors, checkpoints, the best binarized design exported,
  net-tie `.kicad_mod` with rule areas, Touchstone, result JSON, the animation) and five
  end-to-end cases re-validated from the footprint on three grids. After the physics and design
  reviews (port de-embedding, conservative MMA, export of the best design, validation) the
  divider passes its criteria on all three grids (|S11| ≤ −17.7 dB, |S21| ≥ −3.34 dB);
  the diplexer from the stub seed comes close (on the fine grid all but channel B's rejection,
  0.2 dB short), but the Wilkinson-type combiner (output match −11 dB, isolation −13 dB), the
  three-channel bank and the antenna do not. The antenna's export is the tuned closed-form
  patch (the optimizer did not change it) and the patch class cannot meet its band on S2 with
  the 2 % coarse-to-fine shift: a re-scope is the owner's call. Artifacts in `docs/rf/`.
  Next: the antenna's scope, a copper-edge correction in the solver, a `yapnr rf` CLI, an
  external (Meep or openEMS) cross-check, footprints in PnR.

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
14. Owner: decide whether the ladder should default to `--fab-profile jlc-pofv` (the engine's
    default JLCPCB profile) instead of `legacy` (the fixtures' own rules; `docs/decisions.md`).
    With `route_case.py` applying the profile, `jlc-pofv` passes every case too (2026-09-30:
    pool seed 0, 8 of 8; baseline seeds 0 and 1, 16 of 16; other boards than legacy's, e.g. case
    07 with 17 vias instead of 19). Flipping it changes the rules the ladder README states and
    needs a refresh of `docs/animations/`. The first `ladder.yaml` run (the pull request of
    `claude/ladder-animations`) is the first run of its container path.
15. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
    Dependabot `ubuntu` digest update (docs/releases.md, "Maintaining the images").
16. Showcases (`claude/animations-groups-hier`): the first nightly showcase step on the arm64
    runner (its runtime and its placements are unmeasured; `showcase-edge-io.webp` fits its
    2.5 MB budget at quality 70 by 450 bytes here, the encoder steps down where it must). The
    ladder's own animations still sweep single parts through half-turns (their timelines are
    pinned); move them to flips with the next deliberate refresh of `docs/animations/`.

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
