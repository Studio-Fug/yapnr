#!/usr/bin/env bash
# Regenerate the atopile environment's hashed locks (docs/frontends/atopile.md, "The lock"):
#
#   yapnr/frontends/atopile/locks/requirements-<platform>.lock        for `yapnr atopile setup`
#   yapnr/frontends/atopile/locks/build-requirements-linux-aarch64.lock for build_wheel.sh
#
# from requirements.in and build-requirements.in, one lock per platform, all fully hashed and
# resolved as of pins.json's exclude_newer date, so a re-run gives the same files. uv resolves
# every platform from any host (--python-platform).
#
# linux-aarch64 has no atopile wheel on PyPI: its lock leaves atopile out (--no-emit-package) and
# build_wheel.sh builds one from the sha256-pinned sdist. zstd and watchdog have no wheel there
# either; uv installs them from their hashed sdists with the pinned build constraints.
#
# Needs uv, at the version in pins.json (install it with pipx or into a private virtualenv, never
# into the system Python). `--check` regenerates into a temporary directory and fails if any lock
# differs (the freshness check; it needs the network).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
LOCKS=yapnr/frontends/atopile/locks

pin() { python3 -c "import json,sys; print(json.load(open('${LOCKS}/pins.json'))[sys.argv[1]])" "$1"; }

UV="${UV:-uv}"
if ! command -v "${UV}" >/dev/null 2>&1; then
    echo "error: uv not found (set UV=/path/to/uv)" >&2
    exit 1
fi
want="$(pin uv)"
have="$("${UV}" --version | awk '{print $2}')"
if [ "${have}" != "${want}" ]; then
    echo "error: uv ${have} found, pins.json wants ${want}" >&2
    exit 1
fi

out="${LOCKS}"
if [ "${1:-}" = "--check" ]; then
    out="$(mktemp -d)"
    trap 'rm -rf "${out}"' EXIT
fi

compile() {
    local input="$1" platform="$2" target="$3"
    shift 3
    "${UV}" pip compile "${LOCKS}/${input}" \
        --python-version "$(pin python | cut -d. -f1-2)" \
        --python-platform "${target}" \
        --exclude-newer "$(pin exclude_newer)" \
        --generate-hashes \
        --no-strip-extras \
        --no-header \
        --quiet \
        --output-file "${out}/${platform}" \
        "$@"
}

compile requirements.in requirements-darwin-arm64.lock aarch64-apple-darwin
compile requirements.in requirements-darwin-x86_64.lock x86_64-apple-darwin
compile requirements.in requirements-linux-x86_64.lock x86_64-manylinux_2_28
compile requirements.in requirements-linux-aarch64.lock aarch64-manylinux_2_28 \
    --no-emit-package atopile
compile build-requirements.in build-requirements-linux-aarch64.lock aarch64-manylinux_2_28

if [ "${out}" != "${LOCKS}" ]; then
    status=0
    for lock in "${out}"/*.lock; do
        if ! diff -u "${LOCKS}/$(basename "${lock}")" "${lock}"; then
            status=1
        fi
    done
    if [ "${status}" -ne 0 ]; then
        echo "error: the atopile locks are stale; run tools/atopile/update_locks.sh" >&2
    fi
    exit "${status}"
fi
echo "wrote ${LOCKS}/*.lock"
