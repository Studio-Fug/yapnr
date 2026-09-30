#!/usr/bin/env bash
# Build atopile's wheel from its sdist, for a platform PyPI has no wheel for (linux-aarch64).
#
#   tools/atopile/build_wheel.sh <out-dir>
#   yapnr atopile setup --wheel <out-dir>/atopile-0.15.8-*.whl
#
# Everything is pinned (yapnr/frontends/atopile/locks/pins.json):
#   - the sdist, by sha256;
#   - CPython, the python-build-standalone release uv installs (checked);
#   - the build dependencies, from build-requirements-linux-aarch64.lock with --require-hashes
#     (hatchling, hatch-vcs, nanobind, ziglang 0.16.0, cmake, ninja);
#   - atopile's fork of scikit-build-core, which its pyproject.toml names by git *branch*, by
#     commit (checked after the checkout), installed with --no-deps.
# The build runs with --no-build-isolation, so nothing else is resolved or downloaded. The
# wheel's sha256 is printed; a later `setup` installs it with --no-deps next to the hashed lock.
#
# Needs uv (pins.json's version), git, curl, python3, and a C toolchain for the sdists uv builds
# while installing the build dependencies. Runs niced; JOBS limits the native build's parallelism.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <out-dir>" >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCKS="${repo}/yapnr/frontends/atopile/locks"
mkdir -p "$1"
OUT="$(cd "$1" && pwd)"

pin() {
    python3 -c "import json,sys; d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split('.'): d=d[k]
print(d)" "${LOCKS}/pins.json" "$1"
}

sha256() {
    python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"
}

UV="${UV:-uv}"
want="$(pin uv)"
have="$("${UV}" --version | awk '{print $2}')"
if [ "${have}" != "${want}" ]; then
    echo "error: uv ${have} found, pins.json wants ${want}" >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
export UV_PYTHON_INSTALL_DIR="${work}/python" UV_PYTHON_PREFERENCE=only-managed UV_NO_CONFIG=1
export UV_CACHE_DIR="${UV_CACHE_DIR:-${work}/uv-cache}"
# aarch64 under Apple's virtualization: OpenSSL's ARMv8 capability probe raises SIGILL in the
# `cryptography` wheel unless it is disabled. A no-op elsewhere.
export OPENSSL_armcap=0

python_version="$(pin python)"
"${UV}" python install --no-bin "${python_version}"
build="$(cat "${UV_PYTHON_INSTALL_DIR}/cpython-${python_version}-"*/BUILD)"
if [ "${build}" != "$(pin pbs_release)" ]; then
    echo "error: python-build-standalone ${build}, pins.json wants $(pin pbs_release)" >&2
    exit 1
fi

venv="${work}/build-venv"
"${UV}" venv --python "${python_version}" "${venv}"
"${UV}" pip install --python "${venv}/bin/python" --require-hashes --no-deps \
    --requirements "${LOCKS}/build-requirements-linux-aarch64.lock"

commit="$(pin scikit_build_core.commit)"
git clone --quiet --no-checkout "$(pin scikit_build_core.repository)" "${work}/skbc"
git -C "${work}/skbc" checkout --quiet --detach "${commit}"
if [ "$(git -C "${work}/skbc" rev-parse HEAD)" != "${commit}" ]; then
    echo "error: scikit-build-core checkout is not ${commit}" >&2
    exit 1
fi
"${UV}" pip install --python "${venv}/bin/python" --no-deps --no-build-isolation "${work}/skbc"

version="$(pin atopile)"
sdist="${work}/atopile-${version}.tar.gz"
curl -fsSL --retry 3 -o "${sdist}" "$(pin atopile_sdist.url)"
if [ "$(sha256 "${sdist}")" != "$(pin atopile_sdist.sha256)" ]; then
    echo "error: ${sdist} does not match the pinned sha256" >&2
    exit 1
fi
tar -xzf "${sdist}" -C "${work}"

# The sdist carries its version in PKG-INFO; hatch-vcs would look for a git checkout.
export SETUPTOOLS_SCM_PRETEND_VERSION="${version}"
export CMAKE_BUILD_PARALLEL_LEVEL="${JOBS:-2}"
PATH="${venv}/bin:${PATH}" nice -n 10 "${UV}" build --wheel --no-build-isolation \
    --python "${venv}/bin/python" --out-dir "${OUT}" "${work}/atopile-${version}"

for wheel in "${OUT}"/atopile-"${version}"-*.whl; do
    echo "$(sha256 "${wheel}")  ${wheel}"
done
