# syntax=docker/dockerfile:1
#
# ghcr.io/studio-fug/yapnr-kicad: Ubuntu 24.04 with KiCad 10 from the KiCad team's PPA, for
# headless use: kicad-cli, the pcbnew Python module (/usr/bin/python3), footprints, symbols and
# templates. No GUI session, no 3D models. It is the base of ghcr.io/studio-fug/yapnr
# (docker/yapnr/Dockerfile); see docs/containers.md.
#
# Build context: the repository root (.dockerignore passes docker/ and the notices only).
# .github/workflows/image.yaml publishes it for linux/amd64 and linux/arm64 as
# yapnr-kicad:<docker/yapnr-kicad/TAG>, once: a published tag is never overwritten. The PPA
# deletes superseded builds within days, so a published base is the durable record of a KiCad
# build, and the application image builds on it rather than on the PPA. Any change to this
# directory needs a new TAG: <KiCad version>-<N>, N counting rebuilds of one KiCad version. CI
# compares tools/image/base_context.sh with the published base's
# io.github.studio-fug.yapnr.kicad.context label and refuses a changed directory under an old
# TAG. KICAD_VERSION and KICAD_DEB, declared once below, change together with TAG
# (tests/unit/repo/test_images.py).
#
# Targets:
#   base (default)  the runnable image
#   src             the source packages of the bundled KiCad (FROM scratch, files only). They are
#                   fetched on the build platform, so a linux/amd64,linux/arm64 build of this
#                   target needs no emulation. Published as yapnr-kicad:<TAG>-src and never
#                   deleted (docs/containers.md).
#
# Build arguments set by the workflow (and tools/image/build_local.sh). They are declared after
# the package installation, so that a new commit does not invalidate its cache:
#   BASE_TAG      docker/yapnr-kicad/TAG
#   BASE_CONTEXT  tools/image/base_context.sh: the docker/yapnr-kicad this base was built from
#   VCS_REF       the yapnr commit
#
# The Ubuntu archive state the packages come from is taken inside the build, right after
# `apt-get update` (the image's own packages are upgraded to it), and recorded in
# /etc/yapnr/ubuntu-snapshot and /usr/share/doc/yapnr/SOURCES.

ARG KICAD_SERIES=10.0
ARG KICAD_VERSION=10.0.6
ARG KICAD_DEB=10.0.6~ubuntu24.04.1

# --- src: the corresponding source of the KiCad packages ------------------------------------
FROM --platform=$BUILDPLATFORM ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78 AS src-fetch
ARG KICAD_DEB
COPY docker/yapnr-kicad/kicad-ppa.asc /etc/apt/keyrings/kicad-ppa.asc
COPY docker/yapnr-kicad/kicad.sources /etc/apt/sources.list.d/kicad.sources
RUN set -eux; \
    test -n "${KICAD_DEB}"; \
    sed -i 's/^Types: deb$/Types: deb-src/' /etc/apt/sources.list.d/kicad.sources; \
    apt-get update; \
    mkdir -p /src/kicad; \
    cd /src/kicad; \
    apt-get source --download-only \
      "kicad=${KICAD_DEB}" "kicad-footprints=${KICAD_DEB}" \
      "kicad-symbols=${KICAD_DEB}" "kicad-templates=${KICAD_DEB}"; \
    ls -l /src/kicad; \
    test "$(find /src/kicad -name '*.dsc' | wc -l)" -eq 4; \
    rm -rf /var/lib/apt/lists/*

FROM scratch AS src
COPY --from=src-fetch /src/ /src/

# --- base: the runnable image ---------------------------------------------------------------
FROM ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78 AS base
ARG KICAD_SERIES
ARG KICAD_VERSION
ARG KICAD_DEB

# KICAD10_* locate the stock libraries for the seeded library tables (they reference
# ${KICAD10_FOOTPRINT_DIR} and friends). YAPNR_KICAD_* tell yapnr where KiCad is.
ENV LANG=C.UTF-8 \
    HOME=/var/lib/yapnr \
    YAPNR_KICAD_SERIES=${KICAD_SERIES} \
    YAPNR_KICAD_CLI=/usr/bin/kicad-cli \
    YAPNR_KICAD_PYTHON=/usr/bin/python3 \
    KICAD10_FOOTPRINT_DIR=/usr/share/kicad/footprints \
    KICAD10_SYMBOL_DIR=/usr/share/kicad/symbols \
    KICAD10_TEMPLATE_DIR=/usr/share/kicad/template \
    KICAD10_3DMODEL_DIR=/usr/share/kicad/3dmodels

COPY docker/yapnr-kicad/kicad-ppa.asc /etc/apt/keyrings/kicad-ppa.asc
COPY docker/yapnr-kicad/kicad.sources /etc/apt/sources.list.d/kicad.sources
COPY --chmod=0755 docker/yapnr-kicad/yapnr-kicad-env.sh /usr/local/bin/yapnr-kicad-env

RUN set -eux; \
    test -n "${KICAD_SERIES}"; \
    test -n "${KICAD_VERSION}"; \
    test -n "${KICAD_DEB}"; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    # The archive state the package lists describe, as a snapshot.ubuntu.com (and PPA snapshot)
    # ID. Upgrading the image's own packages makes every installed package part of that state.
    snapshot="$(date -u +%Y%m%dT%H%M%SZ)"; \
    apt-get -y upgrade; \
    apt-get install -y --no-install-recommends \
      ca-certificates tini \
      "kicad=${KICAD_DEB}" "kicad-footprints=${KICAD_DEB}" \
      "kicad-symbols=${KICAD_DEB}" "kicad-templates=${KICAD_DEB}"; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /etc/yapnr; \
    echo "${snapshot}" > /etc/yapnr/ubuntu-snapshot; \
    \
    # KiCad runs headless and reports the pinned version, from the CLI and from Python. (With
    # a scratch HOME and TMPDIR: KiCad writes its settings and its instance locks
    # (org.kicad.kicad) there, so neither the image user's HOME nor /tmp keeps them.)
    test -z "${DISPLAY:-}"; \
    scratch_home="$(mktemp -d)"; \
    cli="$(HOME="${scratch_home}" TMPDIR="${scratch_home}" kicad-cli version)"; \
    py="$(HOME="${scratch_home}" TMPDIR="${scratch_home}" \
      python3 -c 'import pcbnew; print(pcbnew.Version())')"; \
    for v in "${cli}" "${py}"; do \
      case "${v}" in "${KICAD_VERSION}"|"${KICAD_VERSION}"[-+~]*) ;; \
        *) echo "KiCad reports '${v}', expected ${KICAD_VERSION}" >&2; exit 1 ;; esac; \
    done; \
    rm -rf "${scratch_home}"; \
    \
    # The global library tables every user starts from (yapnr-kicad-env copies them).
    for d in "${KICAD10_FOOTPRINT_DIR}" "${KICAD10_SYMBOL_DIR}" "${KICAD10_TEMPLATE_DIR}"; do \
      test -d "${d}"; \
    done; \
    mkdir -p /etc/yapnr/kicad-seed; \
    cp "${KICAD10_TEMPLATE_DIR}"/*-lib-table /etc/yapnr/kicad-seed/; \
    test -s /etc/yapnr/kicad-seed/fp-lib-table; \
    test -s /etc/yapnr/kicad-seed/sym-lib-table; \
    grep -q 'KICAD10_FOOTPRINT_DIR' /etc/yapnr/kicad-seed/fp-lib-table; \
    \
    # The unprivileged user: UID/GID 1000 (Ubuntu's own `ubuntu` user goes first).
    userdel --remove ubuntu; \
    groupadd --gid 1000 yapnr; \
    useradd --uid 1000 --gid 1000 --home-dir "${HOME}" --create-home --shell /usr/sbin/nologin \
      yapnr; \
    install -d -o 1000 -g 1000 /project; \
    mkdir -p "${HOME}/.config/kicad/${KICAD_SERIES}"; \
    cp /etc/yapnr/kicad-seed/*-lib-table "${HOME}/.config/kicad/${KICAD_SERIES}/"; \
    chown -R 1000:1000 "${HOME}"

ARG BASE_TAG
ARG BASE_CONTEXT=unknown
ARG VCS_REF=unknown

# Where the source of everything in this image is (THIRD_PARTY.md, "In the container images").
RUN set -eu; \
    test -n "${BASE_TAG}"; \
    snapshot="$(cat /etc/yapnr/ubuntu-snapshot)"; \
    echo "${snapshot}" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$'; \
    ppa="kicad/kicad-10.0-releases"; \
    install -d /usr/share/doc/yapnr; \
    { \
      echo "yapnr-kicad ${BASE_TAG}: Ubuntu 24.04 with KiCad ${KICAD_VERSION}"; \
      echo "Built from https://github.com/Studio-Fug/yapnr/tree/${VCS_REF}/docker/yapnr-kicad"; \
      echo; \
      echo "KiCad (GPL-3.0-or-later) and its footprint, symbol and template libraries"; \
      echo "(CC-BY-SA-4.0 with the KiCad library exception) are the ${KICAD_DEB} packages of"; \
      echo "ppa:${ppa}. Their complete source packages are:"; \
      echo; \
      echo "- in the image ghcr.io/studio-fug/yapnr-kicad:${BASE_TAG}-src (files under /src/kicad),"; \
      echo "  published with this image and never deleted;"; \
      echo "- in Launchpad's PPA snapshot of ${snapshot}:"; \
      echo "  deb-src https://snapshot.ppa.launchpadcontent.net/${ppa}/ubuntu/${snapshot}/ noble main"; \
      echo "- on Launchpad, for as long as it keeps the files of superseded PPA builds:"; \
      for package in kicad kicad-footprints kicad-symbols kicad-templates; do \
        dpkg-query -W -f '${source:Package} ${source:Version}\n' "${package}"; \
      done | sort -u | while read -r name version; do \
        echo "  https://launchpad.net/~kicad/+archive/ubuntu/kicad-10.0-releases/+sourcefiles/${name}/${version}/${name}_${version}.dsc"; \
      done; \
      echo; \
      echo "The Ubuntu packages were installed from the Ubuntu 24.04 archive as it was at"; \
      echo "${snapshot} (UTC; also in /etc/yapnr/ubuntu-snapshot). Their licenses are in"; \
      echo "/usr/share/doc/<package>/copyright; their source packages, at the versions listed below,"; \
      echo "are at"; \
      echo; \
      echo "- https://launchpad.net/ubuntu/+source/<source package>/<version>, which Launchpad keeps"; \
      echo "  for every version published in Ubuntu;"; \
      echo "- https://snapshot.ubuntu.com/ubuntu/${snapshot}/ (deb-src, suites noble, noble-updates"; \
      echo "  and noble-security), or an earlier snapshot for a linux/arm64 package that"; \
      echo "  ports.ubuntu.com had not rebuilt yet (ports builds from the same source packages)."; \
      echo; \
      echo "Installed source packages:"; \
      echo; \
      dpkg-query -W -f '${source:Package} ${source:Version}\n' | sort -u | sed 's/^/  /'; \
    } > /usr/share/doc/yapnr/SOURCES

COPY THIRD_PARTY.md /usr/share/doc/yapnr/THIRD_PARTY.md

LABEL org.opencontainers.image.title="yapnr-kicad" \
      org.opencontainers.image.description="Ubuntu 24.04 with KiCad ${KICAD_VERSION} (kicad-cli, pcbnew) for headless use; the base of ghcr.io/studio-fug/yapnr" \
      org.opencontainers.image.version="${BASE_TAG}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/Studio-Fug/yapnr" \
      org.opencontainers.image.url="https://studio-fug.github.io/yapnr/docs/containers.html" \
      org.opencontainers.image.licenses="GPL-3.0-or-later AND CC-BY-SA-4.0" \
      org.opencontainers.image.base.name="docker.io/library/ubuntu:24.04" \
      io.github.studio-fug.yapnr.kicad.version="${KICAD_VERSION}" \
      io.github.studio-fug.yapnr.kicad.deb="${KICAD_DEB}" \
      io.github.studio-fug.yapnr.kicad.base="ghcr.io/studio-fug/yapnr-kicad:${BASE_TAG}" \
      io.github.studio-fug.yapnr.kicad.source="ghcr.io/studio-fug/yapnr-kicad:${BASE_TAG}-src" \
      io.github.studio-fug.yapnr.kicad.context="${BASE_CONTEXT}"

USER 1000:1000
WORKDIR /project
ENTRYPOINT ["/usr/bin/tini", "--", "yapnr-kicad-env"]
CMD ["kicad-cli", "version"]
