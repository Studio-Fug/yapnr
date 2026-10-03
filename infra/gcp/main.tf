# yapnr experiment infrastructure on Google Cloud (docs/cloud-experiments.md, runbook step 3).
# Creates: the project's APIs, a VPC without ingress, two buckets, three service accounts and
# their grants, an Artifact Registry pull-through cache of ghcr.io per region, Spot instance
# templates for Hyperdisk families, a budget with a Pub/Sub topic, and the budget guard and
# reaper functions. Quotas are not managed here (the kill switch lowers them; an apply must not
# raise them back): see README.md.

provider "google" {
  project = var.project_id
  region  = var.home_region
  # The Billing Budgets API needs a quota project.
  user_project_override = true
  billing_project       = var.project_id
}

module "services" {
  source     = "./modules/services"
  project_id = var.project_id
}

module "network" {
  source      = "./modules/network"
  project_id  = var.project_id
  regions     = var.regions
  subnet_base = var.subnet_base
  depends_on  = [module.services]
}

module "identity" {
  source       = "./modules/identity"
  project_id   = var.project_id
  mac_identity = var.mac_identity
  depends_on   = [module.services]
}

module "storage" {
  source                    = "./modules/storage"
  project_id                = var.project_id
  location                  = var.home_region
  inputs_bucket             = var.inputs_bucket
  runs_bucket               = var.runs_bucket
  submit_email              = module.identity.submit_email
  runner_email              = module.identity.runner_email
  guard_email               = module.identity.guard_email
  result_retention_days     = var.result_retention_days
  checkpoint_retention_days = var.checkpoint_retention_days
  depends_on                = [module.services]
}

module "registry" {
  source            = "./modules/registry"
  project_id        = var.project_id
  project_number    = var.project_number
  regions           = var.regions
  readers           = { submit = module.identity.submit_email, runner = module.identity.runner_email }
  ghcr_token_secret = var.ghcr_token_secret
  ghcr_username     = var.ghcr_username
  depends_on        = [module.services]
}

module "templates" {
  source           = "./modules/templates"
  project_id       = var.project_id
  shapes           = var.template_shapes
  subnetworks      = module.network.subnetworks
  runner_email     = module.identity.runner_email
  boot_disk_gb     = var.boot_disk_gb
  threads_per_core = var.threads_per_core
  depends_on       = [module.services]
}

module "budget" {
  source          = "./modules/budget"
  project_id      = var.project_id
  project_number  = var.project_number
  billing_account = var.billing_account
  budget_usd      = var.budget_usd
  depends_on      = [module.services]
}

module "guard" {
  source                = "./modules/guard"
  project_id            = var.project_id
  region                = var.home_region
  regions               = var.regions
  runs_bucket           = module.storage.runs_bucket
  guard_email           = module.identity.guard_email
  submit_email          = module.identity.submit_email
  build_service_account = module.identity.build_id
  budget_topic          = module.budget.topic_id
  quota_preferences     = var.quota_preferences
  quota_cut_at          = var.quota_cut_at
  reaper_schedule       = var.reaper_schedule
  source_dir            = "${path.module}/functions/guard"
  depends_on            = [module.services]
}
