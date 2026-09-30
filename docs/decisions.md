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
- **Commit identity: GitHub noreply addresses only,** for new commits and for imported history.
  The CI `lint` job checks the author and committer address of every new commit.
- **Issue tracking: GitHub issues** (`#N`); Splanc's `FUG-NNN` keys are not used here.
- **Docs on GitHub Pages, with per-PR previews.** Wired like Splanc and gated on the repository
  variable `YAPNR_PAGES_ENABLED == 'true'`, which the owner sets after the first green build of
  `main`. The repository is public, so publishing is approved.
- **Viewer agent features off by default.** The viewer's Ask agent and AI summaries ship in a later
  PR (PR4c) as optional features, disabled unless explicitly enabled.
- **elkjs is fetched at build time, pinned by sha256, never vendored.** It is served as a
  separate, unmodified file (PR4b). `THIRD_PARTY.md` lists it and three.js.
- **No Nix in yapnr.** KiCad comes from the discovered toolchain (and the official container image
  in CI). Nix extension tags are root-module-only, which Splanc had to patch around.
- **Required CI test job on `ubuntu-24.04-arm`;** the macOS job is informational. The lock is
  CPU-only for darwin-arm64 and linux-aarch64 (below).

## Pinned versions

Update a pin together with the file that holds it, and note why here.

| Component       | Pin                        | Held in                         |
| --------------- | -------------------------- | ------------------------------- |
| Bazel           | `7.7.1`                    | `.bazelversion`                 |
| `rules_python`  | `2.0.3`                    | `MODULE.bazel`                  |
| Python          | `3.11` (hermetic)          | `MODULE.bazel`                  |
| pip hub         | `yapnr_pypi`               | `MODULE.bazel`                  |
| torch           | `>=2.2,<2.4` (lock: 2.3.1) | `requirements.in`, lock         |
| numpy           | `>=1.26,<2` (lock: 1.26.4) | `requirements.in`, lock         |
| pyyaml          | `>=6` (lock: 6.0.3)        | `requirements.in`, lock         |
| Sphinx stack    | see below                  | `requirements.in`               |
| mermaid (JS)    | `11.4.1`                   | `docs/_sphinx/conf.py`          |
| prek            | `0.4.12`                   | `setup-precommit.sh`, `ci.yaml` |
| presubmit hooks | see below                  | `.pre-commit-config.yaml`       |
| `setup-bazel`   | `0.15.0`                   | `.github/workflows/`            |

Rationale:

- **Bazel 7.7.1 and `rules_python` 2.0.3:** the same as Splanc, so yapnr can be Splanc's
  `bazel_dep` without version skew; bumped together.
- **Python 3.11:** Splanc's hermetic toolchain. Code that runs inside KiCad must additionally stay
  stdlib-only and parse under Python 3.9 (KiCad's bundled Python on macOS).
- **`yapnr_pypi`:** pip hub names must be unique across modules; Splanc's hub is `pypi`.
- **torch below 2.4:** from 2.4 on, aarch64 wheels are CUDA-enabled and would drag linux-only
  `nvidia-*` wheels into the single, markerless lock.
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

### The requirements lock

`requirements.lock` is generated on the development Mac (darwin-arm64) with the hermetic Python
3.11 toolchain: `bazel run //:requirements.update`. The resolution is also valid on linux-aarch64,
which is why the required `test` job runs on `ubuntu-24.04-arm`. It is **not** valid on
linux-x86_64: the x86_64 torch 2.3.1 wheel needs CUDA libraries that the lock does not list. PR6a
adds a separate x86_64 lock with the CPU torch index (`requirements_by_platform`).

`//:requirements.test` re-resolves and diffs the lock. It needs network access, so it is tagged
`manual` and runs in exactly one CI job (`test`).
