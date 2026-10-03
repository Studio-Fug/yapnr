# Design: cloud and HPC experiment backends (GCP Batch, Slurm)

Status: **proposal, implemented offline**, written 2026-10-02 for the owner's request of the same
day: stand up GCP so experiments run on cheap, scalable compute, "pull in Slurm (or something
that's an even better fit)", and get everything ready to deploy experiments on C4D Spot instances.
The implementation (`yapnr/exp`, `infra/gcp`) and the owner's runbook are described in
[Cloud and HPC experiments](../cloud-experiments.md); nothing has run on Google Cloud yet. No
Google Cloud credentials exist, and no Google Cloud API was called: every fact comes from public
documentation and pricing pages, accessed 2026-10-02 (§22). Prices are USD
list prices and drift daily. Numbers marked "est." are estimates, not measurements. Owner-specific
values (project, buckets, accounts, regions in use) never appear in this repository; they live in
`~/.config/yapnr/cloud.toml` (§11.4).

## 1. Summary

**The answer to the request.** Run the experiments on **Google Cloud Batch** with Spot VMs,
driven by a small, backend-neutral layer in yapnr (`yapnr exp`). Do not stand up Slurm on GCP. The
same task manifest renders three ways: as Batch job JSON, as an `sbatch --array` script that runs
the published image under **Apptainer** on an academic Slurm allocation, and as a niced local
process pool on the Mac.

| Topic            | Decision                                                                                                                                                                      | Main reason                                                                                                                       | Rejected (§)                                                                                                |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Fabric on GCP    | Cloud Batch jobs, rendered and submitted by `yapnr exp`                                                                                                                       | no service fee, nothing runs between campaigns, Spot retries keyed on exit code 50001, 100,000 tasks per job                      | Slurm via Cluster Toolkit, GKE + Kueue, SkyPilot, Ray, HTCondor, dsub (§4.3)                                |
| HPC              | the same manifest rendered to `sbatch --array` + Apptainer, submitted from the login node                                                                                     | academic centres run Slurm with Apptainer; the image already runs under any UID                                                   | Slurm on GCP as a rehearsal (a single-node Slurm in CI tests the scripts instead) (§5)                      |
| Default instance | **C4D Spot**, `c4d-highcpu-16`, one task per physical core, in a region picked per campaign from fresh prices (provisional home region: `us-west4`), confirmed by calibration | the fastest single-thread core Batch supports: shortest hard rungs, least preemption exposure, closest to the Mac's search budget | `us-central1` as the default; T2D or C3D as the default (cheaper per result, slower; ranked fallbacks) (§6) |
| Images           | GHCR stays the source; an Artifact Registry remote repository (upstream `https://ghcr.io`) per region; everything pinned by digest; Apptainer SIF built from the same digest  | VMs need no internet access; same-region pulls are free                                                                           | direct GHCR pulls, `crane copy` mirroring, Cloud Build, image streaming (§7)                                |
| Storage          | two private buckets: `inputs` (content-addressed bundles) and `runs` (campaigns, results, checkpoints, with lifecycle rules)                                                  | private inputs keep soft delete and never expire; results do                                                                      | one bucket; Filestore or NFS (§8)                                                                           |
| Identity         | impersonation of a `yapnr-submit` service account; no key files; owner login only for bootstrap; an API key only for price lookups                                            | an API key cannot authorize Batch or Compute Engine                                                                               | service-account JSON keys; a general API key (§9)                                                           |
| Spend            | quota ceiling, per-submit estimate with confirm and refuse thresholds, campaign deadlines, budget with a kill switch, lifecycle rules                                         | budget data lags by hours, so only quota is a hard cap                                                                            | Google's "disable billing" recipe (§10)                                                                     |
| Task layer       | `yapnr-task-v1` JSONL manifest; one in-task wrapper; a `_DONE` marker per task; `fetch` rebuilds the local layout                                                             | every task is idempotent and restartable; existing collectors work unchanged                                                      | dsub's task TSV, Batch `taskEnvironments` (limited by the 1 MB job definition) (§11)                        |
| Infrastructure   | OpenTofu root module under `infra/gcp`, HCL kept Terraform-compatible; quotas requested outside it                                                                            | licence; a later `apply` must not undo a kill-switch quota cut                                                                    | Terraform (BUSL), gcloud scripts only (§16)                                                                 |

### 1.1 What the owner provisions instead of an API key

An API key cannot run anything here: Batch and Compute Engine authorize a principal through IAM,
and an API key is not one [44]. Provision these instead once billing exists (runbook in §17):

1. **A Google account with Owner on a new project**, used only for the bootstrap and for
   `tofu apply`, then signed out on the Mac.
2. **The billing account** linked to that project, and permission to create budgets on it
   (Billing Account Administrator or Billing Account Costs Manager) [51].
3. **A day-to-day identity for the Mac** (ideally a separate, low-privilege Google account) whose
   only grant is `roles/iam.serviceAccountTokenCreator` on the `yapnr-submit` service account.
   Agents and the owner submit jobs by impersonation [46]. No service-account key files.
4. **Optionally, an API key restricted to the Cloud Billing API**, for the public price catalog
   the cost estimator reads [45]. It is kept in the macOS Keychain, never in a file.
5. **Public GHCR packages.** Switch `studio-fug/yapnr` and `studio-fug/yapnr-kicad` to public, as
   [releases](../releases.md#one-time-repository-settings) already plans; the switch cannot be
   undone [67]. Checked 2026-10-02: anonymous pull tokens for both still return `401`, while a
   public control image returns `200`.

## 2. Goals, non-goals and constraints

Goals:

- Run the experiment families (regression ladder, tool comparison, private Splanc evaluations, RF
  topology optimisation, later FEA) on elastic, cheap compute with nothing standing while idle.
- One task definition for three backends: `gcp-batch`, `slurm` and `local`.
- Results land in the directory layout local runs produce, so `tools/ci/ladder_summary.py`, the
  animation renderer, the benchmark harness and the halving driver read them unchanged.
- Hard, layered spend limits that still hold when one layer fails. Nothing spends money without an
  estimate shown first.
- Private inputs never leave private storage. Nothing private enters this repository, an image, a
  label or a log line.
- Every result records the image digest, the git commit, the task spec hash and the machine it ran
  on.

Non-goals:

- Multi-user tenancy, GPUs, long-lived services and interactive clusters.
- Submitting from CI: code in a public pull request must never be able to spend money.
- Replacing the GitHub `ladder` workflow, which stays the per-PR and nightly check.
- Cross-cloud Spot hunting (a possible later layer, §4.3).

Constraints: a public AGPL repository with a privacy scan; one owner; a busy development Mac (the
submitter must stay light and local runs niced, per [AGENTS.md](../../AGENTS.md)); Docker is not
usable on that Mac, so the local backend runs the host toolchain; and the engine's wall-clock
budgets make results depend on core speed (§3.3).

## 3. Workloads

### 3.1 Units of work

Every workload is embarrassingly parallel across its units. Times were measured on the
development Mac (Apple M4, 10 cores, 16 GB).

| Workload                 | One task                                                        | Cores                                         | Wall time per task                                                   | Output       | Restart                                                       |
| ------------------------ | --------------------------------------------------------------- | --------------------------------------------- | -------------------------------------------------------------------- | ------------ | ------------------------------------------------------------- |
| Ladder cell              | `run.py --case C --seed S` with one configuration               | 1 (pure-Python A\*, short `kicad-cli` bursts) | 6 s to 5 min with the 8/3 initial pool; hard rungs up to about 1.5 h | about 1.5 MB | from scratch                                                  |
| Initial pool (8/3)       | not a separate task: runs inside the cell's place-route process | as the cell                                   | adds 1–3x to the cell                                                | in the cell  | as the cell                                                   |
| MC halving, stage 0      | one task (`--procs N`)                                          | N                                             | about 30 s                                                           | small        | the driver resumes per record                                 |
| MC halving, evaluation   | one candidate evaluation (`pnr.full_iteration`)                 | 2–5 (two track workers, two KiCad processes)  | rung 1: 12–37 min; native: 48–52 min; deep: up to 16 h observed      | 50–210 MB    | from scratch                                                  |
| Splanc A/B evaluation    | one (arm, placement)                                            | 2–5                                           | 45–60 min at `--seconds 900`                                         | about 75 MB  | from scratch                                                  |
| Tool-comparison cell     | (rung, tool, seed): run the tool, then judge with `measure.py`  | Freerouting `-mt 4`; others 1                 | seconds on small rungs                                               | 1–3 MB       | from scratch                                                  |
| RF topology optimisation | `python -m yapnr.rf.cases run NAME --out DIR`                   | 4 torch threads                               | 22–110 s per iteration; 60 iterations in about 109 min               | under 1 MB   | **resumable**: a checkpoint per iteration, bit-for-bit resume |
| FEA (later)              | one study                                                       | multi-threaded                                | minutes                                                              | varies       | from scratch                                                  |

Memory: a hard ladder cell and an RF run peaked at about 0.17 GB RSS. Evaluation memory was never
recorded; tasks budget 3 GB for a ladder cell and 8 GB for an evaluation until the wrapper's
`maxrss` records (§14) replace these guesses.

### 3.2 What already runs in the image

The ladder already runs inside `ghcr.io/studio-fug/yapnr` in CI (`.github/workflows/ladder.yaml`:
entrypoint `/usr/local/bin/yapnr-kicad-env`, `--init --shm-size 1g`, any UID, the checkout's
engine sources on the image's runtime). The image's `/opt/venv` has numpy and torch, so RF runs
too. Before the private evaluations can run in the cloud (phase 3), these Mac-specific pieces
need replacing:

1. absolute host paths in launchers, environment files and each candidate's `blocks.json`;
2. engine modules that default to a macOS KiCad and read `PNR_KICAD_CLI`/`PNR_KICAD_PYTHON` but
   not the image's `YAPNR_KICAD_*` (the task sets both);
3. the Rust search backend, built today as a macOS library only (a Linux build for amd64 and arm64
   is needed, or comparisons with the Mac are confounded by the Python fallback);
4. `libngspice` for the SI stage (its presence in the image is unchecked);
5. Mac-only machinery (`caffeinate`, disk guards, prune scripts, "other agent busy" checks), which
   the wrapper's disk budget, prune globs and the scheduler replace;
6. images for the other routers of the tool comparison (Freerouting on a JDK, KRT, tscircuit).

### 3.3 Determinism and comparability

Placement and search are seeded, but many router steps have wall-clock deadlines, so a slower
core does less search in the same budget. Evaluations ran 11–28x over their budget on the loaded
Mac. Platforms also differ: the same inputs and seed give a different board on another operating
system or architecture, because torch rounds a few float operations differently (see
[containers](../containers.md#tags)). Consequences for the design:

- A campaign marked `determinism: wall_clock_budgeted` runs on exactly one machine type, and A/B
  arms of one comparison run in one job, interleaved.
- Cloud results are compared with a cloud baseline, never with Mac numbers.
- Every record carries machine type, CPU model, vCPU count, threads per core, platform and load
  (§14).

## 4. Fabric: Google Cloud Batch

### 4.1 Decision

Batch is a managed queue that creates VMs for a job, runs its tasks in containers and deletes the
VMs when the tasks are done. It fits a workload of thousands of independent, restartable,
minutes-to-hours tasks that arrive in bursts:

- **No service fee.** "There is no additional cost for using Batch" [1]. Between campaigns only
  storage is billed.
- **Scale.** Up to 100,000 tasks per task group and 5,000 parallel tasks per job, 2,000 concurrent
  VMs for a single-zone job and 4,000 for a multi-zone job [2].
- **Spot retries by cause.** A task killed by Spot preemption exits with reserved code 50001. A
  lifecycle policy `RETRY_TASK` on listed exit codes retries only those and fails the rest at once;
  `maxRetryCount` is 0–10 [5, 6].
- **Credential-free validation.** Google's Batch v1 discovery document (revision 20260723) is
  public [17], so generated job JSON is checked offline in unit tests.

### 4.2 Batch facts the design relies on

- **One machine type and one region per job.** `allocationPolicy.instances` supports only
  `instances[0]`, and a job has one task group [4, 17]. Since 2026-07-31 `allowedLocations` must lie
  in the job's own region [8]. Region and machine-type fallback therefore live in `yapnr exp`, not
  in Batch. Instance flexibility (several machine types per job) is in Preview since 2026-07-17 and
  cannot mix 4th-generation types with 2nd-generation ones [8, 9]; it is not used until GA.
- **Tasks per VM.** Batch divides the VM's vCPUs by the task's `cpuMilli`, unless
  `taskCountPerNode` is set; the maximum is 20 tasks per VM [3].
- **Exit codes.** 50001 Spot preemption; 50002 the VM stopped reporting; 50003 VM rebooted; 50004
  task could not be cancelled; 50005 the task exceeded `maxRunDuration` or a runnable timeout;
  50006 VM recreated [6]. The retry attempt number is exposed as `BATCH_TASK_RETRY_ATTEMPT` [5].
- **Queued jobs fail after 2 days; running jobs after 14 days** [2].
- **Containers** can come from any registry; private registries take a Secret Manager reference
  for the password [17]. Image streaming needs Artifact Registry in the same location and then
  allows only `imageUri`, `commands`, `entrypoint` and `volumes` [13].
- **Cloud Storage volumes** are mounted with Cloud Storage FUSE, and `mountOptions` takes `gcsfuse`
  flags [12, 17]. FUSE is not POSIX: no file locking, concurrent writes to one object fail, and
  small files are slow [31].
- **Labels** in `allocationPolicy.labels` reach the VMs and disks (and therefore billing); job
  labels reach the job and its log entries [15, 17].
- **Networking without external IPs** works with Private Google Access [14].
- **Known quirks.** A timeout log does not say whether the task or the runnable limit fired, so
  the two are set to different values; Pub/Sub job notifications can skip intermediate states
  [16].

### 4.3 Rejected alternatives

| Option                                                       | What it offers                                                        | Why not (now)                                                                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------------------ | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Slurm on GCP (Cluster Toolkit v1.105.0, `slurm-gcp` v6) [56] | `sbatch` syntax identical to an HPC centre; dynamic Spot node sets    | a standing controller and login node (both `c2-standard-4` by default, $0.208808/h each in `us-central1` [29], about $250–300/month together while idle); the controller is a single point of failure; images and Slurm versions need upkeep; preempted jobs return only if requeue is enabled [57]. The render step gives the same `sbatch` scripts without it. |
| GKE (Autopilot or Standard) with Spot, Kueue and JobSet      | the best multi-tenant story; Kueue quotas and fair sharing [63]       | $0.10 per cluster-hour (one cluster covered by a monthly credit) [61], 15 s grace for regular Spot pods [62], and cluster upgrades and compute classes to own. Too much operation for one owner.                                                                                                                                                                 |
| SkyPilot 0.13 (Apache-2.0)                                   | managed jobs recover from preemption across regions and clouds [59]   | a dedicated cluster per job and a controller VM; its Slurm support is "under active development" and drives the login node over SSH [60], which MFA makes brittle. A candidate later for multi-cloud Spot hunting.                                                                                                                                               |
| Ray                                                          | in-process Python tasks and actors                                    | our tasks are subprocess-heavy (`kicad-cli`, KiCad Python) and independent; losing the head node loses the cluster.                                                                                                                                                                                                                                              |
| HTCondor                                                     | the best high-throughput semantics (DAGs, file transfer, checkpoints) | a standing central manager and access point: Slurm's cost and operation profile. Worth a renderer only if an OSPool-style allocation appears.                                                                                                                                                                                                                    |
| dsub 0.5.6 [64]                                              | a thin CLI on Batch with a task-table model                           | bioinformatics conventions and a dependency for about 200 lines we own anyway; its task-table idea is kept.                                                                                                                                                                                                                                                      |
| Plain managed instance groups or VMs with a queue            | full control                                                          | rebuilds what Batch does (provisioning, retries, cleanup) and leaves VMs running when the queue code fails.                                                                                                                                                                                                                                                      |

## 5. HPC path: Slurm with Apptainer

The owner is applying for allocations through open-source grant programmes; most such centres run
Slurm with Apptainer (or Singularity) and no Docker. The `slurm` backend renders the same manifest
into one job array per resource class:

- **Image.** On the login node (compute nodes often have no internet):
  `apptainer pull yapnr.sif docker://ghcr.io/studio-fug/yapnr@sha256:<index digest>` [65]. Public
  packages make this credential-free; otherwise `APPTAINER_DOCKER_USERNAME`/`PASSWORD` with a
  `read:packages` token. The arm64 image covers Arm clusters. The SIF's sha256 and the OCI digest
  are recorded in every result.
- **Arrays and chunks.** `#SBATCH --array=0-N%K`. Each array element runs a chunk of manifest lines
  in sequence, because `MaxArraySize` defaults to 1001 [58] and centres add QOS caps. The chunk
  size comes from the site's limits in the owner config.
- **Requeue.** `--requeue` plus `--signal=B:USR1@300`: the batch script runs the container in the
  background, traps `USR1`, forwards it, waits, and requeues the element with
  `scontrol requeue` when tasks remain. Already finished tasks are skipped through their `_DONE`
  markers, so a requeued element resumes where it stopped.
- **Run.** `apptainer exec --cleanenv --containall` with the store and a scratch directory bound
  in runs the same wrapper as on GCP (the script is in §11.7). Apptainer runs as the calling user
  with a read-only image [65]; the image already supports any UID and points `HOME` at a private
  directory, which `kicad-cli` needs for its configuration.
- **Submission.** The owner (never an automated SSH session) works on the login node. `plan`
  can run inside the SIF (`apptainer exec yapnr.sif yapnr exp plan --backend slurm ...`), so
  nothing needs installing for it. `submit`, `status` and `cancel` call `sbatch`, `sacct` and
  `scancel`, which exist only on the host: they run from a user-space install of the yapnr wheel
  (`uv tool install`, which also provides Python 3.11), or through the `submit.sh` and
  `status.sh` helpers the plan renders, which need only bash and Slurm and write the same
  submission record. The store is a directory on the site's project or scratch file system with
  the same layout as the bucket (§8).
- **Results** come back with `rsync` (or `gcloud storage rsync` where the site allows it) and are
  assembled by the same `yapnr exp fetch` (§11.8).
- **Private campaigns** are refused on a Slurm site unless the owner marks that site
  `private_ok = true` after reading its data policy.

A single-node Slurm with Apptainer in a GitHub Actions job tests the rendered scripts (§18). If an
allocation turns out to be HTCondor-based, a fourth renderer follows the same pattern.

## 6. Instances, regions and calibration

### 6.1 Spot prices

Spot prices per vCPU-hour and per GB-hour [19], fitted over standard, highcpu and highmem shapes.
T2D and H3 prices include memory. The `us-central1` column is the live page on 2026-10-02; the
cheaper-region column is a public snapshot of the Cloud Billing Catalog of 2026-09-24 [30],
restricted to regions with Batch.

| Family (CPU)                | SMT             | Batch  | Boot disk       | `us-central1`       | Cheaper region (snapshot)                                          | Est. $ per 1000 ladder evaluations, `us-central1` / cheapest |
| --------------------------- | --------------- | ------ | --------------- | ------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------ |
| C4D (EPYC Turin)            | 2 vCPU per core | yes    | Hyperdisk only  | $0.0132 / $0.00152  | `us-west4` $0.0076 / $0.00087; `asia-south1` $0.0088               | 3.2 / 1.9                                                    |
| C4 (Granite/Emerald Rapids) | 2               | yes    | Hyperdisk only  | $0.0207 / $0.00236  | `northamerica-northeast1` $0.0051; `me-central2` $0.0054           | 5.5 / 1.3                                                    |
| C3D (EPYC Genoa)            | 2               | yes    | PD or Hyperdisk | $0.0080 / $0.00106  | `northamerica-northeast1` and `-2` $0.0029; `europe-west9` $0.0031 | 2.5 / 0.9                                                    |
| T2D (EPYC Milan)            | 1 vCPU = 1 core | yes    | PD only         | $0.0253 (with 4 GB) | `asia-northeast3` $0.0049; `europe-southwest1` $0.0073             | 4.0 / 0.8                                                    |
| C4A (Axion, Arm)            | 1 vCPU = 1 core | yes    | Hyperdisk only  | $0.0139 / $0.00158  | `europe-west4` $0.0041 / $0.00047                                  | 3.1 / 0.9                                                    |
| N2D (Milan)                 | 2               | yes    | PD only         | $0.0147 / $0.00197  | `europe-west10` $0.0072                                            | 5.2 / 2.5                                                    |
| N4                          | 2               | yes    | Hyperdisk only  | $0.0168 / $0.00190  | `asia-east2` $0.0044                                               | 6.0 / 1.6                                                    |
| E2                          | 2               | yes    | PD only         | $0.0131 / $0.00175  | `us-west4` $0.0032                                                 | 5.9 / 1.5                                                    |
| H3 (88-core host)           | off             | yes    | PD or Hyperdisk | $0.0333 (with mem)  | `us-east4` $0.0052                                                 | 5.5 / 0.8 (Spot status unclear)                              |
| N4D, T2A, H4D               | –               | **no** | –               | –                   | –                                                                  | not usable with Batch [7]                                    |

The per-evaluation estimate assumes 300 CPU-seconds and 2.5 GB per ladder evaluation on an M4
performance core, scaled by PassMark single-thread ratings (C4D 0.79 of the M4, C4 0.73, C3D 0.64,
T2D 0.57, C4A about 0.52), plus 8% for preemption rework and VM start-up. Python probably favours
the M4 more than PassMark does, so cloud times may be longer; calibration (§6.6) replaces every
speed factor with a measurement. Two observations drive the region policy: `us-central1` (which
shares Spot prices with `us-east1` and `us-west1`) is above the cross-region median for most
families, and prices move: T2D in `us-central1` went from $0.010 to $0.025 per vCPU-hour between
April and September 2026 [30].

### 6.2 Default: C4D Spot, `c4d-highcpu-16`

C4D is the owner's suggestion, and it is the right default, for reasons beyond raw cost:

- **Fastest core Batch supports.** A 1.5 h hard rung on the Mac takes about 1.9 h on C4D but about
  2.6–2.8 h on T2D or C3D (est.). A shorter task loses less work to a preemption.
- **Comparability.** Wall-clock-budgeted search does the most work per budget on the fastest core,
  which keeps cloud results nearest to what the engine does on the Mac.
- **Cost is within about 2x of the cheapest** per result (est. $1.9 against $0.8–0.9 per 1000
  evaluations in the cheapest regions), and a sweep of 1000 ladder cells costs a few dollars on
  any of them.

The shape: **`c4d-highcpu-16`** (about 2 GB per vCPU). Sixteen vCPUs spread the per-VM overheads
(boot disk, start-up, image pull) over 8 tasks, stay under Batch's 20-tasks-per-VM cap, and keep a
single preemption's blast radius at 8 tasks. Evaluations that need more memory use
`c4d-standard-16` (about 4 GB per vCPU). C3D and T2D stay configured as ranked fallbacks, and the
calibration may promote one of them for campaigns where throughput matters more than latency.

### 6.3 Packing and SMT

On C4D, C4, C3D, N2D, N4 and E2 each vCPU is one hyperthread, and billing is per vCPU even when
the second thread is disabled [22]. One single-threaded task per vCPU runs each task at an assumed
0.55–0.65x of a full core; one task per core costs twice as much per task-hour and runs about 1.7x
faster, so cost per result is within about 10% either way (est.). The default is **one task per
physical core** (`cpuMilli: 2000` with SMT on), because it halves hard-rung wall time and with it
preemption exposure, and leaves the sibling thread to `kicad-cli` bursts. The calibration measures
both packings. T2D, C4A and H3 give a full core per vCPU and pack one task per vCPU.

### 6.4 Boot disks and instance templates

C4, C4D, C4A and N4 accept only Hyperdisk; T2D, N2D and E2 accept only Persistent Disk; C3D takes
either [20, 21]. Batch's `Disk.type` still documents only `pd-*` types [10, 17], while the instance
flexibility page says 4th-generation types "don't support persistent disks" and 3rd-generation
and earlier types "use persistent boot disks by default" [9]. Whether a plain Batch instance
policy gets a working Hyperdisk boot disk for C4D is therefore **unverified**. The design does not
depend on it:

- Hyperdisk families run from **global instance templates** (the only kind Batch accepts [11, 17])
  that set a `hyperdisk-balanced` boot disk of 30 GiB pinned to the free baseline of 3,000 IOPS and
  140 MiB/s, about $0.0033 per VM-hour instead of about $0.007 with default performance (est. from
  [23, 24]); Spot provisioning with `DELETE` on termination; no external IP; the runner service
  account; and, for the packing experiment, `threads_per_core = 1`. Batch uses the template's
  network settings [17].
- One template per (shape, region, provisioning model), because a template names a regional
  subnetwork. OpenTofu creates them from the owner's list (§16).
- Persistent-disk families (T2D, N2D, E2) use a plain instance policy with `pd-balanced`.
- The first smoke job (§17) also tries C4D without a template; if that works, the template stays
  only for the settings Batch has no field for.

### 6.5 Region choice

Because a job is pinned to one region and Spot prices differ 2–6x between regions, `yapnr exp plan`
picks the region per campaign: the cheapest of the owner's enabled regions by (Spot price from the
Billing Catalog) x (measured speed factor), among regions that have quota, a subnet, an Artifact
Registry cache and templates. A job that stays `QUEUED` or `SCHEDULED` longer than
`queue_timeout_s` (default 30 min) is cancelled and resubmitted to the next region, but only with
`--fallback` and only within the original estimate. The buckets stay in the home region; tasks in
other regions move megabytes per task, which inter-region transfer pricing makes negligible.

The **provisional home region is `us-west4`** (the cheapest C4D Spot price in North America in the
snapshot), with `northamerica-northeast1` as the second region (cheap C3D and C4). Both choices
must be confirmed at bootstrap with `yapnr exp prices` and `gcloud compute machine-types list`
and are recorded only in the owner config. Before relying on a region, `gcloud beta compute advice
capacity` (obtainability, Preview) and `advice capacity-history` (30 days of preemption rates and
a year of prices, Preview) are read [27, 28].

### 6.6 Calibration plan (phase 1, capped at $10)

Once credentials and quota exist:

1. **Workload.** A pinned yapnr commit and image digest. Six ladder cases x 2 seeds (two easy, two
   medium, two hard ones under 20 minutes on the Mac), one `kicad-cli` DRC of a reference board,
   one FDTD kernel, and a small pyperformance subset as a portable microbenchmark. The same set
   runs on the Mac (niced) as the baseline.
2. **Shapes**, 8 vCPUs, one VM each, in two regions chosen from fresh prices: `c4d-highcpu-8`,
   `c4d-standard-8`, `c4-highcpu-8`, `c3d-highcpu-8`, `t2d-standard-8`, `n2d-highcpu-8` (minimum
   CPU platform AMD Milan), `n4-highcpu-8`, `c4a-highcpu-8` (arm64 image) and `e2-standard-8` as a
   control; `h3-standard-88` only if `advice capacity` shows Spot obtainable. SMT families run
   twice: 8 tasks per VM, and 4 tasks per VM.
3. **Measured per task:** wall time, CPU seconds, peak RSS, effective clock under load; per VM:
   time from create to running, image pull time, preemptions, and the Spot price in effect.
4. **Quality equivalence.** Not bitwise equality (wall-clock budgets and platforms differ, §3.3)
   but the ladder's verdicts and quality figures (opens, violations, vias, copper length) within
   the run-to-run spread of repeated runs on one machine type. Each shape also repeats one cell to
   measure that spread.
5. **Selection rule.** Rank by measured cost per completed result (VM, disk, IP and egress cost,
   including retried work), subject to: hard-rung p95 wall time at most 2x the Mac, obtainability
   at least 0.5 at the needed VM count, Batch support and quality equivalence. Keep the top two
   families x top three regions as the ranked list in the owner config, and refresh it weekly
   from fresh prices x measured speed (`yapnr exp prices --rerank`).

Estimated cost: about 22 VM-hours of 8 vCPUs, est. $3–5 with disks, well under the $10 cap.

## 7. Images

- **Source of truth: GHCR**, built and attested by `image.yaml` for amd64 and arm64. Tasks always
  name an **index digest**; the record stores the platform manifest digest actually pulled.
- **Pull path on GCP: an Artifact Registry remote repository** per enabled region with the custom
  upstream `https://ghcr.io` (GHCR is listed as an example custom upstream; Docker Hub is the only
  preset) [37]. It acts as a pull-through cache, so VMs need no internet access at all (no NAT, no
  external IP), same-region pulls are free, and storage costs about $0.10/GiB-month after 0.5 GiB
  [39]. Tasks reference `<region>-docker.pkg.dev/<project>/ghcr/studio-fug/yapnr@sha256:...`.
- **Prerequisite: public packages.** If the owner keeps them private, the remote repository takes a
  GitHub token with `read:packages` from Secret Manager [38]; the design works either way, but a
  public image is simpler and matches the release plan.
- **Runtime guard.** The planner refuses a campaign whose source commit pins a different runtime
  lock (`docker/yapnr/runtime-<arch>.lock`) than the chosen image's `/opt/venv`, instead of
  building an overlay environment as the ladder workflow does; the image for that commit is built
  on merge anyway.
- **Benchmark tool images** (`yapnr-bench-freerouting`, `-krt`, `-tscircuit`) are small images
  `FROM yapnr-kicad`, so the judge (`measure.py`, KiCad Python) runs in the same task. Each tool's
  licence is checked before an image redistributes it; a tool that cannot be redistributed is
  downloaded by the task from a pinned release with a sha256.
- **Private data never goes into an image.** Private inputs are bundles in the inputs bucket
  (§8), staged at task start.

Rejected: **direct GHCR pulls** (needs an external IP, $0.0025/h per Spot VM, or Cloud NAT at
$0.0014 per VM-hour plus $0.045/GiB processed, about $9 for a 2 GiB image on 100 VMs [35, 36]);
**`crane copy` into a standard repository from CI** (deterministic, but needs CI write access to
GCP; kept as the fallback if the remote repository misbehaves); **Cloud Build** (a second image
pipeline); **image streaming** (forbids `--init` and `--shm-size` [13]; a same-region pull of the
image takes seconds).

## 8. Storage layout and private data

Two buckets in the home region, both with uniform bucket-level access and public access
prevention [32]:

```text
gs://<inputs bucket>/                            soft delete kept (7 days), no lifecycle deletes
  bundles/<sha256>.tar.gz                        content-addressed, immutable: source archives of
                                                 a commit, private evaluation bundles

gs://<runs bucket>/                              soft delete off, lifecycle rules below
  campaigns/<cid>/campaign.json                  the planned campaign (§11.1)
  campaigns/<cid>/tasks.jsonl                    one yapnr-task-v1 per line
  campaigns/<cid>/submissions/<n>.json           one per submit: backend, job ids, region, shape,
                                                 estimate, caps
  campaigns/<cid>/submissions/<n>.indices        array index -> line of tasks.jsonl
  campaigns/<cid>/tasks/<task>/_DONE             written last; names the attempt that completed
  campaigns/<cid>/tasks/<task>/<attempt>/result.tar.gz   all outputs after pruning
  campaigns/<cid>/tasks/<task>/<attempt>/summary/        small files fetched by default
  campaigns/<cid>/tasks/<task>/<attempt>/record.json     provenance and resource record (§14)
  campaigns/<cid>/tasks/<task>/<attempt>/log.tail        last lines of the task's logs
  checkpoints/<cid>/<task>/...                   resumable state (RF; later MC phases)
  control/frozen                                 the kill switch's marker (§10)
```

Rules:

- **Write once, unique paths.** Each attempt writes only under its own prefix
  (`s<submission>r<retry>`) and `_DONE` is the last write, so concurrent or duplicate attempts
  never collide, and the FUSE limits (no locking, no concurrent writers [31]) never matter.
  `_DONE` points at a complete attempt directory.
- **Lifecycle** on the runs bucket [33]: delete `result.tar.gz` objects after 90 days
  (configurable), `checkpoints/` after 14 days, abort incomplete multipart uploads after 1 day;
  keep manifests, records and summaries (kilobytes). Lifecycle changes can take up to 24 hours to
  apply [33].
- **Egress.** Internet egress costs about $0.12/GiB [35], which for full artifacts of 1000 tasks
  (5–50 GB) can exceed the compute. `fetch` downloads summaries by default and full results only
  with `--full` or for named tasks.
- **Encryption.** Google encrypts all data at rest [43]; customer-managed keys add cost and
  operation without protecting against principals that already hold IAM access.

**Private data.** A campaign with `visibility: private` (the Splanc evaluations):

- reads inputs only from the inputs bucket, by sha256;
- gets an opaque campaign id (`<date>-p-<hash>`) and opaque task ids; the human-readable mapping
  stays in the local plan directory under the owner's private results root;
- puts only opaque values in labels (labels appear in billing exports [15]) and only status lines
  in logs (no netlists, no board names);
- is fetched only to the owner's private results root: `fetch` refuses a destination inside a
  checkout whose remote is the public repository;
- is refused on Slurm sites not marked `private_ok`.

The generator that builds private bundles (the referenced Splanc files, inputs, placements and
relocated block boards) lives in the private repository and emits generic `mc-eval` tasks; this
repository holds only the generic kinds.

Rejected: **a single bucket** (private inputs would share soft-delete and lifecycle settings with
disposable results, and one lifecycle mistake could delete them) and **Filestore or NFS volumes**
(a standing cost, and no task needs a shared POSIX file system).

## 9. Identity and access

| Principal                                                            | Used by                                           | Roles                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Owner account                                                        | bootstrap and `tofu apply` only                   | Owner on the project; budget rights on the billing account. Signed out afterwards (`gcloud auth revoke`).                                                                                                                                                                                                                                                  |
| Mac identity (separate low-privilege Google account, or the owner's) | the owner and agents, through `yapnr exp`         | `roles/iam.serviceAccountTokenCreator` on `yapnr-submit` only                                                                                                                                                                                                                                                                                              |
| `yapnr-submit` (impersonated)                                        | plan, submit, status, logs, fetch, cancel         | `roles/batch.jobsEditor`; `roles/iam.serviceAccountUser` **on `yapnr-runner` only** (so it cannot launch jobs as the Compute default account, which has Editor [7]); `roles/logging.viewer`; `roles/storage.objectAdmin` on both buckets; `roles/artifactregistry.reader`; `roles/compute.viewer` (templates, capacity advice); `roles/cloudquotas.viewer` |
| `yapnr-runner` (the job's service account)                           | Batch VMs and containers                          | `roles/batch.agentReporter`, `roles/logging.logWriter` [7]; `roles/storage.objectViewer` on the inputs bucket; `roles/storage.objectAdmin` on the runs bucket; `roles/artifactregistry.reader` on the remote repositories [40]                                                                                                                             |
| `yapnr-guard`                                                        | the kill switch and reaper functions (§10)        | `roles/batch.jobsEditor` (cancel); `roles/cloudquotas.admin` (quota cut); object create on `control/` of the runs bucket (IAM condition on the prefix); optionally `roles/compute.instanceAdmin.v1` for stray VMs                                                                                                                                          |
| Artifact Registry service agent                                      | the remote repository, only if GHCR stays private | `roles/secretmanager.secretAccessor` on the one upstream-token secret                                                                                                                                                                                                                                                                                      |
| `yapnr-ci` (later, Workload Identity Federation)                     | GitHub Actions on `main` only                     | nothing at first; later at most `roles/artifactregistry.writer` on one repository. CI never submits jobs.                                                                                                                                                                                                                                                  |

Details:

- Day to day, a dedicated gcloud configuration sets `auth/impersonate_service_account` to
  `yapnr-submit` (runbook step 6) [46]. The Mac never holds Owner credentials after the
  bootstrap.
- **No service-account keys.** Google discourages them [47]; organisations created since
  2024-05-03 block them by default [48], but a project under a personal account has no
  organisation, so this is a rule we keep ourselves.
- **Workload Identity Federation** for CI, if ever needed, binds on the repository id and
  `refs/heads/main` [49].
- **Network.** A dedicated VPC with one subnet per enabled region, Private Google Access, no
  ingress rules (ingress is denied by default) and no external IPs. Jobs set
  `blockProjectSshKeys`. The project's `default` network, which allows SSH from anywhere [42], is
  not used and may be deleted.
- **Secrets** never appear in job environment variables; Batch takes Secret Manager references
  where a secret is needed [17].

## 10. Cost guards

Budgets do not cap spending: cost data lags by hours and budget notifications arrive "multiple
times per day" [50, 51]. The guards are layered so that each still holds when a higher one fails:

1. **Quota is the hard ceiling.** Spot VMs consume the preemptible CPU quota once it is granted in
   a region, and C4D also counts against its own family quota [25]. The owner requests preemptible
   CPUs in the enabled regions only and leaves on-demand CPU quota small. Worst-case burn is quota
   x Spot price: at `us-central1` prices `c4d-highcpu` costs about $0.0162 per vCPU-hour with its
   memory, so 64 vCPUs (phase 1) burn at most about $1.04/h ($25/day), and 256 vCPUs (phase 2)
   about $4.16/h ($100/day). In `us-west4` the same quota costs about 40% less (snapshot prices).
2. **Per-submit caps** in the owner config, enforced by `plan` and again by `submit`:
   `max_tasks`, `max_parallel_vcpus`, `max_task_wall_s`, `max_retries` (at most 3, only for
   infrastructure exit codes, §13), and `max_campaign_hours`.
3. **Estimate and confirmation.** Every plan prints two numbers, priced from the Billing Catalog
   (cached for a day; with no price the plan fails closed unless the owner passes a price):

   - **expected** = sum over tasks of (calibrated wall time x vCPUs x price) x 1.08, plus disks
     and IPs per VM-hour;
   - **ceiling** = min(every task at its maximum wall time with every retry,
     `max_parallel_vcpus` x `max_campaign_hours` x price) plus per-VM overheads.

   `submit` asks for confirmation when the expected cost exceeds `confirm_usd` (proposed default
   $5) and refuses when the ceiling exceeds `refuse_usd` (proposed: $25 in phase 1 and $100 in
   phase 2), unless `--max-usd` raises it within a hard limit that only the config file can
   change.

4. **Campaign deadlines.** Each job carries a `deadline` label (Unix seconds). A reaper, a Cloud
   Run function triggered by Cloud Scheduler every 15 minutes, cancels jobs past their deadline
   and deletes VMs labelled `yapnr` that outlive their job by an hour. A forgotten campaign
   therefore stops by itself even before the budget reacts.
5. **Budget and kill switch.** A monthly budget (proposed: $50 in phase 1, $150 from phase 2)
   with alerts at 50%, 90% and 100% of actual spend and 100% of forecast, published to Pub/Sub
   [50, 53]. The `budget-guard` function, idempotent because notifications repeat [51]:

   - at 100% of actual spend, cancels every queued, scheduled or running job labelled `yapnr`
     [55] and writes `control/frozen`, which `submit` checks and which running tasks check before
     starting work;
   - at 120%, also lowers the preemptible CPU quota preference to 0
     (`--allow-quota-decrease-below-usage` [54]).

   Google's "disable billing" recipe is not used: it stops everything, and resources "might be
   irretrievably deleted" [52]. `yapnr exp unfreeze` (owner, explicit) clears the marker; quota
   is raised again by the runbook command, which may need re-approval (§21).

6. **Lifecycle rules** (§8) bound storage cost.
7. **Labels for attribution**: `yapnr=1`, `campaign=<cid>`, `kind=<kind>`, `visibility`,
   `submission=<n>` and `deadline` in `allocationPolicy.labels`, which reach VMs and disks [15];
   budgets can filter on labels [53].

Idle cost of the whole infrastructure is est. under $1/month plus stored results [34, 39]:
buckets, the Artifact Registry caches (about one image per region), functions and Pub/Sub within
free tiers, and Cloud Scheduler within its free jobs.

## 11. Task layer and CLI

### 11.1 Concepts

- **Campaign**: a set of tasks planned together from a campaign file, identified by
  `<yyyymmdd>-<kind>-<hash6>` (private: `<yyyymmdd>-p-<hash6>`). `campaign.json` records the
  campaign spec and its sha256, the source commit and bundle sha256, the image index digest, the
  resource classes, the backend artefacts and the estimate.
- **Task**: one unit of work (§3.1), a `yapnr-task-v1` record. Its **spec hash** is the sha256 of
  its canonical JSON (sorted keys, no whitespace, UTF-8) with store paths left symbolic, so the
  same task has the same hash on every backend.
- **Submission**: one submit of some or all of a campaign's pending tasks to one backend, region
  and shape. A campaign can have several (a resubmit after a stockout, the remaining tasks after a
  freeze).
- **Attempt**: one execution of a task (`s<submission>r<retry>`).

The mapping from a backend's array index to a task lives in the store
(`submissions/<n>.indices`), not in Batch's `taskEnvironments`: that field would put every task
into the job definition, which is limited to 1 MB [2], and it exists only on Batch.

### 11.2 `yapnr-task-v1`

<!-- prettier-ignore -->
```yaml
schema: yapnr-task-v1
id: ladder/08-chaser-20-plane/s1 # stable; private campaigns use opaque ids
campaign: 20261002-ladder-3f9a1c
kind: ladder-cell # ladder-cell | bench-cell | mc-place | mc-eval | rf-run | fea | smoke | calibrate
visibility: public # private: inputs bucket only, opaque ids and labels
image: ghcr.io/studio-fug/yapnr@sha256:<index digest>
entrypoint: /usr/local/bin/yapnr-kicad-env
command: ['${PYTHON}', src/hardware/pnr/regression/run.py, --repo, src, --out, out/run,
          --python, '${PYTHON}', --kicad-python, '${KICAD_PYTHON}', --kicad-cli, '${KICAD_CLI}',
          --library, '${FOOTPRINTS}', --case, 08-chaser-20-plane, --seed, '1',
          --initial-pool, --initial-starts, '8', --initial-finalists, '3', --timeout, '1800']
env: { OMP_NUM_THREADS: '1', OPENBLAS_NUM_THREADS: '1', MKL_NUM_THREADS: '1' }
inputs:
  - { dest: src, bundle: <sha256>, kind: source } # git archive of the commit, verified
outputs:
  root: out
  summary: [run/summary.json, run/provenance.json, 'run/*/result.json', 'run/*/drc.json']
  prune: ['**/route-*/*.kicad_pcb']
done: { file: out/run/summary.json, json: { complete: true } }
verdict: { file: out/run/summary.json, json_path: passed } # a FAIL is a result, never a retry
resources: { cpus: 1, memory_gb: 3, disk_gb: 4, max_wall_s: 7200 }
restart: scratch # scratch | resume (checkpoint restored first)
checkpoint: null # rf-run: { path: out/checkpoint.npz, sync_every_s: 300, on_signal: true }
determinism: wall_clock_budgeted # seeded | wall_clock_budgeted (one machine type per campaign)
labels: { arm: baseline, seed: '1' } # analysis labels; only allowlisted keys become cloud labels
```

- `cpus` counts **physical cores**; the renderer turns it into `cpuMilli` per family (2000 per core
  on SMT families).
- `${PYTHON}`, `${KICAD_CLI}`, `${KICAD_PYTHON}` and `${FOOTPRINTS}` come from the backend's
  toolchain profile: the image profile (`/opt/venv/bin/python`, `/usr/bin/kicad-cli`,
  `/usr/bin/python3`, `/usr/share/kicad/footprints`) or the local profile (the headless KiCad
  copy from `yapnr doctor` and the owner config). The wrapper also exports `PNR_KICAD_CLI` and
  `PNR_KICAD_PYTHON`, which the engine reads.
- Retries are not a task field: the policy is global (§13) and capped by the owner config.

### 11.3 Campaign files

Campaign files are small TOML files that live wherever the experiment lives (public ones may be
committed under `experiments/`; private ones stay in the private repository):

```toml
schema = "yapnr-campaign-v1"
kind = "ladder-cell"
visibility = "public"
source = "HEAD"              # a clean commit; --allow-dirty adds the diff and marks every record
image = "edge"               # resolved to an index digest at plan time and pinned

[matrix]
case = ["05-timer-led-10", "06-chaser-14", "07-chaser-20", "08-chaser-20-plane"]
seed = [0, 1, 2, 3, 4, 5, 6, 7]

[config]                     # passed to the kind's generator
initial_pool = true
initial_starts = 8
initial_finalists = 3
timeout = 1800

[resources]
cpus = 1
memory_gb = 3
max_wall_s = 7200

[placement]
families = ["c4d", "c3d"]    # ranked; the planner prices each in each enabled region
spot = true
```

### 11.4 Owner configuration (outside the repository)

`~/.config/yapnr/cloud.toml`; an annotated example will be committed as
`docs/examples/cloud.toml.example`, with placeholder values only. `YAPNR_CLOUD_CONFIG` points at
another file.

```toml
[gcp]
project = "example-project"
home_region = "us-west4"
regions = ["us-west4", "northamerica-northeast1"]
submit_service_account = "yapnr-submit"   # account ids; the address is derived from the project
runner_service_account = "yapnr-runner"
inputs_bucket = "example-yapnr-inputs"
runs_bucket = "example-yapnr-runs"
registry = "{region}-docker.pkg.dev/example-project/ghcr"
subnetwork = "projects/example-project/regions/{region}/subnetworks/yapnr-{region}"
template = "yapnr-{shape}-{model}-{region}"
price_api_key_command = ["security", "find-generic-password", "-s", "yapnr-billing-catalog", "-w"]
ranking = [["c4d", "us-west4"], ["c3d", "northamerica-northeast1"]]   # from calibration

[limits]
max_tasks = 2000
max_parallel_vcpus = 64
max_task_wall_s = 14400
max_campaign_hours = 12
max_retries = 3
confirm_usd = 5
refuse_usd = 25
hard_refuse_usd = 100        # --max-usd cannot go above this

[local]
store = "~/yapnr-runs"
private_results_root = "~/yapnr-private-runs"
workers = 4
nice = 10

[slurm.example-site]
account = "example-account"
partition = "cpu"
store = "$SCRATCH/yapnr-store"
sif = "$PROJECT_DIR/yapnr/yapnr-{digest12}.sif"
max_array = 1000
max_concurrent = 64
chunk = 8
module = "apptainer"
private_ok = false
```

The example file uses documentation values; real values are never committed.

### 11.5 Command line

`yapnr exp` is registered in `yapnr/cli.py` like the part cache (`register(commands)`):

| Command                                                                                 | What it does                                                                                                                                                                                                                         | Touches the cloud                                 |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------- |
| `yapnr exp plan CAMPAIGN.toml --backend B [--offline]`                                  | expands the matrix into `tasks.jsonl`, groups tasks into resource classes, picks region and shape, renders backend artefacts (Batch JSON, `sbatch` script, local plan), validates them, prints the estimate; writes a plan directory | price lookup only (cached; none with `--offline`) |
| `yapnr exp submit PLAN [--yes] [--max-usd X] [--dry-run] [--only TASK...] [--fallback]` | re-validates, checks caps and the freeze marker, uploads bundles (skipping existing ones) and the manifest, submits one job per resource class, writes `submissions/<n>.json`; pending tasks only (no `_DONE`)                       | yes (not with `--dry-run`)                        |
| `yapnr exp status [CID] [--watch]`                                                      | the status table (§15); with `--watch`, also performs region fallback when enabled                                                                                                                                                   | read-only                                         |
| `yapnr exp logs CID [TASK] [--follow]`                                                  | Batch task logs from Cloud Logging, or `log.tail` and the full logs in the result; Slurm output files; local files                                                                                                                   | read-only                                         |
| `yapnr exp fetch CID [--full] [--into DIR]`                                             | downloads summaries (or full results) and assembles the local layout (§11.8)                                                                                                                                                         | read-only (egress)                                |
| `yapnr exp cancel CID [--submission N]`                                                 | cancels the campaign's jobs (Batch cancel, `scancel`, or the local pool's own processes)                                                                                                                                             | yes                                               |
| `yapnr exp doctor --backend B`                                                          | read-only checks: impersonation, buckets, registry resolves the digest, templates, quota values, budget, freeze marker                                                                                                               | read-only                                         |
| `yapnr exp prices [--rerank]`                                                           | refreshes the price cache and shows the ranked (family, region) list                                                                                                                                                                 | Billing Catalog only                              |
| `yapnr exp unfreeze`                                                                    | clears `control/frozen` after the owner confirms                                                                                                                                                                                     | yes                                               |
| `python -m yapnr.exp.task`                                                              | the in-task wrapper (§11.6); not run by hand                                                                                                                                                                                         | –                                                 |

Cloud calls go through one `Cloud` interface. The default implementation runs `gcloud` (installed
in user space) with `--format=json` and a timeout on every call, using the impersonation set in
its configuration; a `FakeCloud` records calls for tests and `--dry-run`. This keeps Google's
Python client libraries (gRPC and friends) out of `requirements.lock` and the image. Price lookups
use the Billing Catalog REST API with the restricted key through the standard library.

Package layout:

```text
yapnr/exp/
  cli.py          register(commands): the commands above
  spec.py         yapnr-task-v1 and yapnr-campaign-v1: validation, canonical JSON, hashes
  config.py       the owner configuration (tomllib), with its own validation
  kinds/          one module per kind: expand(), assemble(), wall-time model, summary globs
  plan.py         campaign -> tasks -> resource classes -> backend artefacts + estimate
  cost.py         price cache, estimates, caps, the freeze check
  store.py        LocalStore and GcsStore (gcloud storage), one interface
  cloud.py        Gcloud (subprocess, JSON, timeouts) and FakeCloud
  backends/       local.py, gcp_batch.py, slurm.py: render, submit, status, cancel, logs
  task.py         the in-task wrapper (standard library only)
  schemas/        JSON schemas of the two specs
third_party/googleapis/batch-v1.json    the Batch v1 discovery document, for offline validation
```

### 11.6 The task wrapper

`python -m yapnr.exp.task --store DIR --campaign CID --submission N [--index I]`, the same on all
backends:

1. Resolve the index from `BATCH_TASK_INDEX`, else `SLURM_ARRAY_TASK_ID` x chunk + position, else
   `--index`; map it through `submissions/<n>.indices` to a line of `tasks.jsonl`, and check the
   line's spec hash against `campaign.json`. Refuse unknown schema versions.
2. If `control/frozen` exists, exit 3 (no retry). If the task's `_DONE` exists, exit 0.
3. Make a private work directory on local disk; point `HOME`, `XDG_*` and `TMPDIR` into it.
4. Stage inputs: copy each bundle from the inputs store, verify its sha256, extract. With
   `restart: resume`, restore the newest checkpoint.
5. Run the command under `nice` with the task's wall-time limit, in its own process group, with a
   scrubbed environment (the ladder runner already drops ambient `PNR_*` switches). Stdout and
   stderr go to files; one JSON status line per stage goes to stdout. On GCP the wrapper polls the
   metadata server's `preempted` flag and handles `SIGTERM`; on Slurm it handles `USR1`; either
   triggers a checkpoint flush for resumable kinds.
6. Collect: evaluate `done` and `verdict`; apply prune globs; write `result.tar.gz`, the summary
   files, `record.json` and `log.tail` under the attempt's prefix; write `_DONE` last.
7. Exit 0 whenever a result was recorded, PASS or FAIL, and also when the engine crashed (verdict
   `error`, with its logs): a crash repeats on retry, so it is data, not an infrastructure fault.
   Exit 75 (`EX_TEMPFAIL`) for staging or upload failures, which are retried (§13).

### 11.7 Backend artefacts

A Batch job for one resource class (illustrative; values in `${}` come from the owner config, and
the renderer's output is validated against the vendored discovery document):

<!-- prettier-ignore -->
```json
{
  "taskGroups": [
    {
      "taskCount": 96,
      "parallelism": 64,
      "taskSpec": {
        "runnables": [
          {
            "container": {
              "imageUri": "${REGION}-docker.pkg.dev/${PROJECT}/ghcr/studio-fug/yapnr@sha256:<index digest>",
              "entrypoint": "/usr/local/bin/yapnr-kicad-env",
              "commands": ["/opt/venv/bin/python", "-m", "yapnr.exp.task",
                           "--store", "/mnt/disks/runs", "--inputs", "/mnt/disks/inputs",
                           "--campaign", "20261002-ladder-3f9a1c", "--submission", "1"],
              "options": "--init --shm-size 1g"
            },
            "timeout": "7500s"
          }
        ],
        "computeResource": { "cpuMilli": 2000, "memoryMib": 3072 },
        "maxRunDuration": "7800s",
        "maxRetryCount": 3,
        "lifecyclePolicies": [
          {
            "action": "RETRY_TASK",
            "actionCondition": { "exitCodes": [50001, 50002, 50003, 50006, 75] }
          }
        ],
        "volumes": [
          { "gcs": { "remotePath": "${RUNS_BUCKET}" }, "mountPath": "/mnt/disks/runs" },
          {
            "gcs": { "remotePath": "${INPUTS_BUCKET}/bundles" },
            "mountPath": "/mnt/disks/inputs",
            "mountOptions": ["-o ro"]
          }
        ]
      }
    }
  ],
  "allocationPolicy": {
    "location": { "allowedLocations": ["regions/${REGION}"] },
    "instances": [{ "instanceTemplate": "yapnr-c4d-highcpu-16-spot-${REGION}" }],
    "serviceAccount": { "email": "yapnr-runner@${PROJECT}.iam.gserviceaccount.com" },
    "labels": {"yapnr": "1", "campaign": "20261002-ladder-3f9a1c", "kind": "ladder-cell",
               "visibility": "public", "submission": "1", "deadline": "1759500000"}
  },
  "labels": { "yapnr": "1", "campaign": "20261002-ladder-3f9a1c" },
  "logsPolicy": { "destination": "CLOUD_LOGGING" }
}
```

The wrapper's own limit is the task's `max_wall_s` (7200 s here), the runnable timeout adds 5
minutes for upload, and `maxRunDuration` another 5, so each limit is distinguishable in logs. The
gcsfuse mounts need options that let the image's UID 1000 write (`uid`, `gid` or `allow_other`);
the smoke job settles the exact flags.

A Slurm array for the same class (illustrative):

```bash
#!/bin/bash
#SBATCH --job-name=yapnr-20261002-ladder-3f9a1c-1
#SBATCH --account=${ACCOUNT} --partition=${PARTITION}
#SBATCH --array=0-11%64
#SBATCH --cpus-per-task=1 --mem=3G --time=16:30:00
#SBATCH --requeue --signal=B:USR1@300
#SBATCH --output=${STORE}/campaigns/20261002-ladder-3f9a1c/slurm/%A_%a.out
set -euo pipefail
module load apptainer 2>/dev/null || true
apptainer exec --cleanenv --containall --bind "${STORE}:/store" --bind "${TMPDIR}:/scratch" \
  "${SIF}" /usr/local/bin/yapnr-kicad-env /opt/venv/bin/python -m yapnr.exp.task \
  --store /store --campaign 20261002-ladder-3f9a1c --submission 1 --chunk 8 &
child=$!
trap 'kill -USR1 "${child}"; wait "${child}" || true;
      scontrol requeue "${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"' USR1
wait "${child}"
```

The local backend runs the wrapper with the local toolchain profile in a pool of `workers`
processes under `nice`, records each process's PID and start time, and cancels only those (never
processes it did not start, per [AGENTS.md](../../AGENTS.md)).

### 11.8 Fetch and assembly

`fetch` copies each task's summary (or full result) of its `_DONE` attempt into
`<dest>/<cid>/tasks/<task>/`, then calls the kind's `assemble()` to rebuild the layout the local
tools expect. For the ladder, the assembled directory is what one `run.py --out` run of all cells
would have written:

- `CASE-seed-S/` directories copied from each task;
- `source-freeze/`, `python-version.txt` and `kicad-version.txt` from any task, after checking that
  every task reports the same `sources_sha256`, engine revision, platform and versions (assembly
  refuses mixed platforms unless `--allow-mixed`);
- `summary.json` with the results in campaign order, `complete` only when every task has `_DONE`,
  `passed` only when every cell passed and no source changed;
- `junit.xml` written by the runner's own writer (a small refactor of `run.py` into an importable
  function);
- `provenance.json` from the shared fields plus a `shards` list (task, attempt, machine type, CPU
  model, image digest, wall time).

`tools/ci/ladder_summary.py`, the animation renderer and any other reader of a ladder run directory
then work unchanged. Other kinds: benchmark cells land in the harness's per-tool layout; MC
evaluation records are imported into the halving run's `dataset.jsonl` (§12); RF runs land in
`<dest>/<case>/` as `yapnr.rf.cases run --out` writes them. The local backend uses the same store
layout and the same assembly, so a campaign assembles identically wherever it ran.

## 12. Sharding the workloads

- **Ladder.** One task per (case, seed, configuration). The initial pool (8 starts, 3 finalists)
  stays inside the cell: splitting it would need an engine seam for little gain. Hard rungs run on
  Spot with scratch restarts; at an assumed preemption rate of a few percent per hour, the rework
  is a few percent of the campaign (the 8% allowance in §6.1 covers it).
- **Tool comparison.** One task per (rung, tool, seed) in the tool's image, judged in the same
  task. Freerouting tasks request 4 cores.
- **MC successive halving.** The driver stays the coordinator, on the Mac: it never needs cloud
  credentials inside the cloud, and it is light. A small seam in the halving driver adds a "plan"
  mode that writes a stage's pending evaluations and an "import" mode that ingests their records,
  both reusing the existing per-record resume logic. `yapnr exp` then runs stage 0 as one
  `mc-place` task, each rung's evaluations as parallel `mc-eval` tasks (rung 1's ten evaluations
  take about the slowest one, est. 37 min instead of 5 h), and imports before the next stage.
  **Deep evaluations** (16 h observed, no resume) run on standard (non-Spot) capacity, from a
  small separate on-demand CPU quota, or stay on the Mac, until a resume-from-phase exists.
- **Splanc A/B.** One task per (arm, placement); all arms of one comparison in one job, on one
  machine type, interleaved so that arms see the same conditions; records carry load averages as
  the evaluation already does.
- **RF.** One task per case, `restart: resume` with checkpoints synced every 5 minutes and on
  preemption notice; ideal for Spot.
- **Block synthesis and FEA.** Later: block synthesis as one task per trial; FEA once it has an
  image.

## 13. Preemption, retries and idempotency

- **What is retried.** One lifecycle policy (Batch supports one): `RETRY_TASK` on 50001 (Spot
  preemption), 50002 (VM lost), 50003 (VM rebooted), 50006 (VM recreated) and 75 (the wrapper's
  transient staging or upload failure), `maxRetryCount` 3 [5, 6]. Everything else fails at once:
  50005 means the task outgrew its limit (retrying repeats it), and engine failures are results.
- **Scratch restarts.** A retried task starts from scratch in a fresh attempt directory. Earlier
  attempts' records stay, so preemption rework is measured, not guessed.
- **Resume restarts.** RF tasks restore the newest checkpoint. Spot gives no notice by default and
  at most a best-effort 30 s shutdown [18], so checkpoints are periodic, not only on signals; the
  templates request the optional 120 s preemption notice where Compute Engine offers it [18].
- **Idempotency.** `_DONE` is written last and names a complete attempt; a task with `_DONE` is
  skipped by every backend; a late duplicate writes its own attempt directory and at worst
  repoints `_DONE` at an equally complete attempt.
- **Resubmission.** When a job ends with tasks still pending (retries exhausted, a 2-day queue
  timeout, a freeze), `yapnr exp submit --resume CID` submits only the pending tasks as a new
  submission, in the same or the next ranked region.
- **Slurm** uses `--requeue` and the `USR1` handler (§11.7); a site's requeue limit bounds it.
- **Local** runs never retry automatically; a rerun skips finished tasks.

## 14. Provenance

Each attempt's `record.json`:

- campaign id and campaign spec sha256; task id and task spec sha256; submission and attempt;
- source commit, dirty flag and source bundle sha256 (the ladder's `provenance.json` takes the
  engine revision from git, which a source archive lacks: a small runner change reads
  `YAPNR_ENGINE_REVISION` when git is unavailable, and the wrapper sets it);
- image index digest, the platform manifest digest pulled, the image's `YAPNR_SOURCE_REVISION`, and
  on Slurm the SIF's sha256;
- backend, region and zone, machine type, provisioning model, CPU model, vCPU count, threads per
  core (from the metadata server on GCP, `lscpu` elsewhere), platform and kernel;
- start and end times, wall time, CPU seconds, peak RSS (`getrusage` of the children), load
  averages, exit codes, verdict, and the Spot price in the estimate.

The assembled outputs carry these as the `shards` list (§11.8), so reports can stratify by
machine type, and `campaign.json` ties every result back to its plan.

## 15. Observability

- **`yapnr exp status`** joins the store (done markers, verdicts, records) with the backend's view
  (Batch job and task states, including status events with preemption exit codes; `squeue` and
  `sacct`; the local pool):

  ```text
  campaign 20261002-ladder-3f9a1c  gcp-batch  us-west4  c4d-highcpu-16 spot  submission 1
  tasks 96   done 71 (pass 64, fail 7, error 0)   running 16   queued 9   preemptions 3
  elapsed 0:42   est. remaining 0:20   est. cost $0.84 (ceiling $6.10)   frozen no
  ```

- **Logs.** Tasks print one JSON line per stage to stdout (Cloud Logging is free up to 50 GiB per
  project per month, then $0.50/GiB [41]); full logs travel in `result.tar.gz`, with `log.tail` for
  failures. `yapnr exp logs` reads whichever is available.
- **Cost per campaign.** The labels (§10) make Cloud Billing reports group by campaign with about a
  day's delay; an optional billing export to BigQuery lets `status --billed` show actual cost. The
  estimate from task records is shown immediately.
- **Notifications.** None at first (status is polled). Batch job notifications to Pub/Sub can be
  added later, bearing in mind they can skip states [16].

## 16. Infrastructure as code: `infra/gcp`

OpenTofu (MPL-2.0) rather than Terraform (BUSL), with HCL kept compatible with both, and the
`google` provider pinned. Plain gcloud scripts were rejected: they have no plan or diff, no drift
detection and no offline tests. Layout:

```text
infra/gcp/
  README.md                 what each module creates, idle cost, apply and destroy
  versions.tf               OpenTofu version floor (mock providers in tests), pinned providers
  backend.tf                backend "gcs" {} with a partial configuration (bucket from the owner)
  main.tf, variables.tf, outputs.tf
  examples/owner.tfvars.example
  modules/
    services/               the project's APIs (Compute, Batch, Logging, Storage, Artifact
                            Registry, IAM, IAM Credentials, Cloud Quotas, Billing Budgets, Cloud
                            Billing, Pub/Sub, Secret Manager, Cloud Run, Cloud Functions, Cloud
                            Build, Eventarc, Cloud Scheduler)
    network/                VPC "yapnr", a subnet per enabled region with Private Google Access,
                            no ingress rules
    storage/                inputs and runs buckets: uniform access, public access prevention,
                            soft delete, lifecycle rules, bucket-level IAM
    identity/               yapnr-submit, yapnr-runner, yapnr-guard and their bindings; the
                            TokenCreator grant for the owner's chosen Mac identity
    registry/               an Artifact Registry remote repository per region (upstream
                            https://ghcr.io, optional upstream credentials), reader grants
    templates/              instance templates per (shape, region, provisioning model) for
                            Hyperdisk families: pinned hyperdisk-balanced boot disk, no external
                            IP, runner account, Spot with DELETE, optional threads per core
    budget/                 the budget and its Pub/Sub topic
    guard/                  budget-guard and reaper functions, their triggers, the scheduler job
  functions/guard/          the functions' Python source (standard library HTTP with
                            metadata-server tokens), unit-tested
  tests/                    tofu test files with mock_provider "google"
```

- **Quotas are not managed here.** The provider can manage quota preferences, but the kill switch
  lowers quota and a later `tofu apply` would silently restore it. Quota requests are runbook
  commands (§17), and `infra/gcp/README.md` lists them.
- **State** lives in a versioned bucket created during bootstrap; its name comes from the owner's
  `-backend-config`, never from the repository.
- **Known provider quirk.** The Billing Budgets API needs a quota project; the budget module sets
  `user_project_override` and `billing_project`.
- **Variables** (all supplied from the owner's `tfvars`, outside the repository): project id,
  home region, enabled regions, bucket names, Mac identity, budget amount and billing account,
  shapes per family, guard thresholds, whether GHCR needs upstream credentials.

## 17. Owner bootstrap runbook

Run once billing exists. `$PROJECT`, `$BILLING`, `$REGION`, `$OWNER`, `$MAC_ID` and `$STATE` are
the owner's values, kept in the owner's notes and config, never in this repository.

1. **Prerequisites.** Make both GHCR packages public (or create a token with `read:packages` for
   the remote repository). Install the gcloud CLI and OpenTofu in user space.
2. **Project and billing** (owner account):

   ```sh
   gcloud auth login
   gcloud projects create "$PROJECT" --name="yapnr experiments"
   gcloud billing projects link "$PROJECT" --billing-account="$BILLING"
   gcloud config configurations create yapnr-owner && gcloud config set project "$PROJECT"
   gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com \
     iam.googleapis.com cloudbilling.googleapis.com billingbudgets.googleapis.com \
     storage.googleapis.com
   ```

3. **State bucket and infrastructure:**

   ```sh
   gcloud storage buckets create "gs://$STATE" --location="$REGION" \
     --uniform-bucket-level-access --public-access-prevention
   gcloud storage buckets update "gs://$STATE" --versioning
   gcloud auth application-default login
   cd infra/gcp
   tofu init -backend-config="bucket=$STATE"
   tofu plan -var-file="$HOME/.config/yapnr/gcp.tfvars" -out=bootstrap.plan
   tofu apply bootstrap.plan
   ```

4. **Quotas.** Look up the exact quota ids first (they are unverified here), then request
   preemptible CPUs and the C4D family quota in each enabled region (phase 1: 64 vCPUs; on-demand
   CPUs stay at the default) [25, 26, 54]:

   ```sh
   gcloud quotas info list --service=compute.googleapis.com --project="$PROJECT" \
     --filter="quotaId~PREEMPTIBLE OR quotaId~FAMILY"
   gcloud quotas preferences create --service=compute.googleapis.com --project="$PROJECT" \
     --quota-id=<id from the listing> --dimensions=region="$REGION" --preferred-value=64 \
     --email="$OWNER" --justification="Batch Spot jobs for open-source PCB routing research"
   ```

   Free-trial billing accounts cannot request increases, and a new billing account may be granted
   less than asked at first.

5. **Price key** (optional), restricted to the Cloud Billing API and kept in the Keychain:

   ```sh
   gcloud services api-keys create --display-name=yapnr-prices \
     --api-target=service=cloudbilling.googleapis.com
   security add-generic-password -s yapnr-billing-catalog -a "$USER" -w
   ```

6. **Day-to-day identity.** `tofu apply` has granted TokenCreator on `yapnr-submit` to `$MAC_ID`.
   Sign that account in and set impersonation:

   ```sh
   gcloud config configurations create yapnr && gcloud auth login "$MAC_ID"
   gcloud config set project "$PROJECT"
   gcloud config set auth/impersonate_service_account "yapnr-submit@${PROJECT}.iam.gserviceaccount.com"
   ```

7. **Sign the owner out** on the Mac: `gcloud auth revoke "$OWNER"` and
   `gcloud auth application-default revoke`.
8. **Owner config**: copy `docs/examples/cloud.toml.example` to `~/.config/yapnr/cloud.toml` and
   fill it from `tofu output`.
9. **Checks**: `yapnr exp doctor --backend gcp-batch` (read-only).
10. **Smoke job** (cap $0.50): `yapnr exp plan experiments/smoke.toml --backend gcp-batch` runs
    `yapnr doctor` and one small ladder cell on C4D with and without the template; then `submit`,
    `status --watch`, `fetch`.
11. **Kill-switch drill**: submit a sleeping smoke task, publish a synthetic over-budget message
    to the budget topic, and confirm the job is cancelled, `control/frozen` appears and `submit`
    refuses; then `yapnr exp unfreeze`. The quota cut is drilled once with a 1-vCPU quota to learn
    whether restoring it needs re-approval.
12. **Calibration** (§6.6), then record the ranking in the owner config.
13. **Teardown**, if ever wanted: cancel all jobs, then `tofu destroy`. The buckets refuse to be
    destroyed while they hold objects, so results and private inputs are deleted only on purpose.

## 18. Tests

All tests run without credentials and without spending money; they are wired through
`yapnr_py_tests()` like the rest of `tests/`.

- **Unit (`tests/unit/exp/`):** spec validation and canonical hashing; campaign expansion (stable
  ids, matrix sizes); resource classes and packing per family; the estimator and the
  confirm/refuse logic, including fail-closed pricing; freeze handling; label sanitising and
  opaque ids for private campaigns; the owner-config loader; refusal to fetch private campaigns
  into a public checkout.
- **Renderers:** golden Batch JSON validated against the vendored discovery document
  (`third_party/googleapis/batch-v1.json`, revision 20260723, BSD-3-Clause, recorded in
  `THIRD_PARTY.md`); golden `sbatch` scripts checked with `bash -n` and shellcheck.
- **Wrapper:** end-to-end runs of `smoke` tasks against a `LocalStore`: skip on `_DONE`, exit-code
  contract, timeouts, a simulated preemption (the wrapper killed mid-run, then retried), resume
  from a checkpoint, prune and summary globs.
- **Assembly:** synthetic ladder shards assembled and compared with a single-run layout;
  `tools/ci/ladder_summary.py --check` reads the result.
- **Dry runs:** `submit --dry-run` and the whole plan-submit-status-fetch cycle against
  `FakeCloud`, asserting the exact `gcloud` calls.
- **Local backend:** a small real campaign (two tiny ladder cells, tagged `kicad`, `manual`).
- **Infrastructure:** `tofu fmt -check`, `tofu validate`, and `tofu test` with
  `mock_provider "google"` (OpenTofu's test framework supports provider mocks [66]); a manual
  target runs `tofu plan` against a fake project id with a dummy access token and
  `-refresh=false`, which creates no resources and calls no API that spends. The guard functions
  have unit tests with fake Batch, Quotas and Storage endpoints, including repeated messages.
- **Slurm (phase 4):** a GitHub Actions job installs `slurm-wlm` and Apptainer on Ubuntu, starts a
  single-node cluster, and runs a rendered array of smoke tasks, including a requeue.

## 19. Phased rollout

1. **Bootstrap and calibration** (budget $50/month, 64 preemptible vCPUs, `refuse_usd` $25).
   Build `yapnr/exp` (spec, local and Batch backends, wrapper, estimator, ladder assembly),
   `infra/gcp`, the runner's `YAPNR_ENGINE_REVISION` fallback, and the docs page for owners. Run
   the runbook, the smoke job, the kill-switch drill and the calibration. Exit: a ranked
   (family, region) list; the C4D boot-disk question settled; a campaign of 100 ladder cells
   assembled and read by `ladder_summary.py`; measured cost per result within 2x of the estimate.
2. **Ladder and benchmarks** (budget $150/month, 256 vCPUs). Seed sweeps of the ladder, the
   benchmark tool images and their assembly, region fallback, the optional billing export. Exit:
   a nightly-sized sweep (8 cases x 16 seeds) finishes in under an hour of wall time.
3. **Private Splanc experiments.** The halving plan/import seam; relocatable bundles built in the
   private repository; the Linux Rust search library and `libngspice` in the image; deep
   evaluations on standard capacity; a cloud baseline before any A/B comparison. Exit: one H8-sized
   halving run end to end, with rung 1 in parallel, and no private string in labels, logs or this
   repository.
4. **Slurm HPC allocation** (when one is granted). The `slurm` renderer and chunking, the SIF
   workflow, the CI single-node test, and the site's limits in the owner config. Exit: the
   calibration set run on the allocation and assembled like the cloud runs.

## 20. Risks

| Risk                                                    | Effect                                    | Mitigation                                                                                                         |
| ------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| GHCR packages stay private                              | VMs and Apptainer cannot pull anonymously | upstream credentials from Secret Manager; `APPTAINER_DOCKER_*` on HPC                                              |
| C4D boot disk under Batch                               | C4D jobs fail to start                    | instance templates with Hyperdisk (§6.4); smoke job tests both paths; fallback to C3D with Hyperdisk or T2D        |
| Quota requests denied or small on a new billing account | little parallelism at first               | start at 64 vCPUs; quota is also the spend cap                                                                     |
| No Spot capacity                                        | jobs wait, then fail after 2 days         | capacity advice before submitting; region fallback with `--fallback`; queue timeout                                |
| Preemption of long tasks                                | lost work up to a task's runtime          | per-core packing, retries only on infrastructure codes, checkpoints for RF, standard capacity for deep evaluations |
| Wall-clock budgets make results machine-dependent       | cloud and Mac results not comparable      | one machine type per campaign, A/B arms in one job, cloud baselines, machine fields in every record                |
| Arm and x86 give different boards                       | C4A results differ from C4D's             | compare within a platform; the calibration's quality-equivalence test decides whether Arm joins the ranking        |
| gcsfuse permissions or caching surprises                | tasks cannot write results                | settle mount flags in the smoke job; fallback to `gcloud storage cp` from a script runnable after the container    |
| Budget lag                                              | overspend before alerts                   | quota ceiling, per-submit caps, deadlines and the reaper act without budget data                                   |
| Kill-switch quota cut needs re-approval to undo         | slow recovery after a false alarm         | the quota cut sits at 120%, above the job-cancel threshold; drill it at 1 vCPU first                               |
| Private data leaks through metadata                     | board names in labels, job names or logs  | opaque ids, label allowlist, status-only stdout, privacy tests, fetch-destination check                            |
| Egress costs exceed compute                             | surprising bills on large fetches         | summaries by default, `--full` per task, lifecycle deletion                                                        |
| Prices drift                                            | stale ranking, wrong estimates            | daily price cache, weekly re-ranking, estimates shown at every submit                                              |

## 21. Open questions for the owner

1. **GHCR visibility**: switch both packages to public now (irreversible), or keep them private
   and store a `read:packages` token for the registry cache? This design recommends public.
2. **Home region**: `us-west4` provisionally; is there a reason (latency is irrelevant here; data
   residency might not be) to prefer another?
3. **Budgets and caps**: $50/month and 64 vCPUs in phase 1, $150 and 256 in phase 2, confirmation
   above $5 and refusal above $25 per submit. Adjust?
4. **A separate Google account for the Mac** (recommended) or the owner's own account with
   impersonation?
5. **Deep evaluations**: standard VMs in the cloud (est. a few dollars each, never preempted) or
   keep them on the Mac until resume-from-phase exists?
6. **Restoring quota** after a kill-switch cut may need a new approval; acceptable, or should the
   guard stop at cancelling jobs and freezing submissions?
7. **Billing export to BigQuery** for actual cost per campaign: wanted from phase 2?

## 22. References

All accessed 2026-10-02.

### Batch

- [1] Batch pricing: <https://cloud.google.com/batch/pricing>
- [2] Batch quotas and limits: <https://docs.cloud.google.com/batch/quotas>
- [3] Create and run a job (tasks per VM): <https://docs.cloud.google.com/batch/docs/create-run-job>
- [4] Batch REST reference, `projects.locations.jobs`:
  <https://docs.cloud.google.com/batch/docs/reference/rest/v1/projects.locations.jobs>
- [5] Automate task retries: <https://docs.cloud.google.com/batch/docs/automate-task-retries>
- [6] Troubleshooting, reserved exit codes: <https://docs.cloud.google.com/batch/docs/troubleshooting>
- [7] Get started (supported machine series, IAM roles):
  <https://docs.cloud.google.com/batch/docs/get-started>
- [8] Release notes (location enforcement from 2026-07-31; instance flexibility Preview,
  2026-07-17): <https://docs.cloud.google.com/batch/docs/release-notes>
- [9] Instance flexibility: <https://docs.cloud.google.com/batch/docs/create-run-job-instance-flexibility>
- [10] Custom boot disks: <https://docs.cloud.google.com/batch/docs/create-run-job-custom-boot-disks>
- [11] Instance templates: <https://docs.cloud.google.com/batch/docs/create-run-job-vm-template>
- [12] Storage volumes: <https://docs.cloud.google.com/batch/docs/create-run-job-storage>
- [13] Image streaming: <https://docs.cloud.google.com/batch/docs/use-image-streaming>
- [14] Jobs without external access: <https://docs.cloud.google.com/batch/docs/job-without-external-access>
- [15] Labels: <https://docs.cloud.google.com/batch/docs/organize-resources-using-labels>
- [16] Known issues: <https://docs.cloud.google.com/batch/docs/known-issues>
- [17] Batch v1 discovery document (revision 20260723), mirrored by Google's Go client:
  <https://github.com/googleapis/google-api-go-client/blob/main/batch/v1/batch-api.json>

### Compute Engine and prices

- [18] Spot VMs: <https://docs.cloud.google.com/compute/docs/instances/spot>
- [19] Spot VM pricing: <https://cloud.google.com/spot-vms/pricing>
- [20] General-purpose machine families: <https://docs.cloud.google.com/compute/docs/general-purpose-machines>
- [21] Compute-optimized machine families:
  <https://docs.cloud.google.com/compute/docs/compute-optimized-machines>
- [22] Threads per core: <https://docs.cloud.google.com/compute/docs/instances/set-threads-per-core>
- [23] Hyperdisk Balanced: <https://docs.cloud.google.com/compute/docs/disks/hd-types/hyperdisk-balanced>
- [24] Disk pricing: <https://cloud.google.com/compute/disks-image-pricing>
- [25] Compute Engine quotas: <https://docs.cloud.google.com/compute/resource-usage>
- [26] Cloud Quotas CLI: <https://docs.cloud.google.com/docs/quotas/gcloud-cli-examples>
- [27] Spot preemption rates and price history:
  <https://docs.cloud.google.com/compute/docs/instances/view-spot-preemption-price>
- [28] VM availability advice: <https://docs.cloud.google.com/compute/docs/instances/view-vm-availability>
- [29] Compute-optimized pricing (`c2-standard-4`):
  <https://cloud.google.com/products/compute/pricing/compute-optimized>
- [30] Cloud Billing Catalog snapshot (2026-04-30, 2026-07-16, 2026-09-24):
  <https://github.com/Cyclenerd/google-cloud-pricing-cost-calculator>

### Storage, network, registry, logging

- [31] Cloud Storage FUSE: <https://docs.cloud.google.com/storage/docs/cloud-storage-fuse/overview>
- [32] Public access prevention: <https://docs.cloud.google.com/storage/docs/public-access-prevention>
- [33] Object lifecycle: <https://docs.cloud.google.com/storage/docs/lifecycle>
- [34] Cloud Storage pricing: <https://cloud.google.com/storage/pricing>
- [35] Network pricing: <https://cloud.google.com/vpc/network-pricing>
- [36] Cloud NAT pricing: <https://cloud.google.com/nat/pricing>
- [37] Remote repositories: <https://docs.cloud.google.com/artifact-registry/docs/repositories/remote-overview>
- [38] Creating remote repositories:
  <https://docs.cloud.google.com/artifact-registry/docs/repositories/remote-repo>
- [39] Artifact Registry pricing: <https://cloud.google.com/artifact-registry/pricing>
- [40] Artifact Registry access control: <https://docs.cloud.google.com/artifact-registry/docs/access-control>
- [41] Cloud Logging pricing: <https://cloud.google.com/stackdriver/pricing>
- [42] VPC firewall rules (the default network): <https://docs.cloud.google.com/vpc/docs/firewalls>
- [43] Default encryption at rest:
  <https://docs.cloud.google.com/docs/security/encryption/default-encryption>

### Identity and billing

- [44] API keys: <https://docs.cloud.google.com/docs/authentication/api-keys>
- [45] Cloud Billing Catalog API:
  <https://docs.cloud.google.com/billing/docs/how-to/get-pricing-information-api>
- [46] Service-account impersonation:
  <https://docs.cloud.google.com/docs/authentication/use-service-account-impersonation>
- [47] Service-account key practices:
  <https://docs.cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys>
- [48] Secure-by-default organisations:
  <https://docs.cloud.google.com/resource-manager/docs/secure-by-default-organizations>
- [49] Workload Identity Federation for pipelines:
  <https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines>
- [50] Budgets: <https://docs.cloud.google.com/billing/docs/how-to/budgets>
- [51] Budget notifications: <https://docs.cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications>
- [52] Disabling billing with notifications:
  <https://docs.cloud.google.com/billing/docs/how-to/disable-billing-with-notifications>
- [53] `gcloud billing budgets create`:
  <https://docs.cloud.google.com/sdk/gcloud/reference/billing/budgets/create>
- [54] `gcloud quotas preferences create`:
  <https://docs.cloud.google.com/sdk/gcloud/reference/quotas/preferences/create>
- [55] `gcloud batch jobs cancel`: <https://docs.cloud.google.com/sdk/gcloud/reference/batch/jobs/cancel>

### Other fabrics and tools

- [56] Cluster Toolkit: <https://github.com/GoogleCloudPlatform/cluster-toolkit>
- [57] Slurm `JobRequeue`: <https://slurm.schedmd.com/slurm.conf.html#OPT_JobRequeue>
- [58] Slurm job arrays: <https://slurm.schedmd.com/job_array.html>
- [59] SkyPilot managed jobs: <https://docs.skypilot.ai/en/latest/examples/managed-jobs.html>
- [60] SkyPilot on Slurm: <https://docs.skypilot.ai/en/latest/reference/slurm/index.html>
- [61] GKE pricing: <https://cloud.google.com/kubernetes-engine/pricing>
- [62] GKE Spot VMs: <https://docs.cloud.google.com/kubernetes-engine/docs/concepts/spot-vms>
- [63] Kueue: <https://kueue.sigs.k8s.io/docs/overview/>
- [64] dsub: <https://github.com/DataBiosphere/dsub>
- [65] Apptainer with Docker and OCI images: <https://apptainer.org/docs/user/latest/docker_and_oci.html>
- [66] OpenTofu `tofu test` (mock providers): <https://opentofu.org/docs/cli/commands/test/>
- [67] GitHub package visibility:
  <https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility>
