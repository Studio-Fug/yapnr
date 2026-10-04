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
  `yapnr-kicad:<tag>-src` images (never pruned), the Ubuntu snapshot recorded with the image, and
  the notices (`LICENSE`, `THIRD_PARTY.md`, `SOURCES`) under `/usr/share/doc/yapnr`. (The snapshot
  is recorded inside the image rather than as a label; see "Container images" below.)

Owner decisions for the history import (PR1; [the migration plan](migration-plan.md), §7):

- **Privacy scan: what counts as an e-mail address** (decided 2026-09-29). The imported engine
  history holds 21 matches of the address pattern that are code: top-level test decorators on added
  or removed patch lines (`+@unittest.skipIf(`, where the diff marker is the local part), a matrix
  product (`W@field.reshape(`) and endpoints in the engine's constraint syntax
  (`net@board.usbc:A6`). The owner chose to refine the rule rather than change imported code or add
  allow markers: an `@` match counts as an e-mail address only if its local part contains a letter
  or digit, it is not immediately followed by `(` (a call), and its top-level domain is in the IANA
  root zone, compared in lower case. The list is a static copy in `tools/privacy/iana_tlds.txt`,
  whose header gives the source URL, the retrieval date and the upstream checksum, followed by the
  upstream file with its version line; refresh it by replacing it with a new download. Nothing else
  changed: personal addresses at public top-level domains stay findings in files, `--all` and
  `--stdin`, the allowlisted commit address stays a finding in files, and `--identities` does not
  use the refinement. By construction, addresses at names outside the root zone (such as `.local`,
  `.lan` or `.internal`) are no longer e-mail findings. So that `.local` machine names stay covered
  as before, the `local-host` rule also reports a `.local` name right after `@`
  (`user@<host>.local`, as in ssh or git's guessed identity), besides hyphenated and URL `.local`
  names. Commit identities at any such name still fail `--identities`.
- **Privacy scan: GitHub's service addresses on trailers** (decided 2026-09-30). Dependabot signs
  its commits off with GitHub's support address (`support` at `github.com`), so both of its pull
  requests failed the history scan. The owner chose to accept GitHub's own service addresses
  (`GITHUB_SERVICE_ADDRESSES`, today only that address) only on a `Signed-off-by:` or `Co-authored-by:`
  trailer line of the `--stdin` history scan. A patch line (it starts with its diff marker), any
  other line, and every file scan still report it; personal addresses on trailers stay findings.

Choices made in PR1 itself, following the plan; the owner reviews them with the pull request:

- **Full-history import of the engine paths** (plan §0.3, item 7). The committed Splanc history of
  the PnR paths is imported with `git filter-repo` (42 commits) rather than as one snapshot commit,
  so blame and `git log --follow` keep the reasons behind the engine's heuristics; the history is
  small (486 blobs, none over 600 KB). Merging PR1 with a merge commit answers the plan's open
  question Q2b. What was imported, how it was rewritten and how it was checked is in the
  [history import manifest](history/import-manifest.md).
- **The test-wiring check keeps its `tests/` scope until PR3.** This replaces the plan's first form
  (extend the check to `hardware/` with a shrink-only baseline in PR1): PR2 replaces
  `hardware/pnr` from the engine snapshots, and PR3 moves its tests under `tests/`, where the glob
  macro wires them and the check covers them, so a baseline for a tree that is replaced twice would
  only be churn. `tools/check_test_wiring.py --scope hardware/` lists the 29 unwired test files of
  the import, and the manifest counts them.
- **The imported Bazel targets run in `bazel test //...`.** `hardware/pnr/BUILD.bazel` loads
  `@yapnr_pypi`, and a minimal `hardware/tools/BUILD.bazel` exports the one tool a test needs.
  Splanc's `pnr.bzl` (atopile and KiCad Python rules, which yapnr does not have) stays in the tree
  unloaded until PR8 replaces it. Tests that fail on the Splanc source commit, or run past their
  timeout, are tagged `manual` with a comment and listed in the plan's appendix B, so CI stays
  green; the manifest records the pass set.

Choices made in PR2 (the newer engine state; [migration plan](migration-plan.md) §7.3), following
the plan; the owner reviews them with the pull request:

- **Snapshot-exact commits, with merges.** Each engine snapshot becomes one commit whose engine
  tree equals the snapshot (apart from the scrubbed files), and the side lines (Electrical221, the
  USB pair engine, N-0001) join through merge commits, as they were developed. The alternative, a
  linear series with the merges applied as plain commits, would lose the side lines' focused
  diffs. Only the head builds in yapnr, as in PR1. PR2 is merged with a merge commit.
- **Machine paths are scrubbed when a snapshot is staged, not in a later commit.** Four engine
  files held absolute paths of the experiment area; a later scrub commit (the plan's first form)
  would have published them in the history. Every commit that carries these files has the
  scrubbed version; the [import manifest](history/import-manifest.md#content-scrub) lists them.
- **No engine edits in PR2.** Tests that fail with the newer engine are tagged `manual` with a
  comment, and the defects found while importing (workers without a timeout, Splanc defaults,
  Splanc data in the test fixtures) are listed for PR3 in the manifest and in the plan's
  appendix B.
- **Electrical221 is imported as it ran in Splanc.** Its `via_coalesce`/`track_graph` cleanup is
  on by default without a flag or an A/B result, unlike the rule for new engine behaviour; the H7
  run uses `src15` without it. It stays as imported (faithful to Splanc's working tree) until the
  owner decides between gating it and an A/B run. (Superseded: gated, default off, in the engine
  hygiene change below.)
- **src8b's default changes stay** (the `jlc-pofv` fab profile, bounded workers, the
  parallel-commit fix, plane-access reuse): they predate the default-off rule and are documented,
  with their measurements, in the src8b commit.
- **The Splanc working tree's viewer changes (`pnr_live`) go to PR4,** not PR2: the cost service
  imports `pnr.capacitor_intent`, which was never committed, and the README reintroduces machine
  names that PR1 scrubbed.

Choices made in the engine hygiene change after PR2 (branch `claude/engine-hygiene`; the PR3
leftovers of the [import manifest](history/import-manifest.md#known-leftovers-for-pr3)); the owner
reviews them with the pull request:

- **Electrical221's cleanup is gated, default off,** which makes the default engine behave as
  `src15`, the engine of the H7 experiment, again. `PNR_PARTIAL_CYCLE_CLEANUP=1` enables the
  partial-track cycle edits (and the stricter cycle-trial match); `PNR_BARREL_CONTACT_BRIDGES=1`
  enables the bridges for barrel-contact tails. Two flags rather than one, so an A/B can separate
  them. Turning them on needs an A/B on the hierarchical engine (the rule for new behaviour);
  Electrical221's own evidence is in its import commit. The full electrical pool was already
  opt-in (`PNR_FULL_ELECTRICAL_POOL=1`). The coalesce report keeps Electrical221's two summary
  keys, which are reporting only.
- **Discarded KiCad items are deleted with `board.Delete`, never `board.Remove`.** `Remove` makes
  the Python wrapper the item's owner, so the item can be freed after its board (the teardown
  SIGSEGV of `fanout_reserve.release`, fixed in src12i). `Remove` stays only for an item that is
  re-added, kept alive on purpose (the warm DRC keepalive arena) or detached with
  `thisown=False`. Code must not use a wrapper of a deleted item, so it reads uuids and sizes
  first. `//hardware/pnr:board_delete_test` fails on any `Remove` call beyond the three it lists
  (per call, so a new one in a kept file fails too). The signal retries of `via_coalesce` and
  `native_loop` and the `os._exit(0)` of the hier workers stay as a second line of defence.
  Replayed on the H7 experiment's real boards against the pre-audit code, the five sites those
  steps reach (`hier.subpcb`, `electrical_repair`'s blocker segments, `via_coalesce.apply`,
  `track_graph.apply_cycle`, `shove.ladder`'s rip-up; 176 items) give the same boards and
  reports, exit cleanly, and no longer print SWIG's "memory leak ... no destructor found"
  messages. The Delete run's placement, plane-access and coalesce geometry equals H7's own
  (src15) boards. A full rung-1 evaluation, which reaches the other sites, is left for a quiet
  machine: its nested workers exceed the two-process KiCad budget kept while H7 runs.
- **Every engine subprocess has a deadline, through `pnr.proc`,** with three env-overridable
  limits sized as wedge guards from the H2 to H7 records: `PNR_WORKER_TIMEOUT` (1800 s, unchanged;
  a worker with its own `--seconds` budget gets at least twice that plus 600 s),
  `PNR_PHASE_TIMEOUT` (4 h, a phase of bounded workers; the longest observed phase took 90 s) and
  `PNR_EVALUATION_TIMEOUT` (max(48 h, 48 x the native budget), one `pnr.full_iteration`; the
  longest observed evaluation took 18.3 h for a 5400 s budget). `0`, a negative value or `inf`
  means no limit (an explicit opt-out; `0` does not mean "kill at once"). A timed-out child
  counts as a failed worker (exit -9), as a crash did before, and the signal retries of
  `native_loop` and `via_coalesce` do not rerun it (a wedge, not a crash). Only the parent
  enforces a deadline, so a timeout kills the child's whole process tree: the child, every
  descendant (stopped with SIGSTOP first, so nothing forks away) and every process group a
  descendant leads. The workers of a killed `pnr.full_iteration` run in their own groups and
  would otherwise outlive it without a deadline. An interrupted wait (Ctrl-C) kills the tree
  too, as `subprocess.run` kills its child. The converted calls, the subpcb cut included, keep
  the child in the caller's process group (`session=False`), as their plain `subprocess.run`
  did, so stopping a run's process group still stops them; the existing `pnr.proc` callers keep
  their own groups. A nested deadline only binds when it is shorter than its parent's: the
  4 h `PNR_PHASE_TIMEOUT` of `keyhole_region`'s cleanup pass applies when the tool runs on its
  own, while under the native loop and the repair adapters the 1800 s (or `worker_timeout`)
  deadline of the whole `keyhole_region` worker comes first. `//hardware/pnr:proc_test` fails on
  any new subprocess call without a deadline (aliased imports included); the opt-in warm DRC
  host daemon is the one exception (its session ends it).
- **CI's history scan includes merge diffs** (`git log -p --diff-merges=separate`): a merge
  commit's diff against each parent is scanned, so a conflict resolution cannot bypass the scan
  (PR2's two merges were checked this way only locally). The explicit option, not `-m`, because
  `-m` follows `log.diffMerges`, and `first-parent` there drops the second parent's diff.
- **The docs enable MyST's `strikethrough` extension** so status lists (the manifest's PR3
  leftovers, the plan's appendix B) can strike done items instead of deleting them.
- **Placement is reproducible per platform, not across platforms** (Studio-Fug/yapnr#6). The same
  inputs and seed give one board on macOS arm64 and another on linux-aarch64 (the CI runner and
  the images). Measured with torch 2.3.1 and numpy 1.26.4 on both: the seeded start, the thread
  count (1) and the legalizer agree bit for bit (each platform's global placement legalizes to
  the other's board exactly), and `torch.use_deterministic_algorithms(True)` changes nothing. The
  two torch wheels round float32 `exp` (0.6 % of inputs), `log` (0.1 %) and `addcmul` (0.07 %,
  Adam's second moment) differently by one ulp. The first gradient difference appears at the
  second optimizer step; the non-convex placement grows it to millimetres by step 200, and
  legalization turns that into another legal placement. Evaluating `exp` and `log` in float64
  removes the gradient difference but not Adam's. The engine is not changed: bitwise
  cross-platform results would need torch's transcendental and fused kernels replaced, would
  change every result, and would break again with the next torch build or on x86_64. Both
  platforms draw from the same distribution (30 seeds each; mean final HPWL with orientation
  1846 mm on macOS and 1838 mm on Linux, without 1862 mm and 1873 mm), so a run is reproducible
  on one image digest or one Mac environment, and comparisons between runs stay on one platform.
- **`orientation_test` checks what the placer guarantees.** Its former check, oriented HPWL at
  most the position-only HPWL for seed 0, held on macOS by 3.6 mm (0.2 %) and failed on
  linux-aarch64 by 35 mm. Over those 30 seeds orientation beat position-only on 18 (macOS) and 20
  (Linux), a mean gain of 16 ± 18 mm and 36 ± 19 mm (± one standard error) against a per-seed
  spread of about 100 mm: the single-seed check fails on 22 of the 60 (platform, seed) pairs, and
  a median over three or five seeds on 27 to 32 % of the seed sets. The test now requires the
  mean HPWL of seeds 0 to 2 with orientation to be at most 5 % above the position-only mean
  (fails on 2.3 % of the seed triples of the measured pairs; the ratio is 0.997 on macOS and
  0.987 on Linux). That is a coarse guard, not proof that orientation costs no wirelength: a
  regression of up to 5 % passes, and the 90°/270° mix-up below fails it by 0.6 %. A tighter
  margin fails more seed triples on a new platform or torch build (2.5 %: 10.5 %; 3 %: 8.1 %;
  4 %: 4.4 %), so the margin stays and the synthetic board is the guard of the search. A
  synthetic board with known best angles, started from explicit positions so the seed does not
  matter, checks that the search finds them (22.5 mm against 42 mm); this well-conditioned
  problem gives the same result on both platforms. A 90°/270° mix-up in the
  rotation convention fails three of the checks. A golden result per platform was rejected: it
  pins numbers that every torch bump changes and cannot state the orientation claim (Linux's
  seed 0 is worse than position-only). Nine placements take 44 s on the development Mac and 51 s
  on two linux-aarch64 cores; the shared CI runner, which took 77 s for the former three, needs
  an estimated 185 to 240 s, so the target is `large` (900 s) and no longer `manual`.

Choices made in PR3a (the mechanical format of the imported engine; [migration plan](migration-plan.md)
PR3a); the owner reviews them with the pull request:

- **Code keys hash each module's canonical syntax tree, not its bytes** (code key scheme 2), so
  the format commits do not invalidate the libraries and trials that routing feedback imports.
  `pnr.feedback.signals` hashes every module of the evaluation closure by module name; comments
  and layout are not in the tree, and on top of that it applies what black's own AST check
  allows (docstrings compared stripped line by line, the `u` prefix, `del (a, b)` as
  `del a, b`) and what isort does: each block of consecutive imports counts as a sorted set of
  imported names, unless the order can matter (a star import, or a name bound twice). An import
  never moves across other code in that form, and a docstring's text still counts. Fields are
  named and empty ones left out, so Python 3.9, 3.11 and 3.12 give the same key for the engine.
  Measured on the engine: black and isort change 338 files, 226 of them in their plain
  `ast.dump` (import order, docstring indentation), none in the canonical form, and both routers'
  keys stay the same. New records stamp `code_key_scheme` next to `code`. A record without it
  is legacy (scheme 1, raw file bytes by path, the exact former key): it is compared under that
  scheme, or re-keyed from its evaluation tree when that tree still hashes to its stamp, so
  trials of frozen snapshot trees survive the format while those of a tree reformatted since
  do not. PR3b's rename needs a scheme that applies the module map to module names and imports.
  The router key string (`router_key` in statuses, tables and libraries) ends with the scheme,
  and the feedback report names a key's scheme when it is not the current one, so a legacy key
  and a new one cannot be mistaken for each other there.
- **The new key has two blind spots, by design.** Reordering the imports inside a block does not
  change it, even where the order matters at import time, so a fix to an import-order bug changes
  the key only if it changes something else too. Whitespace at the start or end of a docstring's
  lines does not change it either (black re-indents docstrings), although `__doc__` keeps it.
  Both are what the format needs to leave alone. In this format, black changed docstring
  whitespace in six files; one of them, `hier/subpcb.py`, passes its module docstring to argparse,
  whose default formatter reflows it, so its `--help` is unchanged.
- **Other hashes of engine source stay raw bytes**, and the format changes them:
  `place/cost_capture.py` (`runtime_sources` and `probe_sources` in cost captures), `profile.py`
  (`source_hashes`), `drc_warm/host.py` (`sha256` in `ready.json`) and `regression/run.py`
  (`source_hashes` in `provenance.json`). They are diagnostic: nothing compares them across trees
  (the regression runner checks its manifest only against its own source freeze, within one run).
- **The format is three mechanical commits, and the pull request is merged with a merge commit**
  so their hashes reach `main` and a follow-up lists them in `.git-blame-ignore-revs` (the plan
  had a squash merge and a follow-up with the squashed hash). black 25.1.0 and isort 6.0.1, the
  pinned hooks run through prek, cover every Python file under `hardware/` (`pnr`, `tools`,
  `experiments`): black changes 369 files, isort 294. The third commit pins isort's settings in
  `.isort.cfg` (black profile, 100 columns, `known_first_party = pnr,yapnr,tools`): without them
  isort guessed first-party packages from its working directory, so `pnr` was third party when
  run from the root (as prek runs it) and first party from `hardware/pnr`, where 117 files then
  failed `isort --check`. Re-sorting with the pinned settings changes 130 files under `hardware/`
  (`pnr` imports get their own block), each only inside one run of imports; the code keys and the
  results of the 43 re-sorted test files are unchanged. black's own equivalence check passes for
  all 369 (plain `ast.dump` differs in 6, docstring whitespace only). isort's differences are all
  reorderings, splits or merges of imports inside one run of consecutive imports (each run
  compared as a multiset), so no import moved across code, and no file needed
  `# isort: skip_file`. isort changes the syntax tree of 250 files (the other 44 only in
  layout); no torch/numpy pair changes order, the engine's import-time effects
  (`torch.set_num_threads(1)` in `pnr.place.model`, three `fab_profile.bind_active` callbacks
  that each set their own module's globals) do not depend on order, and each of 344 modules of
  `hardware/pnr` imports in a fresh interpreter with the same outcome before and after (import
  cycles would show there).
- **No hand lint fixes; a per-file flake8 baseline instead.** After black and isort, flake8 finds
  479 issues in 157 files, 112 of them unused imports. Every fix changes a syntax tree, and 155
  of the findings (27 unused imports, the F821) are in the 127 modules an evaluation runs, where
  any change alters the code key and so invalidates routing-feedback libraries and trials, which
  is what the new key scheme avoids. Outside them an unused import is still a module attribute
  that tests patch or other modules import (`subprocess` in `native_electrical`), so proving one
  unused takes a per-name search. `.flake8` therefore lists each file's remaining codes in
  `per-file-ignores`; new files and codes are checked in full, the list only shrinks, and PR3b's
  codemod (which changes every import anyway) is where it shrinks.
- **Non-Python files under `hardware/` keep per-hook excludes** where a hook would rewrite or
  reject them, measured with the global exclude lifted: prettier rewrites 42 files (SI model and
  test-data JSON, the engine's Markdown), markdownlint fails on 3 Markdown files, buildifier
  rewrites `BUILD.bazel` and `pnr.bzl` (regenerated in PR3b), end-of-file-fixer adds a final
  newline to 28 byte-exact test-data files and trailing-whitespace changes the built viewer's
  CSS. `name-tests-test` rejects two test helpers (`cost_fixture.py`,
  `power_topology_golden.py`), which PR3b moves. Every other hook passes on `hardware/` and now
  covers it.
- **Five tests that read engine source as text now ignore its layout.** `test_via_in_pad` and
  three `test_src15_merge` checks matched Splanc's single quotes and one-line calls, so the
  format changed their results; they now compare with quotes and whitespace normalized (a
  wrapped call's first two lines count as context). `board_delete_test` required
  `thisown=False` on the line of the kept `b.Remove(t)` calls (`a; b` joins that black splits);
  it now requires the next statement to set that item's `thisown` to `False`, on the syntax
  tree. On the tree before and after the format they give the same result per test. Two of the
  `test_src15_merge` checks still fail, as before the format, on Splanc-only files and eight
  tool defaults (PR3c).
- **What Bazel runs of the new key.** `code_key_test` covers the key, legacy records and both
  feedback drivers' seed imports on fake records (halving's `_import_seed_runs`, synth_native's
  `_import_code`), and `test_feedback_signals` is wired as `feedback_signals_test`. The two
  generation tests (`test_halving_generations`, `test_synth_native_generations`) stay unwired:
  without Splanc's Mini inputs every one of their tests skips, so they wait for PR3c's fixtures.
  They pass by hand with the Mini inputs, with new and with legacy stamps.

Choices made in PR4 (the viewer; branch `claude/pr4-viewer`), following the plan where it
applies; the owner reviews them with the pull request:

- **One branch instead of four sub-PRs (4a to 4d).** The deployed viewer is one program whose
  parts import each other (the server wires the cost, schematic, source, notes, agent and 3D
  services together), so it is ported as one commit series: a pure move, the imports (privacy
  scrubbed while staging), a mechanical format, then packaging, lint, the build and the tests.
- **Imported from the deployed viewer only,** after checking it is a superset of Splanc's
  `pnr_live` working tree (plan risk "four diverging viewer copies"). The Splanc working tree's
  uncommitted `pnr_live` changes are imported first, as their own commit.
- **three.js is fetched, not vendored** (this replaces "vendored under `third_party/three/`" in
  the plan and in `THIRD_PARTY.md`): `three.core.js` (1.46 MB) and `three.module.js` (0.66 MB)
  exceed the 600 KB file limit, and the version the viewer uses is 0.186.1 (r186), not r180. It
  is pinned by sha256 like elkjs.
- **elkjs's license is served from the pinned tarball** (`third_party/elkjs/LICENSE.md` in the
  served directory) instead of a copy under `third_party/elkjs/` in the repository: the text then
  always matches the fetched version.
- **The third-party files come in through `use_repo_rule(http_archive)` in `MODULE.bazel`,** not a
  new `bazel_dep` (no rules_js); a genrule copies them unmodified into `//yapnr/viewer:dist`.
  Repositories are fetched lazily, so a build that does not need the viewer never downloads them.
- **No node toolchain.** The two node tests (`test_ui.cjs`, `test_controls_ui.cjs`) read Splanc's
  paths and a captured state; they are dropped, not ported. Browser checks become `tests/e2e`
  (Chrome DevTools) in a later change; until then the viewer's behaviour is covered by the Python
  unit tests and manual screenshots.
- **The Ask agent needs `--agent on` also on loopback** (it was on by default there), its web tools
  need `--agent-web on`, and AI net labels need `--net-summaries on` (they followed the agent).
  The default agent model stays opus with a $2 per-turn and $20 per-process cap; the owner may
  prefer sonnet as the default.
- **Configuration is flags plus a TOML file (`yapnr-viewer-v1`), not the project manifest yet.**
  The manifest's `[viewer]` table and `--project` arrive with the project abstraction (PR3d);
  until then no setting defaults to a machine path, and machine tool paths come from flags,
  `YAPNR_*` variables or `~/.config/yapnr/config.toml`.
- **`pnr.capacitor_intent` is not recreated.** It exists in no snapshot; the cost replay reports a
  placement context that needs it as unavailable. The owner files the issue.
- **The viewer stays out of the wheel and the images** until the engine it imports is in them
  (PR3b); `//yapnr:cli` has no viewer dependency.
- **One spend cap per server process for every paid call** (`--agent-total-usd`, default $20):
  Ask turns and AI net labels draw from the same meter (`yapnr/viewer/agent/spend.py`). A call
  holds its whole budget (`--agent-budget-usd`, default $2; `--net-summary-budget-usd`, default $1)
  while it runs and starts only if the cap covers it, so concurrent calls cannot pass the cap; a
  call without a cost report from the CLI (timeout, cancel, killed) is charged its whole budget.
  The meter is in memory: a restart resets it.
- **The Apache-2.0 license text is kept in the repository** (`third_party/licenses/`), byte for
  byte as published: elkjs's bundle includes an Apache-2.0 web-worker shim, and its tarball has
  only the EPL-2.0 text. It is served next to `elk.bundled.js`; the code itself stays fetched.
- **The Source link names the viewer's own revision,** not the engine commit of the run being
  viewed (plan §1.4, "Network use"): it links the repository tree of the commit the server runs
  and marks a checkout with uncommitted changes as modified. Runs do not record the engine commit
  yet; showing it per run waits for the run manifests (PR3d).
- **No `//yapnr/viewer:dev` target.** There is no auto-reload server: a development viewer is
  `bazel run //:viewer -- --root <live dir> --port <spare port>`, rebuilt after edits to
  `static/`.
- **The viewer's KiCad lookup is stricter than plan §2.3** until `yapnr.kicad.toolchain` replaces
  it (PR6a): it follows AGENTS.md and refuses everything inside `KiCad.app` or
  `/Applications/KiCad`, KiCad's Python included (the plan allows the stock Python); on macOS it
  discovers only the headless copy, never `PATH`; and it does not check for KiCad 10.x (the
  toolchain module will).

Choices made for the regression ladder in CI and the animations (branch
`claude/ladder-animations`); the owner reviews them with the pull request:

- **Tracing is opt-in and observational.** `PNR_TRACE_DIR` enables `pnr.trace` (format
  `pnr-trace-v1`); unset, each hook is one environment lookup. Hooks use no random number
  generator, add no torch operation and change no engine state; the first recorder error disables
  recording for the process. `trace_noop_test` requires byte-identical placements, routes and
  reports with tracing unset, set and unset again (baseline loop and initial pool), and a traced
  and an untraced ladder run give identical `placed.json`, `routes.json` and reports.
- **The legalizer's accepted order, not its backtracking.** A trace records the order in which the
  legalizer placed each part on the path it kept (`legal`), not the branches it rejected, as the
  cost capture keeps only the accepted search path.
- **Animations follow the critical path** (`pnr.provenance`): the ancestry of the final board,
  where a selection contributes only its chosen candidate; the rejected candidates appear as
  montages. Rendering is pure Python and Pillow (no numpy, torch or KiCad); the `.kicad_pcb` reader
  (`pnr.trace_board`) needs no KiCad process.
- **The ladder lane runs in the published image, resolved to a digest,** on `ubuntu-24.04-arm`,
  and runs the checkout's engine sources. A runtime guard installs the checkout's runtime lock into
  an overlay venv when a pull request changes the pins, rather than building the image in the job.
  The lane is informational until the owner makes its `ladder` check required.
- **The ladder routes and is judged under its fixtures' own rules.** The fixtures carry their own
  fab block (0.2 mm clearance, 0.6/0.3 mm vias, documented in the ladder README), and RESULTS-118
  passed under it before fabrication profiles existed. `run.py` now sets `PNR_FAB_PROFILE` for
  every stage from `--fab-profile`, default `legacy`, which enforces exactly that block; the boards
  are RESULTS-118's (all 16 baseline runs, seeds 0 and 1, identical vias and copper) and all eight
  cases pass. The alternative, the engine's default `jlc-pofv` profile, overrides the fixtures'
  numbers with JLC's (0.127 mm clearance, 0.45/0.30 mm vias) and would make the README's stated
  rules false; it stays available as `--fab-profile jlc-pofv`, and `route_case.py` applies the
  selected profile to the router's rules (the identity for legacy), so that configuration routes
  under the rules KiCad judges it by. Before, the unset variable made writeback stamp `jlc-pofv`
  while the router used the fixtures' rules, and cases 04 to 08 failed on vias touching their own
  SMD pads. With the profile applied at the router, `jlc-pofv` passes every case as well (pool
  seed 0 and baseline seeds 0 and 1); making it the ladder's default is the owner's call
  (WORKLOG, "Next").
- **The committed animations.** All eight ladder cases are animated in `docs/animations/` (not
  `_static`, whose references the Pages step rewrites), from a traced pool run (seed 0) on the
  development Mac; all eight pass the gate. A failed case is only rendered with `--allow-failed`,
  and its end card then names the broken rules in red. The README shows `05-timer-led-10`, the
  TLC555 blinker (the request's "555 flasher"), as a GIF (GitHub autoplays it); the page uses WebP.
  `tests/unit/repo/test_animations.py` bounds the folder (WebP 2.5 MB, GIF 5 MB, 20 MB in all,
  hashes in the manifest, no metadata) in place of the large-file hook, and prettier leaves the
  generated JSON as written. A refresh adds about 12 MB to the history, so it is deliberate.
- **A montage follows the winner's replay.** The timeline is one straight line: a selection's
  candidates appear once the winner's own replay reached the state their tiles show, and the bar
  counts committed connections only, so nothing on screen runs ahead of the process.
- **Coarse animations of halving runs.** A successive-halving run records no trace;
  `pnr.provenance.halving_trace` rebuilds one from its saved placements, rung objectives and the
  winning rung's native phases, and the overlay's phases say what was saved, not more. Animations
  of Splanc runs stay local (the board is not public).

Owner decisions for the atopile toolchain and part data (2026-09-30;
[the atopile toolchain](frontends/atopile.md), [the part cache](part-cache.md)):

- **The atopile tooling is imported under `AGPL-3.0-or-later`:** the offline picker server and
  the board-outline script (from rules_atopile, the owner's repository, which declared no license
  when they were copied) and Splanc's scripts (AGPL-3.0). The imports name their source in the
  commit message and the module docstring.
- **No EasyEDA-derived part files and no vendor-API data in yapnr.** Parts and catalogs live in a
  **part cache** outside every repository: a local directory, or a server the owner may run in
  public (user-generated content, with provenance and licence metadata per part and takedowns).
  `tools/check_part_data.py` fails on generated part files in the repository; tests use synthetic
  parts only. Splanc's committed parts and picker catalog were moved into a local cache on the
  development machine, so Splanc builds from a parts lock without them.
- **atopile via a hashed lock; "No Nix" stays.** atopile 0.15.8 is pinned. `yapnr atopile setup`
  installs it with uv from one fully hashed lock per platform (Python 3.14.7 from
  python-build-standalone), resolved as of the date that reproduces the environment Splanc's
  Nix build used. linux-aarch64 has no atopile wheel on PyPI; its wheel is built from the
  sha256-pinned sdist with pinned build dependencies (the scikit-build-core fork by commit). A
  0.15.9 bump comes through an A/B. The `yapnr` image bundles the environment later (A4).
- **Builds are offline and isolated.** A build copies the project, materializes its locked parts
  from the cache, answers part queries from a loopback picker, runs atopile with an allowlisted
  environment and a deadline, and records a UUID-normalized input id. A hook loaded into every
  atopile interpreter (forkserver workers included) replaces the Nix source patches: the picker
  token only for a parsed loopback URL of the runner's port, empty queries answered locally, picks
  attached from the project's parts, no EasyEDA, no git clone of a dependency, no GUI KiCad and no
  contact with a running one.
- **The part cache serves part files only.** A file must match its type, is served only while a
  stored part uses it, and is removed when no part took it up within a grace period; a takedown
  also blocks the part's own files under any name. Tokens are checked before a body is read, sent
  by the clients only to the server named (no redirects) over https or loopback, and the write
  token never with reads. Parts can be marked local-only; a public server refuses a cache that
  holds one.
- **Ordering stays staging-only** (for the later ordering PRs): cart, quote or payment page; the
  human pays on the vendor's page.

Choices made for the constraint and hierarchy showcases (branch `claude/animations-groups-hier`,
[design](design/constraint-and-hier-animations.md) §9, §11 and §12); the owner reviews them with
the pull request:

- **`line_group`** is a new hard constraint (ordered literal members, `pitch_mm` or `gap_mm`,
  `rot`, an optional soft `edge`), placed as one rigid macro inside `place()` with the
  hierarchical macro code; `row` is unchanged.
- **`edge_align` gains `hard` and `tolerance_mm`** (default false and 1.0 mm): a hard edge part
  stays within the tolerance through legalization and the legality checks. The facing is set with
  `orientation`, not by `edge_align`.
- **Showcases stay outside the gate.** `designs.showcases()` is a list beside `designs()`, run
  with `run.py --showcases`; the pull-request lane never runs them and the nightly lane runs them
  for information.
- **The animations folder budget is 30 MB** (was 20 MB): the four showcase files add about
  13 MB. The hierarchical WebP may use 3.5 MB (it is 2.69 MB even at the encoder's last step);
  every other WebP keeps 2.5 MB and every GIF 5 MB.
- **A second media item in the README:** the side-by-side chaser GIF (free LEDs against a line
  group), 800 px, under the 555 flasher.
- **A showcase that fails its gate is not committed as an animation** (the page then says why),
  rather than shown with a red end card. All five cases of the committed run pass.
- **Half-turns are flips.** The placer records only its snapped four-way rotation, so a body or
  part recorded at opposite angles switches at the middle of the interval instead of sweeping
  through angles it never had; quarter turns keep the labelled shorter-arc tween. The ladder's
  own animations keep their sweeps (their timelines are pinned unchanged); only the showcase
  pacing and rigid bodies flip.
- **Off-board poses widen the camera** over that global placement (showcase pacing only), and the
  board zooms back in after legalization, rather than clamping parts to the panel edge.
- **The edge comparison replays its pool.** The followed start never changes its edge order, so
  its shortlist replays all eight starts' recorded global placements before the tiles hold;
  the chaser comparison keeps static tiles (its README GIF is near its 5 MB budget).
- **A line group reserves its members' sides** (`line:<name>`, not a `block:` macro) and refuses a
  source-locked member rather than pinning the whole line to it.
- **Follow-ups:** companion rows (an LED and its resistor as one rigid unit), a hard edge for a
  whole line group, members on the bottom side, plane-access intents inside a line group.

Choices made for RF microstrip inverse design (#29, branch `claude/rf-topopt`,
[design](design/rf-topology-optimization.md) §2 and §16, [guide](rf-inverse-design.md)); the
owner reviews them with the pull request:

- **Own FDTD solver, no new runtime dependency.** Meep has no lumped ports, its eigenmode ports
  do not model metal microstrip and it is conda-only; openEMS is an external GPL program without
  an adjoint. `yapnr.rf` is numpy with an optional torch fast path, deterministic, at most 4
  threads.
- **Line ports into the absorbing boundary** are the case ports (V/I wave separation, the
  reference plane moved to the design region); resistive lumped ports stay for elements and as a
  fallback.
- **Crank–Nicolson conductivity and the exact discrete adjoint** (numerical frequency Ω and
  σ·cos(ωΔt/2)): gradients agree with finite differences to about 1e-8.
- **Copper is a zero-thickness sheet** whose conductance is log-interpolated per pixel and
  averaged onto the grid edges; the damping term of the paper is implemented but off.
- **Own MMA, written from Svanberg's publications** (the reference codes are GPL, nothing is
  copied), in its native min-max form. **Plain MMA is the default;** the conservative variant
  (CCSA/GCMMA, NLopt's MMA as in the paper) is `optimizer.conservative: true`. On the tiny
  two-port test it never let t rise but needed 96 forward runs against 18 and ended worse
  (t −0.10 against −0.28).
- **Mirror symmetry on the design variables** (pixels of one mirror orbit share a variable),
  not by averaging mirrored grids, so symmetric designs stay binary.
- **Footprints are KiCad 10 net ties:** one SMD pad per port, the copper as keyholed `fp_poly`
  islands with `net_tie_pad_groups` (an island on one pad becomes that pad's custom shape), the
  stackup the design assumes in the description; KiCad's own `kicad-cli` loads them (KiCad lane).
- **The end-to-end design cases are manual and slow;** they never run in CI. Each has a smoke
  variant that does (the same topology on a tiny grid for four iterations; it checks the
  pipeline, not the RF targets).
- **The cases do not start from the paper's uniform 0.5.** With copper, ρ̄ = 0.5 is a 377 Ω/sq
  absorber over the whole window: from it the divider grew into one radiating plate. The
  divider starts from a uniform x = 0.3 (an almost transparent sheet on the steep part of the
  projection); the filter banks from a junction of their ports with quarter-wave stubs
  (`optimizer.seed: stubs`, below), since from 0.3 the diplexer's window absorbed for 15
  iterations and then formed a radiating mass; the antenna from
  the closed-form inset-fed patch (`seed: patch`), since every uniform start (0.3, 0.5, 0.7,
  0.7 at β = 32) stayed at the bare feed or a plate. Gray copper absorbs before it radiates.
  The seeds are computed from the spec alone; every pixel stays a design variable.
- **A width and space repair on the pixel grid before export.** With the rules' two-pixel
  filter radius the Zhou constraints read as met while the binary divider kept one-pixel holes
  and diagonal one-pixel nubs (four violations). The export opens the copper and the void with
  a square of the minimum width (space) and bridges corner contacts (the divider: 10 of 1280
  pixels); the result records the change and the validator compares the repaired footprint with
  the optimizer's binary design. A four-pixel filter instead (thresholds from the filter radius,
  now supported) gave smoother shapes but stalled (t ≈ 0.7 at iteration 70, against −0.002 at
  iteration 60 with two pixels).
- **Re-validation reads the exported footprint**, not the optimizer's arrays: it rasterizes the
  KiCad polygons ("inside or on" at pixel centres), recalibrates the feeds from the pad
  widths, excites every port and renormalizes to 50 Ω, once on the optimization grid (which
  must reproduce the binary design pixel for pixel) and once at half the pitch with 1.5 times
  the substrate cells. The criteria live with the presets (`yapnr.rf.cases`).
- **The antenna's band is 9.8–10.2 GHz (4 %), not the design's 9.7–10.3 GHz (6 %).** On the
  case's grid the closed-form inset patch has a −10 dB band of 4.5 % and a radiated fraction of
  0.80 at its peak; refining it for 6 % froze at t = 1.40 (η 0.56 and |S11| −5.3 dB at
  9.7 GHz), since a second resonance would have to appear from nothing. The target levels
  (−12 dB, η ≥ 0.7) and the criteria's form are unchanged; the criteria bands narrow with it
  (coarse 9.8–10.2, fine 9.85–10.15 GHz).
- **The antenna refines its seed with the conservative MMA variant and a 0.05 move** (schedule
  16, 64): plain MMA left the tuned seed on its first step. It still did not improve on the
  seed; the case is recorded as failing rather than tuned further by hand.
- **Passivity is judged at −1e-3 again** (it was −0.01 for a while). The violations came from
  de-embedding the feed's magnitude with the calibration's Im k (below), not from the
  reciprocity error the earlier note blamed.
- **Custom pads keep a rectangular anchor of the port pad's size** (KiCad 10 keeps it), so a
  one-port footprint still says how wide its feed is.

Review fixes (the physics and design reviews of the cases; [guide](rf-inverse-design.md)
"Accuracy"):

- **De-embedding shifts the phase only (Re k).** The two-plane calibration's Im k is not a loss
  measurement: +4 to −1 Np/m depending on where the planes sit relative to the source, against
  0.7–0.9 Np/m from the Poynting flux. The feed between the V/I and reference planes is about
  5 mm; neglecting its loss makes |S| about 0.01 dB low.
- **V/I plane 6h from the reference plane and 6h from the source** (17 and 33 cells on S1 at
  0.3 mm, was 9 and 17): the divider's |S21 − S12| drops from 0.009–0.013 to 0.003–0.006, for
  about a third more cells on a three-port. The calibration takes Z_c and k at least 3h from
  its source and the power factor (Poynting flux over V/I power) at the ports' own distance,
  where the excited port's incident wave reads 1.5–2 % high; the radiated fraction uses it.
  Transmissions stay about 0.1–0.2 dB low at 8–12 GHz (stated in the guide), which is
  conservative for transmission targets.
- **S = B A⁻¹** in the validation sweeps (every port excited), not b_i/a_j: the idle ports see
  incident waves of about 1 %.
- **The exported design is the best binarized design of the run** (evaluated every
  `binary_every` iterations, at every β change and at the end), not the last iterate: the
  antenna's last iterate was worse than its start and the diplexer's than its iteration 45.
- **A conservative MMA step that is not conservative after `max_inner` subproblems is
  rejected,** keeping x and the raised curvature (it used to be accepted, and the antenna's t
  rose from 0.34 to 1.07 on the first step).
- **Robust variants and a reactive interpolation are available, off by default:** eroded and
  dilated designs in the epigraph (`eta_variants`, Hammond et al. §5.3), and gray copper as an
  inductive sheet (`interpolation: reactive`, the analog of the paper's Drude–Lorentz
  interpolation with damping). The reactive sheet did not help the cases (an unfed plate for the
  antenna from a uniform start, broken lines for the diplexer).
- **Lumped resistors in specs** (`lumped`), for the Wilkinson-type combiner: the body stays
  void and carries the resistance on the copper-plane edges along its axis, its pads stay
  copper, and the footprint gets two pads per part.
- **The validator re-simulates on a third grid** (a third of the pitch, twice the substrate
  cells) for the full cases, judges the fine criteria there too and reports every check's
  trend over the three grids; resonant designs are not converged on the fine grid.
- **Radiators get a power balance check** (`validate.power_balance`): the port's net input
  power against the flux out of a box closed by the ground, the other ports' power and the
  dissipation inside, within 2 %; it validates the radiated fraction the antenna is judged by.
- **Footprints carry KiCad rule areas** for the simulated margin: no pour, vias or other
  footprints, and no tracks outside a corridor along each feed.
- **`export_ok` is `None` when the pixel check could not run** (no checkpoint).
- **The divider's transmission target is −3.4 dB (was −3.28) and it is optimized robustly**
  (the eroded design, projection threshold 0.55, in the epigraph). With the extraction
  corrected, −3.28 dB was out of reach (the ideal split is −3.01 dB and transmissions read
  0.1–0.2 dB low) and the run oscillated; without the eroded design |S11| lost 1.8 dB per
  refinement. With both it passes on all three grids. The criteria are unchanged.
- **The filter banks start from the stub seed** (`seed: stubs`), as the design review
  suggested: from the plain junction the diplexer only learned to roll off (rejection 5–12 dB).
- **Moves of 0.05 from β = 32** for the divider, the Wilkinson case and the banks: at 0.1 a
  near-binary design flipped boundary pixels back and forth (t alternating between 0.7 and 8,
  a broken arm each time). The best-design export keeps such excursions out of the footprints.
- **The antenna case is unchanged and fails; changing it is the owner's call.** On S2 the patch
  class has about 4 % of −10 dB bandwidth, the band itself, and the finer grids shift it up by
  2 %; uniform starts (resistive or reactive) and an untuned rectangle did not lead the
  optimizer to a radiator, so the export is the tuned closed-form patch, which the optimizer did
  not change. Options: a broader-band topology, a thicker or lower-εr substrate, a band or
  criteria that allow the shift, or a copper-edge correction in the solver.

Round 2, accuracy (design §21, guide "Accuracy"); each change is an option, off by default,
with the A/B measurements in the design:

- **Copper-edge correction (`solver.edge_correction`)**, the static-field method of Shorthouse
  and Railton rather than grid continuation: Hodge factors on ε and μ of the cells next to
  every copper edge (knife-edge field), the next ring of cells, and corner nodes (the field of a
  flat sector, from a spherical eigenproblem), each a multilinear function of the pixels so the
  adjoint stays exact. Lines, a stub and the patch then agree between the optimization grid and
  a third of its pitch to 0.13–0.22 % (were 1.5–4 %). Grid continuation was not built: with the
  correction it is not needed for these numbers, and export, rules and validation all assume
  one optimization pitch.
- **The corner factors are part of the correction.** First and second ring alone left the stub
  and the patch at 0.5–0.6 %; the corner factors (convex 0.62 κ_n, concave 1.48 κ_n on the cases'
  grids), derived like the edge factors and not fitted, bring them to 0.13 %.
- **The time step with the correction is bounded over a library of dense copper patterns** (times
  1.05 on the eigenvalue), not by taking every factor at its extreme (which no pattern can
  reach and would cost 11–17 % more steps). It is 0.88–0.89 of the plain step at the
  optimization pitch, 0.82–0.83 at a third; a run that diverges stops with an error.
- **Modal port source (`solver.port_source: mode`)**: the line's mode solved for the solver's own
  discretization of the feed's cross-section (block-LU inverse iteration, no new dependency),
  fitted as P0 + ω² P2 over the pulse's band. The static source's surface wave made the
  excited port's incident wave read high (a matched line's |S21| 0.09 dB below its loss, |S11|
  −40 dB); with the modal source within 0.001 dB and −63 dB. Other strips crossing the source
  plane are left out of the solve, as before.
- **Adaptive move limits (`optimizer.adaptive_move`)** instead of making the conservative
  (GCMMA) variant the default: the step test is on the epigraph value only (slack 0.05 ·
  max(1, |t|)), refusals halve the move, accepted points cost nothing extra. On the tiny spec
  plain MMA jumped to t = 17 at a 0.3 move; the adaptive move rose by at most 0.04 and reached
  the same optimum with two refusals.
- **The new options are left out of the spec hash at their defaults**, so specs and run
  directories written before them keep their hashes and resume.

Round 2, the antenna by the method (design §22, guide "(b) Antenna"; every attempt with its
start, formulation, iterations and outcome is in the design's table):

- **The antenna's copper is generated by the optimization from the feed line alone.** The case
  starts from the port's feed continued to the window's centre (`seed: star`, the
  feed-line-only start); only the port pad is fixed. The closed-form inset patch is kept as a
  labelled reference (`cases.antenna_patch_reference`, not a case), for the seed tests and as
  context for the numbers.
- **The formulation that worked is the spec's robust epigraph:** the eroded and dilated designs
  (projection thresholds 0.55 and 0.45) join the minimax with the nominal one (Hammond et al.
  §5.3; the divider uses the eroded one), with the round-2 solver (edge correction, modal
  source) and adaptive moves. From the feed line the plain epigraph grew a patch with two
  parasitic islands that was matched only 8 % above the band (|S11| −27.8 dB at 10.8 GHz) and
  then stalled: its far edge turned into a gray comb (a lossy sheet of a few hundred Ω/sq),
  whose gradient points to void while the void beyond it has almost no sensitivity under the
  log interpolation. With the eroded design in the epigraph a gray boundary is worth nothing
  (the eroded design loses it), the edges stay crisp and the radiator matched the band by
  iteration 8. With the eroded design alone the second case run met its objectives before the
  width and space repair (t −0.040) and missed after it (t +0.273): its design relied on two
  one-pixel slots and on corner contacts with the parasitic patches, which the repair closed
  and bridged. The dilated design closes and bridges them during the optimization, so the
  design must work without them; the third run's exported design met its objectives after the
  repair (t −0.139 over all three designs) and passes the criteria on all three grids.
- **The antenna's power balance tolerance is 4 % (was 2 %).** (The explanation below was not
  established; the review fixes below have the measurements.) The box closed by the ground leaves
  a window where the feed crosses it (needed to keep the feed's guided power out), and on the
  antenna's grid the window limits the balance to about 2–3.5 % for any radiator near the feed:
  the closed-form patch itself reads −1.9 % at its resonance and −3.5 % at 10.35 GHz; for the
  generated antenna a smaller window (margins 1.5 and 0.8 mm) counts the line's guided fringe
  (−4.9 and −10.5 %), a larger one (4.5 and 6 mm) misses more radiation (−3.6 and −5.4 %),
  against −2.1 % with the default. The error is negative (the box finds less power than the
  port reports), the safe side for the radiated fraction it validates (η ≥ 0.84 against a
  criterion of 0.6). Round 1's −5 to −11 % (static source) would still fail. The generated
  antenna's balance is 2.1, 2.8 and 3.4 % on the three grids: it fails the old 2 %.
- **What did not work, and stays available or recorded:** uniform transparent starts (an
  edge-hugging structure, η ≤ 0.57 gray), the reactive sheet from transparency or from the
  feed line (the gray inductive design radiates, its binarized copper does not), the radiation
  objective of Lu, Wadbro, Hassan et al. from a uniform gray sheet (a plate radiator levelling
  off at η 0.6; now `optimizer.epoch_objectives: radiation`), frequency continuation from a
  broad band (a weak broad radiator) or from a band 7.5 % below the target (centred but an
  edge-fed patch of 12 Ω; now `optimizer.epoch_frequency_scale`), a heavier weight on the
  match, and crediting dissipation as radiation (an absorber). Both new options are off by
  default and left out of the spec hash; the case uses neither.
- **The antenna's band is 9.85–10.15 GHz (3 %) at |S11| ≤ −10 dB and η ≥ 0.6 on every grid**
  (optimized for −10 dB and η ≥ 0.7; superseded by the review fixes below: 9.7–10.3 GHz at
  η ≥ 0.7). With the copper-edge correction the closed-form inset
  patch on S2 matches −10 dB over 3.4 % (10.05–10.40 GHz, η 0.88): round 1's 4 % came from the
  uncorrected copper. 3 % asks for about the bandwidth of one patch on this substrate, centred
  to ±0.2 %; it is not trivial (a mis-tuned patch misses it) and a single-layer radiator can
  reach it. The coarse and fine criteria are now the same: the grids agree to about 0.2 % (on
  lines, a stub and the patch; the generated designs, below).
- **The antenna is optimized over 9.65–10.35 GHz and judged over 9.85–10.15 GHz,** the way the
  diplexer's channels are widened by 0.2 GHz. The first case run optimized over the criteria
  band itself and met it on the optimization grid with 0.3 dB to spare at the lower edge; its
  footprint missed by 0.3, 1.6 and 2.1 dB at 9.85 GHz on the three grids (50 Ω, and the lower
  −10 dB edge moved up 0.9 and 1.3 % on the finer grids), while its −10 dB band was 7.3 % wide
  (9.87–10.62 GHz): broad enough, centred too high.
- **One-port reflections are judged at 50 Ω in the objective (`optimizer.reference_ohm`)**, as
  the validator reports them, not against the feed's Z_c: the 11-cell feed is 47.5 Ω, which
  moves a −10 dB reflection by up to 0.6 dB (−10.3 dB against Z_c read −9.7 dB at 50 Ω). For
  one port the renormalization needs no other excitation; multi-port specs keep the feeds'
  Z_c (design §5.4).
- **`OPENBLAS_NUM_THREADS=1` for every RF run** (set in the Bazel targets, documented in the
  guide). numpy's OpenBLAS evaluates the adjoint sources each step; its worker threads kept
  spinning beside torch's 4 and an adjoint run used about 7 cores and took 46.6 s instead of
  28.3 s (the antenna's grid). The earlier rounds' runs were affected the same way (they used
  `OPENBLAS_NUM_THREADS=4`, or none).

Round 2, the cases (design §23, guide "End-to-end cases"; every attempt is in the design's
table):

- **Every case runs with the round-2 solver** (`solver.edge_correction`,
  `solver.port_source: mode`; `cases.ROUND2_SOLVER`), the smoke variants too. The diplexer's
  and the divider's footprints then agree between the three grids within 0.1–2 dB on every
  check (round 1: up to 2.7 dB and 1–4.7 % in frequency).
- **Adaptive moves only once the design is nearly binary** (`optimizer.adaptive_from_beta`,
  new): plain MMA steps below that β, adaptive ones from it on. With adaptive moves (slack 0.05)
  from the start the cases crept: the divider's moves settled at 0.01–0.03 from β = 16 and its
  best design (robust binarized t 0.455 at iteration 80, where round 1's plain MMA had 0.10)
  failed the match on every grid; the non-robust diplexer's moves fell to 0.004–0.01 by
  β = 32; the combiner's to 0.008–0.012 by its sixth iteration. A full MMA step on the
  minimax often raises the maximum (one tried t 16.5 from 0.61), so the move halves more often
  than it grows. Plain MMA explores (its excursions, t up to 15, are kept out of the exports by
  the best-binarized-design export) but did not improve on the end of β = 8 in the diplexer;
  the adaptive steps after it keep what β = 8 found and polish it. The antenna keeps adaptive
  moves throughout (it passed with them).
- **The width and space repair widens a conflicting neck instead of stopping on it.** A
  one-pixel bridge between two blocks offset diagonally by a pixel is too narrow as copper and,
  removed, leaves a one-pixel gap: the opening removed it, the space pass put it back, and the
  repair stopped at that fixed point of its round. Round 1's Wilkinson footprint kept two such
  bridges (the 0.14 mm necks the polygon check reported). The repair now makes the k × k square
  through such a pixel copper (the one needing the fewest new pixels), which keeps the
  connection the design made; removing the bridge would cut it.
- **Robust variants in the epigraph for the combiner, the divider and the filter banks** (the
  dilated and eroded designs, projection thresholds 0.45 and 0.55, as the antenna). Without them the
  optimizer leaned on what the binary, repaired design does not have: the combiner on gray
  copper between its arms (a resistive sheet doing the isolation resistor's work, which the
  binarized design turned into a short: gray t 1.15, binarized t 15–20), the diplexer on
  one-pixel lines the width and space repair removed (binarized t 0.67 before the repair and
  1.52 after; channel B's rejection −17.7 → −6.8 dB). With them the diplexer's binarized design
  reached t 0.49 (0.715 without). It triples the cost per iteration; the forward runs of every
  variant are now kept for the next iteration (`Problem.fwd_cache_size`), which saves a third
  of it with adaptive moves. **From β = 16** (`optimizer.robust_from_beta`, new) in the
  combiner, the divider and the bank: at β = 8 the variants of a gray design are about as gray
  as it is (the combiner's robust run W6 still leaned on gray copper there), so they cost
  three times as much for little; binarized designs are judged with every variant throughout.
  The diplexer, run before the option existed, has them from β = 8.
- **The combiner's isolation resistor must take its share** (`Absorbed("R1", 2).at_least(0.4)`,
  a new requirement quantity: the fraction of the power incident at port 2 that the lumped
  resistor dissipates, ½ c_ω Σ σ_e V_e |Ê_e|² over its edges, through probes on them). An
  ideal Wilkinson's resistor dissipates half of what enters an output port. With gray copper
  a resistive sheet, the optimizer isolated the outputs with gray copper beside the resistor
  in every formulation tried (uniform and seeded starts, nominal and robust, plain and adaptive
  MMA; the reactive sheet disconnected the outputs instead), and the binarized design lost
  the isolation (gray t 0.39–1.15, binarized 1.3–20). The requirement makes the gray loss pay
  against the resistor's share: in its 20-iteration screen the binarized design tracked the
  gray one (t 0.86 against 0.72; W2's nominal screen: 1.12 against 15–20). It did not stop gray
  copper bridging the arms east of the resistor, which became copper shorts at β = 16 (W8).
  The share is an objective, not a criterion; the validator reports it.
- **The combiner keeps the symmetry line east of its resistor void** (`cases.isolation_keepout`:
  a `fixed` void strip from the resistor's east end to the window's east edge, as wide as the
  part's body, 0.6 mm), against W8's gray bridges there. A bound on **the lost fraction**
  instead (`Loss(2).at_most(0.08)`, new: the power leaving neither through a port nor into a
  resistor, from the port waves and the resistor's share) kept the gray design from connecting
  the resistor at all (W9: share 0.01–0.02 and t 3.9 for the first 12 iterations; connecting it
  through gray copper first adds loss). The keepout is what a Wilkinson's layout has anyway
  (the arms meet only at the input junction and through the resistor), computed from the
  part's position, and leaves the rest of the window free; `Loss` stays available to specs.
- **...and west of it, from 1.2 mm to the resistor** (`cases.arm_keepout`, a second `fixed` void
  strip as wide as the part's body, from the port pad plus one minimum width to the resistor's
  west end), and the combiner's β = 8 epoch has 35 iterations (was 25). With the east keepout
  alone (W10, validated: output match −16.2 / −15.0 / −16.4 dB, isolation −14.1 / −13.6 /
  −13.6 dB, failing the −17 and −15 dB criteria) the optimizer cut the input line along the
  axis only from 1.2 mm west of the resistor: the odd-mode path from the resistor back to the
  junction, which a Wilkinson makes a quarter wave (about 5 mm here), was about 3 mm, and the
  output match and the isolation centred above the band (−14 and −15 dB at 9 GHz, −31 and
  −18 dB at 11 GHz). An ideal single-section Wilkinson meets −25 dB at ±10 %, so the targets
  ask for the topology rather than for more iterations. With both strips the arms meet only at
  the input junction (west of 1.2 mm) and through the resistor, as in a Wilkinson's layout;
  their widths, their paths (they bow apart) and the output lines stay free. From the same
  seed W11 reached t 0.04 at the end of β = 8 (W10: 0.56) and a robust binarized t of 0.08
  at β = 16 (W10: 0.74 at best).
- **The combiner's resistor moves to 5.4–6.0 mm from port 1** (was 4.8–5.4 mm; a change of the
  spec's `lumped` part, its criteria unchanged). A Wilkinson's resistor ends its quarter-wave
  arms; the optimizer's arms run side by side as coupled lines, and the resistor terminates
  their odd mode, which is faster than the even one: its quarter wave is about 5 mm against
  4.6 mm. At 4.8–5.4 mm the coupled arms were about 65° long in the odd mode, and round 1 ended
  at −11 dB of output match and −13 dB of isolation, close to what an ideal circuit with those
  lengths gives (−12 dB).
- **The combiner starts from its feeds** (`seed: feeds`, new: each port's 50 Ω line continued to
  the window's centre line, the lines not joined; port 1's ends on the resistor's pads). From a
  uniform x = 0.3 the resistor's pads stayed unconnected (W3) or gray copper did the resistor's
  work (W1, W2); from the joined star the junction shorts the resistor on both sides, and
  cutting either side alone leaves it shorted, so the gradient does not lead to the cut (W4).
  The seed is computed from the ports and the part alone, and every pixel stays a variable.
- **The divider re-runs with the robust variants and adaptive moves from β = 16.** Round 1's
  footprint re-simulated with the round-2 solver misses the coarse match criterion (|S11|
  −16.2 dB against −17; −17.8 and −16.0 dB on the finer grids, which pass): it was tuned to the
  uncorrected copper. Round 1's formulation with the round-2 solver failed with adaptive moves
  throughout (best robust binarized t 0.455; −15.4, −13.4, −13.7 dB) and plain MMA throughout
  oscillated from β = 16 (best t 0.92 by iteration 48). With plain MMA at β = 8 and adaptive
  moves from β = 16 (V3) it passes on all three grids: |S11| −20.3, −19.2 and −19.5 dB,
  |S21| = |S31| −3.33, −3.30 and −3.31 dB (guide, "(a) Power divider").
- **The filter banks' objective bands widen each channel by 0.1 GHz** (was 0.2 GHz) against
  coarse-to-fine shifts, which the edge correction reduced to about 0.2 % on lines, a stub and
  the patch (the generated designs' are in the review fixes below); five points per
  diplexer channel and four per bank channel. The criteria are unchanged.
- **The three-channel bank keeps its spec and fails** (round 2's B1 is published as it is).
  Its best binarized design came at β = 8 (t 1.39; round 1: 2.72): every in-channel
  transmission passes on all three grids (−1.5 to −2.1 dB), but the adjacent channels leak
  (channel A at port 3 −9.7 to −10.0 dB, channel B at port 4 −10.1 to −10.5 dB against −15 and
  −12 dB) and channel A's match is −6.4 dB on the coarse grid (−8 dB). A larger window
  (24 × 24 mm, the outputs 8.1 mm apart, with the variants from β = 8, B2) did worse at β = 8
  (robust binarized t 5.3–5.6) and was stopped at its 22nd iteration under the shared Mac's
  load, so the window stays 18 × 18 mm; no criterion or target was changed. The stubs' notches
  need about 1 % of precision and a pixel of stub length is 5–9 %; past β = 8 the optimizer
  reaches that precision only with near-threshold (gray, lossy) pixels, which the binary
  design loses (design §23.3). Sub-pixel tuning of binary copper is the open problem, not the
  spec.

Round 2, review fixes (the physics and intent reviews of round 2; design §24):

- **The footprint follows the pixel boundaries.** The marching-squares contour cut every convex
  pixel corner and filled every concave one by half a pixel: the same pixels on the
  optimization grid, other copper on the validator's finer grids (+74 to +135 sub-pixels at
  half the pitch, ±70–100 at a third), and the diplexer's S21 notch moved +1.42 and +1.69 % from
  the optimization grid to half and a third of its pitch against +0.08 and +0.12 % for the same
  pixels subdivided. Every finer grid now simulates the optimizer's copper (`copper_xor` 0 in
  every `validation.json`). Simulating chamfered copper during the optimization instead was not
  done: the solver's copper, and the edge correction's static fields, are pixel unions.
- **The width and space repair widens the one-pixel necks that the pixel-exact copper shows.**
  Two two-pixel lines offset diagonally can touch along one pixel edge while every pixel is in a
  2 × 2 copper square (so the opening keeps it); the chamfers widened such a neck to 1.4 pixels
  and the polygon check passed it. The repair now widens each neck the polygon check of the
  exact copper flags, at the facing void pixel with the smaller x unless its new copper would
  come within the minimum space of another copper component (then at the other one: a first
  version joined the antenna's islands to its port pad and its matched band fell from 19 to
  4 %); the choice commutes with the cases' mirror symmetry, and ties of the conflict widening
  also go to the square nearest the centre line. Re-exported: the divider +12 / −2 pixels
  against round 2's export, the antenna +4, the bank +5 / −5, the combiner and the diplexer
  unchanged.
- **Accuracy is stated per kind of structure.** Lines, an open stub and the closed-form patch
  agree between the optimization grid and a third of its pitch to 0.13–0.22 % (§21.4); a stub
  across a two-pixel gap to 0.23 %, a stub split by a two-pixel slot to 0.31 % and a stub with
  2 × 2 holes to 0.10 %; the generated designs' resonant features, now the same copper on every
  grid, by 0.05–0.9 % (design §24.2; round 2's 0.7–2.2 % was mostly the export's chamfers). The
  filter banks' objective bands stay widened by 0.1 GHz (0.8–1.3 % of their channels' centres,
  above the measured shifts); the coarse/fine criteria split of the cases stays as it was.
- **The antenna is judged over the design's band** (9.7–10.3 GHz, 6 %) at |S11| ≤ −10 dB and
  η ≥ 0.7 at every point, the same on every grid, which is what the optimization asked for
  (9.65–10.35 GHz at 0.7); round 2's 9.85–10.15 GHz at η ≥ 0.6 asked for less than the generated
  design does (−12.2 dB and 0.80 or more over 9.7–10.3 GHz on every grid in round 2's
  validation). The report gives the −10 dB band on each grid. The re-exported antenna meets
  both on every grid (|S11| −12.2 / −11.5 / −11.3 dB, η ≥ 0.83 / 0.82 / 0.82; −10 dB band
  19 %).
- **The power balance stays at 4 % and its cause is stated as not established.** Round 2 called
  the error window-limited and its sign safe for η. A study (design §24.7) found the box's energy
  accounting exact (a box without a feed window closes to 0.04 %), so the error is the power
  entering through the feed window against the port's wave power; it does not track the
  window's size, converges with the grid (about 1 % from the optimization grid to half its
  pitch, 0.1 % from there to a third), and with the box closed at the port's V/I plane it changes
  sign across the band (−2.9 to +1.8 %), so neither η nor |S11| is shown to read low. The likely
  cause is the radiator's near field at the port's V/I samples; untested. The criterion is not
  relaxed further: over the new band the generated antenna's error at 9.7 GHz is 3.6, 4.6 and
  4.7 % on the three grids, so it fails the check on the fine and finer grids while meeting
  |S11| and η (η exceeds 0.7 by 0.12 or more, more than twice the imbalance). The owner's call:
  accept the imbalance as a port-measurement uncertainty, judge it against η's margin instead,
  or wait for a modal port extraction.
- **The time-step library holds the diagonal copper patterns** (one-pixel diagonal lines touching
  at corners, two- and three-pixel diagonal stripes, a knight's-move lattice, random diagonal
  stripes). One-pixel diagonal lines every three pixels raise λ to 1.66–1.74 times the plain
  grid's, more than the library's random patterns (1.58–1.64), which used up the 1.05 margin; a
  random search of 55,867 single and diagonal flips from that pattern found nothing larger. The
  step is 2.2 % smaller at the optimization pitch and 2.7 % at a third of it (S1: 0.863 and
  0.800 of the plain step, was 0.882 and 0.822).
- **Adaptive moves can measure their slack from the β epoch's best t**
  (`optimizer.trust_reference: best`, the slack also scaled by β_a/β). Measured from the current
  t (round 2's rule, still the default so that the published specs keep their hashes) each
  accepted step may add a slack: in the divider and the combiner 21 of 40 adaptive steps were
  accepted with t rising, and t crept from 0.037 to 0.112 in the combiner's β = 64 epoch. The
  published runs used the current t; neither rule is a descent guarantee.
- **The RF tests run on one thread each** (`YAPNR_RF_THREADS=1`, with OMP, MKL and OpenBLAS at 1)
  with the long timeout (900 s) where they took more than 40 s on one thread locally, and the two
  slowest files are split (`test_pipeline_gradient_options`, `test_tiny_design_options`): CI ran
  four RF tests side by side on a 4-vCPU runner with 4 torch threads each, and eight of them
  timed out (run 36939460480). No CI run of the new head yet (this round did not push), so CI
  is not shown to pass; locally (Bazel, one test at a time on one thread, the Mac under load)
  the 34 RF targets pass in 1373–1499 s, the slowest in 156–191 s against the 900 s timeout.
- **The diplexer and the three-channel bank are labelled "closed-form stub filter refined by
  topology optimization".** Their `seed: stubs` start puts a quarter-wave open stub per other
  channel on each arm (Hammerstad, Kirschning–Jansen); 90 % of the diplexer's seed copper is in its
  exported design and 77 % of the exported copper was seed copper (the stubs alone: 88 % kept; the
  bank: 83 % and 75 %; `seed_overlap` in `validation.json`, free pixels). Regenerating the diplexer
  from a non-circuit start was tried (D4: the plain junction, `seed: star`, robust from β = 8, plain
  MMA at β = 8, `trust_reference: best` after): after 25 iterations its rejection was 13–16 dB
  (gray; binarized t 1.09 against the stub seed's 0.49), a roll-off diplexer as in round 1, so it
  was stopped and the published diplexer stays the refined stub filter, labelled as such. The
  owner's sign-off is needed on publishing seeded filters at all.
- **The combiner's topology is the seed's and the keepouts'.** The `feeds` seed and the two void
  strips give iteration 0 the Wilkinson's input fork and its two arms to the resistor's pads;
  the optimizer chose the arms' width and path and the outputs (36 % of the exported copper is
  seed copper, free pixels). The guide says so; a uniform or star start with the keepouts was
  not tried.

The native FDTD kernel (branch `claude/rf-kernels`, merged into #29;
[solver backends](rf-solver-backends.md)); the owner reviews these with the pull request:

- **C with ctypes, bit-identical to numpy.** One C11 file on a pthread pool, built without
  floating-point contraction or fast-math, every value computed with the numpy reference's
  operations in the same order; the loader checks the arithmetic, the ABI and the sources'
  sha256 at load time. Rust, OpenCL (no float64 on Apple GPUs) and torch MPS were not chosen;
  CUDA is deferred until grids of 10 M cells and more.
- **The default backend is `auto`: native where the library loads, numpy otherwise** (the
  spec's default, `Simulation`'s and exact problems'). The numpy fallback gives the same float64
  values, so a missing library changes run time only; it is said once on stderr, and
  `YAPNR_RF_REQUIRE_NATIVE=1` turns it into an error. torch stays available by name.
- **float64 is the default precision,** and the cases' presets (full and smoke) and the tiny
  test spec take the defaults: they run native float64, no longer torch float32 (the published
  runs in `docs/rf/` were made with torch float32 and say so in their `spec.json`; specs that
  name torch keep it). Opt-in float32 is fine for the optimizer's steps (gradients within about
  1e-4 at full size), not for gradient checks or validation.
- **`bazel build //...` needs a C toolchain:** `//yapnr/rf` carries the library in its
  runfiles, so every RF test runs native by default. CI's Linux and macOS runners and the
  development Mac have one; without Bazel's native `cc_binary` (Bazel 9) the target is empty
  and everything runs numpy.
- **The wheel is per platform** (`py3-none-manylinux_2_34_x86_64`, `..._aarch64`,
  `macosx_11_0_arm64`), carrying `yapnr.rf` with the library. The image workflow builds each
  Linux wheel on its own architecture's runner and each image from its own wheel; releases
  attach both Linux wheels. glibc 2.34 is the newest symbol version the library needs.
- **`YAPNR_RF_THREADS` sets the native pool and caps torch's threads** (torch at most 4): a job
  on a C4D-16 sets 16 without editing its spec; the Bazel RF tests set 1.
- **A case's run directory keeps its own spec when it resumes.** The presets' new solver
  settings changed their hash, so `cases run` on a directory started before (every published
  run) stopped at its checkpoint. It now resumes a directory whose spec differs from the preset
  only in backend, dtype and threads with that spec (round 2's runs keep torch float32 unless
  the environment chooses), and `design` checks the checkpoint before it writes `spec.json`.
  The alternative, a spec hash without the execution settings, would have changed every
  existing hash.
- **Mode profiles are solved once per process,** keyed by the cross-section's content: the
  mode solve's numpy complex arithmetic was not reproducible to the last bit between processes
  on the development Mac (a complex multiplication rounded fused in some calls and unfused in
  others), which moved calibrations by up to 5e-11 relative; with one profile per process a
  run's problems and both sides of an identity test agree. Two processes can still differ in
  the last bits of a modal-source run (not in the steppers); real-arithmetic products in the
  solve might close that, not tried (it would move every published number in the last bits).

The gloss, dekink and corridor-coalescing pass (`PNR_GLOSS`,
[design](design/gloss.md)); owner decisions of 2026-09-30 (in Splanc) and 2026-10-02:

- **Behind `PNR_GLOSS`, off by default,** in the native loop (passes `06g-gloss` and
  `07g-gloss`) and as the ladder's opt-in `--gloss` stage. With the flag unset the pass is never
  imported or run: the routed copper and the loop's outputs are the same as without the port
  (byte-identical outputs are impossible even between two runs of `main`, because writeback gives
  tracks random uuids; the comparison is uuid-normalised). What does change: the ladder's
  `result.json` gains `copper_sha256` and per-stage CPU and its `provenance.json` a `gloss`
  block, and the plain-router code key changed once, when `native_loop` and `full_iteration`
  gained the hooks.
- **Keys keep the arms apart:** the router key carries a digest of the gloss settings (null with
  the flag unset, so flag-off key strings are unchanged), and an import whose gloss key differs
  is refused; `pnr.gloss` is part of the code key only with the flag, so a gloss edit never
  invalidates a flag-off library.
- **The router's cost, never traded:** rule R (shorter, or the same length with fewer bends) for
  dekink and gloss; corridor moves and adjacency tie-breaks may spend at most the
  `align_parallel` slack, **0.2 mm** of length per member. Dead space never buys router cost
  beyond it, and router cost never buys legality.
- **The legalizer overrides the pass:** per-transaction native checks with a cold KiCad DRC, a
  phase-end gate with bisection and revert, the loop's own gate, and later legalizers that rip up
  glossed copper like any other.
- **Functional groups with a cross-group cap:** packing at minimum pitch is unlimited within a
  functional group and capped at **10 mm** of parallel run between groups (a net in no group is
  its own group; `PNR_GLOSS_CROSS_GROUP_MM`). Groups come from a documented file
  (`PNR_GLOSS_CLASSES`) or are derived from net classes, pairs, SI intents and length-match groups
  (`PNR_GLOSS_CLASSES_FROM`); a file entry wins for its net, and a net derived into several
  groups joins the most specific one (fewest members, then the first source). A noise-budget
  model may later replace the fixed cap with per-pair allowances through one hook
  (`allowed_parallel_mm`).
- **Only `Default`-class signal nets are edited** (E4), as the regional router; admitting named
  signal classes is a later decision.
- **Default-on rule:** the pass is turned on by default only if a paired native-loop A/B, larger
  than Splanc's inconclusive three-placement run and planned for the cloud lane, shows that it
  does not cost completion. The ladder A/B (gloss after a complete route) cannot decide it; any
  regression there (gate, DRC, opens, objective) is a bug.
- **Private board data stays out:** the port's tests and examples use generated geometry and the
  public ladder boards; Splanc's groups file and replay boards are not ported.

Compact placement (`PNR_COMPACT`, shrink-to-fit `PNR_SHRINK`,
[design](design/compact-placement.md)); owner decisions of 2026-10-03:

- **Behind `PNR_COMPACT`, off by default,** in five parts that can be dropped one at a time
  (`PNR_COMPACT_<PART>=0`): spread 1.0 with clustered starts, the compact legalizer (courtyard
  gap, copper margins, finer slots, pads off the edge), offset courtyards, a compactness
  tie-break in the Monte Carlo selection and plane drops planned before routing (`DROPS`).
  With the switch unset the engine's outputs are those of the parent commit (tested); the
  ladder's `result.json` gains a `compactness` measure in every arm.
- **Completion first:** the compactness tie-break ranks after every completion key and the
  vias (before the vias only under `PNR_SHRINK`); copper clearance stays with the router and
  KiCad's DRC.
- **Default-on rule:** only if, per gloss setting, every case that passes with the switch off
  passes with it on, opens and DRC findings are no worse and the pin-1-origin header rung
  passes. `PNR_SHRINK` stays opt-in, since it changes the board outline.
- **Outcome: off by default.** The ladder, the showcases, the header rung and the nightly hard
  rungs pass under it (`08-chaser-20-plane` only once `DROPS` plans its plane drops before
  routing), but 5 of the 16 manual `09-mcu-usb-31` rung cells that pass without it fail with it
  (USB pair skew or a leg unrouted), and 3 with `COURTYARD` alone. Dense placement needs room
  reserved for pair tuning before another A/B.
- **The docs animations show compact placement and gloss** (the owner's request: "regenerate
  the animations in the docs and readme to show the new glossed results"). Compact placement won
  the ladder A/B (every ladder and showcase cell passes; placed bounding box -38 %, copper -16 %,
  vias +10 %); what keeps it off by default is the manual `09-mcu-usb-31` lane, which the
  animations do not show. So the ladder and showcase animations are rendered from one traced run
  with `--compact --gloss` (seed 0, darwin-arm64, engine `cfb7cb3`), each animation's `config`
  in the manifest says so, the pages and the README caption name both opt-in switches, and the
  CI traced runs take the same options so their trace hashes stay comparable. The renderer
  (version 3) plays the gloss stage's saved board as a before/after: the copper the stage
  replaced in red, its new copper in green, marked by geometry, not by track rows.

## Pinned versions

Update a pin together with the file that holds it, and note why here.

| Component        | Pin                        | Held in                                   |
| ---------------- | -------------------------- | ----------------------------------------- |
| Bazel            | `7.7.1`                    | `.bazelversion`                           |
| `rules_python`   | `2.0.3`                    | `MODULE.bazel`                            |
| Python           | `3.11` (hermetic)          | `MODULE.bazel`                            |
| pip hub          | `yapnr_pypi`               | `MODULE.bazel`                            |
| torch            | `>=2.2,<2.4` (lock: 2.3.1) | `requirements.in`, lock                   |
| numpy            | `>=1.26,<2` (lock: 1.26.4) | `requirements.in`, lock                   |
| pyyaml           | `>=6` (lock: 6.0.3)        | `requirements.in`, lock                   |
| Pillow           | `>=12,<13` (lock: 12.3.0)  | `requirements.in`, lock                   |
| Sphinx stack     | see below                  | `requirements.in`                         |
| mermaid (JS)     | `11.4.1`                   | `docs/_sphinx/conf.py`                    |
| prek             | `0.4.12`                   | `setup-precommit.sh`, `ci.yaml`           |
| presubmit hooks  | see below                  | `.pre-commit-config.yaml`                 |
| `setup-bazel`    | `0.15.0`                   | `.github/workflows/`                      |
| Ubuntu (images)  | `24.04`, by digest         | `docker/yapnr-kicad/Dockerfile`           |
| KiCad (images)   | `10.0.6~ubuntu24.04.1`     | `docker/yapnr-kicad/Dockerfile`, `TAG`    |
| Python (image)   | `3.11.15` (uv-managed)     | `docker/yapnr/Dockerfile`                 |
| uv (image build) | `0.12.21`, by digest       | `docker/yapnr/Dockerfile`                 |
| PBS (image)      | `20260807`, by sha256      | `docker/yapnr/Dockerfile`                 |
| Runtime locks    | from `requirements.lock`   | `docker/yapnr/runtime-*.lock`             |
| atopile          | `0.15.8`, hashed locks     | `yapnr/frontends/atopile/locks/`          |
| Python (atopile) | `3.14.7`, PBS `20260929`   | `yapnr/frontends/atopile/locks/pins.json` |
| uv (atopile)     | `0.12.21`                  | `yapnr/frontends/atopile/locks/pins.json` |
| elkjs (viewer)   | `0.9.3`, by sha256         | `MODULE.bazel`                            |
| three (viewer)   | `0.186.1`, by sha256       | `MODULE.bazel`                            |
| Image, release   | every action by commit SHA | `.github/actions/`, `image.yaml`, ...     |

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
- **Pillow below 13:** the renderer of the place-and-route animations (`pnr.animate`), a
  contributor and docs tool: it is in `requirements.in` only, not in `requirements-runtime.in`, so
  the wheel's dependencies and the image's runtime locks do not change. The bytes of an animation
  depend on the libwebp, zlib and FreeType that the Pillow wheels bundle, so the major version is
  capped and the animations' manifest records the exact version.
- **Sphinx stack:** sphinx 9.0.4, myst-parser 5.1.0, furo 2025.12.19, sphinx-copybutton 0.5.2,
  sphinx-design 0.7.0 and sphinxcontrib-mermaid 2.1.0, exactly Splanc's locked versions, so both
  sites build the same way. matplotlib is left out until a page needs generated figures.
- **Presubmit hooks:** Splanc's set and pins (black 25.1.0, isort 6.0.1, flake8 7.0.0,
  shellcheck-py v0.9.0.6, buildifier 8.2.0, prettier v3.1.0, markdownlint-cli v0.38.0,
  pre-commit-hooks v4.5.0), minus `nixpkgs-fmt`, plus the local privacy scan.
- **mermaid, prek, `setup-bazel`:** the same as Splanc.
- **atopile 0.15.8, Python 3.14.7, uv 0.12.21:** atopile is the version Splanc's boards were
  built with; it requires Python 3.14. The locks resolve as of 2026-09-20 (`exclude_newer`), the
  date that reproduces Splanc's Nix environment package for package. uv is the version the image
  build already pins; python-build-standalone 20260929 is the release that uv installs 3.14.7
  from, checked by `setup`. Regenerate with `tools/atopile/update_locks.sh`.
- **elkjs 0.9.3 and three.js 0.186.1:** the versions the Splanc viewer shipped and was tested
  with. The npm tarballs are pinned by sha256 (elkjs
  `b95b224bd1ab71fd40f6d9a6365c28d989745dff6b0d514407bc1eb68f2f561e`, three
  `8cd068708ea44f2c73c944b1cead2ba2f0d5c15c8fc194e5700f4e4f4a033fe7`), and the served files by
  `tests/unit/viewer/test_dist.py`. An upgrade changes both and gets a browser check of the
  schematic and 3D views.
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
  official `kicad/kicad` image is amd64-only (its `9.0` and `10.0` tags alike, checked on
  2026-09-29) and ships 341 MB of demos and a passwordless `sudo`; building KiCad from source costs
  about an hour per architecture and release. The PPA key is checked in
  (`docker/yapnr-kicad/kicad-ppa.asc`, fingerprint
  `FDA8 54F6 1C4D 0D95 72BB 95E5 245D 5502 FAD7 A805`, confirmed against Launchpad).
- **The base is published once per `docker/yapnr-kicad/TAG` and never overwritten,** because the
  PPA deletes superseded builds within days (Launchpad's PPA snapshot service,
  `snapshot.ppa.launchpadcontent.net`, only reaches back to 2026-09-01; `SOURCES` points at it as
  a second copy). CI refuses a change to `docker/yapnr-kicad/` that keeps a published tag, in every
  run, releases included: the base carries a hash of the directory
  (`tools/image/base_context.sh`) as the label `io.github.studio-fug.yapnr.kicad.context`, and the
  plan job compares it with the checkout. The yapnr image is built FROM the base by digest.
- **The publish order is source image, base tags, attestation,** and every step can be re-run:
  the GPL source offer (`<tag>-src`) is in the registry before the base tag it belongs to, a
  re-run accepts a base tag that already points at the same images, and a later run publishes a
  missing `-src` image.
- **The Ubuntu archive snapshot is taken inside the build,** right after `apt-get update`, with
  every package upgraded to that state, and recorded in `/etc/yapnr/ubuntu-snapshot` and
  `SOURCES` instead of a label (a label has to be known before the build starts, so it could not
  describe the installed packages exactly). The ports archive (linux/arm64) is not in the snapshot
  service; its packages are built from the same source packages, which Launchpad keeps per
  version, so `SOURCES` names both.
- **Controller Python: the patch release of Bazel's hermetic 3.11** (`3.11.15`), installed by uv
  from python-build-standalone, with the runtime locks pinned to `requirements.lock` (versions and
  hashes; `tests/unit/repo/test_images.py`). On amd64, torch is `2.3.1+cpu` from the PyTorch CPU
  index; PyPI's x86_64 wheel pulls in the CUDA stack. uv resolves both locks from any host.
- **Measured on linux/arm64 (2026-09-29):** the base is 1.6 GB unpacked and 390 MB compressed (the
  KiCad install adds 1.5 GB to Ubuntu's 0.11 GB); the yapnr layers add about 0.67 GB (CPython
  0.12 GB, the runtime 0.55 GB with byte-compiled sources), 2.3 GB unpacked and 570 MB compressed
  in all. (`docker image ls` with the containerd image store shows 1.98 GB and 2.83 GB: unpacked
  plus compressed.) The KiCad install takes about a minute on a native arm64 host.
- **Every action in the image and release workflows is pinned by commit SHA** (with its version
  in a comment), including `actions/checkout`, the artifact actions and `setup-bazel`: those jobs
  build what is published, or can write packages and attestations. Dependabot keeps the pins
  current (`.github/dependabot.yml`). `ci.yaml` and `macos.yaml` stay on major tags.
- **Notices for bundled native code.** CPython from python-build-standalone ships only its own
  license in the install-only archive uv uses, so the build copies the license texts of the
  libraries it bundles from the same release's full archive (pinned by sha256). The numpy and
  torch wheels bundle OpenBLAS, GCC runtime libraries, the Arm Compute Library (arm64) and
  statically linked Intel oneMKL (amd64), not all of them with license texts or exact sources in
  the wheels' notices; `docker/yapnr/native-libraries-<arch>.txt` names each with its source (the
  GCC runtime libraries by GNU build ID), and `third_party/image-licenses/` holds the missing
  texts.

### The requirements lock

`requirements.lock` is generated on the development Mac (darwin-arm64) with the hermetic Python
3.11 toolchain: `bazel run //:requirements.update`. The resolution is also valid on linux-aarch64,
which is why the required `test` job runs on `ubuntu-24.04-arm`. It is **not** valid on
linux-x86_64: the x86_64 torch 2.3.1 wheel needs CUDA libraries that the lock does not list. PR6a
adds a separate x86_64 lock with the CPU torch index (`requirements_by_platform`).

`//:requirements.test` re-resolves and diffs the lock. It needs network access, so it is tagged
`manual` and runs in exactly one CI job (`test`).
