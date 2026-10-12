# One source of truth for names and sizes: the same YAML files deploy.sh and deploy/runtime.py read.
# deploy/config.yaml holds the defaults; deploy/config.local.yaml (git-ignored, optional) overrides them key by key.
locals {
  cfg_defaults = yamldecode(file("${path.module}/../deploy/config.yaml"))
  cfg_local    = fileexists("${path.module}/../deploy/config.local.yaml") ? yamldecode(file("${path.module}/../deploy/config.local.yaml")) : {}
  cfg          = merge(local.cfg_defaults, local.cfg_local)

  name       = coalesce(var.name, local.cfg.name)
  project_id = coalesce(var.project_id, local.cfg.project) # an error here means: set `project` in the config or pass -var project_id=...
  region     = coalesce(var.region, local.cfg.region)

  create_bucket    = var.artifacts_bucket == null && local.cfg.artifacts_bucket == "" # nobody named an existing bucket: make one
  artifacts_bucket = coalesce(var.artifacts_bucket, local.cfg.artifacts_bucket, "${local.project_id}-${local.name}-files")
  sql_tier         = coalesce(var.sql_tier, local.cfg.sql_tier)
  sql_database     = coalesce(local.cfg.sql_database, local.name)
  sql_user         = coalesce(local.cfg.sql_user, local.name)
  schedule         = coalesce(var.housekeeping_schedule, local.cfg.schedule)
  min_instances    = coalesce(var.min_instances, local.cfg.min_instances)
  max_instances    = coalesce(var.max_instances, local.cfg.max_instances)

  db_url_secret    = "${local.name}-database-url"
  api_token_secret = "${local.name}-api-token"
  secrets_provided = toset(concat(local.cfg.secrets_from_dotenv, local.cfg.secrets_provided))
}
