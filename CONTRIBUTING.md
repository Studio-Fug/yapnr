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
- Mechanical changes (formatting, renames, moves) stay separate from behaviour changes, as a PR
  of their own. Once it is merged, a follow-up adds its commit on `main` to
  `.git-blame-ignore-revs` (squash merging gives it a new hash).
- Before pushing: `prek run --all-files` and `bazel test //...` (see
  [DEVELOPERS.md](DEVELOPERS.md)). CI must be green: `lint`, `test` and `docs` are required.
- Update `WORKLOG.md` and the documentation pages the change touches.

## Commit identity and privacy

This repository is public. Commits use the owner's public commit address, listed in
`tools/privacy/allowed_identities.txt`, as both author and committer: the owner's commits under the
owner's name, agent commits as `Claude Agent`. GitHub accepts only an address verified on the
account as the author of a merge made on the website, which rules out a noreply address there.
GitHub noreply addresses (`<id>+<user>@users.noreply.github.com`) remain accepted. CI checks every
new commit with `tools/privacy_scan.py --identities` and rejects any other address, including
git's guessed `user@host` identity and an empty one; the only other exception is
`noreply@github.com`, the committer GitHub itself uses for merges made on the website. Adding an
address to the allowlist is an owner decision ([docs/decisions.md](docs/decisions.md)).

Never commit machine paths, host names, network addresses, personal e-mail addresses, credentials,
logs or conversation transcripts. The allowlisted commit addresses are no exception in file
contents: only the allowlist file may name them. The privacy scan (`tools/privacy_scan.py`) runs
as a pre-commit hook, as a Bazel test over the whole tree, and in CI over the messages and patches
of every new commit (where the allowlisted commit addresses pass, since every commit header
carries them).

## Reporting security issues

Please do not open a public issue for a security problem; use GitHub's private vulnerability
reporting on the repository instead.
