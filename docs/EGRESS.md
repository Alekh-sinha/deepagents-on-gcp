# Egress control without Agent Gateway (optional, off by default)

**Goal:** the engine (the process inside Agent Runtime that runs the agent and holds the Postgres connection) can reach only the third-party hosts you list. It keeps the Postgres checkpointer,
which Agent Gateway's egress mode could not carry (see the README). It is config-driven: nothing here is switched on unless `deploy/config*.yaml` says so, and with the settings empty the deployment is exactly what it was. A fresh clone therefore gets no enforcement until `deploy/network.sh up` has built the pieces and the settings are in place. It was verified on this project's own deployment (see below).

## How it works

```
Agent Runtime (engine)
   │  PSC interface: the engine's outbound traffic enters YOUR VPC
   ▼
 your VPC ──► Google APIs (Vertex, Secret Manager, Cloud Storage, Cloud SQL Admin, ...)   direct, private access, NOT filtered by host
   │      ──► Cloud SQL, private address only                                             direct, inside the VPC
   │      ──► Secure Web Proxy (explicit proxy, host allowlist) ──► Cloud NAT ──► the internet
   │                                          only api.tavily.com, api.github.com, api.agentmail.to + MCP hosts on the approved list
   ✗ everything else: no route (the engine's own subnet has no NAT, so it cannot skip the proxy)
```

* The engine is told about the proxy through `HTTPS_PROXY` / `HTTP_PROXY`; `NO_PROXY` keeps Google APIs, the metadata server, localhost and the link-local range and the private ranges (`169.254/16`, `10/8`, `172.16/12`, `192.168/16`) off it.
* Cloud NAT is attached to the **proxy's** subnet only. If it covered the engine's subnet, the engine could ignore the proxy and go straight out.
* The allowlist has one source: the connectors' hosts built into `deploy/egress.py`, `mcp.allowed_hosts` in `config/policy.yaml`, and `egress_allowed_hosts` in the deploy config.
  Adding a public host therefore means editing the policy (the app checks it), redeploying the engine (the policy is baked into the image) **and** running `deploy/network.sh allowlist` (the network enforces it).
  That is deliberate: nobody can connect a new third party from the UI alone.

## MCP servers: where they can live

A user adds MCP servers in the UI (Connectors). With `mcp.scope: vpc_or_allowlist` (the shipped policy) a server is accepted **only** if:

1. **It is inside the private network:** every address its host name resolves to lies in `mcp.vpc_ranges` (shipped: `192.168.40.0/24`, a subnet reserved for MCP servers). It is then assumed approved, and plain `http` is fine. The engine contacts it directly, never through the proxy.
2. **or its host is on the approved list** (`mcp.allowed_hosts`): a public host, `https` only, reached through the proxy.

Anything else is refused when the user adds it, with a message that names the range and the list (the Connectors page shows the same text, taken from the engine's `get_config`). An empty list means no public host at all.
Two rules keep "inside the network" from becoming a hole: only the reserved range counts (the database, the proxy and the rest of the VPC do not), and the address is looked up again **on every call**, so a name that is later
pointed elsewhere, or a policy that is tightened after a server was saved, takes effect at once. Each tool call still asks the person first unless they marked that tool safe: registration approval and per-call approval are separate.

`deploy/network.sh up` builds this from the policy: the subnet for `mcp.vpc_ranges`, a firewall rule that lets only the engine's subnet reach machines tagged `<name>-mcp`, and a private DNS zone per `network_dns_domains`
(the engine resolves those names through DNS peering, so `http://calc.mcp.internal:8000/mcp` works by name as well as by address). Machines in that subnet have no route to the internet (Cloud NAT covers only the proxy).
Put a machine in the subnet, tag it `<name>-mcp`, add an A record to the zone, and users can connect to it.

**Verified (November 2026, real engine):** a small MCP server on a VM in that subnet was added by name and by address and listed its tools; a real Gemini turn called both of its tools (the "mcp" kind was auto-approved);
a public host, the database address, the proxy address, the metadata server and `localhost` were all refused; an in-range address with nothing behind it was accepted and reported as unreachable.

## Turn it on

```bash
deploy/network.sh up           # network, attachment, proxy, NAT, range for private SQL (proxy + NAT are billed per hour); prints what to do next
# deploy/config.local.yaml:
#   network_attachment: projects/P/regions/R/networkAttachments/NAME
#   egress_proxy: http://192.168.30.10:443
#   sql_private_ip: true
gcloud sql instances patch <name>-pg --network projects/P/global/networks/<name>-net --no-assign-ip   # RESTARTS the database
deploy/deploy.sh deploy --recreate    # a runtime cannot be moved onto a network after it was created
```

Terraform reads the same three settings (`psc_interface_config`, the proxy variables and `SQL_PRIVATE_IP`). `deploy/network.sh down` removes the network pieces (not the database, not the engine).

## What was verified (November 2026, our project, us-central1)

On a real Agent Runtime deployment with a PSC interface, a Secure Web Proxy and Cloud NAT for the proxy's subnet only:

| Target | Direct from the engine | Through the proxy |
|---|---|---|
| `api.tavily.com`, `api.github.com` (on the allowlist) | no path (timeout) | HTTP 200 in about 0.1 s |
| `example.com`, `api.agentmail.to` (not on the list, in that test) | no path | refused by the proxy |
| Google APIs (Secret Manager, Cloud SQL Admin, Storage, Vertex regional and global, OAuth) | work | not used (`NO_PROXY`) |

The agent also reached its **private-IP Postgres** (LangGraph checkpoint written), answered through Gemini, and ran a real Tavily search with the key read from Secret Manager. Everything that test created was deleted afterwards.
Locally (`tests/agent/test_egress_proxy.py`, no cloud) every third-party client of the product is checked against a fake proxy: the GitHub connector, the AgentMail SDK, both MCP clients and the web search all use it.
That check found one problem, now fixed: Tavily's async client ignores proxy settings, so the search tool always uses its synchronous path (`SyncOnlySearch`).

## What it does NOT do

* **Google APIs stay open to everything IAM allows.** A compromised engine could still call a Google API on a resource in someone else's project (for example, write to a stranger's bucket). The network cannot tell your bucket from theirs.
  VPC Service Controls would close that; it is not set up or tested here.
* **Allowed hosts can still receive data.** The proxy filters by host name only (no TLS inspection), so anything sent to `api.tavily.com` (a search query) or `api.github.com` is allowed. The GitHub tools are further limited by policy
  (`agent/*` branches and pull requests only, repos of the App installation only, every write approved by a person).
* **Inbound calls are not covered.** (The web backend's Cloud Run service has ingress `all` with IAP in front: IAP is an identity check, not a network restriction, so "ingress only through Cloud Run" would be the wrong description.) A PSC interface only changes the engine's *outbound* traffic. Callers (the web backend and Cloud Scheduler) still reach the engine through the Vertex API, controlled by IAM.
  An Agent Gateway ingress could govern that, but a runtime cannot have a gateway and a PSC interface together (the API refuses it). The beta Terraform provider also lists `dedicated_ingress_endpoint_enabled`; we have not looked into it.
* **Found while verifying this:** `httpx` (used by the MCP client) does not understand address ranges in `NO_PROXY`, and a host name can never match a range, so every server in the VPC was first sent to the proxy and refused. The MCP client now decides itself (`goes_direct`).
* **Verified on the live engine since the first version of this page:** the Go Cloud SQL proxy with `--private-ip` inside the real runtime (the engine starts, opens its state and passes the full smoke test on the private address), and the move of the existing
  production database to a private address. The database kept its public address as well, with no authorized networks (only the Cloud SQL connector and Auth Proxy, authenticated with IAM, can use it; adding the private one took about 16 minutes and restarted the instance once); removing the public address is a separate step you may want to take.
* **Still not verified:** VPC Service Controls (to restrict inbound calls and Google API destinations), probing blocked hosts from inside the live engine (the product cannot make arbitrary requests), and the price of the proxy and NAT over a month.
* A client that ignores the proxy settings does not leak; it fails to connect (there is no other route). A new third-party library should be added to `tests/agent/test_egress_proxy.py`.

## Cost

Secure Web Proxy and Cloud NAT bill per hour while they exist, and a private Cloud SQL address needs the peering range (free). Check the current prices before leaving it running; `deploy/network.sh down` removes it.
