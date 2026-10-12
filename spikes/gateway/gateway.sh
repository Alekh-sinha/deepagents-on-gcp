#!/usr/bin/env bash
# SPIKE: can Pinboard run behind Agent Gateway (egress allowlist)? A SECOND runtime "pinboard-gw" is created next to the working one,
# with its own database; the working deployment is never touched. Run from pinboard-agent/:   spikes/gateway/gateway.sh <stage>
#
#   ROUND 1 failed: the gateway's TLS inspection breaks the Cloud SQL proxy's mutual TLS to the instance's PUBLIC address.
#   ROUND 2 (gateway networkConfig -> VPC) failed: even Google API calls got the gateway's own default certificate (agent-gateway-proxy.internal).
#   ROUND 3 (this): the database has only a PRIVATE address in a VPC and the RUNTIME ITSELF joins that VPC through a PSC interface
#   (psc_interface_config); API/web traffic keeps using the plain gateway of round 1. The proxy runs with --private-ip.
#   (PB_GW_VPC=1 would put the VPC config on the gateway again, as in round 2.)
#
#   apis      enable the extra APIs                                      (free)
#   vpc       VPC + /28 subnet + PSC network attachment + private services access for Cloud SQL   (free)
#   sql       a NEW private-IP-only Cloud SQL instance pbgw-pg + database + secret     (BILLED while it exists; ~10 minutes)
#   gateway   delete the round-1 gateway, create the Agent-to-Anywhere gateway WITH networkConfig + IAP authz policy
#   hosts     register the allowed hostnames in Agent Registry           (edit HOSTS below)
#   cert      download the gateway's root CA into deploy/extra-ca/
#   build     rebuild the image (with the CA) as tag :gw
#   deploy bootstrap   FIRST deploy: starts without secrets/database (memory state) so it can come up with no permissions; this creates
#             the project's agent-identity pool, which `grant` needs ("Identity Pool does not exist" otherwise)
#   grant     AFTER the bootstrap deploy: give every Agent Runtime agent identity in this project the permissions the old service account had
#             (a project-wide principalSet; the trust domain is read from the bootstrap runtime: agents.global.org-<ORG>.system.id.goog here)
#   deploy recreate   delete the bootstrap runtime and create the real one (secrets + Cloud SQL)
#   smoke     scripts/smoke_runtime.py --all against the spike runtime
#   enforce   switch the authz policy from DRY_RUN to enforcing
#   down-vpc  delete only the VPC pieces: private Cloud SQL pbgw-pg + secret, attachment, peering, range, subnet, network (keeps gateway, hosts, runtime)
#   down      delete everything the spike created
# DRY_RUN=1 prints commands only. Settings: PB_PROJECT PB_REGION. EVERY stage is a hypothesis taken from the docs: paste failures back.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT="${PB_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${PB_REGION:-us-central1}"
GW="${PB_GW:-pbgw-vpc}"; EXT="pbgw-iap-ext"; POL="pbgw-vpc-policy"
OLD_GW="pbgw-egress"; OLD_POL="pbgw-iap-policy"   # round 1; deleted by `gateway` (all agents in a region must share one egress gateway)
NET="pbgw-net"; SUBNET="pbgw-subnet-agw"; NA="pbgw-na"; PSA_RANGE="pbgw-psa-range"
SQLP="pbgw-pg"; DBSEC="pbgw-database-url"
STATE="spikes/gateway/.state.json"
SQL="pinboard-pg"; BUCKET="${PB_BUCKET:-privai}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/pinboard-repo/pinboard:gw"
GW_PATH="projects/$PROJECT/locations/$REGION/agentGateways/$GW"
DRY_RUN="${DRY_RUN:-}"
# Exact hostnames the app needs (no wildcards allowed). Start with these; the DRY_RUN logs show what else it tried to reach.
HOSTS=(
  "$REGION-aiplatform.googleapis.com" "$REGION-aiplatform.mtls.googleapis.com" "aiplatform.googleapis.com"
  "secretmanager.googleapis.com" "storage.googleapis.com" "sqladmin.googleapis.com" "iamcredentials.googleapis.com"
  "secretmanager.mtls.googleapis.com" "storage.mtls.googleapis.com" "sqladmin.mtls.googleapis.com" "iamcredentials.mtls.googleapis.com"
  "oauth2.googleapis.com" "oauth2.mtls.googleapis.com" "logging.mtls.googleapis.com" "logging.googleapis.com" "agentregistry.googleapis.com"
  "api.tavily.com" "api.github.com" "api.agentmail.to"
)
say() { printf '\n== %s\n' "$*"; }
run() { printf '  $ %s\n' "$*"; [ -n "$DRY_RUN" ] || "$@"; }
runq() { printf '  $ %s\n' "$*"; [ -n "$DRY_RUN" ] || "$@" >/dev/null; }   # same, but hides the command's own output
state_get() { [ -f "$STATE" ] && python3 -c "import json;print(json.load(open('$STATE')).get('$1',''))" || true; }
state_set() { python3 - "$STATE" "$1" "$2" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text()) if p.exists() else {}
d[sys.argv[2]] = sys.argv[3]; p.write_text(json.dumps(d, indent=2))
PY
}
slug() { echo "$1" | tr '.' '-' | cut -c1-60; }

stage_apis() {
  say "APIs"
  run gcloud services enable servicenetworking.googleapis.com compute.googleapis.com networksecurity.googleapis.com networkservices.googleapis.com iap.googleapis.com agentregistry.googleapis.com --project "$PROJECT"
}

stage_vpc() {
  say "VPC, subnet, PSC network attachment, private services access"
  run gcloud compute networks create "$NET" --subnet-mode=custom --project "$PROJECT"
  run gcloud compute networks subnets create "$SUBNET" --network "$NET" --range=192.168.10.0/28 --region "$REGION" --enable-private-ip-google-access --project "$PROJECT"
  run gcloud compute network-attachments create "$NA" --region "$REGION" --subnets "$SUBNET" --connection-preference=ACCEPT_AUTOMATIC --project "$PROJECT"
  # Cloud SQL private IP lives in Google's service network, peered to this VPC
  run gcloud compute addresses create "$PSA_RANGE" --global --purpose=VPC_PEERING --prefix-length=24 --network "$NET" --project "$PROJECT"
  run gcloud services vpc-peerings connect --service=servicenetworking.googleapis.com --ranges="$PSA_RANGE" --network "$NET" --project "$PROJECT"
}

stage_sql() {
  say "private-IP-only Cloud SQL $SQLP (BILLED while it exists)"
  run gcloud sql instances create "$SQLP" --database-version=POSTGRES_16 --edition=ENTERPRISE --tier=db-f1-micro --region "$REGION" \
    --availability-type=zonal --no-backup --network "projects/$PROJECT/global/networks/$NET" --no-assign-ip --project "$PROJECT"
  run gcloud sql databases create pinboard --instance "$SQLP" --project "$PROJECT"
  [ -n "$DRY_RUN" ] && { echo "  (dry run: would create user pinboard with a generated password and the secret $DBSEC)"; return; }
  local pw; pw=$(python3 -c "import secrets; print(secrets.token_urlsafe(24), end='')")
  gcloud sql users create pinboard --instance "$SQLP" --password="$pw" --project "$PROJECT"
  gcloud secrets describe "$DBSEC" --project "$PROJECT" >/dev/null 2>&1 || gcloud secrets create "$DBSEC" --replication-policy=automatic --project "$PROJECT"
  printf 'postgresql://pinboard:%s@127.0.0.1:5432/pinboard?sslmode=disable' "$pw" | gcloud secrets versions add "$DBSEC" --data-file=- --project "$PROJECT" >/dev/null
  echo "  secret $DBSEC written; instance has NO public address: only the VPC can reach it"
}

stage_gateway() {
  say "gateway (Agent-to-Anywhere, in $REGION) with egress to the VPC"
  mkdir -p spikes/gateway/yaml
  # a gateway's network attachment is immutable, so a changed setup means delete + create (the policy that targets it goes first)
  for pol in "$POL" "$OLD_POL"; do run gcloud network-security authz-policies delete "$pol" --location "$REGION" --project "$PROJECT" --quiet 2>/dev/null || true; done
  for gw in "$GW" "$OLD_GW"; do run gcloud network-services agent-gateways delete "$gw" --location "$REGION" --project "$PROJECT" --quiet 2>/dev/null || true; done
  local na_uri="projects/$PROJECT/regions/$REGION/networkAttachments/$NA"
  [ -n "$DRY_RUN" ] || na_uri=$(gcloud compute network-attachments describe "$NA" --region "$REGION" --project "$PROJECT" --format='value(selfLink)' | sed 's#^https://www.googleapis.com/compute/v1/##')
  echo "  network attachment: $na_uri"
  local gw_network=""
  if [ -n "${PB_GW_VPC:-}" ]; then gw_network="networkConfig:
  egress:
    networkAttachment: $na_uri"; echo "  (round 2 mode: gateway networkConfig)"; else echo "  (round 3 mode: gateway WITHOUT networkConfig; the runtime joins the VPC itself)"; fi
  cat > spikes/gateway/yaml/gateway.yaml <<Y
name: $GW
protocols:
  - MCP
googleManaged:
  governedAccessPath: AGENT_TO_ANYWHERE
registries:
  - //agentregistry.googleapis.com/projects/$PROJECT/locations/$REGION
$gw_network
Y
  cat > spikes/gateway/yaml/ext.yaml <<Y
name: $EXT
service: iap.googleapis.com
failOpen: false
timeout: 1s
metadata:
  iapPolicyVersion: "V2"
  iamEnforcementMode: "DRY_RUN"
Y
  cat > spikes/gateway/yaml/policy.yaml <<Y
name: $POL
target:
  resources:
    - "projects/$PROJECT/locations/$REGION/agentGateways/$GW"
policyProfile: REQUEST_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
      - "projects/$PROJECT/locations/$REGION/authzExtensions/$EXT"
Y
  echo "  (the extension starts in DRY_RUN: it LOGS what it would block and blocks nothing; 'enforce' flips it)"
  run gcloud network-services agent-gateways import "$GW" --source=spikes/gateway/yaml/gateway.yaml --location "$REGION" --project "$PROJECT"
  run gcloud beta service-extensions authz-extensions import "$EXT" --source=spikes/gateway/yaml/ext.yaml --location "$REGION" --project "$PROJECT"
  run gcloud network-security authz-policies import "$POL" --source=spikes/gateway/yaml/policy.yaml --location "$REGION" --project "$PROJECT"
}

stage_hosts() {
  say "register ${#HOSTS[@]} hostnames in Agent Registry"
  for h in "${HOSTS[@]}"; do
    run gcloud agent-registry services create "pbgw-$(slug "$h")" --project "$PROJECT" --location "$REGION" --display-name "$h" \
      --endpoint-spec-type=no-spec --interfaces="url=https://$h,protocolBinding=jsonrpc" || echo "    (failed or already exists: $h)"
  done
  echo "  Cloud SQL: the proxy connects to the instance IP on 3307: NOT expressible as a hostname here; see what the smoke test says."
}

stage_cert() {
  say "gateway root CA -> deploy/extra-ca/agw.crt (git-ignored)"
  [ -n "$DRY_RUN" ] && { echo "  (dry run)"; return; }
  gcloud network-services agent-gateways describe "$GW" --location "$REGION" --project "$PROJECT" \
    --format="value[delimiter=\n](agentGatewayCard.rootCertificates)" | sed 's/\\n/\n/g' > deploy/extra-ca/agw.crt
  grep -c "BEGIN CERTIFICATE" deploy/extra-ca/agw.crt | sed 's/^/  certificates found: /'
  local fp; fp=$(sha256sum deploy/extra-ca/agw.crt | cut -c1-16); state_set cert_fp "$fp"
  if [ "$fp" = "$(state_get built_fp)" ]; then echo "  CA unchanged since the last build ($fp): no rebuild needed"; else echo "  CA differs from the one in the current image: run 'build' ($fp)"; fi
}

stage_build() {
  say "build $IMAGE (with the CA in deploy/extra-ca)"; ls deploy/extra-ca
  run gcloud builds submit . --tag "$IMAGE" --region "$REGION" --project "$PROJECT"
  [ -n "$DRY_RUN" ] || state_set built_fp "$(sha256sum deploy/extra-ca/agw.crt | cut -c1-16)"
}

stage_deploy() {
  local mode="${1:-}" extra=()
  case "$mode" in
    bootstrap) extra=(--env STATE_BACKEND=memory --env DATABASE_URL= --env SQL_CONNECTION= --env "PINBOARD_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')") ;;
    recreate) extra=(--recreate) ;;
    *) echo "usage: gateway.sh deploy bootstrap | recreate"; exit 2 ;;
  esac
  say "runtime pinboard-gw ($mode): AGENT_IDENTITY + gateway"
  run uv run python deploy/runtime.py apply --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
    --service-account "pinboard-runtime@$PROJECT.iam.gserviceaccount.com" --sql-connection "$PROJECT:$REGION:$SQLP" --bucket "$BUCKET" \
    --display-name pinboard-gw --state-file "$STATE" --agent-gateway "$GW_PATH" --psc-attachment "projects/$PROJECT/regions/$REGION/networkAttachments/$NA" --env "DATABASE_URL=secret://$DBSEC" --env SQL_PRIVATE_IP=true "${extra[@]}"
  [ -n "$DRY_RUN" ] && return
  local name; name=$(state_get runtime); echo "  runtime: $name"
  say "its identity (needed for 'grant')"
  uv run python - "$name" "$REGION" <<'PY'
import json, sys, google.auth
from google.auth.transport.requests import AuthorizedSession
creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
r = AuthorizedSession(creds).get(f"https://{sys.argv[2]}-aiplatform.googleapis.com/v1/{sys.argv[1]}", timeout=60)
d = r.json(); spec = d.get("spec", {})
print(json.dumps({k: spec.get(k) for k in ("effectiveIdentity", "identityType", "serviceAccount")}, indent=2))
print("(whole spec keys:", sorted(spec), ")")
PY
}

stage_grant() {
  local num; num=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
  # the trust domain is read from the bootstrap runtime itself (it is org-based for projects inside an organization, project-based otherwise)
  local td="${PB_TRUST_DOMAIN:-$(state_get trust_domain)}"
  [ -n "$td" ] || td=$(uv run python - "$(state_get runtime)" "$REGION" <<'PY'
import sys, google.auth
from google.auth.transport.requests import AuthorizedSession
creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
spec = AuthorizedSession(creds).get(f"https://{sys.argv[2]}-aiplatform.googleapis.com/v1/{sys.argv[1]}", timeout=60).json().get("spec", {})
print((spec.get("effectiveIdentity") or "").split("/")[0])
PY
)
  [ -n "$td" ] || { echo "could not read the runtime's identity: run 'deploy bootstrap' first, or set PB_TRUST_DOMAIN=agents.global.org-<ORG>.system.id.goog" >&2; exit 1; }
  state_set trust_domain "$td"
  local id="${1:-principalSet://$td/attribute.platformContainer/aiplatform/projects/$num}"
  say "give $id what the runtime service account has"
  for agent in gcp-sa-aiplatform gcp-sa-aiplatform-re; do   # the platform must be allowed to put a network interface into the attachment's subnet
    runq gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:service-$num@$agent.iam.gserviceaccount.com" --role=roles/compute.networkAdmin --condition=None --quiet || echo "    (failed: $agent)"
  done
  for role in roles/aiplatform.user roles/secretmanager.secretAccessor roles/cloudsql.client roles/logging.logWriter roles/serviceusage.serviceUsageConsumer roles/aiplatform.agentDefaultAccess roles/agentregistry.viewer; do
    runq gcloud projects add-iam-policy-binding "$PROJECT" --member="$id" --role="$role" --condition=None --quiet || echo "    (failed: $role)"
  done
  runq gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="$id" --role=roles/storage.objectAdmin
  for h in "${HOSTS[@]}"; do
    # the IAP "endpoint id" is the last part of the registry service's registryResource, not the service name
    local ep; ep=$(gcloud agent-registry services describe "pbgw-$(slug "$h")" --location "$REGION" --project "$PROJECT" --format='value(registryResource)' 2>/dev/null | sed 's#.*/##')
    if [ -z "$ep" ]; then echo "    (not registered, run 'hosts' first: $h)"; continue; fi
    runq gcloud iap web add-iam-policy-binding --resource-type=agent-registry --endpoint="$ep" --region "$REGION" --project "$PROJECT" \
      --member="$id" --role=roles/iap.egressor || echo "    (failed: $h)"
  done
}

stage_smoke() { say "smoke against the spike runtime"; run uv run python scripts/smoke_runtime.py --all --runtime "$(state_get runtime)"; }

stage_enforce() {
  say "enforce (turns DRY_RUN off)"
  sed -i '/iamEnforcementMode/d' spikes/gateway/yaml/ext.yaml
  run gcloud beta service-extensions authz-extensions import "$EXT" --source=spikes/gateway/yaml/ext.yaml --location "$REGION" --project "$PROJECT"
}

stage_down_vpc() {
  say "delete the private database and the VPC pieces (the gateway, hosts and grants stay)"
  run gcloud sql instances delete "$SQLP" --project "$PROJECT" --quiet || true
  run gcloud secrets delete "$DBSEC" --project "$PROJECT" --quiet || true
  run gcloud compute network-attachments delete "$NA" --region "$REGION" --project "$PROJECT" --quiet || true
  run gcloud services vpc-peerings delete --service=servicenetworking.googleapis.com --network "$NET" --project "$PROJECT" --quiet || true
  run gcloud compute addresses delete "$PSA_RANGE" --global --project "$PROJECT" --quiet || true
  run gcloud compute networks subnets delete "$SUBNET" --region "$REGION" --project "$PROJECT" --quiet || true
  run gcloud compute networks delete "$NET" --project "$PROJECT" --quiet || true
}

stage_down() {
  say "delete the spike"
  local rt; rt=$(state_get runtime)
  [ -n "$rt" ] && run uv run python deploy/runtime.py delete --project "$PROJECT" --region "$REGION" --state-file "$STATE" || true
  for h in "${HOSTS[@]}"; do run gcloud agent-registry services delete "pbgw-$(slug "$h")" --project "$PROJECT" --location "$REGION" --quiet || true; done
  for pol in "$POL" "$OLD_POL"; do run gcloud network-security authz-policies delete "$pol" --location "$REGION" --project "$PROJECT" --quiet || true; done
  run gcloud beta service-extensions authz-extensions delete "$EXT" --location "$REGION" --project "$PROJECT" --quiet || true
  for gw in "$GW" "$OLD_GW"; do run gcloud network-services agent-gateways delete "$gw" --location "$REGION" --project "$PROJECT" --quiet || true; done
  run gcloud sql instances delete "$SQLP" --project "$PROJECT" --quiet || true
  run gcloud sql databases delete pinboard_gw --instance "$SQL" --project "$PROJECT" --quiet || true   # round 1
  run gcloud secrets delete "$DBSEC" --project "$PROJECT" --quiet || true
  run gcloud secrets delete pinboard-gw-database-url --project "$PROJECT" --quiet || true            # round 1
  run gcloud compute network-attachments delete "$NA" --region "$REGION" --project "$PROJECT" --quiet || true
  run gcloud services vpc-peerings delete --service=servicenetworking.googleapis.com --network "$NET" --project "$PROJECT" --quiet || true
  run gcloud compute addresses delete "$PSA_RANGE" --global --project "$PROJECT" --quiet || true
  run gcloud compute networks subnets delete "$SUBNET" --region "$REGION" --project "$PROJECT" --quiet || true
  run gcloud compute networks delete "$NET" --project "$PROJECT" --quiet || true
  rm -f deploy/extra-ca/agw.crt
  echo "  (rebuild the normal image without the CA: deploy/deploy.sh build)"
}

case "${1:-}" in
  apis|vpc|sql|gateway|hosts|cert|build|grant|deploy|smoke|enforce|down-vpc|down) c=$1; shift; "stage_${c//-/_}" "$@" ;;
  *) sed -n 2,22p "$0"; exit 2 ;;
esac
