<!-- markdownlint-disable -->

# openEMS on GCP Batch (Spot C4D): READY

**Status 2026-10-04 (main loop): READY.** The owner applied the `images` registry; Cloud Build
6b1464f5 built both images in 10m41s on E2_HIGHCPU_8 (about $0.19; E2_HIGHCPU_32 is refused by the
project's regional build quota): `openems:0.37.0-rc3-x86-64` (sha256:f6660e28...) and
`openems:0.37.0-rc3-x86-64-v4` (sha256:d468ed9a...). The calibration ran (19 campaigns, all
SUCCEEDED; report `calib/REPORT.md`). PR yapnr#42 (code + infra) is merged to main (fb96642), so use
the main checkout's tools or this worktree; both are the same code.

**Use this configuration (measured, not provisional):**

- `threads = 8`, `models_per_vm = 1` -> one model per c4d-highcpu-8 (8 vCPU, SMT on): 308 MCells/s,
  a 1.25 M-cell column in 5.0 min for $0.0064, the best throughput per dollar. Packing several
  single-thread models on one VM is WORSE (VM memory bandwidth is shared): scale out with VMs.
- Latency-critical single model: `threads = 8` on c4d-highcpu-16 (`models_per_vm = 1`, 16 vCPU): 4.7 min
  (Mac 14.3 min).
- Engine: the default multithreaded engine (fastest of the four at T1). Image: either variant;
  AVX-512 (`-x86-64-v4`) and generic are within about 3 % at T4/T8.
- The regional quota is 64 Spot vCPU shared by every track: at most 8 such VMs at once when nothing
  else runs; plan batches in waves of <= 6 if another track is on GCP.
- Second region (2026-10-04): northamerica-northeast1 has C4 only (no C4D). Same 6000-step column
  benchmark, T8, one model per VM: c4-highcpu-8 40.8 s vs c4d-highcpu-8 28.3 s (C4 1.44x slower),
  c4-highcpu-16 34.1 s vs c4d-highcpu-16 26.3 s (1.30x slower). With Montreal's lower Spot price the
  cost per run is about equal on hc8 (-1.5 %) and 11 % lower on hc16, but runs take longer: keep C4D
  us-west4 first for openEMS; Montreal is the overflow (yapnr exp spills automatically).
  Do not mix C4 and C4D within one comparison (Intel vs AMD can change floating-point paths).
- Mac vs GCP S-parameters: col12-A-e1 and tx12-A-e1 within tolerance; tx12-D-e3 has |dS| 0.0022 (ok)
  but 0.113 dB where |S| > -20 dB (limit 0.1). Expected from wall-clock end criteria (runs stop a few
  hundred steps apart) and FP contraction; for like-for-like comparisons set
  `openems_options = ["exact-endcriteria"]` on both sides.

---

2026-10-03. Status: **nothing has run on GCP; $0.00 spent.** The `infra/gcp` apply that creates the
`images` registry and the build account (7 to add, 0 to change, 0 to destroy) was refused by Claude
Code's permission classifier ("Protected-Scope IaC Apply"). Batch VMs have no internet access, so
without that registry there is no openEMS image they can pull, and no build, smoke test or
calibration could run. Everything after the apply is prepared and tested offline. The
recommendations below are **provisional** until `calib/REPORT.md` exists.

Code: yapnr branch `claude/cloud-openems` (worktree `<local-path>`,
not pushed): `ad68973` infra (images repo, build account), `bb88c60` runtime, openEMS image and
planner, `466487b` engine and openEMS options per model, `d910715` docs fix, `0a9d535` verify
fixes (collect summary name, Cloud Build price). prek is clean, all 134 exp unit tests and
`tofu test` (7/7) pass.

## Adversarial verify (2026-10-03, 22:45-23:30 UTC)

A fresh agent followed this file. It could not run anything on GCP either: step 1 is the same
apply, so steps 2-4 cannot run (§4 `plan` fails with `cannot resolve .../images/openems:...:
NOT_FOUND`). It did not retry the apply or work around it. What it checked instead:

- **GCP state.** No VMs, no running Batch jobs (all jobs are earlier mc-eval/ladder/smoke runs of
  other tracks), no `images` repository, no Cloud Build of this track (the two builds of the day are
  the guard functions' at 05:2x UTC). The ledger's **$0.00** for this track is right.
- **Plan.** `tofu plan` against the live state, re-run: still exactly **7 to add, 0 to change, 0 to
  destroy**. `origin/main` has not moved past the branch point (`6e748ac`).
- **The Batch task path, locally.** The runtime stage of `docker/openems/Dockerfile` (verbatim) built
  on arm64 with the Mac image as its build stage (`verify/runtime-check/`): 51 runtime packages,
  1.57 GB (the single-stage Mac image is 4.47 GB), `import CSXCAD, openEMS, h5py, numpy` OK, no
  secret-like variables in the environment. Then `openems_plan.py plan` + `yapnr exp plan
--backend gcp-batch` of 2 short tx12-A-e1 models (`verify/MINI.toml`, placeholder digest), and
  the rendered Batch command run as Batch would (`docker run --init --user 0:0`, runs store and
  bundles mounted, `BATCH_TASK_INDEX`): both tasks `verdict pass`, records with openEMS's speed
  (67 MCells/s multithreaded at T2, 92 MCells/s `sse` at T1 with `exact-endcriteria`, both on a
  loaded Mac), then `yapnr.exp.fetch` (summary and `--full`) and `openems_plan.py collect` gave
  `runs/<id>/`, `.log`, `.job.json` and the summary (`verify/sim/`).
- **Image secrets.** The build context is `docker/openems/` only (Dockerfile, README,
  cloudbuild.yaml, runtime-packages.sh); no build secrets, tokens or ARGs with credentials; the
  sources are a public clone at a pinned tag.
- **Public repo.** No project ids, buckets, e-mails or machine paths in the diff (tests use
  `example-project`); privacy scan passes.
- **Fixed** (`0a9d535`): `collect --plan DIR` named the summary after DIR instead of the campaign;
  the docs priced Cloud Build per flat build-minute. The billing catalog prices a regional build
  in us-west4 at $0.001808 per vCPU-minute and $0.000396 per GiB-minute: E2_HIGHCPU_32 costs
  **$0.0705 a minute, so $1.06-1.76 for 15-25 minutes** (was $1-1.6). `calib.py run
--budget-usd` now defaults to 2.2 ($4 minus the build's upper end).
- **Noted, not changed.** A job with its own `threads` becomes its own task class: its own Batch
  job and VMs, packed `models_per_vm` to a VM of the campaign's `vm_vcpus`, so it can half-fill a
  VM. Keep one thread count per jobs file (now in the docs). `tofu fmt -check` flags
  `modules/templates/main.tf`, which is the same on main (not this branch).
- Placeholder-digest plans from the check are kept in `verify/` (moved out of `~/yapnr-runs`),
  never to be submitted.

## 0. Variables

```sh
Y=<local-path>
C=<notes>/radar60/cloud-em
PY=<local-path>
yexp() { (cd $Y && PYTHONPATH=$Y $PY -m yapnr.exp.cli "$@"); }
```

## 1. Owner: apply the images registry (once)

```sh
cd $Y/infra/gcp
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud --configuration yapnr-owner auth print-access-token)
tofu plan -var-file=$HOME/.config/yapnr/gcp.tfvars -out=images.plan   # expect 7 to add, 0 change, 0 destroy
tofu apply images.plan
```

Until `claude/cloud-openems` reaches main by PR, an apply from main would delete these 7 resources.

## 2. Build both images (one Cloud Build, about 15–25 min, estimated $1.1–1.8)

```sh
cd $Y && $PY tools/exp/openems_plan.py --configuration yapnr-owner image --run
$PY tools/exp/openems_plan.py digests        # openems:0.37.0-rc3-x86-64 and -x86-64-v4, digests, sizes
```

## 3. Calibrate and validate against the Mac (one command; about 45 min wall)

```sh
cd $C/calib
python3 calib.py gen                 # jobs/<run>.toml (19 runs)
python3 calib.py plan                # pins digests, yapnr exp plan each -> plans.json
nohup python3 calib.py run > run.log 2>&1 &   # submits greedily under 64 vCPU, fetches, collects
python3 calib.py report              # needs numpy (python3 = CommandLineTools 3.9 has it) -> REPORT.md
```

- **Runs (`calib.py` RUNS).** One VM per run. The column model is the stage-2 column + divider, 1.25 M cells (stage2-rf.md §7). The feed model is the rf-uniform TX1 feed as built, 0.83 M cells.
  - Threads 1, 2, 4 and 8 alone; 8 and 16 threads on SMT siblings.
  - Packing that fills c4d-highcpu-8 (4×T1, 2×T2, 1×T4) and c4d-highcpu-16 (8×T1, 4×T2, 2×T4, 1×T8, 16×T1 on SMT).
  - Engines basic, sse, sse-compressed and multithreaded at T1.
  - The generic x86-64 build against the AVX-512 (x86-64-v4) build.
  - The feed model at T4 alone and at 8×T1.
- **Benchmark runs** stop at a fixed 6000 timesteps (`--max-steps 6000 --end-db 1e-9`), so the speed is measured without paying for full runs.
- **Baseline run `base`.** The Mac baselines `tx12-A-e1`, `tx12-D-e3` and `col12-A-e1` run in full with the Mac arguments, 2×T4 per c4d-highcpu-16.
- **Cost.** Plan estimate $1.25 expected, $5.15 ceiling over the 19 campaigns (offline plan, `plans-offline.json`). The us-west4 Spot prices give about $0.4–0.6 for VMs of 10–15 min. Each submit is far under the $5 confirm threshold. `run --budget-usd 2.2` (the default) stops submitting once plan estimates pass $4 minus the build's upper estimate.
- **Report.** `REPORT.md` holds, per run, MCells/s per model and per VM, $/h, $ per 10^12 cell-updates, $ and minutes for one column run to −40 dB (0.0925 T cell-updates), the best throughput-per-dollar and best-latency configurations, the engine comparison, and the S-parameter check.
- **S-parameter tolerance** (`calib/compare_sparams.py MAC_RUN CLOUD_RUN`). Every port as complex S on 58–66 GHz must meet both:

  - |S_cloud − S_mac| ≤ 0.01 (−40 dB);
  - the |S| difference is ≤ 0.1 dB wherever |S| > −20 dB.

  The two runs cannot be bit-identical. openEMS checks the −40 dB energy end criterion every few seconds of wall time, so the runs stop a few hundred steps apart. The AVX-512 build also changes floating-point contraction. For bit-reproducible stop steps in future comparisons, set `openems_options = ["exact-endcriteria"]` on both sides.

## 4. Run a batch of openEMS models (the production path)

Write `JOBS.toml` (full format in the docstring of `$Y/tools/exp/openems_plan.py`; a filled-in
example for the 3 rf-uniform baselines is `verify/JOBS.toml`). Keep one `threads` value per file:
a job with its own `threads` gets its own Batch job and VMs.

```toml
name = "rfuni-verify"                       # [a-z0-9-], at most 41 characters
image = "openems:0.37.0-rc3-x86-64-v4"      # pinned to its digest by `plan`
threads = 2                                 # PROVISIONAL: take T and K from calib/REPORT.md
models_per_vm = 4                           # 2*K*T vCPUs: 16 = c4d-highcpu-16 (8 = -highcpu-8)
memory_gb = 3
max_wall_s = 5400                           # per model; no checkpoints: a preempted model restarts
[inputs]                                    # copied into every task's work dir, by name
code = "/abs/path/rf-uniform/code"
models = "/abs/path/rf-uniform/models"
w = "${REPO}/examples/radar60/rf"
[env]
RFMACRO_ROOT = "w"
[[jobs]]
id = "tx12-A-e1"                            # -> runs/tx12-A-e1/, .log, .job.json
script = "code/feed_sim.py"
args = ["models/tx12-A.json", "--out", "{out}", "--excite", "TX1.P0", "--threads", "{threads}", "--dump"]
# engine = "multithreaded"; openems_options = ["exact-endcriteria"]   (optional, campaign or job)
```

```sh
$PY $Y/tools/exp/openems_plan.py plan JOBS.toml --out $C/campaigns/<name>
yexp plan $C/campaigns/<name>/campaign.toml --backend gcp-batch   # prints cid, VMs, $ expected/ceiling
# log the ceiling in ../gcp-spend.md, then:
yexp submit <cid>
yexp status <cid>                          # repeat until done == tasks
yexp fetch <cid>                           # summaries: logs, records, out/<id>/*.json, *.csv
yexp fetch <cid> --full                    # only if field dumps (h5/npz) are needed (egress)
$PY $Y/tools/exp/openems_plan.py collect <cid> --dest $C/<tree>   # <tree>/runs/<id>/, .log, .job.json, summary
# expected for verify/JOBS.toml (3 models, T2, 4 per c4d-highcpu-16): 1 VM, plan $0.11 expected, $1.01 ceiling
```

Several campaigns may run at once. Keep the sum of their VMs within 64 vCPUs.

## Recommended configuration (provisional until REPORT.md)

openEMS's multithreaded FDTD engine is limited by memory bandwidth. The Mac runs 108 MCells/s at 4 threads, and parallel Mac runs vary from 62 to 145 MCells/s.

- **Throughput per dollar:** expected to be full VMs of low-thread models: 4×T2 or 8×T1 per c4d-highcpu-16 (the `p1`, `p2` and `s1` runs decide).
- **Single-model latency:** expected to be T8 alone on c4d-highcpu-16 (`f8`, against `f16` with SMT).

**Prices** (us-west4 Spot): c4d-highcpu-8 $0.077/h, c4d-highcpu-16 $0.151/h.

**Cost per model** = VM $/h × cell-updates / (VM MCells/s × 3.6e9). At only the Mac's rate (108 MCells/s per c4d-highcpu-16), one column run would cost $0.036 and take 14 min. The calibration replaces this with measured rates.

**Models at once** under the 64-vCPU Spot quota (core packing, 2 vCPUs per thread): T1 32, T2 16, T4 8, T8 4. With SMT packing (1 vCPU per thread): T1 64.

## Limits

- **Quota and spend.** At most 64 vCPUs of Spot C4D at once in the region. Shared with other agents: check before long runs. The confirm threshold is $5 per submit, refusal at $25, the monthly budget is $100, and the radar program cap is $50 (log each campaign in `../gcp-spend.md`).
- **Tasks.** At most 4 h of wall time per task (`max_task_wall_s`) and 12 h per campaign. Preemption retries a task up to 3 times from the start (openEMS does not checkpoint).
- **Inputs.** Inputs are copied into the campaign and uploaded as bundles. Scripts must use relative paths, `{out}` and `{threads}`.
- **Images.** The images are linux/amd64 only (C4D; the AVX-512 build needs Zen 4/5 or another CPU with AVX-512). The registry keeps 5 versions.
