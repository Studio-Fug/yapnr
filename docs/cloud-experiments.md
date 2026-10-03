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

The plan creates 69 resources for two regions ([infra/gcp](https://github.com/Studio-Fug/yapnr/tree/main/infra/gcp)):
APIs, the VPC, both buckets, the service accounts and grants, the registry caches, the Spot
templates, the budget and the guard functions.

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
the id the budget guard knows, `yapnr-preemptible-cpus-<region>`. Check first that the region
offers the planned shapes (the price table only shows that the region bills them):

```sh
gcloud compute machine-types list --project="$PROJECT" \
  --filter="zone~^$REGION- AND name=( c4d-highcpu-16 c4d-highcpu-8 c3d-highcpu-8 )" \
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

## Costs and guards

Prices are list prices from the committed table
([`yapnr/exp/data/gcp-spot-prices.json`](https://github.com/Studio-Fug/yapnr/blob/main/yapnr/exp/data/gcp-spot-prices.json),
accessed 2026-10-02: the [Spot pricing page][spot-pricing] for `us-central1` and a public
[Billing Catalog snapshot][snapshot] of 2026-09-24 for other regions) until `prices --refresh`
replaces them; regions missing from the table are priced at `us-central1`, which is above the
median, and the plan says so. Speeds are PassMark single-thread ratios to the development Mac's M4
until the calibration measures them. Examples from that table (C4D Spot in `us-west4`, 8 tasks on a
`c4d-highcpu-16`):

| Campaign                        | Tasks | Expected | Ceiling |
| ------------------------------- | ----: | -------: | ------: |
| `smoke.toml` on `c4d-highcpu-4` |     2 |  < $0.01 |   $0.03 |
| `ladder-small.toml`             |     4 |    $0.01 |   $0.16 |
| `ladder-sweep.toml` (8 x 16)    |   128 |    $0.07 |   $7.26 |

The layers, each of which holds when the one above fails:

1. **Quota** (step 4): the hard ceiling. Spot burn is at most quota x price: 64 vCPUs of
   `c4d-highcpu` cost about $0.016 per vCPU-hour with memory in `us-central1`, so about $1.04 an
   hour, less in `us-west4`.
2. **Per-submit caps** (`[limits]`): `max_tasks`, `max_task_wall_s`, `max_parallel_vcpus` (the
   jobs' parallelism), `max_retries` (at most 3, infrastructure exit codes only) and
   `max_campaign_hours` (the jobs' deadline). `submit` refuses tasks that a queued or running job
   of the campaign still holds (no task runs twice), and on-demand placements (`spot = false`,
   outside the quota ceiling) unless `allow_on_demand = true`.
3. **The estimate**: every plan prints the expected cost and a ceiling (every task at its maximum
   wall time with every retry, or the quota for `max_campaign_hours`, whichever is lower).
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
concurrency limits, `chunk`), then:

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

## Calibration

The calibration measures what the estimator now assumes. Run `experiments/calibration-ladder.toml`
on the Mac (the reference) and on each candidate shape, then ingest the records:

```sh
yapnr exp plan experiments/calibration-ladder.toml --backend local
yapnr exp plan experiments/calibration-ladder.toml --backend gcp-batch --shape c4d-highcpu-8 --region us-west4
yapnr exp plan experiments/calibration-ladder.toml --backend gcp-batch --shape c3d-highcpu-8 --region northamerica-northeast1
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
`regions` and the tfvars before calibrating them, and compare C4A (arm64) boards only with other
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

- whether a Batch instance policy accepts a `hyperdisk-balanced` boot disk for C4D (the smoke job
  tries both; templates are the default);
- the gcsfuse mount options for the container (the default runs the container as root and mounts
  with `--implicit-dirs`);
- the exact preemptible quota ids, and whether restoring a cut quota needs a new approval;
- the Billing Catalog SKU descriptions `prices --refresh` parses (`Spot Preemptible <FAMILY> ...
Instance Core|Ram running in ...`);
- the instance templates' Hyperdisk settings and the Batch image family at apply time;
- region fallback after a stockout is manual: cancel and plan again with `--region`.

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
