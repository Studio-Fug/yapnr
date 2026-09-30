# The atopile toolchain

yapnr builds [atopile](https://github.com/atopile/atopile) projects itself, without Nix and
without any hosted service: `yapnr atopile setup` installs a hash-pinned atopile environment, and
`yapnr atopile build` runs one bounded, isolated, offline `ato build` in it. The parts a build
needs come from a [part cache](../part-cache.md), never from this repository or from EasyEDA at
build time; the offline part picker answers atopile's part queries from the cache's catalog.

This replaces Splanc's `hardware/tools/ato_build.sh`, rules_atopile's Bazel actions and its Nix
packaging (the three `patches/rules_atopile-*.patch` and the patched `atopile.nix`).

| Command                                | What                                                  |
| -------------------------------------- | ----------------------------------------------------- |
| `yapnr atopile setup`                  | create the pinned atopile environment (needs `uv`)    |
| `yapnr atopile info`                   | the environment and headless KiCad a build would use  |
| `yapnr atopile build <project>`        | build a project (offline, isolated, time-bounded)     |
| `yapnr atopile lock-parts <project>`   | write the project's parts lock (optionally upload)    |
| `yapnr atopile materialize <project>`  | write the locked parts into the project's own tree    |
| `yapnr picker serve --catalog F`       | the offline components API, on the loopback interface |
| `yapnr picker catalog import/validate` | convert or check picker catalogs                      |
| `yapnr part-cache ...`                 | the part cache ([its page](../part-cache.md))         |

## Setting up atopile

```sh
uv --version                 # 0.12.21 (pins.json); install uv with pipx or into a private venv
yapnr atopile setup          # ~/.cache/yapnr/atopile/<lock-id>/, about 15 s with a warm uv cache
yapnr atopile info
yapnr doctor                 # "atopile   0.15.8 via yapnr atopile setup (...) (ok)"
```

`setup` creates `<root>/<lock-id>/` (root: `$YAPNR_ATOPILE_HOME`, else
`$XDG_CACHE_HOME/yapnr/atopile`, else `~/.cache/yapnr/atopile`); the lock id is the first 12 hex
digits of the sha256 of the platform's lock and `pins.json`, so a new lock gets a new directory:

- `python/`: CPython 3.14.7 from python-build-standalone release 20260929, installed by uv; the
  release is checked against `pins.json`.
- `venv/`: installed with `uv pip install --require-hashes --no-deps` from
  `yapnr/frontends/atopile/locks/requirements-<platform>.lock`. Every file is one the lock names
  by sha256; nothing is resolved at install time.
- `yapnr-atopile-env.json`: written last. An environment without it is incomplete and is rebuilt.
  It records the platform, the lock, the versions, and on linux-aarch64 the name and sha256 of the
  self-built atopile wheel.

With `setup --root DIR`, builds find the environment with `YAPNR_ATOPILE_HOME=DIR` (or
`YAPNR_ATO_PYTHON`), and `yapnr atopile info --root DIR` reports it.

`setup` never runs atopile's command line (the version is read from package metadata), so it
cannot touch the user's KiCad or atopile configuration.

### The lock

atopile 0.15.8 requires Python `>=3.14,<3.15` and declares 49 dependencies, nearly all
open-ended, with no lock of its own. yapnr pins one line (`locks/requirements.in`:
`atopile==0.15.8`) and keeps one fully hashed lock per platform:

| Lock                                          | Platform                               |
| --------------------------------------------- | -------------------------------------- |
| `requirements-darwin-arm64.lock`              | macOS on Apple silicon                 |
| `requirements-darwin-x86_64.lock`             | macOS on Intel                         |
| `requirements-linux-x86_64.lock`              | Linux x86_64 (manylinux 2.28)          |
| `requirements-linux-aarch64.lock`             | Linux aarch64, without atopile (below) |
| `build-requirements-linux-aarch64.lock`       | the wheel builder's dependencies       |
| `sdist-build-requirements-linux-aarch64.lock` | what `setup` builds zstd's sdist with  |

They are resolved as of `exclude_newer` in `pins.json` (2026-09-20): that date reproduces, package
for package, the environment Splanc's Nix build resolved and built its boards with. All four
platform locks pin the same versions (a unit test checks it). Regenerate them with uv at the pinned
version, and check them with `--check` (needs the network):

```sh
tools/atopile/update_locks.sh          # rewrite the locks
tools/atopile/update_locks.sh --check  # fail if a re-resolution differs
```

### linux-aarch64: a wheel from the sdist

PyPI has no atopile 0.15.8 wheel for linux-aarch64 (the architecture of yapnr's required CI job
and of the arm64 image). Its lock leaves atopile out (`--no-emit-package atopile`), and
`tools/atopile/build_wheel.sh` builds one from the sdist:

- the sdist is pinned by sha256;
- the build dependencies come from their own hashed lock (hatchling, hatch-vcs, nanobind,
  ziglang 0.16.0, cmake, ninja);
- the fork of scikit-build-core that atopile's `pyproject.toml` names by git **branch** is checked
  out at a pinned **commit** and installed with `--no-deps`;
- the build runs with `--no-build-isolation`, niced, with `OPENSSL_armcap=0`.

```sh
tools/atopile/build_wheel.sh /tmp/atopile-wheel
yapnr atopile setup --wheel /tmp/atopile-wheel/atopile-0.15.8-*.whl
```

`zstd` has no cp314 wheel there either, so a C toolchain is needed on linux-aarch64 hosts. uv
would fetch the build dependencies of its hashed sdist unpinned (build isolation; hashes in build
constraints are checked only for the packages listed). `setup` therefore installs the hashed
`sdist-build-requirements-linux-aarch64.lock` (setuptools, all a `setup.py`-only sdist needs)
into the environment first and installs the lock with `--no-build-isolation`: a build can use
nothing that is not hash-pinned, and one that needs more fails instead of downloading it (checked
with the pinned CPython: zstd 1.5.7.2 builds from its hashed sdist with the pinned setuptools, and
without it the build stops). Checked on Ubuntu 24.04 arm64 under Apple's virtualization (two
cores), before this change: the wheel builds in about 3 minutes, `setup --wheel` takes about
30 s, and the end-to-end tests below pass.

### Discovery

A build finds its atopile interpreter the way KiCad is found, in this order:

1. `$YAPNR_ATO_PYTHON`;
2. `[atopile] python = "..."` in `~/.config/yapnr/config.toml`;
3. the container image's `/opt/atopile/bin/python` (planned, see [below](#not-done-yet));
4. the `setup` environment of the current lock.

The version must be 0.15.8. An explicitly configured interpreter (1 or 2) that is missing or has
another version is an error, not a reason to try the next one.

## Building

```sh
yapnr atopile build path/to/project                 # build "default"
yapnr atopile build path/to/project -b mini --out out/mini
```

For each build the runner:

1. copies the project into a fresh work directory (without the top-level `build/`,
   `manufacturing/` and `yapnr-out/`, any `.git` or `__pycache__`, or atopile's caches;
   `.ato/modules` is kept). With `--files-from LIST` it copies exactly the files the list names
   (paths relative to the project; the Bazel rule passes its declared inputs). The source tree is
   never written; `--update-layout` copies the new layout back, and nothing else does;
2. writes every part of the project's parts lock (`yapnr-parts.lock.json`) into the copy, each file
   checked against the cache's sha256. A part directory already in the tree with other content is
   an error (`--replace-parts` overwrites it);
3. writes the stock KiCad footprint libraries that the sources reference into the build's
   `fp-lib-table` (below);
4. starts the offline picker on the loopback interface, answering from the catalog entries of the
   locked parts and any `--catalog` files;
5. runs `python -m atopile build -b <build> -t ... -x default -x datasheets -x
collect-manufacturing -v` with the [allowlisted environment](#the-build-environment) and a
   deadline (`--timeout`, default 1800 s). On the deadline the whole process tree is stopped and
   killed, atopile's forkserver and workers included;
6. frames the board if asked (`--outline-margin-mm`: parts moved into view, a fitted sheet and an
   `Edge.Cuts` rectangle; for code-only example boards);
7. copies the named outputs and `result.json` into `--out` (default
   `./yapnr-out/<project>/<build>`). An existing output directory is replaced only when it holds
   an earlier build's `result.json`; the project directory never is.

The default targets are `build-design`, `bom`, `variable-report`, `power-tree` and `pinout`.
Allowed besides: `manifest`, `stackup`, `data-interface-layout`, and the KiCad exports `mfg-data`,
`step`, `glb`, `2d-image` and `3d-image`. atopile always adds its `default` target, whose
datasheet downloads reach the network; the runner excludes it, and `datasheets`.

The board, its `fp-lib-table` and the outputs are wherever atopile puts them for the project's
`ato.yaml`: `paths.layout` and `paths.build`, and a build's own `paths.layout`, `fp_lib_table` and
`output_base`. As in atopile, a layout directory holding one board of another name (KiCad's
autosaves aside) is that build's board, and two are an error. Every path must stay inside the
project. The outputs are named after the build whatever the board's file name; `result.json`
records the board's path in the project (`layout`).

| Output     | File                                                   |
| ---------- | ------------------------------------------------------ |
| the board  | `<build>.kicad_pcb`                                    |
| BOM        | `<build>.bom.csv`, `<build>.bom.json`                  |
| variables  | `<build>.variables.ato.json`                           |
| power tree | `<build>.power_tree.ato.json`, `power_tree.md`         |
| pinout     | `pinout/`                                              |
| record     | `result.json`, `ato.log`, `hook.jsonl`, `catalog.json` |

`build/manifest.json` is never copied: it holds absolute paths.

`result.json` records the atopile version and where it was found, the targets, the exit code,
the sha256 of every output, the parts and the sha256 of the parts lock and of the catalog the
picker served, the KiCad version, every request the picker answered, the LCSC ids attached from
the project's parts, and anything the hook refused. Its **input id** is the sha256 of the board
with every UUID replaced by the nil UUID: atopile stamps fresh UUIDs on each build, and two builds
of the same sources give the same input id.

`--online` lets atopile download a picked part that is not in the project from EasyEDA (as
`ato create part` does). Such a build depends on a third-party service and is not reproducible;
use it only while authoring.

### Designators and the layout

atopile updates an existing layout (by default `elec/layout/<build>/<build>.kicad_pcb`) instead of
starting a new one. It keeps that layout's designators and positions, and gives new parts the next free
designator. Without a layout, it numbers every part afresh and places it in a row. If a project
does not commit its layout (Splanc ignores `elec/layout/`), a clean checkout and a developer's
tree can build different designators and positions from the same sources. The nets, footprints
and pads per `atopile_address` stay the same. To compare two builds, start both from the same
layout, or compare them by `atopile_address`. Bazel's `glob(["elec/**"])` also picks up an
uncommitted layout that is present on disk; the Bazel action sees no other file of the project.

### The build environment

The child's environment is built from an allowlist; nothing of the caller's environment is
inherited (no API keys, cloud or vendor credentials, proxies or `PYTHONPATH`):

| Variable                                                   | Value and why                                                                                                                                                                                                   |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `HOME`, `TMPDIR`, `XDG_{CONFIG,CACHE,DATA,STATE}_HOME`     | private directories in the work directory: atopile writes its config, telemetry id and build history there and tries to install its KiCad plugin into the KiCad config under `HOME`; `kicad-cli` needs a `HOME` |
| `PATH`                                                     | `/usr/bin:/bin:/usr/sbin:/sbin`                                                                                                                                                                                 |
| `ATO_NON_INTERACTIVE=1`                                    | no prompts                                                                                                                                                                                                      |
| `CI=1`, `FBRK_TELEMETRY=0`                                 | no datasheet downloads, no telemetry                                                                                                                                                                            |
| `ATO_SERVICES_COMPONENTS_URL`, `ATO_SERVICES_PACKAGES_URL` | the loopback picker (they override `ato.yaml`): part queries, atopile's message of the day (fetched on every command) and package-registry requests are all answered locally                                    |
| `OPENSSL_armcap=0` (aarch64)                               | OpenSSL's ARMv8 capability probe raises SIGILL in the `cryptography` wheel under Apple's virtualization                                                                                                         |
| `GIT_TERMINAL_PROMPT=0`, `GIT_ALLOW_PROTOCOL=file`         | no prompts; offline, git reads local repositories only (atopile clones a missing git dependency during every build)                                                                                             |
| `PYTHONPATH`, `YAPNR_ATO_*`                                | the hook, below                                                                                                                                                                                                 |

### The hook

atopile 0.15.8 runs each build in a worker forked from a multiprocessing forkserver, a separate
interpreter started from the environment. Anything set up only in the parent process (for
example a sign-in token) never reaches the build. The runner therefore puts
`yapnr/frontends/atopile/hook/` on `PYTHONPATH`; its `sitecustomize.py` installs an import hook in
**every** interpreter of the build. The hook does nothing unless `YAPNR_ATO_HOOK=1`; it patches
atopile's modules as they are imported and changes no file of the environment:

- **Loopback picker only.** `ApiClient._cfg` hands atopile a placeholder bearer token only when
  the effective components URL, parsed, is `http`, a literal loopback address (`127.0.0.1` or
  `::1`) and the runner's port. Any other URL is refused before a request is made. atopile
  0.15.8 otherwise refuses to pick without a sign-in session; the placeholder never enters its
  global sign-in state, which its telemetry and agent clients read.
- **Empty queries.** `fetch_parts_multiple([])` returns `[]` without a request.
- **Parts from the project.** A picked LCSC id whose atomic part is in the project's parts
  directory (`supplier_partno="C..."`) is attached from there instead of being downloaded from
  EasyEDA. That is how a pick is served from the part cache.
- **Offline.** EasyEDA is never contacted (unless `--online`); a pick that would need it fails and
  names the missing part. Nor is a git repository: atopile installs missing dependencies at the
  start of every build, cloning `git` ones; offline, the clone is refused and the build fails
  naming the dependency (install it into `.ato/modules` first). Registry dependencies fail on
  their own: the package registry URL is the loopback picker, which serves no packages.
- **No GUI KiCad.** `kicad-cli` is the discovered headless one or none: atopile's search of
  `/Applications/KiCad` and its `pcbnew` launcher are disabled.
- **No running KiCad.** After every build (and while attaching parts) atopile looks for the IPC
  sockets of running KiCad instances in a fixed `/tmp/kicad` to reload an open board. The hook
  hands it none (`kicad-reload-skipped` in `hook.jsonl`), so a build never talks to a KiCad on the
  machine.

Every patch checks that what it replaces exists, so an atopile it was not written for fails
loudly instead of building unpatched.

### KiCad

Only the headless KiCad is used ([DEVELOPERS.md](../../DEVELOPERS.md#kicad)): `$YAPNR_KICAD_CLI`
(or `PNR_KICAD_CLI`), else `~/Applications/KiCad-headless.app` on macOS and `kicad-cli` on `PATH`
on Linux. Programs of the GUI bundle (`/Applications/KiCad`, anything in a `KiCad.app`, any app
bundle that is not background-only) are refused. Without a KiCad, targets that need `kicad-cli`
are unavailable; the default targets do not need it.

atopile 0.15.8 targets KiCad 9 (`KICAD_VERSION = "9.0"`, board format 20241229), which KiCad 10
reads. It resolves a `Library:Footprint` reference only through the project's own
`fp-lib-table`, never KiCad's global table. The runner adds the KiCad 10 stock libraries that the
sources reference to the build's table (`--stock-footprints all` adds all of them, `none`
nothing); entries already there are kept. atopile 0.15.x parts carry their own footprint files,
so today's projects reference none.

## Parts

A project keeps its parts out of its tree: `yapnr-parts.lock.json`, next to `ato.yaml`, pins each
part by content id, and the build materializes them from the [part cache](../part-cache.md):

```json
{
  "parts": [
    {
      "id": "211d87d939ed74ac0b1ff2b144459c44792c3acaa40496cebd4e3d57af14780b",
      "lcsc": "C990000001",
      "name": "Yapnr_Synthetic_SR1K"
    }
  ],
  "parts_dir": "elec/src/parts",
  "schema": "yapnr-atopile-parts-lock-v1"
}
```

```sh
yapnr atopile lock-parts path/to/project --upload --imported-from "my-board"   # parts -> cache
yapnr atopile materialize path/to/project                                     # cache -> tree
```

The picker serves the catalog entries of the locked LCSC ids only, so a type pick chooses among
parts the build can attach offline. `--catalog` adds catalog files (the first catalog wins on a
repeated LCSC id).

## The offline picker

The picker speaks atopile 0.15.8's components API:

| Request                             | Answer                                      |
| ----------------------------------- | ------------------------------------------- |
| `GET /v0/component/lcsc/<id>`       | `{"components": [...]}`                     |
| `GET /v0/component/mfr/<mfr>/<pn>`  | the same; the segments are URL-decoded      |
| `POST /v0/query {"queries": [...]}` | `{"results": [{"components": [...]}, ...]}` |
| `POST /v0/query/<method>`           | `{"components": [...]}`                     |
| `GET /v0/motd`                      | `{"message": null}`                         |
| anything else                       | 404                                         |

Type queries (`resistors`, `capacitors`, `inductors`) filter by kind, package (`R0402` matches
`0402`) and every constrained parameter: a part matches when its band (the nominal value widened
by its tolerance, or a rated value) lies inside the requested interval, and a part that lacks a
constrained parameter does not match. Basic parts rank first, then the value closest to the
middle of the request. The server binds only literal loopback addresses, requires a loopback
`Host` header, limits request bodies, and never logs the `Authorization` header.

Catalogs use the `yapnr-picker-catalog-v1` schema (JSON; YAML when PyYAML is available):

```json
{
  "schema": "yapnr-picker-catalog-v1",
  "provenance": { "source": "...", "retrieved": "2026-09-30", "licence_note": "..." },
  "parts": [
    {
      "lcsc": "C990000001",
      "mpn": "SR1K",
      "manufacturer": "yapnr-synthetic",
      "package": "0603",
      "kind": "resistor",
      "description": "synthetic 1 kohm 1 % 0603 resistor",
      "params": { "resistance_ohm": 1000.0, "tolerance_pct": 1.0, "voltage_max_v": 75.0 },
      "basic": true,
      "stock": "unknown"
    }
  ]
}
```

Kinds: `ic`, `resistor`, `capacitor`, `inductor`, `diode`, `led`, `transistor`, `connector`,
`crystal`, `fuse`, `switch`, `module`, `mechanical`, `other`. Parameters: `resistance_ohm`,
`capacitance_f`, `inductance_h`, `voltage_max_v`, `power_max_w`, `current_max_a`,
`saturation_current_a`, `dc_resistance_ohm`, `self_resonant_frequency_hz`, `tolerance_pct`,
`tempco`. A validation error lists every problem.

`stock` is a count or `"unknown"`. atopile needs numbers, so the picker sends 0 for an unknown
stock and a price of 0.0 when none is known; nothing treats a picker answer as evidence that a
part can be ordered.

`yapnr picker catalog import <splanc-catalog.json> -o catalog.json` converts Splanc's layout
(lists of parts by type): `resistance_ohms` becomes `params.resistance_ohm`, the kind comes from
the list (`passives` by the unit in the description), and nothing else is read out of the
descriptions.

## Bazel

The atopile environment is a toolchain, found by a repository rule with the same discovery as the
runner (`@yapnr_atopile`, `bazel/atopile/repo.bzl`), and `yapnr_atopile_build` runs the runner as
an action:

```starlark
load("@yapnr//bazel/atopile:defs.bzl", "yapnr_atopile_build")

yapnr_atopile_build(
    name = "board",
    ato_yaml = "ato.yaml",
    srcs = glob(["elec/src/**", "elec/layout/**"]) + ["yapnr-parts.lock.json"],
    build = "default",
    cache = "https://parts.example.org",  # or --action_env=YAPNR_PART_CACHE=<dir or URL>
)
```

It produces `board.kicad_pcb`, `board.bom.csv`, `board.result.json`, the `board.out/` directory
and a `YapnrAtopileBuildInfo` provider. The action is `local` and `no-remote` (it uses an
environment and a cache Bazel does not track) and not `requires-network`. It copies only the
declared `srcs` (which must lie inside the project's directory) into its work directory, so an
undeclared file next to them (an untracked layout, say) can neither change the build nor make a
cached result stale. The part cache is the `cache` attribute, else
`--action_env=YAPNR_PART_CACHE`; there is no default inside Bazel (the strict action environment
has no `HOME`), and a project with a parts lock and neither fails at once. Without an atopile
environment only such targets fail, with the reason; after `yapnr atopile setup`, refetch with
`bazel fetch --force @yapnr_atopile//...` (or change one of the discovery variables).
`//tests/fixtures/atopile:synthetic` is a manual example.

## Versions

- yapnr pins atopile 0.15.8, the version Splanc's boards were built with.
- 0.15.9 comes in through an A/B on the synthetic fixture and Splanc's builds, which must give equal
  netlists, variables reports and annotation resolution; a version change is a one-line
  `requirements.in` edit plus regenerated locks and a new `pins.json`.
- The frontend depends on stable outputs only (`.ato` text, `atopile_address` footprint fields, the
  JSON reports). The hook is written for 0.15.8 and fails loudly on anything else.

## Tests

- `bazel test //tests/unit/atopile/... //tests/unit/partcache/...`: hermetic. Synthetic parts; the
  runner runs against a fake atopile interpreter (isolation, environment, outputs, input ids, a
  deadline kill of a process tree); the hook against fake atopile modules, including in `spawn`
  and `forkserver` workers.
- `//tests/e2e/atopile:test_atopile_build` (tags `atopile`, `manual`): real atopile 0.15.8
  builds of a synthetic project, each twice with the same input id: without any pick, with one
  pick by LCSC id and with one type pick, both served by the picker from a temporary part cache.
  A project with a git dependency must fail offline without cloning it, and every build skips
  atopile's reload of an open board in a running KiCad. Run it after `yapnr atopile setup`:

  ```sh
  bazel test //tests/e2e/atopile:all --test_env=YAPNR_ATOPILE_HOME="$HOME/.cache/yapnr/atopile"
  ```

## From Splanc

| Splanc                                               | yapnr                                                    |
| ---------------------------------------------------- | -------------------------------------------------------- |
| `hardware/tools/ato_build.sh`, rules_atopile actions | `yapnr atopile build`, `yapnr_atopile_build`             |
| Nix `atopile.nix` venv with a fixed-output hash      | `yapnr atopile setup` from hashed per-platform locks     |
| patch: empty query returns `[]`                      | the hook (`fetch_parts_multiple`)                        |
| patch: dummy token for a localhost URL (substring)   | the hook: parsed loopback URL and port, per request      |
| wrapper: `OPENSSL_armcap=0`, `ATO_NON_INTERACTIVE`   | the build environment                                    |
| wrapper: `ATO_STOCK_FP_LIB_TABLE` (all Nix libs)     | the referenced KiCad 10 stock libraries                  |
| `picker_server.py` + sibling `catalog.json`          | `yapnr picker`, catalog schema v1, catalogs as arguments |
| `picker_catalog.json`, committed parts               | the local part cache and a parts lock per board          |
| `board_outline.py` (`outline_margin_mm`)             | `--outline-margin-mm`                                    |

## Not done yet

- The `/opt/atopile` environment in the `yapnr` image (A4): the builder stage for the aarch64
  wheel, the notices, the size measurement.
- An A/B of KiCad 9 against KiCad 10 on netlists and DRC. No end-to-end project references a
  stock `Library:Footprint` yet, so whether atopile 0.15.8 reads every KiCad 10 stock footprint
  is unchecked; the referenced-library scan reads `.ato` text only.
- `yapnr parts add` (the port of `gen_parts_robust.sh`, A6), catalog builders (A7) and the board
  macro (A8).
