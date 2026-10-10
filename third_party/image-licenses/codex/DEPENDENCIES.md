# Separate native tools

- ripgrep: MIT OR Unlicense; source <https://github.com/BurntSushi/ripgrep>.
- bubblewrap: LGPL-2.0-or-later; unmodified binary from the pinned Codex archive;
  matching vendored source and COPYING at
  <https://github.com/openai/codex/tree/979011409de0a60b52f179721948e65531d26144/codex-rs/vendor/bubblewrap>.
  It is a separate executable, replaceable in /opt/codex/codex-resources/bwrap.
- Codex embedded dependency source pins: Cargo.lock and third_party at
  <https://github.com/openai/codex/tree/979011409de0a60b52f179721948e65531d26144>.
