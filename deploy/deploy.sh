#!/usr/bin/env bash
# Deploy Pinboard to Agent Runtime with gcloud. Run from anywhere:   pinboard-agent/deploy/deploy.sh <command>
#
#   preflight   read-only: who/where you are, which APIs and resources already exist
#   setup       APIs, Artifact Registry, runtime service account + permissions, secrets            (free)
#   sql         a small Cloud SQL Postgres + the DATABASE_URL secret                               (BILLED while it exists)
#   build       Cloud Build builds the image and pushes it to Artifact Registry                    (pennies)
#   deploy      create or update the Agent Runtime from that image; --recreate deletes and creates it again (BILLED while it exists)
#   scheduler   Cloud Scheduler job that calls the `expire` housekeeping method every 5 minutes     (pennies)
#   all         setup, sql, build, deploy, scheduler
#   web-build   Cloud Build builds the WEB BACKEND image (the public face)                             (pennies)
#   web         deploy the web backend on Cloud Run behind IAP (Google sign-in); only the engine's allowed_users may open it
#   web-smoke   test the deployed web backend with a real IAP-signed request (see scripts/smoke_web.py)
#   smoke       call every method on the deployed runtime and run real tasks through it
#   status      what exists right now
#   down        delete the runtime and the scheduler job; add --sql to delete the database too; --everything also removes secrets and the repo
#
# `web` needs the engine deployed first (it talks to it). Who may open the web app = allowed_users in config/policy.yaml.
# Every command is idempotent (safe to repeat). DRY_RUN=1 prints what would be run and changes nothing.
# Settings: deploy/config.yaml (name, project, region, bucket, ...). Environment variables PB_NAME PB_PROJECT PB_REGION PB_BUCKET PB_SQL_TIER PB_SCHEDULE override it.
set -euo pipefail
cd "$(dirname "$0")/.."

# Every setting and every resource name comes from deploy/config.yaml (+ config.local.yaml, + PB_* environment variables): see that file.
# the project's own Python (it needs pinboard's packages); falls back to the virtualenv if uv is not installed
if command -v uv >/dev/null 2>&1; then PYRUN=(uv run python); else PYRUN=(.venv/bin/python); fi
cfg_out=$("${PYRUN[@]}" deploy/deploy_config.py shell) || exit 2
eval "$cfg_out"
DRY_RUN="${DRY_RUN:-}"
STATE_FILE="deploy/.state.json"
APIS="aiplatform artifactregistry cloudbuild secretmanager sqladmin iam cloudresourcemanager cloudscheduler storage run iap"

say() { printf '\n== %s\n' "$*"; }
# QUIET_CMD=1 run ...   shows the command but hides its (uninteresting) output
run() { printf '  $ %s\n' "$*"; if [ -z "$DRY_RUN" ]; then if [ -n "${QUIET_CMD:-}" ]; then "$@" >/dev/null; else "$@"; fi; fi; }
# like run, but retry: IAM changes take a few seconds to become visible (seen on a brand-new service account)
retry() {
  local n=$1; shift; printf '  $ %s\n' "$*"; [ -n "$DRY_RUN" ] && return 0
  local i=1
  until if [ -n "${QUIET_CMD:-}" ]; then "$@" >/dev/null; else "$@"; fi; do
    [ "$i" -ge "$n" ] && return 1
    echo "    retrying in $((i * 5))s ($i/$n)"; sleep $((i * 5)); i=$((i + 1))
  done
}
# existence checks are real even though nothing changes; in a dry run assume nothing exists so every create command is shown
exists() { [ -z "$DRY_RUN" ] && "$@" >/dev/null 2>&1; }
need_project() { [ -n "$PROJECT" ] || { echo "no project: set PROJECT=... or run gcloud config set project ..." >&2; exit 2; }; }
project_number() { gcloud projects describe "$PROJECT" --format='value(projectNumber)'; }
runtime_name() { [ -f "$STATE_FILE" ] && python3 -c "import json;print(json.load(open('$STATE_FILE')).get('runtime',''))" || true; }

all_dotenv_secrets() {  # every secret in .env (found by NAME, see deploy/dotenv_secrets.py) plus the names config.yaml insists on
  { ENV_EXCLUDE="$ENV_EXCLUDE" "${PYRUN[@]}" deploy/dotenv_secrets.py names 2>/dev/null || true; for s in $SECRETS_FROM_DOTENV; do echo "$s"; done; } | grep . | sort -u
}

ensure_secret_from_stdin() {  # $1 = secret name; the value arrives on stdin and never appears in a command line or in this script's output
  local name=$1
  if ! exists gcloud secrets describe "$name" --project "$PROJECT"; then
    run gcloud secrets create "$name" --replication-policy=automatic --project "$PROJECT"
  fi
  printf '  $ gcloud secrets versions add %s --data-file=-   (value from stdin, not shown)\n' "$name"
  if [ -z "$DRY_RUN" ]; then gcloud secrets versions add "$name" --data-file=- --project "$PROJECT" >/dev/null; else cat >/dev/null; fi
}

cmd_preflight() {
  need_project
  say "who and where"
  echo "  account : $(gcloud config get-value account 2>/dev/null)"
  echo "  project : $PROJECT (number $(project_number))   region: $REGION"
  echo "  image   : $IMAGE"
  echo "  bucket  : gs://$BUCKET/$ARTIFACTS_PREFIX"
  say "APIs"
  local enabled; enabled=$(gcloud services list --enabled --project "$PROJECT" --format='value(config.name)')
  for a in $APIS; do printf '  %-24s %s\n' "$a" "$(echo "$enabled" | grep -qx "$a.googleapis.com" && echo enabled || echo 'not enabled (setup enables it)')"; done
  say "resources"
  exists gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" && echo "  artifact repo : $REPO exists" || echo "  artifact repo : missing"
  exists gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" && echo "  runtime SA    : exists" || echo "  runtime SA    : missing"
  exists gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT" && echo "  cloud sql     : $SQL_INSTANCE exists (billed)" || echo "  cloud sql     : missing"
  exists gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" && echo "  bucket        : gs://$BUCKET exists" || echo "  bucket        : gs://$BUCKET NOT FOUND"
  for s in $(all_dotenv_secrets) $SECRETS_PROVIDED "$API_TOKEN_SECRET" "$DB_URL_SECRET" MCP_ENCRYPTION_KEY; do
    exists gcloud secrets describe "$s" --project "$PROJECT" && echo "  secret        : $s exists" || echo "  secret        : $s missing"
  done
  echo "  runtime       : $(runtime_name | sed 's/^$/(none recorded)/')"
}

cmd_setup() {
  need_project
  say "enable APIs"
  # shellcheck disable=SC2046
  run gcloud services enable $(for a in $APIS; do printf '%s.googleapis.com ' "$a"; done) --project "$PROJECT"
  say "Artifact Registry repository"
  exists gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" ||
    run gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION" --project "$PROJECT"
  say "runtime service account and its permissions"
  exists gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" ||
    run gcloud iam service-accounts create "$RUNTIME_SA_ID" --display-name="$NAME runtime" --project "$PROJECT"
  # serviceusage.serviceUsageConsumer: Cloud Storage calls made inside Agent Runtime are billed to the project and are refused without it
  # (found on the first real file upload: "does not have serviceusage.services.use access to the Google Cloud project")
  for role in roles/aiplatform.user roles/secretmanager.secretAccessor roles/cloudsql.client roles/logging.logWriter roles/serviceusage.serviceUsageConsumer; do
    QUIET_CMD=1 retry 6 gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNTIME_SA" --role="$role" --condition=None --quiet
  done
  exists gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" ||
    run gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --project "$PROJECT" --uniform-bucket-level-access
  QUIET_CMD=1 retry 6 gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$RUNTIME_SA" --role=roles/storage.objectAdmin
  # signed download links are signed through the IAM Credentials API as the runtime identity itself
  QUIET_CMD=1 retry 6 gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --member="serviceAccount:$RUNTIME_SA" --role=roles/iam.serviceAccountTokenCreator --project "$PROJECT"
  say "Google's Agent Runtime service agents may pull the image"
  local num; num=$([ -n "$DRY_RUN" ] && echo "<PROJECT_NUMBER>" || project_number)
  for agent in gcp-sa-aiplatform-re gcp-sa-aiplatform; do
    QUIET_CMD=1 run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:service-$num@$agent.iam.gserviceaccount.com" --role=roles/artifactregistry.reader --condition=None --quiet || echo "    (that service agent does not exist yet; deploy retries this)"
  done
  say "Cloud Build runs as the Compute default service account in new projects"
  QUIET_CMD=1 run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$num-compute@developer.gserviceaccount.com" --role=roles/cloudbuild.builds.builder --condition=None --quiet
  say "secrets (values never printed)"
  for s in $(all_dotenv_secrets); do
    if exists gcloud secrets describe "$s" --project "$PROJECT"; then echo "  $s: already in Secret Manager, left alone"
    elif [ -n "$DRY_RUN" ]; then echo "  (dry run) would copy $s from .env into a new secret"; ensure_secret_from_stdin "$s" </dev/null
    else "${PYRUN[@]}" deploy/dotenv_get.py "$s" | ensure_secret_from_stdin "$s"; fi
  done
  for s in $SECRETS_PROVIDED; do
    if exists gcloud secrets describe "$s" --project "$PROJECT"; then echo "  $s: already in Secret Manager, left alone"
    else echo "  $s is NOT in Secret Manager: add it with   gcloud secrets create $s --data-file=<your file>   (the tools that need it stay off without it)"; fi
  done
  if exists gcloud secrets describe "$API_TOKEN_SECRET" --project "$PROJECT"; then echo "  $API_TOKEN_SECRET: already exists, left alone"
  else python3 -c "import secrets; print(secrets.token_hex(32), end='')" | ensure_secret_from_stdin "$API_TOKEN_SECRET"; fi
  # the key that encrypts the tokens users paste for their MCP servers (a database dump alone must not reveal them); created once, never rotated silently
  if exists gcloud secrets describe MCP_ENCRYPTION_KEY --project "$PROJECT"; then echo "  MCP_ENCRYPTION_KEY: already exists, left alone"
  else "${PYRUN[@]}" -c "from pinboard.mcp import generate_key; print(generate_key(), end='')" | ensure_secret_from_stdin MCP_ENCRYPTION_KEY; fi
  say "setup done"
}

cmd_sql() {
  need_project
  say "Cloud SQL Postgres ($SQL_TIER): BILLED while it exists"
  exists gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT" ||
    run gcloud sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE --tier="$SQL_TIER" --region "$REGION" --availability-type=zonal --no-backup --project "$PROJECT"
  exists gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" --project "$PROJECT" ||
    run gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE" --project "$PROJECT"
  if exists gcloud secrets describe "$DB_URL_SECRET" --project "$PROJECT"; then
    echo "  $DB_URL_SECRET already exists: the database user is left alone"
  else
    local pw; pw=$(python3 -c "import secrets; print(secrets.token_urlsafe(24), end='')")
    if exists gcloud sql users describe "$DB_USER" --instance "$SQL_INSTANCE" --project "$PROJECT"; then
      printf '  $ gcloud sql users set-password %s --instance %s --password=<generated>\n' "$DB_USER" "$SQL_INSTANCE"
      [ -z "$DRY_RUN" ] && gcloud sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password="$pw" --project "$PROJECT" >/dev/null
    else
      printf '  $ gcloud sql users create %s --instance %s --password=<generated>\n' "$DB_USER" "$SQL_INSTANCE"
      [ -z "$DRY_RUN" ] && gcloud sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password="$pw" --project "$PROJECT" >/dev/null
    fi
    # the app reaches the database through the Cloud SQL Auth Proxy that start.sh runs beside it, on localhost
    printf 'postgresql://%s:%s@127.0.0.1:5432/%s?sslmode=disable' "$DB_USER" "$pw" "$DB_NAME" | ensure_secret_from_stdin "$DB_URL_SECRET"
  fi
  say "sql done (connection name: $PROJECT:$REGION:$SQL_INSTANCE)"
}

cmd_build() {
  need_project
  if compgen -G "deploy/extra-ca/*.crt" >/dev/null; then  # left over from the Agent Gateway spike; it belongs only in the spike's image
    echo "deploy/extra-ca contains a certificate (Agent Gateway spike): the normal image must not trust it. Delete it (spikes/gateway/gateway.sh down) and retry." >&2
    exit 1
  fi
  say "build $IMAGE"
  retry 4 gcloud builds submit . --tag "$IMAGE" --region "$REGION" --project "$PROJECT"
}

cmd_deploy() {
  need_project
  say "deploy the Agent Runtime"
  local num; num=$([ -n "$DRY_RUN" ] && echo "<PROJECT_NUMBER>" || project_number)
  for agent in gcp-sa-aiplatform-re gcp-sa-aiplatform; do   # these service agents only exist once Agent Platform has been used
    QUIET_CMD=1 run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:service-$num@$agent.iam.gserviceaccount.com" --role=roles/artifactregistry.reader --condition=None --quiet || true
  done
  # pin the image by its digest: the moving :latest tag could keep serving a cached older copy to the platform
  local pinned="$IMAGE" digest=""
  [ -n "$DRY_RUN" ] || digest=$(gcloud artifacts docker images describe "$IMAGE" --project "$PROJECT" --format='value(image_summary.digest)' 2>/dev/null || true)
  [ -n "$digest" ] && pinned="${IMAGE%:*}@$digest"
  echo "  image: $pinned"
  run "${PYRUN[@]}" deploy/runtime.py apply --project "$PROJECT" --region "$REGION" --image "$pinned" --service-account "$RUNTIME_SA" \
    --sql-connection "$PROJECT:$REGION:$SQL_INSTANCE" --bucket "$BUCKET" "$@"   # e.g. --recreate (needed once to move the engine onto a network)
}

cmd_scheduler() {
  need_project
  local rt; rt=$(runtime_name)
  [ -n "$rt" ] || [ -n "$DRY_RUN" ] || { echo "no runtime recorded in $STATE_FILE: run deploy first" >&2; exit 2; }
  rt=${rt:-<RUNTIME_RESOURCE_NAME>}
  say "scheduler: call the housekeeping method every 5 minutes"
  exists gcloud iam service-accounts describe "$SCHEDULER_SA" --project "$PROJECT" ||
    run gcloud iam service-accounts create "$SCHEDULER_SA_ID" --display-name="$NAME scheduler" --project "$PROJECT"
  QUIET_CMD=1 retry 6 gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SCHEDULER_SA" --role=roles/aiplatform.user --condition=None --quiet
  local uri="https://$REGION-aiplatform.googleapis.com/v1/$rt:query"
  # `create` takes --headers, `update` takes --update-headers (found when the job had to be re-pointed at a new runtime)
  local verb=create headers=--headers; exists gcloud scheduler jobs describe "$JOB" --location "$REGION" --project "$PROJECT" && { verb=update; headers=--update-headers; }
  retry 4 gcloud scheduler jobs "$verb" http "$JOB" --location "$REGION" --project "$PROJECT" --schedule="$SCHEDULE" --time-zone=UTC \
    --uri="$uri" --http-method=POST "$headers=Content-Type=application/json" --message-body='{"classMethod":"expire","input":{}}' \
    --oauth-service-account-email="$SCHEDULER_SA" --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform
}

cmd_smoke() { run "${PYRUN[@]}" scripts/smoke_runtime.py "$@"; }

cmd_web_build() {
  need_project
  say "build $WEB_IMAGE"
  retry 4 gcloud builds submit . --config deploy/cloudbuild-web.yaml --substitutions "_IMAGE=$WEB_IMAGE" --region "$REGION" --project "$PROJECT"
}

cmd_web() {
  need_project
  [ -n "$WEB_USERS" ] || { echo "config/policy.yaml has no allowed_users: nobody could open the web app" >&2; exit 2; }
  local rt; rt=$(runtime_name)
  [ -n "$rt" ] || [ -n "$DRY_RUN" ] || { echo "no runtime recorded in $STATE_FILE: run deploy first" >&2; exit 2; }
  rt=${rt:-<RUNTIME_RESOURCE_NAME>}
  local num; num=$([ -n "$DRY_RUN" ] && echo "<PROJECT_NUMBER>" || project_number)
  say "web service account (it may call the engine, nothing else)"
  exists gcloud iam service-accounts describe "$WEB_SA" --project "$PROJECT" ||
    run gcloud iam service-accounts create "$WEB_SA_ID" --display-name="$NAME web" --project "$PROJECT"
  QUIET_CMD=1 retry 6 gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$WEB_SA" --role=roles/aiplatform.user --condition=None --quiet
  say "IAP service agent"
  run gcloud beta services identity create --service=iap.googleapis.com --project "$PROJECT"
  say "deploy $WEB_SERVICE on Cloud Run with IAP in front (no public access; requests are verified again inside the app)"
  retry 3 gcloud beta run deploy "$WEB_SERVICE" --image "$WEB_IMAGE" --region "$REGION" --project "$PROJECT" --service-account "$WEB_SA" \
    --no-allow-unauthenticated --iap --min-instances "$WEB_MIN" --max-instances "$WEB_MAX" --timeout 3600 \
    --set-env-vars "WEB_AUTH=iap,IAP_AUDIENCE=/projects/$num/locations/$REGION/services/$WEB_SERVICE,RUNTIME_RESOURCE=$rt,RUNTIME_REGION=$REGION"
  say "the IAP service agent may call the service; the allowed users may pass IAP"
  QUIET_CMD=1 retry 6 gcloud run services add-iam-policy-binding "$WEB_SERVICE" --region "$REGION" --project "$PROJECT" \
    --member="serviceAccount:service-$num@gcp-sa-iap.iam.gserviceaccount.com" --role=roles/run.invoker
  for u in $WEB_USERS; do
    local kind=user; case "$u" in *.gserviceaccount.com) kind=serviceAccount ;; esac   # the policy may list a service account too
    QUIET_CMD=1 retry 6 gcloud beta iap web add-iam-policy-binding --member="$kind:$u" --role=roles/iap.httpsResourceAccessor \
      --region "$REGION" --resource-type=cloud-run --service="$WEB_SERVICE" --project "$PROJECT"
  done
  [ -n "$DRY_RUN" ] || echo "  open: $(gcloud run services describe "$WEB_SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')   (sign in with: $WEB_USERS)"
}

cmd_web_smoke() { run "${PYRUN[@]}" scripts/smoke_web.py "$@"; }

cmd_status() {
  need_project
  say "status of $NAME in $PROJECT"
  echo "  runtime   : $(runtime_name | sed 's/^$/(none recorded)/')"
  run gcloud sql instances list --project "$PROJECT" --filter="name=$SQL_INSTANCE" --format="table(name,state,settings.tier,region)"
  run gcloud scheduler jobs list --location "$REGION" --project "$PROJECT" --filter="name~$JOB" --format="table(name.basename(),schedule,state,lastAttemptTime)"
  run gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT/$REPO" --project "$PROJECT" --format="table(package,version,createTime)" --limit 3
}

cmd_down() {
  need_project
  local sql=0 everything=0
  for a in "$@"; do case "$a" in --sql) sql=1;; --everything) sql=1; everything=1;; *) echo "unknown option $a" >&2; exit 2;; esac; done
  say "about to DELETE in $PROJECT: the runtime, the scheduler job$([ $sql = 1 ] && echo ", the Cloud SQL instance $SQL_INSTANCE (and every conversation in it)")$([ $everything = 1 ] && echo ", the image repository, the service accounts and the $NAME secrets")"
  if [ -z "$DRY_RUN" ]; then read -r -p "  type 'delete' to confirm: " ok; [ "$ok" = "delete" ] || { echo "aborted"; exit 1; }; fi
  exists gcloud scheduler jobs describe "$JOB" --location "$REGION" --project "$PROJECT" && run gcloud scheduler jobs delete "$JOB" --location "$REGION" --project "$PROJECT" --quiet
  [ -n "$(runtime_name)" ] && run "${PYRUN[@]}" deploy/runtime.py delete --project "$PROJECT" --region "$REGION"
  [ $sql = 1 ] && run gcloud sql instances delete "$SQL_INSTANCE" --project "$PROJECT" --quiet
  if [ $everything = 1 ]; then
    run gcloud secrets delete "$DB_URL_SECRET" --project "$PROJECT" --quiet || true
    run gcloud secrets delete "$API_TOKEN_SECRET" --project "$PROJECT" --quiet || true
    run gcloud artifacts repositories delete "$REPO" --location "$REGION" --project "$PROJECT" --quiet || true
    run gcloud iam service-accounts delete "$RUNTIME_SA" --project "$PROJECT" --quiet || true
    run gcloud iam service-accounts delete "$SCHEDULER_SA" --project "$PROJECT" --quiet || true
  fi
  say "down done (the bucket gs://$BUCKET and the TAVILY/AGENTMAIL/GITHUB secrets are never deleted by this script)"
}

case "${1:-help}" in
  preflight) cmd_preflight ;;
  setup) cmd_setup ;;
  sql) cmd_sql ;;
  build) cmd_build ;;
  deploy) shift; cmd_deploy "$@" ;;
  scheduler) cmd_scheduler ;;
  all) cmd_setup; cmd_sql; cmd_build; cmd_deploy; cmd_scheduler ;;
  smoke) shift; cmd_smoke "$@" ;;
  web-build) cmd_web_build ;;
  web) cmd_web ;;
  web-smoke) shift; cmd_web_smoke "$@" ;;
  status) cmd_status ;;
  down) shift; cmd_down "$@" ;;
  help | -h | --help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown command '$1'; run: deploy/deploy.sh help" >&2; exit 2 ;;
esac
