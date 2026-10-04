# syntax=docker/dockerfile:1
# openEMS (GPL-3.0) as a task image for `yapnr exp`: the FDTD solver, its CSXCAD and openEMS
# Python bindings and the Python packages task scripts use (numpy, h5py, scipy, matplotlib,
# shapely). It is not a yapnr image: campaigns that run in it name its interpreter in their
# `runtime` table (docs/cloud-experiments.md, "Task images"). See README.md here for the builds.
#
# Ubuntu 24.04 no longer packages openEMS, so this is a source build of openEMS-Project at a pinned
# tag (command line and Python bindings, no GUI) in the `build` stage. The runtime stage holds the
# install, the packages of the shared libraries it links (found with ldd, see runtime-packages.sh)
# and the Python packages.
#
# ARCH_FLAGS picks the instruction set. Empty is the generic build of the platform (x86-64 with
# SSE2 on amd64); "-march=x86-64-v4 -mtune=znver4" is the AVX-512 build for AMD Zen 4/5 (C4D).
# openEMS builds as Release (-O3 -DNDEBUG) by default and its CMake superbuild hands no compiler
# flags to the sub-projects, so the flags go in through CFLAGS and CXXFLAGS, which every CMake
# configure and the Python extension build read. Every engine (basic, sse, sse-compressed,
# multithreaded) is built either way; flush-to-zero stays on (the openEMS default).
#
#   docker build -t openems:local docker/openems
#   docker build -t openems:v4 --build-arg ARCH_FLAGS="-march=x86-64-v4 -mtune=znver4" docker/openems
#   docker run --rm -v "$PWD":/w -w /w openems:local python3 sim.py --threads 4

ARG UBUNTU=ubuntu:24.04

# Build dependencies and the pinned sources: shared by every ARCH_FLAGS variant (one cache layer).
FROM ${UBUNTU} AS deps
ARG OPENEMS_REF=v0.37.0-rc3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git sudo build-essential cmake \
      python3 python3-dev python3-venv python3-pip python3-numpy python3-h5py python3-matplotlib \
      cython3 python3-setuptools python3-setuptools-scm \
      libhdf5-dev libtinyxml-dev libboost-all-dev libcgal-dev libvtk9-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --recursive --depth 1 --branch "${OPENEMS_REF}" \
      https://github.com/thliebig/openEMS-Project.git /src/openEMS-Project \
 && cd /src/openEMS-Project \
 && mkdir -p /src/meta \
 && git rev-parse HEAD > /src/meta/openems-project.commit \
 && git submodule status > /src/meta/openems-submodules.txt \
 && ./scripts/install_deps.sh --check --disable-gui --python

FROM deps AS build
ARG NJOBS=8
ARG ARCH_FLAGS=""
RUN cd /src/openEMS-Project \
 && export CFLAGS="-O3 ${ARCH_FLAGS}" CXXFLAGS="-O3 ${ARCH_FLAGS}" \
 && ./update_openEMS.sh /opt/openEMS --python --disable-GUI --njobs="${NJOBS}" \
      --python-venv-mode site --python-venv-dir /opt/openEMS/venv
COPY runtime-packages.sh /usr/local/bin/runtime-packages
RUN LD_LIBRARY_PATH=/opt/openEMS/lib runtime-packages /opt/openEMS > /src/meta/runtime-packages.txt \
 && printf '%s\n' "${ARCH_FLAGS:-generic}" > /src/meta/arch-flags.txt \
 && cp /src/meta/* /opt/openEMS/

FROM ${UBUNTU}
ARG ARCH_FLAGS=""
ARG OPENEMS_REF=v0.37.0-rc3
ENV DEBIAN_FRONTEND=noninteractive
LABEL org.opencontainers.image.title="openEMS task image" \
      org.opencontainers.image.source="https://github.com/Studio-Fug/yapnr" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.description="openEMS ${OPENEMS_REF} (openEMS-Project, no GUI) with Python bindings" \
      io.github.studio-fug.yapnr.openems.ref="${OPENEMS_REF}" \
      io.github.studio-fug.yapnr.openems.arch-flags="${ARCH_FLAGS}"
COPY --from=build /opt/openEMS /opt/openEMS
RUN apt-get update \
 && xargs -a /opt/openEMS/runtime-packages.txt apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-numpy python3-h5py python3-scipy python3-matplotlib \
      python3-shapely \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/openEMS/venv/bin:/opt/openEMS/bin:$PATH \
    LD_LIBRARY_PATH=/opt/openEMS/lib \
    OMP_NUM_THREADS=1
# Every library resolves; the bindings import where the build machine runs the instruction set
# (an AVX-512 build on a builder without AVX-512 is checked on its first task instead).
RUN if ldd /opt/openEMS/bin/openEMS | grep "not found"; then exit 1; fi \
 && if [ -z "${ARCH_FLAGS}" ] || grep -qw avx512f /proc/cpuinfo; then \
      python3 -c "import CSXCAD, openEMS, h5py, numpy; print('openEMS python ok')"; \
    else echo "no AVX-512 on the build machine: import check skipped"; fi
