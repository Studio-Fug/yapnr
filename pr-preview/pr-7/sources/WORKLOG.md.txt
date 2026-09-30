# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (PR2).

## In progress

- **PR2, newer engine state** (branch `claude/pr2-hier-engine`, not pushed): the engine state that
  was never committed in Splanc, as one commit per snapshot (engine tree equal to the snapshot,
  four files scrubbed at staging): F217 (the Codex agent's working-tree work), the hierarchical
  line src8b to src14, the USB pair (src12b, src13) and N-0001 (src12n) side lines joined by an
  octopus merge (src15.r1), src15 (the H7 engine), then Electrical221 merged in (two resolved
  conflicts), the Bazel adaptation and the docs. `bazel test //...` 100/100; three budget tests,
  `detail_route_test` and `orientation_test` stay `manual`. Lineage, scrub, checks and the PR3
  leftovers: [docs/history/import-manifest.md](docs/history/import-manifest.md#pr2-the-uncommitted-engine-state).
  **Merge with a merge commit**, never squash; the commits before the adaptation do not build.

## Next

1. Owner: review and push PR2 and open it; CI must be green. Decide on Electrical221's default-on
   cleanup (gate it or A/B it; decisions) and, optionally, on making CI's history scan include
   merge diffs (`git log -p -m`): its current form shows no diff for PR2's two merges (both were
   scanned with `-m` locally).
2. Owner: push Splanc's `splanc-mini`, so the 9 newest `Imported-From` links of PR1 resolve (see the
   manifest).
3. PR6a, then PR3: wire the 68 unwired engine test files with the glob macro, bound the workers
   that have no timeout, and replace the Splanc defaults and fixtures (manifest, "Known leftovers
   for PR3"). PR2e: the `board.Remove` audit.
4. PR-R is merged (#2). Its first `main` build pushes `yapnr-kicad:10.0.6-1-src`, then
   `yapnr-kicad:10.0.6-1` (with `10.0.6` and `10.0`), then `yapnr:edge`. Then make both GHCR
   packages **public** (package settings > Change visibility; irreversible, owner-approved). The
   organization must allow public packages first (Organization settings > Packages > Package
   creation: Public), or the option is missing. Check that an anonymous
   `docker pull ghcr.io/studio-fug/yapnr:edge` (and `yapnr-kicad:10.0.6-1-src` on arm64) works and
   that `gh attestation verify oci://ghcr.io/studio-fug/yapnr:edge -R Studio-Fug/yapnr` passes.
5. Create the release-note labels (`tools/release/create_labels.sh`), label open pull requests,
   and run the release dry run once (`gh workflow run release.yaml --ref main`); read the notes
   preview in its summary.
6. Uncomment the container and release badges in `README.md` once the image is public and v0.1.0
   exists; decide on immutable releases (docs/releases.md).
7. PR6a: the KiCad-side workers run under `/usr/bin/python3` (3.12), which cannot import yapnr
   (installed in the 3.11 venv only). Give them an import path with yapnr's pure-Python modules
   and none of the venv's compiled packages, and add a smoke check for it.
8. First release tag `v0.1.0` (owner) once the engine runs end to end inside the published image on
   both architectures (the PR6b example), after the `Image` run of that commit on `main` is green;
   make the `image` check required then.
9. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
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
