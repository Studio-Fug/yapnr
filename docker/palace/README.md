# Palace task image

[AWS Palace](https://github.com/awslabs/palace) (Apache-2.0), the 3D finite-element
electromagnetics solver (driven, eigenmode, electrostatic, magnetostatic and 2D boundary-mode
problems; lumped and wave ports; adaptive mesh refinement; adaptive frequency sweeps), as a task
image for `yapnr exp` campaigns that run electromagnetic models on Cloud Batch
([guide](../../docs/cloud-experiments.md#task-images-palace)). Palace publishes no public
container image (its CI pushes to a private registry), so Cloud Build builds this one into the
project's private `images` repository, which the task VMs read. It is not published on GHCR.

| File                  | What                                                                                                                                         |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `Dockerfile`          | Palace's CMake superbuild at a pinned commit on Ubuntu 24.04 (GCC 13, OpenMPI 4.1, OpenBLAS), plus a Python with gmsh, numpy and shapely     |
| `requirements.txt`    | the Python packages, pinned and hash-checked                                                                                                 |
| `runtime-packages.sh` | the packages of the shared libraries the build links, so the runtime stage installs those and none of the build (as in `docker/openems`)     |
| `cloudbuild.yaml`     | the build on one Cloud Build machine, with the dependency stage pushed as `palace-deps:<tag>-<variant>` mid-build and read back as its cache |

## What is in the image

| Path                                             | What                                                                                                                       |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `/opt/palace/bin/palace-x86_64.bin`              | the solver; run it under `mpirun -np N` (the `palace` wrapper script is there too)                                         |
| `/opt/palace/venv/bin/python`                    | Python 3.12 with gmsh 4.15.2, numpy and shapely: the campaigns' `runtime` interpreter                                      |
| `/opt/palace/share/palace/examples/cpw`          | Palace's coplanar-waveguide example (configurations and meshes), for the smoke test                                        |
| `/opt/palace/share/palace/regression/cpw`        | Palace's regression references for it (`port-S.csv` and the other outputs)                                                 |
| `/opt/palace/share/palace/config-schema.json`    | the configuration schema of this commit                                                                                    |
| `/opt/palace/palace.commit`, `palace.version`, … | the build: the commit, its `git describe` (also `GitTag` in every `palace.json`), `arch-flags.txt`, `runtime-packages.txt` |
| `/opt/palace/share/palace/licenses/`             | the licence files of every dependency the superbuild linked                                                                |

Built: SuperLU_DIST and MUMPS (sparse direct solvers), ARPACK (the eigensolver of the wave-port
mode solve and of eigenmode problems), GSLIB (`VoltagePath`, probes), LIBXSMM (libCEED's CPU
backend), static dependency libraries, 32-bit indices, no OpenMP. Not built: SLEPc/PETSc,
STRUMPACK, SUNDIALS (transient problems), MAGMA, CUDA/HIP. The environment lets OpenMPI run as
root (Batch runs the container as root) and turns cross-memory attach off (no ptrace in the
container).

## Variants

| Tag                          | `ARCH_FLAGS`                     | For                                                       |
| ---------------------------- | -------------------------------- | --------------------------------------------------------- |
| `palace:<commit7>-x86-64-v3` | `-march=x86-64-v3 -mtune=znver4` | x86-64 with AVX2 and FMA: C4D, C4, N4 and the build's e2s |

There is no AVX-512 variant: a second variant doubles the build on the only build machine the
quota allows, while the hot kernels pick their instruction set at run time anyway (OpenBLAS's
dynamic architecture, LIBXSMM's JIT), and AVX-512 gained openEMS about 3 %. `cloudbuild.yaml`
builds one with `_VARIANT=x86-64-v4,_ARCH_FLAGS=-march=x86-64-v4 -mtune=znver4` if a measurement
ever asks for it.

## Build

```sh
python3 tools/exp/palace_plan.py image          # prints the gcloud builds submit command
python3 tools/exp/palace_plan.py image --run    # submits it and waits (about 30 minutes)
python3 tools/exp/palace_plan.py digests        # the tag's digest and size
```

The build runs as `yapnr-image-build` (`infra/gcp`) on `E2_HIGHCPU_8` (8 vCPUs, 8 GB) with a
3-hour timeout. The first one (build `38aa2afc`, 2026-10-04) took 31 minutes: 22 for the
dependencies, 6 for Palace, then the runtime stage and the pushes; about $0.56. The image is
320 MB compressed, its dependency cache 1.5 GB. The dependencies build at `-j6` and Palace at
`-j4`, each with a second pass at `-j2` should the compiler run out of memory (neither needed it).
Locally (any platform; `ARCH_FLAGS=` for arm64, where PyPI has no gmsh wheel and the image goes
without it): `docker build -t palace:local docker/palace`.

## Reproducibility

What a rebuild reproduces, and what it does not:

- **Pinned:** the Ubuntu base (by digest), the Palace commit (checked after the fetch), every
  dependency the superbuild fetches (Palace pins their commits), the Python packages (versions and
  hashes, `requirements.txt`), the compiler flags.
- **Not pinned:** the Ubuntu packages (`apt-get install` takes the archive's current versions:
  GCC, OpenMPI, OpenBLAS). `runtime-packages.txt` in the image lists the ones the solver links,
  with their versions as installed.
- **Tags are mutable:** `palace:<tag>-<variant>` and the `palace-deps` cache tag that the next
  build reads with `--cache-from` are overwritten by each build. The digest is the image's
  identity: campaigns pin it (`palace_plan.py plan` resolves the tag to its digest), and the
  validation and every sign-off name the digest they ran.
- **Source:** `palace_plan.py image` records the yapnr commit as the
  `org.opencontainers.image.revision` label (`-dirty` with uncommitted changes in this directory).

The image of the validation, `palace:b797ea8-x86-64-v3@sha256:9d157377…` (build `38aa2afc`),
was built before the label existed, from a working tree ten minutes before yapnr commit
`a38695a`, whose Dockerfile has the same build steps (the build log matches them); its labels
say `licenses="Apache-2.0"` only. The label changes since then touch the last stage only: the
next build reuses the cached dependency stage, and its digest will differ from the validated
one, so a campaign should keep the validated digest until a rebuilt image has passed the smoke
test and one validation case.

## Licences

Palace is Apache-2.0. Its superbuild fetches and links pinned dependencies; the build copies
each one's licence files from its source tree to `/opt/palace/share/palace/licenses/<name>/`
(next to Palace's own `LICENSE` and `NOTICE`). One of them needs attention: **ParMETIS**,
whose University of Minnesota licence allows use by non-profit institutions for education and
research and by other organizations for evaluation only, and forbids redistribution without
approval
([text](https://bitbucket.org/petsc/pkg-parmetis/src/v4.0.3-p10/LICENSE.txt), accessed
2026-10-04). Palace's superbuild cannot leave ParMETIS out, so this image stays in the private
registry and is never published, and any use beyond evaluation needs the owner's decision. gmsh
(GPL-2.0-or-later) is a Python package in the image's venv for the task scripts; nothing links it
into Palace.
