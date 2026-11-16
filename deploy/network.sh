#!/usr/bin/env bash
# Private networking and egress control for the engine. Run from anywhere:   pinboard-agent/deploy/network.sh <command>     (docs/EGRESS.md)
#
#   up         a private network, the attachment the engine joins, a Secure Web Proxy with a host allowlist, Cloud NAT for the proxy only,
#              and the range Cloud SQL needs for a private address                                  (the proxy + NAT are BILLED per hour)
#   allowlist  re-apply the host allowlist to the proxy (after changing egress_allowed_hosts or mcp.allowed_hosts)
#   status     what exists right now
#   hosts      print the hosts the proxy lets through (no cloud call)
#   down       delete everything `up` created. The Cloud SQL instance and the engine are NOT touched: switch them back first
#
# `up` changes nothing about the running engine. To use the network, put the two lines it prints into deploy/config.local.yaml
# (network_attachment, egress_proxy), make the database private (the command it prints), then `deploy.sh deploy --recreate`.
# Every command is idempotent. DRY_RUN=1 prints what would be run and changes nothing.
# MCP servers: `up` also reserves the block of mcp.vpc_ranges (config/policy.yaml) as a subnet <name>-mcp-subnet, opens it to the engine's subnet only
# (firewall <name>-allow-agent-to-mcp, for machines tagged <name>-mcp) and, for each network_dns_domains entry, creates a private DNS zone you add records to.
# Names derive from `name` in deploy/config.yaml:  <name>-net, <name>-na, <name>-egress-gw, <name>-egress-policy, <name>-router, <name>-nat
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v uv >/dev/null 2>&1; then PYRUN=(uv run python); else PYRUN=(.venv/bin/python); fi
eval "$("${PYRUN[@]}" deploy/deploy_config.py shell)" || exit 2
DRY_RUN="${DRY_RUN:-}"
NET="$NAME-net"; AGENT_SUBNET="$NAME-agent-subnet"; ATTACHMENT="$NAME-na"; PROXY_SUBNET="$NAME-proxy-subnet"; GW_SUBNET="$NAME-egress-subnet"
ROUTER="$NAME-router"; NAT="$NAME-nat"; POLICY="$NAME-egress-policy"; GW="$NAME-egress-gw"; RULE="allow-listed-hosts"; PSA_RANGE="$NAME-psa-range"
AGENT_RANGE="192.168.10.0/28"    # the engine's own addresses (a /28 is the minimum for an attachment)
PROXY_RANGE="192.168.20.0/24"    # the proxy's machines (managed by Google)
GW_RANGE="192.168.30.0/24"       # the proxy's address
GW_IP="192.168.30.10"
MCP_SUBNET="$NAME-mcp-subnet"; MCP_FW="$NAME-allow-agent-to-mcp"; MCP_TAG="$NAME-mcp"
MCP_RANGE="$("${PYRUN[@]}" deploy/egress.py vpc_range | head -1)"   # one range is built; the policy may name more
YAML_DIR="$(mktemp -d)"; trap 'rm -rf "$YAML_DIR"' EXIT

say() { printf '\n== %s\n' "$*"; }
run() { printf '  $ %s\n' "$*"; if [ -z "$DRY_RUN" ]; then "$@"; fi; }
exists() { [ -z "$DRY_RUN" ] && "$@" >/dev/null 2>&1; }
need_project() { [ -n "$PROJECT" ] || { echo "no project: set PB_PROJECT=... or run gcloud config set project ..." >&2; exit 2; }; }
[ -n "${PROJECT:-}" ] || need_project

write_yaml() {
  local matcher
  matcher=$("${PYRUN[@]}" deploy/egress.py matcher) || exit 2
  cat > "$YAML_DIR/policy.yaml" <<EOF
name: projects/$PROJECT/locations/$REGION/gatewaySecurityPolicies/$POLICY
description: $NAME egress allowlist (deploy/egress.py)
EOF
  cat > "$YAML_DIR/rule.yaml" <<EOF
name: projects/$PROJECT/locations/$REGION/gatewaySecurityPolicies/$POLICY/rules/$RULE
description: only these hosts; everything else is refused
enabled: true
priority: 1
basicProfile: ALLOW
sessionMatcher: $matcher
EOF
  cat > "$YAML_DIR/gateway.yaml" <<EOF
name: projects/$PROJECT/locations/$REGION/gateways/$GW
type: SECURE_WEB_GATEWAY
ports: [443]
routingMode: EXPLICIT_ROUTING_MODE
addresses: ["$GW_IP"]
network: projects/$PROJECT/global/networks/$NET
subnetwork: projects/$PROJECT/regions/$REGION/subnetworks/$GW_SUBNET
gatewaySecurityPolicy: projects/$PROJECT/locations/$REGION/gatewaySecurityPolicies/$POLICY
scope: $NAME-egress
EOF
}

cmd_hosts() { "${PYRUN[@]}" deploy/egress.py hosts; }

cmd_allowlist() {
  say "host allowlist"
  write_yaml
  cmd_hosts | sed 's/^/    /'
  run gcloud network-security gateway-security-policies rules import "$RULE" --source "$YAML_DIR/rule.yaml" --location "$REGION" \
      --gateway-security-policy "$POLICY" --project "$PROJECT"
}

cmd_up() {
  say "private network"
  exists gcloud compute networks describe "$NET" --project "$PROJECT" || run gcloud compute networks create "$NET" --subnet-mode=custom --project "$PROJECT"
  exists gcloud compute networks subnets describe "$AGENT_SUBNET" --region "$REGION" --project "$PROJECT" || \
    run gcloud compute networks subnets create "$AGENT_SUBNET" --network "$NET" --range="$AGENT_RANGE" --region "$REGION" --enable-private-ip-google-access --project "$PROJECT"
  exists gcloud compute network-attachments describe "$ATTACHMENT" --region "$REGION" --project "$PROJECT" || \
    run gcloud compute network-attachments create "$ATTACHMENT" --region "$REGION" --subnets "$AGENT_SUBNET" --connection-preference=ACCEPT_AUTOMATIC --project "$PROJECT"

  say "proxy subnets, router and NAT (NAT covers ONLY the proxy's subnet: the engine itself gets no direct internet)"
  exists gcloud compute networks subnets describe "$PROXY_SUBNET" --region "$REGION" --project "$PROJECT" || \
    run gcloud compute networks subnets create "$PROXY_SUBNET" --network "$NET" --range="$PROXY_RANGE" --region "$REGION" --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE --project "$PROJECT"
  exists gcloud compute networks subnets describe "$GW_SUBNET" --region "$REGION" --project "$PROJECT" || \
    run gcloud compute networks subnets create "$GW_SUBNET" --network "$NET" --range="$GW_RANGE" --region "$REGION" --project "$PROJECT"
  exists gcloud compute routers describe "$ROUTER" --region "$REGION" --project "$PROJECT" || \
    run gcloud compute routers create "$ROUTER" --network "$NET" --region "$REGION" --project "$PROJECT"
  exists gcloud compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT" || \
    run gcloud compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" --auto-allocate-nat-external-ips --nat-custom-subnet-ip-ranges="$PROXY_SUBNET" --project "$PROJECT"

  if [ -n "$MCP_RANGE" ]; then
    say "a subnet for MCP servers ($MCP_RANGE): reachable from the engine, with no route to the internet"
    exists gcloud compute networks subnets describe "$MCP_SUBNET" --region "$REGION" --project "$PROJECT" || \
      run gcloud compute networks subnets create "$MCP_SUBNET" --network "$NET" --range="$MCP_RANGE" --region "$REGION" --enable-private-ip-google-access --project "$PROJECT"
    exists gcloud compute firewall-rules describe "$MCP_FW" --project "$PROJECT" || \
      run gcloud compute firewall-rules create "$MCP_FW" --network "$NET" --direction=INGRESS --action=ALLOW --rules=tcp --source-ranges="$AGENT_RANGE" --target-tags="$MCP_TAG" --project "$PROJECT"
  else
    echo "  (mcp.vpc_ranges is empty in the policy: no subnet for MCP servers is created)"
  fi
  for d in $NETWORK_DNS_DOMAINS; do
    zone="$NAME-zone-$(echo "$d" | tr -c 'a-z0-9\n' '-' | sed 's/-*$//')"
    say "private DNS zone $d (add A records for your MCP servers; the engine resolves it through DNS peering)"
    exists gcloud dns managed-zones describe "$zone" --project "$PROJECT" || \
      run gcloud dns managed-zones create "$zone" --dns-name="$d" --visibility=private --networks="$NET" --description="$NAME MCP server names" --project "$PROJECT"
  done
  if [ -n "$NETWORK_DNS_DOMAINS" ]; then   # the platform's service agent needs this to look names up in your zone
    local num; num=$([ -n "$DRY_RUN" ] && echo "<PROJECT_NUMBER>" || gcloud projects describe "$PROJECT" --format='value(projectNumber)')
    for agent in gcp-sa-aiplatform gcp-sa-aiplatform-re; do   # the deploy was refused until the first one had it ("the Vertex AI service account has the dns.peer role")
      run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:service-$num@$agent.iam.gserviceaccount.com" --role=roles/dns.peer --condition=None --quiet
    done
  fi

  say "proxy with a host allowlist"
  write_yaml
  exists gcloud network-security gateway-security-policies describe "$POLICY" --location "$REGION" --project "$PROJECT" || \
    run gcloud network-security gateway-security-policies import "$POLICY" --source "$YAML_DIR/policy.yaml" --location "$REGION" --project "$PROJECT"
  cmd_allowlist
  exists gcloud network-services gateways describe "$GW" --location "$REGION" --project "$PROJECT" || \
    run gcloud network-services gateways import "$GW" --source "$YAML_DIR/gateway.yaml" --location "$REGION" --project "$PROJECT"

  say "range for a private Cloud SQL address (free)"
  exists gcloud compute addresses describe "$PSA_RANGE" --global --project "$PROJECT" || \
    run gcloud compute addresses create "$PSA_RANGE" --global --purpose=VPC_PEERING --prefix-length=24 --network "$NET" --project "$PROJECT"
  run gcloud services vpc-peerings connect --service=servicenetworking.googleapis.com --ranges="$PSA_RANGE" --network "$NET" --project "$PROJECT"

  cat <<EOF

== done. To use it:
  1. deploy/config.local.yaml:
       network_attachment: projects/$PROJECT/regions/$REGION/networkAttachments/$ATTACHMENT
       egress_proxy: http://$GW_IP:443
       sql_private_ip: true
$([ -n "$NETWORK_DNS_DOMAINS" ] && echo "       network_dns_domains: [$NETWORK_DNS_DOMAINS]")
  2. give the database a private address and remove its public one (RESTARTS the instance):
       gcloud sql instances patch $SQL_INSTANCE --network projects/$PROJECT/global/networks/$NET --no-assign-ip --project $PROJECT
  3. deploy/deploy.sh deploy --recreate        (a runtime cannot be moved onto a network after it was created)
EOF
}

cmd_status() {
  gcloud compute networks list --project "$PROJECT" --filter="name=$NET" 2>&1 || true
  gcloud network-services gateways list --location "$REGION" --project "$PROJECT" --filter="name~$GW" 2>&1 || true
  gcloud compute routers list --project "$PROJECT" --filter="name=$ROUTER" 2>&1 || true
  gcloud compute network-attachments list --project "$PROJECT" --filter="name=$ATTACHMENT" 2>&1 || true
}

cmd_down() {
  say "deleting the network pieces of '$NAME' (the Cloud SQL instance and the engine are not touched)"
  if exists gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT" && \
     [ "$(gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT" --format='value(settings.ipConfiguration.privateNetwork)')" != "" ]; then
    echo "  the Cloud SQL instance $SQL_INSTANCE still uses this network: make it public again or delete it first" >&2
    exit 1
  fi
  exists gcloud network-services gateways describe "$GW" --location "$REGION" --project "$PROJECT" && run gcloud network-services gateways delete "$GW" --location "$REGION" --project "$PROJECT" --quiet
  exists gcloud network-security gateway-security-policies rules describe "$RULE" --gateway-security-policy "$POLICY" --location "$REGION" --project "$PROJECT" && \
    run gcloud network-security gateway-security-policies rules delete "$RULE" --gateway-security-policy "$POLICY" --location "$REGION" --project "$PROJECT" --quiet
  exists gcloud network-security gateway-security-policies describe "$POLICY" --location "$REGION" --project "$PROJECT" && \
    run gcloud network-security gateway-security-policies delete "$POLICY" --location "$REGION" --project "$PROJECT" --quiet
  exists gcloud compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT" && run gcloud compute routers nats delete "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT" --quiet
  exists gcloud compute routers describe "$ROUTER" --region "$REGION" --project "$PROJECT" && run gcloud compute routers delete "$ROUTER" --region "$REGION" --project "$PROJECT" --quiet
  exists gcloud compute network-attachments describe "$ATTACHMENT" --region "$REGION" --project "$PROJECT" && run gcloud compute network-attachments delete "$ATTACHMENT" --region "$REGION" --project "$PROJECT" --quiet
  run gcloud services vpc-peerings delete --service=servicenetworking.googleapis.com --network "$NET" --project "$PROJECT" --quiet || true
  exists gcloud compute addresses describe "$PSA_RANGE" --global --project "$PROJECT" && run gcloud compute addresses delete "$PSA_RANGE" --global --project "$PROJECT" --quiet
  exists gcloud compute firewall-rules describe "$MCP_FW" --project "$PROJECT" && run gcloud compute firewall-rules delete "$MCP_FW" --project "$PROJECT" --quiet
  for d in $NETWORK_DNS_DOMAINS; do
    zone="$NAME-zone-$(echo "$d" | tr -c 'a-z0-9\n' '-' | sed 's/-*$//')"
    if exists gcloud dns managed-zones describe "$zone" --project "$PROJECT"; then
      run gcloud dns managed-zones delete "$zone" --project "$PROJECT" || echo "  (the zone $zone still has records you added: delete them, then run this again)"
    fi
  done
  for s in "$MCP_SUBNET" "$GW_SUBNET" "$PROXY_SUBNET" "$AGENT_SUBNET"; do
    exists gcloud compute networks subnets describe "$s" --region "$REGION" --project "$PROJECT" && run gcloud compute networks subnets delete "$s" --region "$REGION" --project "$PROJECT" --quiet
  done
  exists gcloud compute networks describe "$NET" --project "$PROJECT" && run gcloud compute networks delete "$NET" --project "$PROJECT" --quiet
  return 0
}

case "${1:-help}" in
  up) cmd_up ;;
  allowlist) cmd_allowlist ;;
  status) cmd_status ;;
  hosts) cmd_hosts ;;
  down) cmd_down ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
