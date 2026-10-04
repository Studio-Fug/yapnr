# Every value comes from the owner's tfvars file outside the repository
# (examples/owner.tfvars.example lists them with documentation values).

variable "project_id" {
  description = "The project that runs the experiments."
  type        = string
}

variable "project_number" {
  description = "The project's number (`gcloud projects describe <project> --format='value(projectNumber)'`)."
  type        = string
}

variable "home_region" {
  description = "Where the buckets, the budget guard and the scheduler live."
  type        = string
}

variable "regions" {
  description = "Regions jobs may run in: one subnet, one registry cache and the templates each."
  type        = list(string)

  validation {
    condition     = length(var.regions) > 0
    error_message = "List at least one region."
  }
}

variable "inputs_bucket" {
  description = "Bucket for source and private input bundles (soft delete kept, nothing expires)."
  type        = string
}

variable "runs_bucket" {
  description = "Bucket for campaigns, results, checkpoints and control markers."
  type        = string
}

variable "mac_identity" {
  description = "The IAM member that may impersonate yapnr-submit, e.g. user:someone@example.com."
  type        = string
}

variable "billing_account" {
  description = "The billing account id (XXXXXX-XXXXXX-XXXXXX) the budget is created on."
  type        = string
}

variable "budget_usd" {
  description = "The monthly budget in USD, gross of credits (credits do not raise it); the guard cancels jobs at 100% and cuts quota at quota_cut_at."
  type        = number
  default     = 50
}

variable "quota_cut_at" {
  description = "Budget fraction at which the guard sets the preemptible CPU quota to 0."
  type        = number
  default     = 1.2
}

variable "quota_preferences" {
  description = "Quota preferences the guard may cut, as region => quota id (runbook step 4)."
  type        = map(string)
  default     = {}
}

variable "template_shapes" {
  description = "Machine types that get Spot instance templates in every region without an entry in region_template_shapes (Hyperdisk families); c4d-highcpu-8 is the calibration's shape."
  type        = list(string)
  default     = ["c4d-highcpu-16", "c4d-standard-16", "c4d-highcpu-8"]
}

variable "region_template_shapes" {
  description = "Per-region template shapes, replacing template_shapes in the regions listed (a region that lacks a family, e.g. C4D, gets the shapes it offers); {} keeps template_shapes everywhere."
  type        = map(list(string))
  default     = {}

  validation {
    condition     = alltrue([for region in keys(var.region_template_shapes) : contains(var.regions, region)])
    error_message = "Every region in region_template_shapes must be one of regions."
  }

  validation {
    condition     = alltrue([for shapes in values(var.region_template_shapes) : length(shapes) > 0])
    error_message = "List at least one shape per region (leave the region out to use template_shapes)."
  }
}

variable "boot_disk_gb" {
  description = "Boot disk size of templated VMs (hyperdisk-balanced at the free baseline)."
  type        = number
  default     = 30
}

variable "threads_per_core" {
  description = "1 disables SMT on templated VMs (the calibration's packing experiment); null keeps it."
  type        = number
  default     = null
}

variable "subnet_base" {
  description = "Private range the per-region subnets are cut from (/24 each)."
  type        = string
  default     = "10.64.0.0/16"
}

variable "result_retention_days" {
  description = "Days before result archives (result.tar.gz) are deleted from the runs bucket."
  type        = number
  default     = 90
}

variable "checkpoint_retention_days" {
  description = "Days before checkpoints/ objects are deleted from the runs bucket."
  type        = number
  default     = 14
}

variable "ghcr_token_secret" {
  description = "Secret Manager secret version holding a read:packages token, while GHCR is private; null when public."
  type        = string
  default     = null
}

variable "ghcr_username" {
  description = "GitHub user name that goes with ghcr_token_secret."
  type        = string
  default     = null
}

variable "reaper_schedule" {
  description = "Cron schedule of the reaper (cancels jobs past their deadline label)."
  type        = string
  default     = "*/15 * * * *"
}
