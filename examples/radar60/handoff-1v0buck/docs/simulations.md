# Simulations: inputs, commands, results and what is still owed

Everything here concerns the antenna and RF macro v2 (`examples/radar60/rf/`, frozen at
`claude/radar60-rf3b`
`727df41`). Decisions and numbers: `docs/stage3b-rf/freeze.md`, `em-bank.md` and `em-column.md`. The
short status
is in `docs/rf-status.md`.

## Where the inputs are

| Path                                                      | What                                                                                                                                                                                                                                                            |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sims/stage3b-rf-em/models/`                              | Model specs (JSON): `bank-A/B/C/S1` (two-bank boards for D15 and the dummy columns), `cell-*` (column C1 points: `c1a-*`, `c1b-*`, `k0l-*`, `k1l-*`), the PA-corner (`pa-*`) and TX-feed models                                                                 |
| `sims/stage3b-rf-em/jobs/`, `jobs-col/`, `cloud-col.toml` | The openEMS campaign job files (`s3b-e1-*`, E2, E3), with image tag, threads, mesh and wall-time limits                                                                                                                                                         |
| `sims/stage3b-rf-em/setup/`, `setup-col/`                 | Per-model openEMS setups as planned (`ant_sim-N`)                                                                                                                                                                                                               |
| `sims/stage3b-rf-em/bank/`                                | Bank-level models, `make_jobs.py` and the summary of the E1 bank runs (`out/summary.json`)                                                                                                                                                                      |
| `sims/stage3b-rf-em/col/runs/<run>/s.csv`                 | S-parameters per column, PA and cavity run (20 µm unless named `-r15`/`-r27`)                                                                                                                                                                                   |
| `sims/stage3b-rf-em/campaigns/<name>/campaign.toml`       | The planned campaigns: `s3b-c1-k`, `s3b-c1-r15/r20/r27` (convergence), `s3b-c1-wall`, `s3b-e2a/b`, `s3b-e3`, `s3b-pa-r20`                                                                                                                                       |
| `sims/campaign-plans/`                                    | Plan copies for `s3b-e3` and `palace-c1` (`campaign.toml`, `tasks.jsonl`; source bundles not included)                                                                                                                                                          |
| `sims/generator-variants/`                                | The macro generator variants used (`s3b-A/B/C` = D15 pours, `S1`, `K1`/`K2` cavities, `C1lo`/`C1hi` brackets, `open`, `pa0`)                                                                                                                                    |
| `sims/fetched/<campaign>/`                                | Per-task records and result summaries of the final campaigns: `20261005-mceval-1f3598` (s3b-e3: K1/K0 retune + D14 as built), `20261005-mceval-f13ef3` (Palace C1 cross-check), `20261005-mceval-22f21a` and `-540c3c` (E2 C1 points and the D14 fence re-runs) |
| `sims/palace/READY.md`                                    | The Palace image and validation status                                                                                                                                                                                                                          |
| `sims/cloud-em/READY.md`                                  | The openEMS-on-GCP calibration (1 model × 8 threads per c4d-highcpu-8)                                                                                                                                                                                          |
| `sims/SKIPPED-LARGE.txt`                                  | Lists any file over 540 KB that was left out                                                                                                                                                                                                                    |

**Field dumps, NF2FF data, meshes and Palace validation runs are not included**; they are tens of
megabytes and rebuildable by rerunning the campaigns.

**Geometry:**

- **The as-built D14 PA feed and fence:** the generator code on this branch,
  `examples/radar60/rf/rfmacro/params.py` (`pa_feed`, `pa_fence`, `dummies="outer"`) and `vias.py`.
  The as-built fence is 4 vias at y = pocket top + 0.31 mm (U1 frame y 7.31).
- **The model-only test fence:** a row at y 4.82 mm inside the U1 package outline (freeze.md, "D14
  on the as-built fence").
- **The PA pocket:** `[29.563, 33.45, 31.25, 35.0]` (board frame, freeze 727df41).
- **The ground-stitching pour:** D15 = A, a 0.60 mm grid (`sims/generator-variants/s3b-A`).

## How to rerun

```sh
# openEMS: plan a job file into a campaign, then run it on GCP Batch with yapnr exp
python3 tools/exp/openems_plan.py plan sims/stage3b-rf-em/jobs/<job>.toml --out <dir> --digest sha256:d468ed9a6b31469504a4046e214b7b098d16f9cee77a55813b6f0d949735deb0
yapnr exp plan <dir>/campaign.toml --backend gcp-batch
yapnr exp submit <plan> --yes
yapnr exp fetch <plan> --full
python3 tools/exp/openems_plan.py collect <campaign-id> --dest <tree>

# Palace (independent FEM cross-check)
python3 tools/exp/palace_plan.py plan <palace jobs.toml> --out <dir> --digest sha256:604f0796d670d1fd46fd888ba6dc121f82920d4c631eea41503b284a00842d93
# yapnr.rf runs (FDTD + adjoint), as one mc-eval campaign
python3 tools/exp/rf_stage_plan.py <jobs.toml> --repo . --out <dir>
```

You can run openEMS locally on one model with the `docker/openems` image (`--offline` plans for the
local backend). Wall time per model is in each job file. A two-bank model at 27 µm needs about 6 h,
which is why bank-level checks stay at 40 µm.

## Results and convergence (from freeze.md, em-\*.md)

| Check                       | Result                                                                                                      | Mesh / corners                                                                                  | Palace cross-check                                                                                                                      |
| --------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| C1 column match (ANT-02 RL) | Best c1b-6: worst in-band −6.51 dB (openEMS) / −7.2 dB (Palace-referred); no point meets −10 dB (22 points) | 20 µm only for c1b-6; 27/20/15 µm trends exist for earlier c1a points; Dk ±0.10 corners planned | patch-w at L ×1.023: dip 61.65 GHz (−17.5 dB), −10 dB band 60.40–63.15 GHz; frame factor k = 1.0165 holds at L 1.0 (63.57 vs 63.54 GHz) |
| K (cavity)                  | K0 kept; K1 rejected (−8.83/−8.27/−7.80 dB at L 1.00/1.015/1.03)                                            | 20 µm                                                                                           | agrees with the openEMS centring L to within 0.2 %                                                                                      |
| D14 PA-island coupling      | TX1 −33.5 dB as built (fails −40 dB); RX4 −44.9 dB                                                          | 20 µm, port case (pessimistic: the real island is cap-shorted)                                  | not run                                                                                                                                 |
| D15 stitching               | A chosen (quantified warning); isolation 36.7–37.4 dB for A/B/C                                             | 40 µm bank models                                                                               | not run                                                                                                                                 |
| Dummy columns               | S1: isolation 38.2 dB against S2's 36.7 dB; TX1 feed −1.50 against −1.78 dB                                 | 40 µm bank / 20 µm feed                                                                         | —                                                                                                                                       |
| TX feed (RF-03)             | TX3 alone 1.33–1.65 dB; D6 waiver (≤ 2.7 dB) most likely stays                                              | 15/20 µm                                                                                        | —                                                                                                                                       |

## Not run yet (owed before Rev A sign-off)

1. **C2: RF sign-off on routed copper.** The integrated macro plus fanout on the routed board, in
   openEMS and Palace. Needs a complete board.
2. **D14 on routed copper with U1 GND ball vias** (see `docs/rf-status.md`).
3. **The 15 µm trend of c1b-6** and the Dk ±0.10 corners on the frozen point.
4. **The D12 brackets** (rfm1-m/p, L ±1.8 %): generated, not simulated.
5. **Embedded patterns** for ANT-01 (two-way patterns per virtual pair) on the final copper.

## Dependencies you may not have

| Dependency                                         | Status                                                                                                                  | Workaround                                                                                                                                                                                                                                                                          |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| openEMS and Palace container images                | In the owner's private Artifact Registry (`us-west4-docker.pkg.dev/yapnr-experiments/images/{openems,palace}@sha256:…`) | Rebuild from `docker/openems` and `docker/palace` in this repo (`tools/exp/openems_plan.py image`, `palace_plan.py image`). The Palace image is ParMETIS-free (PT-Scotch v7.0.16); never distribute the old ParMETIS build. Digests differ after a rebuild, so record the new ones. |
| GCP project, buckets, quota                        | The owner's account                                                                                                     | The local `yapnr exp` backend runs the same plans on one machine, slowly                                                                                                                                                                                                            |
| TI files (IWR6843 EVM layouts, IBIS, package STEP) | **Not used and never committed**. RF models use the ABL0161 ball map and land sizes from the public datasheet           | —                                                                                                                                                                                                                                                                                   |
| Vendor 3D models                                   | Not needed for simulation; approximate models are generated (`examples/radar60/models3d/`)                              | —                                                                                                                                                                                                                                                                                   |
| Licensed solvers                                   | None. openEMS (GPLv3) and Palace (Apache-2.0) are open source                                                           | —                                                                                                                                                                                                                                                                                   |
