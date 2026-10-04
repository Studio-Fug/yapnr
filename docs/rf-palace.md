# Palace models: planar structures, meshes and configurations

[AWS Palace](https://github.com/awslabs/palace) (Apache-2.0) is a 3D finite-element solver. In
yapnr it is the independent second solver of the RF sign-offs, next to openEMS (FDTD). openEMS
stays the sweep workhorse. Palace is used for the results that need a second method's agreement:
resonances, phase, launches and full arrays.

This page covers the pipeline that prepares Palace's inputs:

1. A **planar document** (`yapnr-planar-v1`) describes the layered structure, independent of the
   solver.
2. **Adapters** fill it from generated lines, from a generator record, or from a region of a
   zone-filled KiCad board.
3. The **Gmsh builder** meshes the document.
4. The **configuration writer** produces Palace's JSON, checked against Palace's own schema.

Palace itself runs in its task image on Google Cloud Batch (see
[Task images (Palace)](cloud-experiments.md#task-images-palace)). Nothing on this page runs
Palace.

## What is in the repository

| Path                            | Contents                                                                          |
| ------------------------------- | --------------------------------------------------------------------------------- |
| `yapnr/rf/planar/model.py`      | the document: validation, exact-arc rings, port faces, the geometry hash (stdlib) |
| `yapnr/rf/planar/stackups.py`   | the radar60 RO4835/RO4450F stack and the Hammerstad roughness factor              |
| `yapnr/rf/planar/clean.py`      | union, crop, vertex merging, collinear removal, arc refitting, XOR area (shapely) |
| `yapnr/rf/planar/adapters.py`   | lines, the radar60 patch, the radar60 feed models, KiCad board regions, `fit_box` |
| `yapnr/rf/palace/mesh.py`       | the Gmsh OCC builder and the mesh record (`mesh.json`)                            |
| `yapnr/rf/palace/config.py`     | Driven and BoundaryMode configurations                                            |
| `yapnr/rf/palace/schema.py`     | offline validation against `third_party/palace/config-schema.json` (stdlib)       |
| `yapnr/rf/palace/validation.py` | the validation models of the Palace plan                                          |

The document model, the configuration writer and the schema check need only the standard
library. The cleaner and most adapters need shapely. The builder needs gmsh, which is
GPL-2.0-or-later and is only ever imported when the builder runs. These tools run in the FEA
environment and in the Palace task image (gmsh 4.15.2, shapely 2.1.2, numpy 2.5.3), never under
Bazel.

```sh
python -m yapnr.rf.palace mesh model.json --out DIR          # model.json, mesh.msh, mesh.json
python -m yapnr.rf.palace driven DIR --band 54 70 0.025 --adaptive-tol 1e-4 --excite TX1.P0
python -m yapnr.rf.palace mode DIR --port P1 --freq 62       # 2D mode solve on a port face
python -m yapnr.rf.palace case model.json --out DIR          # mesh + all run configs in one step
python -m yapnr.rf.palace validate DIR/*.json                # against Palace's schema
```

## 1. The planar document

A document is JSON, in millimetres:

- **`stack.dielectrics`**: slabs `{name, z0, z1, eps_r, tan_d}`.
  - A slab can be limited to an `outline` ring, which makes a finite board.
  - Where two slabs overlap, the later one wins.
- **`stack.layers`**: conductor layers `{name, z, t, sigma, rough_k, model}`. The `model` field
  says how solvers represent the copper:

  - `pec`;
  - `sheet`: zero thickness at `z`, with the surface impedance of `sigma / rough_k²` and
    thickness `t`. This matches the conducting sheet that openEMS uses.
  - `solid`: extruded from `z` to `z + t` and removed from the domain. It is allowed only on a
    layer with nothing but air above it.

  `rough_k` is the Hammerstad roughness factor at the design frequency. For the radar60 L1
  (Rq 0.4 µm) it is 1.806 at 62.05 GHz.

- **`conductors`**: `{layer, net, polygons: [{outer, holes}]}`.
  - A ring is a list of points `[x, y]`.
  - An item `{"mid": [x, y]}` between two points makes that edge a circular arc through `mid`.
- **`vias`**: barrels `{at, drill, from, to, net}`. `from` and `to` are layers, or `zmin` for
  the domain floor.
- **`ports`**: `{name, kind, net, layer, ref, at, dir, width, z0, excite, face}`.
  - `at` is the reference plane on the line's centre.
  - `dir` is the direction the line runs from `at` into the model (`+x`, `-x`, `+y` or `-y`).
  - `ref` is the layer (or `zmin`) that the line refers to.
- **`domain`**: the box `[x0, x1, y0, y1, z0, z1]` and a boundary for each face (`xmin` …
  `zmax`): `abc1` or `abc2` (first- or second-order absorbing), `pec` or `pmc`.
- **`mesh`**: sizing hints for the builder.
- **`provenance`**: the source, the generator, and how much the cleaning moved the copper.
- **`features`**: named regions of interest, for probes and plots.

**Port faces.**

- A **wave port** is a rectangle on the wall behind `at`. The solver de-embeds the distance from
  the wall back to `at` (Palace's `Offset`).
  - Width: 0.8 mm either side of the line (eight 0.2 mm line widths). It is cut at the domain
    edge, and halfway to a neighbouring port on the same wall.
  - Height: from the reference layer to 0.7 mm above the signal layer.
  - The voltage path runs from the strip down to the reference. It sets the mode's polarity and
    gives Palace's Z_PV.
- A **lumped port** is the `width` × substrate rectangle across the dielectric at `at`.

`model.geometry_hash` covers what a solver sees: the stack, copper, vias, ports and domain, but
not the mesh hints or provenance. A mesh record and every result can name the geometry they
belong to.

## 2. Adapters and cleaning

| Adapter                                        | Input                                                                           | Use                                                                                                                                                                                                                             |
| ---------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `line_model("msl" \| "gcpw", length)`          | none                                                                            | a straight 50 Ω line on the 4-mil RO4835 feed stack between two wave ports. The GCPW variant has GND to the side walls and a via fence on each side (offset 0.5 mm, pitch 0.45 mm, drill 0.15 mm).                              |
| `patch_from_record(record, "w" \| "finite")`   | an `rfmacro` record (`radar60-rfmacro/1`)                                       | the inset-fed patch alone on its L2 window, as built by the openEMS single-patch run. `w` runs the board into the south wall under the feed and uses a wave port. `finite` is the stage-2 finite board with a 50 Ω lumped port. |
| `from_feedmodel(model, box)`                   | a radar60 feed model, the JSON that the RF-uniformity `prep.py` cut for openEMS | exactly the geometry of those openEMS runs. The hole-free pieces are re-unioned, then cropped to the box. L2 is the PEC floor. P0 ports point east, P1 ports north.                                                             |
| `from_kicad(board, box, layers, stack, frame)` | a zone-filled `.kicad_pcb`                                                      | read with `yapnr.fab.board`'s S-expression reader (no pcbnew): zone fills, tracks, arcs, vias and their pads. Footprint pads are not read.                                                                                      |

The **cleaner** runs inside every adapter except the line generator:

- it merges vertices closer than 2 µm;
- it drops collinear vertices;
- it refits a run of short chords as one exact arc when every vertex and chord midpoint is within
  1 µm of a single circle.

A polygonized KiCad fill would otherwise put a mesh node every few micrometres along its curves.
The cleaner keeps the tangent points where a straight edge meets an arc. `provenance` records the
XOR area between the input copper and the cleaned copper.

**`fit_box`** moves the x and y walls of a requested box so that no wall cuts a via barrel or
leaves a sliver of dielectric or copper narrower than 50 µm. It prefers to move a wall outward:
that only adds stitched pour beyond the fences, while moving inward could cut away a line's
ground.

**Check against KiCad.** On the radar60 TX feed region, the KiCad reader and the openEMS feed
model agree to within these XOR areas (cleaned):

| Net | XOR area   | Edge length | Mean offset |
| --- | ---------- | ----------- | ----------- |
| GND | 0.0043 mm² | 70 mm       | 0.06 µm     |
| TX1 | 0.0035 mm² | 29 mm       | 0.12 µm     |
| TX2 | 0.0040 mm² | 29 mm       | 0.14 µm     |

The 55 vias east of P0 coincide. Cleaning the feed model itself moved its copper by 0.0076,
0.0021 and 0.0047 mm² (GND, TX1, TX2), mostly where exact arcs replaced `prep.py`'s chords.

## 3. The mesh

`mesh.build(doc, out_dir)` builds the geometry with Gmsh's OpenCASCADE kernel:

- the domain box is the air;
- the slabs (boxes, or extruded outlines) override the air;
- sheet and PEC copper are surfaces with exact arcs;
- solid copper and via barrels are cut out, so their surfaces are the conductor;
- port faces are rectangles: wave-port faces on the walls, lumped-port faces embedded in the core.

Two `occ.fragment` passes make the geometry conforming, with the port faces added in the second
pass. In a single pass, a lumped port touching a window plane made OpenCASCADE return
overlapping solids of negative volume.

The physical groups are Palace's attributes, numbered from 1 with volumes first:

| Group                | Meaning                                         |
| -------------------- | ----------------------------------------------- |
| `air`, `diel:<name>` | volumes (materials)                             |
| `port:<name>`        | port faces                                      |
| `cond:<layer>:<net>` | copper surfaces                                 |
| `via:<net>`          | via barrels and their tops                      |
| `wall:<face>`        | the parts of each outer face that are not ports |

**Sizes.**

- At every copper and via edge the element size is `edge_h` (0.04 mm), growing by `grade` (0.35)
  per mm of distance from the edge.
- Each region is capped at its wavelength / 6 at `f_max` (70 GHz): 0.71 mm in air and 0.38 mm in
  RO4835.
- A thin slab is capped at 1.5 × its thickness (0.15 mm in the 0.1016 mm core).
- Port faces are capped at `port_h` (0.1 mm).

Adaptive refinement in Palace takes it from there.

**Mesh output.**

- Tetrahedra only, written as msh 2.2 binary.
- With one thread (the default), the same document gives the same mesh every time.
- A Netgen optimization pass lifts the worst elements: on the patch the minimum gamma goes from
  0.02 to 0.16. The pass is skipped for models with a lumped port: gmsh 4.15.2's Netgen optimizer
  crashes on a volume with an embedded face.

`mesh.json` records:

- the attribute map with entity and element counts;
- the size caps;
- element quality: gamma and SICN, minimum, first percentile and median;
- the estimated unknowns of Nédélec elements of order 1–3;
- the port faces;
- the geometry hash.

## 4. The configurations

`config.driven` writes a frequency sweep: uniform, or Palace's adaptive fast sweep
(`AdaptiveTol`). With `amr`, it writes an adaptive-refinement run on a few frequencies instead.
Palace sums the error estimate over every solved frequency, so refinement belongs on a narrow
set around the feature. That run saves the adapted mesh, and a second configuration sweeps on
it (`postpro-amr/mesh.meshgz`).

`config.boundary_mode` writes the 2D mode solve on one wave port's plane of the same 3D mesh
(`Solver.BoundaryMode.Attributes`). It gives n_eff and Z_PV along the port's voltage path.
`face="port"` solves the port face alone with the rest of its wall PEC, the shielded guide the 3D
run's port sees; `face="wall"` solves the whole wall, the line as an open structure. A GCPW's
coplanar ground floats in the open-wall solve (no vias in a cross-section), so use the port face
there.

**Copper as `Impedance` (`copper_bc="impedance"`, the validation default).** At b797ea8 a
`Conductivity` boundary that crosses a wave-port face or a BoundaryMode cross-section aborts the
2D mode solve on more than one MPI rank ("Dimension mismatch for MaterialPropertyCoefficient and
libCEED integrator"): the ranks that own none of its edges get an empty coefficient. `Impedance`
boundaries are skipped where empty, so the copper is written as the `Impedance` with the same
admittance at one frequency (`config.impedance_rl`, the band centre by default). Palace's
`Impedance` is a parallel R ∥ L per square of the whole boundary, split over the two faces of a
cracked interior sheet, while `Conductivity` applies its impedance on each face: a sheet gets
Rs = ωLs = Re Z, the outside of solid copper Rs = ωLs = 2 Re Z (thick copper). One-rank runs with
`Conductivity` give the same n_eff and Z_PV to the printed digits. Away from that frequency the
conductor loss goes as 1/(1 + (f0/f)²) instead of √f: about 8 % low at 54 GHz and 6 % high at
70 GHz for f0 = 62 GHz.

| Attribute                                       | Palace boundary or material                                                                             |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `air`, `diel:*`                                 | `Materials`: Permittivity, LossTan                                                                      |
| `cond:*` of a `pec` layer, `via:*`, `pec` walls | `PEC`                                                                                                   |
| `cond:*` of a `sheet` layer                     | `Conductivity`: σ / K², `Thickness` t, `External` false (the sheet is internal); or `Impedance` (above) |
| `cond:*` of a `solid` layer                     | `Conductivity`: σ / K², `External` true (thick-conductor impedance); or `Impedance` (above)             |
| `abc1`/`abc2` walls                             | `Absorbing`, order 1 or 2                                                                               |
| wave ports                                      | `WavePort`: `Offset` back to the reference plane, `VoltagePath`, `Excitation` = Index for excited ports |
| walls with wave ports                           | also `WavePortPEC`, so each port face is a shielded guide in its 2D mode solve                          |
| lumped ports                                    | `LumpedPort`: `R` = z0, `Direction` from the strip down to its reference                                |

Lengths are in mm (`L0` = 1e-3). Frequencies are in GHz. The linear solver is Palace's default
(AMS-preconditioned GMRES, tolerance 1e-6).

`schema.validate` checks a configuration against Palace's own schema. That schema is vendored
unmodified from the commit the task image builds (`third_party/palace`, see
[THIRD_PARTY.md](../THIRD_PARTY.md)). The check uses a small stdlib validator for the JSON Schema
draft-07 keywords that the schema uses, and it also reports keys that Palace marks deprecated. It
agrees with `jsonschema` on all 90 configurations in Palace's examples and tests, and on 2,610
random mutations of them. The image's `palace --dry-run` remains the final check before a solve.

## 5. Validation models

`python -m yapnr.rf.palace validation --out DIR [--record R] [--feed NAME=MODEL ...]
[--feed-result RESULT]` builds the validation models of the Palace plan. Each model goes into its
own directory with `model.json`, `mesh.msh`, `mesh.json` and these configurations:

- `palace-uniform.json`: the sweep on the initial mesh;
- `palace-amr.json`: refinement at a few frequencies;
- `palace-sweep.json`: the sweep on the adapted mesh;
- `palace-mode-*.json`: the 2D mode solves (lines only).

These are the meshes as built on a developer Mac:

| Model                          | Tetrahedra       | DOFs p = 2 / p = 3 (est.)    | gamma min / p01          | Mesh time   |
| ------------------------------ | ---------------- | ---------------------------- | ------------------------ | ----------- |
| `line-msl-5mm` / `-10mm`       | 30,381 / 53,282  | 0.20 / 0.59 M; 0.36 / 1.03 M | 0.20 / 0.46; 0.23 / 0.46 | 1.4 / 2.3 s |
| `line-gcpw-5mm` / `-10mm`      | 81,136 / 143,771 | 0.55 / 1.58 M; 0.98 / 2.80 M | 0.16 / 0.44; 0.12 / 0.44 | 4.1 / 7.5 s |
| `patch-w`                      | 142,043          | 0.91 / 2.66 M                | 0.16 / 0.47              | 5.7 s       |
| `patch-finite`                 | 189,826          | 1.21 / 3.52 M                | 0.18 / 0.35              | 3.2 s       |
| `tx12-A` (with the GND sliver) | 241,894          | 1.62 / 4.65 M                | 0.14 / 0.45              | 13.6 s      |
| `tx12-B` (sliver removed)      | 232,944          | 1.56 / 4.48 M                | 0.15 / 0.45              | 12.9 s      |

The feed models' domain is the interior of the PML in the openEMS run that the comparison is
against. `fit_box` then moves that box off the via barrels. In the openEMS runs, the zone between
the inner box and the PML holds PEC copper. In these models it holds the same copper, modelled as
the L1 sheet.

## 6. Running on Google Cloud

A job in a `palace_plan.py` campaign can build its own model. `palace_job.py` runs the `prepare`
arguments with the task's Python, from the task's work directory, so the package only has to be
an input there:

```toml
[inputs]
yapnr = "bundle/yapnr"               # yapnr/__init__.py, yapnr/rf/planar, yapnr/rf/palace
third_party = "bundle/third_party"   # third_party/palace, for the schema check
models = "models"                    # the planar documents

[[jobs]]
id = "tx12-A-amr"
prepare = ["-m", "yapnr.rf.palace", "case", "models/tx12-A.json", "--out", "{out}"]
config = "{out}/palace-amr.json"
```

`palace_job.py` makes the mesh path absolute and moves Palace's output to `out/<id>/postpro`.
Refinement followed by a sweep of the refined mesh is one job: `stages` are solved first (output
in `out/<id>/stage-<name>`), and `mesh_from` points the main solve at the mesh that stage saved:

```toml
[[jobs]]
id = "tx12-A"
stages = ["models/tx12-A/palace-uniform.json", "models/tx12-A/palace-amr.json"]
mesh_from = "models/tx12-A/palace-amr.json"
config = "models/tx12-A/palace-sweep.json"
memory_gb = 48            # about 12 kB per unknown at order 2 with the sweep's reduced model
```

A stage written `CONFIG@1` runs on one rank. Two things about refinement at b797ea8:

- Sweep the saved mesh in a separate solve rather than trusting the refinement loop's own late
  iterations: with nonconforming refinement, the wave-port modes inside the loop drift after a
  few refinements (line-msl-5mm at 62 GHz: Z_PV 55.9 -> 62.4 ohm and |S21| -0.47 -> -1.76 dB by
  the fifth mesh).
- A saved adapted mesh has its interior sheets split already, so a run that loads it no longer
  treats them as cracked and gives each face the whole `Impedance`. The sweep of a saved mesh
  therefore writes the sheets per face (`config.impedance_rl(..., precracked=True)`; validation's
  `palace-sweep.json` does). Without that, its conductor loss comes out about half.

## 7. What the first runs showed

The validation report (`palace/validation.md` in the project notes) has the numbers; for model
builders:

- **Copper model.** The zero-thickness sheet that openEMS also uses makes the 4-mil 50-ohm lines
  about 53 ohm with an effective permittivity about 5 % high, against 35 µm copper (the 2D solver
  agrees). Sign off with `solid` copper; keep `sheet` for comparisons with openEMS.
- **Port faces.** A wave port's face must not hold a floating conductor: in its 2D mode solve the
  face's edges are PEC, so a coplanar ground strip that does not reach one floats and the port
  picks another mode (tx12's TX1.P0 gave Z_PV 24 ohm). `port_geometry` therefore ends a side
  that has coplanar ground inside it.
- **Voltage paths.** The BoundaryMode and wave-port `VoltagePath` in 3D coordinates is projected
  onto the port plane by Palace; Z_PV comes out as expected.
- **Memory.** About 12 kB per unknown at order 2 for a driven sweep with the reduced model, 8-10
  kB for a single solve: 1.7 M unknowns took 20-23 GB, 2.6 M ran a 32 GB VM out of memory.
- **Absorbing order.** The walls are first-order absorbing (Palace has no PML). On the single
  patch, walls 0.5 λ0 instead of 0.3 λ0 away, or second-order walls, moved the |S11| dip by at
  most 0.06 GHz (0.1 %).
- **Agreement with openEMS.** The TX1 sliver notch agrees within 0.2 %; the line εeff and the
  patch dip agree once openEMS's fill-size trend is extrapolated (openEMS at 20 µm is still 3 %
  high in εeff and 1.2 % low in the patch dip).
