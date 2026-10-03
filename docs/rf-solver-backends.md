# RF solver backends

`yapnr.rf` steps its FDTD fields with one of three backends. They run the same scheme and,
where it matters, give the same numbers:

| Backend  | What it is                                                  | Precision        | Threads                 |
| -------- | ----------------------------------------------------------- | ---------------- | ----------------------- |
| `numpy`  | the reference: about 70 array operations per step in Python | float64, float32 | one (numpy)             |
| `torch`  | the same operations as torch tensors on the CPU             | float64, float32 | intra-op, at most 4     |
| `native` | blocks of steps in C, `yapnr/rf/fdtd/native/fdtd.c`         | float64, float32 | a pthread pool, any `N` |

`native` is optional: a compiled library that nothing requires. Without it, `native` runs the
numpy reference and says so once on stderr.

## Choosing a backend

- **In code:** `Simulation(grid, structure, backend="native", dtype=np.float64, threads=8)`.
  A simulation without a `backend` takes `$YAPNR_RF_BACKEND`, else numpy.
- **In a spec:** `solver: {backend: native, dtype: float64, threads: 8}`.
- **From the environment,** over the spec of every problem (`yapnr.rf.cases run`, the driver,
  validation):

  | Variable            | Meaning                                                                  |
  | ------------------- | ------------------------------------------------------------------------ |
  | `YAPNR_RF_BACKEND`  | `numpy`, `torch` or `native`                                             |
  | `YAPNR_RF_DTYPE`    | `float64` / `f64` or `float32` / `f32`                                   |
  | `YAPNR_RF_THREADS`  | the native thread pool (default: the spec's `threads`)                   |
  | `YAPNR_RF_FDTD_LIB` | the library to load, before `yapnr/rf/fdtd/native/` and Bazel's runfiles |
  | `YAPNR_RF_TBLOCK`   | native schedule: `auto` (default), `0` sweeps, `N` steps per pass        |
  | `YAPNR_RF_CACHE_MB` | the cache `auto` plans for (default: the CPU's last-level cache)         |
  | `YAPNR_RF_ROWS`     | rows of the box per work item (default 8)                                |

  A backend switched by the environment runs float64 unless `YAPNR_RF_DTYPE` says otherwise:
  the specs' float32 was chosen for torch's speed. An exact problem (`Problem(exact=True)`, the
  gradient tests) runs numpy float64, or native float64 when `YAPNR_RF_BACKEND=native`, which
  gives the same bits.

  ```bash
  YAPNR_RF_BACKEND=native YAPNR_RF_THREADS=8 python -m yapnr.rf.cases run divider --out runs/d
  ```

`Problem.describe()` (the run's provenance) records the backend that actually ran and, for
native, the library's file name and sha256, the instruction set its sweeps use and the threads.

## Exactness

The native stepper computes every value with the numpy reference's operations, in the same
order, and the library is built with `-ffp-contract=off -fno-fast-math`: no fused
multiply-add, no reassociation. Each output value is written by one thread from inputs that the
previous phase finished, so the thread count and the schedule do not change it, and IEEE
arithmetic without contraction gives the same result in scalar, NEON, AVX2 or AVX-512 code. The
stated bound is therefore **0 ulp**: a float64 native run equals a numpy float64 run bit for bit
(fields, DTFTs, S-parameters, objective values, adjoint gradients), and a float32 native run
equals numpy float32. The adjoint stays the exact discrete adjoint of the forward scheme because
it is a second ordinary run of the same operator.

What makes this possible is that the values that do not depend on the fields are computed once,
in Python, for every backend: each block of steps tabulates the sources' values
(`sources.combine`, an elementwise sum in a fixed order rather than a BLAS product) and the DTFT
phase factors. Every backend applies these tables, so they cannot drift apart.

`tests/unit/rf/test_native_kernel.py` checks it with `assert_array_equal`:

- random fields stepped through every kernel on four structures (resistive sheet, copper-edge
  correction with its μ planes, inductive sheet, a resistor with PEC edges) and on uneven CPML;
- runs with port sources, probes and decimation;
- the optimizer's evaluation (forward and adjoint runs and gradients), exact and production
  settings;
- the divider and the antenna on their smoke grids;
- 1 to 4 threads, both schedules, float64 and float32.

float32 itself is validated as torch float32 is: S-parameters within 1e-4 of float64. The
finite-difference gradient tests pass on native too (`YAPNR_RF_BACKEND=native`). The tests pass
on macOS arm64 (Apple clang), Linux arm64 (gcc 13) and Linux x86-64 (gcc 13): on a C4D's Zen 5
with the AVX-512 sweeps, and with the baseline sweeps.

## Building the library

The library is one C11 file and a header, built with any C compiler:

```bash
python -m yapnr.rf.fdtd.native_kernel build     # -> yapnr/rf/fdtd/native/libyapnr_fdtd.*
python -m yapnr.rf.fdtd.native_kernel status    # what backend "native" loads
bazel build //yapnr/rf:libyapnr_fdtd.so          # manual target, the same flags
```

- **macOS arm64:** Apple clang; NEON.
- **Linux x86-64:** gcc or clang. The sweeps are compiled three times (`target_clones`: AVX-512,
  AVX2, baseline) and the loader picks the variant the CPU supports (`status()["isa"]`).
- **Linux arm64** (C4A): gcc or clang; NEON.

The installed package carries the C source, so `build` also works from a wheel. Bazel's targets
are manual, so `bazel build //...` and `bazel test //...` need no C toolchain; the plain unit
test compiles the source with the host compiler when there is one and skips otherwise. The
library's own tests and the gradient checks on it:

```bash
bazel test //tests/unit/rf:test_native_kernel_native //tests/unit/rf:test_adjoint_gradients_native \
  //tests/unit/rf:test_pipeline_gradient_native
```

### The container image

The image (`docker/yapnr/Dockerfile`) does not build the library yet, and it has no C compiler
(checked on C4D). The change it needs is one stage per architecture that compiles the wheel's
source and a copy into the venv, beside the module, where the loader looks first:

```dockerfile
FROM ubuntu:24.04@sha256:<the digest the Dockerfile pins> AS rf-native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev unzip
RUN --mount=from=dist,target=/dist set -eux; \
    unzip -q /dist/yapnr-*.whl 'yapnr/rf/fdtd/native/*' -d /src; \
    gcc -O3 -std=c11 -ffp-contract=off -fno-fast-math -fPIC -shared -pthread \
      -o /libyapnr_fdtd.so /src/yapnr/rf/fdtd/native/fdtd.c

# in the final stage, after the wheel is installed:
COPY --from=rf-native /libyapnr_fdtd.so \
  /opt/venv/lib/python3.11/site-packages/yapnr/rf/fdtd/native/libyapnr_fdtd.so
```

The build stage is native per architecture (the image workflow builds amd64 and arm64 on their
own runners), adds about 270 kB, and nothing changes for a job that does not ask for `native`.
Until the image has it, a job can ship the library in its job bundle and point
`YAPNR_RF_FDTD_LIB` at it, as the C4D benchmark below did.

## Performance

The divider's round-2 grid (124 × 74 × 22 = 0.20 M cells, CPML on five sides, the copper-edge
correction, decimation 67), the forward run with every probe, milliseconds per step. Mac mini
M4, shared (load 7–10, nice 10, so the threads ran where the scheduler put them):

| Backend, precision | 1 thread | 2 threads | 4 threads |
| ------------------ | -------- | --------- | --------- |
| numpy float64      | 7.2      |           |           |
| torch float32      |          |           | 2.2–3.3   |
| native float64     | 0.92–1.0 | 0.68      | 0.44–0.55 |
| native float32     | 0.63     |           | 0.33      |

At 0.80 M cells (refine 2) native float64 takes 3.3 ms per step on one thread and 2.2 on four
(torch float32 14, numpy 37). One evaluation of the divider (forward and adjoint runs, 12,484
steps, gradients) takes 7.2 s native float64 and 4.5 s float32 on 4 threads, against 27.4 s
torch float32 under the same load; Python outside the steps is now about 60 ms of it.

On a Spot `c4d-highcpu-16` (AMD EPYC 9B45, Zen 5: 8 cores, 16 threads, 32 MiB L3; the
library cross-built with gcc 13, AVX-512 sweeps), the same forward run, ms/step:

| Backend, precision, schedule  | 1    | 2    | 4    | 8     | 16    |
| ----------------------------- | ---- | ---- | ---- | ----- | ----- |
| numpy float64                 | 6.47 |      |      |       |       |
| torch float32                 |      |      | 1.00 |       |       |
| native float64, sweeps        | 0.79 | 0.63 | 0.34 | 0.196 | 0.144 |
| native float64, 4-step passes |      |      |      | 0.205 | 0.163 |
| native float32, sweeps        |      |      |      | 0.147 | 0.107 |

At 0.80 M cells (refine 2, the size of the Order 0 divider) the box no longer fits in the L3
and the passes pay: native float64 on 16 threads 1.24 ms per step in sweeps, 0.74 in 5-step
passes (8 threads: 1.42 and 1.05); float32 on 16 threads 0.40 and 0.38; torch float32 on
4 threads 3.84. One evaluation of the divider: torch float32 (4 threads, the cases' setting)
12.5 s; native float64 3.2 s on 8 threads and 2.7 s on 16; native float32 2.6 s on 8 threads.
The objective t is 41.41874 for every float64 run and 41.41875 for native float32 (2.4e-7
relative); float64 against numpy is bit for bit (the tests above, run on the same VM).

The `auto` schedule follows these numbers: sweeps while the box (fields and ψ) fits in half the
last-level cache, passes sized to that cache otherwise. Handing out 8 rows per work item
instead of 2 made the Mac about 20 % faster on 4 threads (fewer claims on the shared counter).

## Design notes

- **C with ctypes,** as the router's native maze kernel (`hardware/pnr`): no build dependency
  for anyone who does not ask for it, the same loader order and fallback. Rust was considered
  and not chosen: a heavier toolchain, portable SIMD only on nightly, and no gain on stencil
  loops that the C compilers vectorize. OpenMP was not used because Apple clang has no libomp;
  a small pthread pool hands out rows dynamically (fast and slow cores balance) and sleeps
  between blocks. BLAS has nothing to offer a stencil.
- **Layout:** every component in one padded box of (Nz + 1) planes × (Nx + 1) rows × a 64-byte
  aligned row along y, so the inner loops run over y (75–300) rather than z (about 25), the
  copper's planes are whole planes, and uniform planes take scalar Ca/Cb. `sim.f` are views.
- **Schedules:** a box that fits in cache is swept twice per step (H, then E), the work of each
  sweep split by rows. A larger box is run in passes of N steps over the planes (H of plane k
  and E of plane k − 1 together, the next step three planes behind, each row's sources, μ
  blend, sheet and probes right after its update): the same values with about 3N planes in
  cache instead of the whole box streamed twice per step. Planes larger than a third of the
  cache (the 60 GHz grids may get there) would need passes tiled along x as well; not done yet.
- **Python per step is gone:** Python runs once per block of steps, up to the stop rule's next
  look (about every 290 steps).

## GPUs

- **Metal (torch MPS): no.** MPS has no float64 (a TypeError), every float32 operation costs
  about 68 µs of dispatch, and the GPU shares the CPU's memory bandwidth (about 87 GB/s on the
  M4), so it cannot beat the CPU kernel on these grids.
- **OpenCL: no.** Deprecated on macOS, and Apple GPUs have no float64.
- **CUDA (G2, L4): deferred** until the 60 GHz grids are sized. An L4 has about 300 GB/s, and
  CUDA keeps the bits with `-fmad=false`, so the same exact design would carry over. For grids
  of 10 M cells or more it is plausibly 3–4 times a C4D-16 per VM, but it needs GPU quota, a CUDA
  build lane and drivers in the image.
