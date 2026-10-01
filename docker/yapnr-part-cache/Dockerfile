# syntax=docker/dockerfile:1
#
# yapnr-part-cache: the part cache server alone (docs/part-cache.md, "Running a public
# instance"). Ubuntu 24.04's python3 and yapnr's stdlib-only cache and picker modules; no KiCad,
# no numerical stack, no atopile. Runs as an unprivileged user and keeps its data in /data.
#
#   bazel build //release:wheel_for_test      # or //release:wheel.dist with a version
#   docker buildx build -f docker/yapnr-part-cache/Dockerfile \
#       --build-context dist=<dir with the yapnr wheel> -t yapnr-part-cache:local .
#   docker run -p 127.0.0.1:8780:8780 -v part-cache:/data \
#       -v ./tokens:/run/secrets/part-cache-tokens:ro yapnr-part-cache:local
#
# Put a TLS-terminating reverse proxy with request-rate limits in front of it for a public
# instance; the server itself speaks plain HTTP.

# Replaced by `--build-context dist=<dir>`. Empty on purpose: a build without the wheel fails.
FROM scratch AS dist

FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

ARG VCS_REF=unknown

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin part-cache; \
    mkdir -p /data /opt/yapnr; \
    chown part-cache:part-cache /data

# The pure-Python yapnr wheel, unpacked (only its stdlib-only cache and picker modules run).
RUN --mount=from=dist,target=/tmp/yapnr-dist \
    set -eux; \
    set -- /tmp/yapnr-dist/yapnr-*.whl; \
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then \
      echo "expected exactly one yapnr wheel in the build context 'dist'" >&2; exit 1; \
    fi; \
    python3 -m zipfile -e "$1" /opt/yapnr; \
    python3 -c 'import sys; sys.path.insert(0, "/opt/yapnr"); import yapnr.partcache.server'

COPY --chmod=0755 docker/yapnr-part-cache/entrypoint.sh /usr/local/bin/yapnr-part-cache
COPY LICENSE THIRD_PARTY.md /usr/share/doc/yapnr/

LABEL org.opencontainers.image.title="yapnr-part-cache" \
      org.opencontainers.image.description="The yapnr part cache server" \
      org.opencontainers.image.source="https://github.com/Studio-Fug/yapnr" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONPATH=/opt/yapnr PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER part-cache
VOLUME /data
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/v1/health', timeout=4)"
ENTRYPOINT ["/usr/local/bin/yapnr-part-cache"]
