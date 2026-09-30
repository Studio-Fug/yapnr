# Rules for agents

These rules apply to every automated agent (and every human) working in this repository. They
carry over the rules that governed the place-and-route work in Splanc. When a rule and a request
conflict, stop and ask the owner.

## Engineering rules

- **Selection is mechanical.** Candidates, seeds and branches are chosen by the Monte Carlo and
  successive-halving machinery, never by hand.
- **No model-driven hand routing.** Agents change the engine, not individual boards.
- **Nothing in the loop may be GUI-bound.** No `wx.App`, no KiCad GUI, and no `kicad-cli` from the
  stock macOS application bundle (every call registers a Dock icon). Use the headless copy described
  in [DEVELOPERS.md](DEVELOPERS.md#kicad). Never start `/Applications/KiCad/...` binaries directly.
- **Every worker is time-bounded.** A subprocess without a timeout is a bug.
- **Native KiCad DRC is the judge.** Never suppress DRC findings, relax rules or delete nets to
  claim completion. Electrical contracts are never relaxed without the designer.
- **New engine behaviour lands behind a default-off flag,** with a measured A/B result in the commit
  body.

## Repository rules

- **Public repository.** No machine paths, host names, network addresses, personal e-mail
  addresses, credentials, conversation logs or run logs. `tools/privacy_scan.py` runs as a
  pre-commit hook and as a Bazel test; its findings block the change.
- **Commit identity:** the owner's public commit address, listed in
  `tools/privacy/allowed_identities.txt` (or a GitHub noreply address). Agents commit as
  `Claude Agent` with that address; never change the configured git identity otherwise, and never
  write the address into files other than the allowlist. Agent commits end with the trailers the
  session provides (for example `Co-Authored-By:`).
- **Issues:** GitHub issues, referenced as `#N`.
- **Keep the WORKLOG convention.** `WORKLOG.md` is a short status board (in progress, next,
  blockers, do-not-retry), rewritten at the end of each session, not a diary.
- **Run the checks you touch:** `bazel test //...` (with `--config=lowmem` on a shared machine) and
  `prek run --all-files`.
- Do not push, merge, publish or open pull requests unless the owner asked for it.

## Shared machines

The development Mac also runs long place-and-route experiments. On it:

- Check for other agents' activity (processes you did not start, new handoff entries) before long
  operations, and report it rather than competing.
- Never stop, signal or `renice` experiment processes, and never write into their directories.
- Run Bazel niced with `--config=lowmem`, one Bazel server at a time, with the output base on the
  internal disk (see [DEVELOPERS.md](DEVELOPERS.md#bazel)). Check free disk space first.
- Install tools into a private virtualenv or with `uv`/`pipx`; never into the system Python.
