# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-29 (PR-R).

## In progress

- **PR-R, releases and container images** (branch `claude/pr-r-release-container`): the release
  tag as the only version source (`tools/release/version.py`, no version in `MODULE.bazel`), the
  stamped wheel (`//release:wheel`), the images `ghcr.io/studio-fug/yapnr-kicad` and
  `ghcr.io/studio-fug/yapnr` (`docker/`, smoke test `tools/image/smoke_image.sh`), the `Image`
  and `Release` workflows, release-note categories, and [releases](docs/releases.md) and
  [containers](docs/containers.md) docs. Both images were built and smoke-tested locally on arm64;
  amd64 is first built by CI on the pull request.
- **PR1, engine import with history** (branch `claude/pr1-engine-history`).

## Next

1. Merge PR-R; its first `main` build pushes `yapnr-kicad:10.0.6-1`, `yapnr-kicad:10.0.6-1-src`
   and `yapnr:edge`. Then make both GHCR packages **public** (package settings > Change
   visibility; irreversible, owner-approved), and check that an anonymous
   `docker pull ghcr.io/studio-fug/yapnr:edge` works and that
   `gh attestation verify oci://ghcr.io/studio-fug/yapnr:edge -R Studio-Fug/yapnr` passes.
2. Create the release-note labels (`tools/release/create_labels.sh`), label open pull requests,
   and run the release dry run once (`gh workflow run release.yaml --ref main`).
3. Uncomment the container and release badges in `README.md` once the image is public and v0.1.0
   exists; decide on immutable releases (docs/releases.md).
4. PR1: import the engine with history from Splanc (`git filter-repo` on a scratch clone, privacy
   scan over the whole filtered history before any push). See the
   [migration plan](docs/migration-plan.md#8-pr-sequence).
5. First release tag `v0.1.0` (owner) once the engine runs end to end inside the published image on
   both architectures (the PR6b example); make the `image` check required then.

## Blockers

- None.

## Do not retry

- Resolving `requirements.lock` on linux-x86_64 with the default PyPI index: the torch wheel there
  needs CUDA libraries the lock does not carry. Resolve on darwin-arm64 or linux-aarch64 (PR6a
  adds a separate x86_64 lock).
