# Cloud and HPC experiments (`yapnr exp`)

`yapnr exp` runs experiment campaigns (regression ladder sweeps, tool-comparison cells, Monte Carlo
evaluations, RF optimisation runs) on three backends from one task description:

- **`local`**: a niced process pool on your machine, with the host's KiCad;
- **`gcp-batch`**: Google Cloud Batch on Spot VMs (C4D by default), nothing running between
  campaigns;
- **`slurm`**: a job array on an HPC allocation, running the published image under Apptainer.

The reasoning behind every choice is in the [design](design/cloud-experiments.md). This page is the
owner's guide: what to provision, the exact bootstrap commands, and everyday use.

> **Status (2026-10-02).** The local backend runs end to end (a real ladder cell, planned, run,
> fetched and assembled; `tools/ci/ladder_summary.py --check` passes on the result). The
> `gcp-batch` and `slurm` backends render, validate and dry-run everything offline, and their
> output is pinned by golden files, but **nothing has run on Google Cloud or on a Slurm site
> yet**: there are no credentials. The first real run is the smoke job of
> [step 10](#10-smoke-job), at a cost of cents. What that run settles is listed under
> [Not verified yet](#not-verified-yet).

## What the owner provides

An API key cannot run Batch or Compute Engine: they authorize an IAM principal, and an API key is
not one ([API keys][api-keys]). Provide these instead:

1. **A Google account with Owner on a new project**, used for the bootstrap and `tofu apply` only.
2. **A billing account linked to the project**, and permission to create budgets on it (Billing
   Account Administrator or Billing Account Costs Manager). Free-trial billing accounts cannot get
   quota increases.
3. **Your decisions on the caps** (proposed: a $50 monthly budget, 64 preemptible vCPUs per region,
   confirmation above $5 and refusal above $25 per submit; see [Costs](#costs-and-guards)) and the
   regions (proposed: `us-west4`, then `northamerica-northeast1`).
4. **A day-to-day identity for this Mac**: ideally a separate, low-privilege Google account whose
   only grant is to impersonate `yapnr-submit`. Your own account works too.
5. **Public GHCR packages** (`studio-fug/yapnr` and `studio-fug/yapnr-kicad`), or a GitHub token
   with `read:packages` for the registry cache. On 2026-10-02 anonymous pulls of both still
   returned `401` (a public control image returned `200`), so they are still private.
6. **Optionally, an API key restricted to the Cloud Billing API**, for `yapnr exp prices
--refresh` ([pricing API][catalog]); it lives in the macOS Keychain.

Agents never log in for you. In Claude Code, run login commands yourself with the `!` prefix (for
example `! gcloud auth login`), which runs them in your shell with your browser; the agent only
sees the output. Nothing in this setup uses service-account key files.

## Quick start: the local backend

```sh
mkdir -p ~/.config/yapnr
cp docs/examples/cloud.toml.example ~/.config/yapnr/cloud.toml   # then edit [local]
yapnr exp plan experiments/ladder-small.toml --backend local
yapnr exp submit <plan> --wait          # or without --wait: a detached, niced pool
yapnr exp status <plan>
yapnr exp fetch <plan>                  # -> <store>/fetched/<campaign>/assembled/default
python3 tools/ci/ladder_summary.py <store>/fetched/<campaign>/assembled/default --check
```

Use `bazel run //:yapnr -- exp ...` from a checkout. The local toolchain is the headless KiCad copy
([DEVELOPERS.md](../DEVELOPERS.md#macos-use-a-headless-copy-no-dock-icons)) and a Python with the
engine's dependencies, set in `[local]` of the owner config or through `YAPNR_KICAD_CLI`,
`YAPNR_KICAD_PYTHON` and `YAPNR_KICAD_FOOTPRINTS`. `yapnr exp doctor --backend local` checks them.

## Campaigns, plans and tasks

A **campaign file** (TOML, `schema = "yapnr-campaign-v1"`) names a kind, a matrix and options;
public examples are in [`experiments/`](https://github.com/Studio-Fug/yapnr/tree/main/experiments):

| Kind          | One task                                                  | Example                                  |
| ------------- | --------------------------------------------------------- | ---------------------------------------- |
| `ladder-cell` | `run.py --case C --seed S` with the options in `[config]` | `ladder-small.toml`, `ladder-sweep.toml` |
| `bench-cell`  | one tool on one rung and seed, judged in the same task    | (tool images first)                      |
| `mc-place`    | stage 0 of a successive-halving run                       | from the halving driver's stage plan     |
| `mc-eval`     | one candidate evaluation                                  | from the halving driver's stage plan     |
| `rf-run`      | one RF topology-optimisation case, resumable              | `matrix.case`                            |
| `smoke`       | writes the interpreter, platform and `kicad-cli version`  | `smoke.toml`                             |

`yapnr exp plan` turns it into a **plan directory**: `campaign.json` (the campaign id, pinned image
digest, source commit and bundle, the tasks' hashes, resource classes, placements and the
estimate), `tasks.jsonl` (one `yapnr-task-v1` per line), `task.py` (the wrapper every backend
runs), `bundles/` (a `git archive` of the source commit and any data inputs, named by sha256) and
`backend/<name>/` (the Batch job JSON, `sbatch` scripts or pool items, for review). A dirty
checkout is refused unless `--allow-dirty`, which every record then reports. A campaign id looks
like `20261002-ladder-3f9a1c`; the same file, commit, image and backend give the same id.

`submit` sends the pending tasks (those without a `_DONE` marker in the store), one **submission**
per resource class: one Batch job, one Slurm array or one local pool. Running `submit` again after
preemptions, failures or a freeze sends only what is still pending.

Inside every task, the wrapper stages the bundles (checking their sha256), runs the command niced,
with a wall-time limit and a scrubbed environment (no credentials, no ambient `PNR_*` switches, a
private `HOME`), then writes `result.tar.gz`, the summary files, `record.json` (machine, CPU model,
image, wall and CPU time, peak RSS, verdict) and `log.tail` under its own attempt directory, and
`_DONE` last. A pass or a fail is a result; so is an engine crash (`verdict: error`), because a retry
would repeat it. Only infrastructure failures are retried.

A task that reaches `max_wall_s` is a result too (`verdict: timeout`), so set the limit from the
Mac's wall time with room to spare: at the table's speed factors a cell takes about 1.3x as long
on C4D and 1.6-1.8x on C3D or T2D. About 3x the Mac's time covers both; a hard rung of 1.5 hours
on the Mac wants about 16,000 s, above `ladder-sweep.toml`'s 7,200 s and the example
`max_task_wall_s` of 14,400 (raise both for a hard-rung campaign).

## Commands

| Command                                                                             | Changes something in a cloud                           |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `yapnr exp plan CAMPAIGN --backend B [--offline --image-digest D]`                  | no (a registry lookup of the image tag unless offline) |
| `yapnr exp submit PLAN [--dry-run] [--yes] [--max-usd X] [--only TASK...] [--wait]` | yes, unless `--dry-run`                                |
| `yapnr exp status PLAN [--json]`                                                    | no                                                     |
| `yapnr exp logs PLAN [TASK]`                                                        | no                                                     |
| `yapnr exp fetch PLAN [--full] [--into DIR] [--from DIR]`                           | no (egress is billed)                                  |
| `yapnr exp cancel PLAN [--submission N] [--dry-run]`                                | yes                                                    |
| `yapnr exp doctor --backend B`                                                      | no                                                     |
| `yapnr exp prices [--refresh] [--family F]`                                         | no (Billing Catalog read)                              |
| `yapnr exp unfreeze --backend gcp-batch`                                            | yes (deletes the freeze marker)                        |
| `yapnr exp calibration ingest --reference DIR --cloud DIR... --out FILE`            | no                                                     |

`PLAN` is the plan directory `plan` printed, or the campaign id. `submit --dry-run` on `gcp-batch`
prints every `gcloud` call it would make (all with `--impersonate-service-account=yapnr-submit@...`)
without making any.

## Google Cloud bootstrap

Run once, after billing exists. `$PROJECT`, `$BILLING`, `$REGION`, `$OWNER`, `$MAC_ID` and `$STATE`
are your values; keep them in your notes and the owner config, never in this repository. Install
the [gcloud CLI][gcloud-install] and [OpenTofu][tofu-install] in user space first.

### 1. Images

Make both GHCR packages public (package settings, "Change visibility"; this cannot be undone) or
create a GitHub token with `read:packages` and store it in Secret Manager after step 3
(`ghcr_token_secret` in the tfvars). Check: `yapnr exp plan experiments/smoke.toml --backend
gcp-batch` resolves `edge` to a digest only when the package is public or `YAPNR_REGISTRY_TOKEN` is
set.

### 2. Project and billing (owner account)

```sh
gcloud config configurations create yapnr-owner   # first: a new configuration has no account
! gcloud auth login "$OWNER"              # in Claude Code: the ! prefix, you sign in yourself
gcloud projects create "$PROJECT" --name="yapnr experiments"
gcloud billing projects link "$PROJECT" --billing-account="$BILLING"
gcloud config set project "$PROJECT"
gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com \
  iam.googleapis.com cloudbilling.googleapis.com billingbudgets.googleapis.com \
  storage.googleapis.com
gcloud projects describe "$PROJECT" --format='value(projectNumber)'   # project_number below
```

### 3. State bucket and infrastructure

```sh
gcloud storage buckets create "gs://$STATE" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$STATE" --versioning
! gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT"
cp infra/gcp/examples/owner.tfvars.example ~/.config/yapnr/gcp.tfvars   # then edit it
cd infra/gcp
tofu init -backend-config="bucket=$STATE"
tofu plan -var-file="$HOME/.config/yapnr/gcp.tfvars" -out=bootstrap.plan
tofu apply bootstrap.plan
```

The plan creates 80 resources for the example's two regions ([infra/gcp](https://github.com/Studio-Fug/yapnr/tree/main/infra/gcp)):
APIs, the VPC, both buckets, the service accounts and grants, the registry caches and the
`images` repositories for task images ([openEMS](#task-images-openems),
[Palace](#task-images-palace)), the Spot templates (C4D in `us-west4`; C4 in
`northamerica-northeast1`, which has no C4D: `region_template_shapes`), the budget and the guard
functions.

The project's `default` network allows SSH from anywhere and gives VMs external IPs; yapnr jobs
never use it, so delete it, and with it any way a hand-written job could land there:

```sh
gcloud compute firewall-rules list --filter="network=default" --format="value(name)" \
  | xargs -r gcloud compute firewall-rules delete --quiet
gcloud compute networks delete default --quiet
```

### 4. Quotas

Spot VMs consume the preemptible CPU quota once it is granted in a region ([quotas][quotas]). The
quota ceiling is the only hard cap on spending, so request only what the caps allow, in the enabled
regions only. Look the quota id up first (it is not verified here), then create a preference with
the id `yapnr-preemptible-cpus-<region>`, or request the quota in the console (IAM & Admin, Quotas &
System Limits); the budget guard finds an existing preference by quota id and region. Check first
that the region offers the planned shapes (the price table only shows that the region bills them):

```sh
gcloud compute machine-types list --project="$PROJECT" \
  --filter="zone~^$REGION- AND name=( c4d-highcpu-16 c4d-highcpu-8 c4-highcpu-16 c4-highcpu-8 )" \
  --format="table(name,zone)"
gcloud quotas info list --service=compute.googleapis.com --project="$PROJECT" \
  --filter="quotaId~PREEMPTIBLE" --format="value(quotaId)"
gcloud quotas preferences create --service=compute.googleapis.com --project="$PROJECT" \
  --quota-id=<id from the listing> --preference-id="yapnr-preemptible-cpus-$REGION" \
  --dimensions=region="$REGION" --preferred-value=64 --email="$OWNER" \
  --justification="Batch Spot jobs for open-source PCB routing research"
```

Repeat per region, and list each region and quota id in `quota_preferences` of the tfvars (then
`tofu apply` again). Leave the on-demand CPU quota at its default. A new billing account may get
less than it asks for at first.

The quota, not the price, sets how fast campaigns finish: at one task per physical core, 64
preemptible vCPUs of C4D run 32 single-threaded tasks at once, the work of about 25 M4
performance cores at the table's speed factor (the development Mac has 4 performance and 6
efficiency cores), for about $0.60 an hour in `us-west4`. Ask for more (256 vCPUs is about $2.40
an hour) once the kill-switch drill (step 9) and the first campaigns have shown the guards work;
raise `max_parallel_vcpus` with it.

### 5. Price key (optional)

```sh
gcloud services enable apikeys.googleapis.com
gcloud services api-keys create --display-name=yapnr-prices \
  --api-target=service=cloudbilling.googleapis.com
security add-generic-password -s yapnr-billing-catalog -a "$USER" -w   # paste the key
yapnr exp prices --refresh
```

### 6. Day-to-day identity

`tofu apply` granted `$MAC_ID` the right to impersonate `yapnr-submit` and nothing else:

```sh
gcloud config configurations create yapnr
! gcloud auth login "$MAC_ID"
gcloud config set project "$PROJECT"
gcloud config set auth/impersonate_service_account "yapnr-submit@${PROJECT}.iam.gserviceaccount.com"
```

`yapnr exp` passes `--impersonate-service-account` and `--project` on every call anyway; set
`gcloud_configuration = "yapnr"` in the owner config to pin the configuration too.

### 7. Owner config

Copy [`docs/examples/cloud.toml.example`](examples/cloud.toml.example) to
`~/.config/yapnr/cloud.toml` and fill `[gcp]` from `tofu output -json owner_config`.

### 8. Checks

```sh
yapnr exp doctor --backend gcp-batch      # buckets, Batch access, quotas, templates, freeze
```

### 9. Kill-switch drill

With the owner configuration (it may publish to the topic), publish a synthetic over-budget message
and check that `yapnr-submit` is disabled (`yapnr exp doctor` and `submit` fail to impersonate it)
and `control/frozen` appears. Only the owner can undo the freeze: re-enable the account, then clear
the marker:

```sh
gcloud pubsub topics publish yapnr-budget --configuration=yapnr-owner --message='{
  "budgetDisplayName": "drill", "costAmount": 51, "budgetAmount": 50,
  "costIntervalStart": "<first day of this month>T00:00:00Z", "currencyCode": "USD"}'
gcloud iam service-accounts describe "yapnr-submit@${PROJECT}.iam.gserviceaccount.com" \
  --configuration=yapnr-owner --format="value(disabled)"          # True
gcloud iam service-accounts enable "yapnr-submit@${PROJECT}.iam.gserviceaccount.com" \
  --configuration=yapnr-owner
yapnr exp unfreeze --backend gcp-batch
```

After a real freeze, do the same once you know why the budget ran out.

To drill the quota cut too, use a cost of 61 (above 120%), then restore the quota with the step 4
command and learn whether that needs a new approval.

### 10. Smoke job

Two tiny tasks on C4D, once without a template (does Batch take a Hyperdisk boot disk from an
instance policy?) and once from the template; each costs well under a cent:

```sh
yapnr exp plan experiments/smoke.toml --backend gcp-batch --shape c4d-highcpu-4 --no-template
yapnr exp submit <plan> --dry-run && yapnr exp submit <plan>
yapnr exp status <plan>; yapnr exp fetch <plan>
yapnr exp plan experiments/smoke.toml --backend gcp-batch --shape c4d-highcpu-16
yapnr exp plan experiments/ladder-small.toml --backend gcp-batch   # then the first ladder cells
```

### 11. Sign the owner out

```sh
gcloud auth revoke "$OWNER"
gcloud auth application-default revoke
```

### 12. Calibration

See [Calibration](#calibration). Then record the ranking in `[gcp] ranking`.

## Regions and quota-aware placement

Each region has its own preemptible CPU quota (step 4), so a second region doubles how much runs at
once. A region needs four things: the quota, an entry in the tfvars' `regions` (its subnet,
registry caches and templates; `region_template_shapes` when it lacks a family of
`template_shapes`), `quota_preferences` for the budget guard, and an entry in `[gcp] regions` of
the owner config. Then rank the (family, region) pairs; each ranked pair of a Hyperdisk family
needs templates for its shapes in its region (`yapnr exp doctor` lists them):

```toml
[gcp]
regions = ["us-west4", "northamerica-northeast1"]
ranking = [["c4d", "us-west4"], ["c4", "northamerica-northeast1"]]
```

A second pair in the same region (C4 in `us-west4`, say) adds no capacity: it draws on the same
quota.

- **Plan.** Every ranked pair a class can use is a candidate, in rank order (a campaign without
  `[placement] families` may use every ranked family). `plan` prints them with their price per
  VM-hour and the class's expected cost and ceiling there, and the ceiling if every class landed
  on its dearest candidate; its estimate and caps are those of the first. Without a ranking a
  class has one candidate, as before.
- **Submit.** For each class, `submit` reads the candidates' regional `PREEMPTIBLE_CPUS` limit
  and live usage (`gcloud compute regions describe`), subtracts the vCPUs the same submit already
  sent to that region, and places the class on the first candidate with room for one more of its
  VMs. A candidate whose instance template does not exist is skipped; a quota it cannot read is
  not room. When no candidate has room the class waits in the first (Batch queues the job until
  quota frees up). The caps are checked on the chosen placements, `submissions/<n>.json` records
  the choice under `placement.choice` (the pair, its rank, why, and each candidate looked at), and
  `status` prints each submission's region. A class with one candidate reads nothing, and
  `submit --dry-run` reads no quota (it makes no calls), so it shows the first candidate.
- **One machine type.** A wall-clock-budgeted campaign (`ladder-cell`, `bench` and `mc-eval` by
  default) runs on one machine type: a family is a candidate only if it gives every class the same
  shape, the first submission chooses it by quota, and later submissions keep it. A campaign that
  is compared with an earlier one should pin the earlier one's family (`plan --family c4d`, or
  `--shape`): C4 and C4D search at different speeds.
- **Pins.** `plan --region R` keeps only that region's candidates; `submit --region R` keeps only
  the plan's candidates in R for this submit (no spill to another region).
- **Data.** The buckets stay in the home region, so tasks elsewhere read their bundles and write
  their results across regions (megabytes per task). yapnr images come through each region's
  `ghcr` cache; an image from the `images` repository is pulled from the region its reference
  names, through Private Google Access, at $0.01 per GiB between North American regions (Billing
  Catalog, October 2026): about $0.02 per VM for the 1.6 GB openEMS image.
- **Not seen.** Usage counts running VMs only: a job queued a moment ago, or another campaign's
  submit at the same time, is not counted, and neither is a Spot stockout (a region with quota
  but no capacity). Cancel and `submit --region` the other region.

## Costs and guards

Prices are list prices from the committed table
([`yapnr/exp/data/gcp-spot-prices.json`](https://github.com/Studio-Fug/yapnr/blob/main/yapnr/exp/data/gcp-spot-prices.json):
every region's Spot prices from the [Cloud Billing Catalog API][catalog], accessed 2026-10-04)
until `prices --refresh` replaces them; regions missing from the table are priced at
`us-central1`, which is above the median, and the plan says so. A price only shows that a region
bills a family, not that it offers the machine types (step 4 checks that). Speeds are PassMark
single-thread ratios to the development Mac's M4 until the calibration measures them. Examples
from that table, as `plan` prints them with the example owner config (C4D Spot in `us-west4`, one
task per physical core):

| Campaign                                          | Tasks | Expected | Ceiling |
| ------------------------------------------------- | ----: | -------: | ------: |
| `smoke.toml` on `c4d-highcpu-4` (`--no-template`) |     2 |  < $0.01 |   $0.05 |
| `ladder-small.toml` (one `c4d-highcpu-16`)        |     4 |    $0.01 |   $0.38 |
| `calibration-ladder.toml` (per shape, 8 vCPUs)    |    12 |    $0.01 |   $1.03 |
| `ladder-sweep.toml` (8 x 16)                      |   128 |    $0.07 |   $6.96 |

The ceilings are far above the expected costs on purpose: they price every attempt at its full
`max_wall_s` plus Batch's 10-minute grace, with every retry, on whole VMs even when a small job
fills only part of one.

The layers, each of which holds when the one above fails:

1. **Quota** (step 4): the hard ceiling. Spot burn is at most quota x price: 64 vCPUs of
   `c4d-highcpu` cost about $0.016 per vCPU-hour with memory in `us-central1`, so about $1.04 an
   hour, less in `us-west4`.
2. **Per-submit caps** (`[limits]`): `max_tasks`, `max_task_wall_s`, `max_parallel_vcpus` (the
   jobs' parallelism), `max_retries` (at most 3, infrastructure exit codes only) and
   `max_campaign_hours` (the jobs' deadline). `submit` refuses tasks that a queued or running job
   of the campaign still holds (no task runs twice), and on-demand placements (`spot = false`,
   outside the quota ceiling) unless `allow_on_demand = true`.
3. **The estimate**: every plan prints the expected cost and a ceiling (every attempt at its
   maximum wall time plus the grace, with every retry, on whole VMs; or the quota's VMs for
   `max_campaign_hours` plus the reaper's interval, whichever is lower).
   `submit` asks above `confirm_usd` and refuses a ceiling above `refuse_usd` unless `--max-usd`
   raises it, never above `hard_refuse_usd`.
4. **The reaper**: every 15 minutes it cancels jobs past their `deadline` label and deletes yapnr VMs
   an hour past it.
5. **The budget and kill switch**: at 100% of the monthly budget the guard disables
   `yapnr-submit` (no client can submit until the owner re-enables it), writes `control/frozen`
   (submit and every task check it) and cancels every yapnr job; at 120% it sets the preemptible
   CPU quota preferences to 0. The budget counts spend gross of credits, so free-trial or
   research credits do not hide a runaway; raise `budget_usd` to spend credits faster. Budget
   data lags by hours ([budgets][budgets]), which is why it is the last layer. Google's "disable
   billing" recipe is not used: it can delete resources.
6. **Lifecycle rules** delete result archives after 90 days and checkpoints after 14.

`fetch` downloads summaries by default; `--full` adds every `result.tar.gz`, and internet egress
is billed (about $0.12 per GiB, [network pricing][network-pricing]).

## Slurm and Apptainer (HPC allocations)

Add the site to the owner config (`[slurm.<site>]`: account, partition, store, SIF path, array and
concurrency limits, `chunk`, `slots`, `speed`), then:

```sh
yapnr exp plan experiments/ladder-sweep.toml --backend slurm --site <site>
rsync -a <plan>/ <login node>:<somewhere>/        # by hand; agents never SSH to a site
# on the login node:
bash <somewhere>/backend/slurm/submit.sh
bash <somewhere>/backend/slurm/status.sh
```

`submit.sh` needs only bash, coreutils, Slurm and Apptainer: it stages the bundles and campaign
files into the site's store, builds the SIF once from the pinned digest
(`apptainer pull ... docker://ghcr.io/studio-fug/yapnr@sha256:...`), and submits one
`sbatch --array` per resource class. Each array element runs `chunk` tasks in sequence (the plan
raises `chunk` until the array fits `max_array`), with `--requeue --signal=B:USR1@300`: the batch
shell forwards the signal, the wrapper flushes checkpoints and exits, and the element is requeued;
finished tasks are skipped. Copy the store's `campaigns/<id>` back and run
`yapnr exp fetch <plan> --from <copy>`. A private campaign is refused on a site unless the owner
config marks it `private_ok = true`.

Before writing an allocation request, read the site's charging policy:

- **Per core** (shared nodes): keep `slots = 1`; each array element is one task's cores.
- **Per node** (the scheduler gives whole nodes, as many large centres do): set `slots` to the
  tasks one node holds (its cores over the task's cores, within its memory) and add
  `extra_sbatch = ["--exclusive"]`. Each element then runs that many wrappers side by side and
  is charged one node; with `slots = 1` every one-core task would be charged a whole node.
- **The request itself**: `plan --backend slurm` prints core-hours at the site's `speed`
  (default 0.5 of the Mac's M4 performance core, typical of HPC server cores by single-thread
  rating), before requeues and the idle tail of each element. A calibration run on the site
  replaces the guess before the full request: plan `experiments/calibration-ladder.toml` with
  `--backend slurm`, fetch it with `--from`, and `calibration ingest` it like a cloud shape; its
  factor is keyed `slurm/<CPU model>`, the value for `speed`. Sites that bill node-hours want the
  core-hours divided by the cores per node.
- **Images**: `submit.sh` pulls the SIF on the login node, keeping Apptainer's layer cache and
  build directory under the store (`APPTAINER_CACHEDIR`, `APPTAINER_TMPDIR`; set them to
  override), because home quotas and login-node `/tmp` are small. While the GHCR packages are
  private, export `APPTAINER_DOCKER_USERNAME` and `APPTAINER_DOCKER_PASSWORD` (a `read:packages`
  token) before running it. Compute nodes need no internet access.

## Calibration

The calibration measures what the estimator now assumes. Run `experiments/calibration-ladder.toml`
on the Mac (the reference) and on each candidate shape, then ingest the records:

```sh
yapnr exp plan experiments/calibration-ladder.toml --backend local
yapnr exp plan experiments/calibration-ladder.toml --backend gcp-batch --shape c4d-highcpu-8 --region us-west4
yapnr exp plan experiments/calibration-ladder.toml --backend gcp-batch --shape c4-highcpu-8 --region northamerica-northeast1
# ... submit each, fetch each, then:
yapnr exp calibration ingest --reference <fetched>/<local id> --cloud <fetched>/<id> ... \
  --out ~/.config/yapnr/calibration.json
```

The speed factors are ratios to the reference's wall times, so a loaded reference makes every cloud
shape look faster than it is. Run the reference on an otherwise idle Mac (stop the other
experiments first) and plan it with a copy of the owner config (`YAPNR_CLOUD_CONFIG=<copy>`) whose
`[local]` has `nice = 0` and `workers` at most the performance cores
(`sysctl -n hw.perflevel0.physicalcpu`: 4 on the development Mac, which also has 6 efficiency
cores). `ingest` prints the reference's median load average and warns when it reached half the
Mac's cores.

`ingest` pairs tasks by kind and labels and writes, per machine type and family, the median of
reference wall time over cloud wall time, with the number of pairs and the spread; set
`[prices] calibration` to use it. Each shape costs a few cents at the table's prices; the design's
full calibration (nine families, two regions, both SMT packings) is capped at $10.

Read two rankings from the factors. Cost per result (price / speed) says where a campaign is
cheapest; results per hour within the quota (speed / vCPUs per task) says how fast it finishes,
and the quota is the scarce part: C4D at one task per core yields about 0.4 of an M4 core per
quota vCPU, T2D and C4A (a full core per vCPU) about 0.55 at the table's factors. Hard rungs and
wall-clock-budgeted search favour the fastest core (C4D); seed sweeps of short cells may favour
throughput. Neither T2D nor C4A is offered in the two proposed regions: add a region that has them
(the price table lists `northamerica-northeast2` for T2D and `europe-west4` for C4A) to
`regions` and the tfvars before calibrating them (C4A, a Hyperdisk-only family, also needs
`c4a-highcpu-8` in `template_shapes`), and compare C4A (arm64) boards only with other
arm64 runs, such as the Mac's and the CI ladder's. `ingest` keys factors by machine type, not by
packing: ingest a one-task-per-vCPU run (`[placement] packing = "vcpu"` in a copy of the campaign)
into a calibration file of its own.

## Private campaigns

`visibility = "private"` in a campaign file gives opaque campaign and task ids (`<date>-p-<hash>`,
`p/<16 hex>`), keeps the readable mapping in the local plan directory only, leaves the readable
spec out of the uploaded `campaign.json`, puts only opaque values in labels, plans into
`[local] private_results_root`, and refuses to fetch anywhere else or into a checkout of the public
repository. The generator of private bundles lives in the private repository and emits the generic
kinds.

### Any command as a task: stage plans

Until the halving driver writes its own stage plans, `mc-eval` runs any list of commands, which is
how private board experiments (an A/B of engine snapshots, a full iteration per placement) move to
the cloud without code changes. Each line of a JSON Lines file is one task: the command, the
directories it needs (each packed into a bundle and unpacked at `dest` in the task's work
directory, which is also the working directory), its environment, resources, and the JSON record
it writes under `out/`:

```text
{"id": "arm-a/s0", "command": ["${PYTHON}", "-m", "pnr.mc.halving", "--out", "out/run",
   "--inputs", "boards/inputs", "--constraints", "boards/constraints.json", "--repo", ".",
   "--seed", "0", "--procs", "4", "--native-parallel", "2", "--native-workers", "2"],
 "inputs": [{"dest": "snap", "path": "snapshots/arm-a"}, {"dest": "boards", "path": "board"}],
 "env": {"PYTHONPATH": "snap/hardware/pnr", "PNR_LIVE_CANDIDATE": "hier"},
 "resources": {"cpus": 4, "memory_gb": 12, "disk_gb": 4, "max_wall_s": 14400},
 "record": "out/run/status.json", "labels": {"arm": "a", "seed": "0"}}
```

(one line per task in the file; wrapped here). The campaign file names it:

```toml
schema = "yapnr-campaign-v1"
kind = "mc-eval"
name = "snapshot-ab"
source = "none"          # the engine comes from the snapshot bundles
image = "edge"
visibility = "private"

[config]
stage_plan = "stage.jsonl"   # relative to the campaign file; input paths relative to it
```

Paths in commands and `env` are relative to the work directory: no host paths, and no KiCad
paths, which the wrapper sets for the image (`PNR_KICAD_CLI`, `PNR_KICAD_PYTHON`,
`PNR_KICAD_FOOTPRINTS`). Give each task the cores its processes use (`--procs` and the native
workers above; `cpus = 2` for an evaluation that runs two KiCad processes) and the disk its work
directory needs (`disk_gb`, default 10 for `mc-eval`): Batch packs by CPU and memory only, so
`plan` caps the tasks per VM to fit the boot disk (`boot_disk_gb` less 12 GB for the OS and the
image). A task without checkpoints starts again after a Spot preemption, so keep Spot tasks to a
few hours; longer ones belong on standard VMs (`[placement] spot = false`, which
`limits.allow_on_demand` must allow) or on the Mac. `fetch` writes `assembled/dataset.jsonl`, one
line per task with its record and the machine it ran on. A private A/B campaign of this shape
(a toy engine snapshot as the bundle) was planned, run and fetched end to end on the local
backend.

Three optional keys serve long evaluations. `"checkpoint": {"path": "out/run"}` makes the task
resumable: the wrapper copies the top-level files of that directory to the store every
`sync_every_s` (default 300, at least 30) and when it is stopped (`on_signal`, default true), and
restores them before a retry, so a command that resumes from its run directory continues after a
Spot preemption instead of starting again. A command that exits 75 without its record has its
checkpoint synced too and is retried, so a long run can stop before `max_wall_s` and go on in the
next attempt (at most `max_retries` retries per submission, preemptions included; `submit` again
resumes after that). `"prune"` lists globs under `out/` that the result
archive leaves out (caches), and `"verdict": {"file": "out/run/report.json", "json_path": "ok"}`
reads a pass or a fail from the record.

#### RF runs

`tools/exp/rf_stage_plan.py` writes such a campaign for `yapnr.rf` runs from a list of jobs: a
preset case each, optionally a spec file that replaces the case's spec, a criteria file that
replaces the case's pass criteria and dense frequencies (for a spec in another band), the
starting design (`seed`), an iteration cap, the threads and the resources (the format is in its
docstring).

```sh
python3 tools/exp/rf_stage_plan.py jobs.toml --repo <checkout with yapnr.rf> --out <dir>
yapnr exp plan <dir>/campaign.toml --backend gcp-batch --shape c4d-highcpu-8
```

The source bundle is a `git archive` of the committed revision (`--commit`, default `HEAD`),
never the working tree, so a branch another session is still editing runs as it was last
committed. Each task runs the job bundle's `rf_job.py`, which runs `python -m yapnr.rf.cases run
CASE --out out/<id>` with the spec, the criteria, `optimizer.seed` or `solver.threads` replaced
(or that command itself for a job with nothing to replace and `attempt_s = 0`) on as many cores as
it has threads, with `OMP_NUM_THREADS` and `MKL_NUM_THREADS` at that number and
`OPENBLAS_NUM_THREADS=1`. Cores are physical, two vCPUs each on SMT shapes: one 4-thread run fills
a `c4d-highcpu-8`. The solver runs its native kernel by default, on the job's threads (`YAPNR_RF_THREADS`,
re-validations included), from the image's yapnr wheel when the bundle's C sources are the ones
that library was built from ([solver backends](rf-solver-backends.md)). Every line sets
`YAPNR_RF_REQUIRE_NATIVE=1` unless its job says `require_native = false`: a bundle whose C
sources differ from the image's would otherwise run the numpy reference, 12–60 times slower, with
one line in the task's log; with it the task fails at its first simulation and the diagnostic
job's `ok` is false. `--image-commit REV` (the image's source revision, its
`org.opencontainers.image.revision`) refuses such a bundle before anything is uploaded, and the
manifest records both sources' sha256. A job's `backend`, `dtype` and `env` (other `YAPNR_RF_*`
variables, such as `YAPNR_RF_TBLOCK`) set the solver's environment over its spec. A bundle from
before the native kernel runs torch on at most 4 threads, and the generator warns above that.
The record is `out/<id>/validation.json` (the verdict is its `ok`), the run directory is the
checkpoint, and the cases runner resumes from it; `--max-iterations` counts the iterations of one
attempt, so an attempt resumed mid-loop may run that many again. A run longer than `max_wall_s`
goes on over attempts: `rf_job.py` stops the loop before an iteration that would end after
`attempt_s` (default `max_wall_s` less a 24th), starts the end (binarize, export, re-validate)
only with `end_s` left (default a third of the attempt, at most an hour), and otherwise exits 75.
Every attempt makes progress. On Batch a submission holds `max_retries + 1` attempts, and the
reaper cancels it `max_campaign_hours` after `submit` (with the example limits, about three
attempts of 3.8 hours; preemptions take attempts too); the checkpoint stays in the store, and
`submit` again goes on from it. A job with `validate = "<run directory>"` re-validates a finished
run instead (a fetched one: its top-level files go into the plan as an input), as a task of its
own that starts again after a preemption: for a fine-grid validation longer than what is left of
the run's last attempt (pass `--no-fine` or `--finer 0` to the run in `args`). A job with
`diagnostic = true` runs the job bundle's `rf_diag.py` instead, which records the interpreter,
the CPUs, the versions of numpy, torch, Pillow and PyYAML, whether the sources compile on the
image's Python, the native FDTD library the bundle would run (or why none), and
`python -m yapnr.rf.cases --help`; run one before the first campaign of a new
branch or image. `--plain-lines` leaves the three optional keys and the attempts out, for a
`yapnr` whose `mc-eval` refuses them.

### Task images (openEMS)

A campaign runs in the yapnr image unless `image` names another one by its full reference. Batch
VMs have no internet access, so such an image lives in the project's `images` repository
(`[gcp] images`, one per region, made by `infra/gcp`), and a campaign pins it by digest. An image
that is not a yapnr image says how it runs the task wrapper in a `[runtime]` table: `python`, the
interpreter that runs `task.py` and that `${PYTHON}` names in commands (default
`/opt/venv/bin/python`), and `entrypoint`, the launcher around both (default the yapnr image's
`yapnr-kicad-env`; `""` for none). The wrapper needs only the standard library of Python 3.9 or
later.

```toml
image = "<region>-docker.pkg.dev/<project>/images/openems:0.37.0-rc3-x86-64-v4@sha256:..."

[runtime]
python = "/opt/openEMS/venv/bin/python"
entrypoint = ""
```

[`docker/openems`](https://github.com/Studio-Fug/yapnr/tree/main/docker/openems) builds openEMS
from source with its Python bindings, numpy, h5py, scipy, matplotlib and shapely, in two
variants: a generic x86-64 build and an AVX-512 build (`-march=x86-64-v4 -mtune=znver4`) for
C4D's Zen 5 cores. Cloud Build builds both on one machine and pushes them as
`openems:<openEMS version>-x86-64` and `...-x86-64-v4`, running as `yapnr-image-build`; the
submitter must be allowed to start builds and act as that account (the project owner). A
regional build bills the vCPU- and GiB-minutes of the `e2-highcpu-8` machine `cloudbuild.yaml`
asks for (in us-west4 in October 2026, $0.0018 per vCPU-minute and $0.0004 per GiB-minute: about
$0.018 a minute; see the Cloud Build pricing page), and the stored images per GiB-month like the
registry caches. A new project's regional quota may refuse larger build machines
(`FAILED_PRECONDITION ... cannot run builds of this machine type in this region`), so the file asks
for the 8-vCPU machine; raise `machineType` and `_NJOBS` together once the quota allows.

`tools/exp/openems_plan.py` does the rest. `image` prints (`--run`: submits) the build,
`digests` lists the built tags with their digests and sizes, `plan` turns N model scripts into
one `mc-eval` campaign of N tasks (the format is in its docstring), and `collect` lays fetched
results out as local runs leave them:

```sh
python3 tools/exp/openems_plan.py image --run
python3 tools/exp/openems_plan.py plan models.toml --out <dir>      # pins the image's digest
yapnr exp plan <dir>/campaign.toml --backend gcp-batch
yapnr exp submit <cid> && yapnr exp status <cid>      # again until every task is done
yapnr exp fetch <cid> --full
python3 tools/exp/openems_plan.py collect <cid> --dest <tree>       # <tree>/runs/<model>/...
```

Each task runs `SCRIPT ARGS` through the job bundle's `openems_job.py` with `OMP_NUM_THREADS` at
the model's `threads` and records `out/<id>.job.json` (its verdict is `ok`, the exit code of the
script): wall and CPU time, peak memory, the CPU and whether it has AVX-512, the image's build
flags and openEMS's own iterations, cells, seconds and MCells/s. `engine` (`basic`, `sse`,
`sse-compressed` or `multithreaded`, openEMS's default) and `openems_options` (openEMS
command-line options such as `exact-endcriteria`, which makes the stop step independent of the
machine's speed), for the campaign or per model, reach every `openEMS.Run` of an unchanged model
script: the runner starts it under a bootstrap that adds them to `Run`'s keyword arguments, and
the record and the task's labels carry them. `models_per_vm` models of
`threads` cores share a VM: `vm_vcpus` is `2 x models_per_vm x threads` on C4D (two vCPUs a core),
or half that with `packing = "vcpu"`, which runs the threads on SMT siblings; a shape without an
instance template runs from an instance policy. A model with its own `threads` becomes a task
class of its own (its own Batch job and VMs, still `models_per_vm` to a VM of `vm_vcpus`), so
keep one thread count per jobs file to fill the VMs. openEMS runs do not checkpoint, so a
preempted model starts again.

### Task images (Palace)

[AWS Palace](https://github.com/awslabs/palace) (Apache-2.0) is the finite-element solver that
checks openEMS's FDTD results: driven, eigenmode, electrostatic, magnetostatic and 2D
boundary-mode problems, lumped and wave ports, conductivity and impedance boundaries, adaptive
mesh refinement and adaptive frequency sweeps, all MPI-parallel. Palace publishes no public
container image, so [`docker/palace`](https://github.com/Studio-Fug/yapnr/tree/main/docker/palace)
builds its CMake superbuild at a pinned `main` commit (one with fixes for several boundary
attributes that share a material entry, after release v0.18.1) on Ubuntu 24.04 with OpenMPI and
OpenBLAS: SuperLU_DIST and MUMPS, ARPACK for the wave-port mode solves and eigenmodes, GSLIB and
LIBXSMM; no SLEPc, STRUMPACK, SUNDIALS (transient) or GPU. The image adds Palace's
coplanar-waveguide example and its regression references, and a Python 3.12 with gmsh, numpy and
shapely, so a task can mesh its model before the solve. There is one variant,
`palace:<commit7>-pts-x86-64-v3` (AVX2), because a second one doubles the build while the hot
kernels pick AVX-512 at run time anyway (OpenBLAS, LIBXSMM).

The image has no ParMETIS, which Palace's superbuild always links but whose licence allows
evaluation use only outside non-profit research and forbids redistribution: `docker/palace/patches`
builds Scotch/PT-Scotch (CeCILL-C) in its place and keeps the orderings parallel. SuperLU_DIST's
parallel ordering (`ColumnOrdering: "ParMETIS"`) calls Scotch's ParMETIS-compatible library, MUMPS
calls PT-Scotch (`ColumnOrdering: "PTScotch"`), and the default stays SuperLU_DIST with serial
METIS. The build proves the absence (the superbuild, the installed files and symbols, Palace's link
map, a conformance test of the ordering against the ParMETIS manual, an SPDX bill of materials and
syft's scan of the image) and fails otherwise; the image's README has the details. The earlier
build with ParMETIS, `palace:b797ea8-x86-64-v3`, is kept for evaluation (comparisons) only.

Cloud Build builds it on the 8-vCPU machine the regional quota allows, in a 3-hour budget (the
default timeout is 10 minutes); a build without its caches takes about 25 minutes (14 for the
dependencies, 5 for Palace), about $0.45, and one with both dependency stages cached about 7
minutes (October 2026). The dependency stages are pushed as
`palace-deps:<tag>-x86-64-v3-solvers` (the ordering libraries and the direct solvers) and
`palace-deps:<tag>-x86-64-v3` before Palace compiles and are read back with `--cache-from`, so a
build that fails in MFEM or Palace, or a change to the runtime stage, does not rebuild the
dependencies. `tools/exp/palace_plan.py` works
like `openems_plan.py` (both use `tools/exp/image_tasks.py`); the jobs format is in its docstring:

```sh
python3 tools/exp/palace_plan.py image --run                      # about 30 minutes
python3 tools/exp/palace_plan.py plan models.toml --out <dir>      # pins the image's digest
yapnr exp plan <dir>/campaign.toml --backend gcp-batch
yapnr exp submit <cid> && yapnr exp status <cid>      # again until every task is done
yapnr exp fetch <cid>                                 # logs, records, Palace's tables
python3 tools/exp/palace_plan.py collect <cid> --dest <tree>       # <tree>/runs/<model>/...
```

A job names a Palace configuration (`config`, a path in the task: an input, or the image's
`/opt/palace/share/palace/examples/...`), optional overrides (`set = {"Solver.Order" = 3}`, for the
campaign or per job), an optional `prepare` script that meshes the model and writes the
configuration into `{out}` first, optional `stages` (configurations solved first, each into
`out/<id>/stage-<k>-<name>` with `k` its place in the list, `CONFIG@1` on one rank; a stage
listed twice is refused) with `mesh_from` naming the stage whose saved adapted mesh the main
configuration solves (refinement, then a sweep of the refined mesh, in one task), and an optional
`reference` directory whose `port-S.csv` the result must match. Each task
runs the job bundle's `palace_job.py`: it reads the configuration
(Palace's relaxed JSON: comments, trailing commas, integer ranges), applies the overrides, moves
the output to `out/<id>/postpro` and makes the mesh path absolute (`out/<id>/config.json`), checks
it with `palace --dry-run`, then solves with `mpirun -np <ranks>` from the configuration's
directory. OpenMPI runs as root there (Batch's container user) without cross-memory attach (no
ptrace in the container), over shared memory (its `ob1` layer with the `vader` transport, not UCX
or libfabric: one VM, no RDMA) on the 1 GiB of `/dev/shm` the task containers get. The record,
`out/<id>.job.json` (its verdict is `ok`: the solve exited 0 and matched the reference), keeps the
stage that failed, wall and CPU time, the CPU, and Palace's own numbers from `palace.json` and its
log: degrees of freedom and mesh elements, the AMR refinements and the unknowns of each solve,
linear solves and iterations, timers, peak memory per rank and summed over the ranks. It is
written when the task starts (`failed` is `running`), after every stage and at the end, so a task
killed at its time limit or preempted still leaves the stages it finished.

The smoke campaign (2026-10-04) ran Palace's coplanar-waveguide example from the image, a driven
sweep over 7 frequencies with four wave ports, on 8 ranks of a `c4d-highcpu-16`: 117,764 degrees
of freedom, 34 s of solve, 224 MB peak per rank, and its S-parameters matched Palace's regression
reference to a complex difference of 6e-8 (the task used 51 s of the VM, which started 37 s
after scheduling).

A model gets a VM: `ranks` MPI ranks on as many physical cores (`vm_vcpus` is twice that on C4D),
bound to them; `packing = "vcpu"` puts a rank on each hardware thread, and with
`models_per_vm` above one the ranks are left unbound (two `mpirun`s would bind to the same
cores). `memory_gb` decides between the highcpu and standard shapes (`yapnr exp plan`'s rule);
the plan asks for an instance policy when none of its families has a Spot template for that
shape (`infra/gcp` makes them for `c4d-highcpu-8`, `c4d-highcpu-16` and `c4d-standard-16`, and
for `c4-highcpu-8` and `c4-highcpu-16` in the example's second region). A job may set its own
`ranks` or `memory_gb`: `yapnr exp` gives each such resource class its own shape, and the plan
checks every job, so one that needs a shape without a template (56 GB: `c4d-highmem-16`; 16
ranks: a 32-vCPU shape) turns the campaign to instance policies instead of being refused at
submit. A job with fewer ranks than the campaign still reserves the campaign's cores (a
rank-scaling run gets the VM to itself rather than sharing it with another job bound to the same
cores). Palace does not checkpoint and adaptive refinement cannot restart, so a preempted model
starts again: keep Spot models to an hour or two.

## Testing

Everything above is tested offline and for free: `bazel test //tests/unit/exp/...
//infra/gcp/functions/guard/...` (the wrapper's exit codes, preemption and retry, checkpoints;
planning; golden Batch JSON validated against the vendored
[Batch v1 discovery document](https://github.com/Studio-Fug/yapnr/tree/main/third_party/googleapis);
the exact `gcloud` calls of a dry-run submit; golden `sbatch` scripts with `bash -n` and
shellcheck; fetch and ladder assembly; prices and calibration; the budget guard), and the manual
`bazel test //infra/gcp:tofu_check` (see [infra/gcp](https://github.com/Studio-Fug/yapnr/tree/main/infra/gcp)).
The test process cannot reach a cloud: real `gcloud` calls are disabled while
`YAPNR_EXP_NO_CLOUD` is set.

## Not verified yet

What only a real project or site can settle, in the order the first runs will meet it:

- at the first `tofu apply`: the instance templates' Hyperdisk settings and the Batch image
  family, and whether a custom role may hold `iam.serviceAccounts.disable` and be granted on the
  single `yapnr-submit` account (the kill switch's first step);
- the exact preemptible quota ids (step 4), and whether restoring a cut quota needs a new
  approval (the drill of step 9);
- the Billing Catalog SKU descriptions `prices --refresh` parses (`Spot Preemptible <FAMILY> ...
Instance Core|Ram running in ...`);
- in the smoke job (step 10): whether a Batch instance policy accepts a `hyperdisk-balanced` boot
  disk for C4D (it tries both; templates are the default); the gcsfuse mount options (the
  container runs as root and mounts with `--implicit-dirs`) together with the runs bucket's IAM
  condition that keeps tasks out of `control/`;
- on a Slurm site: whether `USR1` reaches the wrappers through Apptainer's PID namespace and
  whether users may `scontrol requeue` their own array elements (the scripts are tested with
  stub `apptainer` and `scontrol` only);
- a Spot stockout is not seen by quota-aware placement (a region with quota but no capacity):
  cancel and `submit --region` another candidate region.

## References

Accessed 2026-10-02.

- [Batch quotas and limits](https://docs.cloud.google.com/batch/docs/quotas),
  [task retries](https://docs.cloud.google.com/batch/docs/automate-task-retries),
  [reserved exit codes](https://docs.cloud.google.com/batch/docs/troubleshooting),
  [instance templates](https://docs.cloud.google.com/batch/docs/create-run-job-vm-template),
  [VM OS images](https://docs.cloud.google.com/batch/docs/vm-os-environment-overview),
  [storage volumes](https://docs.cloud.google.com/batch/docs/create-run-job-storage),
  [Batch pricing](https://cloud.google.com/batch/pricing)
- [Spot VM pricing][spot-pricing], [Spot VMs](https://docs.cloud.google.com/compute/docs/instances/spot),
  [disk pricing](https://cloud.google.com/compute/disks-image-pricing),
  [Billing Catalog snapshot][snapshot]
- [Compute Engine quotas][quotas],
  [`gcloud quotas preferences create`](https://docs.cloud.google.com/sdk/gcloud/reference/quotas/preferences/create),
  [quota preference API](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences/patch)
- [Budgets][budgets],
  [budget notifications](https://docs.cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications),
  [Cloud Billing Catalog API][catalog], [API keys][api-keys],
  [service-account impersonation](https://docs.cloud.google.com/docs/authentication/use-service-account-impersonation)
- [Artifact Registry remote repositories](https://docs.cloud.google.com/artifact-registry/docs/repositories/remote-overview),
  [network pricing][network-pricing], [Cloud Scheduler pricing](https://cloud.google.com/scheduler/pricing)
- [Apptainer with OCI images](https://apptainer.org/docs/user/latest/docker_and_oci.html),
  [Slurm job arrays](https://slurm.schedmd.com/job_array.html),
  [OpenTofu `tofu test`](https://opentofu.org/docs/cli/commands/test/)

[api-keys]: https://docs.cloud.google.com/docs/authentication/api-keys
[budgets]: https://docs.cloud.google.com/billing/docs/how-to/budgets
[catalog]: https://docs.cloud.google.com/billing/docs/how-to/get-pricing-information-api
[gcloud-install]: https://docs.cloud.google.com/sdk/docs/install
[network-pricing]: https://cloud.google.com/vpc/network-pricing
[quotas]: https://docs.cloud.google.com/compute/resource-usage
[snapshot]: https://github.com/Cyclenerd/google-cloud-pricing-cost-calculator
[spot-pricing]: https://cloud.google.com/spot-vms/pricing
[tofu-install]: https://opentofu.org/docs/intro/install/
