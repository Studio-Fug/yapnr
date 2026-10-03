# Global Spot instance templates for Hyperdisk-only families (C4D, C4, C4A, N4), one per shape and
# region, because a template names a regional subnetwork. Batch accepts only global templates and
# uses the template's machine type, boot disk, network and scheduling. The boot disk is
# hyperdisk-balanced pinned to the free baseline (3,000 IOPS, 140 MiB/s); the image is Batch's
# Container-Optimized OS, which Batch supports without external access.

variable "project_id" {
  type = string
}

variable "shapes" {
  type = list(string)
}

variable "subnetworks" {
  description = "Subnetwork self links by region."
  type        = map(string)
}

variable "runner_email" {
  type = string
}

variable "boot_disk_gb" {
  type = number
}

variable "threads_per_core" {
  type    = number
  default = null
}

locals {
  templates = {
    for pair in setproduct(var.shapes, keys(var.subnetworks)) :
    "yapnr-${pair[0]}-spot-${pair[1]}" => { shape = pair[0], region = pair[1] }
  }
}

resource "google_compute_instance_template" "spot" {
  for_each     = local.templates
  project      = var.project_id
  name         = each.key
  machine_type = each.value.shape
  description  = "yapnr exp: Spot ${each.value.shape} in ${each.value.region}"
  labels       = { yapnr = "1" }

  disk {
    boot                   = true
    auto_delete            = true
    source_image           = "projects/batch-custom-image/global/images/family/batch-cos-stable-official"
    disk_type              = "hyperdisk-balanced"
    disk_size_gb           = var.boot_disk_gb
    provisioned_iops       = 3000
    provisioned_throughput = 140
  }

  # No access_config: no external IP. Private Google Access on the subnet reaches Google APIs.
  network_interface {
    subnetwork = var.subnetworks[each.value.region]
  }

  scheduling {
    provisioning_model          = "SPOT"
    preemptible                 = true
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    # Batch runs templates through managed instance groups, which refuse Spot VMs whose termination
    # action is DELETE (CODE_GCE_UNSUPPORTED_OPERATION); Batch deletes its VMs when the job ends.
    instance_termination_action = "STOP"
  }

  service_account {
    email  = var.runner_email
    scopes = ["cloud-platform"]
  }

  metadata = {
    block-project-ssh-keys = "true"
  }

  dynamic "advanced_machine_features" {
    for_each = var.threads_per_core == null ? [] : [var.threads_per_core]
    content {
      threads_per_core = advanced_machine_features.value
    }
  }

  lifecycle {
    create_before_destroy = false
  }
}

output "names" {
  value = { for name, t in local.templates : name => t }
}
