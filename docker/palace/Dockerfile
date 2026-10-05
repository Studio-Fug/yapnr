# syntax=docker/dockerfile:1
# AWS Palace (Apache-2.0), the 3D finite-element electromagnetics solver, as a task image for
# `yapnr exp`: Palace at a pinned commit with MPI (OpenMPI), and a Python with gmsh, numpy and
# shapely so a task can mesh its model before it solves. It is not a yapnr image: campaigns that
# run in it name its interpreter in their `runtime` table (docs/cloud-experiments.md, "Task
# images (Palace)"). See README.md here for the builds and the licences.
#
# Palace publishes no public container, so this is a build of its CMake superbuild, which fetches
# and builds its own pinned dependencies (MFEM, hypre, libCEED, METIS, ...). Only the solvers we
# use are built: SuperLU_DIST and MUMPS (sparse direct), ARPACK (the wave-port mode and eigenmode
# solves), GSLIB (VoltagePath, probes), LIBXSMM (libCEED's CPU backend); not SLEPc/PETSc,
# STRUMPACK, SUNDIALS (transient), MAGMA or GPU support. Static dependency libraries, 32-bit
# indices, no OpenMP (Palace is MPI-first), the compilers and OpenMPI of Ubuntu 24.04.
#
# No ParMETIS: its licence allows evaluation use only outside non-profit research and forbids
# redistribution. patches/ replaces it in the superbuild with Scotch/PT-Scotch (CeCILL-C), which
# keeps the parallel orderings: SuperLU_DIST's ParMETIS_V3_NodeND comes from Scotch's
# ParMETIS-compatible library on PT-Scotch, and MUMPS calls PT-Scotch itself. The build proves it
# (provenance.py, conformance/, sbom.py; README.md, "No ParMETIS").
#
# Stages: `toolchain` (apt), `solvers` (the pinned, patched source; Scotch, METIS, ScaLAPACK,
# SuperLU_DIST and MUMPS, and the checks on them), `deps` (every other dependency: the slow,
# cacheable part), `build` (Palace itself), `runtime` (what the solver links, Python, the cpw
# example and its regression references for the smoke test). ARCH_FLAGS picks the instruction set;
# the default "-march=x86-64-v3 -mtune=znver4" (AVX2/FMA) runs on every current x86-64 VM
# including the e2 build machines, and OpenBLAS and LIBXSMM pick their AVX-512 kernels at run time.
#
#   docker build -t palace:local docker/palace                           # amd64 (or arm64, below)
#   docker build -t palace:local --build-arg ARCH_FLAGS= docker/palace   # arm64 (Apple silicon)
#   docker run --rm -v "$PWD":/w -w /w palace:local mpirun -np 4 palace-x86_64.bin cfg.json

ARG UBUNTU=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

# --- compilers, MPI, BLAS/LAPACK (flex and bison: Scotch's strategy parser) ----------------------
FROM ${UBUNTU} AS toolchain
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git build-essential gfortran \
      cmake pkg-config python3 libopenmpi-dev openmpi-bin libopenblas-serial-dev \
      libunwind-dev zlib1g-dev flex bison \
 && rm -rf /var/lib/apt/lists/*

# --- the pinned Palace source, patched; the ordering libraries and the direct solvers ------------
FROM toolchain AS solvers
# A main commit, not the v0.18.1 release: PR 962 (boundary terms of several attributes that share
# one material entry: wave ports, surface conductivity) is only on main. PALACE_VERSION is what
# `git describe` gives for it; it is embedded as palace.json's GitTag.
ARG PALACE_COMMIT=b797ea8060a52241cd9ab1176199f06af8585816
ARG PALACE_VERSION=v0.18.1-160-gb797ea8
ARG ARCH_FLAGS="-march=x86-64-v3 -mtune=znver4"
ARG NJOBS=6
# Where the superbuild's ParMETIS download points: a URL that cannot be fetched, so a build that
# tried to fetch ParMETIS would fail (the patch builds Scotch instead; provenance.py checks it)
ENV PALACE_PARMETIS_TRIPWIRE=file:///nonexistent/parmetis-is-not-built
RUN git init -q /src/palace \
 && cd /src/palace \
 && git remote add origin https://github.com/awslabs/palace.git \
 && git fetch -q --depth 1 origin "${PALACE_COMMIT}" \
 && git checkout -q --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${PALACE_COMMIT}" \
 && mkdir -p /src/meta /src/provenance \
 && git rev-parse HEAD > /src/meta/palace.commit \
 && printf '%s\n' "${PALACE_VERSION}" > /src/meta/palace.version \
 && printf '%s\n' "${ARCH_FLAGS:-generic}" > /src/meta/arch-flags.txt
# patches/palace-ptscotch.diff: Scotch/PT-Scotch (pinned commit) in place of ParMETIS in the
# superbuild; the two patches it adds to the superbuild's patch lists: mumps-ptscotch.diff (the
# MUMPS CMake wrapper's PT-Scotch option) and scotch-hdgraph-fold.diff (Scotch's hdgraphFold2()
# refactor of v7.0.15 reverted: it aborts MUMPS's parallel analysis at 8 ranks; README.md)
COPY patches/ /src/patches/
RUN cd /src/palace \
 && git apply --check /src/patches/palace-ptscotch.diff \
 && git apply /src/patches/palace-ptscotch.diff \
 && cp /src/patches/mumps-ptscotch.diff extern/patch/mumps/patch_ptscotch.diff \
 && mkdir -p extern/patch/scotch \
 && cp /src/patches/scotch-hdgraph-fold.diff extern/patch/scotch/patch_hdgraph_fold.diff \
 && (cd /src/patches && sha256sum ./*.diff) > /src/meta/patches.sha256 \
 && printf 'pt-scotch\n' > /src/meta/ordering.txt
RUN (cmake -S /src/palace -B /src/build \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/palace \
      -DCMAKE_C_FLAGS="${ARCH_FLAGS}" -DCMAKE_CXX_FLAGS="${ARCH_FLAGS}" \
      -DCMAKE_Fortran_FLAGS="${ARCH_FLAGS}" \
      -DBUILD_SHARED_LIBS=OFF -DPALACE_WITH_64BIT_INT=OFF -DPALACE_WITH_OPENMP=OFF \
      -DPALACE_WITH_SUPERLU=ON -DPALACE_WITH_MUMPS=ON -DPALACE_WITH_STRUMPACK=OFF \
      -DPALACE_WITH_SLEPC=OFF -DPALACE_WITH_ARPACK=ON -DPALACE_WITH_SUNDIALS=OFF \
      -DPALACE_WITH_GSLIB=ON -DPALACE_WITH_LIBXSMM=ON -DPALACE_WITH_MAGMA=OFF \
      -DPALACE_GIT_COMMIT_ID="${PALACE_VERSION}" \
      -DEXTERN_PARMETIS_URL="${PALACE_PARMETIS_TRIPWIRE}" \
      -DPALACE_LINK_MAP=/src/provenance/palace.map \
      > /src/provenance/superbuild-configure.log 2>&1 \
     || (cat /src/provenance/superbuild-configure.log; exit 1)) \
 && cat /src/provenance/superbuild-configure.log
# Scotch (with PT-Scotch and its ParMETIS API), METIS and ScaLAPACK come with these. NJOBS stays
# below the 8 cores of an 8 GB build machine; a second pass at -j2 finishes the job if it ran out
# of memory anyway.
RUN cmake --build /src/build --target superlu_dist mumps -- -j"${NJOBS}" \
 || cmake --build /src/build --target superlu_dist mumps -- -j2
# Scotch's ParMETIS_V3_NodeND against the ParMETIS manual's description (conformance/README.md),
# then the checks that nothing of ParMETIS was fetched, built or installed
COPY conformance/ /src/conformance/
COPY --chmod=0755 provenance.py /usr/local/bin/palace-provenance
RUN (sh /src/conformance/run.sh /opt/palace > /src/provenance/nodend-conformance.txt 2>&1 \
     || (cat /src/provenance/nodend-conformance.txt; exit 1)) \
 && cat /src/provenance/nodend-conformance.txt
RUN palace-provenance deps --tripwire "${PALACE_PARMETIS_TRIPWIRE}" \
      --report /src/provenance/deps-solvers.txt

# --- every other dependency (the cacheable layer) ------------------------------------------------
FROM solvers AS deps
ARG NJOBS=6
# Every other dependency target of the superbuild (MFEM pulls hypre and GSLIB; libCEED pulls
# LIBXSMM). MFEM's kernel units are large: -j6, then -j2 after an out-of-memory.
RUN targets="mfem libCEED json json_schema_validator fmt eigen scn arpack-ng" \
 && (cmake --build /src/build --target ${targets} -- -j"${NJOBS}" \
     || cmake --build /src/build --target ${targets} -- -j2) \
 && palace-provenance deps --tripwire "${PALACE_PARMETIS_TRIPWIRE}" \
      --report /src/provenance/deps.txt

# --- Palace ------------------------------------------------------------------------------------
FROM deps AS build
# Palace's C++20 units are large (1-2 GB of compiler memory each): PALACE_NJOBS stays below the
# core count of an 8 GB build machine, and a second pass at -j2 finishes after an out-of-memory.
ARG PALACE_NJOBS=4
RUN cmake --build /src/build --target palace -- -j"${PALACE_NJOBS}" \
 || cmake --build /src/build --target palace -- -j2
# Before strip: the link map and the symbols of the executable say where every ParMETIS-named
# function in it comes from (Scotch); then the components of the build for the SBOM
COPY --chmod=0755 sbom.py /usr/local/bin/palace-sbom
RUN palace-provenance binary --map /src/provenance/palace.map \
      --binary "$(ls /opt/palace/bin/palace-*.bin)" --report /src/provenance/binary.txt \
 && palace-sbom build --out /src/provenance/sbom-build.json
# The install keeps the shared libraries the solver loads from /opt/palace/lib (libCEED,
# LIBXSMM) and the headers libCEED may read at run time; the static archives go.
RUN strip /opt/palace/bin/palace-*.bin \
 && find /opt/palace -name '*.a' -delete \
 && rm -rf /opt/palace/share /opt/palace/lib/cmake /opt/palace/lib/pkgconfig \
      /opt/palace/lib64/cmake /opt/palace/lib64/pkgconfig \
 && find /opt/palace/include -mindepth 1 -maxdepth 1 ! -name palace ! -name ceed ! -name ceed.h \
      -exec rm -rf {} + \
 && grep -qaF "$(cat /src/meta/palace.version)" /opt/palace/bin/palace-*.bin
COPY runtime-packages.sh /usr/local/bin/runtime-packages
# Licences: each dependency's licence files from its source tree, the CeCILL-C text Scotch's
# licence refers to, and MUMPS's own (the release the MUMPS CMake wrapper downloads)
RUN runtime-packages /opt/palace/bin /opt/palace/lib* > /src/meta/runtime-packages.txt \
 && mkdir -p /opt/palace/share/palace/examples /opt/palace/share/palace/regression \
 && cp -r /src/palace/examples/cpw /opt/palace/share/palace/examples/ \
 && cp -r /src/palace/test/data/regression/ref/cpw /opt/palace/share/palace/regression/ \
 && cp /src/palace/scripts/schema/config-schema.json /opt/palace/share/palace/ \
 && cp /src/palace/LICENSE /src/palace/NOTICE /opt/palace/share/palace/ \
 && for dir in /src/build/extern/*/ /src/build/extern/mumps-build/mumps-src/; do \
      name=$(basename "${dir}"); \
      for file in "${dir}"LICENSE* "${dir}"COPYING* "${dir}"COPYRIGHT* "${dir}"NOTICE* \
                  "${dir}"Copyright* "${dir}"LICENCE*; do \
        [ -f "${file}" ] || continue; \
        mkdir -p "/opt/palace/share/palace/licenses/${name}"; \
        cp "${file}" "/opt/palace/share/palace/licenses/${name}/"; \
      done; \
    done \
 && test -f /opt/palace/share/palace/licenses/mumps-src/LICENSE \
 && cp /src/build/extern/scotch/doc/CeCILL-C_V1-en.txt /opt/palace/share/palace/licenses/scotch/ \
 && mkdir -p /opt/palace/share/palace/provenance \
 && cp /src/provenance/deps-solvers.txt /src/provenance/deps.txt /src/provenance/binary.txt \
       /src/provenance/nodend-conformance.txt /src/provenance/superbuild-configure.log \
       /src/provenance/sbom-build.json /src/meta/patches.sha256 \
       /opt/palace/share/palace/provenance/ \
 && cp -r /src/patches /opt/palace/share/palace/provenance/patches \
 && cp /usr/local/bin/palace-sbom /opt/palace/share/palace/provenance/sbom.py \
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
ARG YAPNR_COMMIT=""
# The licences of what the image holds, as SPDX: Palace, hypre, METIS and scnlib (Apache-2.0),
# MFEM, ScaLAPACK, ARPACK-NG, LIBXSMM, OpenBLAS, OpenMPI, numpy, shapely (BSD-3-Clause),
# SuperLU_DIST (BSD-3-Clause-LBNL), libCEED (BSD-2-Clause), GSLIB, fmt, nlohmann/json and the
# MUMPS CMake wrapper (MIT), Eigen (MPL-2.0), MUMPS and Scotch (CeCILL-C), GEOS (LGPL-2.1) and gmsh
# (GPL-2.0-or-later). The texts are in /opt/palace/share/palace/licenses, the SPDX bill of
# materials in /opt/palace/share/palace/provenance/sbom.spdx.json (README.md).
LABEL org.opencontainers.image.title="Palace task image" \
      org.opencontainers.image.source="https://github.com/Studio-Fug/yapnr" \
      org.opencontainers.image.revision="${YAPNR_COMMIT}" \
      org.opencontainers.image.licenses="Apache-2.0 AND BSD-3-Clause AND BSD-3-Clause-LBNL AND BSD-2-Clause AND MIT AND MPL-2.0 AND CECILL-C AND LGPL-2.1-only AND GPL-2.0-or-later" \
      org.opencontainers.image.description="AWS Palace ${PALACE_VERSION} (MPI, ARPACK, MUMPS, SuperLU_DIST, PT-Scotch orderings) with gmsh" \
      io.github.studio-fug.yapnr.palace.commit="${PALACE_COMMIT}" \
      io.github.studio-fug.yapnr.palace.version="${PALACE_VERSION}" \
      io.github.studio-fug.yapnr.palace.arch-flags="${ARCH_FLAGS}" \
      io.github.studio-fug.yapnr.palace.ordering="pt-scotch"
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
# No ParMETIS: no file of that name anywhere in the image, and none among the components of the
# SPDX bill of materials (Palace, every superbuild dependency, the Debian and Python packages)
RUN if find / -xdev -iname '*parmetis*' | grep .; then exit 1; fi \
 && python3 /opt/palace/share/palace/provenance/sbom.py image \
      --partial /opt/palace/share/palace/provenance/sbom-build.json \
      --out /opt/palace/share/palace/provenance/sbom.spdx.json
