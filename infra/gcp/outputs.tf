# The values the owner config (~/.config/yapnr/cloud.toml) needs: `tofu output`.

output "owner_config" {
  description = "The [gcp] section for ~/.config/yapnr/cloud.toml."
  value = {
    project                     = var.project_id
    home_region                 = var.home_region
    regions                     = var.regions
    inputs_bucket               = module.storage.inputs_bucket
    runs_bucket                 = module.storage.runs_bucket
    submit_service_account      = module.identity.submit_account_id
    runner_service_account      = module.identity.runner_account_id
    registry                    = "{region}-docker.pkg.dev/${var.project_id}/ghcr"
    images                      = "{region}-docker.pkg.dev/${var.project_id}/images"
    image_build_service_account = module.identity.image_build_account_id
    subnetwork                  = "projects/${var.project_id}/regions/{region}/subnetworks/yapnr-{region}"
    template                    = "yapnr-{shape}-{model}-{region}"
  }
}

output "templates" {
  description = "Instance templates by shape and region."
  value       = module.templates.names
}

output "budget_topic" {
  description = "The Pub/Sub topic budget notifications go to (the kill-switch drill publishes here)."
  value       = module.budget.topic_id
}
