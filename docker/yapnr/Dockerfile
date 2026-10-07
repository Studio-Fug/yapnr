# syntax=docker/dockerfile:1
#
# ghcr.io/studio-fug/yapnr: the yapnr command line with everything it runs: KiCad 10 from the
# yapnr-kicad base, CPython 3.11 with numpy and CPU-only torch, and yapnr itself. linux/amd64
# and linux/arm64; see docs/containers.md.
#
# Build context: the repository root (.dockerignore passes docker/, the notices and
# third_party/image-licenses only). The yapnr wheel of the target architecture (bazel build
# //release:wheel.dist on that architecture: it carries yapnr.rf's native FDTD library, which
# the controller Python loads from site-packages) comes in through the named build context
# `dist`:
#
#   docker buildx build -f docker/yapnr/Dockerfile --build-context dist=<dir with the wheel> .
#
# tools/image/build_local.sh does the whole thing locally; CI: .github/workflows/image.yaml,
# which builds FROM the base by digest (KICAD_BASE=<name>:<tag>@sha256:..., KICAD_BASE_DIGEST).
#
# Inside the image:
#   /opt/venv            the controller: uv-managed CPython (python-build-standalone, the patch
#                        release of Bazel's hermetic 3.11) with the hash-checked runtime lock
#   /usr/bin/python3     KiCad's Python with pcbnew, for KiCad-side workers (YAPNR_KICAD_PYTHON)
#   /usr/local/bin/yapnr the command line, through yapnr-kicad-env (any UID gets a HOME)
#   /project             the working directory; bind-mount the project here
#   /usr/share/doc/yapnr LICENSE, THIRD_PARTY.md, SOURCES and licenses/ (THIRD_PARTY.md)

ARG KICAD_BASE=ghcr.io/studio-fug/yapnr-kicad:10.0.6-1
ARG PYTHON_VERSION=3.11.15
# The python-build-standalone release that uv installs PYTHON_VERSION from (the build checks
# it), and the checksums of that release's full archives, which carry the license texts of the
# libraries CPython bundles (OpenSSL, SQLite, Tcl/Tk, ncurses, ...); the install-only archive
# that uv installs has CPython's own license only. Update them together with uv.
ARG PBS_RELEASE=20260807
ARG PBS_FULL_SHA256_AMD64=acf9a483357be3afd0618e1db13a86a286f2927d3dd2dea1500448c502bb9018
ARG PBS_FULL_SHA256_ARM64=d442ee19356e8f018fbc6113b65fd80c67854a09232ea9d60952e23c0f38d07e

FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

# Replaced by `--build-context dist=<dir>`. Empty on purpose: a build without the wheel fails.
FROM scratch AS dist

# --- the license texts of the libraries in python-build-standalone's CPython ----------------
FROM --platform=$BUILDPLATFORM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3 AS python-licenses
ARG TARGETARCH
ARG PYTHON_VERSION
ARG PBS_RELEASE
ARG PBS_FULL_SHA256_AMD64
ARG PBS_FULL_SHA256_ARM64
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) triple=x86_64-unknown-linux-gnu; sha256="${PBS_FULL_SHA256_AMD64}" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sha256="${PBS_FULL_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl zstd; \
    name="cpython-${PYTHON_VERSION}%2B${PBS_RELEASE}-${triple}-pgo%2Blto-full.tar.zst"; \
    curl -fsSL --retry 3 -o /tmp/pbs.tar.zst \
      "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${name}"; \
    echo "${sha256}  /tmp/pbs.tar.zst" | sha256sum -c -; \
    mkdir -p /tmp/pbs /licenses; \
    zstd -dc /tmp/pbs.tar.zst | tar -x -C /tmp/pbs python/licenses; \
    cp /tmp/pbs/python/licenses/* /licenses/; \
    for name in cpython openssl-3 sqlite tcl ncurses libedit libffi mpdecimal zlib bzip2 liblzma; do \
      test -s "/licenses/LICENSE.${name}.txt"; \
    done; \
    ls /licenses

FROM ${KICAD_BASE}
ARG TARGETARCH
ARG PYTHON_VERSION
ARG PBS_RELEASE

USER root

# The runtime: exactly the pins (and hashes) of requirements.lock, per architecture. On amd64
# torch is the CPU-only build from the PyTorch index (--torch-backend=cpu); PyPI's arm64 torch
# wheel is CPU-only already. Headers and tests of torch are not needed at run time. uv runs as
# root with root's HOME and a cache mount, so nothing of it lands in the image user's HOME
# (`--no-bin`: no ~/.local/bin/python3.11 link either), and uv itself is not in the image.
# Nothing that changes with every commit is in scope here, so this layer stays cached.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=docker/yapnr/runtime-amd64.lock,target=/tmp/runtime-amd64.lock \
    --mount=type=bind,source=docker/yapnr/runtime-arm64.lock,target=/tmp/runtime-arm64.lock \
    --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    export HOME=/root UV_CACHE_DIR=/root/.cache/uv UV_PYTHON_INSTALL_DIR=/opt/python \
      UV_PYTHON_PREFERENCE=only-managed UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy; \
    case "${TARGETARCH}" in \
      amd64) backend="--torch-backend=cpu" ;; \
      arm64) backend="" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    uv python install --no-bin "${PYTHON_VERSION}"; \
    build="$(cat "/opt/python/cpython-${PYTHON_VERSION}-linux-"*/BUILD)"; \
    if [ "${build}" != "${PBS_RELEASE}" ]; then \
      echo "uv installed python-build-standalone ${build}, expected ${PBS_RELEASE}:" \
        "update PBS_RELEASE and the checksums" >&2; \
      exit 1; \
    fi; \
    uv venv --python "${PYTHON_VERSION}" /opt/venv; \
    uv pip install --python /opt/venv/bin/python --require-hashes --no-deps ${backend} \
      --requirements "/tmp/runtime-${TARGETARCH}.lock"; \
    torch_dir=/opt/venv/lib/python3.11/site-packages/torch; \
    rm -rf "${torch_dir}/include" "${torch_dir}/test"; \
    /opt/venv/bin/python -c 'import numpy, torch, yaml, shapely; print(torch.__version__, numpy.__version__, shapely.__version__)'

# The energy pass's geometry is a different Python ABI from the controller.
# Install its separately hash-locked wheels into a private target, never system Python.
# Default workers do not see this directory; --energy activates it explicitly.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=docker/yapnr/energy-kicad-amd64.lock,target=/tmp/energy-kicad-amd64.lock \
    --mount=type=bind,source=docker/yapnr/energy-kicad-arm64.lock,target=/tmp/energy-kicad-arm64.lock \
    --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    export HOME=/root UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy; \
    uv pip install --python /usr/bin/python3 --target /opt/energy-kicad --require-hashes --no-deps \
      --requirements "/tmp/energy-kicad-${TARGETARCH}.lock"; \
    /usr/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' > /opt/energy-kicad/python-version.txt; \
    PYTHONPATH=/opt/energy-kicad /usr/bin/python3 -c 'import pcbnew, shapely; assert shapely.__version__ == "2.1.2"; print(shapely.geos_version_string)'
ENV PNR_ENERGY_SITE_PACKAGES=/opt/energy-kicad

# Declared after the runtime layer: each of these changes with every commit or base.
ARG KICAD_BASE
ARG KICAD_BASE_DIGEST=
ARG YAPNR_VERSION=
ARG VCS_REF=unknown

# yapnr itself, from the Bazel-built wheel, without dependencies (the lock above has them).
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=from=dist,target=/tmp/yapnr-dist \
    set -eux; \
    export HOME=/root UV_PYTHON_PREFERENCE=only-managed UV_COMPILE_BYTECODE=1; \
    set -- /tmp/yapnr-dist/yapnr-*.whl; \
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then \
      echo "expected exactly one yapnr wheel in the build context 'dist'" \
        "(docker buildx build --build-context dist=<dir> ...; see tools/image/build_local.sh)" >&2; \
      exit 1; \
    fi; \
    uv pip install --python /opt/venv/bin/python --no-deps --no-cache "$1"; \
    if [ -n "${YAPNR_VERSION}" ]; then \
      test "$(/opt/venv/bin/yapnr --version)" = "yapnr ${YAPNR_VERSION}"; \
    fi; \
    if find /var/lib/yapnr ! -user 1000 | grep .; then \
      echo "files above in the image user's HOME are not the user's" >&2; exit 1; \
    fi

COPY --chmod=0755 docker/yapnr/yapnr.sh /usr/local/bin/yapnr
COPY LICENSE THIRD_PARTY.md /usr/share/doc/yapnr/
COPY third_party/image-licenses/ /usr/share/doc/yapnr/licenses/
COPY --from=python-licenses /licenses/ /usr/share/doc/yapnr/licenses/python-build-standalone/

# Append this layer's sources to the base image's SOURCES (THIRD_PARTY.md), with the native
# libraries that this architecture's numpy and torch wheels bundle
# (docker/yapnr/native-libraries-<arch>.txt). (`--no-cache`: no uv cache is left in /root.)
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=docker/yapnr,target=/tmp/yapnr-ctx \
    set -eu; \
    export HOME=/root; \
    { \
      echo; \
      echo "yapnr ${YAPNR_VERSION:-(unversioned build)} (AGPL-3.0-or-later; LICENSE in this directory)"; \
      echo "Source: https://github.com/Studio-Fug/yapnr/tree/${VCS_REF}; release source archives"; \
      echo "are attached to https://github.com/Studio-Fug/yapnr/releases."; \
      echo; \
      echo "CPython ${PYTHON_VERSION} in /opt/python is the python-build-standalone ${PBS_RELEASE} build"; \
      echo "(https://github.com/astral-sh/python-build-standalone/releases/tag/${PBS_RELEASE}),"; \
      echo "installed by uv. The licenses of CPython and of the libraries it bundles (OpenSSL,"; \
      echo "SQLite, Tcl/Tk, ncurses, libedit, libffi, mpdecimal, zlib, bzip2, xz, ...) are in"; \
      echo "licenses/python-build-standalone/."; \
      echo; \
      echo "The Python packages in /opt/venv come from PyPI (on linux/amd64, torch from"; \
      echo "https://download.pytorch.org/whl/cpu); each carries its license in its .dist-info"; \
      echo "directory."; \
      echo; \
      cat "/tmp/yapnr-ctx/native-libraries-${TARGETARCH}.txt"; \
      echo; \
      echo "Installed packages:"; \
      echo; \
      uv pip list --no-cache --python /opt/venv/bin/python --format freeze | sed 's/^/  /'; \
    } >> /usr/share/doc/yapnr/SOURCES

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/usr/local/bin:/opt/venv/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1 \
    YAPNR_SOURCE_REVISION=${VCS_REF}

LABEL org.opencontainers.image.title="yapnr" \
      org.opencontainers.image.description="yapnr (yet another place and route) for KiCad boards, with KiCad and the Python runtime bundled" \
      org.opencontainers.image.version="${YAPNR_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/Studio-Fug/yapnr" \
      org.opencontainers.image.url="https://studio-fug.github.io/yapnr/docs/containers.html" \
      org.opencontainers.image.documentation="https://studio-fug.github.io/yapnr/" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later AND GPL-3.0-or-later AND CC-BY-SA-4.0" \
      org.opencontainers.image.base.name="${KICAD_BASE}" \
      org.opencontainers.image.base.digest="${KICAD_BASE_DIGEST}" \
      io.github.studio-fug.yapnr.python.version="${PYTHON_VERSION}" \
      io.github.studio-fug.yapnr.python.build="${PBS_RELEASE}"

USER 1000:1000
WORKDIR /project
# No HEALTHCHECK: it would mark every `run` or `doctor` container unhealthy. The viewer's
# compose service defines its own (docs/containers.md). The viewer and its port arrive in PR4a.
ENTRYPOINT ["/usr/bin/tini", "--", "yapnr"]
CMD ["doctor"]
