<!-- markdownlint-disable -->

# Palace image and runner (P0)

Date 2026-10-04 (UTC). Owner request (2026-10-04): "let's get the palace integration stood up
alongside stage 3". This is phase P0 of `design.md` §5 (and the tools of P3): the Palace task
image, its Cloud Build, the campaign tools (`palace_plan.py`, `palace_job.py`), tests, the docs
section, and a one-task smoke on GCP with a stock Palace example.

Evidence labels: **[S]** solver output; **[D]** arithmetic from other numbers. Run times and
memory below are what the cloud runs recorded; nothing RF is measured.

## 1. Result

| Item             | Value                                                                                                                                                                                                                                                                 |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Image            | `us-west4-docker.pkg.dev/<project>/images/palace:b797ea8-x86-64-v3@sha256:9d15737769150ad368cce02dd9c1ca4c27c64ecdfd3969cf3d01dc94c1aae194` (320 MB compressed)                                                                                                       |
| Dependency cache | `palace-deps:b797ea8-x86-64-v3@sha256:d6c7ac8615fd1e475dd418f0721dc78968491a26152c008ef9aaab9a64a32da2` (1.5 GB)                                                                                                                                                      |
| Palace           | `main` b797ea8060a5 (2026-10-02), `git describe` v0.18.1-160-gb797ea8, schema 2-1-0; includes PR 962                                                                                                                                                                  |
| Build            | Cloud Build `38aa2afc` SUCCESS, 05:23:57–05:55:20, **31 min 23 s** on E2_HIGHCPU_8: dependency stage 22 min (MFEM about 12 of it), Palace 6 min (112 units at `-j4`), runtime stage and pushes 2 min; no out-of-memory retry was needed                               |
| Build cost       | **≈ $0.56** [D] (32 billed min × $0.0176/min: 8 vCPU × $0.001808 + 8 GiB × $0.000396, us-west4 regional) + storage ≈ 1.9 GB ≈ $0.19/month                                                                                                                             |
| Smoke            | campaign `20261004-mceval-0e5ee6`: **pass**. Palace's `cpw_wave_uniform` (driven, 4 wave ports, 7 frequencies 2–32 GHz) from the image, 8 MPI ranks on a c4d-highcpu-16 Spot (AMD EPYC 9B45), us-west4                                                                |
| Smoke numbers    | 117,764 DOFs (p = 2; 23,464 on the coarse level), 16,097 elements, solve 34 s wall (preconditioner 16.6 s, linear solve 3.5 s, setup 5.0 s), 14 linear solves / 486 GMRES iterations, peak 224 MB per rank, 1.72 GB summed; CPU 270 s user over 34 s (7.9 cores busy) |
| Smoke check [S]  | `port-S.csv` against Palace's own regression reference (built with its CI stack): 56 complex S values, **max \|ΔS\| = 5.9e-8** (tolerance 0.01)                                                                                                                       |
| Smoke cost       | ≈ $0.005 [D] (VM scheduled 05:57:38, container 05:58:15–05:59:06: 37 s to start including the 320 MB pull, 51 s task)                                                                                                                                                 |
| Track spend      | **≈ $0.57 of $8** (ceilings logged: $3.17 build + $0.41 smoke)                                                                                                                                                                                                        |

## 2. What was built (yapnr `claude/palace`, not pushed)

| Commit               | Content                                                                                                                                                                             |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a38695a`            | `docker/palace/`: Dockerfile, `cloudbuild.yaml`, `requirements.txt` (hash-checked gmsh 4.15.2, numpy 2.5.3, shapely 2.1.2), `runtime-packages.sh` (copy of the openEMS one), README |
| `3fb6b12`            | `tools/exp/palace_plan.py`, `palace_job.py`, `image_tasks.py` (shared with `openems_plan.py`), BUILD wiring, `tests/unit/exp/test_palace_plan.py` (13 tests)                        |
| `d707395`, `fcb2a08` | docs: "Task images (Palace)" in `docs/cloud-experiments.md`                                                                                                                         |
| `0c130d7`            | instance templates kept when any family has one for the VM's shape (C4 in the second region)                                                                                        |
| `fefbf25`            | measured build and smoke numbers in docs and README; CPU model from `/proc/cpuinfo`; AMR counters named plainly                                                                     |

The branch is shared with the P1/P2 session (`776bca6`…`19b5c6f`, `palace/pipeline.md`): linear
history, nobody rebased.

**Image.** Ubuntu 24.04 (pinned digest), GCC 13.3 (Ubuntu's default, matching OpenMPI's Fortran
modules; Palace CI builds C++20 with GCC 12–14), OpenMPI 4.1.6, OpenBLAS 0.3.26 serial. Superbuild
options: SuperLU_DIST and MUMPS on, ARPACK on (wave-port mode and eigenmode solves), GSLIB and
LIBXSMM on; SLEPc/PETSc, STRUMPACK, SUNDIALS, MAGMA, OpenMP, GPU off; static dependency
libraries, 32-bit indices. `-march=x86-64-v3 -mtune=znver4`. The build checks that every library
resolves, that the binary embeds the version, and runs `palace --version` and `--dry-run` on the
bundled example. The image carries the cpw example, Palace's regression references for it, the
configuration schema, and every dependency's licence files.

**Decisions and deviations from the design.**

- **No public image to pull:** `ghcr.io/awslabs/palace` does not exist and the CI caches
  `ghcr.io/awslabs/palace-1.2`, `-develop` refuse anonymous pulls (checked 2026-10-04); release
  containers go to a private ECR (`.github/publish_containers/publish.py`). So we build.
- **One variant, no AVX-512:** a second variant doubles the build; OpenBLAS (dynamic arch) and
  LIBXSMM (JIT) already use AVX-512 on C4D at run time. `_VARIANT`/`_ARCH_FLAGS` substitutions
  build a v4 variant if a measurement ever asks for it.
- GCC 13 instead of the design's GCC 14 (see above); 3-hour timeout instead of 4 (ceiling $3.17);
  `-j6` dependencies / `-j4` Palace with a `-j2` second pass (not needed).
- **Pinned to `main`, not v0.18.1**, as the design says (PR 962). Palace CI was green for Linux
  on b797ea8 (incl. the gcc/openblas/arpack/mumps/static configuration).
- **Runner (`palace_job.py`)** rewrites the configuration rather than relying on its paths: output
  to `out/<id>/postpro`, mesh path absolute, `--set Dotted.Key=value` overrides; checks with
  `--dry-run`; runs `mpirun -np R --bind-to core --map-by core` (unbound when several models share
  a VM), OpenMPI as root, `ob1`/`vader`, no cross-memory attach. Records: exit and failed stage,
  wall/CPU, the CPU, DOFs, elements, multigrid DOFs, AMR refinements and the unknowns per solve,
  linear solves/iterations, all timers, peak memory per rank and summed, the image build, and
  the complex \|ΔS\| against an optional reference. An optional `prepare` script meshes in the task.
- **Plan (`palace_plan.py`)**: one model per VM by default (8 ranks, c4d-highcpu-16, 24 GB);
  `memory_gb` picks highcpu/standard; an instance policy only when no family has a template for
  the shape. openEMS behaviour unchanged: the refactor writes byte-identical campaigns and the
  same build command (checked against the previous `openems_plan.py`; its 7 tests pass).

**Tests.** prek clean (incl. privacy). Bazel: all of `//tests/unit/exp/...` (14 targets) plus
`//tests/unit/repo:test_wiring`, `test_privacy_scan`, `test_images` pass.

## 3. Findings for the owner and the next stages

1. **ParMETIS licence (review).** Palace's superbuild always links ParMETIS 4.0.3, whose
   University of Minnesota licence allows non-profit research use and evaluation by others, and
   forbids redistribution
   ([text](https://bitbucket.org/petsc/pkg-parmetis/src/v4.0.3-p10/LICENSE.txt)). The image stays
   private; using it beyond evaluation is the owner's decision. Palace's own CI images and Spack
   package link it too.
2. **`origin/main` moved** to b5f12be (#43, quota-aware placement over two regions) after this
   branch started at fb96642. Merging will conflict in `tools/exp/openems_plan.py` (its `DEFAULTS`
   families became `["c4d", "c4"]` and a comment changed; this branch moved its helpers to
   `image_tasks.py`). Resolution: keep the refactor, take #43's families and comment. Not rebased
   here because the P1/P2 session commits to the same branch.
3. Palace's `families` default is `["c4d"]` (calibrated, image in us-west4). With #43, `["c4d",
"c4"]` lets a model spill to C4 in Montreal (C4 templates exist for highcpu-8/16; the image is
   pulled cross-region, about $0.01 per GiB).
4. **Mac Docker is full**: the Docker Desktop disk is at 100 % (158 GB; 103 GB in six running
   containers of other sessions, 144 GB of images). Local image builds fail with "not enough free
   space"; local openEMS runs may hit it too. Nothing was deleted.
5. Timing point for the cost table [D]: 117,764 DOFs × 7 frequencies took 34 s on 8 cores, about
   5 s per frequency, half of it preconditioner setup. Scaled linearly to 2 M DOFs that is about
   1.4 min per frequency, inside the design's 1–3 min assumption; the calibration (cpw at 4/8/16
   ranks, case (a)) should replace it.
6. `openems_job.py` records the CPU model as `x86_64` (Python's `platform.processor()` on Linux);
   fixed in `palace_job.py` only, to keep openEMS unchanged. The wrapper's dataset has the model.

## 4. How to use it

```sh
PY=<local-path> Y=<local-path>
$PY $Y/tools/exp/palace_plan.py plan JOBS.toml --out <dir>     # format: palace_plan.py docstring
(cd $Y && PYTHONPATH=$Y $PY -m yapnr.exp.cli plan <dir>/campaign.toml --backend gcp-batch)
(cd $Y && PYTHONPATH=$Y $PY -m yapnr.exp.cli submit <cid>)     # log the ceiling first
(cd $Y && PYTHONPATH=$Y $PY -m yapnr.exp.cli status <cid>)     # until done
(cd $Y && PYTHONPATH=$Y $PY -m yapnr.exp.cli fetch <cid>)
$PY $Y/tools/exp/palace_plan.py collect <cid> --dest <tree>
```

The smoke's jobs file is `smoke/JOBS.toml`; its plan, submit and status logs and the collected
results (`smoke/runs/`: record, log, effective config, `postpro/port-S.csv`, `palace.json`) are
next to it. The build log is `build/build-38aa2afc.log`.

## Sources (accessed 2026-10-04 UTC)

- Palace b797ea8: <https://github.com/awslabs/palace/tree/b797ea8060a52241cd9ab1176199f06af8585816>
  (`CHANGELOG.md`, `docs/src/install.md`, `CMakeLists.txt`, `cmake/External*.cmake`,
  `palace/drivers/basesolver.cpp` for `palace.json`, `palace/utils/iodata.cpp` for the
  configuration syntax, `test/unit/regression/cases.cpp` for the cpw tolerances,
  `.github/workflows/containers.yml`, `ghcr-cleanup.yml`, `.github/publish_containers/publish.py`)
- Palace CI check runs on b797ea8: <https://api.github.com/repos/awslabs/palace/commits/b797ea8060a52241cd9ab1176199f06af8585816/check-runs>
- `git describe` distance: <https://api.github.com/repos/awslabs/palace/compare/v0.18.1...b797ea8060a52241cd9ab1176199f06af8585816>
- GHCR anonymous token endpoint (ghcr.io/token) for `awslabs/palace`, `palace-1.2`, `palace-develop`
- ParMETIS licence: <https://bitbucket.org/petsc/pkg-parmetis/src/v4.0.3-p10/LICENSE.txt>
- PyPI: <https://pypi.org/project/gmsh/4.15.2/>, <https://pypi.org/project/numpy/2.5.3/>,
  <https://pypi.org/project/shapely/2.1.2/>
- libCEED Makefile rpath (39f259f): <https://github.com/CEED/libCEED/blob/39f259f89332e936122f7e02d6088a1dae3fb628/Makefile>
- Cloud Build pricing (per vCPU- and GiB-minute, regional): <https://cloud.google.com/build/pricing> ;
  values as used in `radar60/cloud-em/READY.md`
- Internal: `palace/design.md`, `palace/pipeline.md`, `radar60/cloud-em/READY.md`,
  `radar60/cloud-em/calib/REPORT.md`
