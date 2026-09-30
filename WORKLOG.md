# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (engine hygiene; ladder animations).

## In progress

- **Engine hygiene** (branch `claude/engine-hygiene`, not pushed), the PR3 leftovers of PR2
  (merged, #7): Electrical221's cleanup gated default off (`PNR_PARTIAL_CYCLE_CLEANUP`,
  `PNR_BARREL_CONTACT_BRIDGES`; the default is src15 again), the `board.Remove` audit (27 calls
  now `board.Delete`, 3 kept; `board_delete_test` checks each call; replayed on H7's real boards
  for five sites with identical results), every engine subprocess bounded through `pnr.proc`
  (`PNR_WORKER_TIMEOUT`, `PNR_PHASE_TIMEOUT`, `PNR_EVALUATION_TIMEOUT`; 0 means no limit; a
  timeout kills the child's whole process tree; a deadline kill is not retried; `proc_test`),
  CI's history scan with merge diffs (`git log -p --diff-merges=separate`), and
  `orientation_test` re-enabled as `large` (#6: placement is deterministic per platform only, so
  it compares a three-seed mean with a 5 % margin, a coarse guard, and checks known best angles
  on a synthetic board; passes on macOS and linux-aarch64). The review's findings are fixed in
  follow-up commits on the branch. Reasons and limits: [docs/decisions.md](docs/decisions.md);
  struck leftovers:
  [docs/history/import-manifest.md](docs/history/import-manifest.md#known-leftovers-for-pr3).

- **Regression ladder in CI and PnR animations** (branch `claude/ladder-animations`, not
  pushed): `pnr.trace` (opt-in `PNR_TRACE_DIR`, format `pnr-trace-v1`; `trace_noop_test` and a
  traced/untraced ladder A/B show byte-identical results), `pnr.trace_board` (stdlib
  `.kicad_pcb` reader), `pnr.provenance` (critical path), `pnr.animate` (Pillow; WebP, GIF,
  optional MP4), `run.py --trace`, `//hardware/pnr:animate` and `:ladder_animations`, and the
  informational `ladder.yaml` lane. Not yet done: `docs/animations/`, the docs page and the
  README animation.

## Next

1. Owner: review and push the hygiene branch and open its PR; CI must be green (it runs
   `orientation_test` again; close #6 with it).
2. Owner: A/B Electrical221's two flags on the hierarchical engine before turning them on.
   Before the next experiment runs this engine, one rung-1 evaluation with it (`board.Delete`
   everywhere) when the Mac is free: the replay covered 5 of the 27 changed sites, and a full
   evaluation's nested workers exceed the two-KiCad-process budget kept while H7 runs.
3. Owner: push Splanc's `splanc-mini`, so the 9 newest `Imported-From` links of PR1 resolve (see the
   manifest).
4. PR6a, then PR3: wire the 68 unwired engine test files with the glob macro (the two hygiene tests
   are wired) and replace the Splanc defaults and fixtures (manifest, "Known leftovers for PR3").
   The KiCad-dependent tests run under the headless KiCad Python only (DEVELOPERS.md).
5. PR-R is merged (#2). Its first `main` build pushes `yapnr-kicad:10.0.6-1-src`, then
   `yapnr-kicad:10.0.6-1` (with `10.0.6` and `10.0`), then `yapnr:edge`. Then make both GHCR
   packages **public** (package settings > Change visibility; irreversible, owner-approved). The
   organization must allow public packages first (Organization settings > Packages > Package
   creation: Public), or the option is missing. Check that an anonymous
   `docker pull ghcr.io/studio-fug/yapnr:edge` (and `yapnr-kicad:10.0.6-1-src` on arm64) works and
   that `gh attestation verify oci://ghcr.io/studio-fug/yapnr:edge -R Studio-Fug/yapnr` passes.
6. Create the release-note labels (`tools/release/create_labels.sh`), label open pull requests,
   and run the release dry run once (`gh workflow run release.yaml --ref main`); read the notes
   preview in its summary.
7. Uncomment the container and release badges in `README.md` once the image is public and v0.1.0
   exists; decide on immutable releases (docs/releases.md).
8. PR6a: the KiCad-side workers run under `/usr/bin/python3` (3.12), which cannot import yapnr
   (installed in the 3.11 venv only). Give them an import path with yapnr's pure-Python modules
   and none of the venv's compiled packages, and add a smoke check for it.
9. First release tag `v0.1.0` (owner) once the engine runs end to end inside the published image on
   both architectures (the PR6b example), after the `Image` run of that commit on `main` is green;
   make the `image` check required then.
10. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
    Dependabot `ubuntu` digest update (docs/releases.md, "Maintaining the images").

## Blockers

- The regression ladder fails KiCad's gate on `main` at 2b8e52d for cases 04 to 08 (seed 0,
  baseline and initial pool alike): 40 findings of the rule `jlc-pofv_via_to_smd_pad` (0.127 mm),
  which writeback writes into each project because `PNR_FAB_PROFILE` defaults to `jlc-pofv`
  (`pnr.fab_profile`, src8b). The ladder routes with `compile_routing_rules` output, whose `fab`
  block has no `via_to_smd_pad_mm`, so the router does not keep vias off SMD pads; the boards
  equal RESULTS-118's (same vias and copper). With `PNR_FAB_PROFILE=legacy` (passed through a
  scratch copy of `run.py`) cases 04 to 08 pass. Owner: route the ladder under the profile it is
  judged by (apply the profile to the rules in `route_case.py`) or run it with the legacy
  profile; the `ladder` lane and passing animations need one of them.

## Do not retry

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
