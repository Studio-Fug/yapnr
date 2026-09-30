# syntax=docker/dockerfile:1
#
# ghcr.io/studio-fug/yapnr: the yapnr command line with everything it runs: KiCad 10 from the
# yapnr-kicad base, CPython 3.11 with numpy and CPU-only torch, and yapnr itself. linux/amd64
# and linux/arm64; see docs/containers.md.
#
# Build context: the repository root (.dockerignore passes docker/ and the notices only). The
# yapnr wheel (bazel build //release:wheel.dist) comes in through the named build context
# `dist`:
#
#   docker buildx build -f docker/yapnr/Dockerfile --build-context dist=<dir with the wheel> .
#
# tools/image/build_local.sh does the whole thing locally; CI: .github/workflows/image.yaml.
#
# Inside the image:
#   /opt/venv            the controller: uv-managed CPython (python-build-standalone, the patch
#                        release of Bazel's hermetic 3.11) with the hash-checked runtime lock
#   /usr/bin/python3     KiCad's Python with pcbnew, for KiCad-side workers (YAPNR_KICAD_PYTHON)
#   /usr/local/bin/yapnr the command line, through yapnr-kicad-env (any UID gets a HOME)
#   /project             the working directory; bind-mount the project here

ARG KICAD_BASE=ghcr.io/studio-fug/yapnr-kicad:10.0.6-1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711

FROM ${UV_IMAGE} AS uv

# Replaced by `--build-context dist=<dir>`. Empty on purpose: a build without the wheel fails.
FROM scratch AS dist

FROM ${KICAD_BASE}
ARG KICAD_BASE
ARG TARGETARCH
ARG PYTHON_VERSION=3.11.15
ARG YAPNR_VERSION=
ARG VCS_REF=unknown

USER root

# The runtime: exactly the pins (and hashes) of requirements.lock, per architecture. On amd64
# torch is the CPU-only build from the PyTorch index (--torch-backend=cpu); PyPI's arm64 torch
# wheel is CPU-only already. Headers and tests of torch are not needed at run time. uv runs as
# root with root's HOME and a cache mount, so nothing of it lands in the image user's HOME
# (`--no-bin`: no ~/.local/bin/python3.11 link either), and uv itself is not in the image.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=docker/yapnr,target=/tmp/yapnr-ctx \
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
    uv venv --python "${PYTHON_VERSION}" /opt/venv; \
    uv pip install --python /opt/venv/bin/python --require-hashes --no-deps ${backend} \
      --requirements "/tmp/yapnr-ctx/runtime-${TARGETARCH}.lock"; \
    torch_dir=/opt/venv/lib/python3.11/site-packages/torch; \
    rm -rf "${torch_dir}/include" "${torch_dir}/test"; \
    /opt/venv/bin/python -c 'import numpy, torch, yaml; print(torch.__version__, numpy.__version__)'

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

# Append this layer's sources to the base image's SOURCES (THIRD_PARTY.md).
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    set -eu; \
    export HOME=/root; \
    { \
      echo; \
      echo "yapnr ${YAPNR_VERSION:-(unversioned build)} (AGPL-3.0-or-later; LICENSE in this directory)"; \
      echo "Source: https://github.com/Studio-Fug/yapnr/tree/${VCS_REF}; release source archives"; \
      echo "are attached to https://github.com/Studio-Fug/yapnr/releases."; \
      echo; \
      echo "CPython ${PYTHON_VERSION} in /opt/python is a python-build-standalone build"; \
      echo "(https://github.com/astral-sh/python-build-standalone), installed by uv. The Python"; \
      echo "packages in /opt/venv come from PyPI (on linux/amd64, torch from"; \
      echo "https://download.pytorch.org/whl/cpu); each carries its license in its .dist-info"; \
      echo "directory. Installed packages:"; \
      echo; \
      uv pip list --python /opt/venv/bin/python --format freeze | sed 's/^/  /'; \
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
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.base.name="${KICAD_BASE}" \
      io.github.studio-fug.yapnr.python.version="${PYTHON_VERSION}"

USER 1000:1000
WORKDIR /project
# No HEALTHCHECK: it would mark every `run` or `doctor` container unhealthy. The viewer's
# compose service defines its own (docs/containers.md). The viewer and its port arrive in PR4a.
ENTRYPOINT ["/usr/bin/tini", "--", "yapnr"]
CMD ["doctor"]
