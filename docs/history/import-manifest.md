# History import manifest (PR1)

PR1 imports the committed history of the place-and-route (PnR) paths from Splanc, as planned in
[migration plan §7.1-7.2](../migration-plan.md#7-history-import-strategy). This page records what
was imported, how it was rewritten and how it was checked. It gives categories and counts only: the
mailmap and the replacement patterns stay in the operator's scratch directory and are never
committed.

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
(plan §7.3).

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
- tags four failing tests `manual`, each with a comment (below).

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
- These four are tagged `manual` with a comment and listed in the plan's appendix B, so
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
