#!/bin/bash

# Setup script for the presubmit lint hooks (see .pre-commit-config.yaml).
#
# The lint gate is run by prek, a single-binary, drop-in reimplementation of
# pre-commit that reads the same .pre-commit-config.yaml. CI runs the same
# version (.github/workflows/ci.yaml), so lints cannot pass in one place and
# fail in the other. Once installed, the hooks run on every `git commit`.
#
# prek is installed in isolation, never into a system or user site-packages:
# with uv or pipx when available, otherwise into a private virtualenv under
# .venv/prek (gitignored).
set -euo pipefail

PREK_VERSION=0.4.12
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_ROOT}/.venv/prek"

if command -v prek &>/dev/null; then
    PREK="prek"
    echo "prek already installed: $(prek --version)"
elif [ -x "${VENV_DIR}/bin/prek" ]; then
    PREK="${VENV_DIR}/bin/prek"
    echo "prek already installed: $("${PREK}" --version)"
else
    echo "Installing prek ${PREK_VERSION}..."
    if command -v uv &>/dev/null; then
        uv tool install "prek==${PREK_VERSION}"
        PREK="prek"
    elif command -v pipx &>/dev/null; then
        pipx install "prek==${PREK_VERSION}"
        PREK="prek"
    elif command -v python3 &>/dev/null; then
        python3 -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/python" -m pip install --quiet "prek==${PREK_VERSION}"
        PREK="${VENV_DIR}/bin/prek"
    else
        echo "Error: need one of uv, pipx or python3 to install prek."
        echo "See https://github.com/j178/prek for other install methods."
        exit 1
    fi
fi

cd "${REPO_ROOT}"

echo "Installing git hooks..."
"${PREK}" install --prepare-hooks

echo "Running lints on all files..."
"${PREK}" run --all-files

echo "Presubmit setup complete. Hooks run on every commit;"
echo "run manually with: ${PREK} run --all-files"
