# A dedicated VPC: one subnet per region with Private Google Access (Batch, Cloud Storage and
# Artifact Registry without external IPs or NAT), and no ingress rules (ingress is denied by
# default). The project's `default` network, which allows SSH from anywhere, is not used.

variable "project_id" {
  type = string
}

variable "regions" {
  type = list(string)
}

variable "subnet_base" {
  type = string
}

resource "google_compute_network" "yapnr" {
  project                 = var.project_id
  name                    = "yapnr"
  auto_create_subnetworks = false
  description             = "yapnr experiment VMs (Batch); no ingress rules"
}

resource "google_compute_subnetwork" "region" {
  for_each                 = { for i, r in var.regions : r => i }
  project                  = var.project_id
  name                     = "yapnr-${each.key}"
  region                   = each.key
  network                  = google_compute_network.yapnr.id
  ip_cidr_range            = cidrsubnet(var.subnet_base, 8, each.value)
  private_ip_google_access = true
}

output "network" {
  value = google_compute_network.yapnr.id
}

output "subnetworks" {
  description = "Subnetwork self links by region."
  value       = { for r, s in google_compute_subnetwork.region : r => s.self_link }
}
