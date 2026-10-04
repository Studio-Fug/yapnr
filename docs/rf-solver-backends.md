# RF solver backends

`yapnr.rf` steps its FDTD fields with one of three backends. They run the same scheme and,
where it matters, give the same numbers:

| Backend  | What it is                                                  | Precision        | Threads                 |
| -------- | ----------------------------------------------------------- | ---------------- | ----------------------- |
| `native` | blocks of steps in C, `yapnr/rf/fdtd/native/fdtd.c`         | float64, float32 | a pthread pool, any `N` |
| `numpy`  | the reference: about 70 array operations per step in Python | float64, float32 | one (numpy)             |
| `torch`  | the same operations as torch tensors on the CPU             | float64, float32 | intra-op, at most 4     |

**The default is `auto`: native float64 wherever its library loads, the numpy reference
otherwise.** Native float64 is numpy float64 bit for bit (below), so a run's results do not
depend on whether the library was there, only its time does: one evaluation is 13–16 times
faster than numpy on the shared Mac (4 threads) and 38–42 times on a C4D-16, and 3.4–6.3 times
faster than the torch float32 the cases ran on before ([measurements](#performance)). The
library ships in the yapnr wheel (one per platform) and so in the container image, Bazel builds
it for `//yapnr/rf` and its tests, and a checkout builds it with one command. The first
simulation of a process says on stderr, once, which
library runs (`yapnr.rf native FDTD: <path> (<instruction set>; <compiler>)`) or why numpy
runs instead (`yapnr.rf native FDTD: <reason>; numpy backend used (the same float64 values,
slower)`). With `YAPNR_RF_REQUIRE_NATIVE=1` a missing or refused library is an error instead;
cloud jobs that pay for native speed should set it.

## Choosing a backend

- **In code:** `Simulation(grid, structure)` runs `$YAPNR_RF_BACKEND`, else `auto`; or ask:
  `Simulation(..., backend="native", dtype=np.float64, threads=8)`. `sim.backend` names the one
  that runs.
- **In a spec:** `solver: {backend: auto, dtype: float64, threads: 4}` are the defaults;
  `backend` is `auto`, `native`, `numpy` or `torch`. Specs written before the native kernel say
  `backend: torch, dtype: float32` (the cases' setting then) and keep running torch unless the
  environment says otherwise.
- **From the environment,** over the spec of every problem (`yapnr.rf.cases run`, the driver,
  validation):

  | Variable                  | Meaning                                                                                  |
  | ------------------------- | ---------------------------------------------------------------------------------------- |
  | `YAPNR_RF_BACKEND`        | `auto`, `numpy`, `torch` or `native`                                                     |
  | `YAPNR_RF_DTYPE`          | `float64` / `f64` or `float32` / `f32`                                                   |
  | `YAPNR_RF_THREADS`        | the native thread pool (default: the spec's `threads`); caps torch's threads (at most 4) |
  | `YAPNR_RF_FDTD_LIB`       | the library to load, before the package's own, Bazel's runfiles and an installed wheel's |
  | `YAPNR_RF_REQUIRE_NATIVE` | `1`: no fallback; a missing or refused library raises                                    |
  | `YAPNR_RF_TBLOCK`         | native schedule: `auto` (default), `0` sweeps, `N` steps per pass                        |
  | `YAPNR_RF_CACHE_MB`       | the cache `auto` plans for (default: the CPU's last-level cache)                         |
  | `YAPNR_RF_ROWS`           | rows of the box per work item (default 8)                                                |

  `native` chosen by the environment over a spec that names `torch` runs float64 unless
  `YAPNR_RF_DTYPE` says otherwise (the specs' float32 was chosen for torch's speed), and without
  a usable library runs the spec's own backend and precision (torch float32, 4–7 times faster
  than numpy float64), saying so once. An exact problem (`Problem(exact=True)`, the gradient
  tests) runs float64 on `auto` unless the arguments or the environment name `numpy` or
  `native`: the same bits either way.

  ```bash
  YAPNR_RF_THREADS=16 YAPNR_RF_REQUIRE_NATIVE=1 python -m yapnr.rf.cases run divider --out runs/d
  ```

`Problem` logs the backend, precision and threads it runs (`FDTD backend: native float64, 4
thread(s) (libyapnr_fdtd.so, x86-64 avx512f)`), and `Problem.describe()` (the run's provenance) records
the backend that actually ran, the threads and, for native, the library's file name and sha256,
the sources' sha256 it was built from, the compiler and the instruction set its sweeps use. The
pool never takes more threads than the process may use (its CPU affinity); on a shared machine
keep `YAPNR_RF_THREADS` at the free cores: a thread waiting at a phase's barrier spins for about
50 µs, yields for up to a millisecond and then sleeps.

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

The bits depend on the build, so the build is checked rather than trusted. `fdtd.c` has
`#pragma STDC FP_CONTRACT OFF` (clang's default `-ffp-contract=on` would otherwise fuse about
240 multiply-adds into a library built without the flags; the fields then differ from numpy by
about 1e-15) and does not compile with fast-math. At load time the loader runs a probe compiled
into the library, in the instruction-set variant its sweeps use, with inputs the compiler cannot
see: x·y − fl(x·y) must be 0 (no fused multiply-add) and (u + v) − u must be 0 for v below u's
ulp (no reassociation), in float64 and float32. A library that fails is refused, as one built
with `-ffp-contract=fast` (which overrides the pragma) or by gcc in a GNU mode (which ignores
it). Every build records the sha256 of the sources (`build` with `-DYF_SRC_SHA`, Bazel through
a generated header), and the loader refuses a library built from other sources than the ones
beside the module (a stale build), as it refuses one with another ABI or other structure sizes.
Under `bazel test` the loader looks only in the runfiles, never in the source tree.

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
- 1 to 4 threads, both schedules, float64 and float32;
- backend selection: `auto` with and without a library, the environment's overrides, the
  fallback's one line, `YAPNR_RF_REQUIRE_NATIVE`, an installed wheel's library;
- the loader's refusals (stale sources, `-ffp-contract=fast`, fast-math) and the C side's
  bounds checks (a pass longer than 32 steps, a work item larger than its scratch: error -4).

`tests/unit/rf/test_native_identity.py` compares the sha256 of whole evaluations (steps, objective
values, design gradients, S-parameters) for every round-2 solver option on and off: the
copper-edge correction (its ε and μ factors, the extra gradient probes on the corrected edges
and its time-step bound over the dense-pattern library, diagonal patterns included) and the
static and modal port sources, on the divider and the antenna (smoke grids) and on a design of
one-pixel diagonal lines every three pixels (the time step's worst pattern) on the divider's
grid; also the Wilkinson-type combiner (a lumped resistor and its absorbed-power requirement)
and the reactive copper sheet. Native float64 on one thread with the sweeps and on three threads
with 3-step passes equals numpy float64; native float32 equals numpy float32; float32 is within
1e-5 (objective values) and 2e-5 (gradients) of float64. The same matrix on the cases' full
grids is in the benchmark notes below.

float32 itself is validated as torch float32 is: S-parameters within 1e-4 of float64, and on
the tiny production-settings spec the objective values within 1e-5 and the gradients within
2e-5 relative (the test's bounds; measured 1.5e-8 and 0.5–2.4e-6, torch float32 0.6e-8 and
0.6–2.0e-6). Over a full-size run the gradients drift further: on the divider at refine 2
(0.80 M cells, 24,965 steps) native float32 against float64 gives objective values within
5.7e-7 and per-objective gradients within 0.85e-5 to 1.2e-4 (5.6e-5 overall, cosine within
1.5e-9 of 1), the same order as the torch float32 the cases use (9.3e-7; 1.0e-5 to 4.8e-5,
3.1e-5 overall). That is fine for the optimizer's steps and not for gradient checks, so float64
is the default. The finite-difference gradient tests run on the default backend (native under
Bazel; `bazel test //tests/unit/rf:test_pipeline_gradient_numpy` and its siblings run them on
numpy) with the same numbers either way: relative errors against Richardson differences from
6.6e-11 to 2.2e-10 for most pipeline variants, 2.0e-9 with a reference impedance and 4.2e-8
for the absorbed-power requirement, and 1.9e-9 to 4.2e-8 (the S21 phase) in
`test_adjoint_gradients`, where the difference quotient's truncation dominates. Their
assertions (1e-6 and 1e-5) leave 24 times that margin or more. The tests pass on macOS arm64
(Apple clang), Linux arm64 (gcc 13; also under ThreadSanitizer, with no reports) and Linux
x86-64 (gcc 13): on a C4D's Zen 5 with the AVX-512 sweeps, and with the baseline sweeps.

## Building the library

The library is one C11 file and a header, built with any C compiler:

```bash
bazel build //yapnr/rf:libyapnr_fdtd.so          # what //yapnr/rf and its tests carry
python -m yapnr.rf.fdtd.native_kernel build     # a checkout: -> yapnr/rf/fdtd/native/libyapnr_fdtd.*
python -m yapnr.rf.fdtd.native_kernel status    # what backend "native" loads
```

- **macOS arm64:** Apple clang; NEON. Bazel builds it for macOS 11 and later.
- **Linux x86-64:** gcc or clang. The sweeps are compiled three times (`target_clones`: AVX-512,
  AVX2, baseline) and the loader picks the variant the CPU supports (`status()["isa"]`).
- **Linux arm64** (C4A): gcc or clang; NEON.

Bazel builds the library with the host's C toolchain as part of `bazel build //...`, records the
sources' sha256 through a generated header (`//yapnr/rf:fdtd_src_sha`) and puts it in the
runfiles of `//yapnr/rf`, so every test and binary that depends on the package runs native by
default. On Linux it links nothing but libc (the toolchain's default libraries, libstdc++ among
them, are off). `test_native_kernel` and `test_native_identity` run on that library and fail
rather than skip without it (`YAPNR_RF_REQUIRE_NATIVE`). Where Bazel has no native `cc_binary`
(Bazel 9 moved it to rules_cc) the target is an empty filegroup and everything runs numpy until
rules_cc is added. The C source also travels with the package wherever it goes (a checkout, a
job bundle, the runfiles, a `bazel_dep`, the wheel), so `build` works there.

### The wheel

The yapnr wheel carries `yapnr.rf` with its library at `yapnr/rf/libyapnr_fdtd.so` (where the
loader looks beside the package) and the C sources, so it is one wheel per platform, tagged for
the platform Bazel built it on (`release/wheel.bzl`):

| Platform     | Wheel                                             | Built                                        |
| ------------ | ------------------------------------------------- | -------------------------------------------- |
| Linux x86-64 | `yapnr-X.Y.Z-py3-none-manylinux_2_34_x86_64.whl`  | the image workflow, on `ubuntu-24.04`        |
| Linux arm64  | `yapnr-X.Y.Z-py3-none-manylinux_2_34_aarch64.whl` | the image workflow, on `ubuntu-24.04-arm`    |
| macOS arm64  | `yapnr-X.Y.Z-py3-none-macosx_11_0_arm64.whl`      | locally (`bazel build //release:wheel.dist`) |

glibc 2.34 is the newest symbol version the library needs (`pthread_create` in libc); the image
workflow checks that and that it needs nothing beyond libc. Releases attach both Linux wheels.
`tests/unit/release/test_wheel.py` checks the tag, the package's files and that the wheel's
library loads with the loader's checks.

### The container image

The image installs the wheel of its own architecture (`docker/yapnr/Dockerfile`), so its Python
loads the library from site-packages and runs native float64 by default; the image's smoke test
(`tools/image/rf_native_smoke.py`) checks that it loads, is the default and steps fields
bit-identically to numpy, also as another UID on a read-only root file system. The image has no
C compiler and needs none. On a Mac, `tools/image/build_local.sh` compiles the library for the
image's Linux architecture in a container and puts it into the Mac's wheel
(`tools/release/linux_wheel.py`).

RF cloud jobs run a job bundle's sources on `PYTHONPATH` (`tools/exp/rf_stage_plan.py`); the
loader then also looks at the installed wheel's library, and uses it when the bundle's C sources
are the ones it was built from (the sha256 it records). A bundle whose C source differs from the
image's runs numpy instead, says so, or with `YAPNR_RF_REQUIRE_NATIVE=1` stops at once; such a
job can still ship a library of its own and point `YAPNR_RF_FDTD_LIB` at it.

## Performance

### The default, measured on the integrated branch

One evaluation (forward and adjoint runs, gradients; the optimizer's unit per iteration) of a
gray design on the cases' full grids, round-2 settings (copper-edge correction, its time step
over the diagonal patterns, modal source), line calibrations cached, seconds. `auto` is the
default schedule; the Mac ran at most 4 threads, niced, at a load of 4–6.

| Grid (cells)                  | numpy f64, 1 thread | torch f32, 4 threads | native f64, 1 / 4 / 16 threads | native f32 |
| ----------------------------- | ------------------- | -------------------- | ------------------------------ | ---------- |
| C4D-16: divider (0.20 M)      | 85.5                | 12.4                 | 11.2 / 4.09 / **2.24**         | 1.96       |
| C4D-16: antenna (0.26 M)      | 147                 | 23.2                 | 21.0 / 6.78 / **3.70**         | 3.15       |
| C4D-16: diagonal design       | 114                 | 16.3                 | 14.6 / 5.00 / **2.68**         | 2.31       |
| C4D-16: divider, refine 2     | –                   | –                    | – / – / **17.3** (sweeps 30.5) | 7.67       |
| C4D-16: 60 GHz column, 1.79 M | –                   | –                    | – / – / **126**                | 55.5       |
| M4: divider                   | 72.3                | 25.1                 | 11.8 / **5.07** / –            | 5.36       |
| M4: antenna                   | 147                 | 39.0                 | 26.9 / **11.4** / –            | 6.56       |
| M4: diagonal design           | 100                 | 36.5                 | 14.2 / **6.20** / –            | 4.45       |
| M4: divider, refine 2         | –                   | –                    | – / **55.5** / – (sweeps 60.5) | 20.7       |

- **Against the cases' former torch float32:** 5.5–6.3 times faster on C4D-16, 3.4–5.9 times on
  the Mac at 4 threads; against numpy 38–42 times (C4D) and 13–16 times (Mac). On each machine
  every float64 row of a grid has the same objective value t, numpy's included.
- **The schedule:** at 0.20 M cells `auto` runs the sweeps on the C4D (the box fits its 32 MiB
  L3) and 7-step passes on the M4 (6–13 % faster than sweeps there); at 0.80 M cells the passes
  win on both (C4D 17.3 against 30.5 s, M4 55.5 against 60.5 s). On the C4D the antenna's
  8-step passes are within 10 % of the sweeps either way.
- **The library behind the C4D rows is the one CI built** into the linux/amd64 wheel (gcc 13,
  AVX-512 sweeps), run from a job bundle of the same commit; `test_native_kernel` and the
  gradient tests passed on it there.
- **The full-size identity matrix** (`test_native_identity`'s twelve configurations on the full
  grids: divider, antenna, diagonal design × edge correction × port source): native float64
  equal to numpy float64 by sha256 in every configuration on both machines (C4D: 1 and 16
  threads, sweeps and `auto`; M4: 4 threads, both schedules), native float32 equal to numpy
  float32 (M4), and float32 within 2.0e-6 of float64 in the objective values and 1.2e-5 in the
  worst gradient.

The rest of this section is the rf-kernels benchmark (before the merge with round 2's review
fixes; the kernel is the same).

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
last-level cache, passes sized to that cache otherwise, and sweeps for any run whose probes take
more than 0.25 DTFT samples per cell and step (probe edges × frequencies / decimation / cells):
in a pass a row's samples are accumulated by the thread that updated the row, so a flux section
on a few rows keeps the other threads waiting. The line calibrations (decimation 1, 105
frequencies: 7.7 samples per cell and step) took 43.1 s in passes and 8.9 s in sweeps on the
divider (M4, 4 threads); the optimizer's runs (about 0.009) are faster in passes. Handing out 8
rows per work item
instead of 2 made the Mac about 20 % faster on 4 threads (fewer claims on the shared counter).
The barrier that sleeps after a millisecond (and scratch allocated once per pool) costs nothing
measurable: on the shared Mac at 6 threads, interleaved against the previous build, the divider
ran at 0.34 ms per step both ways, the 1.79 M-cell grid at 3.97 against 4.05 ms and the
0.50 M-cell grid at 0.83 against 0.97 ms (medians of three); one divider evaluation took 4.5 s
against 5.0 s, with the same bits.

### Larger grids

With 8 rows per work item and the `auto` schedule, on another Spot `c4d-highcpu-16`: one
evaluation (forward and adjoint runs, gradients), seconds. The 60 GHz grids are synthetic,
built only for timing: a series-fed column of two patches with its feed on a 0.127 mm laminate
(εr 3.0), 58–63 GHz, with the starting design the two patches themselves.

| Grid                 | Cells  | Steps  | numpy float64 | torch float32, 4 threads | native float64, 1 / 4 / 8 / 16 threads | native float32, 16 |
| -------------------- | ------ | ------ | ------------- | ------------------------ | -------------------------------------- | ------------------ |
| divider              | 0.20 M | 12,484 | 87.4          | 12.4                     | 10.8 / 3.91 / 2.47 / 2.22              | 1.99               |
| antenna              | 0.26 M | 16,353 | 147           | 23.3                     | 19.5 / 6.75 / 4.03 / 3.66              | 3.08               |
| 60 GHz column, 50 µm | 0.50 M | 23,738 | 421 \*        | 51 \*                    | – / – / 12.0 / 11.0                    | 6.84               |
| 60 GHz column, 25 µm | 1.79 M | 41,929 | 2,960 \*      | 420 \*                   | – / – / – / 124                        | 53.9               |

\* The forward run's milliseconds per step (12–60 steps) times the evaluation's steps.

- **Memory-bound from about 1 M cells.** At 1.79 M cells native float64 takes 2.39 ms per step
  on both 8 and 16 threads (1.3 ns per cell-step). The 2-step passes are 1.9 times faster
  than plain sweeps (4.46 ms), and float32 halves the time again (1.11 ms).
- **Memory:** an evaluation peaks at about 1.07 GB at 1.79 M cells (on the Mac and on the
  C4D). Most of it is outside the field arrays.
- **Exactness on these grids:** native float64 equals numpy bit for bit on all four grids,
  through 60–200 steps with sources and probes, both in sweeps and in passes. On the divider
  and the antenna the whole evaluation (values and gradients) is equal at every thread count.
- **Mac against the C4D:** float64 results differ by about 1e-12 relative at most. The numpy
  reference differs between the two machines by the same amount.

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
- **Hardening:** `yf_run_block` checks every count, work item and the pass length against the
  structures and its limits (error -4), and allocates each thread's scratch once per pool
  (error -5 when out of memory) instead of aborting inside a worker.
- **Bazel:** the library is declared through `tools/bazel/native_cc.bzl` (buildifier lints
  `yapnr/rf`, unlike `hardware/`, and would add an unresolvable `@rules_cc` load to a direct
  `cc_binary`). Where Bazel has no native `cc_binary` (Bazel 9) the macro declares nothing, so
  `//yapnr/rf`, which downstream modules load, still loads; adding `rules_cc` is the fix then.

## GPUs

- **Metal (torch MPS): no.** MPS has no float64 (a TypeError), every float32 operation costs
  about 68 µs of dispatch, and the GPU shares the CPU's memory bandwidth (about 87 GB/s on the
  M4), so it cannot beat the CPU kernel on these grids.
- **OpenCL: no.** Deprecated on macOS, and Apple GPUs have no float64.
- **CUDA (G2, L4): deferred** until the 60 GHz grids are sized. An L4 has about 300 GB/s, and
  CUDA keeps the bits with `-fmad=false`, so the same exact design would carry over. For grids
  of 10 M cells or more it is plausibly 3–4 times a C4D-16 per VM, but it needs GPU quota, a CUDA
  build lane and drivers in the image.
