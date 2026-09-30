# Contributing to yapnr

## Outside contributions are not accepted yet

yapnr is being migrated out of the Splanc repository and is maintained by its owner (with
automated agents working on the owner's behalf). **Pull requests from outside contributors are not
accepted at this time**, and will be closed without review. Bug reports and questions are welcome
as [GitHub issues](https://github.com/Studio-Fug/yapnr/issues).

This policy will be revisited together with the inbound license terms (for example a contributor
license agreement or a Developer Certificate of Origin sign-off) once the migration is done.

## License

yapnr is licensed under the GNU Affero General Public License, version 3 or (at your option) any
later version (`AGPL-3.0-or-later`); see [LICENSE](LICENSE). Everything committed to this
repository is licensed under the same terms (inbound = outbound).

## How changes are made

- Every change is a pull request against `main`. PRs are squash-merged; the only exceptions are the
  history-import PRs, which are merged with a merge commit so the imported history survives.
- Commit and PR titles follow `<Area>: <summary> (#N)`, where `#N` is a GitHub issue. The body
  explains why, and carries measured results for any engine change.
- New engine behaviour lands behind a default-off flag with an A/B result (see
  [AGENTS.md](AGENTS.md)).
- Mechanical commits (formatting, renames, moves) stay separate from behaviour changes and are
  listed in `.git-blame-ignore-revs`.
- Before pushing: `prek run --all-files` and `bazel test //...` (see
  [DEVELOPERS.md](DEVELOPERS.md)). CI must be green: `lint`, `test` and `docs` are required.
- Update `WORKLOG.md` and the documentation pages the change touches.

## Commit identity and privacy

This repository is public. Commits must use a GitHub noreply address
(`<id>+<user>@users.noreply.github.com`) as both author and committer; CI rejects any other
address. Never commit machine paths, host names, network addresses, personal e-mail addresses,
credentials, logs or conversation transcripts. The privacy scan (`tools/privacy_scan.py`) runs as a
pre-commit hook and as a Bazel test.

## Reporting security issues

Please do not open a public issue for a security problem; use GitHub's private vulnerability
reporting on the repository instead.
