<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="branding/yapnr-logo-light.png">
    <img src="branding/yapnr-logo-256.png" alt="yapnr" width="256">
  </picture>
</p>

# yapnr

**Yet another place and route** engine for printed circuit boards.

yapnr places components and routes copper for KiCad boards. It uses a mechanical,
Monte-Carlo-driven search: hierarchical block synthesis, power-first placement, native KiCad
routing and DRC in the loop, and electrical contracts (current, pairs, plane access) declared
next to the design.

The engine is being migrated here from the [Splanc](https://github.com/fughilli/splanc) repository,
where it was developed to lay out the Splanc Mini board. The migration plan and progress are in
[`docs/`](docs/index.md) and [`WORKLOG.md`](WORKLOG.md).

## Status

Alpha and pre-import: this repository currently holds the build, CI and documentation scaffolding,
a command-line stub and the [migration plan](docs/migration-plan.md). KiCad 10 only.

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

## License

Copyright (C) 2026 Kevin Balke. Licensed under the GNU Affero General Public License v3.0 or later
(`AGPL-3.0-or-later`); see [LICENSE](LICENSE). Third-party material is listed in
[THIRD_PARTY.md](THIRD_PARTY.md).
