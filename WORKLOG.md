# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (PR4 and #10 merged; ladder animations merged with main; line groups,
hard board edges and the hierarchical ladder driver on `claude/animations-groups-hier`).

## In progress

- **Merged today:** PR3a (#9, code key scheme 2 and the engine format), the viewer (#12, PR4:
  `yapnr/viewer`, `bazel run //:viewer -- --root <live>`, paid features off by default, elkjs and
  three.js fetched pinned; guide [docs/viewer.md](docs/viewer.md), choices in
  [docs/decisions.md](docs/decisions.md)), and #13 (an explicit `started.json` stamp for the
  routing-feedback staleness check, fixing #10 on Linux). The format commits of #9 and #12 are in
  `.git-blame-ignore-revs` (#11, #14).
- **Branch in progress:** `claude/atopile-toolchain` (atopile 0.15.8 without Nix, the offline
  part picker, a part cache server seeded locally); it branches from before PR3a and merges
  `main` before its pull request.
- **Regression ladder in CI and PnR animations** (branch `claude/ladder-animations`; `main`
  merged in, the engine files it hooks re-formatted as PR3a did): `pnr.trace` (opt-in
  `PNR_TRACE_DIR`, format `pnr-trace-v1`; `trace_noop_test` and a traced/untraced ladder A/B show
  byte-identical results), `pnr.trace_board` (stdlib
  `.kicad_pcb` reader), `pnr.provenance` (critical path; halving runs in coarse mode),
  `pnr.animate` (Pillow; WebP, GIF, optional MP4), `run.py --trace` and `--fab-profile` (default
  `legacy`, the fixtures' own rules), `//hardware/pnr:animate` and `:ladder_animations`, the
  informational `ladder.yaml` lane, and the docs: `docs/regression-ladder.md` with all eight
  animations in `docs/animations/` and the 555 flasher (`05-timer-led-10`) GIF in `README.md`.
  All eight cases pass (traced pool run, seed 0; baseline seeds 0 and 1, the nightly lane's
  configuration: 16 of 16, the vias and copper of RESULTS-118). The review's findings are fixed;
  the container path of `ladder.yaml` has still never run (the image is private and the Mac's
  tokens lack `read:packages`).

- **Constraint and hierarchy showcases** (branch `claude/animations-groups-hier`, on top of
  `claude/ladder-animations`; design `docs/design/constraint-and-hier-animations.md`, §10 "As
  built"). Engine side done, all opt-in: a HARD `line_group` constraint placed as one rigid
  macro inside `place()`, `edge_align` `hard`/`tolerance_mm`, own-net fixed copper in the
  detail router, and `regression/hier_case.py` (blocks placed and routed on their own boards,
  placed as macros, knitted with block copper held fixed). Traces gain `groups`,
  `group_members`, `header.constraints`, a `fixed` event and per-template block traces.
  `run.py --showcases` and `--trace-placement-every`; `designs.showcases()`: `line-chaser-20`,
  `edge-io-12-free`, `edge-io-12`, `hier-twin-bank-32` (32 parts: the draft's "31" miscounted).
  Showcase run (pool seed 0, 8 starts, 3 finalists, snapshots every 5): all five cases pass the
  gate and the constraint audit, 0 opens, 0 findings; vias 19 (07), 20 (line), 9 (edge free),
  8 (edge), 38 (hier); 123, 128, 65, 35 and 179 s. Placement and routes are identical across
  two runs. A/B of the eight ladder cases (branch base against this engine, traced pool run):
  identical `placed.json`, `routes.json` and trace digests, 8 of 8 pass. Next: implementer B
  (renderer, comparison and hierarchical storyboard, docs page, README item, manifest,
  `ladder.yaml` nightly step).

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
6. PR6a, then PR3b onwards: wire the 67 unwired engine test files with the glob macro (the two
   hygiene tests and PR3a's `test_feedback_signals` are wired; the two feedback generation tests
   skip without Splanc's Mini inputs) and replace the Splanc defaults and fixtures (manifest,
   "Known leftovers for PR3"). The KiCad-dependent tests run under the headless KiCad Python only
   (DEVELOPERS.md). PR3a landed before the KiCad lane: its format is checked by syntax-tree
   equivalence and fresh-interpreter imports, not by a KiCad-side test run. PR3b burns down the
   `.flake8` baseline and fixes the `via_coalesce` F821 with a test of its own.
7. PR-R is merged (#2). Its first `main` build pushes `yapnr-kicad:10.0.6-1-src`, then
   `yapnr-kicad:10.0.6-1` (with `10.0.6` and `10.0`), then `yapnr:edge`. Then make both GHCR
   packages **public** (package settings > Change visibility; irreversible, owner-approved). The
   organization must allow public packages first (Organization settings > Packages > Package
   creation: Public), or the option is missing. Check that an anonymous
   `docker pull ghcr.io/studio-fug/yapnr:edge` (and `yapnr-kicad:10.0.6-1-src` on arm64) works and
   that `gh attestation verify oci://ghcr.io/studio-fug/yapnr:edge -R Studio-Fug/yapnr` passes.
8. Create the release-note labels (`tools/release/create_labels.sh`), label open pull requests,
   and run the release dry run once (`gh workflow run release.yaml --ref main`); read the notes
   preview in its summary.
9. Uncomment the container and release badges in `README.md` once the image is public and v0.1.0
   exists; decide on immutable releases (docs/releases.md).
10. PR6a: the KiCad-side workers run under `/usr/bin/python3` (3.12), which cannot import yapnr
    (installed in the 3.11 venv only). Give them an import path with yapnr's pure-Python modules
    and none of the venv's compiled packages, and add a smoke check for it.
11. First release tag `v0.1.0` (owner) once the engine runs end to end inside the published image on
    both architectures (the PR6b example), after the `Image` run of that commit on `main` is green;
    make the `image` check required then.
12. Owner: decide whether the ladder should default to `--fab-profile jlc-pofv` (the engine's
    default JLCPCB profile) instead of `legacy` (the fixtures' own rules; `docs/decisions.md`).
    With `route_case.py` applying the profile, `jlc-pofv` passes every case too (2026-09-30:
    pool seed 0, 8 of 8; baseline seeds 0 and 1, 16 of 16; other boards than legacy's, e.g. case
    07 with 17 vias instead of 19). Flipping it changes the rules the ladder README states and
    needs a refresh of `docs/animations/`. The first `ladder.yaml` run (the pull request of
    `claude/ladder-animations`) is the first run of its container path.
13. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
    Dependabot `ubuntu` digest update (docs/releases.md, "Maintaining the images").

## Blockers

None.

## Known issues

- `detail_route_test` (large, not in the PR lane) fails `test_drc_clean_by_construction` on the
  Mac at the branch base as well (a shared footprint cell on the `splanc_dev` fixture); not
  caused by this branch.

## Do not retry

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
