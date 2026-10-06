<!-- markdownlint-disable -->

# Palace as the second EM solver: research and design

Date 2026-10-04 (UTC). Owner request (2026-10-04): "let's get the palace integration stood up alongside
stage 3". Role agreed with the owner: openEMS (FDTD, staircase mesh) stays the sweep workhorse. AWS Palace
(FEM) is the independent second solver for the critical sign-offs: the 60 GHz column, the BGA-to-GCPW
launch, and the full bank with the finite board and radome.

Evidence labels: **[D]** analytical estimate, **[S]** solver prediction. Nothing is measured. Statements
about Palace are taken from its source and documentation at the commit named in §1 and are cited.

Status: research and design only. No code is committed yet. The worktree
`yapnr-wt/palace` (branch `claude/palace`, from `origin/main` fb96642) exists and is unchanged.
GCP spend: **$0.00**.

## 1. Palace today

**Version.** The latest release is **v0.18.1 (2026-09-21)**; v0.18.0 came out on 2026-09-09
[P-CL]. Main is at `b797ea8` (2026-10-02). Palace is Apache-2.0, pre-1.0, and has made interface
changes in every recent release.

**Pin a main commit after PR 962, not v0.18.1.** PR 962 is unreleased ("In progress" in the
changelog). It fixes boundary terms that were being added to attributes outside their own boundary
whenever attributes with equal properties shared one material entry. The fix changes results for
wave-port system matrices, surface conductivity and rational impedance "for configurations with
several such attributes" [P-CL]. Our models have exactly that: several ports, and conductors per net.

v0.18.1 itself carries two fixes we depend on:

- **PR 886: wave-port S-parameters for hybrid modes are now unitary.** The microstrip/GCPW quasi-TEM
  mode is such a mode; before the fix, a lossless guide could report |S11| > 1.
- **PR 936: far-field units fixed.** `farfield-rE.csv` was off by a factor Lc/Z0.

### Features that matter for PCB mm-wave work

**Ports** [P-BC]

- **Lumped ports** can be internal and can have several elements, each with a `Direction`. The CPW
  example uses two elements, each at 2·Z0.
- **Wave ports** solve a 2D boundary-mode eigenproblem on the port face (MFEM SubMesh). The face
  must be planar and must lie on the _outer_ boundary.
  - `Offset` de-embeds the port to a reference plane.
  - `VoltagePath` fixes the polarity and enables Z_PV. It needs GSLIB.
  - `WavePortPEC` forces chosen edges of the port face to PEC.
- Palace's CPW example shows lumped ports reflecting much more than wave ports, and the difference
  shrinks with h/p refinement [P-cpw]. Validation therefore uses **wave ports**. Lumped ports are
  for internal excitations, such as the BGA ball.
- **Standalone 2D mode solve.** `BoundaryMode` (v0.17) runs the mode analysis on its own: γ, n_eff
  and Z on a 2D cross-section [P-cpw2d].

**Conductors** [P-BC], [P-ref]

- **PEC** can be applied to internal 2D sheets. `CrackInternalBoundaryElements` defaults to true, so
  the sheet is two-sided.
- **`Conductivity`** takes σ, an optional `Thickness` and `External`.
  - Without `Thickness`: Zs = (1+i)/(δσ), which assumes the conductor is much thicker than δ.
  - With `Thickness`: the finite-thickness Zs, which reaches the correct DC limit.
  - At 60 GHz, δ_Cu ≈ 0.27 µm, so the thick-conductor assumption holds.
- **`Impedance`** (R, L, C per square) and **`RationalImpedance`** (a rational Zs(s), new in v0.18)
  are also available, the latter for fitted roughness models.

**Open boundaries** [P-BC]

- Absorbing boundaries are **first order** (Sommerfeld) or **second order** (frequency domain only).
- **There is no PML** ("not yet implemented").

**Far field** (v0.15.0) [P-FF]

- Stratton–Chu integration gives r·E(θ, φ) in `farfield-rE.csv`. Sampling is set by `NSample` or by
  explicit `ThetaPhis`.
- The integration surface must be an **external** boundary, normally the absorbing box, and must not
  cross anisotropic materials.
- Gain and directivity are left to the user: compute them from |rE|² with the unit-incident-power
  port normalization. Check that normalization on Palace's half-wave dipole example before quoting
  any gain number.

**Adaptive frequency sweep** [P-AFS]

- It builds a projection-based ROM from a few full solves. `AdaptiveTol` of about 1e-3 is the
  suggested start for S-parameters.
- The docs say to validate it against uniform samples for each new model.
- In v0.18.1, wave-port mode solves inside the online sweep use a per-port ROM (PR 909).

**Adaptive mesh refinement** [P-model], [P-ref], [P-src]

- The error estimate is Zienkiewicz–Zhu flux recovery on B and D. A global indicator ends the
  iterations.
- Refinement is nonconformal on any mesh, or conformal on simplex meshes. It works for every problem
  type except transient.
- In a uniform driven sweep, the indicator is **summed over every solved frequency**. In an adaptive
  sweep, it is summed over the offline (full) samples only [P-src].
- So AMR should run on a narrow band around the feature, followed by a full sweep on the saved
  mesh. `SaveAdaptMesh` writes `.meshgz` in v0.18.1.

**Outputs**

- `port-S.csv`, `port-V/I.csv`, `domain-E.csv`, `surface-F.csv`, probes and `farfield-rE.csv`.
- ParaView VTU (SI units) or MFEM grid functions.
- `palace.json`: DOFs, element counts, solver iterations, timers, and peak memory per rank and node.
- `<config>_resolved.json`: every default made explicit. `--dry-run` produces it without solving
  [P-post], [P-src].
- Configs are validated at run time against an embedded JSON schema (draft-07,
  `scripts/schema/config-schema.json`).

**Meshes**

- Any MFEM format, plus Nastran and COMSOL.
- Palace's own Gmsh examples write **msh 2.2** with named physical groups [P-model], [P-cpw].
- Lengths are in units of `L0`; we use L0 = 1e-3 for millimetres.

**Build** [P-inst]

- **CMake superbuild.** Prerequisites are C++20, MPI, BLAS/LAPACK and CMake ≥ 3.24. The superbuild
  fetches METIS/ParMETIS, Hypre, MFEM 4.10, libCEED, LIBXSMM, GSLIB and nlohmann_json/fmt/Eigen.
- **Optional dependencies:** SuperLU_DIST (on by default), MUMPS, STRUMPACK, SLEPc/PETSc or ARPACK
  (eigen), SUNDIALS (transient), MAGMA, CUDA/HIP and cuDSS.
- **Spack:** `spack install palace +mumps ...`.
- **Containers:**
  - Built through `spack containerize`, which emits a Dockerfile or an Apptainer `.def`.
  - CI builds images from `.github/actions/build-container/spack_env`, but publishes them only to a
    private ECR repository and S3 bucket.
  - So there is **no public image** we can pull [P-ci], and we build our own.
- **GPU** is optional (CUDA/HIP builds, `Solver.Device = "GPU"`, cuDSS). It is out of scope: we have
  no GPU quota.

**MPI** [P-par], [P-run]

- Palace is MPI-first: `palace -np N config.json` wraps mpirun. OpenMP is off by default.
- **One VM:** ranks = physical cores, one model per VM.
- **Several VMs:** Batch can fill a hosts file and allow passwordless SSH between VMs
  (`requireHostsFile`, `permissiveSsh`) [GCP-B]. We defer this:
  - The quota is 64 Spot vCPU, so one c4d-64 is the ceiling anyway.
  - C4D has no RDMA, and multigrid/direct solves over TCP scale poorly.
  - yapnr.exp would need multi-VM task groups.

## 2. Prior art and what to reuse

| Source                                                                | What it does                                                                                                                                                                                                   | Reuse                                                                                                                                                                                                                                                                                               |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Palace `examples/cpw`, `cpw2d`, `antenna` [P-cpw], [P-cpw2d], [P-ant] | CPW with 2D PEC sheets _and_ thick metal; wave vs lumped ports; uniform vs adaptive sweep; BoundaryMode Z0/n_eff; half-wave dipole with an absorbing sphere and FarField                                       | Smoke test and calibration (regression references are in `test/data/regression`); the gain-normalization check; Gmsh size-field recipe (Distance → Threshold from the gap curves, `MeshSizeExtendFromBoundary = 0`). **No microstrip, patch or PCB example exists**: our validation fills that gap. |
| gplugins `palace` (MIT; 2ed97e7, 2026-10-01) [GP]                     | gdsfactory LayerStack → meshwell prisms → Palace JSON. Metal as PEC surfaces; "edge signals" become WavePorts; "internal signals" become LumpedPort `Elements` with `Direction`; physical name → attribute map | The JSON-mapping pattern, written fresh: no gdsfactory dependency                                                                                                                                                                                                                                   |
| meshwell (GPL-3.0; 6ab26f2, 2026-09-24) [MW]                          | Shapely → OCC 2.5D prisms, vertex snapping, arc refitting of polygonized curves, per-entity resolution specs, parallel CAD                                                                                     | Its ideas (per-entity sizing; refitting arcs on KiCad zone fills) in our builder. An optional backend later; GPL-3.0 is compatible with AGPL-3.0.                                                                                                                                                   |
| SQDMetal (Apache-2.0; ba5222d, 2026-09-30) [SQD]                      | Qiskit Metal → Gmsh → Palace driven and eigenmode; `port-S.csv` readers; notes on HPC (Slurm) builds                                                                                                           | Result readers; the wave-port mode sanity check                                                                                                                                                                                                                                                     |
| EMerge (GPL-2.0+; e23842b, 2026-09-29) [EM]                           | Python FEM with a PCB layouter (strip paths, vias, lumped elements); modal-port helper sizes the port face at 5× strip width, up to the air top                                                                | Port-face sizing heuristic                                                                                                                                                                                                                                                                          |
| KQCircuits (GPL-3.0; b3ed0ce, 2026-07-17) [KQC]                       | KLayout → Elmer, Sonnet and Ansys exports with gmsh, and a Singularity flow. **No Palace export** on main.                                                                                                     | Ideas only                                                                                                                                                                                                                                                                                          |
| yapnr FEA design (`docs/design/fea-integration.md`)                   | Already lists Palace 0.18.1 as the tier-3 driven solver. Palace's published validation is for superconducting chips only (arXiv 2511.01220, 2511.09041), not lossy PCB lines.                                  | The licence rule: Palace stays an external program                                                                                                                                                                                                                                                  |

## 3. The yapnr design

The design has three layers: one geometry description shared by both solvers, a Palace back end, and
cloud tooling that mirrors the openEMS path.

### 3.1 `yapnr/rf/planar`: solver-neutral layered structure (schema `yapnr-planar-v1`, mm)

The schema has these parts:

- **`stack`**
  - Dielectric slabs: `z0`, `z1`, `eps_r`, `tan_d`, an optional outline polygon for a finite board,
    and fill regions. The fill regions cover cases such as RO4450F filling the L2 windows.
  - Conductor layers: `z`, `t`, `sigma`, roughness `K`, and a `model` of `pec`, `sheet` or `solid`.
- **`conductors`**
  - Per (layer, net): polygons with holes.
  - Ring edges are segments or **exact arcs** `{arc: [start, mid, end]}`.
- **`vias`**: x, y, drill, layer span, net, and pads.
- **`keepouts`** and mesh hints:
  - `edge_h`, a gap rule, `h_max` by region, and a λ fraction.
  - `refine` boxes, which become Palace `Refinement.Boxes`.
- **`ports`**: name, kind (`wave` or `lumped`), net, layer, `at`, `dir` (±x/±y), reference-plane
  offset, `z_ref`, and a voltage path. For lumped ports also the ground side and elements.
- **`domain`**
  - The box: margins, air above and below.
  - A boundary per face: `abc1`, `abc2`, `pec` or `port`.
  - The far-field faces.
- **`provenance`**: source, generator version, and geometry hash.

There are three adapters and a cleaner:

1. **`from_feedmodel`**: the rf-uniform `prep.py` JSON (`tx12-*`, `col12-*`). This is the fast path
   for validation (c).
2. **`from_rfmacro`**: `geom.Path`/`Seg` with true arcs, windows, fences and patches. This feeds the
   column, the patch and the launch.
3. **`from_kicad`**:
   - The same pcbnew route as `kx.py`, run on a board after `kicad-cli pcb drc --refill-zones
--save-board`.
   - Inputs: a crop box, a net list and layers.
   - Alternative: KiCad's IPC-2581 export, which keeps nets, arcs and the stackup.
4. **Cleaner** (all adapters): snap at ≤ 1 µm, merge collinear vertices, and refit arcs on
   polygonized zone fills. Thousands of 5 µm chords would otherwise force tiny elements.

The geometry path is common-mode between the two solvers. Each export is therefore checked against
KiCad by area XOR, ≤ 0.001 mm². The rf-uniform models gave 0.0008 mm².

Later, an `openems.py` back end reads the same description, so every comparison uses one geometry.

### 3.2 `yapnr/rf/palace`: Gmsh builder, config writer, results

**`mesh.py`: the Gmsh OCC builder** (gmsh 4.15, GPL-2.0+, optional import as in `xsec`)

Geometry:

- Dielectric volumes are boxes or extruded outlines.
- Conductors are built in one of three ways:
  - `sheet`: surfaces at z.
  - `solid`: extruded by t and cut from the domain.
  - `pec`: a PEC bottom face.
- Via barrels are cylinders, or n-gon prisms as a fallback for robustness. Pads are surfaces.
- One `occ.fragment` call joins everything into a conforming mesh.

Physical groups:

- One per dielectric material, plus air.
- One per (layer, net) conductor, one per via net, one per port face, and one per outer face.
- `mesh.json` records the name → attribute map, element counts and quality.

Size fields:

- Distance from conductor curves feeds a Threshold field:
  - h_min = min(0.05 mm, w_min/4, gap_min/4).
  - At least 2 elements across the 0.1016 mm core under copper.
  - Growth 1.3.
- h_max ≈ λ_local/6 at p = 2: 0.38 mm in RO4835 and 0.7 mm in air at 70 GHz [D].
- Vias get 8–12 segments around the barrel.
- Tetrahedra only, linear geometry by default; second-order curved geometry is an option.
- Output is **msh 2.2 binary**. Gmsh runs with ≤ 4 threads.

Conductor model choice:

- `sheet` with `Conductivity` σ/K² (K = 1.81) and `Thickness` 0.035, internal. This is like-for-like
  with openEMS's conducting sheet.
- `solid` 35 µm for sign-off, which captures the gap capacitance that em-baseline §5 flagged.
- `pec` for quick runs.

**`config.py`: the Palace JSON writer**

- **Problem:** `Driven`, `BoundaryMode` or `Eigenmode`.
- **Model:** `Mesh`, L0 = 1e-3, and AMR settings: `Tol`, `MaxIts`, `MaxSize`, `UpdateFraction`,
  `Nonconformal`, `SaveAdaptMesh`.
- **Materials:** εr and tan δ.
- **Boundaries:** PEC, Conductivity, Absorbing (order), WavePort (`Offset`, `VoltagePath`,
  `WavePortPEC`), LumpedPort `Elements`, FarField, SurfaceFlux.
- **Solver:**
  - `Order`: 2 by default, 3 for the p-check.
  - `Driven.Samples`, `AdaptiveTol`.
  - Linear: GMRES with the default AMS/p-multigrid, `PCMatShifted`, and MUMPS or SuperLU as the
    fallback.
- Tests validate the output offline against Palace's `config-schema.json`, vendored and pinned to
  the image commit (Apache-2.0, recorded in THIRD_PARTY.md), with `jsonschema`. The image also runs
  `palace --dry-run` in the smoke test.

**`results.py`**

- Reads `port-S.csv`, `port-V/I`, `palace.json` and `farfield-rE.csv`.
- Writes Touchstone.
- **Renormalizes wave-port S to 50 Ω** using Z_PV. Palace's S is normalized to the port mode, while
  openEMS ports are referenced to 50 Ω.
- Finds notches and resonances, and computes gain from rE.

### 3.3 Cloud: `docker/palace`, `tools/exp/palace_plan.py`, `tools/exp/palace_job.py`

**`docker/palace/Dockerfile`**

- Base: Ubuntu 24.04, gcc-14 with gfortran, OpenMPI, OpenBLAS and ScaLAPACK from apt.
- Palace superbuild at the pinned commit (`PALACE_GIT_COMMIT_ID` set):
  - **On:** SuperLU_DIST, MUMPS, GSLIB (needed for `VoltagePath`), LIBXSMM, and ARPACK for
    eigenmode.
  - **Off:** SLEPc/PETSc, STRUMPACK, SUNDIALS, MAGMA, CUDA, OpenMP.
  - Leaving these out shortens the build.
- One `-march=x86-64-v3` variant. Unlike openEMS (where AVX-512 gained about 3 %), this lets the
  E2 builder run the smoke test, and LIBXSMM JITs at run time.
- The runtime stage adds python3 with the gmsh 4.15 wheel, numpy and shapely, so **the task meshes
  from the small planar JSON**.
- Tag: `palace:<commit7>-x86-64-v3`.

**`cloudbuild.yaml`**

- E2_HIGHCPU_8 (quota) with `NJOBS=6`: 8 GB RAM risks running out of memory at -j8.
- `timeout: 14400s`, and `--timeout` on the submit, because the default is 10 minutes.
- A separate `palace-deps:<hash>` stage, used with `--cache-from`, so a Palace bump rebuilds only
  Palace.
- Estimated 60–90 min, $1.1–1.6 at about $0.018/min [D].

**`palace_job.py`** (standard library only)

- Runs `mpirun -np R --bind-to core palace-x86_64.bin config.json`.
- Sets `OMPI_ALLOW_RUN_AS_ROOT=1` and `_CONFIRM=1`, because the task container runs as root.
- If 1 GiB of `/dev/shm` is too small, sets `OMPI_MCA_btl_vader_single_copy_mechanism=none`.
  Otherwise the per-campaign shm size becomes a backend option.
- An optional pre-step meshes the model: `{script}` builds the mesh and config, then Palace runs.
- Writes `out/ID.job.json`:
  - Exit code, wall and CPU time.
  - DOFs and elements per AMR iteration, linear iterations and timers, read from `palace.json`.
  - Peak memory as the sum over ranks, also from `palace.json`. `ru_maxrss` sees only one child
    process, so it would undercount.
  - CPU and AVX flags.

**`palace_plan.py`**

- Mirrors `openems_plan.py`: `image`, `digests`, `plan JOBS.toml`, `collect`.
- Uses the same `[runtime]` table: python `/opt/palace/venv/bin/python`, entrypoint `""`.
- JOBS keys: `ranks`, `models_per_vm` (default 1), `memory_gb`, `max_wall_s`, `families = ["c4d"]`.
- vm_vcpus = 2 × ranks × models_per_vm.
- Defaults:
  - c4d-highcpu-16 with 8 ranks for lines and the patch.
  - c4d-standard-16 (64 GB) with 8 ranks for the feeds and the AMR.
  - c4d-standard-32 or -64 through an instance policy for the bank.
- There is no checkpoint, so preempted tasks restart from the beginning. Runs are kept under about
  2 h. Only uniform sweeps can `Restart`.

## 4. Validation plan (acceptance: ≤ 1 % in frequency and ≤ 0.5 dB, or an explained difference)

All three cases follow the same rules.

**Same geometry.** Both solvers read one planar description. Like-for-like copper is a sheet with
σ/K² and t = 35 µm. Palace's box is openEMS's inner box with the lines running to the walls: the PML
becomes wave ports, with `Offset` back to the P0/P1 planes.

**Convergence in Palace has four checks:**

- **h:** AMR on a narrow band, 5–8 iterations, recording the quantity of interest against DOFs.
- **p:** a re-solve at Order 3 on the final mesh.
- **Sweep:** the adaptive sweep (`AdaptiveTol` 1e-4 near a resonance) against 5 uniform points.
- **Box size:** the absorbing boundary at λ0/4 against λ0/2, for radiating cases.

**Convergence in openEMS:** three meshes and a Richardson extrapolation. The new fine-mesh runs go on
GCP with `exact-endcriteria`.

**Reported per case:** DOFs, wall time, peak memory and $.

**(a) 50 Ω 4-mil lines.** Microstrip w 0.200, and GCPW w 0.200 / g 0.200 (48.8 Ω [D]), with sheet and
solid copper.

1. Palace `BoundaryMode` at 1, 30 and 62 GHz against the `xsec` quasi-static table (Z0 50.04 / 48.83 Ω,
   εeff 2.704 / 2.627) and Kirschning–Jansen dispersion [D]. Acceptance at 1 GHz: Z0 and εeff within
   0.5 %.
2. 3D lines of 5 and 10 mm with wave ports in Palace, and the same lines in openEMS. Compare εeff from
   the S21 phase difference, Z0, and loss against Wheeler's 0.074 dB/mm. Target: within 1 %, and loss
   within 0.01 dB/mm.

This case also checks the equivalence of the copper models (Palace `Conductivity` + `Thickness`
against openEMS's conducting sheet).

**(b) Calibrated patch (`patch-c-i30`: L 1.151, inset 0.30, on the window).** openEMS stage 2 gave a
|S11| minimum at 61.90 GHz, −26.9 dB, on the 25/40 µm mesh [S]. No fine-mesh openEMS patch run exists
yet.

- **Comparison geometry `-w`** (both solvers): the board extends to the south wall under the feed, so
  that Palace can use a wave port. openEMS runs it at 25/40, 17/27 and 12/20 µm.
- **Palace-only tie-back:** the original finite board with a 50 Ω lumped port, against stage 2.
- **Metrics:** the frequency of minimum |S11|, the edges of the RL ≥ 10 dB band, and |S11| where it is
  above −15 dB.
- The value of the −27 dB minimum is too sensitive to tell the solvers apart: about 1 % in Zin moves
  it by several dB. Compare complex Zin at f0 instead.
- **Information only:** broadside directivity and efficiency against openEMS NF2FF, after the dipole
  normalization check.

**(c) TX1 feed with the 3.49 mm GND sliver (`tx12-A` against `-B`, excitation TX1.P0).**

- **openEMS so far:** the notch at 56.95 / 59.75 / 59.95 / 59.83 GHz on the 40 / 27 / 20 / 27+8-core
  meshes [S].
- **Added in openEMS:** a 15 µm run of A and B, for a three-point extrapolation (about 60.0–60.5 GHz
  expected).
- **Palace:**
  - AMR with uniform samples at 59 / 60 / 61 GHz, then an adaptive sweep from 54 to 70 GHz on the
    adapted mesh, at p = 2 and p = 3.
  - Then the `solid` variant, to quantify the copper-thickness shift that openEMS cannot see.
- **Metrics:**
  - Notch frequency within 1 %.
  - |S21| and S11 at 62.05 and 63.8 GHz within 0.5 dB.
  - At 60.3 GHz (on the notch flank), compare at equal detuning from the notch: a 1 % shift moves
    |S21| there by several dB.
  - The TX1 phase difference between A and B.

**Run time and cost on C4D Spot.** These are estimates [D] until a Palace calibration campaign
measures them. The calibration covers Palace's cpw examples at 4, 8 and 16 ranks, plus case (a).
Assumptions:

- About 6.3 DOFs per tetrahedron at p = 2.
- 1–3 min per frequency per 2 M DOFs on 8 cores.
- Prices: c4d-highcpu-16 $0.151/h, c4d-highcpu-8 $0.077/h, c4d-standard-16 about $0.2/h (assumed;
  `yapnr exp plan` prints the catalog price).

| Case                                                       | Size         | VM                       | Wall           | $                           |
| ---------------------------------------------------------- | ------------ | ------------------------ | -------------- | --------------------------- |
| Image build                                                | –            | Cloud Build E2_HIGHCPU_8 | 60–90 min      | 1.1–1.6                     |
| Smoke and calibration (cpw ×3 rank counts, `--dry-run`)    | ≤ 0.5 M DOF  | c4d-highcpu-16           | 3 × 5–10 min   | ≈ 0.1                       |
| (a) 2D mode, 4 lines × 3 f                                 | ≤ 0.1 M      | c4d-highcpu-8            | < 5 min        | < 0.01                      |
| (a) 3D lines, 2 types × 2 lengths × 2 copper models        | 0.3–0.8 M    | c4d-highcpu-16           | 5–15 min each  | ≈ 0.2                       |
| (b) patch-w + tie-back, AMR + sweep, p = 2 and p = 3       | 1–3 M        | c4d-standard-16          | 0.5–1.5 h each | 0.3–0.8                     |
| (c) tx12 A and B, AMR + sweep, p = 2 and p = 3, + solid    | 2–5 M        | c4d-standard-16          | 1–3 h each     | 0.8–2.0                     |
| openEMS references (patch-w × 3 meshes, tx12 A/B at 15 µm) | 2–10 M cells | c4d-highcpu-8            | 0.2–1.5 h each | ≈ 0.3                       |
| **Total**                                                  |              |                          |                | **≈ 2.8–5.0 of the $8 cap** |

Spending rules:

- Every submit stays under $5.
- Campaigns run in waves of 16-vCPU VMs or fewer, under the shared 64-vCPU quota.
- Each campaign is logged here and in `radar60/gcp-spend.md` before it is submitted.

## 5. Risks and effort

| Risk                                                                                                               | Mitigation                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| Correctness fixes we depend on are unreleased (PR 962), and Palace is pre-1.0 with interface changes every release | Pin a main commit (≥ b797ea8). Vendor the schema at that commit. Re-pin at v0.19 and re-run (a) as the regression.    |
| No PML: first- and second-order absorbing boundaries reflect, which affects the patch and the bank                 | Second order on a box ≥ λ0/2 away; a box-size study; wave ports, not absorbing walls, terminate the lines             |
| Mesh blow-up from polygonized KiCad fills and OCC booleans with 100–200 vias                                       | Arc refitting and snapping; via n-gon fallback; HXT; per-region h_max; meshing time and element counts in `mesh.json` |
| GMRES stalls at a high-Q resonance (the notch) or with absorbing boundaries                                        | `PCMatShifted`; MUMPS or SuperLU fallback on c4d-standard (5–10× the memory)                                          |
| Build on E2_HIGHCPU_8: runs out of memory, or exceeds the 10 min default timeout                                   | -j6, a 4 h timeout, the cached deps stage; Batch VMs have no internet, so the build stays on Cloud Build              |
| MPI in the container: root user, 1 GiB shm                                                                         | `OMPI_ALLOW_RUN_AS_ROOT*`, vader single-copy off, a configurable shm size                                             |
| Spot preemption, and AMR cannot restart                                                                            | Runs ≤ 2 h; Batch retries; saved adapted meshes reused                                                                |
| Comparison artefacts: port references (modal against 50 Ω), sheet-model equivalence, shared geometry path          | Renormalize to 50 Ω via Z_PV; case (a) checks copper loss; the KiCad XOR check                                        |
| Far-field normalization or units                                                                                   | Check against the dipole example (D = 1.64) before quoting gain                                                       |
| The full bank with radome (about 10 λ0) may need 15–30 M DOFs, which is more than one VM                           | p = 3 with a coarser air mesh, symmetry planes, c4d-highmem-64; multi-node or GPU later (both out of scope now)       |
| Quota shared with other tracks                                                                                     | Plan waves; check before long runs                                                                                    |

**Effort, in agent-days:**

| Phase | Work                                                                                                  | Days       |
| ----- | ----------------------------------------------------------------------------------------------------- | ---------- |
| P0    | Image, Cloud Build and a smoke test on the cpw example                                                | 1          |
| P1    | Planar schema, three adapters, the cleaner, and tests                                                 | 1.5        |
| P2    | Gmsh builder, config writer, offline schema tests, and the `results` readers                          | 2          |
| P3    | `palace_plan` / `palace_job`, collect, and docs (`docs/cloud-experiments.md`, "Task images (Palace)") | 1          |
| P4    | Validation (a)–(c) and the report                                                                     | 2          |
| P5    | The launch, column and bank sign-off models                                                           | 2–3, later |

That is about **7.5 days to a validated solver**, then the sign-off models.

## Sources (accessed 2026-10-04 UTC)

- [P-CL] Palace changelog, main b797ea8: <https://github.com/awslabs/palace/blob/main/CHANGELOG.md> ;
  releases: <https://github.com/awslabs/palace/releases>
- [P-BC] <https://github.com/awslabs/palace/blob/main/docs/src/guide/boundaries.md>
- [P-ref] <https://github.com/awslabs/palace/blob/main/docs/src/reference.md> (finite-thickness Zs, ZZ
  estimator, far-field)
- [P-FF] <https://github.com/awslabs/palace/blob/main/docs/src/features/farfield.md>
- [P-AFS] <https://github.com/awslabs/palace/blob/main/docs/src/features/adaptive_driven_solver.md>
- [P-model] <https://github.com/awslabs/palace/blob/main/docs/src/guide/model.md>
- [P-post] <https://github.com/awslabs/palace/blob/main/docs/src/guide/postprocessing.md>
- [P-par], [P-run] <https://github.com/awslabs/palace/blob/main/docs/src/guide/parallelism.md> ,
  <https://github.com/awslabs/palace/blob/main/docs/src/run.md>
- [P-inst] <https://github.com/awslabs/palace/blob/main/docs/src/install.md>
- [P-ci] <https://github.com/awslabs/palace/blob/main/.github/workflows/publish-containers.yml> ,
  <https://github.com/awslabs/palace/blob/main/.github/actions/build-container/spack_env/spack.yaml>
- [P-src] <https://github.com/awslabs/palace/blob/main/palace/drivers/drivensolver.cpp> (estimate per
  frequency), <https://github.com/awslabs/palace/blob/main/palace/drivers/basesolver.cpp> (`palace.json`),
  <https://github.com/awslabs/palace/blob/main/scripts/schema/config-schema.json>
- [P-cpw], [P-cpw2d], [P-ant] <https://github.com/awslabs/palace/tree/main/examples> (cpw, cpw2d,
  antenna) and docs/src/examples/\*.md
- [GP] <https://github.com/gdsfactory/gplugins/tree/main/gplugins/palace>
- [MW] <https://github.com/simbilod/meshwell>
- [SQD] <https://github.com/sqdlab/SQDMetal> (SQDMetal/PALACE)
- [EM] <https://github.com/FennisRobert/EMerge> (src/emerge/\_emerge/geo/pcb.py)
- [KQC] <https://github.com/iqm-finland/KQCircuits> (simulations/export: ansys, elmer, sonnet)
- [GCP-B] <https://docs.cloud.google.com/batch/docs/reference/rest/v1/projects.locations.jobs> (TaskGroup
  `requireHostsFile`, `permissiveSsh`)
- Internal: `radar60/rf-uniform/em-baseline.md`, `radar60/stage2-rf.md` §3 and §7,
  `radar60/cloud-em/READY.md`, yapnr `docs/design/fea-integration.md`.
