terraform {
  required_version = ">= 1.5"
  required_providers {
    google      = { source = "hashicorp/google", version = ">= 6.0" }
    google-beta = { source = "hashicorp/google-beta", version = ">= 6.0" } # only for the IAP service agent
    random      = { source = "hashicorp/random", version = ">= 3.6" }
  }
  # The state holds the generated database password and API token. Keep it somewhere private; for more than one person use a bucket:
  #   backend "gcs" { bucket = "<a private state bucket>", prefix = "agent" }
}

provider "google" {
  project = local.project_id
  region  = local.region
}

provider "google-beta" {
  project = local.project_id
  region  = local.region
}
