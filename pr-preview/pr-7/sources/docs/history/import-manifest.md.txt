# History import manifest

PR1 imports the committed history of the place-and-route (PnR) paths from Splanc, as planned in
[migration plan §7.1-7.2](../migration-plan.md#7-history-import-strategy). PR2 adds the newer
engine state that was never committed in Splanc ([PR2 section](#pr2-the-uncommitted-engine-state)).
This page records what was imported, how it was rewritten and how it was checked. It gives
categories and counts only: the mailmap and the replacement patterns stay in the operator's scratch
directory and are never committed.

## Source

| Item                  | Value                                                                   |
| --------------------- | ----------------------------------------------------------------------- |
| Repository and branch | `fughilli/splanc`, `splanc-mini`                                        |
| Source commit         | `b009c9456e60b49133a07ca3d7d9be46a5f54df8`                              |
| Commits on the branch | 271                                                                     |
| Commits imported      | 42 (those that touch the paths below; 229 pruned as empty)              |
| Files at the tip      | 273                                                                     |
| Imported tip in yapnr | `9298b9ab6dbd1eac690da1eca5f305b3ea90f7cb` (second parent of the merge) |
| Where the filter ran  | a scratch clone with its remote removed; the Splanc checkout untouched  |

Only committed history is imported. The uncommitted engine work and the newer snapshots are PR2
(plan §7.3, [below](#pr2-the-uncommitted-engine-state)).

## Tools

| Tool                           | Version                                    |
| ------------------------------ | ------------------------------------------ |
| `git`                          | 2.39.5                                     |
| `git-filter-repo`              | 2.47.0 (`a40bce548d2c`), in a scratch venv |
| gitleaks                       | 8.30.1                                     |
| `tools/privacy_scan.py`        | as of this pull request                    |
| Bazel (for the pass set below) | 7.7.1                                      |

## Imported paths

Files stay at their Splanc paths until PR3b moves them (plan appendix A), so `git blame` and
`git log --follow` keep working across the move.

```text
hardware/pnr/                                   engine, tests, regression harness, splanc_dev fixture
hardware/tools/keyhole_region.py                KiCad-side region worker
hardware/tools/keyhole_repair.py  keyhole_loop.py  keyhole_shift_via.py  keyhole_deform.py
hardware/tools/scan_via_proximity.py
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
LICENSE                                         identical to yapnr's; merges without a conflict
```

At the tip: 230 files under `hardware/pnr/`, 37 under `hardware/tools/`, 3 under
`hardware/experiments/`, 2 under `docs/hardware/`, and `LICENSE`.

`hardware/tools/audit_pair_contacts.py`, listed in the plan as "if tracked", was never committed in
Splanc; it arrives with the uncommitted engine work in PR2. Nothing outside these paths is in the
imported history: no Splanc Mini design inputs, library tables, rule files, `runtime/` copies, logs
or handoff documents.

## Rewrites

The filter ran with `--paths-from-file`, `--mailmap`, `--replace-text`, `--replace-message`, a
`--commit-callback` and `--prune-empty always`. Source code is imported unchanged.

- **Identities.** Every author and committer maps to the owner's public commit address (owner
  decision, [decisions](../decisions.md)): 13 commits carry the owner's name and 29 carry
  `Claude Agent` (agent commits and one commit by Splanc's issue automation). The imported commits
  used four identities: personal addresses, a machine-local address, and an automation noreply
  address. `Co-Authored-By` trailers are kept.
- **File contents.** One file, `hardware/tools/pnr_live/README.md` (a single version in the
  history): machine-local host names and a private network address are replaced with documentation
  values. No other file differs from Splanc.
- **Commit messages.** In one message, a personal address in a trailer maps to the owner's public
  commit address, and a Splanc pull request reference gains the `fughilli/splanc` prefix, so it
  cannot link to a yapnr issue. No other message text changed.
- **Provenance.** Each commit ends with an `Imported-From: fughilli/splanc@<sha>` trailer naming its
  original commit. Author and committer dates are preserved for all 42 commits.

Apart from that README, the imported tip tree is byte-identical to Splanc's `b009c945` for all 273
imported paths.

## Checks

Counts from `tools/privacy_scan.py` (with the e-mail rule as refined in this pull request, see
[decisions](../decisions.md)), before the rewrites (the paths filter only) and on the imported
history. `--stdin` runs over `git log -p`, in CI's format (`--format='%ae %ce%n%B'`) unless noted:

| Check                                     | Before rewrites                                  | Imported |
| ----------------------------------------- | ------------------------------------------------ | -------- |
| `--identities`, author and committer (84) | 77 rejected                                      | 0        |
| `--stdin`, CI format                      | 103 (78 e-mail, 22 host name, 3 private address) | 0        |
| `--stdin`, default `git log -p` format    | 54 (39 e-mail, 12 host name, 3 private address)  | 0        |
| Tree scan of the imported tip (`--all`)   | 5 (one README: 2 host name, 3 private address)   | 0        |
| gitleaks 8.30.1, 42 commits               | 0 leaks                                          | 0 leaks  |

Before the rewrites, the e-mail findings are 77 commit addresses in the header lines and the one
message trailer; the host name findings are 20 commit addresses and 2 README lines, and the private
address findings are in the README.

With the e-mail rule as it was before this pull request, the imported history also had 21 matches
of the address pattern that are code, not addresses: 18 test decorators on added or removed patch
lines (`+@unittest.skipUnless(`, where the diff marker is the local part), a matrix product
(`W@field.reshape(`) and two endpoints in the engine's constraint syntax (`net@board.usbc:A6`). The
owner chose to refine the rule instead of changing the imported code (decisions); the counts above
use the refined rule, under which none of them is an address.

A targeted search of every blob (486) and every message for this machine's specifics (host and
network names, network addresses, volume, home and scratch paths, the login name, personal
addresses) finds nothing. What remains is intended: the documentation values and the words
"Tailscale" and "tailnet" in the viewer README, `$HOME`-relative Bazel output bases in build
examples, the owner's name in design notes (as on the commits), and standard install locations
(KiCad's application bundle, Homebrew tools), which PR3c replaces with toolchain discovery.

Other results:

- Size: 486 blobs; the largest is 554,513 bytes (`splanc_dev.kicad_pcb`); no file exceeds 600 KB.
- History: 42 commits, linear, merged into yapnr with `git merge --allow-unrelated-histories`.
- Messages: filter-repo reported 30 abbreviated hashes of pruned commits; all of them appear only in
  messages of pruned commits. The only commit hashes in the 42 imported messages are their
  `Imported-From` trailers.
- Provenance links: the 9 newest `Imported-From` trailers (`54663fb` to `9298b9a` here) name commits
  that were not on GitHub when the import was made. They resolve once Splanc's `splanc-mini` is
  pushed.

## Bazel

The adaptation commit after the merge:

- points `hardware/pnr/BUILD.bazel` at `@yapnr_pypi` (numpy, pyyaml and torch are in yapnr's lock);
- adds a minimal `hardware/tools/BUILD.bazel` that exports `keyhole_region.py`, a data dependency
  of the native power-access test bundle (Splanc's file also covered its atopile tools);
- keeps `hardware/pnr/pnr.bzl`, Splanc's atopile and KiCad rule, unloaded: it loads Splanc-only
  toolchains, and PR8 replaces it;
- tags five failing tests `manual`, each with a comment (below).

Pass set, from `bazel test //... --config=ci` with `--config=lowmem` on the development Mac
(hermetic Python 3.11, torch 2.3.1, numpy 1.26.4):

- 70 of the 74 imported test targets pass. 53 test methods in 10 of them skip because KiCad's
  `pcbnew` is not importable in the hermetic interpreter; the KiCad lane (PR6a) runs them.
- `placement_budget_test`, `progress_budget_test` and `retry_controller_test` fail with
  `KeyError: 'graph'`: their recorded worker fixture has no `graph` key, which
  `native_loop.congestion_snapshot` reads. The imported code is Splanc's `b009c945` code
  unchanged, and they fail the same way there.
- `detail_route_test` takes 870 to 1000 s on the development Mac, at or over its 900 s `large`
  timeout (it timed out in one of two runs), and one of its four tests,
  `test_drc_clean_by_construction`, fails in both (two nets share a footprint cell). It was not run
  in Splanc's own Bazel environment, whose lock pairs torch 2.3.1 with numpy 2.
- `orientation_test` passes on macOS but, on the `ubuntu-24.04-arm` CI runner, needs more than
  its 60 s `small` timeout and then fails its HPWL acceptance check (oriented 1839 vs
  position-only 1804). Its size is raised to `medium` and it is tagged `manual`; tracked in
  Studio-Fug/yapnr#6 (placer numerics on linux-aarch64, which the container images use).
- These five are tagged `manual` with a comment and listed in the plan's appendix B, so
  `bazel test //...` stays green. `native_regression`, a binary that needs KiCad, was already
  `manual`.
- Nothing else needs a Splanc-only toolchain: no imported `BUILD` file loads Nix, atopile or KiCad
  rules once `pnr.bzl` is left unloaded.

## Known leftovers

- **Test wiring.** 29 imported test files are not wired to a Bazel target (25 under
  `hardware/pnr/tests`, 3 viewer tests under `hardware/tools`, the geometry105 equivalence test;
  `tools/check_test_wiring.py --scope hardware/` lists them). The wiring check covers `tests/`
  only; `hardware/` gets wired when PR3 moves the engine's tests there
  ([decisions](../decisions.md)).
- **Lint.** `hardware/` and `docs/hardware/` are under the presubmit's reserved global exclude
  until PR3a formats them. The privacy scan still covers them (the Bazel repo check and CI).
- **Fixture.** The 3D model paths in `splanc_dev.kicad_pcb` point into Splanc's layout and do not
  resolve here; routing does not need them.
- **Splanc-specific defaults and install paths** are removed in PR3c.

## Reproducing

With the variables of plan §0 bound locally, and the mailmap and the replacement patterns in
`$SCRATCH`:

```sh
git clone --no-local --single-branch --branch splanc-mini "$SPLANC" "$SCRATCH/filter"
cd "$SCRATCH/filter" && git remote remove origin    # nothing can be pushed back
git filter-repo --force --paths-from-file paths.txt --mailmap mailmap \
  --replace-text replace-text.txt --replace-message replace-message.txt \
  --commit-callback "$(cat imported_from.py)" --prune-empty always
git log --format='%ae%n%ce' | "$YAPNR/tools/privacy_scan.py" --identities
git log -p --format='%ae %ce%n%B' | "$YAPNR/tools/privacy_scan.py" --stdin
```

`paths.txt` is the path list above, one entry per line. `imported_from.py` is the commit callback
that adds the provenance trailer:

```python
import re as _re
_msg = commit.message.rstrip(b"\n")
_lines = _msg.split(b"\n")
_i = len(_lines)
while _i > 0 and _lines[_i - 1].strip() != b"":
    _i -= 1
_last = _lines[_i:]
_is_trailer_block = _i > 0 and all(_re.match(rb"^[A-Za-z0-9-]+: \S", _l) for _l in _last)
_sep = b"\n" if _is_trailer_block else b"\n\n"
commit.message = _msg + _sep + b"Imported-From: fughilli/splanc@" + commit.original_id + b"\n"
```

The rewrite is deterministic: the same inputs and filter-repo version give the same commit
hashes.

## PR2: the uncommitted engine state

PR2 lands the engine state that existed only outside git, as planned in
[migration plan §7.3](../migration-plan.md#73-landing-the-uncommitted-engine-state-pr2): the
engine work in Splanc's working tree on top of `b009c945`, which was never committed, and the
snapshot lineage of the hierarchical PnR experiments up to `src15`, the engine of the running H7
experiment. The sources are directories, not commits, so `git filter-repo` is not involved: each
snapshot becomes one commit whose engine tree equals the snapshot, and the side lines join
through merge commits. The commit messages summarize the snapshots' worklogs and the experiment
log (flags and defaults, reasons, measured results) without their machine paths; the worklogs
themselves are not imported.

### Sources

The snapshots are read-only sources; each was copied to a scratch directory, checked against the
digest below, scrubbed there and then committed. The engine digest is the sha256 of the sorted
`path<TAB>sha256` lines of the engine paths (below), without `__pycache__`. The hierarchical
snapshots carry F217's `hashes.json` unchanged, which does not describe them, so the digests are
used instead.

| Snapshot        | Files | Engine digest (first 16) | Parent by content                   | Author date      |
| --------------- | ----- | ------------------------ | ----------------------------------- | ---------------- |
| F217            | 271   | `89d9a76c6419eec8`       | Splanc `b009c945`                   | 2026-09-25 13:29 |
| F222            | 274   | `7312f6e39b2bc4b7`       | F217 + Electrical221                | 2026-09-26 11:12 |
| src8b           | 308   | `0317f10eb8963bd5`       | F217                                | 2026-09-28 09:01 |
| src10.frozen    | 321   | `c00e99c97bef73df`       | src8b                               | 2026-09-28 17:26 |
| src10           | 321   | `5375f18c504b69d9`       | src10.frozen                        | 2026-09-28 21:25 |
| src11           | 360   | `8f82b72ebbc2aa5f`       | src10                               | 2026-09-28 23:41 |
| src12           | 360   | `5c65524664e06010`       | src11                               | 2026-09-29 10:06 |
| src12h          | 360   | `3df271f45f31544d`       | src12                               | 2026-09-29 13:41 |
| src12i          | 360   | `2187c11f3948ed5b`       | src12h                              | 2026-09-29 16:30 |
| src14           | 392   | `71d834a9e7e2abbe`       | src12i                              | 2026-09-29 20:14 |
| src12b (side)   | 361   | `062d5240935a5bed`       | src12                               | 2026-09-29 13:01 |
| src13.r8 (side) | 363   | `77da19a9bcc0cd3f`       | src12b                              | 2026-09-29 21:48 |
| src12n (side)   | 363   | `0571f4e087af4063`       | src12h                              | 2026-09-29 23:01 |
| src15.r1        | 402   | `a7877cea44e4378e`       | merge of src14, src13.r8 and src12n | 2026-09-30 00:52 |
| src15           | 402   | `0eee1b7630890632`       | src15.r1 + review fixes (12 files)  | 2026-09-30 03:19 |

- **F217** is the fresh217 source freeze (2026-09-25) of Splanc's working tree: the Codex agent's
  uncommitted engine work. **F222**, the fresh222 freeze, adds the Electrical221 integration; its
  `hardware/pnr` is byte-identical to Splanc's working tree, which has not changed since.
- The author date of a snapshot commit is the capture time (F217, F222) or the newest source
  modification time; times are Pacific (UTC-7). The committer date is the time of the import.
- Directories that are engine-identical to one above produce no commit: `src9.frozen` and
  `src10.base` (= src8b), `src10b` and `src11.base` (= src10), `src11.frozen` and `src12.base`
  (= src11), `src12n.base` (= src12h), `src14.base` (= src12i), `src15.base` (= src14),
  `src13.base` (= src12b) and `src13` (= src13.r8). Earlier captures of the src13 line are older
  versions of src13.r8 and are not imported. The snapshots `src2` to `src8`, `src8.frozen` and
  `src9` no longer exist; src8b carries their work.
- `src15` was re-verified against its digest immediately before its commit: it is the engine of a
  running experiment and was edited until 03:19 on 2026-09-30.

### Engine paths

```text
hardware/pnr/                          engine package, tests, testdata, regression, si_models,
                                       BUILD.bazel, pnr.bzl, WORKLOG.md
hardware/tools/keyhole_region.py       the only tools the source freezes carry
hardware/tools/freeze_mini_inputs.py
hardware/tools/audit_pair_contacts.py  Splanc working tree only (untracked there, see PR1 above)
```

Not imported: the Splanc board design (`hardware/splanc_dev` outside the engine's own test data,
atopile sources, constraints, sourcing), the snapshots' module and Bazel configuration and
patches, `hashes.json` and `metadata.json`, the snapshot-level worklogs (four of them hold machine
paths), the merge and SI evidence directories, `__pycache__` and every run directory. The Splanc
working tree's viewer changes (`pnr_live`) go to PR4 ([decisions](../decisions.md)). No step
deletes an engine file; every step adds or modifies files only.

### Lineage and commits

The verified lineage differs from the plan's first version (§7.3 is updated in this pull
request): `src10.frozen` precedes `src10` (`src10` adds the owner's 0.15 mm stretch floor);
`src14` is a lineage step; `src13` (USB pairs) and `src12n` (N-0001) are side lines that join at
`src15`; and Electrical221 is in no hierarchical snapshot: it forks from F217, like the whole
hierarchical line.

```text
main
 └ F217 ─┬─ src8b ─ src10.frozen ─ src10 ─ src11 ─ src12 ─┬─ src12h ─┬─ src12i ─ src14 ─┐
         │                                                │          └─ src12n ────────┤
         │                                                └─ src12b ─ src13.r8 ────────┤
         │                                                           merge = src15.r1 ─┘
         │                                                           src15
         └─ Electrical221 (F222) ──────────────────────────────────  merge Electrical221
                                                                     Bazel adaptation
                                                                     docs
```

| Commit                                    | Parents              | Files vs first parent      |
| ----------------------------------------- | -------------------- | -------------------------- |
| Import F217                               | `main`               | 40 added, 21 modified      |
| Import Electrical221                      | F217                 | 3 added, 5 modified        |
| Import src8b                              | F217                 | 37 added, 45 modified      |
| Import src10.frozen                       | src8b                | 13 added, 8 modified       |
| Import src10                              | src10.frozen         | 2 modified                 |
| Import src11                              | src10                | 39 added, 7 modified       |
| Import src12                              | src11                | 2 modified                 |
| Import src12h                             | src12                | 5 modified                 |
| Import src12i                             | src12h               | 1 modified                 |
| Import src14                              | src12i               | 32 added, 13 modified      |
| Import src12b                             | src12                | 1 added, 3 modified        |
| Import src13 (src13.r8)                   | src12b               | 2 added, 14 modified       |
| Import src12n                             | src12h               | 3 added, 17 modified       |
| Merge the USB pair and N-0001 lines       | src14, src13, src12n | 10 added, 36 modified      |
| Import src15                              | the merge            | 12 modified                |
| Merge Electrical221 into the src15 engine | src15, Electrical221 | 3 added, 5 modified        |
| Adapt the newer engine to yapnr's Bazel   | the merge            | `hardware/pnr/BUILD.bazel` |
| Docs                                      | the adaptation       | this page and the plan     |

In all, 228 files change against `main` (+72,122 / -614 lines) in 350 new blobs; the largest is
327,031 bytes (`testdata/feedback/board-placed.json`), and none exceeds 600 KB.

- **Snapshot commits.** Each snapshot commit's engine tree equals its snapshot for every file,
  except the four scrubbed files below; checked for each commit against the snapshot directory.
  The octopus merge records the three lines as parents and has the tree of `src15.r1`, the
  validated result of the per-file 3-way merges done in the experiment area.
- **Electrical221 merge.** `git merge` of the Electrical221 commit into src15 conflicts in two
  files, resolved as follows. In `pnr/via_coalesce.py`, the report's summary keeps src15's
  `removed_cycle_tracks` and adds Electrical221's `reconstructed_cycle_tracks` and
  `removed_cycle_length_mm`. In `pnr.bzl` (two hunks), Electrical221's `PNR_FULL_ELECTRICAL_POOL`
  branch uses src15's headless `"$_KI_CLI"` in both branches, and the action tools are the placer,
  the native and electrical controllers, and the SI tool when `si_report` is set.
  `BUILD.bazel`, `pnr/track_graph.py` and `tests/test_via_coalesce.py` merge cleanly.
- **Build.** Every commit before the adaptation carries Splanc's `BUILD.bazel` (it loads `@pypi`),
  as PR1's import commits do: only the adaptation commit and the docs commit build in yapnr, so a
  bisection inside this series needs the adaptation applied.

### Content scrub

Before the scrub, `tools/privacy_scan.py` over every distinct engine blob of the sources (564
blobs across all snapshots and the working tree) reports 37 findings, all absolute volume paths,
in four files. A targeted search of the same blobs for host and network names, network addresses,
the login name, home and scratch paths and personal addresses finds nothing else. The four files
are scrubbed when each snapshot is staged, before its commit, so no commit carries the paths:

| File                                     | First in     | Findings | Replacement                                              |
| ---------------------------------------- | ------------ | -------- | -------------------------------------------------------- |
| `testdata/power_topology/converter.json` | src8b        | 21       | the design source's repository-relative path             |
| `testdata/power_topology/pd.json`        | src8b        | 10       | the same                                                 |
| `tests/test_shove.py`                    | src10.frozen | 2        | `<repo>/` placeholder for the case path (the test skips) |
| `tests/test_shove_native.py`             | src10.frozen | 4        | `<repo>/` placeholder for the case and interpreter paths |

The fixtures' findings are provenance records (`source.path`); the tests' are the path of a
recorded block case and of an experiment virtualenv. The replacement patterns stay in the
operator's scratch directory. No other file differs from its snapshot.

### Privacy and tree checks

| Check                                                             | Result                       |
| ----------------------------------------------------------------- | ---------------------------- |
| `--identities`, author and committer of every PR2 commit          | 0 rejected                   |
| `--stdin` over `git log -p` of the PR2 commits, CI's format       | 0 findings                   |
| the same with `-m` (CI's form shows no diff for merge commits)    | 0 findings                   |
| `--all` on the head tree                                          | 0 findings                   |
| every blob of every PR2 commit's tree                             | 0 findings                   |
| gitleaks 8.30.1, PR2 commits and the head's `hardware` tree       | 0 leaks                      |
| each snapshot commit's engine tree against its snapshot directory | equal, except the four above |

Authors and committers are `Claude Agent` with the owner's public commit address; the author dates
are those in the source table. Every message ends with the session's two trailers.

### Bazel adaptation and pass set

The adaptation commit re-applies PR1's adaptation to Splanc's newer `BUILD.bazel`:

- the `@yapnr_pypi` load;
- the SI test comprehension listed `test_si_annotations.py` and `test_si_extract.py` twice for
  their own targets, which Bazel rejects as a duplicate label, so the whole package failed to
  load; each source is now listed once;
- `si_integration_test` depends on `:pnr_fab`: its `RouteMainTest` cases import
  `pnr.route.__main__`, which `:pnr_route` excludes;
- PR1's `manual` tags, re-applied with an updated comment (below).

Pass set, from `bazel test //... --config=ci` with `--config=lowmem` and
`--nocache_test_results` on the development Mac (hermetic Python 3.11, torch 2.3.1, numpy 1.26.4):

- 100 of 100 test targets pass: 92 imported (the 69 that passed at PR1 and 23 newly wired:
  `bus_classes`, `cost_capture`, `cost_inspect`, `cost_outline`, `cost_pool_context`,
  `probe_cost_capture`, `electrical_pool`, `fanout_reserve`, `group_backtrack`, `movable_groups`,
  `partial_cycle`, `relative_rows`, nine `si_*` targets, `si_integration` and
  `terminal_min_width`) and yapnr's 8.
- 85 test methods in 17 imported targets skip: they need KiCad's `pcbnew`, the headless KiCad
  copy, or the opt-in live SI cases (`PNR_SI_LIVE=1`). The KiCad lane (PR6a) runs them.
- Still `manual`, each with a comment in `BUILD.bazel` and listed in the plan's appendix B:
  - `placement_budget_test`, `progress_budget_test`, `retry_controller_test`: they fail earlier
    than at PR1 with `FileNotFoundError: 'fixture-python'`. They patch
    `native_loop.subprocess.run`, but since src8b the native loop starts its workers through
    `pnr.proc.run`, so the fake interpreter of the recorded fixture is executed. The engine code
    is unchanged in this PR; the tests are fixed with the fixtures in PR3.
  - `detail_route_test`: unchanged, 848 s on the development Mac and
    `test_drc_clean_by_construction` fails as at PR1.
  - `orientation_test`: size `medium` and `manual` as at PR1 (Studio-Fug/yapnr#6; it passes on
    macOS).
- `pnr.bzl` stays unloaded, and `hardware/tools/BUILD.bazel` is unchanged: `keyhole_region.py`
  is still the only tool a test needs.

### Test wiring

At the head, 68 test files under `hardware/pnr` are not wired to a Bazel target: the 25 of PR1
and 43 new ones. `tools/check_test_wiring.py --scope hardware/` reports 73 there (77 under
`hardware/` with the four outside `hardware/pnr` that PR1 listed): it looks for each file's name in
the `BUILD` file, and five SI test files (`test_si_deck`, `test_si_metrics`, `test_si_models`,
`test_si_physics`, `test_si_runner`) are named only through the `si_%s_test` list comprehension,
which wires them. They stay unwired until PR3 moves the tests under `tests/`.

### Known leftovers for PR3

No engine code changes in this PR; these are carried forward (plan appendix B lists the defects):

- **Workers without a time bound** (AGENTS.md: a subprocess without a timeout is a bug). New in
  PR2: `pnr/electrical_pool.py` (the pool workers), `pnr/hier/native_block.py` (two calls),
  `pnr/mc/halving.py` (one), `pnr/transaction_cleanup.py` (one), the parallel pair trials in
  `pnr/paired_bootstrap.py` (polled until a stop event, no deadline) and the cleanup pass of
  `hardware/tools/keyhole_region.py`. From PR1's code, still unbounded: `drc_warm`,
  `electrical_repair`, `full_iteration`, `geometry_optimize`, `paired_bootstrap`,
  `plane_leaf_repair`, `power_detour_repair`, `staged_signal` and `regression/run.py`. Two of
  PR1's are fixed by src8b (`native_loop`, `via_coalesce` through `pnr.proc.run`). All move to
  `pnr.proc.run`.
- **Engine code tied to the Splanc design.** New in PR2: `pnr/feedback/signals.py` recognizes
  annotation sources by a `hardware/splanc_dev/elec/src/*.ato` pattern, and
  `pnr/hier/native_block.py` and `pnr/mc/halving.py` default to `hardware/splanc_dev`, its `.ato`
  source and its fab JSON files. From PR1: the defaults of `full_iteration`, `geometric_native`,
  `drc_warm` and `freeze_mini_inputs.py`. These become project inputs (PR3c, PR3d).
- **Tests that read Splanc design files** (`hardware/splanc_dev`, the experiment area, or
  `parents[4]`), skipping or failing without them: `power_topology_golden.py`, `test_fab_profile`,
  `test_feedback_moves`, `test_feedback_signals`, `test_halving_generations`, `test_macro_hull`,
  `test_place_pair_weights`, `test_shove`, `test_shove_native`, `test_si_extract`,
  `test_si_integration`, `test_si_live`, `test_src2_library`, `test_synth_native_generations`,
  `test_terminal_min_width` and `test_via_in_pad` (PR1: `test_ingest`). They get fixtures from the
  examples or become `manual` tests of an external project (PR3c, PR6b).
- **Splanc design excerpts in the engine's test data and models.** None is private, but they are
  Splanc Mini data: `testdata/feedback` (25 JSON files of one converter block, the largest 327 KB),
  `testdata/power_topology` (provenance names the Splanc board), `testdata/si` (an excerpt of one
  routed board, three comment lines of `.ato`, and `SOURCE.json`, which names the run it came
  from), `si_models/parts.json` (Splanc part directories and supplier part numbers) and
  `si_models/profile-ws2812b-din.json` (a document path in the experiment area). Regenerate from
  the examples (PR3c, PR6b). PR1's `testdata/splanc_dev` board is the same case; it also carries
  mode 100755 from PR1.
- **Install paths.** `Path.home()/'Applications/KiCad-headless.app'` defaults in `si/extract`,
  `si/models`, `si/runner` and `regression/{run.py,README.md}`; the
  `output/pnr-regression-runtime/bin/python` default in `regression/run.py`; the
  `/Applications/KiCad` defaults in 12 modules, `regression/{run.py,README.md}` and `pnr.bzl`:
  toolchain discovery (PR6a, PR3c).
- **GUI-bound tool:** `hardware/tools/audit_pair_contacts.py` starts a `wx.App` (outside the
  loop): make it headless or drop it (PR3b).
- **Default-on behaviour:** Electrical221's `via_coalesce`/`track_graph` changes (partial-cycle
  cleanup, barrel-contact bridges) are unflagged and now yapnr's default; the H7 run uses src15
  without them. Gate or A/B them (decisions).
- Unchanged from the plan: the `board.Remove` audit (PR2e) and the missing
  `pnr.capacitor_intent` (PR4b).

### Reproducing the series

With the variables of plan §0 bound locally and the scrub patterns in `$SCRATCH`, for each
snapshot in the order of the commit table:

```sh
rsync -a --exclude __pycache__ "$SNAPSHOT/hardware/pnr/" "$SCRATCH/snap/$NAME/hardware/pnr/"
cp "$SNAPSHOT"/hardware/tools/{keyhole_region,freeze_mini_inputs}.py "$SCRATCH/snap/$NAME/hardware/tools/"
# compare the engine digest with the source table, apply the scrub, then:
rsync -a --delete --exclude __pycache__ "$SCRATCH/snap/$NAME/hardware/pnr/" hardware/pnr/
git add -A hardware/pnr hardware/tools && GIT_AUTHOR_DATE="$DATE" git commit -F "$MESSAGE"
```

The octopus merge is written with `git commit-tree` from the `src15.r1` tree and the three parents;
the Electrical221 merge is a plain `git merge` with the two resolutions above.
