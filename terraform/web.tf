# ── the web backend: Cloud Run behind Identity-Aware Proxy ───────────────────
# The ONLY public surface. Google sign-in (IAP) decides who may open it; the app verifies IAP's signed header again and talks to the engine
# with its own service account. Same thing `deploy/deploy.sh web` builds. The image is built by `deploy/deploy.sh web-build` (not by Terraform).
# Set web_enabled = false to leave the web backend out.

locals {
  web_service = "${local.name}-web"
  web_image   = "${local.region}-docker.pkg.dev/${local.project_id}/${google_artifact_registry_repository.repo.repository_id}/${local.name}-web:${var.image_tag}"
  # who may open it: the same list the engine's policy uses, unless overridden
  policy        = yamldecode(file("${path.module}/../config/policy.yaml"))
  web_users     = var.web_users != null ? var.web_users : [for u in try(local.policy.allowed_users, []) : lower(trimspace(u))]
  web_user_ids  = toset([for u in local.web_users : endswith(u, ".gserviceaccount.com") ? "serviceAccount:${u}" : "user:${u}"])
  web_instances = { min = coalesce(var.web_min_instances, local.cfg.web_min_instances), max = coalesce(var.web_max_instances, local.cfg.web_max_instances) }
}

resource "google_project_service" "web_api" {
  for_each           = var.web_enabled ? toset(["run", "iap"]) : toset([])
  service            = "${each.key}.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "web" {
  count        = var.web_enabled ? 1 : 0
  account_id   = local.web_service
  display_name = "${local.name} web"
}

# it may call the engine (aiplatform.user), nothing else
resource "google_project_iam_member" "web_calls_engine" {
  count   = var.web_enabled ? 1 : 0
  project = local.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.web[0].email}"
}

# the IAP service agent (created on demand) is the one that calls Cloud Run after a user passed sign-in
resource "google_project_service_identity" "iap" {
  count      = var.web_enabled ? 1 : 0
  provider   = google-beta
  service    = "iap.googleapis.com"
  depends_on = [google_project_service.web_api]
}

resource "google_cloud_run_v2_service" "web" {
  count               = var.web_enabled ? 1 : 0
  name                = local.web_service
  location            = local.region
  iap_enabled         = true # direct IAP on Cloud Run; needs the BETA launch stage
  launch_stage        = "BETA"
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.web[0].email
    timeout         = "3600s" # server-sent events stay open; Cloud Run's limit, not ours (a 331 s connection was proven through IAP)
    scaling {
      min_instance_count = local.web_instances.min
      max_instance_count = local.web_instances.max
    }
    containers {
      image = local.web_image
      env {
        name  = "WEB_AUTH"
        value = "iap"
      }
      env {
        name  = "IAP_AUDIENCE"
        value = "/projects/${local.project_number}/locations/${local.region}/services/${local.web_service}"
      }
      env {
        name  = "RUNTIME_RESOURCE"
        value = google_vertex_ai_reasoning_engine.engine.id
      }
      env {
        name  = "RUNTIME_REGION"
        value = local.region
      }
    }
  }

  depends_on = [google_project_service.web_api, google_project_iam_member.web_calls_engine]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invokes" {
  count      = var.web_enabled ? 1 : 0
  project    = local.project_id
  location   = local.region
  name       = google_cloud_run_v2_service.web[0].name
  role       = "roles/run.invoker"
  member     = "serviceAccount:service-${local.project_number}@gcp-sa-iap.iam.gserviceaccount.com"
  depends_on = [google_project_service_identity.iap]
}

resource "google_iap_web_cloud_run_service_iam_member" "users" {
  for_each               = var.web_enabled ? local.web_user_ids : toset([])
  project                = local.project_id
  location               = local.region
  cloud_run_service_name = google_cloud_run_v2_service.web[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}
