# One source of truth for names and sizes: the same YAML files deploy.sh and deploy/runtime.py read.
# deploy/config.yaml holds the defaults; deploy/config.local.yaml (git-ignored, optional) overrides them key by key.
locals {
  cfg_defaults = yamldecode(file("${path.module}/../deploy/config.yaml"))
  # both branches are STRINGS (an object in one branch and {} in the other is "inconsistent" to Terraform); a malformed local file is still an error
  cfg_local = yamldecode(fileexists("${path.module}/../deploy/config.local.yaml") ? file("${path.module}/../deploy/config.local.yaml") : "{}")
  cfg       = merge(local.cfg_defaults, local.cfg_local)

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

  # Private networking and egress control (docs/EGRESS.md). All empty/false by default = nothing changes.
  network_attachment = local.cfg.network_attachment
  egress_proxy       = local.cfg.egress_proxy
  # keep identical to NO_PROXY in deploy/egress.py (a test compares the two)
  no_proxy_base = ".googleapis.com,googleapis.com,metadata.google.internal,localhost,127.0.0.1,169.254.0.0/16,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
  # private DNS zones: names under them live inside the VPC (each domain loses its trailing dot and is listed bare and as a suffix)
  no_proxy = join(",", concat([local.no_proxy_base], flatten([for d in local.network_dns_domains : [trimsuffix(d, "."), ".${trimsuffix(d, ".")}"]])))
  proxy_env = local.egress_proxy == "" ? {} : {
    HTTPS_PROXY = local.egress_proxy
    HTTP_PROXY  = local.egress_proxy
    NO_PROXY    = local.no_proxy
    https_proxy = local.egress_proxy
    http_proxy  = local.egress_proxy
    no_proxy    = local.no_proxy
  }
  network_dns_domains = local.cfg.network_dns_domains
  sql_private_env     = local.cfg.sql_private_ip ? { SQL_PRIVATE_IP = "true" } : {}
}
