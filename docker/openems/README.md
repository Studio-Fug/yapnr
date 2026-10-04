# openEMS task image

[openEMS](https://www.openems.de) (GPL-3.0), the FDTD solver, as a task image for `yapnr exp`
campaigns that run electromagnetic models on Cloud Batch ([guide](../../docs/cloud-experiments.md#task-images-openems)).
It is not published on GHCR: Cloud Build pushes it to the project's private `images` repository,
which the task VMs read.

| File                  | What                                                                                                                                                         |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Dockerfile`          | openEMS-Project at a pinned tag, built from source (command line and Python bindings, no GUI), on Ubuntu 24.04, plus numpy, h5py, scipy, matplotlib, shapely |
| `runtime-packages.sh` | the packages of the shared libraries the build links, so the runtime stage installs those and none of the build                                              |
| `cloudbuild.yaml`     | the generic x86-64 and the AVX-512 build side by side on one Cloud Build machine                                                                             |

## Variants

| Tag                       | `ARCH_FLAGS`                     | For                                                  |
| ------------------------- | -------------------------------- | ---------------------------------------------------- |
| `openems:<ver>-x86-64`    | none (x86-64 baseline, SSE2)     | any amd64 machine                                    |
| `openems:<ver>-x86-64-v4` | `-march=x86-64-v4 -mtune=znver4` | AVX-512 machines: C4D (AMD Zen 5), C4 and N4 (Intel) |

openEMS builds as `Release` (`-O3 -DNDEBUG`); its CMake superbuild passes no compiler flags to the
sub-projects, so `ARCH_FLAGS` reaches them through `CFLAGS`/`CXXFLAGS`, which each CMake configure
and the Python extension build read. Every engine stays built (`--engine=basic`, `sse`,
`sse-compressed`, `multithreaded`). The v4 image stops with an illegal instruction on a machine
without AVX-512 (Arm families, older x86); its build checks the Python import only where the build
machine has AVX-512, so its first task is its test. `/opt/openEMS/arch-flags.txt`,
`openems-project.commit` and `openems-submodules.txt` in the image record the build.

## Build

```sh
python3 tools/exp/openems_plan.py image          # prints the gcloud builds submit command
python3 tools/exp/openems_plan.py image --run    # submits it and waits (about 15-25 minutes)
python3 tools/exp/openems_plan.py digests        # the tags' digests and sizes
```

The build runs as `yapnr-image-build` (`infra/gcp`), which may write to `images` and read the
build context under `cloudbuild/` in the inputs bucket. Locally (any platform, for example arm64
on a Mac): `docker build -t openems:local docker/openems`.
