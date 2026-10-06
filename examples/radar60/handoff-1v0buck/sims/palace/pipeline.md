<!-- markdownlint-disable -->

# Palace mesh and configuration pipeline (P1 + P2)

Date 2026-10-04 (UTC). Follows [design.md](design.md) §3.1–3.2 and §4. Evidence labels:
**[D]** analytical or estimated. Nothing here is a solver result: Palace has not run, and nothing
is measured. GCP spend for this step: **$0.00**, logged in `gcp-spend.md` and
`../radar60/gcp-spend.md`.

## 1. What was built

Branch `claude/palace` of yapnr, worktree `yapnr-wt/palace`, four commits (not pushed):

| Commit    | Content                                                                                                                                                                                                                  |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `776bca6` | `third_party/palace/`: Palace's `config-schema.json` (schema 2-1-0) at b797ea8, unmodified, with Palace's LICENSE and NOTICE; THIRD_PARTY.md entry with checksums                                                        |
| `d858eab` | `yapnr/rf/planar/`: the `yapnr-planar-v1` document (`model.py`), the radar60 stack (`stackups.py`), the cleaner (`clean.py`), the adapters (`adapters.py`); 24 tests                                                     |
| `44b517e` | `yapnr/rf/palace/`: the Gmsh OCC builder (`mesh.py`), the config writer (`config.py`), the offline schema check (`schema.py`), the validation models (`validation.py`), `python -m yapnr.rf.palace` (`cli.py`); 18 tests |
| `19b5c6f` | `docs/rf-palace.md` (user guide) and its entry in the docs index                                                                                                                                                         |

**Checks.**

- prek is clean on every file of these commits, including the privacy scan.
- `tools/check_test_wiring.py` passes.
- All 42 tests pass in the FEA environment (gmsh 4.15.2, shapely 2.1.2). With the system Python
  3.9 alone, 22 pass and 20 skip (they need shapely or gmsh).
- Bazel was not run: another session's Bazel server was busy on this worktree, and AGENTS.md
  allows one server at a time. The BUILD files follow `//yapnr/rf/coupons`. The tests that need
  shapely or gmsh are `manual` under Bazel, as `test_xsec` is there.

**Shared branch.** A parallel session committed the P0/P3 work to the same branch:
`docker/palace`, `tools/exp/palace_plan.py`, `palace_job.py` and the "Task images (Palace)"
docs, commits a38695a..fcb2a08. Its `docker/palace/Dockerfile` edit is still uncommitted in the
worktree. My commits name their paths explicitly and touch none of its files.

### Pieces, as specified in the design

- **Planar description.** It covers:

  - stack slabs, with an optional board outline;
  - conductor layers with a copper model: `pec`, `sheet` (σ/K², t) or `solid`;
  - copper per (layer, net) with exact arcs (`{"mid": [x, y]}` ring items);
  - vias;
  - wave and lumped ports, each with a reference plane, a direction and a reference layer;
  - the domain box, with a boundary per face;
  - provenance and features.

  `port_geometry` derives each wave-port face (±0.8 mm, from the reference layer to 0.7 mm above
  L1, cut at the domain edge and halfway to a neighbour), the de-embedding offset and the
  voltage path. `geometry_hash` covers what a solver sees.

- **Importers.**
  - `line_model`: microstrip (MSL), or GCPW with via fences.
  - `patch_from_record`: from the rfmacro record `rfm1-n.json`. It has two variants: `w` (the
    board runs into the wall, wave port) and `finite` (lumped port).
  - `from_feedmodel`: the `prep.py` JSON, i.e. exactly the openEMS geometry.
  - `from_kicad`: a region of a zone-filled `.kicad_pcb`, read with `yapnr.fab.board`'s
    S-expression reader, without pcbnew. It reads zone fills, tracks, arcs, vias and via pads;
    footprint pads are not read.
- **Cleaner.**
  - It merges vertices closer than 2 µm and drops collinear vertices. Tangent points are kept, so
    a refit arc starts where the straight edge ends.
  - It refits chord runs as exact arcs, within 1 µm.
  - It reports the XOR area it moved.
- **Gmsh builder.**
  - Air box, slabs, sheets, solid copper, via cylinders (removed), and port faces.
  - Fragmenting runs in two passes. One pass returned overlapping solids of negative volume when
    a lumped port touched the window plane.
  - Physical groups: `air`, `diel:*`, `port:*`, `cond:<layer>:<net>`, `via:<net>`, `wall:<face>`.
  - Sizes: 0.04 mm at copper edges, growing by 0.35 per mm of distance, capped at λ/6 at 70 GHz
    (air 0.71 mm, RO4835 0.38 mm) and at 1.5 × the slab thickness (core 0.15 mm); port faces at
    0.1 mm.
  - With one thread, a model meshes identically every time.
  - A Netgen optimization pass runs, except on a model with an embedded face: gmsh 4.15.2's
    Netgen optimizer segfaults on `patch-finite`.
- **Config writer.**
  - **Driven:** a uniform sweep, or an adaptive sweep (`AdaptiveTol`). An AMR configuration
    refines at a few points and saves the adapted mesh; a separate sweep configuration then runs
    on that mesh.
  - **BoundaryMode:** the mode on a port face of the 3D mesh (`Solver.BoundaryMode.Attributes`).
  - **Boundary mapping:**
    - Materials.
    - PEC for the floor, the vias and `pec` copper.
    - Conductivity for copper: σ/K² with K 1.806 (17.78 MS/m), Thickness 0.035, External false.
    - Absorbing.
    - WavePort, with Offset, VoltagePath and Excitation = Index.
    - WavePortPEC on the walls behind ports.
    - LumpedPort with R = z0 and Direction -Z.
  - **Schema check:** the stdlib draft-07 validator agrees with `jsonschema` on all 90
    configurations in Palace's examples and tests, and on 2,610 random mutations of them.

## 2. Check against KiCad

`from_kicad` was run on the zone-filled macro board (`rf-uniform/boards/asis/rfm1-n`, U1 frame
(100, 100), F.Cu). It was compared with the `tx12-A` feed model inside the Palace box, east of
P0, where `prep.py` used the board's copper:

| Net | XOR (cleaned) | Edge length | Mean offset |
| --- | ------------- | ----------- | ----------- |
| GND | 0.0043 mm²    | 70 mm       | 0.06 µm     |
| TX1 | 0.0035 mm²    | 29 mm       | 0.12 µm     |
| TX2 | 0.0040 mm²    | 29 mm       | 0.14 µm     |

- **Vias:** all 55 coincide (0.0 µm).
- **Before via pads were read:** the GND XOR was 0.031 mm². KiCad's fill polygons do not include
  the via's own pad, which pokes out of the pour finger at (7.25, 5.47).
- **The design's ≤ 0.001 mm² target is not met in absolute terms.** The residue is a sub-µm
  offset spread over long edges, from `prep.py`'s `simplify(0.001)` and hole splitting and from
  the arc refit. I propose stating the criterion as a mean offset of ≤ 0.5 µm, or as an XOR per
  mm of edge, for the owner to decide.
- **Cleaning the feed model** moved 0.0076 / 0.0021 / 0.0047 mm² (GND / TX1 / TX2). That is
  mostly exact arcs replacing `prep.py`'s chords, i.e. moving towards KiCad's true arcs.

## 3. The validation models

These are in `models/<case>/` (56 MB): `model.json`, `mesh.msh` (msh 2.2 binary), `mesh.json`,
and the configs. All configs pass the schema check. The DOF counts are estimated from the edge
and face counts [D].

| Case               | Tets    | Nodes  | DOFs p2 / p3  | γ min / p01 / median | Mesh time (1 thread) |
| ------------------ | ------- | ------ | ------------- | -------------------- | -------------------- |
| line-msl-5mm       | 30,381  | 6,437  | 0.20 / 0.59 M | 0.20 / 0.46 / 0.79   | 1.5 s                |
| line-msl-10mm      | 53,282  | 11,124 | 0.36 / 1.03 M | 0.23 / 0.46 / 0.80   | 2.6 s                |
| line-gcpw-5mm      | 81,136  | 17,595 | 0.55 / 1.58 M | 0.16 / 0.44 / 0.77   | 5.4 s                |
| line-gcpw-10mm     | 143,771 | 30,976 | 0.98 / 2.80 M | 0.12 / 0.44 / 0.78   | 10.3 s               |
| patch-w            | 142,043 | 25,759 | 0.91 / 2.66 M | 0.16 / 0.47 / 0.81   | 7.9 s                |
| patch-finite       | 189,826 | 31,994 | 1.21 / 3.52 M | 0.18 / 0.35 / 0.76   | 4.4 s                |
| tx12-A (sliver)    | 241,894 | 49,538 | 1.62 / 4.65 M | 0.14 / 0.45 / 0.77   | 16.3 s               |
| tx12-B (no sliver) | 232,944 | 47,925 | 1.56 / 4.48 M | 0.15 / 0.45 / 0.77   | 17.2 s               |

All eight build in 55 s on one Mac thread, with a 0.73 GB peak. No element has SICN ≤ 0.

**Configs.**

| Case  | Uniform / sweep band      | Adaptive tolerance | Excited port | AMR points              | Other                            |
| ----- | ------------------------- | ------------------ | ------------ | ----------------------- | -------------------------------- |
| Lines | 54–70 GHz, 0.5 GHz step   | 1e-3               | P1           | 62 GHz                  | BoundaryMode at 1, 30 and 62 GHz |
| Patch | 58–66 GHz, 0.05 GHz step  | 1e-4               | P1           | 61.0, 61.9 and 62.8 GHz |                                  |
| tx12  | 54–70 GHz, 0.025 GHz step | 1e-4               | TX1.P0       | 59, 60 and 61 GHz       |                                  |

All cases use order p = 2, MaxIts 6, Tol 1e-2 and MaxSize 4 M DOF.

**Deviation from the design (feed domain).** The design took openEMS's _inner_ box as the Palace
domain. I used the PML interior of the 27 µm openEMS run instead (`tx12-A-e1-fine`, the mesh
where A and B pair), because that is the region openEMS actually solves. `fit_box` then moved
the walls:

| Wall | Move      |
| ---- | --------- |
| xmin | −0.025 mm |
| xmax | +0.205 mm |
| ymin | −0.480 mm |
| ymax | +0.210 mm |

The resulting box is x 4.397–11.029, y 0.371–7.278 mm, with air up to 1.408 mm. Every via is now
either at least 50 µm inside a wall or wholly outside it, and no copper vertex sits within 50 µm
of a wall. The search prefers moving a wall outward, because the walls of the inner box, or of
the PML interior, cut fence rows. Moving inward first had left a 20 µm GND strip under TX2.

Port offsets with this box:

| Ports          | Offset   | Face half-width                                |
| -------------- | -------- | ---------------------------------------------- |
| TX1.P0, TX2.P0 | 0.803 mm | 0.65–0.80 mm                                   |
| TX1.P1, TX2.P1 | 0.792 mm | 0.65–0.80 mm; TX2.P1 0.687 mm on the east side |

In openEMS, the band between the inner box and the PML is PEC copper. In Palace, the same copper
is the L1 sheet [D: negligible beyond the fences].

**Render:** `figs/tx12-A-mesh.png` shows the L1 surface mesh, a zoom on the TX1 finger with the
3.49 mm sliver, and the TX1.P0 port-face mesh. It is ready for the gallery.

## 4. Local Palace run

Not done. A Mac arm64 build is not cheap here: there is no MPI, CMake or gfortran, the
superbuild needs MFEM, hypre and the solvers, and the shared Mac was at load 4–20. The parallel
session's x86 image (Cloud Build pending in `gcp-spend.md`) runs `palace --dry-run` in every
task. That is where the configs first meet Palace.

## 5. For the validation stage

1. **Bundle and prepare.** A `palace_plan.py` job can mesh in the task:

   - inputs `yapnr` (the package tree) and `third_party`;
   - `prepare = ["-m", "yapnr.rf.palace", "case", "models/<case>.json", "--out", "{out}"]`;
   - `config = "{out}/palace-amr.json"`.

   The image has gmsh 4.15.2, shapely 2.1.2 and numpy 2.5.3, the same versions as here. The
   Palace builds in this directory can be uploaded instead.

2. **AMR → sweep.** `palace_job.py` moves the output to `out/<id>/postpro`. The sweep job
   therefore needs `set = {"Model.Mesh" = "<amr out>/postpro/mesh.meshgz"}` (`.mesh` without
   zlib; the name comes from Palace's `geodata.cpp`).
3. **Check on the first `--dry-run` or smoke run:**
   - **BoundaryMode voltage path.** The BoundaryMode configs give the voltage path in 3D
     coordinates on the port face. It is unverified whether Palace maps such a path onto the
     extracted submesh.
   - **Port mode boundary conditions.** In the port's 2D mode solve, the walls are PEC
     (WavePortPEC) and a conductivity sheet crossing the face is a Robin edge. Palace's
     `waveportoperator.cpp` adds internal boundary elements for that case.
   - **Copper model equivalence.** Whether the Conductivity sheet is equivalent to openEMS's
     conducting sheet: line loss against 0.074 dB/mm [D].
4. **Not built yet:**
   - the `solid`-copper variants of the lines and of tx12;
   - the openEMS back end reading the same documents;
   - `results.py`: Touchstone output and renormalization to 50 Ω through Z_PV.

## Sources (accessed 2026-10-04 UTC)

- Palace at b797ea8: `docs/src/guide/boundaries.md` (wave ports, WavePortPEC, conductivity),
  `docs/src/guide/problem.md` (BoundaryMode from a 3D mesh), `scripts/schema/config-schema.json`,
  `palace/models/surfaceconductivityoperator.cpp` (`External` doubles the thickness),
  `palace/models/waveportoperator.cpp` (internal boundary edges on port faces),
  `palace/utils/geodata.cpp` (the `SaveAdaptMesh` name), `examples/cpw/mesh/mesh.jl` (port faces
  and groups): <https://github.com/awslabs/palace/tree/b797ea8060a52241cd9ab1176199f06af8585816>
- Gmsh 4.15 Python API (`occ.addCircleArc(..., center=False)`, `Constant` field, Netgen
  optimizer): <https://gmsh.info/doc/texinfo/gmsh.html>
- Hammerstad–Jensen roughness factor, as used by `rfmacro/closedform.py` and the openEMS runs
  (`radar60/stage2-rf.md` §7).
- Internal: `radar60/rf-uniform/em-baseline.md`, `rf-uniform/code/prep.py`, `feed_sim.py`,
  `kx.py`, `runs/tx12-A-e1-fine/result.json`; yapnr `claude/radar60`
  `examples/radar60/rf/openems/column_sim.py` and `generated/rfm1-n/rfm1-n.json`.
