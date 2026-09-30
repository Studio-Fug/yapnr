# Decisions

The owner's decisions for yapnr, and the pinned tool versions with their rationale. Update this file
in the same change as the file that holds a pin (`MODULE.bazel`, `requirements.in`,
`.pre-commit-config.yaml`, the workflows). Architecture decision records (ADRs) join this page in
PR7 under `docs/adr/`.

## Decision log

Owner decisions of 2026-09-29, taken while planning the migration (see
[the migration plan](migration-plan.md), §0.2):

- **License: `AGPL-3.0-or-later`.** `LICENSE` holds the verbatim AGPL-3.0 text; the SPDX identifier
  appears in `README.md`, the docs footer and `CONTRIBUTING.md`. No per-file SPDX headers for now.
  Matches Splanc's declaration; one identifier keeps a later relicense cheap.
- **No license metadata in Bazel for now.** If it is added later, it goes into `REPO.bazel` as
  `repo(default_package_metadata = ...)`, which covers every package, never into a root
  `package()`, which would cover only the root package. This avoids a `rules_license` dependency
  before anything consumes it.
- **Contributions: owner only, for now.** Outside contributions are not accepted yet;
  `CONTRIBUTING.md` says so. Revisit together with the inbound license terms (CLA or DCO).
- **Commit identity: the owner's public commit address** (decided 2026-09-29; it replaces the
  earlier "GitHub noreply addresses only" rule), for new commits and for imported history: the
  owner's commits under the owner's name, agent commits as `Claude Agent`, and every identity in
  the imported Splanc history mapped to it (migration plan §7.2). The address is listed in
  `tools/privacy/allowed_identities.txt`, the only file that may name it. Reason: GitHub accepts
  only an address verified on the account as the author of a merge made on the website, so a
  noreply address cannot author the merges into `main`; the owner chose to publish this address
  on commits. The CI `lint` job checks the author and committer address of every new commit with
  `tools/privacy_scan.py --identities`, which accepts the allowlisted addresses,
  `users.noreply.github.com` addresses (the earlier PR0 commits keep theirs) and
  `noreply@github.com` (GitHub's committer for web merges). It also scans the messages and
  patches of the new commits, where the allowlisted addresses pass; in file contents they remain
  findings like any other personal address. Adding an address to the allowlist is an owner
  decision recorded here.
- **Issue tracking: GitHub issues** (`#N`); Splanc's `FUG-NNN` keys are not used here.
- **Docs on GitHub Pages, with per-PR previews.** Wired like Splanc and gated on the repository
  variable `YAPNR_PAGES_ENABLED == 'true'`, which the owner sets after the first green build of
  `main`; a manual run of CI on `main` then creates the `gh-pages` branch, and Pages is pointed at
  it (the order is in `ci.yaml` and `WORKLOG.md`). Previews are published only after `lint`,
  `test` and `docs` pass. The repository is public, so publishing is approved.
- **Viewer agent features off by default.** The viewer's Ask agent and AI summaries ship in a later
  PR (PR4c) as optional features, disabled unless explicitly enabled.
- **elkjs is fetched at build time, pinned by sha256, never vendored.** It is served as a
  separate, unmodified file (PR4b). `THIRD_PARTY.md` lists it and three.js.
- **No Nix in yapnr.** KiCad comes from the discovered toolchain (and the official container image
  in CI). Nix extension tags are root-module-only, which Splanc had to patch around.
- **Required CI test job on `ubuntu-24.04-arm`;** the macOS job is informational. The lock is
  CPU-only for darwin-arm64 and linux-aarch64 (below).

Owner decisions for releases and container images (PR-R; [releases](releases.md),
[containers](containers.md)):

- **Versions: SemVer tags `vX.Y.Z`,** release candidates `vX.Y.Z-rc.N`. While 0.x, a breaking or
  results-changing release bumps MINOR; the `results-change` label marks such pull requests.
- **The tag is the only version source.** No `version` in `MODULE.bazel`; the wheel is stamped at
  build time and `__version__` reads the installed metadata.
- **Only the owner creates `v*` tags** (a tag ruleset); agents never create tags or releases. The
  release workflow runs on the tag push and has a dry-run dispatch.
- **Release notes are GitHub's generated notes,** grouped by pull request label
  (`.github/release.yml`). No Conventional Commits, no release-please.
- **Public images on GHCR:** `ghcr.io/studio-fug/yapnr-kicad` (Ubuntu 24.04 with KiCad from the
  KiCad team's PPA) and `ghcr.io/studio-fug/yapnr`, native linux/amd64 and linux/arm64 builds with
  SBOM, provenance and attestations. Owner approved making the packages public (a one-time,
  irreversible switch after the first push).
- **Redistributing KiCad in the images is approved,** with the source offer as permanent
  `yapnr-kicad:<tag>-src` images (never pruned), the Ubuntu snapshot recorded in a label, and the
  notices (`LICENSE`, `THIRD_PARTY.md`, `SOURCES`) under `/usr/share/doc/yapnr`.

## Pinned versions

Update a pin together with the file that holds it, and note why here.

| Component        | Pin                        | Held in                                  |
| ---------------- | -------------------------- | ---------------------------------------- |
| Bazel            | `7.7.1`                    | `.bazelversion`                          |
| `rules_python`   | `2.0.3`                    | `MODULE.bazel`                           |
| Python           | `3.11` (hermetic)          | `MODULE.bazel`                           |
| pip hub          | `yapnr_pypi`               | `MODULE.bazel`                           |
| torch            | `>=2.2,<2.4` (lock: 2.3.1) | `requirements.in`, lock                  |
| numpy            | `>=1.26,<2` (lock: 1.26.4) | `requirements.in`, lock                  |
| pyyaml           | `>=6` (lock: 6.0.3)        | `requirements.in`, lock                  |
| Sphinx stack     | see below                  | `requirements.in`                        |
| mermaid (JS)     | `11.4.1`                   | `docs/_sphinx/conf.py`                   |
| prek             | `0.4.12`                   | `setup-precommit.sh`, `ci.yaml`          |
| presubmit hooks  | see below                  | `.pre-commit-config.yaml`                |
| `setup-bazel`    | `0.15.0`                   | `.github/workflows/`                     |
| Ubuntu (images)  | `24.04`, by digest         | `docker/yapnr-kicad/Dockerfile`          |
| KiCad (images)   | `10.0.6~ubuntu24.04.1`     | `docker/yapnr-kicad/Dockerfile`, `TAG`   |
| Python (image)   | `3.11.15` (uv-managed)     | `docker/yapnr/Dockerfile`                |
| uv (image build) | `0.12.21`, by digest       | `docker/yapnr/Dockerfile`                |
| Runtime locks    | from `requirements.lock`   | `docker/yapnr/runtime-*.lock`            |
| Docker actions   | by commit SHA              | `.github/actions/`, `.github/workflows/` |

Rationale:

- **Bazel 7.7.1 and `rules_python` 2.0.3:** the same as Splanc, so yapnr can be Splanc's
  `bazel_dep` without version skew; bumped together.
- **Python 3.11:** Splanc's hermetic toolchain. Code that runs inside KiCad must additionally stay
  stdlib-only and parse under Python 3.9 (KiCad's bundled Python on macOS).
- **`yapnr_pypi`:** pip hub names must be unique across modules; Splanc's hub is `pypi`.
- **torch below 2.4:** the same ceiling as Splanc's `requirements.in`, and the version the engine
  was tuned on: the experiment environment runs torch 2.3.1 with numpy 1.26 (numpy 1 ABI), and the
  lock resolves torch 2.3.1. Lifting the ceiling is a separate change with a measured regression
  comparison. The ceiling is not what keeps the aarch64 lock CPU-only: torch 2.4 to 2.9 also
  restrict their `nvidia-*` and `triton` dependencies to x86_64.
- **numpy 1.x:** torch 2.3 wheels are built against the numpy 1 ABI, and the engine was tuned on
  numpy 1.26. (Splanc's lock pairs torch 2.3.1 with numpy 2, which this avoids.) The interop smoke
  test in `tests/unit/interop` guards it.
- **Sphinx stack:** sphinx 9.0.4, myst-parser 5.1.0, furo 2025.12.19, sphinx-copybutton 0.5.2,
  sphinx-design 0.7.0 and sphinxcontrib-mermaid 2.1.0, exactly Splanc's locked versions, so both
  sites build the same way. matplotlib is left out until a page needs generated figures.
- **Presubmit hooks:** Splanc's set and pins (black 25.1.0, isort 6.0.1, flake8 7.0.0,
  shellcheck-py v0.9.0.6, buildifier 8.2.0, prettier v3.1.0, markdownlint-cli v0.38.0,
  pre-commit-hooks v4.5.0), minus `nixpkgs-fmt`, plus the local privacy scan.
- **mermaid, prek, `setup-bazel`:** the same as Splanc.
- **GitHub Actions majors** (`actions/checkout@v4`, `setup-python@v5`, `cache@v4`,
  `upload-artifact@v4`, `download-artifact@v4`, `setup-bazel@0.15.0`): Splanc's. They declare Node
  20, which GitHub now runs on Node 24 with a deprecation warning. Move to the Node 24 majors
  together with Splanc, and record it here.
- **CI caches:** `setup-bazel`'s own caches are off. One `actions/cache` entry holds Bazelisk's
  downloads and the Bazel repository cache, keyed on OS, CPU architecture, `.bazelversion`,
  `MODULE.bazel`, `MODULE.bazel.lock` and `requirements.lock`; the disk cache is per job. CI sets
  `BAZELISK_HOME`, which takes precedence over `.bazeliskrc`.

### Container images

- **Base: Ubuntu 24.04 plus `ppa:kicad/kicad-10.0-releases`, on both architectures.** One
  Dockerfile gives the same distribution, KiCad build and worker Python (3.12) everywhere. The
  official `kicad/kicad` image is amd64-only from 10.0 on and ships 341 MB of demos and a
  passwordless `sudo`; building KiCad from source costs about an hour per architecture and
  release. The PPA key is checked in (`docker/yapnr-kicad/kicad-ppa.asc`, fingerprint
  `FDA8 54F6 1C4D 0D95 72BB 95E5 245D 5502 FAD7 A805`, confirmed against Launchpad).
- **The base is published once per `docker/yapnr-kicad/TAG` and never overwritten,** because the
  PPA deletes superseded builds within days (and its snapshot service does not reach back). CI
  refuses a change to `docker/yapnr-kicad/` that keeps a published tag.
- **Controller Python: the patch release of Bazel's hermetic 3.11** (`3.11.15`), installed by uv
  from python-build-standalone, with the runtime locks pinned to `requirements.lock` (versions and
  hashes; `tests/unit/repo/test_images.py`). On amd64, torch is `2.3.1+cpu` from the PyTorch CPU
  index; PyPI's x86_64 wheel pulls in the CUDA stack. uv resolves both locks from any host.
- **Measured on linux/arm64 (2026-09-29):** the base is 1.98 GB unpacked (the KiCad install adds
  1.4 GB to Ubuntu); the yapnr layers add about 0.65 GB (CPython 0.12 GB, the runtime 0.52 GB with
  byte-compiled sources). The KiCad install takes about a minute on a native arm64 host.
- **Actions on the image and release paths that push or sign are pinned by commit SHA**
  (`docker/*`, `actions/attest`), since those jobs can write packages and attestations. The other
  `actions/*` stay on the major tags the rest of the workflows use.

### The requirements lock

`requirements.lock` is generated on the development Mac (darwin-arm64) with the hermetic Python
3.11 toolchain: `bazel run //:requirements.update`. The resolution is also valid on linux-aarch64,
which is why the required `test` job runs on `ubuntu-24.04-arm`. It is **not** valid on
linux-x86_64: the x86_64 torch 2.3.1 wheel needs CUDA libraries that the lock does not list. PR6a
adds a separate x86_64 lock with the CPU torch index (`requirements_by_platform`).

`//:requirements.test` re-resolves and diffs the lock. It needs network access, so it is tagged
`manual` and runs in exactly one CI job (`test`).
