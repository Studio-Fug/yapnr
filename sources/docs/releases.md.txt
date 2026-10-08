# Releases and versioning

How yapnr is versioned, what a release publishes, and the owner's checklist for cutting one. The
workflows are `.github/workflows/release.yaml` and `.github/workflows/image.yaml`; the container
images are described in [containers](containers.md). Release notes live on
[GitHub Releases](https://github.com/Studio-Fug/yapnr/releases); there is no `CHANGELOG.md`.

## Versions

- **Release tags** are annotated `vMAJOR.MINOR.PATCH` tags ([SemVer 2.0](https://semver.org/)) on a
  commit of `main`. **Release candidates** are `vX.Y.Z-rc.N` (N from 1), always in that hyphen-dot
  form: a bare `vX.Y.Zrc1` is not a SemVer version at all, and Bazel's version ordering would put
  it _above_ `vX.Y.Z`. There are no alpha or beta tags; the workflows ignore tags of any other
  shape (`tools/release/version.py` skips them when it looks for the nearest release).
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

| What                                                       | Where                                                                                                |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `yapnr-vX.Y.Z.tar.gz`                                      | release asset: `git archive` of the tag (Bazel, AGPL source)                                         |
| `yapnr-X.Y.Z-py3-none-manylinux_2_34_{x86_64,aarch64}.whl` | release assets: the stamped wheels, one per Linux architecture (with yapnr.rf's native FDTD library) |
| `yapnr-docs-X.Y.Z.tar.gz`                                  | release asset: the documentation site                                                                |
| `SHA256SUMS`                                               | release asset: checksums of the above                                                                |
| Both Linux wheels                                          | PyPI project `yapnr`, from the same tested Bazel artifacts                                           |
| `ghcr.io/studio-fug/yapnr`                                 | `X.Y.Z`, `X.Y`, `latest` (see [containers](containers.md#tags))                                      |

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
- **Pushes to `main`** that change what goes into an image: the same, then push
  `ghcr.io/studio-fug/yapnr:edge` and `:sha-<7>`, with the version `X.Y.(Z+1).devN+g<sha>`
  (`X.Y.Zrc(K+1).devN+g<sha>` after a release candidate, `0.0.0.devN+g<sha>` before the first
  tag). The KiCad base is pushed the first time its tag (`docker/yapnr-kicad/TAG`) is built: first
  its `-src` image (both platforms), then the tags, then the attestation. The publish job can be
  re-run after a failure, and a later run publishes a missing `-src` image.
- **Releases** (called by `release.yaml`): the same, with the release tags.
- **Every run** compares `docker/yapnr-kicad` (`tools/image/base_context.sh`) with the
  `io.github.studio-fug.yapnr.kicad.context` label of the published base and fails if they
  differ: a published base tag is never overwritten, and a release never ships a base that does
  not match the tagged commit. The yapnr image is built FROM the base by digest.

`release.yaml` runs when a `v*` tag is pushed:

1. **verify:** the tag is annotated, matches `vX.Y.Z` or `vX.Y.Z-rc.N`, and its commit is on
   `main` (or a `release/X.Y` branch); computes the version, the previous stable tag, and whether
   the release becomes `latest` (a backport such as 0.2.5 never takes `latest` from 0.3.0).
2. **ci:** all of `ci.yaml` at the tag (lint, test, docs).
3. **image:** `image.yaml`, publishing `X.Y.Z`, `X.Y` and `latest` (release candidates:
   `X.Y.Z-rc.N` only).
4. **pypi:** checks the version and both Linux architectures, then publishes the tested Bazel
   wheels using PyPI Trusted Publishing (OIDC). No API token is stored in GitHub.
5. **release:** assembles the files above, attests them, creates a **draft** GitHub release with
   the files and the notes (a prerelease for `-rc` tags), then publishes it. Nothing follows the
   publish step, so the workflow works with immutable releases.

**Dry run:** Actions > Release > Run workflow (on any branch), or
`gh workflow run release.yaml --ref <branch>`. It runs every job and uploads the release files as a
workflow artifact, writes the release notes into the run summary (the header for the placeholder
tag `v0.0.0`, and the notes GitHub would generate, through the same API and `.github/release.yml`),
and publishes nothing: no image tags, no GitHub release, no PyPI upload.

## PyPI installation and setup

After a release is published:

```sh
python -m pip install "yapnr==X.Y.Z" --extra-index-url https://download.pytorch.org/whl/cpu
```

The published wheels support Linux x86-64 and ARM64 with glibc 2.34 or newer. They include
RF export modules, coupon resources, the PALACE schema, and the native FDTD library. Headless
KiCad and external EM solvers are separate runtime requirements; use the pinned container when
those are needed.

Before the first upload, a PyPI project owner must register a trusted publisher (or a pending
publisher for a new project): project `yapnr`, GitHub owner `Studio-Fug`, repository `yapnr`,
workflow `release.yaml`, environment `pypi`. Configure the GitHub environment with release
branch/tag restrictions. These names must match exactly. The workflow alone cannot register
the publisher on PyPI. See [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

Manual dry runs never request a publishing token or upload distributions. Only verified release
tags publish; release candidates use their PEP 440 `rc` version. PyPI versions are immutable:
fix a failed distribution with a new version rather than replacing an existing release.

## Release checklist (owner)

Only the owner creates `v*` tags; a tag ruleset enforces it. Agents prepare release pull requests
and never create or push tags.

1. **Release pull request** (for minors; optional for patches), titled `release: vX.Y.Z`:
   upgrade notes in `docs/releases/vX.Y.md` for `breaking` and `results-change` items, the new
   `X.Y` tag in the README and the container docs' examples, and a `WORKLOG.md` entry.
2. **Dry run** on `main` after it merges: `gh workflow run release.yaml --ref main`; it must be
   green. Wait as well for the `Image` run of the commit you are about to tag to finish green on
   `main`: if that commit introduced a new base tag, `main` publishes the base, and a release run
   racing it would try to publish the same base tag and fail.
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
  The organization must allow public packages (Organization settings > Packages > Package
  creation: Public), or the option is missing. The switch cannot be undone. Until then, anonymous
  `docker pull` fails.
- **Dependabot** (`.github/dependabot.yml`) proposes updates of the pinned actions and of the
  `ubuntu` and `uv` image digests once a month; see
  [maintaining the images](#maintaining-the-images).
- **Labels:** `tools/release/create_labels.sh`.
- **Immutable releases** (Settings > General > Releases): recommended. The release workflow
  publishes last, so it works with them on.
- **The `image` check** becomes required with v0.1.0 (it summarizes `image.yaml`).

## Maintaining the images

- **The KiCad base is frozen per tag.** `yapnr-kicad:<KiCad version>-<N>` keeps the Ubuntu
  packages of the day it was built (every package upgraded to the archive state recorded in
  `/etc/yapnr/ubuntu-snapshot`). Security fixes arrive by rebuilding under the next `N`: bump
  `docker/yapnr-kicad/TAG` (and nothing else, or together with a Dependabot `ubuntu` digest update)
  in a pull request, and the merge publishes the new base.
- **When:** at least once a month, when a KiCad patch release lands in the PPA (a new KiCad
  version, `N` back to 1), and soon after an Ubuntu security notice for a package in the base.
  The next yapnr patch release then ships the new base; `edge` has it right away.
- **Dependabot** proposes the `ubuntu:24.04` and `ghcr.io/astral-sh/uv` digests monthly. An
  `ubuntu` update changes `docker/yapnr-kicad`, so its pull request fails the `Image` check until
  `docker/yapnr-kicad/TAG` is bumped in it. A uv update may install a newer python-build-standalone
  release: the build then fails until `PBS_RELEASE` and the two `PBS_FULL_SHA256_*` checksums in
  `docker/yapnr/Dockerfile` are updated (from that release's `SHA256SUMS`).
- **The runtime locks** follow `requirements.lock` (`tools/image/update_runtime_locks.sh`); after a
  numpy or torch update, check the native libraries the new wheels bundle and update
  `docker/yapnr/native-libraries-<arch>.txt`, `THIRD_PARTY.md` and `third_party/image-licenses/`.
