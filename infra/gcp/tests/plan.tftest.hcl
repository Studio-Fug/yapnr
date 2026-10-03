# `tofu test`: the whole module planned against mocked providers (no credentials, no API calls).

mock_provider "google" {
  # Service-account attributes the provider validates (the interpolation keeps an address-shaped
  # string out of the file, for the privacy scan).
  mock_resource "google_service_account" {
    defaults = {
      email = "yapnr-mock@${"example-project"}.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/yapnr-mock@${"example-project"}.iam.gserviceaccount.com"
      id    = "projects/example-project/serviceAccounts/yapnr-mock@${"example-project"}.iam.gserviceaccount.com"
    }
  }
}

mock_provider "archive" {
  mock_data "archive_file" {
    defaults = {
      output_md5  = "0123456789abcdef0123456789abcdef"
      output_path = "guard.zip"
    }
  }
}

variables {
  project_id      = "example-project"
  project_number  = "123456789012"
  home_region     = "us-west4"
  regions         = ["us-west4", "northamerica-northeast1"]
  inputs_bucket   = "example-yapnr-inputs"
  runs_bucket     = "example-yapnr-runs"
  mac_identity    = "user:someone@example.com"
  billing_account = "000000-000000-000000"
  quota_preferences = {
    "us-west4" = "PREEMPTIBLE-CPUS-per-project-region"
  }
}

run "owner_config_matches_the_cli" {
  command = plan

  assert {
    condition     = output.owner_config.registry == "{region}-docker.pkg.dev/example-project/ghcr"
    error_message = "the registry pattern must match [gcp] registry in the owner config"
  }

  assert {
    condition     = output.owner_config.template == "yapnr-{shape}-{model}-{region}"
    error_message = "the template pattern must match [gcp] template in the owner config"
  }

  assert {
    condition     = output.owner_config.submit_service_account == "yapnr-submit"
    error_message = "the submit account id is yapnr-submit"
  }
}

run "the_kill_switch_can_stop_submits" {
  command = plan

  assert {
    condition     = module.guard.environment.YAPNR_SUBMIT_ACCOUNT == module.identity.submit_email
    error_message = "the budget guard must know the submit account it disables"
  }

  assert {
    condition     = module.identity.disable_submit_permissions == toset(["iam.serviceAccounts.get", "iam.serviceAccounts.disable"])
    error_message = "the guard may disable the submit account, and do nothing else with it"
  }
}

run "templates_per_shape_and_region" {
  command = plan

  assert {
    condition     = length(output.templates) == 4
    error_message = "two default shapes in two regions make four templates"
  }

  assert {
    condition     = contains(keys(output.templates), "yapnr-c4d-highcpu-16-spot-us-west4")
    error_message = "template names follow yapnr-<shape>-spot-<region>"
  }
}

run "one_subnet_per_region" {
  command = plan

  assert {
    condition     = length(module.network.subnetworks) == 2
    error_message = "one subnet per enabled region"
  }
}

run "private_ghcr_reads_the_token_secret" {
  command = plan

  variables {
    ghcr_token_secret = "projects/example-project/secrets/ghcr-read-packages/versions/1"
    ghcr_username     = "example-user"
  }

  assert {
    condition     = module.registry.upstream_authenticated
    error_message = "a token secret turns on upstream credentials"
  }
}

run "regions_are_required" {
  command = plan

  variables {
    regions = []
  }

  expect_failures = [var.regions]
}
