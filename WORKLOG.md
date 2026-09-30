# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-30 (PR3a: code keys and engine format).

## In progress

- **PR3a** (branch `claude/pr3a-codekey-format`, not pushed): code key scheme 2 (routing-feedback
  code keys hash each module's canonical syntax tree, so formatting does not change them; legacy
  records are still read), then black and isort over every Python file under `hardware/` as two
  mechanical commits, five source-text tests made layout-independent, and the lint config:
  `hardware/` out of the global exclude, a per-file flake8 baseline in `.flake8` (479 findings in
  157 files, none fixed by hand), per-hook excludes for the non-Python files a hook would rewrite.
  Proof and reasons: [docs/decisions.md](docs/decisions.md) ("Choices made in PR3a"). Engine
  hygiene (#8) is merged; close #6 if it is still open (#8 re-enabled `orientation_test`).

## Next

1. Owner: review and push PR3a and open its pull request; **merge it with a merge commit**, not a
   squash, so the two format commits keep their hashes. After the merge, a follow-up commit adds
   both format commits (`Format: black ...` and `Format: isort ...`, hashes as on `main`) to
   `.git-blame-ignore-revs`. Before importing trials made by a yapnr checkout from before the
   format (legacy code keys, no `code_key_scheme`), keep a frozen copy of that pre-format tree
   (they re-key from it) or import with `--import-code-mismatch rebase` or `warn`; the default
   `error` refuses them. Splanc's frozen snapshot trees are not affected.
2. Owner: A/B Electrical221's two flags on the hierarchical engine before turning them on.
   Before the next experiment runs this engine, one rung-1 evaluation with it (`board.Delete`
   everywhere) when the Mac is free: the replay covered 5 of the 27 changed sites, and a full
   evaluation's nested workers exceed the two-KiCad-process budget kept while H7 runs.
3. Owner: push Splanc's `splanc-mini`, so the 9 newest `Imported-From` links of PR1 resolve (see the
   manifest).
4. PR6a, then PR3b onwards: wire the 68 unwired engine test files with the glob macro (the two
   hygiene tests are wired) and replace the Splanc defaults and fixtures (manifest, "Known
   leftovers for PR3"). The KiCad-dependent tests run under the headless KiCad Python only
   (DEVELOPERS.md). PR3a landed before the KiCad lane: its format is checked by syntax-tree
   equivalence and fresh-interpreter imports, not by a KiCad-side test run. PR3b burns down the
   `.flake8` baseline and fixes the `via_coalesce` F821 with a test of its own.
5. PR-R is merged (#2). Its first `main` build pushes `yapnr-kicad:10.0.6-1-src`, then
   `yapnr-kicad:10.0.6-1` (with `10.0.6` and `10.0`), then `yapnr:edge`. Then make both GHCR
   packages **public** (package settings > Change visibility; irreversible, owner-approved). The
   organization must allow public packages first (Organization settings > Packages > Package
   creation: Public), or the option is missing. Check that an anonymous
   `docker pull ghcr.io/studio-fug/yapnr:edge` (and `yapnr-kicad:10.0.6-1-src` on arm64) works and
   that `gh attestation verify oci://ghcr.io/studio-fug/yapnr:edge -R Studio-Fug/yapnr` passes.
6. Create the release-note labels (`tools/release/create_labels.sh`), label open pull requests,
   and run the release dry run once (`gh workflow run release.yaml --ref main`); read the notes
   preview in its summary.
7. Uncomment the container and release badges in `README.md` once the image is public and v0.1.0
   exists; decide on immutable releases (docs/releases.md).
8. PR6a: the KiCad-side workers run under `/usr/bin/python3` (3.12), which cannot import yapnr
   (installed in the 3.11 venv only). Give them an import path with yapnr's pure-Python modules
   and none of the venv's compiled packages, and add a smoke check for it.
9. First release tag `v0.1.0` (owner) once the engine runs end to end inside the published image on
   both architectures (the PR6b example), after the `Image` run of that commit on `main` is green;
   make the `image` check required then.
10. Rebuild the KiCad base monthly (bump `docker/yapnr-kicad/TAG` to the next `-N`), or with the
    Dependabot `ubuntu` digest update (docs/releases.md, "Maintaining the images").

## Blockers

- None.

## Do not retry

- Resolving `requirements.lock` on linux-x86_64 with the default PyPI index: the torch wheel there
  needs CUDA libraries the lock does not carry. Resolve on darwin-arm64 or linux-aarch64 (PR6a
  adds a separate x86_64 lock).
- Changing imported code or adding allow markers so that the privacy scan passes: the owner chose
  to refine the scan's e-mail rule instead (`docs/decisions.md`).
- Running `git filter-repo` (or `gc`, or a branch switch) in the Splanc checkout: experiments run
  from it. Filter a scratch clone and remove its `origin` remote first (migration plan §7.5).
- Scrubbing imported machine paths in a later commit: the earlier commits would still publish them.
  Scrub each snapshot when it is staged, before its commit (PR2).
- Reproducing the `board.Remove` teardown SIGSEGV on synthetic boards: `Remove` and `Delete` both
  exit 0 there (zones, footprints, tracks, vias, any free order); the evidence is src12i's run.
  On H7's real boards `Remove` did not crash either (it leaks: SWIG's "no destructor found").
- Using SWIG's "memory leak ... no destructor found" messages in H7 logs to find the `Remove`
  sites a run reached: only some item types print it (shove's messages are `SHAPE_SEGMENT`,
  not `Remove`). Trace `BOARD.Delete`/`BOARD.Remove` calls instead.
- Making placement bitwise identical across macOS and Linux (thread count, deterministic
  algorithms, float64 `exp`/`log`): the thread count is already 1, deterministic mode changes
  nothing, and Adam's `addcmul` also rounds differently between the torch builds (#6).
- Comparing one seed's HPWL between placer variants, or across platforms: the per-seed spread
  (about 100 mm, 5 %) exceeds most effects; compare means over seeds on one platform (#6).
