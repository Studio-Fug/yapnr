# syntax=docker/dockerfile:1
# AWS Palace (Apache-2.0), the 3D finite-element electromagnetics solver, as a task image for
# `yapnr exp`: Palace at a pinned commit with MPI (OpenMPI), and a Python with gmsh, numpy and
# shapely so a task can mesh its model before it solves. It is not a yapnr image: campaigns that
# run in it name its interpreter in their `runtime` table (docs/cloud-experiments.md, "Task
# images (Palace)"). See README.md here for the builds and the licences.
#
# Palace publishes no public container, so this is a build of its CMake superbuild, which fetches
# and builds its own pinned dependencies (MFEM, hypre, libCEED, METIS/ParMETIS, ...). Only the
# solvers we use are built: SuperLU_DIST and MUMPS (sparse direct), ARPACK (the wave-port mode
# and eigenmode solves), GSLIB (VoltagePath, probes), LIBXSMM (libCEED's CPU backend); not
# SLEPc/PETSc, STRUMPACK, SUNDIALS (transient), MAGMA or GPU support. Static dependency libraries,
# 32-bit indices, no OpenMP (Palace is MPI-first), the compilers and OpenMPI of Ubuntu 24.04.
#
# Stages: `toolchain` (apt), `deps` (the pinned source and every dependency: the slow, cacheable
# part), `build` (Palace itself), `runtime` (what the solver links, Python, the cpw example and
# its regression references for the smoke test). ARCH_FLAGS picks the instruction set; the
# default "-march=x86-64-v3 -mtune=znver4" (AVX2/FMA) runs on every current x86-64 VM including
# the e2 build machines, and OpenBLAS and LIBXSMM pick their AVX-512 kernels at run time.
#
#   docker build -t palace:local docker/palace                           # amd64 (or arm64, below)
#   docker build -t palace:local --build-arg ARCH_FLAGS= docker/palace   # arm64 (Apple silicon)
#   docker run --rm -v "$PWD":/w -w /w palace:local mpirun -np 4 palace-x86_64.bin cfg.json

ARG UBUNTU=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

# --- compilers, MPI, BLAS/LAPACK -----------------------------------------------------------------
FROM ${UBUNTU} AS toolchain
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git build-essential gfortran \
      cmake pkg-config python3 libopenmpi-dev openmpi-bin libopenblas-serial-dev \
      libunwind-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# --- the pinned Palace source and every dependency (the cacheable layer) -------------------------
FROM toolchain AS deps
# A main commit, not the v0.18.1 release: PR 962 (boundary terms of several attributes that share
# one material entry: wave ports, surface conductivity) is only on main. PALACE_VERSION is what
# `git describe` gives for it; it is embedded as palace.json's GitTag.
ARG PALACE_COMMIT=b797ea8060a52241cd9ab1176199f06af8585816
ARG PALACE_VERSION=v0.18.1-160-gb797ea8
ARG ARCH_FLAGS="-march=x86-64-v3 -mtune=znver4"
ARG NJOBS=6
RUN git init -q /src/palace \
 && cd /src/palace \
 && git remote add origin https://github.com/awslabs/palace.git \
 && git fetch -q --depth 1 origin "${PALACE_COMMIT}" \
 && git checkout -q --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${PALACE_COMMIT}" \
 && mkdir -p /src/meta \
 && git rev-parse HEAD > /src/meta/palace.commit \
 && printf '%s\n' "${PALACE_VERSION}" > /src/meta/palace.version \
 && printf '%s\n' "${ARCH_FLAGS:-generic}" > /src/meta/arch-flags.txt
# Every dependency target of the superbuild (MFEM pulls hypre, METIS/ParMETIS, SuperLU_DIST,
# MUMPS with ScaLAPACK, GSLIB; libCEED pulls LIBXSMM). NJOBS stays below the 8 cores of an 8 GB
# build machine (MFEM's kernel units are large); a second pass at -j2 finishes the job if it ran
# out of memory anyway.
RUN cmake -S /src/palace -B /src/build \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/palace \
      -DCMAKE_C_FLAGS="${ARCH_FLAGS}" -DCMAKE_CXX_FLAGS="${ARCH_FLAGS}" \
      -DCMAKE_Fortran_FLAGS="${ARCH_FLAGS}" \
      -DBUILD_SHARED_LIBS=OFF -DPALACE_WITH_64BIT_INT=OFF -DPALACE_WITH_OPENMP=OFF \
      -DPALACE_WITH_SUPERLU=ON -DPALACE_WITH_MUMPS=ON -DPALACE_WITH_STRUMPACK=OFF \
      -DPALACE_WITH_SLEPC=OFF -DPALACE_WITH_ARPACK=ON -DPALACE_WITH_SUNDIALS=OFF \
      -DPALACE_WITH_GSLIB=ON -DPALACE_WITH_LIBXSMM=ON -DPALACE_WITH_MAGMA=OFF \
      -DPALACE_GIT_COMMIT_ID="${PALACE_VERSION}" \
 && targets="mfem libCEED json json_schema_validator fmt eigen scn arpack-ng" \
 && (cmake --build /src/build --target ${targets} -- -j"${NJOBS}" \
     || cmake --build /src/build --target ${targets} -- -j2)

# --- Palace ------------------------------------------------------------------------------------
FROM deps AS build
# Palace's C++20 units are large (1-2 GB of compiler memory each): PALACE_NJOBS stays below the
# core count of an 8 GB build machine, and a second pass at -j2 finishes after an out-of-memory.
# The install keeps the shared libraries the solver loads from /opt/palace/lib (libCEED,
# LIBXSMM) and the headers libCEED may read at run time; the static archives go.
ARG PALACE_NJOBS=4
RUN (cmake --build /src/build --target palace -- -j"${PALACE_NJOBS}" \
     || cmake --build /src/build --target palace -- -j2) \
 && strip /opt/palace/bin/palace-*.bin \
 && find /opt/palace -name '*.a' -delete \
 && rm -rf /opt/palace/share /opt/palace/lib/cmake /opt/palace/lib/pkgconfig \
      /opt/palace/lib64/cmake /opt/palace/lib64/pkgconfig \
 && find /opt/palace/include -mindepth 1 -maxdepth 1 ! -name palace ! -name ceed ! -name ceed.h \
      -exec rm -rf {} + \
 && grep -qaF "$(cat /src/meta/palace.version)" /opt/palace/bin/palace-*.bin
COPY runtime-packages.sh /usr/local/bin/runtime-packages
RUN runtime-packages /opt/palace/bin /opt/palace/lib* > /src/meta/runtime-packages.txt \
 && mkdir -p /opt/palace/share/palace/examples /opt/palace/share/palace/regression \
 && cp -r /src/palace/examples/cpw /opt/palace/share/palace/examples/ \
 && cp -r /src/palace/test/data/regression/ref/cpw /opt/palace/share/palace/regression/ \
 && cp /src/palace/scripts/schema/config-schema.json /opt/palace/share/palace/ \
 && cp /src/palace/LICENSE /src/palace/NOTICE /opt/palace/share/palace/ \
 && for dir in /src/build/extern/*/; do \
      name=$(basename "${dir}"); \
      for file in "${dir}"LICENSE* "${dir}"COPYING* "${dir}"COPYRIGHT* "${dir}"NOTICE* \
                  "${dir}"Copyright* "${dir}"LICENCE*; do \
        [ -f "${file}" ] || continue; \
        mkdir -p "/opt/palace/share/palace/licenses/${name}"; \
        cp "${file}" "/opt/palace/share/palace/licenses/${name}/"; \
      done; \
    done \
 && cp /src/meta/* /opt/palace/

# --- Python for meshing: gmsh, numpy, shapely (hash-checked) -------------------------------------
FROM ${UBUNTU} AS runtime-base
ENV DEBIAN_FRONTEND=noninteractive
# python3-venv for the venv; the X and GL libraries are what the gmsh wheel's libgmsh links
# (its GUI parts are never used); openmpi-bin for mpirun.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv openmpi-bin \
      libglu1-mesa libgl1 libxrender1 libxcursor1 libxfixes3 libxext6 libxft2 libfontconfig1 \
      libxinerama1 libx11-6 libgomp1 zlib1g \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /opt/palace/requirements.txt
RUN python3 -m venv /opt/palace/venv \
 && /opt/palace/venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: \
      -r /opt/palace/requirements.txt \
 && /opt/palace/venv/bin/python -c "import numpy, shapely; print('numpy', numpy.__version__)" \
 && if [ "$(uname -m)" = x86_64 ]; then \
      /opt/palace/venv/bin/python -c "import gmsh; print('gmsh', gmsh.__version__)"; fi

# --- the task image -----------------------------------------------------------------------------
FROM runtime-base
ARG ARCH_FLAGS="-march=x86-64-v3 -mtune=znver4"
ARG PALACE_COMMIT=b797ea8060a52241cd9ab1176199f06af8585816
ARG PALACE_VERSION=v0.18.1-160-gb797ea8
LABEL org.opencontainers.image.title="Palace task image" \
      org.opencontainers.image.source="https://github.com/Studio-Fug/yapnr" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="AWS Palace ${PALACE_VERSION} (MPI, ARPACK, MUMPS, SuperLU_DIST) with gmsh" \
      io.github.studio-fug.yapnr.palace.commit="${PALACE_COMMIT}" \
      io.github.studio-fug.yapnr.palace.version="${PALACE_VERSION}" \
      io.github.studio-fug.yapnr.palace.arch-flags="${ARCH_FLAGS}"
COPY --from=build /opt/palace /opt/palace
RUN apt-get update \
 && xargs -a /opt/palace/runtime-packages.txt apt-get install -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
# Tasks run as root in the container (Batch): OpenMPI refuses that unless told. The container has
# no ptrace capability, so no cross-memory-attach between ranks (shared memory still is used).
ENV PATH=/opt/palace/venv/bin:/opt/palace/bin:$PATH \
    OMPI_ALLOW_RUN_AS_ROOT=1 \
    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
    OMPI_MCA_btl_vader_single_copy_mechanism=none \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1
# Every library resolves, the solver runs (version, configuration check of the cpw example)
# where the build machine has the instruction set.
RUN if ldd /opt/palace/bin/palace-*.bin | grep "not found"; then exit 1; fi \
 && palace_bin=$(ls /opt/palace/bin/palace-*.bin) \
 && cd /opt/palace/share/palace/examples/cpw \
 && "${palace_bin}" --version \
 && "${palace_bin}" --dry-run cpw_wave_uniform.json > /tmp/dry-run.txt \
 && tail -n 2 /tmp/dry-run.txt && rm /tmp/dry-run.txt
