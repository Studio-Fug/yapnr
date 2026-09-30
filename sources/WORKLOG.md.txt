# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (PR1).

## In progress

- **PR1, engine import with history** (branch `claude/pr1-import`, not pushed): the refined e-mail
  rule of the privacy scan (owner decision in `docs/decisions.md`: an `@` match is an address only
  with a letter or digit in its local part, not followed by `(`, at an IANA top-level domain), then
  the committed Splanc history of the PnR paths (42 commits, filtered with `git filter-repo` in a
  scratch clone, source code unchanged) merged with `--allow-unrelated-histories`, then an
  adaptation commit (`@yapnr_pypi` load, a minimal `hardware/tools/BUILD.bazel`, four failing
  tests tagged `manual`: three fail on the Splanc source commit too, one fails and runs at or past
  its timeout),
  then the manifest and docs. Scrub, counts and checks:
  [docs/history/import-manifest.md](docs/history/import-manifest.md). **Merge with a merge
  commit**, never squash.

## Next

1. Owner: review and push PR1 and open it; CI must be green (`lint` scans all its new commits).
   Merge on GitHub with a merge commit, authored with the owner's public commit address.
2. Owner: push Splanc's `splanc-mini`, so the 9 newest `Imported-From` links resolve (see the
   manifest).
3. PR2a/PR2b: the uncommitted engine state as a commit series from the snapshots (migration plan
   §7.3), with the same privacy gates as PR1.
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
