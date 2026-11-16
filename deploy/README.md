# Deploying Pinboard

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

If `deploy` says the update failed, run `deploy/deploy.sh deploy --recreate` (the runtime's name changes, so run `deploy.sh scheduler` and `deploy.sh web` again to point them at the new one).
A new image normally updates the runtime in place. To create a **second** runtime next to the existing one instead (blue/green), pass `--state-file`: `deploy/deploy.sh deploy --state-file deploy/.state-new.json`.

Files: `deploy.sh` (gcloud steps), `runtime.py` (create/update/delete the runtime; its resource name is kept in the git-ignored `.state.json`, or in the file you pass with `--state-file`),
`network.sh` (private network, egress proxy, MCP subnet), `egress.py` (the proxy allowlist and proxy settings, worked out from the policy and the config), `deploy_config.py` (reads `config.yaml` and derives every name),
`start.sh` (container entrypoint: Cloud SQL proxy, then `pinboard serve`), `dotenv_get.py` and `dotenv_secrets.py` (the `.env` handling below).
Tested without a cloud: `tests/deploy/`. `terraform/` mirrors these steps.


## The web backend (Cloud Run behind IAP)

```bash
deploy/deploy.sh web-build     # Cloud Build -> <name>-web image
deploy/deploy.sh web           # service account, Cloud Run with IAP, access for the users in `allowed_users` (config/policy.yaml, with your config/policy.local.yaml merged over it)
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
# add $SA@$P.iam.gserviceaccount.com to allowed_users in config/policy.local.yaml, then deploy.sh build && deploy.sh deploy
uv run python scripts/smoke_web.py --url https://NAME-web-PROJECTNUMBER.REGION.run.app --sa $SA@$P.iam.gserviceaccount.com --long --hold 330
# afterwards: remove it from policy.local.yaml, redeploy, and delete the service account
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


## Your own values

Nothing personal is in the shipped files. `deploy/config.local.yaml` (project, bucket, network settings) and `config/policy.local.yaml` (who may sign in, the e-mail sender and recipients; merged over `config/policy.yaml`) are
git-ignored; copy `config/policy.local.example.yaml` to start. The image build and the deploy tools read both. With the placeholders in the shipped policy nobody can sign in and nothing can be e-mailed.


## Private networking, egress control, and moving to a new runtime

Optional, off by default; the full explanation, limits and verification are in [../docs/EGRESS.md](../docs/EGRESS.md).

```bash
deploy/network.sh up           # VPC, attachment, Secure Web Proxy + host allowlist, Cloud NAT for the proxy only, MCP subnet, private DNS zone (the proxy and NAT are BILLED per hour)
# put the lines it prints into deploy/config.local.yaml:  network_attachment, egress_proxy, sql_private_ip, network_dns_domains
gcloud sql instances patch <name>-pg --network projects/PROJECT/global/networks/<name>-net      # adds a private address; RESTARTS the database (about 15 minutes)
deploy/deploy.sh build
deploy/deploy.sh deploy --state-file deploy/.state-new.json                      # a NEW runtime on the network, next to the old one
uv run python scripts/smoke_runtime.py --runtime <the new runtime> --all         # test it; the old one keeps serving
cp deploy/.state-new.json deploy/.state.json && deploy/deploy.sh scheduler && deploy/deploy.sh web      # switch the callers; delete the old runtime when you are happy
deploy/network.sh allowlist    # after changing mcp.allowed_hosts or egress_allowed_hosts: re-apply the proxy's host list
deploy/network.sh down         # remove it all again (the database and the engine are not touched)
```

A network interface cannot be added to an existing runtime, which is why this is a blue/green move. A runtime cannot have both this interface and an Agent Gateway.
