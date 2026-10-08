<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="branding/yapnr-logo-light.png">
    <img src="branding/yapnr-logo-256.png" alt="yapnr" width="256">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/Studio-Fug/yapnr/actions/workflows/ci.yaml?query=branch%3Amain"><img
    src="https://github.com/Studio-Fug/yapnr/actions/workflows/ci.yaml/badge.svg?branch=main"
    alt="CI"></a>
  <a href="https://github.com/Studio-Fug/yapnr/actions/workflows/macos.yaml?query=branch%3Amain"><img
    src="https://github.com/Studio-Fug/yapnr/actions/workflows/macos.yaml/badge.svg?branch=main"
    alt="macOS"></a>
  <a href="https://studio-fug.github.io/yapnr/"><img
    src="https://img.shields.io/github/deployments/Studio-Fug/yapnr/github-pages?label=docs"
    alt="Docs"></a>
  <!-- Enable once the first public image (after the GHCR visibility switch) and the first
  release (v0.1.0) exist; see docs/releases.md.
  <a href="https://github.com/Studio-Fug/yapnr/releases/latest"><img
    src="https://img.shields.io/github/v/release/Studio-Fug/yapnr?sort=semver&display_name=tag"
    alt="Latest release"></a>
  <a href="https://github.com/Studio-Fug/yapnr/pkgs/container/yapnr"><img
    src="https://img.shields.io/badge/ghcr-amd64%20%7C%20arm64-2496ED?logo=docker&logoColor=white"
    alt="Container image ghcr.io/studio-fug/yapnr (amd64, arm64)"></a>
  -->
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg"
    alt="License: AGPL-3.0-or-later"></a>
  <img src="https://img.shields.io/badge/python-3.11-3776ab.svg" alt="Python 3.11">
  <a href="https://github.com/j178/prek"><img
    src="https://img.shields.io/badge/pre--commit-prek-brightgreen.svg" alt="pre-commit: prek"></a>
</p>

# yapnr

**Yet another place and route** engine for printed circuit boards.

<p align="center">
  <img src="docs/animations/05-timer-led-10.gif" width="640"
    alt="Animation: yapnr places and routes a TLC555 LED flasher, from unplaced footprints to a
    compact, fully routed board; the gloss pass smooths its copper, and the board passes KiCad's
    DRC">
</p>

_The 555 flasher (a TLC555 astable blinker, 10 parts, 2 layers) from the
[regression ladder](docs/regression-ladder.md): from unplaced footprints to 100 % routed, with the
placement starts and routes it was chosen from, then the gloss pass (the copper it replaces in
red, its new copper in green), ending on KiCad's clean DRC verdict. Recorded with the opt-in
[compact placement](docs/design/compact-placement.md) and [gloss](docs/design/gloss.md)
(`PNR_COMPACT=1`, `PNR_GLOSS=1`; both off by default)._

<p align="center">
  <img src="docs/animations/showcase-chaser-line.gif" width="800"
    alt="Animation, side by side: an LED chaser placed and routed twice, its LEDs placed freely
    on the left and held in one line on the right, each LED's series resistor beside it, which
    the placer moves and turns as a whole">
</p>

_Left: LEDs placed freely. Right: the same chaser with its LEDs held in a
[line group](docs/constraints-and-hierarchy.md); compact placement keeps each LED's series resistor
flush beside it, and the placer moves and turns the whole row. Both with compact placement and
gloss, as above._

yapnr places components and routes copper for KiCad boards. It uses a mechanical,
Monte-Carlo-driven search: hierarchical block synthesis, power-first placement, native KiCad
routing and DRC in the loop, and electrical contracts (current, pairs, plane access) declared
next to the design.

The engine is being migrated here from the [Splanc](https://github.com/fughilli/splanc) repository,
where it was developed to lay out the Splanc Mini board. The migration plan and progress are in
[`docs/`](docs/index.md) and [`WORKLOG.md`](WORKLOG.md).
The papers and open-source projects it builds on are listed in
[Foundations and references](docs/references.md).

## Status

Alpha: this repository holds the build, CI and documentation scaffolding, a command-line stub, the
[migration plan](docs/migration-plan.md), and the engine imported from Splanc at its original
paths under `hardware/`: its committed history and its newer, uncommitted state
([manifest](docs/history/import-manifest.md)). The package restructuring follows. KiCad 10 only.

## Quick start

With [Bazelisk](https://github.com/bazelbuild/bazelisk) installed as `bazel`:

```sh
bazel run //:yapnr -- --version   # the CLI
bazel run //:doctor               # environment report
bazel test //...                  # unit tests and repository checks
bazel run //docs:build            # documentation -> docs/site/html/
```

See [DEVELOPERS.md](DEVELOPERS.md) for setup (including the headless KiCad copy on macOS) and
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution policy.

## Container image

`ghcr.io/studio-fug/yapnr` bundles KiCad 10, CPython 3.11 with CPU-only torch, and yapnr, for
linux/amd64 and linux/arm64, so running yapnr needs Docker and nothing else:

```sh
docker run --rm ghcr.io/studio-fug/yapnr:edge doctor
```

`edge` follows `main`; release tags (`X.Y.Z`, `X.Y`, `latest`) start with v0.1.0. See
[docs/containers.md](docs/containers.md) for projects, UID mapping, resource limits and Apple
Silicon, and [docs/releases.md](docs/releases.md) for versioning and releases.

## License

Copyright (C) 2026 Kevin Balke. Licensed under the GNU Affero General Public License v3.0 or later
(`AGPL-3.0-or-later`); see [LICENSE](LICENSE). Third-party material is listed in
[THIRD_PARTY.md](THIRD_PARTY.md).

## Design with an agent

Ask your agent “Please use Studio-Fug/yapnr to design X” and have it read
[AGENTS.md](AGENTS.md). Start `yapnr agent chat` in an interactive container;
[the execution guide](docs/agent-workflow.md) covers authentication, resource
limits, resumption and the open assembly-validation gates.
