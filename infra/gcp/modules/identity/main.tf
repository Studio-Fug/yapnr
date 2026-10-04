# Three service accounts and their grants (docs/design/cloud-experiments.md, section 9). Nobody
# gets key files: the owner's Mac identity may only impersonate yapnr-submit, which may only run
# jobs as yapnr-runner (never as the Compute Engine default account, which has Editor).

variable "project_id" {
  type = string
}

variable "mac_identity" {
  type = string
}

resource "google_service_account" "submit" {
  project      = var.project_id
  account_id   = "yapnr-submit"
  display_name = "yapnr exp: plans, submits, watches and fetches campaigns"
}

resource "google_service_account" "runner" {
  project      = var.project_id
  account_id   = "yapnr-runner"
  display_name = "yapnr exp: Batch VMs and task containers"
}

resource "google_service_account" "guard" {
  project      = var.project_id
  account_id   = "yapnr-guard"
  display_name = "yapnr exp: budget kill switch and reaper"
}

resource "google_service_account" "build" {
  project      = var.project_id
  account_id   = "yapnr-fn-build"
  display_name = "yapnr exp: builds the guard functions"
}

resource "google_project_iam_member" "submit" {
  for_each = toset([
    "roles/batch.jobsEditor",
    "roles/logging.viewer",
    "roles/compute.viewer",
    "roles/cloudquotas.viewer",
    "roles/artifactregistry.reader",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.submit.email}"
}

resource "google_project_iam_member" "runner" {
  for_each = toset([
    "roles/batch.agentReporter",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_project_iam_member" "guard" {
  for_each = toset([
    "roles/batch.jobsEditor",
    "roles/cloudquotas.admin",
    "roles/compute.instanceAdmin.v1",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.guard.email}"
}

resource "google_project_iam_member" "build" {
  for_each = toset([
    "roles/cloudbuild.builds.builder",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.build.email}"
}

# yapnr-submit may launch jobs as yapnr-runner only.
resource "google_service_account_iam_member" "submit_acts_as_runner" {
  service_account_id = google_service_account.runner.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.submit.email}"
}

# The owner's day-to-day identity may impersonate yapnr-submit, and nothing more.
resource "google_service_account_iam_member" "mac_impersonates_submit" {
  service_account_id = google_service_account.submit.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = var.mac_identity
}

# The budget guard disables yapnr-submit at 100% of the budget, so no client can submit until the
# owner re-enables it. A custom role on that one account: Service Account Admin would also let the
# guard change the account's IAM policy and impersonate it.
resource "google_project_iam_custom_role" "disable_submit" {
  project     = var.project_id
  role_id     = "yapnrDisableSubmit"
  title       = "yapnr budget guard: disable the submit account"
  description = "Disable (not enable, not change) a service account; granted on yapnr-submit only."
  permissions = ["iam.serviceAccounts.get", "iam.serviceAccounts.disable"]
}

resource "google_service_account_iam_member" "guard_disables_submit" {
  service_account_id = google_service_account.submit.name
  role               = google_project_iam_custom_role.disable_submit.name
  member             = "serviceAccount:${google_service_account.guard.email}"
}

output "submit_email" {
  value = google_service_account.submit.email
}

output "runner_email" {
  value = google_service_account.runner.email
}

output "guard_email" {
  value = google_service_account.guard.email
}

output "build_id" {
  value = google_service_account.build.id
}

output "disable_submit_permissions" {
  value = toset(google_project_iam_custom_role.disable_submit.permissions)
}

output "submit_account_id" {
  value = google_service_account.submit.account_id
}

output "runner_account_id" {
  value = google_service_account.runner.account_id
}
