# Releases and versioning

How yapnr is versioned, what a release publishes, and the owner's checklist for cutting one. The
workflows are `.github/workflows/release.yaml` and `.github/workflows/image.yaml`; the container
images are described in [containers](containers.md). Release notes live on
[GitHub Releases](https://github.com/Studio-Fug/yapnr/releases); there is no `CHANGELOG.md`.

## Versions

- **Release tags** are annotated `vMAJOR.MINOR.PATCH` tags ([SemVer 2.0](https://semver.org/)) on a
  commit of `main`. **Release candidates** are `vX.Y.Z-rc.N` (N from 1), always in that hyphen-dot
  form: a bare `vX.Y.Zrc1` would sort _above_ `vX.Y.Z` in SemVer and in Bazel. There are no alpha
  or beta tags.
- **The tag is the only version source.** `MODULE.bazel` has no `version`, and the sources carry
  none: the wheel is stamped at build time (`--stamp --embed_label=<version>` on
  `//release:wheel.dist`), and `yapnr --version` reads the installed wheel's metadata. A source
  tree that is not installed (`bazel run //:yapnr`) reports `0.0.0.dev0`.
- **Every other build is a development build**, versioned by `tools/release/version.py` from
  `git describe` so that it sorts after the release it descends from:

  | HEAD                               | Version (PEP 440)          | Example               |
  | ---------------------------------- | -------------------------- | --------------------- |
  | at `vX.Y.Z`                        | `X.Y.Z`                    | `0.3.1`               |
  | at `vX.Y.Z-rc.N`                   | `X.Y.ZrcN`                 | `0.4.0rc1`            |
  | N commits after `vX.Y.Z`           | `X.Y.(Z+1).devN+g<sha>`    | `0.3.2.dev5+gab12cd3` |
  | N commits after `vX.Y.Z-rc.K`      | `X.Y.Zrc(K+1).devN+g<sha>` | `0.4.0rc2.dev3+g...`  |
  | no release tag yet, N commits      | `0.0.0.devN+g<sha>`        | `0.0.0.dev7+g...`     |
  | uncommitted changes (local builds) | `...+g<sha>.dirty`         |                       |

  `tools/release/version.py --field pep440` prints the version of a checkout; without `--field`
  it prints everything, including the image tags for a given `--ref`.

### What bumps what

| Change                                                                   | While 0.x | From 1.0 |
| ------------------------------------------------------------------------ | --------- | -------- |
| Breaking change to the public surface (below)                            | MINOR     | MAJOR    |
| `results-change`: the same inputs, seed and flags give a different board | MINOR     | MINOR    |
| Store layout change with an automatic migration                          | MINOR     | MINOR    |
| New feature, backward compatible                                         | PATCH     | MINOR    |
| Fix, pin bump without a result change, KiCad patch update in the image   | PATCH     | PATCH    |
| New default KiCad major version (boards upgrade irreversibly)            | MINOR     | MAJOR    |
| Documentation only                                                       | none      | none     |

SemVer allows anything to change in 0.y.z; yapnr uses MINOR for breaking and results-changing
releases while it is 0.x, so pinning `X.Y` is safe. No image tag `0` is ever published.

**The public surface:** the CLI (commands, flags, `--json` output, exit codes); `yapnr.toml`
(`yapnr-project-v1`) and the store `layout_version` without an automatic migration; the bundle
format `yapnr-bundle-v1`; the `@yapnr//bazel:defs.bzl` rules and providers; the telemetry and notes
schemas; and the container interface (entrypoint, environment variables, `/project`, the viewer
port 8781, the UID 1000 default).

Proposed criteria for 1.0.0: the manifest and store layout unchanged (incompatibly) for two
minors, the Bazel ruleset through one full release cycle with Splanc's boards, and the container
interface documented.

## Labels and release notes

GitHub generates the notes from the merged pull requests, grouped by label
(`.github/release.yml`); a pull request lands in the first matching category. Every pull request
gets **one area label**, and two more when they apply:

| Label                                                                       | Meaning                                            |
| --------------------------------------------------------------------------- | -------------------------------------------------- |
| `engine`, `viewer`, `cli`, `bazel`, `release`, `docs`, `ci`, `dependencies` | the area                                           |
| `breaking`                                                                  | breaks the public surface (listed first)           |
| `results-change`                                                            | same inputs, seed and flags give a different board |
| `skip-changelog`                                                            | leave the pull request out of the notes            |

`tools/release/create_labels.sh` creates them (idempotent). Agents add labels with
`gh pr edit <N> --add-label <label>`; the pull request template has a reminder.

A header written by `tools/release/notes_header.py` comes first: `docker pull` by tag and by
digest, the wheel with the PyTorch CPU index, the Bazel `archive_override` snippet with its
`integrity`, the bundled KiCad version, and a link to `docs/releases/vX.Y.md` when that file
exists. Final releases compare against the previous _stable_ tag, so the notes cover the whole RC
cycle.

## What a release publishes

| What                           | Where                                                           |
| ------------------------------ | --------------------------------------------------------------- |
| `yapnr-vX.Y.Z.tar.gz`          | release asset: `git archive` of the tag (Bazel, AGPL source)    |
| `yapnr-X.Y.Z-py3-none-any.whl` | release asset: the stamped wheel                                |
| `yapnr-docs-X.Y.Z.tar.gz`      | release asset: the documentation site                           |
| `SHA256SUMS`                   | release asset: checksums of the above                           |
| `ghcr.io/studio-fug/yapnr`     | `X.Y.Z`, `X.Y`, `latest` (see [containers](containers.md#tags)) |

Every release file and image is attested; check one with
`gh attestation verify <file or oci://image> -R Studio-Fug/yapnr`. The images also carry an SBOM
and provenance. Consume a release from Bazel
through the uploaded archive (GitHub's automatic `/archive/` tarballs are not byte-stable):

```starlark
bazel_dep(name = "yapnr", version = "0.1.0")   # ignored under the override; kept so that
archive_override(                              # moving to the BCR = deleting the override
    module_name = "yapnr",
    urls = ["https://github.com/Studio-Fug/yapnr/releases/download/v0.1.0/yapnr-v0.1.0.tar.gz"],
    integrity = "sha256-<from the release notes>",
    strip_prefix = "yapnr-0.1.0",
)
```

## What the workflows do

`image.yaml` (see [containers](containers.md#building-the-images-locally)):

- **Pull requests:** build both images for both architectures on native runners (no push) and
  smoke-test them. A `plan` job skips the rest when nothing that goes into an image changed, so
  the workflow always reports. Not a required check until v0.1.0.
- **Pushes to `main`:** the same, then push `ghcr.io/studio-fug/yapnr:edge` and `:sha-<7>`, with
  the version `X.Y.(Z+1).devN+g<sha>` (`0.0.0.devN+g<sha>` before the first tag). The KiCad base is
  pushed the first time its tag (`docker/yapnr-kicad/TAG`) is built, with its `-src` image, and is
  never overwritten.
- **Releases** (called by `release.yaml`): the same, with the release tags.

`release.yaml` runs when a `v*` tag is pushed:

1. **verify:** the tag is annotated, matches `vX.Y.Z` or `vX.Y.Z-rc.N`, and its commit is on
   `main` (or a `release/X.Y` branch); computes the version, the previous stable tag, and whether
   the release becomes `latest` (a backport such as 0.2.5 never takes `latest` from 0.3.0).
2. **ci:** all of `ci.yaml` at the tag (lint, test, docs).
3. **image:** `image.yaml`, publishing `X.Y.Z`, `X.Y` and `latest` (release candidates:
   `X.Y.Z-rc.N` only).
4. **release:** assembles the files above, attests them, creates a **draft** GitHub release with
   the files and the notes (a prerelease for `-rc` tags), then publishes it. Nothing follows the
   publish step, so the workflow works with immutable releases.

**Dry run:** Actions > Release > Run workflow (on any branch), or
`gh workflow run release.yaml --ref <branch>`. It runs every job and uploads the release files as a
workflow artifact, and publishes nothing: no image tags, no GitHub release.

## Release checklist (owner)

Only the owner creates `v*` tags; a tag ruleset enforces it. Agents prepare release pull requests
and never create or push tags.

1. **Release pull request** (for minors; optional for patches), titled `release: vX.Y.Z`:
   upgrade notes in `docs/releases/vX.Y.md` for `breaking` and `results-change` items, the new
   `X.Y` tag in the README and the container docs' examples, and a `WORKLOG.md` entry.
2. **Dry run** on `main` after it merges: `gh workflow run release.yaml --ref main`; it must be
   green.
3. **Release candidate** (required for a MINOR with `breaking` or `results-change` items, or a
   store layout change; patches go straight to step 5):

   ```sh
   git fetch origin && git checkout --detach origin/main
   git tag -s v0.4.0-rc.1 -m "yapnr 0.4.0-rc.1"      # -a if tags are not signed
   git push origin v0.4.0-rc.1
   ```

4. **Regression on the candidate:** the full regression ladder (`//tests/regression:ladder`, from
   PR6b), the nightly macOS KiCad lane, and the published image's smoke test on both architectures
   (`tools/image/smoke_image.sh ghcr.io/studio-fug/yapnr:0.4.0-rc.1` after a `docker pull`). A fix
   means a new candidate (`-rc.2`) from `main`.
5. **Final tag** on the same commit as the last candidate:

   ```sh
   git tag -s v0.4.0 -m "yapnr 0.4.0" <commit of v0.4.0-rc.N>
   git push origin v0.4.0
   ```

6. **Check the release:** the workflow is green, the GitHub release has four files and the install
   header, `docker pull ghcr.io/studio-fug/yapnr:0.4` works anonymously, and
   `gh attestation verify oci://ghcr.io/studio-fug/yapnr:0.4.0 -R Studio-Fug/yapnr` passes.

A bad release is superseded by the next patch release; tags are never moved or reused. Fix forward
on `main`; a `release/X.Y` branch is created only when a backport is actually needed.

## One-time repository settings

- **Tag ruleset** on `refs/tags/v*` (in place): restricts creating, updating and deleting release
  tags to the owner.
- **Package visibility:** new GHCR packages start **private**. After the first push of
  `ghcr.io/studio-fug/yapnr` and `ghcr.io/studio-fug/yapnr-kicad` (the first `main` build after
  this workflow lands), make both public (package settings > Danger Zone > Change visibility).
  The switch cannot be undone. Until then, anonymous `docker pull` fails.
- **Labels:** `tools/release/create_labels.sh`.
- **Immutable releases** (Settings > General > Releases): recommended. The release workflow
  publishes last, so it works with them on.
- **The `image` check** becomes required with v0.1.0 (it summarizes `image.yaml`).
