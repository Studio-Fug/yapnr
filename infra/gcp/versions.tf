# OpenTofu (MPL-2.0) is the tool this module is tested with; the HCL stays Terraform-compatible.
# Provider versions are pinned to a minor series, and .terraform.lock.hcl (committed) records the
# exact versions with the registry's checksums for every platform.
terraform {
  required_version = ">= 1.8.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }
}
