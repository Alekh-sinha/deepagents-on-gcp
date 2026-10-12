# The settings that decide what everything is called live in deploy/config.yaml (+ deploy/config.local.yaml), the same files deploy.sh reads.
# These variables are OPTIONAL overrides: leave them out (null) to use the config files.
variable "name" {
  type        = string
  default     = null
  description = "Prefix for every resource; default: `name` in deploy/config.yaml"
}

variable "project_id" {
  type        = string
  default     = null
  description = "GCP project id; default: `project` in the config files"
}

variable "region" {
  type    = string
  default = null
}

variable "artifacts_bucket" {
  type        = string
  default     = null
  description = "Bucket for saved files; default: `artifacts_bucket` in the config, or (if that is empty) a new <project>-<name>-files bucket created here"
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Image tag in Artifact Registry. The image itself is built by `deploy/deploy.sh build` (Cloud Build), not by Terraform."
}

variable "sql_tier" {
  type    = string
  default = null
}

variable "housekeeping_schedule" {
  type    = string
  default = null
}

variable "min_instances" {
  type    = number
  default = null
}

variable "max_instances" {
  type    = number
  default = null
}

# Same values deploy/runtime.py takes from .env
variable "sandbox_runtime" {
  type        = string
  default     = ""
  description = "SANDBOX_RUNTIME from .env (created by `uv run pinboard ping`). Leave empty for an agent without a sandbox (policy features.sandbox: false)."
}

variable "extra_env" {
  type        = map(string)
  default     = {}
  description = "Further environment variables for the agent, e.g. AGENT_BUILDER = \"my_package.agent:build\". Never put secrets here: they go in Secret Manager."
}

variable "github_app_id" {
  type    = string
  default = ""
}

variable "agentmail_inbox_id" {
  type    = string
  default = ""
}

variable "model" {
  type    = string
  default = "google_genai:gemini-2.5-flash"
}

variable "web_enabled" {
  type        = bool
  default     = true
  description = "Create the web backend (Cloud Run behind IAP). Build its image first with `deploy/deploy.sh web-build`."
}

variable "web_users" {
  type        = list(string)
  default     = null
  description = "Who may open the web app (e-mail addresses; *.gserviceaccount.com are treated as service accounts). Default: allowed_users in config/policy.yaml."
}

variable "web_min_instances" {
  type    = number
  default = null
}

variable "web_max_instances" {
  type    = number
  default = null
}
