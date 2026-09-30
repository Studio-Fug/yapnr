#!/usr/bin/env bash
# Build the container images for this machine's architecture, load them into Docker and
# smoke-test them (docs/containers.md, "Building the images locally").
#
#   tools/image/build_local.sh              # yapnr-kicad:local, then yapnr:local on top of it
#   tools/image/build_local.sh --base REF   # yapnr:local on an existing base, e.g.
#                                           #   ghcr.io/studio-fug/yapnr-kicad:10.0.6-1
#   tools/image/build_local.sh --no-smoke   # skip tools/image/smoke_image.sh
#
# Needs Docker with BuildKit (Docker Desktop, colima, OrbStack) and Bazel (Bazelisk), which
# builds the yapnr wheel stamped with the version tools/release/version.py derives from git.
# Users of the published images need neither: they `docker pull`.
#
# Each image is a few GB. On a shared machine: check free disk space first, build once, and
# remove the images afterwards (docker image rm yapnr:local yapnr-kicad:local).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

BASE=""
SMOKE=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --base) BASE="$2"; shift ;;
        --no-smoke) SMOKE=0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

version="$(python3 tools/release/version.py --field pep440)"
revision="$(git rev-parse HEAD)"
base_tag="$(cat docker/yapnr-kicad/TAG)"
kicad_version="${base_tag%-*}"
echo "==> yapnr ${version} (${revision:0:7}), KiCad base ${base_tag}"

echo "==> building the wheel"
bazel build //release:wheel.dist --stamp --embed_label="${version}"
dist="$(mktemp -d)"
trap 'rm -rf "${dist}"' EXIT
cp "$(bazel info bazel-bin)"/release/wheel_dist/yapnr-*.whl "${dist}/"
ls "${dist}"

if [ -z "${BASE}" ]; then
    echo "==> building yapnr-kicad:local"
    docker buildx build --load \
        --file docker/yapnr-kicad/Dockerfile \
        --tag yapnr-kicad:local \
        --build-arg BASE_TAG="${base_tag}" \
        --build-arg BASE_CONTEXT="$(tools/image/base_context.sh)" \
        --build-arg VCS_REF="${revision}" \
        .
    BASE=yapnr-kicad:local
    if [ "${SMOKE}" = 1 ]; then
        tools/image/smoke_image.sh --kicad-only --kicad-version "${kicad_version}" "${BASE}"
    fi
fi

echo "==> building yapnr:local on ${BASE}"
docker buildx build --load \
    --file docker/yapnr/Dockerfile \
    --tag yapnr:local \
    --build-context dist="${dist}" \
    --build-arg KICAD_BASE="${BASE}" \
    --build-arg YAPNR_VERSION="${version}" \
    --build-arg VCS_REF="${revision}" \
    .

if [ "${SMOKE}" = 1 ]; then
    tools/image/smoke_image.sh --kicad-version "${kicad_version}" --version "${version}" \
        --revision "${revision}" yapnr:local
fi
