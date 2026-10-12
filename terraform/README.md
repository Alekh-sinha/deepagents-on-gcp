# Terraform for Pinboard (M7)

Mirrors `deploy/deploy.sh`: APIs, Artifact Registry, runtime + scheduler service accounts and IAM, Cloud SQL, secrets, the Agent Runtime and the housekeeping
scheduler job. It does NOT build the image (`deploy/deploy.sh build` does) or create the sandbox runtime / the bucket.

```bash
cd terraform
# names, project, region, sizes: edit deploy/config.yaml (or deploy/config.local.yaml); the same file feeds deploy.sh
cp terraform.tfvars.example terraform.tfvars      # fill in sandbox_runtime etc.
terraform init
terraform plan
terraform apply
# keys that must not enter the state (Terraform only creates the empty secrets):
gcloud secrets versions add TAVILY_API_KEY --data-file=-    # and AGENTMAIL_API_KEY, GITHUB_APP_PRIVATE_KEY
# then   uv run python scripts/smoke_runtime.py --runtime $(terraform output -raw runtime) --all
```

- Use EITHER `deploy.sh` OR Terraform for one project. To move the project deployed by `deploy.sh` under Terraform, use `imports.tf.example`.
- `class_methods.json` is generated from `pinboard.api.CONTRACT_METHODS`; a test fails when they differ (regenerate with the one-liner in that test).
- The state holds the generated database password and API token: keep it private (see the `backend` comment in `versions.tf`).
- UNVERIFIED: `agent_framework = "langgraph"` (the Terraform docs list `langchain`/`google-adk`; the Python SDK accepted `langgraph`); the plan against the real project; image updates (the runtime changes only when the image TAG changes: use a new `image_tag` per build, `latest` will not trigger an update).

## The web backend

`web.tf` creates the web backend as `deploy/deploy.sh web` does: a Cloud Run service behind Identity-Aware Proxy, its service account (which may call the engine and nothing
else), the IAP service agent's permission to invoke it, and one access binding per allowed user. Build its image first with `deploy/deploy.sh web-build`.
Who may open it = `allowed_users` in `config/policy.yaml` (override with `-var 'web_users=["you@example.com"]'`). Leave it out with `-var web_enabled=false`.
`terraform validate` passes; a real `plan`/`apply` of this file has NOT been run. If `deploy.sh web` already created the service, import it first
(`terraform import 'google_cloud_run_v2_service.web[0]' projects/PROJECT/locations/REGION/services/NAME-web`) or Terraform will report it already exists.
`RUNTIME_RESOURCE` is taken from the engine resource's `id`: check that it has the form `projects/NUMBER/locations/REGION/reasoningEngines/ID`.
