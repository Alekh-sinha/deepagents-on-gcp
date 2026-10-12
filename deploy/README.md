# Deploying Pinboard (M6)

Run everything from `pinboard-agent/`. Every command is idempotent; `DRY_RUN=1` prints what would run and changes nothing.

```bash
gcloud auth login && gcloud auth application-default login
# Everything that names or sizes the deployment is in deploy/config.yaml (default name: agent). Put your own values in
# deploy/config.local.yaml (git-ignored); PB_NAME, PB_PROJECT, PB_REGION, PB_BUCKET, PB_SQL_TIER, PB_SCHEDULE override both.
python deploy/deploy_config.py show     # what will be created, with every derived name

deploy/deploy.sh preflight     # read-only: account, project, which APIs/resources exist
deploy/deploy.sh setup         # APIs, Artifact Registry, runtime service account + IAM, secrets (free)
deploy/deploy.sh sql           # small Cloud SQL Postgres + DATABASE_URL secret            (BILLED while it exists)
deploy/deploy.sh build         # Cloud Build -> image in Artifact Registry
deploy/deploy.sh deploy        # create/update the Agent Runtime                           (BILLED while it exists)
deploy/deploy.sh scheduler     # Cloud Scheduler calls housekeeping (`expire`) every 5 min
deploy/deploy.sh smoke         # = scripts/smoke_runtime.py; add --search --sandbox (or --all)
deploy/deploy.sh status
deploy/deploy.sh down [--sql] [--everything]
```

Before `deploy`: `.env` must contain `SANDBOX_RUNTIME` (run `uv run pinboard ping` once). Put the GitHub App private key in Secret Manager yourself
(`setup` tells you the command). Secrets are only ever referenced by name (`secret://...`) in the runtime configuration.

If `deploy` says the update failed, re-run `deploy/runtime.py apply ... --recreate` (the URL changes; run `deploy.sh scheduler` again).

Files: `deploy.sh` (gcloud steps), `runtime.py` (create/update/delete the runtime; resource name kept in the git-ignored `.state.json`),
`start.sh` (container entrypoint: Cloud SQL proxy, then `pinboard serve`), `dotenv_get.py` (prints one `.env` value to stdin of `gcloud secrets versions add`).
Tested without a cloud: `tests/deploy/test_deploy_tools.py`. `terraform/` mirrors these steps.


## The web backend (Cloud Run behind IAP)

```bash
deploy/deploy.sh web-build     # Cloud Build -> <name>-web image
deploy/deploy.sh web           # service account, Cloud Run with IAP, access for the users in config/policy.yaml allowed_users
```
Open the printed URL in a browser, sign in with an allowed Google account, then go to `/_diag` and press the button: it creates a conversation
and shows the reply arriving live (the summary line says whether it arrived incrementally).

**Automated check from a terminal** (`scripts/smoke_web.py`) signs in as a SERVICE ACCOUNT, because IAP accepts a JWT signed by one (`aud` = the
service URL + `/*`). Create one, use it, and delete it; it must be listed in `allowed_users` (the engine's policy) for the chat steps:
```bash
SA=iap-smoke; P=$(gcloud config get-value project)
gcloud iam service-accounts create $SA --project $P
gcloud iam service-accounts add-iam-policy-binding $SA@$P.iam.gserviceaccount.com --member=user:YOU@gmail.com --role=roles/iam.serviceAccountTokenCreator
gcloud beta iap web add-iam-policy-binding --member=serviceAccount:$SA@$P.iam.gserviceaccount.com --role=roles/iap.httpsResourceAccessor \
  --region REGION --resource-type=cloud-run --service NAME-web
# add $SA@$P.iam.gserviceaccount.com to allowed_users in config/policy.yaml, then deploy.sh build && deploy.sh deploy
uv run python scripts/smoke_web.py --url https://NAME-web-PROJECTNUMBER.REGION.run.app --sa $SA@$P.iam.gserviceaccount.com --long --hold 330
# afterwards: remove it from policy.yaml, redeploy, and delete the service account
```
What it proves: IAP refuses anonymous requests and wrong-audience tokens; the backend verified IAP's signed header; a turn streams in pieces
through the real front end; `Last-Event-ID` resumes exactly; (`--hold`) a connection held for minutes is not cut.


## Secrets and settings from `.env`

`deploy.sh setup` reads your `.env` and sorts it **by name**, so there is no list of secrets to keep up to date:

- a variable whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PRIVATE` or `CREDENTIAL` is a **secret**. It is copied into Secret Manager
  under the same name if it is not there yet (the value goes through a pipe, never a command line). The engine reads it by name.
- every other variable is a **plain setting** and becomes an environment variable of the engine.
- never copied: values the deployment decides itself (database, API token, state backend, `GCP_*`, `ARTIFACTS_URI` ...) and settings that only make sense on
  your computer (`SKILLS_SOURCE`, `OUTPUT_DIR`, `SANDBOX_NAME`, `PINBOARD_DEBUG` ...). Add more with `env_exclude` in `config.yaml`.
- placeholders (`your-...`), empty values and `secret://` references are skipped.

`python deploy/dotenv_secrets.py names` shows which names would be treated as secrets (no values). A secret that is not in `.env` (a key file, for example)
is listed under `secrets_provided` in `config.yaml`; `setup` tells you if it is missing. Terraform cannot read `.env`, so with Terraform you list the secret
names in `config.yaml` (`secrets_from_dotenv`, `secrets_provided`) and add their values yourself.
