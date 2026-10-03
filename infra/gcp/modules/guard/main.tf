# Two Cloud Run functions (2nd gen), source in functions/guard (standard library only):
#   budget_guard  on every budget notification: at 100% of actual spend, disable yapnr-submit,
#                 write control/frozen and cancel every job labelled yapnr=1; at quota_cut_at, also
#                 set the preemptible CPU quota preferences to 0. Idempotent: notifications repeat
#                 several times a day.
#   reaper        every 15 minutes (Cloud Scheduler -> Pub/Sub): cancel jobs past their deadline
#                 label, and delete yapnr VMs that outlive their deadline by an hour.

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "regions" {
  type = list(string)
}

variable "runs_bucket" {
  type = string
}

variable "guard_email" {
  type = string
}

variable "submit_email" {
  description = "The account every submit runs as; the guard disables it at 100% of the budget."
  type        = string
}

variable "build_service_account" {
  type = string
}

variable "budget_topic" {
  type = string
}

variable "quota_preferences" {
  type = map(string)
}

variable "quota_cut_at" {
  type = number
}

variable "reaper_schedule" {
  type = string
}

variable "source_dir" {
  type = string
}

data "archive_file" "source" {
  type        = "zip"
  output_path = "${path.root}/.build/guard.zip"

  source {
    content  = file("${var.source_dir}/main.py")
    filename = "main.py"
  }

  source {
    content  = file("${var.source_dir}/requirements.txt")
    filename = "requirements.txt"
  }
}

resource "google_storage_bucket_object" "source" {
  bucket = var.runs_bucket
  name   = "functions/guard-${data.archive_file.source.output_md5}.zip"
  source = data.archive_file.source.output_path
}

resource "google_pubsub_topic" "tick" {
  project = var.project_id
  name    = "yapnr-reaper-tick"
  labels  = { yapnr = "1" }
}

resource "google_cloud_scheduler_job" "reaper" {
  project  = var.project_id
  region   = var.region
  name     = "yapnr-reaper"
  schedule = var.reaper_schedule

  pubsub_target {
    topic_name = google_pubsub_topic.tick.id
    data       = base64encode("{\"reap\": true}")
  }
}

locals {
  environment = {
    YAPNR_PROJECT           = var.project_id
    YAPNR_REGIONS           = join(",", var.regions)
    YAPNR_RUNS_BUCKET       = var.runs_bucket
    YAPNR_QUOTA_PREFERENCES = join(",", [for region, quota in var.quota_preferences : "${region}=${quota}"])
    YAPNR_QUOTA_CUT_AT      = tostring(var.quota_cut_at)
    YAPNR_SUBMIT_ACCOUNT    = var.submit_email
  }
  functions = {
    budget = { entry = "budget_guard", topic = var.budget_topic }
    reaper = { entry = "reaper", topic = google_pubsub_topic.tick.id }
  }
}

resource "google_cloudfunctions2_function" "this" {
  for_each = local.functions
  project  = var.project_id
  location = var.region
  name     = "yapnr-${each.key}"
  labels   = { yapnr = "1" }

  build_config {
    runtime         = "python312"
    entry_point     = each.value.entry
    service_account = var.build_service_account

    source {
      storage_source {
        bucket = var.runs_bucket
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    available_memory      = "256M"
    timeout_seconds       = 300
    service_account_email = var.guard_email
    environment_variables = local.environment
  }

  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = each.value.topic
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = var.guard_email
  }
}

# The Eventarc trigger invokes the functions as yapnr-guard.
resource "google_cloud_run_service_iam_member" "invoker" {
  for_each = google_cloudfunctions2_function.this
  project  = var.project_id
  location = var.region
  service  = each.value.service_config[0].service
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.guard_email}"
}

output "environment" {
  value = local.environment
}

output "functions" {
  value = { for k, f in google_cloudfunctions2_function.this : k => f.name }
}
