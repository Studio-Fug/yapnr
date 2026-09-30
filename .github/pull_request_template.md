## Summary

<!-- What changes and why. Link issues as #N (GitHub issues; no tracker keys). -->

## Testing

<!-- Commands run and results. Name the tier: unit / kicad / regression / e2e. -->

- [ ] `bazel test //...` (tier and config: ...)
- [ ] `prek run --all-files`

## Checklist

- [ ] New engine behaviour is behind a default-off flag, with a measured A/B result in the commit
      body (or: no engine behaviour change)
- [ ] Privacy: no machine paths, host names, addresses, personal e-mail or credentials
      (`tools/privacy_scan.py`); commits use a GitHub noreply identity
- [ ] Docs and `WORKLOG.md` updated where this PR changes behaviour, layout or status
- [ ] Mechanical-only changes (format, rename, move) are a PR of their own; after it is merged, its
      commit on `main` goes into `.git-blame-ignore-revs` in a follow-up (or: none)
