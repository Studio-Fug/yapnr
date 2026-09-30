# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (PR4, the viewer; PR3a merged).

## In progress

- **PR4, the live viewer** (branch `claude/pr4-viewer`): the deployed Splanc viewer
  ported to `yapnr/viewer` (move, scrubbed imports, format, packaging and configuration, lint,
  fetched JavaScript and Bazel targets, tests and docs). `bazel run //:viewer -- --root <live>`;
  configuration by flags or a `yapnr-viewer-v1` TOML file, no machine defaults; the Ask agent,
  its web tools and AI net labels are off unless enabled; elkjs 0.9.3 and three.js 0.186.1 are
  fetched pinned by sha256 into `//yapnr/viewer:dist`; 11 offline unit test targets on a
  synthetic atopile fixture, live checks manual in `tests/e2e/viewer`. Choices:
  [docs/decisions.md](docs/decisions.md) ("Choices made in PR4"); guide:
  [docs/viewer.md](docs/viewer.md). Checked against a copy of three lanes of a live experiment
  (`bazel run //:viewer`, agent off): PCB, schematic, 3D (headless `kicad-cli`), Source, Inspect
  and Notes work, and the Ask tab says it is off. Review fixes: one spend meter for Ask turns and
  net labels (a call holds its budget while it runs; stopped turns count at their budget),
  library defaults off, a server test of the defaults, the Apache-2.0 text served next to elkjs,
  the Source link to the exact tree (marked modified), interface addresses on Linux, no Ask
  buttons with the agent off, no source-index request without sources, and clearer messages (3D
  without three.js, cost replay of a board outside the run). Merged with `main` after PR3a: the
  old `hardware/tools/pnr_live` copies PR3a reformatted are dropped (PR4 moved and formatted
  them itself).
- **PR3a is merged** (#9, a merge commit): code key scheme 2 (routing-feedback code keys hash each
  module's canonical syntax tree, so formatting does not change them; legacy records are still
  read), black and isort over every Python file under `hardware/` as mechanical commits, and the
  lint config (`.flake8` per-file baseline, `.isort.cfg`). Its three format commits go into
  `.git-blame-ignore-revs` in #11. Proof and reasons: [docs/decisions.md](docs/decisions.md)
  ("Choices made in PR3a").

## Next

1. Owner (PR4): review the viewer branch; decide the agent's default model (opus, $2 per turn,
   $20 per process) and file the issue for `pnr.capacitor_intent` (the cost replay reports
   contexts that need it as unavailable). If PR4 is merged with a merge commit, list its move and
   format commits in `.git-blame-ignore-revs` afterwards (a squash folds them away).
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
12. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
    Dependabot `ubuntu` digest update (docs/releases.md, "Maintaining the images").

## Blockers

- None.

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
