data "google_project" "this" {}

locals {
  project_number = data.google_project.this.number
  runtime_sa     = google_service_account.runtime.email
  image          = "${local.region}-docker.pkg.dev/${local.project_id}/${google_artifact_registry_repository.repo.repository_id}/${local.name}:${var.image_tag}"
  apis = [
    "aiplatform", "artifactregistry", "cloudbuild", "secretmanager", "sqladmin",
    "iam", "cloudresourcemanager", "cloudscheduler", "storage",
  ]
}

resource "google_project_service" "api" {
  for_each           = toset(local.apis)
  service            = "${each.key}.googleapis.com"
  disable_on_destroy = false
}

# ── images ───────────────────────────────────────────────────────────────
resource "google_artifact_registry_repository" "repo" {
  repository_id = "${local.name}-repo"
  location      = local.region
  format        = "DOCKER"
  depends_on    = [google_project_service.api]
}

# ── the identity the runtime runs as ─────────────────────────────────────
resource "google_service_account" "runtime" {
  account_id   = "${local.name}-runtime"
  display_name = "${local.name} runtime"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "runtime" {
  for_each = toset(["roles/aiplatform.user", "roles/secretmanager.secretAccessor", "roles/cloudsql.client", "roles/logging.logWriter", "roles/serviceusage.serviceUsageConsumer"])
  project  = local.project_id
  role     = each.key
  member   = "serviceAccount:${local.runtime_sa}"
}

resource "google_storage_bucket" "files" {
  count                       = local.create_bucket ? 1 : 0
  name                        = local.artifacts_bucket
  location                    = local.region
  uniform_bucket_level_access = true
  depends_on                  = [google_project_service.api]
}

resource "google_storage_bucket_iam_member" "runtime_artifacts" {
  bucket     = local.artifacts_bucket
  role       = "roles/storage.objectAdmin"
  member     = "serviceAccount:${local.runtime_sa}"
  depends_on = [google_storage_bucket.files]
}

# signed download URLs: the runtime signs as itself
resource "google_service_account_iam_member" "runtime_signs_urls" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${local.runtime_sa}"
}

# Google's Agent Runtime service agents must be allowed to pull the image (without this: IMAGE_PULL_BACKOFF)
resource "google_project_iam_member" "service_agent_pull" {
  for_each = toset(["gcp-sa-aiplatform-re", "gcp-sa-aiplatform"])
  project  = local.project_id
  role     = "roles/artifactregistry.reader"
  member   = "serviceAccount:service-${local.project_number}@${each.key}.iam.gserviceaccount.com"
}

# Cloud Build runs as the Compute default service account in new projects
resource "google_project_iam_member" "cloud_build" {
  project = local.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${local.project_number}-compute@developer.gserviceaccount.com"
}

# ── database ─────────────────────────────────────────────────────────────
resource "google_sql_database_instance" "pg" {
  name                = "${local.name}-pg"
  database_version    = "POSTGRES_16"
  region              = local.region
  deletion_protection = false # a learning project; set true for anything real

  settings {
    tier              = local.sql_tier
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    backup_configuration { enabled = false }
  }
  depends_on = [google_project_service.api]
}

resource "google_sql_database" "main" {
  name     = local.sql_database
  instance = google_sql_database_instance.pg.name
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "main" {
  name     = local.sql_user
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# ── secrets ──────────────────────────────────────────────────────────────
# Values that Terraform generates: the database URL (through the Cloud SQL Auth Proxy in the container) and the API token.
resource "random_password" "api_token" {
  length  = 48
  special = false
}

# The key that encrypts the tokens users paste for their MCP servers: a Fernet key = URL-safe base64 of 32 random bytes, with its padding.
resource "random_id" "mcp_key" {
  byte_length = 32
}

resource "google_secret_manager_secret" "generated" {
  for_each  = toset([local.db_url_secret, local.api_token_secret, "MCP_ENCRYPTION_KEY"])
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.api]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.generated[local.db_url_secret].id
  secret_data = "postgresql://${local.sql_user}:${random_password.db.result}@127.0.0.1:5432/${local.sql_database}?sslmode=disable"
}

resource "google_secret_manager_secret_version" "api_token" {
  secret      = google_secret_manager_secret.generated[local.api_token_secret].id
  secret_data = random_password.api_token.result
}

resource "google_secret_manager_secret_version" "mcp_key" {
  secret      = google_secret_manager_secret.generated["MCP_ENCRYPTION_KEY"].id
  secret_data = "${random_id.mcp_key.b64_url}="
}

# Secrets YOU provide: Terraform creates the empty container only, so no key ever enters the state.
#   gcloud secrets versions add TAVILY_API_KEY --data-file=-        (and the same for the others)
resource "google_secret_manager_secret" "provided" {
  for_each  = local.secrets_provided
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.api]
}

# ── the Agent Runtime ────────────────────────────────────────────────────
locals {
  class_methods = jsondecode(file("${path.module}/class_methods.json"))
  env = merge(
    {
      GCP_PROJECT         = local.project_id
      GCP_LOCATION        = local.region
      MODEL               = var.model
      GOOGLE_USE_VERTEX   = "true"
      SECRETS_BACKEND     = "gcp"
      STATE_BACKEND       = "postgres"
      SQL_CONNECTION      = google_sql_database_instance.pg.connection_name
      DATABASE_URL        = "secret://${local.db_url_secret}"
      PINBOARD_API_TOKEN  = "secret://${local.api_token_secret}"
      TRUST_PLATFORM_AUTH = "true"
      APPROVAL_MODE       = "execute"
      ARTIFACTS_URI       = "gs://${local.artifacts_bucket}/${local.name}"
      SANDBOX_TTL_SECONDS = "3600"
      PINBOARD_USER_ID    = "owner"
      # The engine hides these secrets' values in its output. Terraform cannot read .env, so it lists the names from the config file only.
      EXTRA_SECRET_NAMES = join(",", sort(tolist(local.secrets_provided)))
    },
    var.sandbox_runtime == "" ? {} : { SANDBOX_RUNTIME = var.sandbox_runtime },
    var.github_app_id == "" ? {} : { GITHUB_APP_ID = var.github_app_id },
    var.agentmail_inbox_id == "" ? {} : { AGENTMAIL_INBOX_ID = var.agentmail_inbox_id },
    var.extra_env, # any other setting of the agent, e.g. AGENT_BUILDER = "my_package.agent:build"
  )
}

resource "google_vertex_ai_reasoning_engine" "engine" {
  display_name = local.name
  description  = "${local.name} agent"
  region       = local.region

  spec {
    agent_framework = "langgraph" # worked with the Python SDK; the Terraform docs list only langchain / google-adk (UNVERIFIED here)
    service_account = local.runtime_sa

    container_spec {
      image_uri = local.image
    }

    deployment_spec {
      min_instances = local.min_instances
      max_instances = local.max_instances
      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }
    }

    class_methods = jsonencode(local.class_methods)
  }

  depends_on = [
    google_project_iam_member.service_agent_pull,
    google_project_iam_member.runtime,
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_version.api_token,
    google_sql_database.main,
    google_sql_user.main,
  ]
}

# ── housekeeping: Cloud Scheduler calls the `expire` method every few minutes ─
resource "google_service_account" "scheduler" {
  account_id   = "${local.name}-scheduler"
  display_name = "${local.name} scheduler"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "scheduler" {
  project = local.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "housekeeping" {
  name      = "${local.name}-housekeeping"
  region    = local.region
  schedule  = local.schedule
  time_zone = "UTC"

  http_target {
    http_method = "POST"
    uri         = "https://${local.region}-aiplatform.googleapis.com/v1/${google_vertex_ai_reasoning_engine.engine.id}:query"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ classMethod = "expire", input = {} }))
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_project_service.api, google_project_iam_member.scheduler]
}
