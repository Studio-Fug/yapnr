# Worklog

A short, live status board: rewritten at the end of each session, not appended to. History lives in
git and in the pull requests.

Last updated: 2026-09-29 (PR0).

## In progress

- **PR0, bootstrap** (branch `claude/pr0-bootstrap`): Bazel skeleton with the hermetic Python 3.11
  toolchain and the `yapnr_pypi` lock, presubmit hooks with the privacy scan, CI (lint, test,
  docs, gated Pages deploy), the Sphinx docs skeleton, the CLI stub (`--version`, `doctor`) and
  repo-check tests.

## Next

1. Push PR0 and get CI green; then set the repository variable `YAPNR_PAGES_ENABLED=true` and
   point Pages at the `gh-pages` branch root.
2. Configure repository settings: squash merge by default; a ruleset on `main` requiring `lint`,
   `test` and `docs`; private vulnerability reporting on (CONTRIBUTING.md points to it). Squash
   merges must be authored with a noreply address (GitHub's "keep my email addresses private"),
   or the identity check fails on `main`.
3. PR1: import the engine with history from Splanc (`git filter-repo` on a scratch clone, privacy
   scan over the whole filtered history before any push). See the
   [migration plan](docs/migration-plan.md#8-pr-sequence).

## Blockers

- None.

## Do not retry

- Resolving `requirements.lock` on linux-x86_64 with the default PyPI index: the torch wheel there
  needs CUDA libraries the lock does not carry. Resolve on darwin-arm64 or linux-aarch64 (PR6a
  adds a separate x86_64 lock).
