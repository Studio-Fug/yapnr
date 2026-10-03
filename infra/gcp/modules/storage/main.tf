# Two private buckets in the home region (docs/design/cloud-experiments.md, section 8):
#   inputs  source and private bundles: soft delete kept, nothing expires;
#   runs    campaigns, results, checkpoints, control/: lifecycle rules, no soft delete.
# Both refuse public access, use uniform bucket-level access and are never force-destroyed:
# `tofu destroy` fails while they hold objects, so results and private inputs go only on purpose.

variable "project_id" {
  type = string
}

variable "location" {
  type = string
}

variable "inputs_bucket" {
  type = string
}

variable "runs_bucket" {
  type = string
}

variable "submit_email" {
  type = string
}

variable "runner_email" {
  type = string
}

variable "guard_email" {
  type = string
}

variable "result_retention_days" {
  type = number
}

variable "checkpoint_retention_days" {
  type = number
}

resource "google_storage_bucket" "inputs" {
  project                     = var.project_id
  name                        = var.inputs_bucket
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = { yapnr = "1" }

  soft_delete_policy {
    retention_duration_seconds = 604800
  }
}

resource "google_storage_bucket" "runs" {
  project                     = var.project_id
  name                        = var.runs_bucket
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = { yapnr = "1" }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle_rule {
    condition {
      age            = var.result_retention_days
      matches_suffix = ["result.tar.gz"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age            = var.checkpoint_retention_days
      matches_prefix = ["checkpoints/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }
}

# The submitter uploads bundles and campaign files and reads results.
resource "google_storage_bucket_iam_member" "submit" {
  for_each = {
    inputs = google_storage_bucket.inputs.name
    runs   = google_storage_bucket.runs.name
  }
  bucket = each.value
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.submit_email}"
}

# Tasks read inputs and write their results.
resource "google_storage_bucket_iam_member" "runner_inputs" {
  bucket = google_storage_bucket.inputs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.runner_email}"
}

# Tasks read everything in the runs bucket (control/frozen included) and write anywhere but
# control/: a task must not be able to clear the kill switch's marker.
resource "google_storage_bucket_iam_member" "runner_runs_read" {
  bucket = google_storage_bucket.runs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.runner_email}"
}

resource "google_storage_bucket_iam_member" "runner_runs" {
  bucket = google_storage_bucket.runs.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.runner_email}"

  condition {
    title      = "not-control-prefix"
    expression = "!resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.runs.name}/objects/control/\")"
  }
}

# The guard writes control/frozen, and nothing else.
resource "google_storage_bucket_iam_member" "guard_control" {
  bucket = google_storage_bucket.runs.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.guard_email}"

  condition {
    title      = "control-prefix-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.runs.name}/objects/control/\")"
  }
}

output "inputs_bucket" {
  value = google_storage_bucket.inputs.name
}

output "runs_bucket" {
  value = google_storage_bucket.runs.name
}
