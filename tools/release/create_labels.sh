#!/usr/bin/env bash
# Create (or update) the pull request labels that group the release notes
# (.github/release.yml; docs/releases.md). Idempotent. Run once per repository, by the owner
# or on the owner's behalf:
#
#   tools/release/create_labels.sh [OWNER/REPO]     # default: the current gh repository
set -euo pipefail

repo=()
if [ "$#" -gt 0 ]; then repo=(--repo "$1"); fi

label() {
    gh label create "$1" --color "$2" --description "$3" --force ${repo[@]+"${repo[@]}"}
}

# One area label per pull request.
label engine 024823 "Placement, routing and evaluation engine"
label viewer 1d76db "The web viewer"
label cli 5319e7 "Command line, projects and the store"
label bazel 43a047 "Bazel build and the downstream ruleset"
label release ba9015 "Container images, releases and versioning"
label docs 0075ca "Documentation"
label ci ededed "CI workflows and repository checks"
label dependencies 0366d6 "Dependency and pin updates"
# Added when they apply; they decide the version bump (docs/releases.md).
label breaking b60205 "Breaks the public surface: CLI, manifest, store, bundles, rules, image"
label results-change d93f0b "Same inputs, seed and flags give a different board"
# Leaves the pull request out of the release notes.
label skip-changelog ffffff "Not listed in the release notes"
