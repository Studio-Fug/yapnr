<!-- markdownlint-disable -->

> **HOLD LIFTED (main loop, 2026-10-04):** sign-off runs use the ParMETIS-free image `palace:b797ea8-pts-x86-64-v3@sha256:604f0796d670d1fd46fd888ba6dc121f82920d4c631eea41503b284a00842d93` (PT-Scotch, CeCILL-C) with the DEFAULT ordering. The parallel orderings stay opt-in until the G3 scaling runs. The old ParMETIS image is for evaluation-only comparisons; never use it for sign-off and never distribute it.

# Palace sign-off runs: READY (for the stage-3 agent)

Date 2026-10-04. Owner request (2026-10-04): "let's get the palace integration stood up alongside
stage 3". Palace (AWS, Apache-2.0, 3D FEM) is the **independent second solver** for the radar60
sign-offs: the 60 GHz column, the BGA-to-GCPW launch, the bank with finite board and radome.
openEMS stays the sweep workhorse (`../radar60/cloud-em/READY.md`). Evidence in
`validation.md` (sections 1-9: the first validation; section 10: the review fixes). Labels: **[S]**
solver prediction, **[D]** arithmetic or closed form; nothing here is measured.

**Status: READY** for feed-, launch- and column-sized sign-off runs (up to about 4 M
unknowns per model); **not yet for the bank** (section 6, item 4). Validated against openEMS and
the 2D solver (validation.md §1), including two adversarial reviews whose findings are all fixed
in the code (PR 46, CI green) or carried as limits below. The validated image, digest and
settings are the defaults of `python -m yapnr.rf.palace case`.

What the validation leaves the stage-3 owner (for review) [S]:

- With real 35 µm copper the TX1 GND-sliver notch sits at 61.3-61.4 GHz (Palace refined 61.30,
  openEMS 35 µm PEC 61.42 GHz), 60.4-62.9 GHz over the RO4835 Dk range 3.33-3.66: inside ANT-02
  whatever the laminate does. It costs TX1 0.3-2.6 dB (up to 6 dB at Dk 3.33) and about +40° at
  62.05 GHz. Remove the slivers (em-baseline's variant B).
- The stage-2 single patch, calibrated on a 40 µm openEMS mesh, sits 2-3 % high on converged
  meshes with real copper (dip about 63.0-63.6 GHz against the 61.9 GHz target).
- openEMS sign-off runs need 20 µm fill or finer **and 8 cells across the 4-mil core**; its εeff
  is still about 3 % high there (about 25° over a 14 mm feed): do not take absolute feed phase
  from openEMS alone.

## 1. What to use it for

Run Palace on the cases that decide a sign-off, next to openEMS, and sign off on the agreement of
the two (or an explained difference):

| Case                                      | Palace model                                                                      | openEMS counterpart                                                                          |
| ----------------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Feed resonances (GND slivers), feed phase | solid copper, lossy L2 floor, one refinement                                      | 20 um fill or finer, 8 cells across the core; thick copper as PEC (`--thick`) for frequency  |
| BGA-to-GCPW launch                        | `from_kicad` region with pads, solid copper, solder mask (`stackups.solder_mask`) | as above; the mask is not in `feed_sim_x.py` yet (add a dielectric box over L1)              |
| Column with divider                       | solid copper, wave port at the divider input                                      | `val/code/column_sim_nz.py` (column_sim.py with `--nz-core/--nz-bond`) at 20 um, 8/8 z-cells |
| Bank with finite board and radome         | not yet: section 6                                                                |                                                                                              |

## 2. Setup (once)

```sh
Y=<local-path>         # or the main checkout once PR 46 is merged
PY=<local-path>
FEA=${REPO}/.venv/palace/bin/python   # gmsh 4.15.2, shapely, numpy, scikit-fem
yexp() { (cd $Y && PYTHONPATH=$Y $PY -m yapnr.exp.cli "$@"); }
```

The FEA venv is re-created with `python3.12 -m venv .venv/palace && .venv/palace/bin/pip install
gmsh==4.15.2 shapely==2.1.2 numpy==2.5.3 scikit-fem==12.0.2 scipy matplotlib` (docs/rf-palace.md).
Image: `palace:b797ea8-pts-x86-64-v3@sha256:604f0796d670d1fd46fd888ba6dc121f82920d4c631eea41503b284a00842d93`
(no ParMETIS: Scotch/PT-Scotch in its place, `palace/no-parmetis/build.md`; `palace_plan.py plan`
resolves the tag to this digest; smoke 6/6 plus the expected MUMPS+ParMETIS failure, and the
validated `line-gcpw-5mm-sg` order 2 reproduced within 5e-10 of the old image's output at 8 ranks,
`no-parmetis/review/`). The validation runs used the ParMETIS image
`palace:b797ea8-x86-64-v3@sha256:9d157377…` with the default (serial METIS) ordering; that image is
kept for evaluation comparisons only and must not be used for sign-off runs. Do not rebuild
without re-running the smoke test and one validation case: `docker/palace/README.md`.

## 3. A sign-off run, command by command

1. **Planar document** (Python, FEA venv, `PYTHONPATH=$Y`):

   ```python
   from yapnr.rf.planar import adapters, model, stackups
   diel, layers = stackups.radar60("feed", l1_model="solid")          # RO4835 4 mil, L1 35 um
   mask = stackups.solder_mask(layers[0], outline=MASK_RING)          # 17 um, Dk 3.8, Df 0.025
   doc = adapters.from_kicad(BOARD, [x0, x1, y0, y1], {"F.Cu": "L1"}, (diel + [mask], layers),
                             frame=(kx0, ky0, True), nets=["GND", "RF_TX1"])  # floor: lossy L2
   doc["name"] = "launch-tx1"; doc["ports"] = [...]; model.check(doc)
   ```

   Feed regions already cut for openEMS: `adapters.from_feedmodel(prep_json, box=fit_box(...))`
   (`validation.feed_case`). Check `provenance.pads_skipped` (unsupported pad shapes) and the
   port faces (`model.port_geometry`: a face must end inside coplanar ground).

2. **Mesh + configs:** `PYTHONPATH=$Y $FEA -m yapnr.rf.palace case launch-tx1.json --out
models/launch-tx1 --band 54 70 0.025 --excite TX1.P0 --amr-freqs 60.3 62.05 63.8 --orders 3`.
   Any model not named `line-`/`patch-`/`tx12-` gets the sign-off settings (section 4) and
   solid copper; it prints them. `mesh.json` gives the unknowns (`dofs_estimate`).
3. **Jobs file** (`JOBS.toml` next to `models/`):

   ```toml
   name = "s3-launch-tx1"
   image = "palace:b797ea8-pts-x86-64-v3"
   ranks = 8
   families = ["c4d", "c4"]
   memory_gb = 48                       # 12 kB per unknown at order 2 (section 5)
   max_wall_s = 3600
   [inputs]
   models = "models"
   [[jobs]]
   id = "launch-tx1"
   stages = ["models/launch-tx1/palace-uniform.json", "models/launch-tx1/palace-amr.json"]
   mesh_from = "models/launch-tx1/palace-amr.json"
   config = "models/launch-tx1/palace-sweep.json"
   ```

   Group jobs of similar length into one memory class (yapnr exp takes a class's wall limit and
   ceiling from its longest task).

4. **Plan, log, submit, fetch:**

   ```sh
   PYTHONPATH=$Y $PY $Y/tools/exp/palace_plan.py plan $PWD/JOBS.toml --out $PWD/campaign
   yexp plan $PWD/campaign/campaign.toml --backend gcp-batch     # prints the ceiling
   # add a row to palace/gcp-spend.md and radar60/gcp-spend.md BEFORE submitting (ceiling < $5)
   yexp submit <cid>; yexp status <cid>; yexp fetch <cid>
   ```

   Results land in `~/yapnr-runs/fetched/<cid>/tasks/mc~<id>/summary/<id>/`: `stage-1-palace-uniform/`
   (order 2, initial mesh), `stage-2-palace-amr/` (the refinement), `postpro/` (the sweep of the
   refined mesh), `<id>.job.json` (wall, memory, unknowns).

5. **Compare** (FEA venv):

   ```python
   from yapnr.rf.palace import results
   for name, post in results.stage_dirs(task_dir):
       f, S = results.port_s(post + "/port-S.csv"); fz, Z = results.port_z(post + "/port-Z.csv")
       S50 = results.to_50(S, Z, excite=1)        # openEMS's 50-ohm waves
   out = results.compare((f, S50[(2, 1)]), (f_oems, s21_oems), [60.3, 62.05, 63.8], feature=(55, 66))
   ```

   For openEMS thick-copper runs read the waves with each port's own line impedance
   (`val2/code/analyze.py: modal_oems`): the MSL port misreads the current on thick copper.

6. **Verdict:** frequency of the deciding feature within 1 % and |S21| within 0.5 dB, or an
   explained difference; convergence shown by Palace's refined-against-initial and order-3
   results and openEMS's z-refined run; the Dk range of RO4835 (3.33-3.66) reported as a range,
   since it moves resonances more than the solvers differ.

## 4. Recommended configuration

| Item           | Setting                                                                                                                                                                                                                                     | Why                                                                                                                                                     |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --- | ------------------------------------------------------- |
| Image          | `palace:b797ea8-pts-x86-64-v3@sha256:604f0796…` (no ParMETIS)                                                                                                                                                                               | smoke test against Palace's regression data; reproduces the validation image's output (default ordering) within 5e-10                                   |
| Copper         | **solid 35 µm** (`with_layer_model(doc, "solid")`; `case` does it), written as `Impedance` by admittance at the band centre (the default)                                                                                                   | the zero-thickness sheet makes the 50 Ω lines 53 Ω with εeff 5 % high and moves the TX1 notch by 2.4 %; `Conductivity` aborts multi-rank wave-port runs |
| Ground         | **lossy `metal` floor** (L2, K of the LoPro foil; default of the adapters)                                                                                                                                                                  | about a quarter of the lines' conductor loss is in the ground (0.15 dB over a 14 mm feed)                                                               |
| Solder mask    | `stackups.solder_mask(L1, outline=...)` where the board has mask (17 µm, Dk 3.8, Df 0.025: assumptions, stage-2 plan 15-20 µm, Dk 3.5-4)                                                                                                    | the launch runs under mask; conformal by default                                                                                                        |
| Ports          | wave ports, faces ±0.8 mm and 0.7 mm above L1, **ending inside coplanar ground**, `Offset` to the reference planes; S renormalized with `results.to_50`                                                                                     | a face ending in a gap gave a 24 Ω port mode                                                                                                            |
| Order and mesh | order 2 on the gmsh mesh (0.04 mm at copper edges); the check: **one refinement** (`palace-amr.json`, at the feature frequencies) swept in a separate solve (`palace-sweep.json`), and/or order 3 (`--orders 3`)                            | one refinement moved the sheet notch 0.13 %, the solid one 0.28 % (61.13 → 61.30 GHz); order 2 → 3 moved εeff 0.65 %                                    |
| Refinement     | `Nonconformal`, one step, `MaxSize` 2 M (the default)                                                                                                                                                                                       | long loops drift (issue 4) and run out of memory                                                                                                        |
| Sweep          | adaptive, `AdaptiveTol` 1e-3, at most 30 samples, 25 MHz output                                                                                                                                                                             | 10-13 full solves per case; matched separate full solves to 2e-5                                                                                        |
| Walls          | first-order absorbing ≥ 0.3 λ0 from copper for feeds and single elements; **≥ λ0 and second order with a box study for anything radiating at oblique angles** (columns with neighbours, the bank, the radome)                               | first-order walls reflect −15 dB at 45°, −9.5 dB at 60°                                                                                                 |
| Parallel       | 8 MPI ranks bound to cores, one model per 16-vCPU VM (`c4d-standard-16`, 62 GB)                                                                                                                                                             | about 12 kB per unknown at order 2 (sweep): ≤ 4 M unknowns                                                                                              |
| openEMS side   | 20 µm fill or finer **with 8 cells across the 4-mil core** (`feed_sim_x.py --nz-core 8`, `column_sim_nz.py --nz-core 8 --nz-bond 8`); thick copper as PEC (`--thick 0.035 --zcu 0.035`) for frequencies, read with own-line-impedance waves | z refinement moves Z 2 %, loss 10 %, small                                                                                                              | S11 | 2 dB, the patch dip +0.13 GHz, the thick notch +0.4 GHz |

## 5. Cost and time per case

Spot list prices of 2026-10-04 × (task wall + 2 min VM start), boot disk included [D]; Palace 8
ranks on 16 vCPUs, openEMS 8 threads on a c4d-highcpu-8 (or 4 on the Mac).

| Case                                       | Solver, run                                                         | Size               | VM              | Wall                       | Peak memory | $ per run                            |
| ------------------------------------------ | ------------------------------------------------------------------- | ------------------ | --------------- | -------------------------- | ----------- | ------------------------------------ |
| TX1 feed (tx12), solid copper, lossy floor | Palace order-2 sweep (`palace-uniform`)                             | 1.9-2.0 M unknowns | c4d-standard-16 | 14 min                     | 23 GB       | 0.04                                 |
|                                            | one refinement at 4 frequencies (`palace-amr`)                      | 2.0-2.1 M          | c4d-standard-16 | 11-13 min                  | 26-27 GB    | 0.03                                 |
|                                            | sweep of the refined mesh (`palace-sweep`)                          | 2.0-2.1 M          | c4d-standard-16 | 17 min                     | 26-27 GB    | 0.05                                 |
|                                            | **the three stages in one job**                                     |                    | c4d-standard-16 | **41-44 min**              | 27 GB       | **0.13**                             |
|                                            | the same on C4 (Montreal)                                           |                    | c4-standard-16  | about 70 min (1.7x)        |             | about 0.13                           |
| Single patch, sheet                        | Palace order-2 sweep / order 3                                      | 1.3 / 3.7 M        | c4-standard-16  | 23 / 71 min                | 15 / 30 GB  | 0.05 / 0.14                          |
| Lines (5/10 mm)                            | Palace order-2 sweeps / order 3                                     | 0.2-1.1 / 0.6-3 M  | 16 vCPU         | 1-2 / 4-25 min             | 3-22 GB     | 0.005 / 0.02-0.04                    |
| 2D port modes                              | Palace BoundaryMode, a dozen per task                               | < 0.05 M           | 16 vCPU         | 10-20 s each               | 0.4 GB      | 0.004 per task                       |
| TX1 feed                                   | openEMS 20 µm, 8 z-cells, sheet / 35 µm PEC (A: the ringing sliver) | 3.5-3.6 M cells    | Mac 4 threads   | 15-16 min (A), 5-6 min (B) | -           | 0 (about 0.01-0.03 on c4d-highcpu-8) |
| Single patch                               | openEMS 20 µm, 8/8 z-cells                                          | 2.3 M cells        | Mac 4 threads   | 33 min                     | -           | 0 (about 0.03)                       |

A full feed sign-off (both solvers, A and B variants) costs about $0.3 and an hour once VMs are
available; budget the queue, not the compute.

## 6. Limits and open issues

1. **Spot quota.** 64 Spot vCPUs per region (us-west4 C4D, Montreal C4), shared with every
   track; on-demand VMs are refused by the owner config (`limits.allow_on_demand`). On
   2026-10-04 both regions were full for 1.5-3 h; a submitted job waits in the region it was
   placed in (re-plan with `families = ["c4"]` to move it to Montreal). Batch also leaves jobs
   QUEUED after a round of quota failures even when the region frees up: if a job sits QUEUED
   while `gcloud compute regions describe` shows free PREEMPTIBLE_CPUS, `yexp cancel <cid>` and
   `yexp submit <cid> --only <task ids>` (submit places a class only where there is room). Plan
   sign-off runs early.
2. **No checkpoint.** A preempted Palace task restarts from scratch; keep a task under 1-2 h.
3. **Memory.** 12 kB per unknown at order 2 (sweep), 8 kB at order 3; the feed models are
   1.7-2.0 M unknowns (20-25 GB). More than about 4 M needs a bigger shape (docs/rf-palace.md
   section 10): per-job `ranks`/`memory_gb` work (instance policy).
4. **The bank is not planned as one run yet:** 15-30 M unknowns (180-360 GB) is a
   `c4d-highmem-48/64` and a whole region's Spot quota for 1-2 h per sweep. First a rank-scaling
   run (4/8/16 ranks on a feed: not done), then columns, column pairs and a radome on one column;
   the full bank once at the end, with a wall-distance study.
5. **Absorbing walls only** (no PML): see section 4.
6. **Copper impedance is frozen at one frequency**: conductor loss −8 % / +6 % at 54 / 70 GHz for
   f0 = 62 GHz; compare losses near the band centre, or run two configs.
7. **Refinement loops:** do not trust late iterations of a long loop (issue 4: port modes drift);
   one step, then a separate sweep (the default).
8. **Launch ports:** the planar document's lumped port is a vertical rectangle under the line;
   a port between the signal ball and the package's ground balls needs a new port kind.
   Unsupported pad shapes (trapezoid, chamfered, custom arcs) are listed in
   `provenance.pads_skipped` (none on radar60-reva).
9. **Solid copper** only on a layer with nothing but air or its own coating above (L1); inner
   layers are sheets or PEC.
10. **Image licence:** the sign-off image has no ParMETIS (Scotch/PT-Scotch, CeCILL-C, in its
    place; licence texts and patches in `/opt/palace/share/palace/`). The old ParMETIS image
    (`palace:b797ea8-x86-64-v3`) is for evaluation comparisons only (docker/palace/README.md).
11. **Spend accounting** is task wall × list price [D]; there is no billing export to reconcile
    against (owner).
12. **Palace issues 1-4** (validation.md section 8) are handled in yapnr, not yet reported upstream
    (issue 4 is a zero-thickness-strip effect: it does not appear with solid copper).
13. **Ground loss:** Palace's lossy-floor share is 1.2-1.3x the 2D solver's quasi-static estimate
    (0.0136 against 0.0114 dB/mm on the MSL); the total line loss agrees within 0.005 dB/mm, but
    which ground share is right is open.
14. **Rank scaling (4/8/16 ranks) is not measured** (no free quota); per-job `ranks`/`memory_gb`
    are placed correctly now, so it is one small campaign of the tx12 model.

## 7. Files

- Code: yapnr PR 46 (`claude/palace`): `yapnr/rf/planar`, `yapnr/rf/palace` (`case`, `results`),
  `tools/exp/palace_plan.py`, `palace_job.py`, `docker/palace`, `docs/rf-palace.md`.
- Validation: `validation.md`, `val/` (first validation), `val2/` (review fixes: `code/`
  build_fix.py, analyze.py, patch2.py, xsec_ref2.py; one directory per campaign), `figs/`.
- Spend: `gcp-spend.md` (and `../radar60/gcp-spend.md`).
