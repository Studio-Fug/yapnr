# Developing yapnr

How the repository is built, tested and laid out, plus the environment notes worth knowing. For
what yapnr is, see [README.md](README.md); for the migration from Splanc, see
[docs/migration-plan.md](docs/migration-plan.md).

## Prerequisites

- [Bazelisk](https://github.com/bazelbuild/bazelisk) as `bazel`. It reads `.bazelversion` (7.7.1)
  and caches Bazel under `.bazelisk/` in the checkout (`.bazeliskrc`).
- `git` and a `python3` (any 3.9+) for the presubmit hooks. Bazel brings its own hermetic Python
  3.11 for builds and tests.
- KiCad 10, only for the KiCad test lane (from PR6a). See [KiCad](#kicad).

## Everyday commands

```sh
bazel run //:yapnr -- --version       # the CLI
bazel run //:doctor                   # environment report (Python, numpy, torch, KiCad CLI)
bazel test //...                      # unit tests and repo checks (no KiCad needed)
bazel test --config=quick //...       # skip tests tagged slow
bazel run //docs:build                # docs -> docs/site/html/
bazel run //docs:serve                # preview on http://127.0.0.1:8000/
prek run --all-files                  # presubmit lints
tools/release/version.py --field pep440   # the version of this checkout (from git)
tools/image/build_local.sh            # build and smoke-test the container images (Docker)
```

Test tiers and tags are described in [docs/architecture.md](docs/architecture.md#test-tiers).
New test files go under `tests/<tier>/<area>/test_*.py` in a package whose `BUILD.bazel` calls
`yapnr_py_tests()`; the wiring check fails on test files no target runs.

## Bazel

### Output base: internal, case-sensitive disk

Keep Bazel's **output base** on an internal, case-sensitive disk, not on an external or
case-insensitive volume: rules_python's extracted interpreter tree has files that differ only by
case, and a case-insensitive output base corrupts. The content-addressed caches that `.bazelrc`
puts in the checkout (`.bazel-disk-cache/`, `.bazel-repo-cache/`) are safe anywhere, because their
file names are hashes.

Set it once in a gitignored `user.bazelrc` at the repository root (it is `try-import`ed by
`.bazelrc`). Use an absolute path; `~` is not expanded there:

```text
startup --output_base=/absolute/path/to/home/.cache/yapnr-bazel
```

If a Bazel server wedges (for example a defunct `java` process after a crash), point Bazel at a
fresh output base rather than killing processes you did not start.

### Shared, busy machines

On a workstation that is also running experiments, limit Bazel and lower its priority:

```sh
nice -n 10 bazel test --config=lowmem //...
```

`--config=lowmem` sets `--jobs=2 --local_resources=cpu=2 --local_resources=memory=HOST_RAM*.25`.
Put `build --config=lowmem` in `user.bazelrc` to make it the default. Run one Bazel server at a
time (`bazel shutdown` when done) and check free disk space before large fetches (torch alone is
several hundred MB).

### Python dependencies

`requirements.in` lists the direct dependencies; `requirements.lock` is the resolved, hashed set
that Bazel consumes as `@yapnr_pypi`. After editing `requirements.in`:

```sh
bazel run //:requirements.update      # regenerate requirements.lock (hermetic Python 3.11)
bazel test //:requirements.test       # freshness check (network; tagged manual)
```

Resolve on darwin-arm64 or linux-aarch64 only; see
[docs/decisions.md](docs/decisions.md#the-requirements-lock) for why the lock is not valid on
linux-x86_64 yet. Commit `MODULE.bazel.lock` whenever it changes; CI runs with
`--lockfile_mode=error`.

## Presubmit (prek)

The lint gate is [prek](https://github.com/j178/prek), a drop-in reimplementation of pre-commit,
with the hooks and pins in `.pre-commit-config.yaml` (Splanc's set plus a privacy scan). Set up a
checkout once:

```sh
./setup-precommit.sh
```

The script uses a `prek` on `PATH` only if it is 0.4.12 (it warns otherwise) and else installs
prek 0.4.12 into a private virtualenv under `.venv/prek` (with `uv` when available); it never
installs into the system Python. It then installs the git hook and runs every lint once. CI runs
the same version with the same config.

The privacy scan (`tools/privacy_scan.py`) rejects absolute home, volume and temporary-directory
paths (also in the dash-encoded form agent tooling uses for project directories), tailnet and
`.local` host names, tailnet and private network addresses, personal e-mail addresses and
credentials. Use repository-relative or `~` paths, documentation values (`example.com`,
`192.0.2.0/24`) and GitHub noreply addresses instead. The owner's public commit address is no
exception in files; only `tools/privacy/allowed_identities.txt` names it. `--list-rules` prints
the rules.

## KiCad

yapnr drives KiCad 10 headlessly: `kicad-cli` for DRC and exports, and KiCad's bundled Python
(`pcbnew`) for worker processes. `yapnr doctor` reports what is configured; set the CLI with
`YAPNR_KICAD_CLI` (`PNR_KICAD_CLI` is accepted as an alias). Toolchain discovery
(`yapnr.kicad.toolchain`) arrives in PR6a.

### macOS: use a headless copy (no Dock icons)

On macOS every `kicad-cli` call from the stock `KiCad.app` registers with LaunchServices as a
foreground application, so the KiCad icon blips in the Dock for each call (the engine makes
several per second). **Never point yapnr, tests or scripts at the stock bundle.** Make a
background-only copy instead; an APFS clone costs no extra disk:

```sh
mkdir -p ~/Applications
cp -c -R /Applications/KiCad/KiCad.app ~/Applications/KiCad-headless.app
plutil -replace CFBundleIdentifier -string org.kicad.kicad.headless \
  ~/Applications/KiCad-headless.app/Contents/Info.plist
plutil -replace LSBackgroundOnly -bool true \
  ~/Applications/KiCad-headless.app/Contents/Info.plist
codesign --force --deep --sign - ~/Applications/KiCad-headless.app
export YAPNR_KICAD_CLI=~/Applications/KiCad-headless.app/Contents/MacOS/kicad-cli
```

The copy's `kicad-cli` registers as background-only and never shows a Dock icon; its DRC results
match the stock bundle. KiCad's Python workers do not register with the Dock. A future
`yapnr kicad make-headless` command automates this (PR3d). Redo the copy after upgrading KiCad.

## Container images and releases

The container images (`ghcr.io/studio-fug/yapnr` and its KiCad base) are described in
[docs/containers.md](docs/containers.md), including how to build them locally
(`tools/image/build_local.sh`, which needs Docker and a few GB of disk). Versions come from git tags
only; [docs/releases.md](docs/releases.md) has the versioning rules, the pull request labels that
group the release notes, and the owner's release checklist. Agents never create tags or releases.

After changing a runtime pin (torch, numpy, pyyaml or their dependencies) in `requirements.lock`,
regenerate the image's runtime locks with `tools/image/update_runtime_locks.sh` (needs `uv`);
`tests/unit/repo/test_images.py` fails until they agree.

## Repository layout

| Path                             | What                                                                  |
| -------------------------------- | --------------------------------------------------------------------- |
| `yapnr/`                         | the Python package (today: version and CLI)                           |
| `tests/unit/`                    | hermetic unit tests and repo checks                                   |
| `tools/`                         | privacy scan, test-wiring check, internal Bazel macros                |
| `tools/release/`, `tools/image/` | version derivation, release notes, image build and smoke test         |
| `release/`                       | the yapnr wheel (`//release:wheel`)                                   |
| `docker/`                        | the container images: `yapnr-kicad` (base) and `yapnr`                |
| `docs/`                          | documentation (Sphinx with MyST); the site is built by `//docs:build` |
| `branding/`                      | logo, mark, favicons and palette                                      |
| `.github/`                       | CI workflows, CODEOWNERS, pull request template                       |

The planned full layout is in [docs/migration-plan.md](docs/migration-plan.md#1-repository-layout).

## CI

`.github/workflows/ci.yaml` runs on pull requests, pushes to `main` and on demand:

- `lint`: prek on all files; every new commit must use an address listed in
  `tools/privacy/allowed_identities.txt` (the owner's public commit address) or a GitHub noreply
  address (`privacy_scan.py --identities`), and the new commits' messages and patches are
  privacy-scanned.
- `test`: `bazel test //... --config=ci` on `ubuntu-24.04-arm`, and the lock freshness check.
- `docs`: builds the site and uploads it as an artifact.
- `deploy-preview`, `cleanup-preview`, `deploy-pages`: publish to GitHub Pages (previews under
  `pr-preview/pr-N/`, only after `lint`, `test` and `docs` pass). Skipped unless the repository
  variable `YAPNR_PAGES_ENABLED` is `true`. The first deploy is a manual run on `main`, which
  creates the `gh-pages` branch; the steps are in the comment above the Pages jobs in `ci.yaml`.

`lint`, `test` and `docs` are the required checks. `.github/workflows/macos.yaml` runs the tests on
`macos-latest` for information only.

`.github/workflows/image.yaml` builds and smoke-tests the container images for linux/amd64 and
linux/arm64 on every pull request and publishes `edge` from `main` (informational until v0.1.0).
`.github/workflows/release.yaml` runs on release tags (and as a dry run on demand): CI at the tag,
the images with the release tags, and the GitHub release. See
[docs/releases.md](docs/releases.md#what-the-workflows-do).
