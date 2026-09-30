#!/usr/bin/env bash
# Regenerate the container image's runtime locks from requirements-runtime.in:
#
#   docker/yapnr/runtime-arm64.lock  linux/arm64: torch from PyPI (its aarch64 wheel is CPU-only)
#   docker/yapnr/runtime-amd64.lock  linux/amd64: torch 2.3.1+cpu from the PyTorch CPU index
#                                    (PyPI's x86_64 wheel pulls in the CUDA stack)
#
# Both are constrained to requirements.lock, so the image installs exactly the
# versions `bazel test` runs, and are fully hashed. uv resolves both from any
# host (--python-platform). Run this after `bazel run //:requirements.update`
# changes a runtime pin, and commit the result; tests/unit/repo/test_images.py
# fails while the locks and requirements.lock disagree.
#
# The image installs the amd64 lock with `uv pip install --torch-backend=cpu`
# (docker/yapnr/Dockerfile), which routes torch to the PyTorch CPU index.
#
# Needs uv (https://docs.astral.sh/uv/); install it with pipx or into a private
# virtualenv, never into the system Python.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv not found on PATH (see https://docs.astral.sh/uv/)" >&2
    exit 1
fi

compile() {
    local arch="$1" platform="$2"
    shift 2
    uv pip compile requirements-runtime.in \
        --constraint requirements.lock \
        --python-version 3.11 \
        --python-platform "${platform}" \
        --generate-hashes \
        --no-strip-extras \
        --custom-compile-command "tools/image/update_runtime_locks.sh" \
        --output-file "docker/yapnr/runtime-${arch}.lock" \
        --quiet \
        "$@"
    echo "wrote docker/yapnr/runtime-${arch}.lock"
}

compile arm64 aarch64-manylinux_2_28
compile amd64 x86_64-manylinux_2_28 --torch-backend cpu
