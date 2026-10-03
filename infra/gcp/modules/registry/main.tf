# An Artifact Registry remote repository per region with the custom upstream https://ghcr.io: a
# pull-through cache, so VMs pull the yapnr image without internet access and same-region pulls
# are free. While the GHCR packages are private, the repository authenticates upstream with a
# read:packages token from Secret Manager.

variable "project_id" {
  type = string
}

variable "project_number" {
  type = string
}

variable "regions" {
  type = list(string)
}

variable "readers" {
  description = "Service accounts that pull images, by a static name (for_each keys must be known at plan time)."
  type        = map(string)
}

variable "ghcr_token_secret" {
  type    = string
  default = null
}

variable "ghcr_username" {
  type    = string
  default = null
}

resource "google_artifact_registry_repository" "ghcr" {
  for_each      = toset(var.regions)
  project       = var.project_id
  location      = each.value
  repository_id = "ghcr"
  description   = "Pull-through cache of ghcr.io (yapnr images, pinned by digest)"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  labels        = { yapnr = "1" }

  remote_repository_config {
    description = "ghcr.io"

    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }

    dynamic "upstream_credentials" {
      for_each = var.ghcr_token_secret == null ? [] : [1]
      content {
        username_password_credentials {
          username                = var.ghcr_username
          password_secret_version = var.ghcr_token_secret
        }
      }
    }
  }
}

resource "google_artifact_registry_repository_iam_member" "reader" {
  for_each = {
    for pair in setproduct(var.regions, keys(var.readers)) : "${pair[0]}-${pair[1]}" => pair
  }
  project    = var.project_id
  location   = each.value[0]
  repository = google_artifact_registry_repository.ghcr[each.value[0]].repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.readers[each.value[1]]}"
}

# The Artifact Registry service agent reads the upstream token (private GHCR only).
resource "google_secret_manager_secret_iam_member" "upstream" {
  count     = var.ghcr_token_secret == null ? 0 : 1
  project   = var.project_id
  secret_id = regex("secrets/([^/]+)", var.ghcr_token_secret)[0]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:service-${var.project_number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"
}

output "repositories" {
  value = { for r, repo in google_artifact_registry_repository.ghcr : r => repo.id }
}

output "upstream_authenticated" {
  value = var.ghcr_token_secret != null
}
