# State lives in a versioned bucket the owner creates during the bootstrap (runbook step 3). Its
# name never enters the repository: `tofu init -backend-config="bucket=<state bucket>"`.
terraform {
  backend "gcs" {
    prefix = "yapnr/infra"
  }
}
