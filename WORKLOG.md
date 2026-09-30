# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (engine hygiene).

## In progress

- **Engine hygiene** (branch `claude/engine-hygiene`, not pushed), the PR3 leftovers of PR2
  (merged, #7): Electrical221's cleanup gated default off (`PNR_PARTIAL_CYCLE_CLEANUP`,
  `PNR_BARREL_CONTACT_BRIDGES`; the default is src15 again), the `board.Remove` audit (27 calls
  now `board.Delete`, 3 kept; `board_delete_test`), every engine subprocess bounded through
  `pnr.proc` (`PNR_WORKER_TIMEOUT`, `PNR_PHASE_TIMEOUT`, `PNR_EVALUATION_TIMEOUT`; `proc_test`),
  and CI's history scan with merge diffs (`git log -p -m`). Reasons and limits:
  [docs/decisions.md](docs/decisions.md); struck leftovers:
  [docs/history/import-manifest.md](docs/history/import-manifest.md#known-leftovers-for-pr3).

## Next

1. Owner: review and push the hygiene branch and open its PR; CI must be green.
2. Owner: A/B Electrical221's two flags on the hierarchical engine before turning them on.
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

- None.

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
