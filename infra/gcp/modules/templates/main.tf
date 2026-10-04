# Global Spot instance templates for Hyperdisk-only families (C4D, C4, C4A, N4), one per shape and
# region, because a template names a regional subnetwork. Batch accepts only global templates and
# uses the template's machine type, boot disk, network and scheduling. The boot disk is
# hyperdisk-balanced pinned to the free baseline (3,000 IOPS, 140 MiB/s); the image is Batch's
# Container-Optimized OS, which Batch supports without external access.
#
# Every region gets `shapes`, unless `region_shapes` lists its own (a region that does not offer a
# family, such as C4D outside the regions that have it, gets the shapes it does offer instead).

variable "project_id" {
  type = string
}

variable "shapes" {
  description = "Machine types templated in every region without its own entry in region_shapes."
  type        = list(string)
}

variable "region_shapes" {
  description = "Machine types by region, replacing shapes for the regions listed."
  type        = map(list(string))
  default     = {}
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
  # The names do not depend on which list a shape came from, so moving a region to its own list
  # keeps the templates both lists name.
  templates = merge([
    for region in keys(var.subnetworks) : {
      for shape in lookup(var.region_shapes, region, var.shapes) :
      "yapnr-${shape}-spot-${region}" => { shape = shape, region = region }
    }
  ]...)
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
    provisioning_model  = "SPOT"
    preemptible         = true
    automatic_restart   = false
    on_host_maintenance = "TERMINATE"
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
