# yapnr migration plan

Status: **approved plan; PR0 and PR-R merged, PR1 in review** (2026-09-30). This file is committed
to the public yapnr repository. It contains no machine paths, host names, network addresses or
personal e-mail addresses; operators bind the path variables below locally.

| Variable   | Meaning                                                              |
| ---------- | -------------------------------------------------------------------- |
| `$SPLANC`  | Local Splanc checkout, branch `splanc-mini` (includes unpushed work) |
| `$HIER`    | `$SPLANC/output/hier`, the hierarchical-PnR experiment area          |
| `$FREEZE`  | A frozen copy of the Codex agent's uncommitted engine work           |
| `$YAPNR`   | Local yapnr clone                                                    |
| `$SCRATCH` | A scratch directory outside both repositories                        |

## 0. Summary

yapnr ("yet another place and route") is the PnR system that grew inside Splanc (`hardware/pnr`,
parts of `hardware/tools`, the live viewer, and the experiment harness in `$HIER`). This plan
moves it into its own repository in reviewable pieces, removes the Splanc-specific assumptions,
adds a **project abstraction** that owns every runtime artifact, and ends with Splanc consuming
yapnr as a **headless Bazel ruleset**.

### 0.1 Facts verified for this plan

- **The repository is public.** `Studio-Fug/yapnr` is public; `main` holds one bootstrap commit
  (`LICENSE`, `README.md`, `.gitignore`, `branding/`).
  - Consequence: everything pushed is public. The privacy scrub (Appendix C) is a hard gate for
    every push, including imported history.
- **License text.** `LICENSE` is byte-identical to Splanc's (AGPL-3.0 text, 34,504 bytes) and is
  committed on `main`.
  - Consequence: GitHub detects `agpl-3.0`; the SPDX identifier is declared as in §1.4.
- **Splanc's declarations are inconsistent.** Its Sphinx `conf.py` says `AGPL-3.0-or-later`, its
  `package.json` says `UNLICENSED`, and there are no SPDX headers.
  - Consequence: yapnr declares one identifier consistently (§1.4).
- **Imported history has several author identities, not all of them noreply addresses.**
  - Consequence: the history import rewrites identities with a mailmap (§7.2) so that every
    imported commit uses the owner's public commit address (decided, Q2 in §9).
- **History under the PnR paths is small:** 516 blobs, 5.5 MB; the largest blob is 554 KB
  (`testdata/splanc_dev/splanc_dev.kicad_pcb`).
  - Consequence: a full-history import is cheap and passes the 600 KB large-file hook.
- **The newest engine is uncommitted.** Snapshots `src8b`, `src9.frozen`, `src10*`, `src11*`,
  `src12`, `src12h`, `src12i` are done; `src12b`/`src13` (USB pairs) and `src12n` (N-0001) are in
  progress. `src2` to `src7` no longer exist on disk.
  - Consequence: PR2 recreates the lineage as a commit series from the surviving snapshots (§7.3).
  - Update (PR2, 2026-09-30): `src13` and `src12n` were finished and merged into `src15` (with
    `src14`, SI v1), the engine of the H7 experiment; PR2 imports the whole lineage up to `src15`
    plus Electrical221 (§7.3, [import manifest](history/import-manifest.md)).
- **Splanc CI has never been green for PnR code** (lint debt; the Linux test job fails on an
  unrelated Nix package), and CI never runs the native (KiCad-side) code paths.
  - Consequence: yapnr gets a lint baseline and a real KiCad test lane **before** the big
    mechanical rewrite (§8, PR6a).

### 0.2 Owner decisions (2026-09-29)

1. **License `AGPL-3.0-or-later`** (SPDX), matching Splanc's declaration.
2. **Owner-only contributions for now.** `CONTRIBUTING.md` states that outside contributions are
   not accepted yet.
3. **Commit identity: the owner's public commit address**, listed in
   `tools/privacy/allowed_identities.txt`, for new commits and for imported history; GitHub
   noreply addresses remain accepted. This replaces the first form of the decision (noreply
   addresses only): GitHub accepts only an address verified on the account as the author of a
   merge made on the website, and the owner chose to publish this address on commits. CI checks
   every new commit.
4. **Viewer Ask agent and AI summaries ship off by default** (PR4c).
5. **elkjs is fetched at build time, pinned by sha256, never vendored** (PR4b). `THIRD_PARTY.md`
   lists it and three.js.
6. **GitHub issues** (`#N`) replace Splanc's `FUG-NNN` keys.
7. **GitHub Pages is approved.** Deploy and per-PR previews are wired like Splanc and gated on the
   repository variable `YAPNR_PAGES_ENABLED == 'true'`, which is set after the first green build
   of `main`.

### 0.3 Design decisions in this plan (owner can override)

1. Python package `yapnr` (renamed from `pnr`), import root = repository root.
2. The viewer is part of the package (`yapnr/viewer`), not a separate web app.
3. The Rust search backend lives in `native/search` and is built by `rules_rust`.
4. Tests are split by **interpreter**: `tests/unit` (hermetic Python 3.11), `tests/kicad` (KiCad's
   Python), `tests/regression` (end-to-end ladder).
5. KiCad in CI: official Docker image `kicad/kicad:10.0.6` for the required Linux KiCad lane;
   Homebrew cask on `macos-latest` as a nightly parity lane. **No Nix in yapnr.**
6. The engine's `PNR_*` environment flags keep their names; they get a typed registry. New
   infrastructure variables use `YAPNR_*`.
7. History: full history of the PnR paths via `git filter-repo`, then the uncommitted snapshots as
   a commit series, landed with merge commits (everything else is squash-merged).
8. Mechanical changes (format, rename, moves) are separate commits, and their commits on `main` are
   listed in `.git-blame-ignore-revs`. Squash merging rehashes, so a hash is added in a follow-up
   after the merge (PR3b, whose pure-move commit must survive, is merged with a merge commit).
   In-flight engine work is ported through them with a checked-in tool.
9. `drc_warm` (GUI-bound) and the old `keyhole_{repair,loop,shift_via,deform}` adapters are
   imported with history but deleted during restructuring. They remain recoverable from history.

## 1. Repository layout

```text
yapnr/                          repository root
├── LICENSE                     AGPL-3.0 text (committed on main)
├── README.md  AGENTS.md  DEVELOPERS.md  CONTRIBUTING.md  WORKLOG.md  THIRD_PARTY.md
├── MODULE.bazel  MODULE.bazel.lock  .bazelversion  .bazeliskrc  .bazelrc  .bazelignore
├── BUILD.bazel                 requirements targets, top-level aliases (//:yapnr, //:doctor)
├── requirements.in  requirements.lock  requirements_linux_x86_64.lock (PR6a)
├── .pre-commit-config.yaml  .flake8  .markdownlint.json  .markdownlintignore
├── .prettierrc  .prettierignore  .gitignore  .git-blame-ignore-revs  setup-precommit.sh
├── .github/                    workflows/{ci.yaml,macos.yaml}, CODEOWNERS,
│                               pull_request_template.md
├── yapnr/                      the Python package
│   ├── __init__.py  __main__.py
│   │                           `python -m yapnr` == CLI
│   ├── cli/                    argparse CLI, one module per command group (PR0: cli.py)
│   ├── core/                   graph, constraints, electrical, plane_intent,
│   │                           power_topology, fab_profile, flags (registry), rules
│   ├── place/                  differentiable placement, legalize, power_first, hull, ...
│   ├── route/                  global route, feedback loop, detail/ (maze, keyhole, ...)
│   ├── native/                 KiCad-side native loop and electrical stages (stdlib only)
│   ├── shove/  hier/  mc/  feedback/
│   │                           as today
│   ├── kicad/                  all KiCad I/O: ingest, writeback, library_table, specctra, drc
│   │   ├── toolchain.py        KiCad discovery (kicad-cli, KiCad Python, headless copy)
│   │   ├── staging.py          stages the KiCad-side closure for worker processes
│   │   └── workers/region.py   today's hardware/tools/keyhole_region.py
│   ├── runtime/                live telemetry, runtime controls, profiling, proc (timeouts)
│   ├── frontends/              design-input plugins
│   │   ├── base.py             DesignFrontend protocol, canonical annotations
│   │   ├── atopile/            .ato annotations, atopile_address, module hierarchy, index
│   │   └── kicad/              sheet-path addresses, sidecar annotations, footprint fields
│   ├── project/                manifest, store, objects, inputs, snapshots, runs, supervisor,
│   │                           profiles, retention, bundle, kicad_export, baseline, legacy_hier
│   ├── diagnostics/            via proximity, pair-contact audit, congestion exports/renders
│   ├── review/                 PDF/PNG review exports (optional deps)
│   ├── stages/                 experimental native post-stages (plane access, ground)
│   └── viewer/                 live viewer: server, services/, notes/, kicad_scripts/, static/
├── native/search/              search.rs (heading-aware A*), BUILD.bazel
├── bazel/                      downstream ruleset: defs.bzl, providers.bzl, extensions.bzl,
│                               toolchains/, private/ (PR8)
├── tests/
│   ├── unit/<subpackage>/      hermetic Python 3.11 + numpy/torch/yaml; no KiCad
│   ├── unit/repo/              repo checks: test wiring, privacy scan (PR0)
│   ├── kicad/<subpackage>/     run under KiCad's Python and/or need kicad-cli
│   ├── regression/             generic 8-circuit ladder (designs.py, run.py, native.py, ...)
│   ├── fixtures/               boards/splanc_dev (79 parts), feedback/, power_topology/, graphs/
│   ├── e2e/                    viewer browser tests (Chrome DevTools client), manual
│   └── downstream/             tiny consumer workspace for the ruleset (PR8)
├── examples/
│   ├── led555/                 KiCad-native example project (CI fixture, frontend "kicad")
│   └── atopile-blinky/         atopile-frontend fixture (sources + prebuilt board)
├── third_party/                three/ (MIT, vendored); elkjs/ (EPL-2.0, license text only)
├── tools/                      privacy_scan.py, check_test_wiring.py, repo_root.py,
│                               bazel/py_tests.bzl (PR0); kicad_test_runner.sh, migrate/
└── docs/                       Sphinx (MyST) site; see §6
```

### 1.1 Rationale for the main choices

- **One package, subpackages by subsystem.** Today's `pnr/` has about 80 flat modules. The rename
  maps them into `core`, `native`, `kicad` and `runtime`, so the KiCad boundary is visible in the
  tree. `native/` stays flat in the first pass so the move is purely mechanical. Appendix A has
  the full module map.
- **Interpreter boundary is explicit.** Modules that run inside KiCad's Python (3.9 on macOS, 3.13
  in the Docker image, 3.14 in nixpkgs) must be stdlib-only and 3.9-parseable. A unit test computes
  the import closure of every worker entry point and enforces both rules. That closure replaces
  the hand-maintained `pnr_kicad_srcs` filegroup, which is already missing modules.
- **A single KiCad adapter package.** KiCad plans to remove the SWIG `pcbnew` bindings in 11.0. New
  `pcbnew` use goes through `yapnr.kicad`. A lint test ratchets the number of `import pcbnew` sites
  outside `yapnr/kicad` and `yapnr/native` (about 225 lazy imports today) so an IPC-API backend
  can be added later.
- **Viewer inside the package.** The server is stdlib Python that imports engine modules. Keeping
  it in `yapnr.viewer` makes `bazel run //:viewer` and version pinning trivial. Static assets live
  in `yapnr/viewer/static/`.

### 1.2 Test tiers

| Tier        | Location           | Interpreter                  | Bazel tags                     |
| ----------- | ------------------ | ---------------------------- | ------------------------------ |
| unit        | `tests/unit`       | hermetic 3.11 (`yapnr_pypi`) | none, or `slow`                |
| kicad       | `tests/kicad`      | KiCad Python, `kicad-cli`    | `kicad`                        |
| regression  | `tests/regression` | both                         | `kicad`, `slow`; ladder manual |
| e2e viewer  | `tests/e2e`        | hermetic + Chrome            | `manual`                       |
| repo checks | `tests/unit/repo`  | hermetic                     | `external` (never cached)      |

Where they run:

- unit and repo checks: the `test` job on every PR (`ubuntu-24.04-arm`) and the informational
  macOS job;
- kicad: the `kicad-integration` job on every PR (from PR6a) and the macOS nightly;
- regression: a smoke case in `kicad-integration`, the full ladder nightly;
- e2e viewer: on demand.

Repo checks include: every `tests/**/test_*.py` is wired to a Bazel target (in Splanc 66 test files
are not registered); the privacy scan of the whole tree is clean (both in PR0). Later PRs add: the
KiCad-side closure is stdlib-only and 3.9-parseable; every `PNR_*` read is in the flag registry;
generated docs and schemas are fresh.

The repo checks read the whole working tree, which a Bazel test does not declare as inputs. They
locate the checkout by following the runfiles symlink of `//:MODULE.bazel`, refuse to run against
a runfiles or execroot copy, and are tagged `external` so Bazel never serves a cached result.

Tests stay `unittest`-style (no current test uses pytest). Four files need both torch and `pcbnew`
(`test_intermediate_pair_timing`, `test_native_electrical`, `test_native_placement_copper`,
`test_pair_bridge`); they are split during the move.

### 1.3 Examples and fixtures

- `examples/led555` is a real KiCad 10 project generated once from `tests/regression/designs.py`
  (the 555 timer circuit; stock footprints) and saved in KiCad 10 format. It has a `yapnr.toml`
  manifest, rules and a `ci-smoke` profile bounded to about 5 minutes. CI uses it end to end
  (import, run, export, DRC, pack/unpack).
- `examples/atopile-blinky` holds small `.ato` sources with `# @pnr-*` annotations plus a pre-built
  `.kicad_pcb` carrying `atopile_address` fields. It tests the atopile frontend without running
  atopile in CI.
- `tests/fixtures/boards/splanc_dev` keeps the existing 79-part dev board (already public in Splanc
  under AGPL). Provenance fields with absolute paths are scrubbed.
- The Splanc Mini inputs (`$HIER/inputs*`) are **not** migrated. They contain absolute URIs and
  belong to the Splanc project. Tests that use them become `manual` tests that take an external
  project path from `YAPNR_EXTERNAL_PROJECT`.

### 1.4 License and top-level documents

- **License: `AGPL-3.0-or-later`** (owner decision). `LICENSE` is the verbatim AGPL-3.0 text. The
  SPDX identifier is declared in:

  - `README.md` (license section);
  - `docs/_sphinx/conf.py` (`copyright`, shown in the site footer);
  - `CONTRIBUTING.md` (inbound = outbound).

  No per-file SPDX headers for now: one `LICENSE` plus one identifier keeps a later relicense
  cheap. Code imported from Splanc is already AGPL-3.0-or-later by the same owner, so it is
  compatible.

- **No license metadata in Bazel for now.** `rules_license` is not a dependency. If metadata is
  added later, it goes into `REPO.bazel` as `repo(default_package_metadata = [...])`, which covers
  every package; never into a root `package(default_package_metadata = ...)`, which covers only
  the root package.
- **Network use (AGPL section 13).** The viewer is served over a network. It gets an About/Source
  link showing the repository URL and the exact engine commit of the run being viewed.
- **Third-party material** is listed in `THIRD_PARTY.md`:
  - three.js r180 plus `GLTFLoader` (MIT), vendored under `third_party/three/` with its license
    (PR4d).
  - elkjs 0.9.3 (EPL-2.0, which contains an Apache-2.0 web-worker shim). It is **never
    committed**: it is fetched with a sha256-pinned `http_file` and served as a separate,
    unmodified file (never bundled or minified with AGPL code). Its license text is kept under
    `third_party/elkjs/` (PR4b).
  - KiCad stock footprints and 3D models (CC-BY-SA-4.0 with the KiCad library exception) are
    referenced, not vendored.
- **README.md:** what yapnr is, status (alpha, KiCad 10 only), quick start (`bazel run //:yapnr`),
  links to docs, license.
- **AGENTS.md** carries over the rules that governed the Splanc work:
  - selection is mechanical (Monte Carlo and halving): never hand-pick seeds or branches;
  - no model-driven hand routing;
  - nothing in the loop may be GUI-bound (no `wx.App`, no Dock-icon `kicad-cli`);
  - every worker is time-bounded;
  - native KiCad DRC is the judge: never suppress DRC or delete nets to claim completion;
  - new engine behaviour lands behind a default-off flag with an A/B result in the commit body;
  - public-repository privacy rules and the commit identity (the owner's public commit address);
  - do not disturb running experiments; check for other agents before long runs;
  - keep the WORKLOG convention.
- **DEVELOPERS.md** covers:
  - setup: bazelisk, `./setup-precommit.sh` (prek in isolation, never system-wide), KiCad 10, and
    on macOS the headless KiCad copy (later automated by `yapnr kicad make-headless`);
  - everyday commands and test tiers;
  - the Bazel output base on an internal, case-sensitive disk, and `--config=lowmem`;
  - lockfile regeneration and a CI overview.
- **CONTRIBUTING.md** covers:
  - the policy: outside contributions are not accepted yet (owner decision);
  - squash-merged PRs; commit style `<Area>: <summary> (#N)` with measured results in the body;
  - `prek run --all-files` before pushing;
  - the commit identity rule and the privacy rules;
  - the inbound license terms (inbound = outbound).
- **WORKLOG.md:** short live status board (in progress, next, blockers, do-not-retry), rewritten at
  the end of each session, not a diary.

## 2. De-Splanc-ification

Line numbers refer to `$HIER/src12h/hardware/pnr` unless another tree is named. Every item is
closed by the PR in brackets.

### 2.1 Repository layout and worker staging

- `native_loop.py:569`: `--repo` defaults to cwd, then `os.chdir(repo)`.
  - No repository root; the controller works in the run's work dir. [3c]
- `native_loop.py:585-600`: copies `pnr/` to `worker_root/hardware/pnr/pnr` and
  `hardware/tools/keyhole_region.py` next to it; sets `PYTHONPATH=worker_root/hardware/pnr`;
  adapter paths at lines 789, 834, 912, 939, 969, 994.
  - `yapnr.kicad.staging.stage(work_dir)` copies the computed KiCad-side closure to
    `<work>/.worker/yapnr/`. Workers run as `python -m yapnr.kicad.workers.region`. [3b/3c]
- `keyhole_region.py:22`: `sys.path.insert(parents[1]/'pnr')`.
  - Module entry point; `PYTHONPATH` set by the stager. [3b]
- `full_iteration.py:61`: `--repo parents[3]`; `mc/halving.py:43-44,961`;
  `hier/native_block.py:16-17`.
  - Removed. Staging plus toolchain discovery. [3c]
- `feedback/signals.py:188,275,287`: `PNR_ROOT`; a regex on
  `.../hardware/splanc_dev/elec/src/*.ato`; the package regex `^pnr(\....)+$`.
  - Source roots come from `frontend.source_roots()`; the package regex uses `yapnr`. [3b/3e]
- `regression/run.py:14,33-35,64-65`; `regression/export_reviews.py:5,17`.
  - Tools are resolved from the package (`yapnr.diagnostics`, `yapnr.review`), not from repository
    paths. [3c]

### 2.2 Board-specific defaults

- `full_iteration.py:23-24`, `mc/halving.py:317-322`, `hier/native_block.py:89-94`: default
  `hardware/splanc_dev/mini-*-fab.json`.
  - Required inputs, supplied by the project input set (`rules/*.json`). [3c]
- `full_iteration.py:33`, `geometric_native.py:11,38`: `splanc_mini.ato` hard-coded.
  - Canonical `annotations.json` in the input set, produced by the frontend. [3c/3e]
- `full_iteration.py:51`: fallback to a Splanc `output/.../fp-lib-table`.
  - The input set always carries a `${KIPRJMOD}`-relative `fp-lib-table`; missing means error.
    [3c]
- `regression/native.py:22`: UUID namespace `splanc-regression/`.
  - `yapnr-regression/`. Regenerate goldens; recorded as an ADR. [3c]
- `native_loop.py:77,711,717`: phase `02-usb-pairs` used for every differential pair.
  - `02-diff-pairs`; the viewer maps the old name for old telemetry. [3c]
- `fab_profile.py:42`: engine default `jlc-pofv`.
  - Unchanged during migration. `yapnr init` writes `fab_profile` explicitly into each manifest,
    so a later default change cannot silently alter a project. [3d]

### 2.3 Toolchain paths (macOS-only today)

- `/Applications/KiCad/...` in `full_iteration.py:29-30`, `mc/halving.py:45`,
  `hier/native_block.py:15`, `hier/power_quality.py:37`, `regression/run.py:15,53-55`,
  `pnr.bzl:75`, viewer `server.py:129`, `cost_service.py:36`.
  - `yapnr.kicad.toolchain` (below). [6a/3c]
- `PNR_KICAD_CLI` is honoured only in 5 files; `via_coalesce.py:306` and
  `power_bank_stage.py:160` use a bare `kicad-cli`.
  - Every call goes through `toolchain.kicad_cli()`. A repo check forbids the literal `kicad-cli`
    elsewhere. [6a]
- `regression/run.py:55`: `--python` defaults to the Splanc experiment venv.
  - Defaults to `sys.executable`; workers use `toolchain.kicad_python()`. [6a]
- `drc_warm/launch_host.py`: GUI `pcbnew.app`, KiCad prefs dir, `splanc_drc120` plugin,
  `SPLANC_DRC120_ROOT`.
  - Not migrated (GUI-bound; the owner's rule). A future headless warm-DRC design is an ADR
    placeholder. [3b]
- `PNR_RUST_SEARCH_LIB` must be set by hand, pointing at a `.dylib` copied into every inputs dir.
  - The controller resolves the library from runfiles or `YAPNR_SEARCH_LIB` and passes it to
    workers. The library is removed from input sets. [5]

`yapnr.kicad.toolchain` resolves `kicad-cli` and KiCad Python in this order:

1. `YAPNR_KICAD_CLI` / `YAPNR_KICAD_PYTHON` (`PNR_KICAD_CLI` accepted as an alias).
2. `~/.config/yapnr/config.toml` `[kicad]`.
3. On macOS, `~/Applications/KiCad-headless.app` (background-only clone; no Dock icon). The stock
   `/Applications/KiCad/KiCad.app` is used only for KiCad's Python, never for `kicad-cli`, which
   would show a Dock icon on every call.
4. `kicad-cli` on `PATH` plus a `python3` that can `import pcbnew`.

It checks the version (10.x required), caches the result per process, and `yapnr doctor` prints
it. `yapnr kicad make-headless` (macOS) creates the background-only APFS clone that the Splanc work
currently makes by hand (clone, new bundle identifier, `LSBackgroundOnly`, ad-hoc signature). PR0
ships a `doctor` stub that reports the Python, numpy and torch versions and whether
`YAPNR_KICAD_CLI`/`PNR_KICAD_CLI` is set, without ever running KiCad.

### 2.4 atopile coupling becomes a frontend

- `ingest.py:118-125` reads the footprint field `atopile_address` into `Component.address`.
  - An `AddressProvider` from the frontend. atopile uses `atopile_address`; KiCad-native uses the
    sheet path plus reference (from the footprint's `Sheetname`/`Sheetfile`/`path`). [3e]
- `electrical.py:29,41,46,126,135` parse `# @pnr-current` / `# @pnr-pair` from `.ato` and match
  addresses with `._p` trimmed; `plane_intent.py:33` parses `# @pnr-plane-access`.
  - Parsing moves to `frontends/atopile/annotations.py`, which emits canonical annotations
    (`yapnr-annotations-v1`). `core.electrical` / `core.plane_intent` compile only canonical
    annotations. [3e]
- `hier/blocks.py:41-62` derives blocks from dotted atopile module paths.
  - `frontend.hierarchy()`: the atopile module tree, or KiCad hierarchical sheets. [3e]
- `source_footprints.py`, `writeback.py` assume an atopile-resolved, row-placed board.
  - Frontend capability `initial_placement = "rows" | "as-is"`. [3e]
- Viewer `ato_index.py`, `source_service.py` (atopile elaboration, `SPLANC_ATO_SRC`).
  - `frontend.source_index()`; the atopile implementation moves to
    `frontends/atopile/index.py`. [4b]
- `pnr.bzl` loads `AtopileLayoutInfo` from `@atopile_rules`.
  - The ruleset takes plain files (`.kicad_pcb`, footprints, sources). The atopile adaptation is a
    few lines in the consumer (Splanc). [8]

Planned annotations (`@pnr-inductance` N-0002, `@pnr-noise-*` N-0003) go straight into the
canonical schema, so both frontends get them.

### 2.5 Tests, fixtures and documentation

- `tests/test_shove.py:224`, `tests/test_shove_native.py:29-32`: absolute case paths and runtime
  Python.
  - Fixtures under `tests/fixtures`; toolchain discovery. [3c]
- `test_halving_generations.py:26-27`, `test_feedback_moves.py:19-20`,
  `test_place_pair_weights.py:18-19`, `test_synth_native_generations.py:22`,
  `test_src2_library.py:25,28`: reach into `$HIER` via `parents[4]`.
  - Small extracted fixtures where possible; otherwise `manual` tests on
    `YAPNR_EXTERNAL_PROJECT`. [3c]
- `power_topology_golden.py:43-44`, `test_fab_profile.py:20`, `test_regression_inputs.py:10`.
  - Fixtures. [3c]
- `testdata/power_topology/{converter,pd}.json` embed absolute provenance paths.
  - Scrubbed to relative paths **before the first push** of that data. [2]
- `tests/test_src2_native-loop.py` has a hyphen in its name.
  - Renamed. [3b]
- `__init__.py` cites FUG-138 and Splanc docs; `fab_profile.py`/`via_in_pad.py` cite
  `docs/fab-comparison.md` (which exists only in `$HIER/docs`).
  - yapnr docs pages; the fab comparison is imported, scrubbed, as `docs/engine/fab-profiles.md`.
    [7]

### 2.6 Viewer

- `server.py:11,21,148`: repo = `parents[3]`, `hardware/pnr` on `sys.path`.
  - The viewer imports the installed `yapnr`. Subprocess services run the engine snapshot
    recorded in the run manifest. [4a]
- `server.py:28-43,127,167`: `inputs10b`, `splanc_dev` defaults, prefs under `output/`,
  `restart-status.json`.
  - `--project`; paths from the manifest and store (`user/settings.json`). [4a]
- `agent_service.py:29,164,175-212`, `net_llm.py:14`: Splanc prompt, file list, absolute path to
  the `claude` CLI.
  - The prompt and document list come from the manifest `[viewer]`. The CLI is found with
    `shutil.which`. **Off by default** (owner decision). [4c]
- `notes_store.py:29-30,601-602`, schemas `splanc-notes-v1`, MCP server `splanc_notes`, env
  `SPLANC_*`.
  - `yapnr-notes-v1`, `yapnr_notes`, `YAPNR_*`; store dir `notes/`. Existing `notes.jsonl` lines
    carry no schema tag, so this is safe. [4c]
- UI title "SPLANC", `window.Splanc*`, `splanc:*` events, `splanc-*` localStorage keys;
  `agent.js:110` strips `/elec/src/`.
  - `yapnr` names; the prefix to strip comes from the frontend. [4a]
- `cost_compute.py:13` imports `pnr.capacitor_intent`, which was never committed anywhere.
  - Recover the module if it exists in a snapshot; otherwise remove the code path and file an
    issue. [4b]
- Some tests and fixtures embed machine-local values (network literals, absolute source roots).
  - Documentation-range values; fixtures regenerated from `examples/`. [4a/4c]

## 3. Project abstraction

A **yapnr project** is a directory that holds the design inputs, rules and experiment specs
(committed), plus a **store** that holds everything the engine and viewer produce (not committed,
relocatable, content-addressed). It replaces the ad-hoc `$HIER` layout: `inputs*/`, `src*/`,
`runs/`, `blocks/`, `live/`, `notes/`, `drc-cache*/`, `env2.json` and the `launch_*.sh` scripts.

### 3.1 On-disk layout

A project may live inside another repository (for example Splanc's `hardware/splanc_dev/pnr`).
`[C]` marks committed files, `[L]` machine-local ones (gitignored).

```text
<project>/
├── yapnr.toml                  manifest, schema yapnr-project-v1                        [C]
├── yapnr.local.toml            machine-local overrides (store path, workers)            [L]
├── design/                     frontend inputs                                          [C]
│   ├── <name>.kicad_pro, <name>.kicad_pcb
│   │                           (kicad frontend; atopile builds its board elsewhere)
│   └── annotations.yaml        kicad-frontend contracts (atopile uses .ato comments)
├── rules/                                                                               [C]
│   ├── constraints.yaml        today's constraints.yaml (schema v0)
│   ├── electrical-fab.json     current/copper/temperature/via budgets
│   ├── plane-access-fab.json   plating/loss/drop model
│   └── fab-profile.toml        optional overrides on a built-in profile
├── profiles/<name>.toml        named flag profiles                                      [C]
├── experiments/<name>.toml     declarative experiment specs                             [C]
└── .yapnr/                     THE STORE (default location; see store.path)             [L]
    ├── store.json              {schema: yapnr-store-v1, project_id, created, layout_version}
    ├── objects/sha256/<2 hex>/<62 hex>
    │                           immutable blobs; hard-linked or APFS-cloned into place
    ├── inputs/<input-id>/      frozen input set (today: $HIER/inputs10b)
    │   ├── inputs.json         {frontend, source_rev, files: {name: sha256}, created}
    │   ├── board.kicad_pcb  board.kicad_pro  fp-lib-table (${KIPRJMOD} URIs)
    │   ├── graph.json  rules.json (relative source paths)  annotations.json
    │   ├── constraints.yaml  electrical-fab.json  plane-access-fab.json
    │   └── footprints/<nick>.pretty/*.kicad_mod
    ├── engines/<engine-id>/    frozen engine snapshot (today: $HIER/srcNN)
    │   ├── engine.json         {git_commit, dirty_patch_sha256, tree_sha256,
    │   │                        native_lib_sha256, python_lock_sha256, created}
    │   └── yapnr/...           read-only source copy (+ libpnr_search)
    ├── runs/<run-id>/
    │   ├── run.json            provenance (below)
    │   ├── status.json         engine-written progress (unchanged format)
    │   ├── result.json         normalized objective summary and best-candidate pointer
    │   ├── logs/run.log  logs/supervisor.log
    │   ├── work/               engine work tree, unchanged:
    │   │                       cand/pNNN/{placed.json,rung1/,native/,deep/}
    │   └── .pruned             retention marker
    ├── libraries/<library-id>/ block libraries (today: blocks/<run>/)
    │   ├── library.json  blocks.json
    │   └── <template>/{library.json, trials.jsonl, native/<tag>/...}
    ├── live/<run-id>/          telemetry (PNR_LIVE_DIR points here)
    │   ├── events/<ns>-<hex>.json
    │   │                       yapnr-live-event-v1 (compatible with pnr-live-event-v1)
    │   └── boards/<sha>.kicad_pcb
    │                           hard links into objects/
    ├── baselines/<baseline-id>.json
    │                           hand-edited board lineage
    ├── user/                   viewer user state; never auto-deleted
    │   └── pins/  drafts/  snapshots/  controls.json  settings.json
    ├── notes/                  notes.jsonl (source of truth), conversations/; never auto-deleted
    ├── cache/                  derived; always safe to delete
    │   └── drc/  geometry/  schematic/  source/  component-costs/
    └── locks/                  advisory fcntl locks (store, per-run)
```

Artifacts fall into four classes, and every command treats them consistently:

- **Provenance and results:** `run.json`, `result.json`, `status.json`, final boards, `inputs/`,
  `engines/`. Kept; referenced objects are pinned.
- **Telemetry:** `live/`. Append-only; GC keeps lanes referenced by pins, snapshots or best
  candidates.
- **User state:** `user/`, `notes/`. Never deleted automatically; always included in bundles
  unless excluded.
- **Cache and intermediates:** `cache/`, `runs/*/work/**` intermediates. Deletable; `prune`
  implements today's `autoprune.sh` policy.

### 3.2 Identifiers and provenance

- **Engine id** `e-<12 hex>`: the first 12 hex digits of the tree hash over `yapnr/**/*.py`, the
  native library hash and the flag registry. A run with `engine = "workspace"` freezes the working
  tree into `engines/` at launch, using clones or hard links. Every run is therefore reproducible,
  and in-progress development can never change a running experiment. This is today's `srcNN`
  discipline, made automatic.
- **Input id** `i-<12 hex>`: hash of the canonical `inputs.json`.
- **Profile hash**: hash of the resolved flag environment.
- **Run id** `r-<YYYYMMDD>-<HHMMSS>-<label>-<4 hex>`. The suffix comes from the run key =
  hash(engine id, input id, profile hash, kind, params, seed). `yapnr run` warns when an identical
  run key already exists.
- **Candidate address**: `<run-id>:<cand>[/<stage>]`, e.g. `r-20261004-0930-h7-3f2a:p027/deep`.
  Telemetry lane ids use the same form.
- **Code keys**: the feedback subsystem's code-key provenance keeps working. Keys are rooted at
  `yapnr.` and include the engine id.

`run.json` (schema `yapnr-run-v1`) records:

- `id`, `kind`, `label`, `spec` (copy of the experiment TOML), `params`, `seeds`;
- `engine` (id and `engine.json`), `inputs` (id), `profile` (name, resolved env, hash);
- `argv`, `after` (dependency runs), `parent` (for `resume`);
- `host` (OS, CPU count, KiCad version; **no host name**);
- `supervisor` (pid, start-time identity), `state` (`queued|running|paused|done|failed|stopped`);
- `started`, `ended`, `result` (pointer to `result.json`).

### 3.3 Manifest (`yapnr.toml`, schema `yapnr-project-v1`)

The source of truth is a dataclass model in `yapnr/project/manifest.py`. The JSON Schema
(`yapnr/project/schema/project-v1.schema.json`) is generated from it and pinned by a freshness
test. TOML is read with `tomllib`; manifests are only read by the controller, never by KiCad-side
workers.

```toml
schema = "yapnr-project-v1"
name = "splanc-mini"
description = "Splanc Mini main board"

[design]
frontend = "atopile"                  # "kicad" | "atopile" (plugin registry)

[design.atopile]
root = "../elec"                      # directory with ato.yaml, relative to the project
build = "mini"
entry = "src/splanc_mini.ato:SplancMini"
annotation_sources = ["src/splanc_mini.ato"]
# or ["ato", "build", "-b", "mini"]
build_command = ["bazel", "build", "//hardware/splanc_dev:splanc_mini"]
board_output = "../build/builds/mini/mini.kicad_pcb"

# [design.kicad]                      # kicad frontend instead:
# project = "design/led555.kicad_pro"
# annotations = "design/annotations.yaml"

[rules]
constraints = "rules/constraints.yaml"
electrical_fab = "rules/electrical-fab.json"
plane_access_fab = "rules/plane-access-fab.json"
fab_profile = "jlc-pofv"              # built-in name, or a path to a profile file

[engine]
default_profile = "default"

[store]
path = ".yapnr"                       # override in yapnr.local.toml or YAPNR_STORE

[retention]
prune_intermediates_after = "2m"      # after the run's evaluation.json appears
keep_live = "referenced"              # "all" | "referenced" | "none"

[resources]
disk_pause_below_gb = 8
disk_resume_above_gb = 15
keep_awake = true                     # macOS caffeinate -w <pid>

[viewer]
title = "Splanc Mini"
docs = ["../../docs/hardware/mini.md"]  # context offered to the optional agent
agent = false                         # off by default (owner decision)
```

Machine-level settings (KiCad paths, the path to the optional agent CLI, the default store root)
live in `~/.config/yapnr/config.toml` or `YAPNR_*` variables. They never go in the committed
manifest.

**Profiles** (`profiles/<name>.toml`) replace `env.json`/`env2.json`. Machine paths are split out
into toolchain config:

```toml
schema = "yapnr-profile-v1"
extends = "default"
[flags]
PNR_POWER_FIRST = true
PNR_SHOVE = true
PNR_FEEDBACK = true
PNR_WORKER_TIMEOUT = 1800
```

Flags are validated against the registry (`yapnr/core/flags.py`: name, type, default, owning
module, status `experimental|stable|deprecated`, doc string). Unknown flags are an error. The
registry generates `docs/engine/flags.md`.

**Experiment specs** (`experiments/<name>.toml`) replace the imperative `launch_*.sh` scripts:

```toml
schema = "yapnr-experiment-v1"
# full | halving | hier | block-library | pair-bench | doe | regression
kind = "halving"
label = "h7"
inputs = "latest"         # or an input id
engine = "workspace"      # or an engine id
profile = "h6"
seeds = [6]
after = ["nb8"]           # wait for these runs (by label or id)
[params]                  # passed to the kind's argv builder; validated per kind
candidates = 64
deep_seconds = 5400
library = "nb7h"          # a library id or label (hier kind)
[flags]                   # per-experiment overrides on top of the profile
PNR_MACRO_HULL = true
[resources]
workers = 6
```

Each `kind` is a small plugin in `yapnr/project/kinds/` that maps `params` to the engine entry
point (`yapnr.native.full_iteration`, `yapnr.mc.halving`, `yapnr.hier.synth_native`,
`yapnr.native.paired_bootstrap`, ...) and knows how to summarize results into `result.json`. `doe`
needs a public replacement for the private `synth_native._native` that `doe_block.py` uses today.

### 3.4 CLI

All commands take `-C <project>` (default: nearest ancestor with `yapnr.toml`) and `--json` for
machine-readable output. Via Bazel: `bazel run //:yapnr -- <args>`, with relative paths resolved
against `BUILD_WORKING_DIRECTORY`.

- `yapnr init [DIR] --frontend kicad|atopile [--from-kicad PRO] [--from-atopile DIR --build B]`
  - Create the manifest, `rules/` templates, `profiles/default.toml`, and a `.gitignore` entry
    for the store.
- `yapnr doctor`
  - Report toolchain discovery (KiCad version, headless status, `pcbnew` import, Python,
    torch/numpy, native search library) and store health. PR0 ships a stub.
- `yapnr kicad make-headless`
  - macOS: create the background-only KiCad copy used for `kicad-cli`.
- `yapnr import kicad <pro|pcb>`
  - Freeze a KiCad project into a new input set (portable `fp-lib-table`, footprints copied,
    graph and rules dumped).
- `yapnr import atopile [--build B] [--board PCB]`
  - Run the configured build (or take a prebuilt board), parse annotations, freeze an input set.
- `yapnr import legacy-hier <dir>`
  - Register an existing `$HIER`-style tree (engines from `src*`, inputs, runs, block libraries,
    live telemetry, notes) **read-only**, by hard link when on the same volume.
- `yapnr inputs ls|show|diff A B`
  - Inspect input sets.
- `yapnr snapshot create [--label L]|ls|verify ID|diff A B`
  - Engine snapshots.
- `yapnr run <experiment.toml> [--profile P] [--engine E] [--inputs I] [--label L] [--detach]`
  - Launch under a supervisor (pid identity, keep-awake, disk guard, telemetry dir, log).
- `yapnr run --kind K [params...]`
  - Ad-hoc run without a spec file (the spec is still recorded).
- `yapnr resume <run-id>`
  - Continue a stopped or failed run where the kind supports it.
- `yapnr status [<run-id>] [--watch]`
  - Today's `status.sh`: disk, load, per-run stage stats, block trial stats, deep evaluations.
- `yapnr stop|pause|continue <run-id>`
  - Signal only processes this store launched (verified by pid and start time).
- `yapnr ls runs|libraries|inputs|engines|baselines`
  - List.
- `yapnr show <run-id>[:cand[/stage]]`
  - Details, and the objective vector
    `[violations, blocked, reference, subwidth, unqualified_pairs, unconnected]`.
- `yapnr compare <run|cand> <run|cand>...`
  - Objective and per-stage tables; engine, input and flag diffs between the runs.
- `yapnr prune [--dry-run] [--run R]`
  - Retention policy on intermediates (today's `autoprune.sh`).
- `yapnr gc [--cache] [--live] [--objects] [--dry-run]`
  - Telemetry and object GC (today's `prune_live.py`); never touches `user/` or `notes/`.
- `yapnr pack <run-id>[:cand[/stage]] -o F`
  - Portable bundle (§3.5). Options: `--best`, `--with-telemetry`, `--without-user-state`,
    `--without-engine`.
- `yapnr unpack <bundle> [-C project]`
  - Restore a bundle into a project store.
- `yapnr export kicad <run-id>[:cand[/stage]] -o DIR [--zip] [--with-3d]`
  - Standalone KiCad project (§3.6).
- `yapnr baseline add <edited.kicad_pcb> --from <run:cand>`
  - Re-import a hand-edited board as a new baseline input set (§3.6). Options:
    `--lock none|placement|copper`, `--name N`.
- `yapnr viewer [--run R] [--port N] [--listen ADDR]`
  - Serve the live viewer for this project (loopback by default).
- `yapnr notes ls|export|show N`
  - Design notes.
- `yapnr drc <pcb>`
  - Native KiCad DRC through the discovered toolchain (headless).
- `yapnr review <run:cand>`
  - PDF/PNG review export (optional dependencies).

### 3.5 Save and restore (bundles)

- **Format:** a deterministic `tar.gz` (sorted entries, mtime 0, uid/gid 0; stdlib only) named
  `*.yapnr.tgz`, containing:
  - `bundle.json` (schema `yapnr-bundle-v1`: bundle kind, project name, the objects list with
    sha256 and size, run/input/engine ids);
  - `objects/`, `inputs/<id>/`, `engines/<id>/` (unless `--without-engine`);
  - `runs/<id>/{run.json,result.json,status.json,logs/}` plus the selected candidate's stage
    directory;
  - optionally `live/<id>/`, `user/`, `notes/`.
- **Restore** verifies every hash, imports objects, registers ids, and refuses id collisions with
  different content. User state is merged by id, never overwritten. A restored run can be viewed,
  compared, exported and resumed like a local one.
- **Portability:** everything inside a bundle is relative. Absolute paths are rejected at pack
  time (the privacy scanner is reused).

### 3.6 KiCad portability

`yapnr export kicad` produces a standalone project that opens on another machine:

- `<name>.kicad_pcb` (from the object store) and `<name>.kicad_pro` (from the input set, with net
  classes and rules);
- `<name>.kicad_dru` (written by `core.fab_profile` for the project's profile);
- `fp-lib-table` whose URIs are all `${KIPRJMOD}/footprints/<nick>.pretty`, with the used
  footprints copied (reusing `library_table --portable`);
- 3D models: `--with-3d` copies them to `${KIPRJMOD}/3dmodels/` and rewrites model paths;
  otherwise stock `${KICAD10_3DMODEL_DIR}` references are kept;
- `yapnr-provenance.json` (run, candidate, engine id, input id, flags);
- `kicad-cli pcb drc` is run in the exported directory and its report is included. The kicad CI
  lane asserts the export DRC matches the run's final DRC.

`yapnr baseline add` closes the loop after a human edits a board in KiCad:

1. Validate that the netlist matches the source input set (components, pads, nets). Differences
   are reported; ECOs require re-importing from the frontend (`--allow-netlist-change` only for
   renames the frontend can explain).
2. Diff against the source candidate: moved, rotated or flipped parts; added or removed tracks,
   vias and zones.
3. Create a new input set with the edited board as the seed. `--lock placement` emits fixed
   placement constraints. `--lock copper` keeps the hand copper as fixed copper (`fixed_copper`).
4. Record lineage in `baselines/<id>.json`. `yapnr run --inputs <new id>` continues from it.

## 4. Bazel

### 4.1 Module

`.bazelversion` is **7.7.1** (same as Splanc) so yapnr can be a `bazel_dep` of Splanc; both are
bumped together later. `.bazeliskrc` is copied. `MODULE.bazel.lock` is committed and CI runs with
`--lockfile_mode=error`.

PR0 declares only `rules_python`; the other dependencies arrive with the PRs that use them:

```starlark
module(name = "yapnr")  # no version since PR-R: the release tag is the version (docs/releases.md)

bazel_dep(name = "rules_python", version = "2.0.3")                     # PR0
bazel_dep(name = "bazel_skylib", version = "1.9.0")                     # PR5 (string_flag)
bazel_dep(name = "rules_shell", version = "0.6.1")                      # PR6a (sh_test runner)
bazel_dep(name = "rules_rust", version = "0.71.3")                      # PR5
# release tarballs, later:
bazel_dep(name = "rules_pkg", version = "1.2.0", dev_dependency = True)
# viewer JS tests (PR4):
bazel_dep(name = "rules_nodejs", version = "<pin in PR4>", dev_dependency = True)

python = use_extension("@rules_python//python/extensions:python.bzl", "python")
python.toolchain(python_version = "3.11", is_default = True)

pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip")
pip.parse(
    hub_name = "yapnr_pypi",  # must be unique across modules; Splanc uses "pypi"
    python_version = "3.11",
    requirements_lock = "//:requirements.lock",
    # PR6a: requirements_by_platform adds //:requirements_linux_x86_64.lock (CPU torch)
)
use_repo(pip, "yapnr_pypi")

rust = use_extension("@rules_rust//rust:extensions.bzl", "rust")
rust.toolchain(edition = "2021", versions = ["1.85.0"])  # root-first: Splanc's wins
use_repo(rust, "rust_toolchains")
register_toolchains("@rust_toolchains//:all")

kicad = use_extension("//bazel:extensions.bzl", "kicad")  # PR8
kicad.autodetect(name = "yapnr_kicad")
use_repo(kicad, "yapnr_kicad")
register_toolchains("@yapnr_kicad//:all")
```

- **Python deps** (`requirements.in`):
  - `torch>=2.2,<2.4` (CPU): Splanc's ceiling, and the experiment environment runs torch 2.3.1
    with numpy 1.26 (numpy 1 ABI). It is not what keeps the aarch64 lock CUDA-free: torch 2.4 to
    2.9 also restrict their `nvidia-*` and `triton` dependencies to x86_64;
  - `numpy>=1.26,<2` (torch 2.3 wheels use the numpy 1 ABI, and the experiment environment runs
    numpy 1.26); a unit test checks `torch.as_tensor(numpy.zeros(3)).numpy()`;
  - `pyyaml>=6`;
  - the docs stack at Splanc's locked versions: `sphinx` 9.0.4, `myst-parser` 5.1.0, `furo`
    2025.12.19, `sphinx-copybutton` 0.5.2, `sphinx-design` 0.7.0, `sphinxcontrib-mermaid` 2.1.0.
    `matplotlib` is added when a page needs generated figures.
- **The lock** is generated on the development Mac (darwin-arm64) with the hermetic Python 3.11
  toolchain (`bazel run //:requirements.update`). It is also valid on linux-aarch64, which is why
  the required `test` job runs on `ubuntu-24.04-arm`; it is not valid on linux-x86_64 (the x86_64
  torch wheel needs CUDA libraries the lock does not list). `//:requirements.test` checks
  freshness; it needs the network, is tagged `manual`, and runs in exactly one CI job (`test`).
- **Parity risk:** Splanc's Bazel lock pairs torch 2.3.1 with numpy 2.4.6, while the experiment
  venv runs numpy 1.26.4 on Python 3.12. yapnr's lock follows the experiment venv (numpy 1.26).
  PR6a runs the regression ladder under both the experiment venv and the lock and records any
  difference before experiments move to the Bazel environment.
- **No Nix.** yapnr does not need atopile, and KiCad comes from the toolchain (below). If Nix is
  ever added (for example a KiCad 11 canary), its extension usages must be
  `dev_dependency = True`, because `rules_nixpkgs` tags are root-module-only. Splanc had to patch
  `rules_atopile` for exactly this.

`.bazelrc`:

- Kept from Splanc:
  - the `startup` JVM flags (the SVE workaround);
  - `--enable_bzlmod`, the BCR registry, in-tree repository and disk caches;
  - `--incompatible_strict_action_env`, `--symlink_prefix=bazel-`;
  - `--test_output=errors`, colour, timestamps.
- Dropped: `workspace_status_command`, the Vite strategy, the memory caps, `build:td`, isolated
  extension usages.
- Added in PR0:
  - `test --test_tag_filters=-kicad` (the default lane needs no KiCad);
  - `test:quick --test_tag_filters=-kicad,-slow` and `test:all --test_tag_filters=`;
  - `build:lowmem --jobs=2 --local_resources=cpu=2 --local_resources=memory=HOST_RAM*.25` (for
    the shared, busy Mac; run niced, one Bazel server at a time);
  - `common:ci --lockfile_mode=error`;
  - `try-import %workspace%/user.bazelrc` (per-user output base and defaults).
- Added in PR6a: `test:kicad --test_tag_filters=kicad` plus `--test_env=` pass-through for
  `YAPNR_KICAD_CLI`, `YAPNR_KICAD_PYTHON` and `YAPNR_REQUIRE_KICAD` (strict action env strips
  them otherwise).

### 4.2 Targets

- Libraries: one `py_library` per subpackage: `//yapnr/core`, `//yapnr/place`, `//yapnr/route`,
  `//yapnr/native`, `//yapnr/kicad`, `//yapnr/shove`, `//yapnr/hier`, `//yapnr/mc`,
  `//yapnr/feedback`, `//yapnr/runtime`, `//yapnr/frontends/...`, `//yapnr/project`,
  `//yapnr/diagnostics`, `//yapnr/review`, `//yapnr/viewer`. In Splanc, `hier/`, `mc/` and
  `feedback/` are in no target at all. PR0 has `//yapnr` (version and CLI).
- `//yapnr:headless` (`py_library`): everything except `viewer` and `review`; what the ruleset
  uses.
- `//yapnr/kicad:kicad_side` (`filegroup`): generated from the import closure; the closure test
  pins it.
- `//:yapnr` (alias of `//yapnr:cli`): `bazel run //:yapnr -- <command>`. PR0.
- `//:doctor` (alias of `//yapnr:doctor`): `bazel run //:doctor`. PR0.
- `//:viewer` (alias): `bazel run //:viewer -- --project <dir> [--run R]`.
- `//yapnr/viewer:dev` (`py_binary`): viewer with auto-reload on a dev port; never the deployed
  ports.
- `//yapnr/kicad/workers:region` (`py_binary`): worker entry points are also runnable for
  debugging.
- `//native/search:pnr_search` (`rust_shared_library`, `.so`/`.dylib`) and
  `//native/search:search_test` (`rust_test`).
- `//native:search_backend` (skylib `string_flag`, `python`|`rust`): default `python` until PR5
  proves equivalence.
- `//tests/unit/...`: one `py_test` per file, generated by the `yapnr_py_tests()` macro
  (`tools/bazel/py_tests.bzl`) from a glob; the wiring test fails on orphans. PR0.
- `//tests/kicad/...`: `kicad_py_test` per file, tag `kicad` (below).
- `//tests/regression:smoke` (`py_test`, tags `kicad`, `slow`): one design, one seed, bounded
  time.
- `//tests/regression:ladder` (`py_binary`, `manual`):
  `bazel run //tests/regression:ladder -- --designs all --seeds 1,2` (16 cases).
- `//examples/led555:pnr`, `:pnr_test`, `:kicad_project`: ruleset targets that exercise the
  downstream API in-repo (PR8).
- `//docs:build`, `//docs:serve` (`py_binary`): as in Splanc. PR0.
- `//:requirements.update` and `//:requirements.test` (`compile_pip_requirements`, tagged
  `manual`). PR0.
- `//tools:privacy_scan`, `//tools:check_test_wiring` (`py_binary`): the repo checks as
  runnable tools. PR0.
- `//tools/migrate:port` (`py_binary`): ports an in-flight snapshot through the format and rename
  commits (§7.4).

`kicad_py_test` is a macro that expands to an `sh_test` running `tools/kicad_test_runner.sh`:

- locates KiCad Python through the toolchain or `YAPNR_KICAD_PYTHON`;
- sets `PYTHONPATH` to the runfiles copy of `//yapnr/kicad:kicad_side` plus the test;
- sets `HOME=$TEST_TMPDIR` so KiCad writes its settings in the sandbox, not the user profile;
- runs `-m unittest <module>`.

Without KiCad it prints a skip notice and exits 0, **unless** `YAPNR_REQUIRE_KICAD=1`, in which
case it fails. That setting is used in CI, so there are no silent skips.

### 4.3 Downstream headless ruleset (API designed now, implemented in PR8)

"Headless" means: no viewer, no agent, no GUI, no hand-set environment, time-bounded actions. The
rules depend only on `//yapnr:headless`, the pip hub and the KiCad toolchain.

```starlark
load(
    "@yapnr//bazel:defs.bzl",
    "yapnr_board",
    "yapnr_fab",
    "yapnr_kicad_project",
    "yapnr_pnr",
    "yapnr_pnr_test",
    "yapnr_project_run",
)

# -> YapnrBoardInfo; freezes an input set as build outputs
yapnr_board(
    name = "mini_board",
    pcb = ":splanc_mini.kicad_pcb",  # any .kicad_pcb (Splanc exposes the atopile layout's pcb)
    project_file = None,  # optional .kicad_pro (net classes)
    footprints = glob(["elec/src/parts/**/*.kicad_mod"]),
    frontend = "atopile",  # "kicad" | "atopile"
    annotation_sources = ["elec/src/splanc_mini.ato"],  # atopile frontend
    annotations = None,  # kicad frontend: annotations.yaml
    constraints = "mini-constraints.yaml",
    electrical_fab = "mini-routing-electrical-fab.json",
    plane_access_fab = "mini-plane-access-fab.json",
    fab_profile = "jlc-pofv",
)

# -> YapnrResultInfo + OutputGroupInfo(kicad_project, reports)
yapnr_pnr(
    name = "mini_pnr",
    board = ":mini_board",
    strategy = "full",  # "full" | "halving" | "hier"
    profile = "pnr/profiles/production.toml",  # or flags = {...}; validated by the registry
    flags = {},
    seed = 1,
    time_budget_s = 5400,  # hard cap; enforced by the supervisor
    placement_rounds = 6,
    workers = 4,
    library = None,  # optional block library (hier strategy): a yapnr_pnr output or files
    tags = ["manual", "requires-kicad"],
)
# outputs: mini_pnr.kicad_pcb, .evaluation.json, .drc.json, .provenance.json,
# mini_pnr.report/

yapnr_pnr_test(
    name = "mini_pnr_gate",
    result = ":mini_pnr",
    max_violations = 0,
    max_unconnected = 0,
    require_qualified_pairs = True,
)

# gerbers, drill, BOM, CPL zip
yapnr_fab(name = "mini_fab", result = ":mini_pnr", vendor = "jlcpcb")

# §3.6 as a build output
yapnr_kicad_project(name = "mini_kicad", result = ":mini_pnr", with_3d = False)

# bazel run //...:h7 -> non-hermetic long experiment writing into the project store
yapnr_project_run(
    name = "h7",
    project = "pnr/yapnr.toml",
    experiment = "pnr/experiments/h7.toml",
)
```

Plus a convenience macro `yapnr_board_pnr(name, ...)` = board + pnr + test + fab, which matches
today's `atopile_pnr` (so Splanc's `splanc_mini.fab` can be expressed in one call).

- **Providers:**
  - `YapnrBoardInfo(input_set_dir, pcb, graph, rules, annotations, footprints)`;
  - `YapnrResultInfo(pcb, evaluation, drc, provenance, report_dir)`.
- **Toolchain:** `@yapnr//bazel/toolchains:kicad_toolchain_type`; rule
  `kicad_toolchain(cli, python, version, env)`.
  - The module extension `kicad.autodetect()` generates a local toolchain using the same order as
    `yapnr.kicad.toolchain`.
  - A consumer can instead register its own. Splanc could bind its Nix KiCad, **but** Splanc's Nix
    pins give KiCad 9.0.1, which cannot read KiCad 10 boards. See the Splanc import-back risks.
- **Actions** are tagged `requires-kicad` and `no-remote`, keep `local` as today's rules do, and
  pass no repository path. Worker staging happens inside the action's output tree.
- **Compatibility constraints:**
  - Bazel 7.7.1;
  - a unique pip hub name;
  - Rust code compiles with rustc 1.85 (`search.rs` does);
  - no root-only extension tags outside `dev_dependency`;
  - the Python 3.11 toolchain is requested (the root's default wins).
- **Consumption:** `bazel_dep(name = "yapnr", version = "0.0.0")` plus a `git_override` with
  `module_name = "yapnr"`, `remote = "https://github.com/Studio-Fug/yapnr.git"` and a pinned
  `commit`. For local co-development: `--override_module=yapnr=<local clone>`.

## 5. CI and presubmit

### 5.1 Presubmit (prek, same pins as Splanc)

`.pre-commit-config.yaml` keeps Splanc's hooks and versions:

- black 25.1.0 `--line-length=100`;
- isort 6.0.1 `--profile=black --line-length=100`;
- flake8 7.0.0, configured in `.flake8` (`max-line-length = 120`, `extend-ignore = E203`);
- shellcheck-py v0.9.0.6 `--severity=warning`;
- buildifier and buildifier-lint 8.2.0;
- mirrors-prettier v3.1.0 (markdown/json/yaml);
- markdownlint-cli v0.38.0 `--fix` (100 columns; tables and code blocks exempt);
- pre-commit-hooks v4.5.0: trailing-whitespace, end-of-file-fixer, check-yaml,
  check-added-large-files `--maxkb=600`, check-merge-conflict, check-case-conflict, check-json,
  debug-statements, name-tests-test `--pytest-test-first`.

Changes from Splanc:

- Dropped: `nixpkgs-fmt` (no `.nix` files), the container-overlay local hook, and Splanc-specific
  excludes.
- **Global exclude reserved** for `^(hardware/|docs/hardware/|third_party/)`: the verbatim Splanc
  import (until PR3a/PR3b) and vendored upstream files. No other paths go there.
- `check-added-large-files` excludes `third_party/` explicitly (vendored files are size-reviewed
  when vendored).
- Added local hook `yapnr-privacy-scan` (`tools/privacy_scan.py`, stdlib only) with generic
  patterns:

  - absolute home paths (macOS `/Users/<name>` in any case, Linux `/home/<name>`, Windows with
    either slash, WSL), volume paths (macOS volumes, Linux removable media), macOS per-user
    temporary directories, and the dash-encoded form agent tooling uses for project directories
    (`-Users-<name>-...`), with placeholders such as `<name>` allowed only as a whole component;
  - `*.ts.net` host names and hyphenated or URL `*.local` machine names;
  - 100.64.0.0/10 (CGNAT, tailnet) and RFC 1918 private addresses;
  - e-mail addresses other than `users.noreply.github.com`, `noreply`/`no-reply` mailboxes, the
    git user of code hosts and reserved example domains (the allowlisted commit addresses are
    findings in files too);
  - API keys and tokens (GitHub, Anthropic, OpenAI, AWS, Slack, Google, GitLab, Tailscale) and
    private-key blocks.

  The scanner allowlists itself, its test, this plan and the commit address allowlist
  (`tools/privacy/allowed_identities.txt`), supports a `privacy-scan: allow` line marker, and
  redacts what it prints (CI logs are public). The same scanner runs as a Bazel test over the
  whole working tree (`//tests/unit/repo:test_privacy_scan`), including the globally excluded
  paths, and in CI over the messages and patches of every new commit, where the file allowlist
  does not apply (so the allowlisted files must scan clean as patches too) and the allowlisted
  commit addresses pass (every commit header carries them). Its `--identities` mode is the
  commit identity gate: one address per line, and only the addresses in
  `tools/privacy/allowed_identities.txt` (the owner's public commit address), GitHub noreply
  addresses and `noreply@github.com` pass.

- `name-tests-test` (`--pytest-test-first`) applies to `tests/`; helpers live in `tools/`. When
  the regression helpers (`tests/regression/{designs,run,native}.py`) and the `tests/fixtures/*.py`
  helpers land (PR3b), either add a narrow per-hook exclude for exactly those files (Splanc has
  one) or move the helpers out of `tests/`.
- **Lint baseline for imported code.** Until PR3a, `hardware/` and `docs/hardware/` (the verbatim
  import) are skipped by all hooks through the reserved global exclude (and by `.flake8`
  `extend-exclude` and `.markdownlintignore`). PR3a formats `hardware/` and drops it from those
  excludes; PR3b moves the code out; PR7 splits `docs/hardware/` into the docs pages. After PR3a,
  flake8 uses `per-file-ignores` in `.flake8` for the remaining E501/E731/E402 findings, and that
  list only shrinks. The measured debt: about 1,378 lines over 120 characters, about 2,000 lines
  with `;` joins, and 149 real F-codes in the committed code, including one genuine `F821` in
  `via_coalesce.py` (`items.clear()` in `main`).

`setup-precommit.sh` follows Splanc's (prek 0.4.12, then `prek install` and
`prek run --all-files`), except that prek is installed in isolation: a `prek` on `PATH` is used
only at the pinned version (with a warning otherwise), and else the pinned version goes into a
private virtualenv under `.venv/prek` (with `uv` when available), never into the system Python.

### 5.2 GitHub Actions

`ci.yaml` triggers on `push: main`, `pull_request` (`opened, synchronize, reopened, closed`) and
`workflow_dispatch`. It has top-level `permissions: contents: read`; only the Pages jobs widen it,
for themselves. `concurrency` groups runs per PR (or ref) with `cancel-in-progress` for pull
requests. A started run on `main` is never cancelled, but GitHub keeps one pending run per group,
so a queued `main` run is replaced by a newer one. Every job has a `timeout-minutes`.

Jobs in `ci.yaml`:

- `lint` (ubuntu-latest; required): Splanc's job (setup-python 3.11, `pip install prek==0.4.12`,
  prek cache keyed on the config hash, `prek run --all-files --show-diff-on-failure`), plus two
  checks over the new commits (the PR's range, or the pushed range on `main`):
  - the **commit identity check**: `git log --format='%ae%n%ce' <range>` piped through
    `tools/privacy_scan.py --identities`, so only the owner's public commit address (listed in
    `tools/privacy/allowed_identities.txt`), GitHub noreply addresses and `noreply@github.com`
    (GitHub's committer for web merges) pass;
  - the **history scan**: `git log -p --diff-merges=separate --format='%ae %ce%n%B' <range>`
    piped through `tools/privacy_scan.py --stdin`, which covers commit messages and intermediate
    commits (merge-commit PRs land them all on `main`), and the diff of every merge commit
    against each parent (plain `git log -p` shows none; `-m` would depend on `log.diffMerges`).
- `test` (`ubuntu-24.04-arm`; required): `bazel test //... --config=ci`, then
  `bazel test //:requirements.test` (the one job that checks the lock). The aarch64 runner matches
  the CPU-only torch lock. Moves to ubuntu-latest once the x86_64 lock exists, if faster.
- `kicad-integration` (ubuntu-latest, `container: kicad/kicad:10.0.6@sha256:<digest>`,
  `--user root`; required from PR6a): install bazelisk and `build-essential` (linker for
  rules_rust); `YAPNR_KICAD_CLI=/usr/bin/kicad-cli`, `YAPNR_KICAD_PYTHON=/usr/bin/python3`,
  `YAPNR_REQUIRE_KICAD=1`; `bazel test --config=ci --config=kicad //...` including the
  regression smoke and the example end to end.
- `docs` (ubuntu-latest; required): `bazel run //docs:build`, upload the site as an artifact.
- `deploy-preview` / `cleanup-preview` (ubuntu-latest; not required):
  `rossjrw/pr-preview-action@v1` into `gh-pages` under `pr-preview/pr-N/`; the preview only after
  `lint`, `test` and `docs` pass (`gh-pages` keeps removed previews in its history);
  `if: vars.YAPNR_PAGES_ENABLED == 'true'` and a same-repository head; job-level
  `contents: write` and `pull-requests: write`; a per-PR concurrency group.
- `deploy-pages` (ubuntu-latest; not required): on push to `main` and on a manual run on `main`
  (the first deploy, which creates `gh-pages`), after `lint`, `test` and `docs`:
  `JamesIves/github-pages-deploy-action@v4` with `clean-exclude: pr-preview/` and `force: false`;
  same gate; job-level `contents: write`; its own concurrency group. Publishing by pushing to
  `gh-pages` needs neither `pages:` nor `id-token:` permissions.
- The Pages jobs use separate concurrency groups because GitHub cancels an older pending job in a
  group even with `cancel-in-progress: false`; pushes from different groups race, and both
  actions rebase a rejected push instead of forcing it.
- `downstream` (`ubuntu-24.04-arm`; required from PR8): `bazel test //...` in `tests/downstream`
  with `--override_module=yapnr=$GITHUB_WORKSPACE`.

`macos.yaml` (not required):

- `test-macos` (macos-latest; PRs and `main`): `bazel test //... --config=ci` (darwin wheels,
  later the `.dylib` build). PR0.
- `macos-kicad` (macos-latest; nightly `schedule` and `workflow_dispatch`; PR6b):
  `brew install --cask kicad` (10.0.6; cache the Homebrew download dir), the headless copy,
  `bazel test --config=kicad //...`, the full regression ladder, results uploaded as an artifact.

**Why the official Docker image for the required KiCad lane:**

- It is the same KiCad version as the development Mac (10.0.6, published 2026-09-22), pinned by
  digest.
- It is free and pulls quickly (about 0.8 GB compressed).
- It ships `kicad-cli` and a `pcbnew`-enabled Python 3.13, so it exercises 3.9-to-3.13
  compatibility of the KiCad-side code.

Alternatives considered:

- The Ubuntu PPA: slow install with large dependencies.
- nixpkgs `kicad-small`: needs Nix and root-only module tags, has a darwin gap, and Splanc's pins
  give 9.0.1.
- The Homebrew cask: 1.4 GB and macOS minutes; kept as the nightly parity lane with the bundled
  Python 3.9.
- A self-hosted runner on the development Mac: rejected because yapnr is a public repository.

**Caching:**

- `bazel-contrib/setup-bazel@0.15.0` without its own caches (their keys carry neither the CPU
  architecture nor the lock files, and its Bazelisk cache path is not where `.bazeliskrc` points).
  Instead, one `actions/cache` entry for Bazelisk's downloads (`BAZELISK_HOME` set in CI) and the
  repository cache, keyed on OS, architecture, `.bazelversion`, `MODULE.bazel`,
  `MODULE.bazel.lock` and `requirements.lock`, plus a per-job disk cache keyed on the locks;
- the prek cache;
- the Homebrew download cache (nightly).

Nix caching and runner-space actions from Splanc are not needed.

**Repository settings** (applied with `gh` after PR0 is pushed):

- Squash merge is the default. Merge commits are allowed only for the history-import PRs (PR1,
  PR2) and for PR3b (its pure-move commit must survive). No linear-history rule.
- A ruleset on `main` requires a PR and the checks `lint`, `test`, `docs` (and `kicad-integration`
  from PR6a), with 0 approvals (single-seat organization).
- `.github/CODEOWNERS`: `* @fughilli`.
- A PR template checklist:
  - tests run and which tier;
  - new behaviour behind a default-off flag;
  - measured result;
  - privacy scan and commit identity;
  - docs and WORKLOG updated.
- Pages source: the `gh-pages` branch root. Publishing is approved; until `YAPNR_PAGES_ENABLED`
  is `true` the deploy jobs are skipped. The first deploy, after the first green build of `main`:
  set the variable, run CI manually on `main` (this creates `gh-pages`), then point Settings >
  Pages at `gh-pages`, `/ (root)`.

## 6. Documentation

Sphinx with MyST, adapted from Splanc:

- `docs/build_docs.py` stages the tree from `$BUILD_WORKSPACE_DIRECTORY`, mirroring the repository
  layout so that links between Markdown files resolve on GitHub and on the site alike;
  `docs/index.md` is the root document and the site root redirects to it. `_make_jekyll_safe()` is
  copied verbatim (needed for `pr-preview/` subdirectories) and writes `.nojekyll`.
- `docs/_sphinx/conf.py`:
  - extensions: `myst_parser`, `sphinx_copybutton`, `sphinx_design`, `sphinxcontrib.mermaid`;
    plain `mermaid` code fences are treated as the directive, so diagrams render on GitHub too;
  - theme `furo` with the brand logos (`light_logo`/`dark_logo`), the SVG favicon and the palette
    as accent colours; `source_repository` = the yapnr GitHub URL;
  - `copyright` names `AGPL-3.0-or-later`;
  - `mermaid_version = "11.4.1"`.
- Warnings start non-fatal as in Splanc and become `-W --keep-going` once PR7 lands.
- PR7 may add `sphinx-autoapi` for a static API reference that parses source without importing
  torch or `pcbnew` (new dependency; recorded in `docs/decisions.md`).

PR0 ships `index.md`, `architecture.md` (placeholder), `decisions.md`, `about-the-name.md` and this
plan. The target structure:

```text
docs/
├── index.md              what yapnr is; pipeline diagram (mermaid); where to start
├── getting-started/      install.md, quickstart.md (examples/led555),
│                         toolchains.md (KiCad discovery, headless macOS copy, doctor)
├── architecture/         overview.md (place -> route -> native electrical -> DRC ->
│                         evaluation -> feedback), interpreters.md (hermetic controller vs
│                         KiCad-Python workers, staging, timeouts), data-model.md (BoardGraph,
│                         rules.json, evaluation objective), telemetry.md (yapnr-live-event-v1)
├── project/              concepts.md, manifest.md (generated reference), store-layout.md,
│                         lifecycle.md, experiments.md (kinds, profiles, flags), bundles.md,
│                         kicad-portability.md
├── cli/                  reference.md (generated from argparse; freshness-tested)
├── engine/               placement.md, global-routing.md, native-phases.md (00-placement ...
│                         09-final-audit), diff-pairs.md, planes-and-power.md, vias.md,
│                         shove.md, hierarchy.md, mc-halving.md, feedback.md,
│                         contracts-and-annotations.md, fab-profiles.md, drc.md,
│                         native-search-backend.md, flags.md (generated from the registry)
├── frontends/            atopile.md, kicad.md, annotations.md (canonical schema),
│                         writing-a-frontend.md
├── viewer/               overview.md, api.md, services.md, notes-and-agent.md, deploying.md,
│                         license-notice.md
├── bazel/                targets.md, ruleset.md (§4.3 as reference), downstream-splanc.md
├── development/          contributing.md, developers.md, testing.md (tiers, tags), ci.md
├── decisions.md          owner decisions and the pinned-versions table (as in Splanc)
├── adr/                  NNNN-*.md architecture decision records (below)
├── history/              splanc-origins.md, import-manifest.md (§7)
├── about-the-name.md     the name and the mark
└── migration-plan.md     this document
```

Seed ADRs, written from the Splanc handoff log in scrubbed form (no board-private data, no machine
details):

1. Candidate selection is mechanical (Monte Carlo and successive halving). No hand-picked seeds.
2. Hierarchical layout: blocks become macros, a library per template, top-level assembly.
3. Power-first placement: tiers, trunks and hot-loop terms are derived mechanically.
4. Nothing in the loop may be GUI-bound. `kicad-cli` runs headless. Every worker is time-bounded
   (`proc.py`).
5. Native KiCad DRC is the judge: final cold DRC and a content-addressed DRC cache.
6. Fab profiles: the default is JLC 4-layer with epoxy-filled, capped vias; filled via-in-pad.
7. Electrical contracts are source annotations and are never relaxed without the designer.
8. Differential pairs: `max_uncoupled_mm` applies per continuous uncoupled run; stub rules.
9. Routing-to-placement feedback with code-key provenance.
10. Shove support.
11. KiCad object lifetime: `Delete` rather than `Remove` for detached items (the teardown SIGSEGV
    fix).
12. Design-notes workflow (N-000x proposals accepted by the designer).
13. Planned: macro shrink and hull (N-0001), `@pnr-inductance` (N-0002), noise budgets (N-0003),
    SPICE-verified signal-integrity requirements.
14. A learned placement policy is deferred; keep the data hooks.

`docs/hardware/pnr-system.md` and `pnr-inputs.md` come over with history in PR1 and are split into
the pages above in PR7.

## 7. History import strategy

### 7.1 What is imported with history

`git filter-repo` runs on a **scratch clone**:
`git clone --no-local --single-branch --branch splanc-mini $SPLANC $SCRATCH/splanc-filter`. It
includes the local commits not yet pushed to Splanc. The Splanc working tree is never touched.
Paths kept:

```text
hardware/pnr/                                   (40 commits since 2026-08-21)
hardware/tools/keyhole_region.py
hardware/tools/keyhole_repair.py  keyhole_loop.py  keyhole_shift_via.py  keyhole_deform.py
hardware/tools/audit_pair_contacts.py (if tracked)  scan_via_proximity.py
hardware/tools/export_mini_review.py  mini_review_annotations.py  rasterize_mini_review.py
hardware/tools/verify_mini_review.py  review_mini_contacts.py  freeze_mini_inputs.py
hardware/tools/consolidate_ground.py  optimize_plane_access.py  plane_access_trial.py
hardware/tools/export_plane_access_clusters.py  export_pnr_congestion.py
hardware/tools/render_pnr_congestion.py
hardware/tools/export_elastic_experiment.py  render_elastic_experiment.py  watch_pnr_pdf.py
hardware/tools/pnr_live/  hardware/tools/pnr_viewer/
docs/hardware/pnr-system.md  docs/hardware/pnr-inputs.md
hardware/experiments/tscircuit-mini/geometry105/rust/search.rs
hardware/experiments/tscircuit-mini/geometry105/build-rust.sh
hardware/experiments/tscircuit-mini/geometry105/test_search_equivalence.py
```

Not imported:

- Splanc board tools (`place_splanc_mini.py`, `check_splanc_power.py`, `check_mini_eol.py`, EOL
  generators, atopile helper scripts, `package_pcb_models.py`);
- `hardware/tools/BUILD.bazel` (shared with the atopile tools; rewritten);
- `hardware/experiments/**/runtime/**` (997 MB of stale engine copies);
- `pnr.bzl`'s consumers in `hardware/splanc_dev`.

### 7.2 Rewrites during filtering

- `--mailmap` (a file kept in `$SCRATCH`, never committed) maps every identity to the owner's
  public commit address, the one listed in `tools/privacy/allowed_identities.txt` (owner decision,
  Q2 in §9): the machine-local (tailnet-derived) addresses, the other personal addresses and the
  noreply addresses alike, each with the matching name (the owner's name for the owner's
  identities, `Claude Agent` for agent identities). Co-Authored-By trailers are preserved.
- `--replace-text` and `--replace-message` (a patterns file in `$SCRATCH`, never committed)
  replace:

  - tailnet host names with `tailnet-host.example`;
  - tailnet (CGNAT) addresses with the documentation address `192.0.2.1`;
  - absolute volume and home-directory prefixes with `<repo>` and `~`.

  Blobs already public in Splanc are scrubbed too, so yapnr never re-publishes them.

- A `--commit-callback` appends `Imported-From: fughilli/splanc@<original sha>` for provenance.
- `--prune-empty always`.

**Gate:** before the filtered history is ever pushed, two checks must pass on it:

- `git log -p --all | tools/privacy_scan.py --stdin` is clean;
- `git log --all --format='%ae%n%ce' | tools/privacy_scan.py --identities` passes (every
  imported commit carries the owner's public commit address).

The scan report (counts only) goes into `docs/history/import-manifest.md` together with the paths
list, source commit, filter-repo version and the commit map size.

The filtered branch is fetched into `$YAPNR` and merged with
`git merge --allow-unrelated-histories` onto PR0's `main`. **PR1 must be merged with a merge
commit** (squash would destroy the history). A follow-up commit in the same PR adapts
`BUILD.bazel` loads to `@yapnr_pypi` and adds a minimal `hardware/tools/BUILD.bazel`. Files stay
at their Splanc paths until PR3b, so blame and `git log --follow` survive the later `git mv`. The
test-wiring check keeps its `tests/` scope; `hardware/` is wired when PR3 moves the engine's tests
under `tests/`, and the manifest counts the unwired files of the import until then (decided in PR1,
see `docs/decisions.md`; this replaces a shrink-only baseline for `hardware/`).

### 7.3 Landing the uncommitted engine state (PR2)

Snapshots are read-only sources. Each one is copied into `$SCRATCH`, checked against an engine
digest (sha256 over the sorted `path<TAB>sha256` lines of `hardware/pnr` and the engine tools,
without `__pycache__`; the snapshots' `hashes.json` files are F217's copied along, so they verify
only the source freezes), scrubbed (§2.5, Appendix C), and then committed. Each snapshot commit's
engine tree equals its snapshot; side lines join through merge commits. What was imported, the
digests and the checks are in the [import manifest](history/import-manifest.md#pr2-the-uncommitted-engine-state).

The lineage as verified by content on 2026-09-30 (it corrects this section's first version):

```text
main ─ F217 ─┬─ src8b ─ src10.frozen ─ src10 ─ src11 ─ src12 ─┬─ src12h ─┬─ src12i ─ src14 ─┐
             │                                                │          └─ src12n ────────┤
             │                                                └─ src12b ─ src13.r8 ────────┤
             │                                                          src15.r1 (merge) ──┘
             │                                                          src15
             └─ Electrical221 ───────────────────────────────────────── merge, Bazel, docs
```

1. **F217**, the fresh217 freeze of Splanc's working tree minus Electrical221: the Codex agent's
   uncommitted work on top of `b009c945` (`pair_joint`, `portal_retry`, `power_bank_*`,
   `transaction_cleanup`, cost capture, relative rows, ...), and
   `hardware/tools/audit_pair_contacts.py`.
2. **Electrical221** (the fresh222 freeze, byte-identical to Splanc's working tree): the full
   electrical pool, partial-cycle cleanup in `track_graph`/`via_coalesce`, a `pnr.bzl` branch and
   3 tests. It forks from F217 and is in no hierarchical snapshot.
3. **src8b**: everything from `src2` to `src8` (their snapshots are gone): `fab_profile`
   (default `jlc-pofv`), `fanout_reserve`, `via_in_pad`, `power_topology`, `proc` (worker
   timeouts), `place/power_first`, and the packages `hier/` and `mc/`.
4. **src10.frozen**: the `shove/` package (`PNR_SHOVE`). It **precedes** `src10`.
5. **src10**: the owner's 0.15 mm stretch floor in `shove/world.py`.
6. **src11**: the routing-to-placement feedback loop, `feedback/` (`PNR_FEEDBACK`).
7. **src12**: macro-first `place/legalize.py`; pair budget overrides in `paired_bootstrap.py`.
8. **src12h**: `PNR_KICAD_CLI` (headless KiCad) in 5 files.
9. **src12i**: `fanout_reserve.release` uses `board.Delete(zone)` (teardown SIGSEGV fix).
10. **src14**: SI v1 (`PNR_SI`, `pnr/si/`, `si_models/`) and the terminal-width contract.
11. **src12b** and **src13.r8** (side line from src12): the USB pair engine (A-D), all flags
    default off.
12. **src12n** (side line from src12h): N-0001 macro shrink, per-side hull, used-area ranking.
13. **src15.r1**: the octopus merge of src14, src13.r8 (base src12) and src12n (base src12h), plus
    headless KiCad defaults and bus-type classes (`PNR_BUS_CLASSES`); **src15**: its review
    fixes, the engine of the H7 run.
14. **Merge of Electrical221** into src15. Two conflicts: the summary dict at the end of
    `via_coalesce.main` (keep both key sets) and two hunks of `pnr.bzl` (Electrical221's pool
    branch with src15's headless `$_KI_CLI`; the SI tool added to the action tools).
15. **Bazel adaptation**, then the docs.

Engine-identical directories (`src9.frozen`, `src10.base`, `src10b`, `src11.base`,
`src11.frozen`, `src12.base`, `src12n.base`, `src14.base`, `src15.base`, `src13.base`, `src13`)
produce no commit.

- Author: `Claude Agent` with the owner's public commit address (owner decision). The committer
  date is the import time; the author date is the capture time or the snapshot's newest source
  mtime. Trailers: the session's; the F217 and Electrical221 bodies name the Codex agent.
- Each commit body names the snapshot and its digest, the flags it introduced (and their
  defaults) and the measured results from the log (for example deepS 33/0, H6 12/0).
- The Splanc design (`hardware/splanc_dev` outside the engine's own test data, `contracts/`) is
  never included.
- The four files with machine paths (`testdata/power_topology/{converter,pd}.json`,
  `tests/test_shove{,_native}.py`) are scrubbed when each snapshot is staged, so no commit carries
  the paths. (The first version of this plan scrubbed the fixtures in a separate commit, which
  would have published the paths in history.)
- Only the head builds in yapnr: the snapshot commits carry Splanc's `BUILD.bazel`, as PR1's
  imported commits do.
- Merged with a merge commit.

**PR2e: `board.Remove` audit** (separate). Classify the ~25 remaining `board.Remove` call sites:
use `Delete` unless the item is re-added. `via_coalesce`'s "retry after signal 11" is a likely
instance of the same class. Done after PR2 in the engine hygiene change, together with the worker
time bounds and the Electrical221 gate ([decisions](decisions.md)); the retries stay as a second
line of defence.

Engine work that starts after PR2 is made on yapnr branches; work still in Splanc snapshots after
PR3a/PR3b have landed goes through `//tools/migrate:port` (§7.4).

### 7.4 Porting in-flight work through mechanical commits

Formatting and renaming are deterministic functions `f`. For an in-flight snapshot `B` built on
base `P`, the change to apply after the mechanical commit is `f(B) - f(P)`, where `f(P)` is exactly
what `main` holds. `bazel run //tools/migrate:port -- --base <P dir> --tip <B dir>`:

1. copies both trees;
2. applies the same module map, import codemod, black and isort to each;
3. emits a patch against `main`.

The tool runs in CI on a fixture pair so it cannot rot.

### 7.5 Keeping Splanc's running experiments unaffected

- Read-only access to `$SPLANC` and `$HIER`. All filtering, cloning and diffing happens in
  `$SCRATCH` or `$YAPNR`. Snapshot trees are cloned before reading.
- Never stop, signal or `renice` experiment processes. The deployed viewers keep running from
  `$HIER/viewer`. Any yapnr viewer instance uses its own ports and reads copies or a store created
  by `yapnr import legacy-hier` (hard links, no writes to the source).
- Heavy work runs niced with `--config=lowmem`, one Bazel server at a time, with the output base
  on the internal disk, and not while a deep run is in its native phase. Check the load average
  and free disk first (the disk guard pauses experiments below 8 GB free; yapnr work needs more
  than 15 GB free on the experiment volume).
- Check for another agent's activity (new handoff entries, processes this session did not start)
  before long operations, and report it rather than competing.
- **Transition of engine work:**
  - Once PR2 lands, new engine changes are made on yapnr branches.
  - Until PR3b, yapnr's `hardware/pnr` layout is identical to a snapshot's, so the existing
    `runx.sh` harness can run a yapnr checkout directly (`PYTHONPATH=<yapnr>/hardware/pnr`).
  - Between PR3b and PR3f, experiments either stay on frozen snapshots or run
    `python -m yapnr.mc.halving` from a checkout with explicit arguments (PR3c removes the hidden
    defaults).
  - After PR3f, Splanc experiments move to `yapnr run` with a Splanc project manifest and an
    external store; older history is registered with `yapnr import legacy-hier`.

## 8. PR sequence

**Merge order:** PR0, PR1, PR2, **PR6a**, PR3a-f, PR5, PR4a-d, PR6b, PR2e (ported via §7.4 if
late), PR7, PR8, then the Splanc import-back. (PR2c and PR2d, the USB pair and N-0001 lines, were
finished before PR2 and are part of it.)

PR6 is split, and its first half moves ahead of PR3. PR3a/PR3b rewrite every file mechanically,
and today no CI runs the native (KiCad-side) code paths, so the KiCad lane must exist first. The
labels otherwise follow the requested numbering. Each PR updates `WORKLOG.md` and the docs pages it
touches. "Same pass set" means the recorded list of passing, skipped and known-failing tests is
unchanged.

### PR0: bootstrap (about 55 files, about 5.1k lines, excluding the lockfiles)

- **Contents:**
  - `README.md` (updated), `AGENTS.md`, `DEVELOPERS.md`, `CONTRIBUTING.md` (owner-only policy),
    `WORKLOG.md`, `THIRD_PARTY.md` (three.js and elkjs);
  - the Bazel skeleton (§4.1): `MODULE.bazel` with `rules_python`, the hermetic Python 3.11
    toolchain and the `yapnr_pypi` hub, `MODULE.bazel.lock`, `.bazelversion`, `.bazeliskrc`,
    `.bazelrc`, `.bazelignore`; no license metadata in Bazel;
  - `requirements.in` and `requirements.lock` (generated on the development Mac);
  - presubmit configs (§5.1): `.pre-commit-config.yaml` with the reserved global exclude and the
    privacy hook, `.flake8`, markdownlint and prettier configs, `setup-precommit.sh`;
  - `ci.yaml` (lint with the identity check, test on `ubuntu-24.04-arm` with the lock check,
    docs, gated Pages deploy and previews) and `macos.yaml` (`test-macos`, not required);
  - the docs skeleton: `docs/_sphinx/conf.py` (furo, MyST, mermaid, brand logos, favicon and
    palette), `index.md`, `architecture.md`, `decisions.md`, `about-the-name.md`, this plan;
    `//docs:build` and `//docs:serve`;
  - `yapnr/__init__.py` (`__version__`), `yapnr/__main__.py`, `yapnr/cli.py` (`--version`, a
    `doctor` stub that never runs KiCad), `//:yapnr` and `//:doctor`;
  - tests: the CLI, the torch/numpy interop smoke, and the repo checks (test wiring, privacy scan
    of the whole tree), with `tools/privacy_scan.py`, `tools/check_test_wiring.py` and the
    `yapnr_py_tests()` macro;
  - `.github/CODEOWNERS`, the PR template, `.git-blame-ignore-revs` (empty).
- **Acceptance:**
  - `bazel test //...` green locally (lowmem) and in CI;
  - `prek run --all-files` clean;
  - `bazel run //docs:build` produces `docs/site/html`;
  - deploy jobs are skipped until `YAPNR_PAGES_ENABLED` is set;
  - GitHub shows AGPL-3.0;
  - privacy scan clean, and every commit uses an accepted identity (the owner's public commit
    address; the earlier PR0 commits keep a noreply address, which also passes).
- **Risks:**
  - everything pushed is public (the privacy gates above);
  - the Bazel output base on a case-insensitive external volume (use the documented location);
  - lock resolution platform (aarch64 lock; see the `test` job).

### PR1: core engine import with history (merge commit)

- **Contents:**
  - the filtered history (§7.1-7.2) at Splanc paths, source code unchanged;
  - the privacy scan's refined e-mail rule (owner decision, `docs/decisions.md`): the imported
    history holds decorators, a matrix product and constraint endpoints that the old rule read as
    addresses;
  - one adaptation commit (Bazel loads to `@yapnr_pypi`, tools BUILD, stale `pnr.bzl` kept but
    unloaded, the four failing tests of appendix B tagged `manual`);
  - `docs/history/import-manifest.md`, and the PR1 decisions in `docs/decisions.md`.
- **Acceptance:**
  - `git log --follow` works on sample files, and the original commit count is preserved minus
    empty commits;
  - the history-wide privacy scan and identity check are clean;
  - `bazel test //hardware/pnr/...` gives the same pass set as Splanc HEAD (recorded in the PR);
  - no file over 600 KB.
- **Risks:**
  - an identity the mailmap misses (the history-wide identity check rejects it);
  - a missed scrub pattern (mitigated by the history-wide scan);
  - the reviewer cannot read 28k lines, so review focuses on the manifest, scrub report and the
    adaptation commit.

### PR2: newer engine state (merge commit)

- **Contents:** the §7.3 series: F217, Electrical221, the hierarchical snapshots up to `src15`
  (with the USB pair and N-0001 side lines, formerly PR2c and PR2d), the Electrical221 merge, the
  Bazel adaptation, and the docs (import manifest PR2 section, this plan, decisions, worklog).
- **Acceptance:**
  - each snapshot commit's engine tree equals its snapshot, except the four scrubbed files
    (checked per commit and recorded in the manifest);
  - `bazel test //... --config=ci` green at the head, with the pass set, the skips and the
    `manual` tests recorded in the manifest; tests that newly fail are tagged `manual` with a
    comment (no engine edits in PR2) and listed in appendix B;
  - privacy scan clean, including merge diffs (`git log -p -m`), and gitleaks clean;
  - the unwired test files are counted; `hier/`, `mc/` and `feedback/` get their Bazel targets in
    PR3 with the glob macro (moved from this PR: most of their tests need KiCad or Splanc
    inputs).
- **Risks:**
  - `src2` to `src7` are unrecoverable, so the src8b commit is large (mitigated by a detailed body
    and per-feature notes);
  - the commits before the adaptation do not build in yapnr (as in PR1);
  - Electrical221's cleanup is on by default and the H7 run does not use it (decisions);
  - the merge-commit requirement.

### PR6a: KiCad test lane and toolchain discovery

- **Contents:**
  - path-independent KiCad toolchain discovery (initially `hardware/pnr/pnr/kicad_toolchain.py`,
    moved to `yapnr.kicad.toolchain` in PR3b) used by every `kicad-cli`/KiCad-Python call site,
    including the two bare `kicad-cli` ones;
  - the `kicad_py_test` macro and runner (`rules_shell` joins `MODULE.bazel`);
  - the `.bazelrc` KiCad config;
  - existing KiCad-needing tests re-tagged `kicad`;
  - `regression/run.py` made path-independent;
  - `//tests/regression:smoke`;
  - the `kicad-integration` container job;
  - `requirements_linux_x86_64.lock` (CPU torch index) through `requirements_by_platform`;
  - the regression ladder run under both numpy 1.26 (experiment venv) and the lock, with the
    results in the PR.
- **Acceptance:**
  - `bazel test --config=kicad //...` passes on the development Mac and in the container, with
    `YAPNR_REQUIRE_KICAD=1`;
  - the 16 test files bundled in `power_access_native_test` actually execute (no skips);
  - the smoke run takes under 10 minutes on CI.
- **Risks:**
  - KiCad-side code under Python 3.13 (container) versus 3.9 (Mac);
  - KiCad 10.0.6 container behaviour differences (fonts, library env vars);
  - CI minutes (bounded by the smoke design).

### PR3a: mechanical format and lint fixes

- **Contents:**
  - black and isort on the imported trees, as a format-only PR of its own (squash-merged; a
    follow-up adds its `main` commit to `.git-blame-ignore-revs`);
  - hand fixes for F-codes (F821 in `via_coalesce`, F401/F841/F811/F402);
  - the `.flake8` baseline for E501/E731/E402;
  - `hardware/` removed from the global exclude, `.flake8` and `.markdownlintignore`.
- **Acceptance:**
  - black's AST-equivalence check;
  - same pass set in both lanes;
  - `prek run --all-files` clean with no path excludes for `hardware/`.
- **Risks:** isort moving imports across `sys.path` bootstrap code in worker scripts. Such files
  get `# isort: skip_file` and are removed in PR3b anyway.

### PR3b: package rename and restructure

- **Contents:**
  - `tools/migrate/module_map.toml` plus a codemod;
  - commit 1: pure `git mv` to the §1 layout (100% similarity); PR3b is merged with a merge
    commit so this commit survives, and a follow-up lists both commits in
    `.git-blame-ignore-revs`;
  - commit 2: import and `-m` string rewrite (about 1,272 import statements, about 100 module
    strings), `feedback/signals.py` regexes, BUILD files regenerated per subpackage;
  - tests moved to `tests/unit|kicad`, the four mixed files split, the hyphenated test renamed;
  - `drc_warm`, the legacy keyhole adapters and `pnr_viewer` deleted (pointer in
    `docs/history/splanc-origins.md`);
  - the KiCad-side closure test;
  - `//tools/migrate:port` with its fixture test.
- **Acceptance:**
  - same pass set;
  - `git log --follow`/blame continuity on samples;
  - the port tool reproduces `src13` against `main` as a clean patch (dry run).
- **Risks:**
  - a huge diff (reviewed as tool plus map, not line by line);
  - collisions with in-flight work (§7.4).

### PR3c: de-Splanc paths and defaults

- **Contents:** every §2.1-2.3 and §2.5 item marked 3c, plus worker staging
  (`yapnr.kicad.staging`).
- **Acceptance:**
  - `rg -i 'splanc|splanc_dev|splanc_mini'` over `yapnr/` and `tests/` returns only the fixture
    README and history docs;
  - no absolute paths in code (privacy scan);
  - running from a directory other than the repository works (a test runs the smoke from
    `$TEST_TMPDIR`).
- **Risks:** the UUID namespace change alters generated identifiers, so goldens must be
  regenerated and reviewed.

### PR3d: project store and manifest

- **Contents:**
  - `yapnr/project` (manifest model and generated schema, store, objects, input sets, engine
    snapshots, run records);
  - CLI `init`, `doctor` (full), `kicad make-headless`, `import kicad`, `inputs`, `snapshot`,
    `ls`, `show`;
  - the flag registry and its repo check;
  - docs `project/*`.
- **Acceptance:**
  - unit tests with a fake engine kind;
  - schema and CLI-reference freshness tests;
  - `import kicad` of the led555 KiCad project produces a portable input set (no absolute URIs).
- **Risks:** store design churn. Mitigated by the `layout_version` field and a migration hook from
  the start.

### PR3e: frontends and canonical annotations

- **Contents:**
  - the `DesignFrontend` protocol;
  - the atopile frontend (annotation parser moved out of `core`, `atopile_address` provider,
    module hierarchy, build command);
  - the KiCad frontend (sheet-path addresses, `annotations.yaml`, footprint fields);
  - the `yapnr-annotations-v1` schema;
  - `import atopile`;
  - the `examples/atopile-blinky` fixture.
- **Acceptance:**
  - the Splanc Mini annotations (run manually with `YAPNR_EXTERNAL_PROJECT`) compile to the same
    `rules.json` policy as before;
  - a KiCad-native hierarchical fixture yields blocks and resolved annotations.
- **Risks:** semantic drift in address matching (`._p` trimming). Mitigated by a golden comparison
  on the Mini.

### PR3f: experiment runner and lifecycle (three sub-PRs)

- **3f-1 contents:**
  - `run`, `resume`, `status`, `stop|pause|continue`, `compare`;
  - the supervisor (pid identity, keep-awake, disk guard, log, telemetry dir);
  - experiment kinds;
  - profiles.
- **3f-2 contents:** `prune`, `gc`, `pack`, `unpack`, `import legacy-hier`.
- **3f-3 contents:** `export kicad`, `baseline add`.
- **Acceptance:**
  - a smoke experiment runs through the CLI in the KiCad lane;
  - pack then unpack round-trips byte for byte;
  - export DRC equals run DRC;
  - `import legacy-hier` on a copied sample of `$HIER` registers runs without writing to the
    source.
- **Risks:**
  - process supervision edge cases (orphans, sleep and wake);
  - the size of `live/` (78k event files), handled by hard links and streaming indexing.

### PR5: native search backend

- **Contents:**
  - `native/search/search.rs` (history already imported) with `rust_shared_library` and
    `rust_test`;
  - the runfiles lookup in the controller, passed to workers;
  - the `//native:search_backend` flag;
  - the Python equivalence test (`keyhole` with the Rust versus Python backend, against
    `keyhole_reference`);
  - `build-rust.sh` retired.
- **Acceptance:**
  - builds on linux-x86_64, linux-aarch64 and darwin-arm64 with rustc 1.85;
  - the equivalence test is green;
  - the regression smoke gives identical routes with both backends.
- **Risks:** the rules_rust toolchain interplay with Splanc (root-first, 1.85, fine); `ctypes`
  callbacks under the KiCad Python.

### PR4: viewer (four sub-PRs)

- **4a:**
  - `git mv` of the history-imported `pnr_live` to `yapnr/viewer`, then the viewer-dev core delta
    (server, event schema, settings, extract, static app);
  - project- and run-driven configuration; renames; About/Source (AGPL section 13);
  - a published `yapnr-live-event-v1` JSON Schema (accepts v1 events);
  - node tests through the dev-only node toolchain; `bazel run //:viewer`.
- **4b:**
  - services: component cost (resolve `capacitor_intent`), schematic (elkjs as a fetched,
    sha256-pinned, separately served file; never vendored), source browser through
    `frontend.source_index()`.
- **4c:**
  - the notes store and MCP, the optional agent and AI net labels (**off by default**; CLI via
    `shutil.which`, spend caps, web guard), schema and env renames, test literals replaced.
- **4d:** the 3D view (three.js r180 + GLTFLoader vendored, GLB through the headless `kicad-cli`),
  once the in-progress viewer-dev work lands.
- **Acceptance:**
  - viewer tests pass;
  - `bazel run //:viewer -- --project examples/led555` shows the smoke run;
  - no `splanc` strings outside history docs;
  - static fixtures regenerated from examples;
  - privacy scan clean.
- **Risks:**
  - four diverging viewer copies (import from viewer-dev only, after confirming it is a superset);
  - the EPL-2.0 dependency;
  - agent features in a public repository.

### PR6b: tiny example and end-to-end lane

- **Contents:**
  - the `examples/led555` project (manifest, rules, profiles, `ci-smoke` experiment);
  - an end-to-end test: `init`, `import kicad`, `run`, `export kicad` + DRC, `baseline add`,
    `pack`/`unpack`;
  - the `macos-kicad` nightly job.
- **Acceptance:** green on the container lane and a nightly macOS run.
- **Risks:** runtime budget; stock-library variables differ between the container and macOS
  (handled by the export copying footprints).

### PR7: documentation content

- **Contents:**
  - §6 pages, split from `pnr-system.md`/`pnr-inputs.md`;
  - scrubbed ADRs;
  - generated references (CLI, manifest, flags, telemetry);
  - `-W` enabled.
- **Acceptance:** the strict docs build is clean; every public CLI command and every flag is
  documented.
- **Risks:** leaking Splanc-private or machine details from the handoff log. Everything is written
  fresh, not copied.

### PR8: downstream ruleset

- **Contents:**
  - `bazel/` (providers, rules, `kicad` extension and toolchain) implementing §4.3;
  - analysis tests (skylib `unittest`);
  - `examples/led555/BUILD.bazel` using the rules;
  - the `tests/downstream` consumer workspace plus the `downstream` CI job;
  - `docs/bazel/*`.
- **Acceptance:**
  - `bazel build //examples/led555:pnr` and `bazel test //examples/led555:pnr_test --config=kicad`
    green in the container;
  - the downstream workspace resolves yapnr through `--override_module`;
  - buildifier-lint clean.
- **Risks:**
  - sandboxing of KiCad actions (keep `local`);
  - long actions in Bazel (time budget attribute, `manual` tag by default).

### Splanc import-back (separate Splanc PR, later)

- **Contents:**
  - `bazel_dep(name = "yapnr")` + `git_override(commit = <sha>)`;
  - a Splanc macro wrapping `yapnr_board_pnr` that maps the atopile layout's pcb;
  - `splanc_mini.fab`, `splanc_dev.fab` and `splanc_mini.mesh_experiment` re-expressed;
  - a Splanc project manifest (`hardware/splanc_dev/pnr/yapnr.toml`, atopile frontend, existing
    rules files) with its store on the external disk;
  - `yapnr import legacy-hier` of `$HIER`;
  - delete `hardware/pnr`, the moved tools and `docs/hardware/pnr-*` (pointer to the yapnr docs).
- **Must carry** the owner-approved contract edits that today exist only in hier snapshots:

  - the VBUS neck;
  - the gate-drive branches;
  - the SW2/VOUT/SW1 envelopes;
  - the USB ESD stub and the per-run uncoupled rule.

  They move into the production `splanc_mini.ato` with their datasheet comments.

- **Risks:**
  - Splanc's Nix KiCad is 9.0.1 and cannot load KiCad 10 boards: bump the pin (nixos-unstable has
    10.0.6) or use yapnr's autodetect toolchain;
  - `hub_name` and `rules_rust` interplay;
  - Splanc CI is not green today for unrelated reasons (lint debt, the `freerouting` Nix package
    on x86_64).
- **Contribution flow afterwards:** engine changes land in yapnr first; Splanc bumps the
  `git_override` commit.

### Migration done when

- Splanc builds its boards through `@yapnr` with no copy of the engine;
- all required yapnr checks are green on `main`, including a real KiCad lane;
- every runtime artifact the Splanc experiments produce lives in a project store that can be
  packed, restored and exported to KiCad;
- the docs site is published on GitHub Pages.

## 9. Open questions for the owner

Resolved on 2026-09-29 (see §0.2 and `docs/decisions.md`):

- **Q1, license variant and inbound terms:** `AGPL-3.0-or-later`. Inbound terms are not needed yet,
  because outside contributions are not accepted; revisit (CLA or DCO) before opening
  contributions.
- **Q3, public docs:** approved. The Pages deploy and previews are wired and gated on
  `YAPNR_PAGES_ENABLED`, which is set after the first green build of `main` (then a manual run on
  `main` creates `gh-pages`).
- **Q4, agent features:** they ship in yapnr as optional features, off by default.
- **Q5, elkjs:** acceptable as a separately fetched, sha256-pinned, unmodified asset; never
  vendored.
- **Q6, issue keys:** GitHub issues (`#N`).
- **Q2, identity mapping in the imported history:** decided. Every identity in the imported
  history (machine-local addresses, other personal addresses and noreply addresses) maps to the
  owner's public commit address, listed in `tools/privacy/allowed_identities.txt`, with the
  matching name (the owner's, or `Claude Agent` for agent identities); new commits use the same
  address. GitHub accepts only an address verified on the account as the author of a merge made
  on the website, which rules out a noreply address for the merges into `main`, and the owner
  chose to publish this address on commits. The mailmap stays in `$SCRATCH`, never committed.

Still open:

- **Q2b, full-history import** (left over from Q2): confirm that a full-history import is wanted
  rather than a single snapshot commit. PR1 implements the full-history import (§0.3, item 7);
  merging it with a merge commit answers this question.

## Appendix A: module map (old to new)

- `graph`, `constraints`, `electrical`, `plane_intent`, `power_topology`, `fab_profile`:
  `yapnr.core.*` (annotation parsing moves to `frontends/atopile`).
- `place.*`, `incremental_place`: `yapnr.place.*`.
- `route.*`, `route.detail.*` (including `keyhole`, `keyhole_reference`, `rust_search`, `coupled`,
  `regional`, `layered`, `joint`, `portal_joint`, `spatial_conflicts`): `yapnr.route.*`.
- `yapnr.native.*` (flat first; grouping later): `native_loop`, `native_electrical`,
  `full_iteration`, `electrical_pool`, `phase_capture`, `placement_copper`, `placement_trials`,
  `paired_bootstrap`, `pair_joint`, `pair_contact_audit`, `electrical_audit`, `electrical_repair`,
  `plane_access`, `plane_leaf`, `plane_leaf_repair`, `power_bank_stage`, `power_bank_reuse`,
  `power_detour`, `power_detour_repair`, `fanout_reserve`, `via_in_pad`, `via_coalesce`,
  `track_graph`, `pad_entry`, `pad_entry_neck`, `terminal_repair`, `reference_guard`,
  `pad_identity`, `regional_blockers`, `transaction_cleanup`, `obsolete_branch`,
  `connectivity_restore`, `portal_retry`, `staged_signal`, `speculative`, `route_epoch`,
  `merge_additive`, `escape_shove`, `geometric_tree`, `geometric_native`, `geometry_optimize`,
  `congestion_diagnostics`.
- `ingest`, `writeback`, `source_footprints`, `library_table`, `specctra`, `planes`,
  `fixed_copper`, `quality`, `native_drc`: `yapnr.kicad.*` (`native_drc` becomes
  `yapnr.kicad.drc`).
- `hardware/tools/keyhole_region.py`: `yapnr.kicad.workers.region`.
- `live`, `runtime_controls`, `profile`, `proc`, `phase_budget`: `yapnr.runtime.*`
  (`runtime_controls` becomes `controls`).
- `shove.*`, `hier.*`, `mc.*`, `feedback.*`, `feedback_boundary`: the same names under `yapnr.`;
  `feedback_boundary` becomes `yapnr.feedback.boundary`.
- `drc_warm.*`: not migrated (GUI-bound).
- `scan_via_proximity`, `audit_pair_contacts`, `export_pnr_congestion`, `render_pnr_congestion`,
  `export_elastic_experiment`, `render_elastic_experiment`, `watch_pnr_pdf`,
  `export_plane_access_clusters`: `yapnr.diagnostics.*` (drop "Splanc Mini" titles).
- `export_mini_review`, `mini_review_annotations`, `rasterize_mini_review`, `verify_mini_review`,
  `review_mini_contacts`: `yapnr.review.*` (renamed without "mini"; optional deps reportlab,
  pypdf, Pillow, pdftoppm, rsvg-convert).
- `consolidate_ground`, `optimize_plane_access`, `plane_access_trial`: `yapnr.stages.*`
  (experimental).
- `freeze_mini_inputs`: `yapnr.project.inputs` (generalized freeze).
- `keyhole_{repair,loop,shift_via,deform}`, `pnr_viewer/`: history only (deleted in PR3b).
- `hardware/tools/pnr_live/` + `$HIER/viewer-dev`: `yapnr.viewer`.
- The `$HIER` harness (`runx.sh`, `envexport.py`, `env*.json`, `launch_*.sh`, `status.sh`,
  `diskguard.sh`, `autoprune.sh`, `prune_live.py`, `pairtest.py`, `pairs/*.sh`, `doe_block.py`):
  `yapnr run/status/prune/gc`, the supervisor, profiles, and the experiment kinds `pair-bench` and
  `doe`.

## Appendix B: known defects to carry as issues

- `via_coalesce.main` calls `items.clear()` but `items` exists only in `worker()` (F821). Fixed in
  PR3a.
- ~~About 25 `board.Remove` call sites may detach items that crash KiCad Python at teardown
  (PR2e).~~ Done: 27 discarded-item calls use `board.Delete`; 3 `Remove` calls stay (kept alive
  or detached with `thisown=False`), guarded by `//hardware/pnr:board_delete_test`.
- `pnr.capacitor_intent` is imported by the viewer's cost service but was never committed (PR4b).
- Pre-existing errors: 7 in the KiCad-Python suite (torch/env imports), 1 in the runtime suite
  (`test_pair_joint_dispatch` import).
- The `pnr_kicad_srcs` filegroup is incomplete and only works because actions run unsandboxed
  (replaced by the closure in PR3b).
- Test files not wired into Bazel (fixed by the glob macro and the wiring check in PR3): 29
  after PR1; after PR2, 68 under `hardware/pnr` (PR1's 25 and 43 new) plus the four outside it.
  `tools/check_test_wiring.py --scope hardware/` reports five more under `hardware/pnr`: SI test
  files that only the `si_%s_test` list comprehension names, which does wire them.
- Three native-loop controller tests (`placement_budget_test`, `progress_budget_test`,
  `retry_controller_test`) fail on the Splanc source commit of PR1: their recorded worker fixture
  has no `graph` key, which `native_loop.congestion_snapshot` reads. PR1 tags them `manual`. Since
  src8b (PR2) they fail earlier, with `FileNotFoundError: 'fixture-python'`: they patch
  `native_loop.subprocess.run`, but the native loop starts its workers through `pnr.proc.run`, so
  the fixture's fake interpreter is executed. Still `manual`.
- `detail_route_test` takes 870 to 1000 s on the development Mac, at or over its 900 s `large`
  timeout, and its `test_drc_clean_by_construction` fails: two nets share a footprint cell. PR1
  tags it `manual`; unchanged with PR2's engine (848 s, the same failure).
- ~~Workers without a time bound (PR2's engine): `electrical_pool`, `hier/native_block` (2 calls),
  `mc/halving`, `transaction_cleanup`, the parallel pair trials of `paired_bootstrap` (stopped
  only by an event) and the cleanup pass in `hardware/tools/keyhole_region.py`; from PR1's code,
  `drc_warm`, `electrical_repair`, `full_iteration`, `geometry_optimize`, `paired_bootstrap`,
  `plane_leaf_repair`, `power_detour_repair`, `staged_signal` and `regression/run.py`. All move
  to `pnr.proc.run` (PR3).~~ Done: all bounded through `pnr.proc`; `//hardware/pnr:proc_test`
  scans for new unbounded calls.
- Engine code tied to the Splanc design (PR2): `feedback/signals.py` finds annotation sources by a
  `hardware/splanc_dev/elec/src/*.ato` pattern; `hier/native_block.py` and `mc/halving.py`
  default to `hardware/splanc_dev` inputs (PR3c, PR3d). Tests and test data that use Splanc
  design files are listed in the [import manifest](history/import-manifest.md#known-leftovers-for-pr3).
- ~~Electrical221's partial-cycle cleanup and barrel-contact bridges in `via_coalesce` and
  `track_graph` are on by default, without a flag or an A/B result (PR2; decisions).~~ Gated,
  default off (`PNR_PARTIAL_CYCLE_CLEANUP`, `PNR_BARREL_CONTACT_BRIDGES`); the A/B is open.
- `hardware/tools/audit_pair_contacts.py` starts a `wx.App` (GUI-bound, outside the loop).

## Appendix C: privacy scrub checklist (gate for every push)

- `tools/privacy_scan.py` over every pushed tree (the Bazel repo check) and over the new commits'
  identities (the CI `lint` job).
- A history-wide scan of the filtered Splanc history (PR1) and of every snapshot commit (PR2).
- Machine paths in:
  - tests: `test_shove*`, `testdata/power_topology`;
  - tools: review, render and plane-access scripts;
  - the viewer: `server.py`, `agent_service.py`, `net_llm.py`, `.scratch/*`, static HTML
    fixtures.
- Machine-local network names and addresses in documentation and test literals.
- Never migrated:
  - conversation logs;
  - agent turn logs;
  - LLM caches under `live/`;
  - run logs;
  - the handoff and continuation documents.
- Inputs with absolute library URIs (`inputs*/fp-lib-table`, `rules.json` `path` entries) are not
  committed. Input sets are created fresh by `yapnr import`, which writes relative paths.
