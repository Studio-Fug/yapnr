# Palace task image

[AWS Palace](https://github.com/awslabs/palace) (Apache-2.0), the 3D finite-element
electromagnetics solver (driven, eigenmode, electrostatic, magnetostatic and 2D boundary-mode
problems; lumped and wave ports; adaptive mesh refinement; adaptive frequency sweeps), as a task
image for `yapnr exp` campaigns that run electromagnetic models on Cloud Batch
([guide](../../docs/cloud-experiments.md#task-images-palace)). Palace publishes no public
container image (its CI pushes to a private registry), so Cloud Build builds this one into the
project's private `images` repository, which the task VMs read. It is not published on GHCR.

| File                  | What                                                                                                                                                     |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Dockerfile`          | Palace's CMake superbuild at a pinned commit on Ubuntu 24.04 (GCC 13, OpenMPI 4.1, OpenBLAS), plus a Python with gmsh, numpy and shapely                 |
| `patches/`            | Scotch/PT-Scotch in place of ParMETIS in the superbuild (`palace-ptscotch.diff`), and the MUMPS CMake wrapper's PT-Scotch option (`mumps-ptscotch.diff`) |
| `conformance/`        | an MPI test of `ParMETIS_V3_NodeND` written from the ParMETIS manual, run against Scotch's version during the build ([README](conformance/README.md))    |
| `provenance.py`       | the build's checks that no ParMETIS was fetched, built, installed or linked                                                                              |
| `sbom.py`             | the image's SPDX bill of materials, and its comparison with syft's                                                                                       |
| `requirements.txt`    | the Python packages, pinned and hash-checked                                                                                                             |
| `runtime-packages.sh` | the packages of the shared libraries the build links, so the runtime stage installs those and none of the build (as in `docker/openems`)                 |
| `cloudbuild.yaml`     | the build on one Cloud Build machine, with the dependency stages pushed mid-build and read back as its cache, then syft's scan of the image              |

## What is in the image

| Path                                             | What                                                                                                                                                         |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/opt/palace/bin/palace-x86_64.bin`              | the solver; run it under `mpirun -np N` (the `palace` wrapper script is there too)                                                                           |
| `/opt/palace/venv/bin/python`                    | Python 3.12 with gmsh 4.15.2, numpy and shapely: the campaigns' `runtime` interpreter                                                                        |
| `/opt/palace/share/palace/examples/cpw`          | Palace's coplanar-waveguide example (configurations and meshes), for the smoke test                                                                          |
| `/opt/palace/share/palace/regression/cpw`        | Palace's regression references for it (`port-S.csv` and the other outputs)                                                                                   |
| `/opt/palace/share/palace/config-schema.json`    | the configuration schema of this commit                                                                                                                      |
| `/opt/palace/palace.commit`, `palace.version`, … | the build: the commit, its `git describe` (also `GitTag` in every `palace.json`), `arch-flags.txt`, `ordering.txt`, `patches.sha256`, `runtime-packages.txt` |
| `/opt/palace/share/palace/licenses/`             | the licence files of every dependency the superbuild linked (MUMPS's own as `mumps-src`, Scotch's with the CeCILL-C text)                                    |
| `/opt/palace/share/palace/provenance/`           | the patches, the check reports (`deps.txt`, `binary.txt`, `nodend-conformance.txt`), the superbuild's configure log and `sbom.spdx.json`                     |

Built: SuperLU_DIST and MUMPS (sparse direct solvers), Scotch and PT-Scotch (orderings),
ARPACK (the eigensolver of the wave-port mode solve and of eigenmode problems), GSLIB
(`VoltagePath`, probes), LIBXSMM (libCEED's CPU backend), static dependency libraries, 32-bit
indices, no OpenMP. Not built: ParMETIS, SLEPc/PETSc, STRUMPACK, SUNDIALS (transient problems),
MAGMA, CUDA/HIP. The environment lets OpenMPI run as root (Batch runs the container as root) and
turns cross-memory attach off (no ptrace in the container).

## No ParMETIS

ParMETIS's licence (University of Minnesota; [text](https://github.com/KarypisLab/ParMETIS/blob/main/LICENSE))
allows use by non-profit institutions for education and research and by US government agencies,
and by others for evaluation only, and forbids redistribution without approval. Palace's
superbuild links it unconditionally (SuperLU_DIST and MUMPS call it for their parallel orderings),
so this image replaces it, keeping the orderings parallel:

- **SuperLU_DIST** calls `ParMETIS_V3_NodeND` for `ColumnOrdering: "ParMETIS"` (its parallel
  ordering and parallel symbolic factorization). It now gets Scotch's ParMETIS-compatible library
  (`libptscotchparmetisv3`, CeCILL-C), which orders with PT-Scotch's distributed nested
  dissection. Scotch v7.0.16 is pinned by commit, built single-threaded and fully deterministic
  (the same ordering on every run), with 32-bit `SCOTCH_Num` like SuperLU_DIST's `int_t`.
- **MUMPS** calls PT-Scotch itself (`-Dptscotch`, with `DETERMINISTIC_PARALLEL_GRAPH`) instead of
  ParMETIS: `ColumnOrdering: "PTScotch"` is its parallel analysis. Its `"ParMETIS"` now fails
  with MUMPS error -38 (ordering not available), never silently.
- Unchanged: the default, `ColumnOrdering: "Default"`, is SuperLU_DIST with serial METIS on every
  rank (METIS is Apache-2.0). Every run of the validation used it, so no validated number ran
  ParMETIS code.

Palace's ordering names are per solver: for SuperLU_DIST, `"Scotch"` and `"PTScotch"` mean its
default (serial METIS) and only `"ParMETIS"` is parallel; for MUMPS, `"PTScotch"` is parallel.

The build proves the absence, and fails otherwise:

1. **Superbuild** (`provenance.py deps`): no ParMETIS project, target or source directory; its
   download URL is a tripwire that cannot be fetched; Scotch at the pinned commit.
2. **Files**: the only `*parmetis*` files installed are Scotch's `parmetis.h` and
   `libptscotchparmetisv3.a` (the final image has none, `find / -xdev`).
3. **Symbols**: only `libptscotchparmetisv3.a` defines ParMETIS names, only METIS defines
   `METIS_NodeND`; SuperLU_DIST still calls the ParMETIS API, MUMPS calls PT-Scotch and no
   ParMETIS name. In Palace's link map (`provenance.py binary`, before `strip`) every archive is a
   known dependency or a system library, and every archive member that defines a ParMETIS-named
   symbol comes from `libptscotchparmetisv3.a`; the executable has `ParMETIS_V3_NodeND` and no
   `ParMETIS_V32_NodeND`. Names are compared without regard to case (Fortran names count).
4. **Behaviour**: `conformance/` checks Scotch's `ParMETIS_V3_NodeND` against the ParMETIS manual
   (permutation, the separator tree in `sizes`, postorder numbering, separators that separate,
   the same result on every run) at 1 to 8 ranks.
5. **Bill of materials**: `sbom.py` writes SPDX 2.3 (`provenance/sbom.spdx.json`): Palace and the
   patches, every superbuild dependency (git remote and commit, or download URL; licence), the
   MUMPS release, the Debian and Python packages. The build fails if any is ParMETIS, and again if
   syft's independent scan of the pushed image finds it.

The patches were written without reading ParMETIS's source, from its manual and the consumers'
source (SuperLU_DIST, MUMPS, MFEM, Palace) and Scotch's.

## Variants

| Tag                              | `ARCH_FLAGS`                     | For                                                       |
| -------------------------------- | -------------------------------- | --------------------------------------------------------- |
| `palace:<commit7>-pts-x86-64-v3` | `-march=x86-64-v3 -mtune=znver4` | x86-64 with AVX2 and FMA: C4D, C4, N4 and the build's e2s |

`-pts` marks the build with PT-Scotch in place of ParMETIS. There is no AVX-512 variant: a second
variant doubles the build on the only build machine the quota allows, while the hot kernels pick
their instruction set at run time anyway (OpenBLAS's dynamic architecture, LIBXSMM's JIT), and
AVX-512 gained openEMS about 3 %. `cloudbuild.yaml` builds one with
`_VARIANT=x86-64-v4,_ARCH_FLAGS=-march=x86-64-v4 -mtune=znver4` if a measurement ever asks for it.

## Build

```sh
python3 tools/exp/palace_plan.py image          # prints the gcloud builds submit command
python3 tools/exp/palace_plan.py image --run    # submits it and waits (about 40 minutes)
python3 tools/exp/palace_plan.py digests        # the tag's digest and size
```

The build runs as `yapnr-image-build` (`infra/gcp`) on `E2_HIGHCPU_8` (8 vCPUs, 8 GB) with a
3-hour timeout. Stages: `solvers` (the patched source; Scotch, METIS, ScaLAPACK, SuperLU_DIST,
MUMPS; the conformance test and the checks), pushed as `palace-deps:<tag>-<variant>-solvers`;
`deps` (MFEM, hypre, libCEED and the rest), pushed as `palace-deps:<tag>-<variant>`; then Palace
and the task image, and syft's scan. The first build with ParMETIS (build `38aa2afc`,
2026-10-04) took 31 minutes, about $0.56. The dependencies build at `-j6` and Palace at `-j4`,
each with a second pass at `-j2` should the compiler run out of memory. Locally (any platform;
`ARCH_FLAGS=` for arm64, where PyPI has no gmsh wheel and the image goes without it):
`docker build -t palace:local docker/palace`.

## Reproducibility

What a rebuild reproduces, and what it does not:

- **Pinned:** the Ubuntu base (by digest), the Palace commit (checked after the fetch), every
  dependency the superbuild fetches (Palace pins their commits; the patch pins Scotch's, checked
  after the build), the Python packages (versions and hashes, `requirements.txt`), the compiler
  flags, syft (by digest).
- **Not pinned:** the Ubuntu packages (`apt-get install` takes the archive's current versions:
  GCC, OpenMPI, OpenBLAS). `runtime-packages.txt` in the image lists the ones the solver links,
  with their versions as installed, and the SBOM lists every package.
- **Tags are mutable:** `palace:<tag>-<variant>` and the `palace-deps` cache tags that the next
  build reads with `--cache-from` are overwritten by each build. The digest is the image's
  identity: campaigns pin it (`palace_plan.py plan` resolves the tag to its digest), and the
  validation and every sign-off name the digest they ran.
- **Source:** `palace_plan.py image` records the yapnr commit as the
  `org.opencontainers.image.revision` label (`-dirty` with uncommitted changes in this directory).

The image of the validation, `palace:b797ea8-x86-64-v3@sha256:9d157377…` (build `38aa2afc`), is
the build with ParMETIS. It stays in the registry under its own tag, for evaluation (comparisons
with this build) only; nothing new should run on it.

## Licences

Palace is Apache-2.0. Its superbuild fetches and links pinned dependencies; the build copies
each one's licence files from its source tree to `/opt/palace/share/palace/licenses/<name>/`
(next to Palace's own `LICENSE` and `NOTICE`): Apache-2.0 (hypre also MIT, METIS, scnlib),
BSD-3-Clause (MFEM, ScaLAPACK, ARPACK-NG, LIBXSMM), BSD-3-Clause-LBNL (SuperLU_DIST),
BSD-2-Clause (libCEED), MIT (GSLIB, fmt, nlohmann/json, json-schema-validator, the MUMPS CMake
wrapper), MPL-2.0 (Eigen, header-only), CeCILL-C (MUMPS, Scotch). The Ubuntu packages carry their
own (`/usr/share/doc/<package>/copyright`). gmsh (GPL-2.0-or-later) is a Python package in the
image's venv for the task scripts; nothing links it into Palace. The image's label lists them as
SPDX; `provenance/sbom.spdx.json` has them per component.
