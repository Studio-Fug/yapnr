#!/bin/bash

# Setup script for the presubmit lint hooks (see .pre-commit-config.yaml).
#
# The lint gate is run by prek, a single-binary, drop-in reimplementation of
# pre-commit that reads the same .pre-commit-config.yaml. CI runs the same
# version (.github/workflows/ci.yaml), so lints cannot pass in one place and
# fail in the other. Once installed, the hooks run on every `git commit`.
#
# prek is installed in isolation, never into a system or user site-packages:
# a prek already on PATH is used only if it is the pinned version; otherwise the
# pinned version goes into a private virtualenv under .venv/prek (gitignored),
# created with uv when available, else with python3 -m venv.
set -euo pipefail

PREK_VERSION=0.4.12
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_ROOT}/.venv/prek"

# `prek --version` prints "prek X.Y.Z (commit date)".
prek_version() {
    "$1" --version 2>/dev/null | awk '{print $2}'
}

PREK=""
if command -v prek &>/dev/null; then
    found="$(prek_version prek)"
    if [ "${found}" = "${PREK_VERSION}" ]; then
        PREK="$(command -v prek)"
    else
        echo "Warning: the prek on PATH is ${found:-of unknown version}; CI runs" \
            "${PREK_VERSION}. Using a private ${PREK_VERSION} under .venv/prek instead."
    fi
fi
if [ -z "${PREK}" ] && [ -x "${VENV_DIR}/bin/prek" ] \
    && [ "$(prek_version "${VENV_DIR}/bin/prek")" = "${PREK_VERSION}" ]; then
    PREK="${VENV_DIR}/bin/prek"
fi
if [ -z "${PREK}" ]; then
    echo "Installing prek ${PREK_VERSION} into .venv/prek..."
    rm -rf "${VENV_DIR}"
    if command -v uv &>/dev/null; then
        uv venv --quiet "${VENV_DIR}"
        uv pip install --quiet --python "${VENV_DIR}/bin/python" "prek==${PREK_VERSION}"
    elif command -v python3 &>/dev/null; then
        python3 -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check \
            "prek==${PREK_VERSION}"
    else
        echo "Error: need uv or python3 to install prek."
        echo "See https://github.com/j178/prek for other install methods."
        exit 1
    fi
    PREK="${VENV_DIR}/bin/prek"
fi
echo "Using ${PREK} ($("${PREK}" --version))"

cd "${REPO_ROOT}"

echo "Installing git hooks..."
"${PREK}" install --prepare-hooks

echo "Running lints on all files..."
"${PREK}" run --all-files

echo "Presubmit setup complete. Hooks run on every commit;"
echo "run manually with: ${PREK} run --all-files"
