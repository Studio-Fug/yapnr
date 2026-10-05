# infra/gcp: experiment infrastructure on Google Cloud

The OpenTofu root module behind `yapnr exp --backend gcp-batch`
([guide](../../docs/cloud-experiments.md), [design](../../docs/design/cloud-experiments.md)).
OpenTofu (MPL-2.0) is the tool it is tested with; the HCL stays Terraform-compatible. Every
owner-specific value comes from a tfvars file outside the repository
([example](examples/owner.tfvars.example)); the state lives in a versioned bucket the owner creates
first.

## What it creates

| Module      | Resources                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `services`  | The project's APIs (Batch, Compute Engine, Cloud Storage, Artifact Registry, IAM, Cloud Quotas, Billing Budgets, Pub/Sub, Cloud Run functions and their build chain, Cloud Scheduler, Secret Manager, Logging). Destroy leaves them enabled.                                                                                                                                                                                                                                                                                                          |
| `network`   | VPC `yapnr` with one subnet per region (`yapnr-<region>`, Private Google Access), no ingress rules and no NAT: VMs have no external IP and reach Google APIs privately.                                                                                                                                                                                                                                                                                                                                                                               |
| `storage`   | The inputs bucket (soft delete kept, nothing expires) and the runs bucket (no soft delete; `result.tar.gz` deleted after 90 days, `checkpoints/` after 14, incomplete uploads after 1), both private with uniform access, never force-destroyed; bucket grants (tasks cannot write `control/`).                                                                                                                                                                                                                                                       |
| `identity`  | `yapnr-submit` (impersonated by the owner's Mac identity), `yapnr-runner` (the jobs' account), `yapnr-guard` (kill switch and reaper) `yapnr-fn-build` (builds the functions) and `yapnr-image-build` (Cloud Build of task images: writes to `images`, reads the build context under `cloudbuild/` in the inputs bucket, writes logs), with the grants of design section 9, and a custom role that lets `yapnr-guard` disable (only) `yapnr-submit`. No keys.                                                                                         |
| `registry`  | An Artifact Registry remote repository `ghcr` per region, upstream `https://ghcr.io`: a pull-through cache of the yapnr images. With `ghcr_token_secret`, it authenticates upstream while the packages are private. A standard repository `images` per region for task images that are not on GHCR (third-party solvers such as [openEMS](../../docker/openems/README.md)): the submit and runner accounts read, `yapnr-image-build` writes, and a cleanup policy keeps the five newest versions of an image and deletes untagged ones after 30 days. |
| `templates` | Global Spot instance templates `yapnr-<shape>-spot-<region>` for Hyperdisk-only families (`template_shapes` in every region, or a region's own list in `region_template_shapes`): Batch's Container-Optimized OS image, a 30 GiB `hyperdisk-balanced` boot disk at the free baseline (3,000 IOPS, 140 MiB/s), no external IP, the runner account, `STOP` on preemption (managed instance groups refuse `DELETE`; Batch deletes its VMs), optional `threads_per_core`.                                                                                 |
| `budget`    | A monthly budget on the project, gross of credits (alerts at 50, 90 and 100% of actual spend, 100% of forecast) publishing to the Pub/Sub topic `yapnr-budget`.                                                                                                                                                                                                                                                                                                                                                                                       |
| `guard`     | Two Cloud Run functions from [`functions/guard`](functions/guard/main.py): `yapnr-budget` (at 100% of the budget: disable `yapnr-submit`, write `control/frozen` and cancel every yapnr job; at `quota_cut_at`: set the preemptible CPU quota preferences to 0) and `yapnr-reaper` (every 15 minutes: cancel jobs past their deadline label, delete VMs an hour past it).                                                                                                                                                                             |

`tofu plan` lists 80 resources for the [example](examples/owner.tfvars.example)'s two regions and
five templates.

**Idle cost** (nothing running): the buckets' stored bytes, the cached images in Artifact Registry
(about $0.10 per GiB-month after the first 0.5 GiB: every digest a job pulled stays cached in its
region until deleted, so delete old digests with `gcloud artifacts docker images delete` after
image updates), and nothing else that bills by the hour: functions, Pub/Sub, Scheduler (three free
jobs per billing account) and Logging stay within their free tiers at this volume. No VM, NAT or
IP exists between campaigns.

## Regions and template shapes

Every region in `regions` gets a subnet, a registry cache, an `images` repository and the
templates of `template_shapes`, unless `region_template_shapes` gives the region a list of its own.
A region that does not offer a family needs its own list, or the apply fails on its templates;
check what a region offers first:

```sh
gcloud compute machine-types list --filter="zone~^$REGION- AND name~'^(c4|c4d)-highcpu-(8|16)$'" \
  --format="table(name,zone)"
```

```hcl
regions         = ["us-west4", "northamerica-northeast1"]
template_shapes = ["c4d-highcpu-16", "c4d-standard-16", "c4d-highcpu-8"]   # us-west4
region_template_shapes = {
  "northamerica-northeast1" = ["c4-highcpu-16", "c4-highcpu-8"]          # no C4D there
}
```

Template names do not depend on which list a shape comes from, so adding a region or giving one
its own list only adds templates; a shape dropped from a region's list is destroyed. `yapnr exp`
places a class on the first `[gcp] ranking` pair whose region has Spot quota for one more VM
([guide](../../docs/cloud-experiments.md#regions-and-quota-aware-placement)), so each ranked
(family, region) pair needs templates for the shapes its classes use (`yapnr exp doctor` lists
them). Adding a region also needs its preemptible CPU quota (below) and, for the budget guard,
an entry in `quota_preferences`.

## Quotas are not managed here

The budget guard lowers the preemptible CPU quota when spending runs away; a later `tofu apply`
must not silently raise it again. Quotas are requested with the runbook's commands
([guide, step 4](../../docs/cloud-experiments.md#4-quotas)) or in the console. List them in
`quota_preferences` (region to quota id) so the guard knows them. The guard updates the existing
preference for that quota and region, whatever its id (a console request gets a generated one),
and creates `yapnr-preemptible-cpus-<region>` only when none exists.

## Apply and destroy

```sh
cd infra/gcp
tofu init -backend-config="bucket=$STATE"
tofu plan -var-file="$HOME/.config/yapnr/gcp.tfvars" -out=yapnr.plan
tofu apply yapnr.plan
tofu output -json owner_config   # the [gcp] section of ~/.config/yapnr/cloud.toml
```

The account that applies needs Owner on the project and budget rights on the billing account
(Billing Account Administrator or Billing Account Costs Manager); it uses Application Default
Credentials (`gcloud auth application-default login`). Teardown: cancel all jobs, empty the buckets
on purpose (they refuse to be destroyed while they hold objects), then `tofu destroy`.

## Checks (no credentials, no cloud calls)

```sh
python3 infra/gcp/tofu_check.py      # or: bazel test //infra/gcp:tofu_check --test_env=TOFU=...
```

It runs `tofu fmt -check`, `tofu validate`, `tofu test` (the [tests](tests/plan.tftest.hcl) plan
the whole module against mocked providers) and `tofu plan` against the documentation project of the
example tfvars with a placeholder token, `-refresh=false` and all HTTP(S) traffic sent to a closed
local port, so a provider that tried to call an API would fail the check. `tofu init` downloads the
pinned providers from the OpenTofu registry ([lock file](.terraform.lock.hcl)). The guard functions
have their own unit tests (`bazel test //infra/gcp/functions/guard:all`).

Not verified without a project: whether the provider accepts every value at apply time (quota ids,
Hyperdisk settings on a template, the Batch image family), which the runbook's first apply and smoke
job settle.
