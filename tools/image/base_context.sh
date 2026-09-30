#!/usr/bin/env bash
# Print the identity of docker/yapnr-kicad at a commit: a hash over the paths, modes and blob ids
# of every file in it except Markdown. The base image carries it as the label
# io.github.studio-fug.yapnr.kicad.context, and .github/workflows/image.yaml compares the label of
# the published base with this value, so that a changed directory is never shipped under an old
# docker/yapnr-kicad/TAG, whatever triggered the run (docs/containers.md).
#
#   tools/image/base_context.sh [COMMIT]    # default HEAD
#
# A working tree whose docker/yapnr-kicad differs from COMMIT gets a "-dirty" suffix (local
# builds only; CI builds clean checkouts).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
commit="${1:-HEAD}"

context="$(git ls-tree -r "${commit}" -- docker/yapnr-kicad | grep -vE '\.md$' | git hash-object --stdin)"
if [ "$#" -eq 0 ] && [ -n "$(git status --porcelain -- docker/yapnr-kicad)" ]; then
    context="${context}-dirty"
fi
echo "${context}"
