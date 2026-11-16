# Pinboard: a LangGraph agent on Google Cloud

Pinboard is a personal AI assistant. It researches, writes files, sends e-mail and pushes code to GitHub, and **every side effect waits for a human decision**. It is built on deepagents / LangGraph with Gemini on Vertex AI.
This repository also records how it is deployed on Google Cloud, and why: the managed runtime, the web front door, state in Postgres, private networking and egress control, human approval, the deployment scripts and Terraform, and the tests.
Where a statement rests on Google's documentation and was not tested here, the page says so.

The page follows the order of the work. **[The Google Cloud way](#the-google-cloud-way)**: what the platform offers an agent. **[Problems we ran into](#problems-we-ran-into)**: where that path hurt, including a full write-up of why Agent Gateway did not work for us.
**[The design we ended up with](#the-design-we-ended-up-with)**: what runs today. **[Why it is built this way](#why-it-is-built-this-way)**: each part explained (Cloud Run, state, egress, identity, sandbox, guardrails, deployment).
Then **[what is in place, and what is not](#what-is-in-place-and-what-is-not)**, [how to run it](#how-to-run-it) and [how to put your own agent in it](#how-to-add-your-own-agent).

Original plan: **[PLAN.md](PLAN.md)** · how to work on the repo (commands, rules, platform facts): **[CLAUDE.md](CLAUDE.md)** · network egress in depth: **[docs/EGRESS.md](docs/EGRESS.md)**.

## The Google Cloud way

Google's managed path for an agent has three layers: **run** it (Agent Runtime), put a **front door** on it (Cloud Run with IAP), and **govern** it (Agent Registry, Agent Gateway, agent identity). The services around them are the usual ones.

| You need | Google Cloud service | What it gives you | In this project |
|---|---|---|---|
| Run the agent | **Agent Runtime** (Vertex AI) | A managed runtime: callers are authenticated by IAM, `:query` and `:streamQuery` endpoints, autoscaling between a minimum and maximum number of instances. You hand it either a Python agent object (Google's LangGraph page) or **your own container** that follows the runtime contract | Own container: we need our own HTTP API, a database proxy next to the app, and fixed dependencies |
| The model | **Vertex AI** (Gemini) | Models through the same project and IAM | Yes. Newer Gemini models are served only from the `global` location |
| Agent state | **Cloud SQL for PostgreSQL** (LangGraph checkpointer); Agent Runtime Sessions / Memory Bank | Postgres is what Google's own LangGraph page uses. Sessions and Memory Bank are Google's ADK model, not a LangGraph checkpointer | Postgres. Memory Bank is not usable as a checkpointer |
| A front door for people | **Cloud Run + IAP** | Google sign-in and access by identity, not by network | Yes: the web backend |
| Secrets | **Secret Manager** | Secrets read by name, with IAM | Yes |
| Build and ship | **Cloud Build**, **Artifact Registry** | Image build and storage | Yes |
| Recurring work | **Cloud Scheduler** | Calls an HTTP endpoint on a schedule | Yes: housekeeping every 5 minutes |
| Run model-written code | **Agent Platform sandboxes** | Disposable remote Linux machines | Yes, with cost controls |
| Govern traffic and identity | **Agent Gateway**, **Agent Registry**, agent identity | Central allowlists and default-deny egress for many agents | **Tried, not used** (see below) |
| Private networking | **VPC**, **Private Service Connect interface**, **Secure Web Proxy**, **Cloud NAT**, **Cloud DNS** | Put the agent's outbound traffic inside your own network and control it | Yes: this is how egress is controlled here |
| Infrastructure as code | `gcloud`, the Terraform provider (`google_vertex_ai_reasoning_engine`) | Repeatable deployments | Both: `deploy/deploy.sh` and `terraform/` |

Two things are deployed: the **engine** (on Agent Runtime) and the **web backend** (on Cloud Run). The React app runs on your computer today and talks to the web backend.

## Problems we ran into

### The big one: Agent Gateway and a Postgres checkpointer

Google's documentation presents **Agent Gateway** as the way to govern an agent's traffic: one place with central rules (an allowlist of the hosts an agent may call, default-deny
egress, agent identities) for many agents, instead of rules scattered inside each agent's code. That is exactly what we wanted for this project, so we tried it first, in this order:
our own engine, then Google's own documented LangGraph example, then a lab of variations.

**What we found: an egress gateway and a direct database connection do not mix.** A LangGraph agent whose checkpointer is Postgres (Cloud SQL) deploys fine on Agent Runtime.
Attach an egress Agent Gateway and the deployment fails with an SSL handshake error on the database connection, in every configuration we tried (public IP, private IP with VPC egress,
the Cloud SQL Auth Proxy that our engine uses, and the Cloud SQL Python connector that Google's own example uses).

| Experiment (details in the notebook) | Result |
|---|---|
| No gateway, Cloud SQL checkpointer (Google's example, and this engine) | **Works** |
| Egress + ingress gateways, Cloud SQL public IP | **Fails** at the database socket: `SSLV3_ALERT_HANDSHAKE_FAILURE` |
| Same, with the token-binding opt-out variable | **Fails** the same way |
| Egress gateway with VPC egress (PSC attachment), Cloud SQL private IP only | **Fails** the same way (the traffic still seems to be intercepted; this part is an inference from network probes) |
| **Ingress-only** gateway, plus `GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES=false` | **Works** (tried on Google's example agent, not on Pinboard itself). The variable weakens protection against stolen tokens: a learning aid, not a production default |
| Egress gateway + a **Firestore** checkpointer (a Google API over HTTPS/gRPC) | **Works** (a small reference saver written for the lab: documents are capped at about 1 MiB) |
| Agent Runtime Sessions / Memory Bank as the checkpointer | Not possible: they are Google's ADK session model, not a LangGraph checkpointer |

Other things we learned while trying (from our own spikes, so they are about our setup): gRPC does not get through the gateway's proxy, so the Secret Manager client had to switch to REST
(`gcp/vault.py`); a runtime that uses an agent identity cannot also have a service account, so permissions go to the identity instead; agent-identity runtimes call the `*.mtls.googleapis.com`
endpoints; the allowlist is one Agent Registry entry per exact host name (no wildcards); a runtime can use the gateway **or** its own VPC interface, never both (the API refuses the combination).

**What we do instead, and run today:** keep the Postgres checkpointer and control egress with Google's network building blocks: the engine joins your VPC through a **Private Service Connect interface**,
its only way to third-party hosts is an **explicit proxy with a host allowlist**, and the database has a **private address**. The design and its limits are below and in [`docs/EGRESS.md`](docs/EGRESS.md).
If you want a gateway anyway you have three real options: (1) ingress-only governance; (2) keep egress governance and move the checkpointer to an HTTP-based store (the Firestore saver in the notebook, or a thin HTTPS service in front of Postgres);
(3) wait and re-run the lab, because Agent Gateway and Agent Runtime change quickly. These results are from October 2026, in one project and region.

**Run the lab yourself:** [`docs/notebooks/agent_gateway_checkpointer_lab_colab.ipynb`](docs/notebooks/agent_gateway_checkpointer_lab_colab.ipynb) (Google Colab). It builds every case above,
captures the failures, reads the engine logs and prints a diagnosis plus a results table. It needs a project **inside a Google Cloud organization**, one Colab secret (`DB_PASSWORD`),
and it creates billed resources (two Cloud SQL instances, gateways, a VPC) that its last section can delete: use a throwaway project, and set `PROJECT_ID` in its configuration cell first.

Documentation we used:

* [Route Agent Runtime traffic through Agent Gateway](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy)
* [Agent Gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview)
* [Set up VPC connectivity for Agent Gateway](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-vpc-connectivity)
* [Troubleshoot Agent Gateway connectivity](https://docs.cloud.google.com/gemini-enterprise-agent-platform/troubleshooting/troubleshoot-agent-gateway)
* [Authenticate agents with their own identity](https://docs.cloud.google.com/iam/docs/auth-agent-own-identity)
* [Agent identity and Context-Aware Access (why tokens are bound, and why a gateway changes that)](https://docs.cloud.google.com/access-context-manager/docs/caa-agent-security)
* **Where LangGraph is connected, in Google's own words:** [Create a LangGraph agent on Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/create-a-langgraph-agent)
  (the `checkpointer_builder` with Cloud SQL used in the lab; the page says nothing about a gateway)
* [The Agent Runtime contract](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract) ·
  [Deploy with Terraform](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-terraform) ·
  [Memory Bank](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank) ·
  [Using a Private Service Connect interface with Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/private-service-connect-interface) ·
  [Sandboxes overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sandbox)

### Everything else that bit us

Each of these cost real time. They are the reason the deploy tooling and the code look the way they do.

| What happened | Cause | What we did |
|---|---|---|
| A deploy fails with only "failed to deploy" | The platform hides the reason; it is in the runtime's own logs (Cloud Logging) | Read the logs first. Two deploys also failed with a bare `code 13` and worked on a retry (unexplained) |
| Requests arrive as a JSON string containing JSON | Agent Runtime encodes the body twice | `parse_call` decodes until it has an object |
| `max_per_turn: 5` was refused as "not a whole number" | Agent Runtime delivers **every JSON number as a float** (`5.0`) | Whole-number floats are accepted (found on the deployed engine, not locally) |
| A container deploy is refused | `agent_framework` is required (`langgraph` works) | Set in `deploy/runtime.py` |
| A new service account does not work for a few seconds | IAM propagation | The deploy script retries; the platform's own service agents also need roles (Artifact Registry reader; `dns.peer` for **both** Vertex service agents, or the DNS-peering deploy is refused) |
| Saving a file from the engine fails with a `serviceusage` error | Storage calls from the runtime are billed to the project | `roles/serviceusage.serviceUsageConsumer` on the runtime's service account |
| Calls of 15 minutes are killed with an empty error | The platform cuts unary calls (600 s worked, 900 s did not) | Turns run in a **background worker** and write an event log; clients follow the log and reconnect (below) |
| The first use of a sandbox takes 30 to 130 seconds | Sandbox creation | Lazy creation, one sandbox per conversation, and a capped warm pool |
| A LangGraph table vanished from the database | Someone deleted it by hand; LangGraph's setup trusts its migration marker and never recreates it | `repair_checkpoint_tables` runs at start-up and recreates what is missing |
| An engine cannot be moved onto a network later | A PSC interface is fixed when the runtime is created, and cannot be combined with a gateway | A **blue/green** deploy: build the new runtime next to the old one, test it, then switch the callers |
| Clients ignore the proxy settings | `requests` and Go understand address ranges in `NO_PROXY`; `httpx` does not; Tavily's async client ignores proxies altogether | The MCP client decides itself (private = direct); web search is always the sync client; a test with a fake proxy checks every client |
| IAP in front of Cloud Run rejects scripts | The JWT audience is `/projects/NUMBER/locations/REGION/services/NAME`; a script must sign a token whose audience is the service URL plus `/*`; anonymous requests get a bare 404 | The web backend verifies IAP's signed header; `scripts/smoke_web.py` tests it from outside |
| A newer Gemini model answers 404 | It is served only from the `global` location here | The model pool records a location per model |

## The design we ended up with

### The system, as deployed

```
  You, in a browser
  React app (frontend/): on your computer today
        │  HTTPS, same origin, CSRF header
        ▼
  ┌───────────────────────────────┐  verified e-mail = the user id; the browser never reaches the engine
  │ Cloud Run · web backend       │  IAP checks who you are (identity, not network)
  └──────────────┬────────────────┘
                 │ Vertex AI API (:query / :streamQuery), IAM, the backend's own service account
                 ▼
  ┌─────────────────────────────────────────────┐   Cloud Scheduler ─► `expire` every 5 min
  │ Agent Runtime · the engine (our container)  │   (housekeeping)
  └──────────────┬──────────────────────────────┘
                 │ Private Service Connect interface: the engine's OUTBOUND traffic enters your VPC (inbound is not affected)
  ┌──────────────▼──────────────────────────────────────────────────────────────────────────────┐
  │ your VPC                                                                                    │
  │   ├─► Cloud SQL (private address): Postgres, the LangGraph checkpointer and all state       │
  │   ├─► MCP subnet 192.168.40.0/24: your own tools, reached directly (by name via DNS peering)│
  │   ├─► Secure Web Proxy (host allowlist) ─► Cloud NAT ─► Tavily, GitHub, AgentMail           │
  │   └─► Google APIs (Private Google Access): Vertex AI, Secret Manager, Cloud Storage, the    │
  │        sandbox service                                          (limited by IAM only)       │
  │   no other route: the engine's own subnet has no NAT                                        │
  └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### One message, end to end

1. The browser sends the message to the **web backend** on Cloud Run. IAP has already verified the person; the backend passes the verified e-mail to the engine as the user id.
2. The **engine** creates a *turn*, returns at once, and a background worker takes a lease on it. The worker builds the agent (its checkpointer is in Postgres), runs it, and writes small events (tokens, tool calls, results) to an event log.
3. The browser follows the log through a live stream (server-sent events) that the backend relays. The stream can be resumed from any position, and the backend reconnects to the engine every 270 seconds.
4. When the agent wants a **side effect** (a shell command, an e-mail, a GitHub write, a plan) the graph pauses on an interrupt. The engine stores an *approval*, tells the browser, and the worker lets go: the turn is **parked**, possibly for hours.
5. The person decides. The engine resumes the agent **from the checkpoint in Postgres**, possibly on a different instance, and it carries on exactly where it stopped.
6. On the way, tools call out: Gemini, Secret Manager and Cloud Storage directly; Tavily, GitHub and AgentMail through the proxy; MCP servers inside the VPC directly; shell commands in a remote sandbox.
7. At the end of the turn, files the agent saved are copied to Cloud Storage and indexed; the last event closes the stream. Every five minutes Cloud Scheduler asks the engine to expire stale approvals, find workers that died, sweep idle sandboxes and fire scheduled tasks.

### Inside the engine

```
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ ENGINE · Vertex AI Agent Runtime (a container)                          src/pinboard/engine/  │
 │   api.py  the engine's methods     service.py  turns, approvals, park-and-resume              │
 │   worker.py  background workers + lease     events.py  the event log     sessions.py          │
 │   spare_pool.py  warm sandbox      schedules.py  cron jobs      mcp_servers.py  your MCP      │
 │   auto_approve.py  "approve all" rules                                                        │
 │  ┌────────────────────────────────────────────────────────────────────────────────────────┐  │
 │  │ AGENT · LangGraph + deepagents                                      src/pinboard/agent/ │  │
 │  │   builder.py assembles it   subagents.py researcher / coder / mailer   delegation.py    │  │
 │  │   tools/ web search, e-mail, GitHub, files   gates.py + approvals.py  what needs a human │  │
 │  │   models.py model choice   runner.py runs / resumes the graph   prompts.py   contract.py │  │
 │  └────────────────────────────────────────────────────────────────────────────────────────┘  │
 └───┬──────────────┬──────────────────┬──────────────────┬──────────────────┬─────────────────┘
     ▼              ▼                  ▼                  ▼                  ▼
 Cloud SQL      Vertex AI          Agent Platform     Cloud Storage      Secret Manager
 (Postgres)     (Gemini)           sandbox service    saved files        gcp/vault.py
 state/         agent/models.py    gcp/sandbox.py     gcp/artifacts.py   keys, tokens
 runs, events,                     gcp/sessions.py    gcp/outputs.py
 approvals,                        gcp/spares.py
 LangGraph checkpoints
```

| Piece | Google Cloud service | Files |
|---|---|---|
| Housekeeping every 5 minutes (expire approvals, find lost workers, sweep idle sandboxes, fire schedules) | **Cloud Scheduler** calls the engine's `expire` method | `deploy/deploy.sh scheduler`, `engine/service.py` (`maintenance`) |
| Build and ship | **Cloud Build**, **Artifact Registry**, **IAM** | `Dockerfile`, `deploy/`, `terraform/` |
| Who may open the app | **IAP** on Cloud Run (identity, not network) | `web/auth.py`, `deploy/deploy.sh web` |
| Web search | Tavily (outside Google) | `agent/tools/web_search.py` |
| E-mail | AgentMail (outside Google) | `integrations/agentmail.py`, `agent/tools/email_tools.py` |
| Code on GitHub | a GitHub App (outside Google) | `integrations/github.py`, `agent/tools/github_tools.py` |
| Your own tools | MCP servers you paste in: inside the private network, or on an approved list ([rules](docs/EGRESS.md#mcp-servers-where-they-can-live)) | `mcp/`, `engine/mcp_servers.py` |
| Rules that cannot be talked around | `config/policy.yaml` (+ your own `policy.local.yaml`), checked inside the tools | `core/policy.py` |

## Why it is built this way

### Two tiers: Cloud Run for people, Agent Runtime for the agent

* **The browser never talks to the engine.** Only the web backend does, with its own service account (`roles/aiplatform.user`), and it tells the engine who the user is from the **verified sign-in**, never from anything the browser sent.
* **IAP, not a VPC, decides who gets in.** A VPC restricts by network; IAP restricts by identity, which is what "only these people" means. The backend runs `--no-allow-unauthenticated --iap`, verifies IAP's signed header
  (signature, issuer, and the audience `/projects/NUMBER/locations/REGION/services/NAME`) and ignores the plain e-mail header. Allowed people get `roles/iap.httpsResourceAccessor`; the list is `allowed_users` in the policy, so the engine and the web app cannot disagree.
* **Streaming works through Cloud Run and IAP.** A connection held open for 331 seconds was not cut, and a long answer arrived as many small messages. The backend still reconnects to the engine every 270 seconds and the browser can resume with `Last-Event-ID`.
* **The web tier is hardened like a public app**: a CSRF header plus a same-origin check on every state-changing route, a Content-Security-Policy (no remote images, so an answer's markdown cannot leak data to a stranger's server), at most 6 streams per user.
* It scales to zero (`web_min_instances: 0`), so you pay for it only when it is used. Locally you can use Google sign-in instead of IAP (`WEB_AUTH=google`).

### The engine: durable turns, parked approvals

An agent that waits for a person cannot live inside one HTTP request. So a **turn runs in a background worker** and writes events to Postgres; clients tail the log. The worker holds a **lease** (a heartbeat, with fencing: a worker that lost its lease writes nothing),
so a crashed instance is detected by the housekeeping job and the turn is resumed once, automatically. A pending approval is just a row plus a checkpoint, so the decision can arrive from another process, hours later, after a restart.
This is what makes it safe to run on a platform that scales and replaces instances. It is covered by tests that simulate a restart in the middle of a pause, on a real Postgres.

### State: Postgres

* One Cloud SQL instance holds LangGraph's checkpoints and our own tables (runs, events, approvals, artifacts, schedules, MCP servers, spare sandboxes). Postgres gives us the transactions the design needs (gapless event numbers, one atomic claim of a spare sandbox, lease fencing).
* The engine reaches it through the **Cloud SQL Auth Proxy** running next to the app in the same container (`deploy/start.sh`), with `--private-ip` when the database has a private address. A plain Postgres driver and connection pool talk to `127.0.0.1`.
* Migrations are idempotent and run at start-up; missing LangGraph tables are recreated (see the table of problems).

### Network and egress: what the agent can reach

The goal: the engine can reach **only the third-party hosts you list**, while keeping the Postgres checkpointer that Agent Gateway could not carry.

| Destination | Path | What controls it |
|---|---|---|
| Google APIs (Gemini, Secret Manager, Cloud Storage, Cloud SQL Admin, the sandbox service) | Out of the PSC interface into the VPC, then Private Google Access, **direct** | IAM only (a network rule cannot tell your bucket from a stranger's) |
| Postgres | The private address inside the VPC | VPC and Cloud SQL |
| Tavily, GitHub, AgentMail | An **explicit proxy** (Secure Web Proxy) with a host allowlist, then **Cloud NAT** | The allowlist (exact host names) |
| MCP servers | A reserved subnet inside the VPC, **direct** and by name (DNS peering), or an approved public host through the proxy | The policy (`mcp.scope`), re-checked on every call, and the network |
| Anything else | No route | The engine's own subnet has no NAT, so it cannot skip the proxy |
| Calls **into** the engine | The Vertex AI API with IAM; the PSC interface does not change this | IAM only |
| The shell sandbox | A Google service outside your VPC; per Google's documentation its outbound internet is **off by default** (not tested by us) | Google's default |

* **Why a proxy and not just a firewall:** the proxy filters by host name, so the allowlist reads as a list of services, not IP ranges. Cloud NAT is attached to the **proxy's** subnet only, which is what makes the proxy unavoidable.
* **Every HTTP client has to cooperate:** a proxy is explicit (clients must be told to use it). The proxy settings reach every client through environment variables, and a test runs each real client against a fake proxy to prove it. That test found that Tavily's async client ignores proxies and that `httpx` cannot match address ranges in `NO_PROXY`.
* **The same list drives the app and the network:** `mcp.allowed_hosts` in the policy is also the proxy's allowlist, so a host the app accepts is a host the network lets through. A user cannot connect a new third party from the UI alone.
* **Verified.** On a throwaway agent with this exact network layout: allowed hosts answered in about 0.1 s through the proxy, the same hosts timed out when tried directly, and hosts not on the list were refused by the proxy. On the live engine: it runs on the private database, a real web search went through the proxy, and a real Gemini turn called tools on an MCP server inside the VPC by name (non-listed and non-VPC servers were refused when added). We did not probe for blocked hosts from inside the live engine, because the product has no way to make arbitrary requests.
* **What it does not do:** data can still leave to an allowed host or through a Google API your IAM allows; inbound calls are IAM only (VPC Service Controls would help and is not set up). Full detail, the commands and the limits: **[docs/EGRESS.md](docs/EGRESS.md)**.
* It is **off by default** and switched on by three lines of config (`network_attachment`, `egress_proxy`, `sql_private_ip`); `deploy/network.sh up` builds the pieces, Terraform has the same settings.

### Identity, secrets and least privilege

| Who | Can | How |
|---|---|---|
| The engine's service account | Call Vertex AI, read secrets, connect to Cloud SQL, write logs, bill Storage calls to the project, write to one bucket, sign download URLs for itself | `aiplatform.user`, `secretmanager.secretAccessor`, `cloudsql.client`, `logging.logWriter`, `serviceusage.serviceUsageConsumer`, `storage.objectAdmin` on the bucket, `serviceAccountTokenCreator` on itself |
| The web backend | Call the engine, nothing else | its own service account with `aiplatform.user` |
| Cloud Scheduler | Call the engine's `expire` method | its own service account |
| People | Open the web app | `iap.httpsResourceAccessor`, from `allowed_users` |

* **Secrets are read by name** from Secret Manager (`secret://NAME`); `deploy.sh setup` copies every `.env` variable whose name looks secret (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`...). The engine hides any known secret value that would appear in output before the model sees it.
* **GitHub:** the agent uses a GitHub App; each token is minted on demand, narrowed to one repository and to contents + pull requests, and the tools allow only `agent/*` branches and pull requests, never the default branch.
* **MCP tokens** are stored encrypted with a key in Secret Manager and are never returned by the API.

### The sandbox: how cost and files are managed

The agent runs shell commands and writes files in a **sandbox**: a remote Linux machine from the Agent Platform sandbox service (`gcp/sandbox.py`). It holds no secrets
and it is disposable: Google deletes it after a fixed lifetime (`SANDBOX_TTL_SECONDS`, 3600 s), and we delete it much sooner. Creating one takes 30-130 seconds and it bills while it exists,
so most of the machinery below is about creating as few as possible, keeping them as short as possible, and never losing the user's files.

**Cost: how few sandboxes, for how long**

| What | How | Where |
|---|---|---|
| Created only when needed | **Lazy creation**: a chat-only turn never creates a sandbox; it appears the first time a tool needs a shell or a file | `gcp/sandbox.py` (`open_lazy`) |
| One per conversation, reused | The sandbox belongs to the **session**, not to a turn: later messages attach to it, so files and installed packages are still there | `gcp/sessions.py`, `engine/service.py` |
| Deleted when idle | A conversation idle for `limits.sandbox_idle_minutes` (20) has its sandbox deleted by the housekeeping sweep; one with a pending approval is protected while the approval lasts | `gcp/sessions.py` (`sweep`), Cloud Scheduler every 5 min |
| Never trusts a stale one | Before a turn it asks the platform whether the recorded sandbox still answers; one about to expire (under 120 s left) or gone is replaced, and the agent is told | `engine/service.py` (`_open`), `gcp/sessions.py` |
| Hides the start-up wait, with a hard cost cap | **Warm pool**: ONE spare sandbox is prepared when the user starts typing (`warm_sandbox`), taken by the first turn that needs a sandbox, deleted if nobody uses it within `spare_max_minutes` (15), and at most `max_spares_per_day` (10) are made. Extra cost is therefore bounded by 10 x 15 = 150 sandbox-minutes a day. Set `warm_sandbox.mode: off` to go back to lazy creation | `engine/spare_pool.py`, `gcp/spares.py`, table `pb_spares` |
| Claimed exactly once | The claim is one atomic database statement, and only one spare may be active at a time | `state/postgres.py` |
| One-shot (legacy) runs | Their throwaway sandbox is deleted when the run ends | `engine/service.py` (`_release`) |
| Other limits | `max_concurrent_turns` (4), `max_turn_minutes` (30), `max_helper_model_calls` (40 model calls per helper task) | `config/policy.yaml` |

Known gap: a restart in the middle of creating a sandbox can orphan it until its own lifetime ends. Idle price per hour is not verified: check your billing page.

**Files: nothing the user cares about lives only in a sandbox**

* The agent saves its deliverables in `/workspace/out`. At the end of **every** turn (finished, waiting for approval, cancelled or failed) the new or changed files are copied to Cloud Storage
  (`ARTIFACTS_URI`) and indexed in Postgres (table `pb_artifacts`), **per user**, under a hashed user id so no e-mail address appears in a path: `users/<hash>/artifacts/<session>/<turn>/<path>`
  (`gcp/artifacts.py`). An unchanged file is not stored again; a changed one becomes a new version. At most 50 files / 25 MB per capture.
* The UI lists them across conversations and downloads them through short-lived signed URLs; the agent has `list_artifacts` and `restore_artifact` to bring one back into a fresh sandbox
  (`agent/tools/artifact_tools.py`, 5 MB per file). When a sandbox was replaced, the agent is told in its next message that earlier files are gone and where to find them.
* **Skills** (reusable instructions and scripts) are read from a local, read-only copy, so listing them never creates a sandbox; a copy is placed in each new sandbox so scripts can run (`gcp/skills.py`, `agent/backends.py`).
* **Secrets never enter the sandbox**, and any known secret value that would appear in command output is replaced with `[REDACTED:NAME]` before the model sees it (`gcp/vault.py`).
* Downloads from a sandbox are capped at 5 MB per file.

### Guardrails

What bounds the damage a confused or manipulated agent can do, strongest first. The two input guards at the end are **defence in depth, not the security boundary**: heuristic filters can always be evaded.

| Control | What it stops | What it does not stop |
|---|---|---|
| **Human approvals + audit trail** (gated tools: shell, e-mail, GitHub writes, plans, questions, connected MCP tools) | side effects happening unseen; "what happened?" is answerable | a person approving carelessly |
| **Policy inside every tool**, checked before an approval is even asked and again on edited arguments | e-mail to anyone but the allow-listed addresses, pushes to other repos, to non-`agent/` branches or the default branch, models and limits outside the list | anything the tool is allowed to do |
| **Approve automatically has hard limits**: only shell and GitHub by default (e-mail, plans, connected tools and unknown tools only if you list them), never questions, a per-turn cap (`max_auto_approvals_per_turn`, 25), and `approvals.never_auto_tools` always asks | one click turning into unlimited side effects | what the allowed kinds can do within the cap |
| **Least-privilege identities**: a dedicated service account, a GitHub App token narrowed to one repo and to contents + pull requests, secrets read by name | a stolen token reaching other repos or data | Google APIs the service account may call |
| **Remote sandbox** (disposable, holds no secrets, files return only as artifacts). Google documents its outbound internet as **off by default**, with no route to private ranges or the metadata server, and our code never turns it on | host compromise, secret theft from the shell | abuse inside the sandbox. The default is Google's documented behaviour; **we have not tested it** |
| **Network egress control** (optional, off by default; [`docs/EGRESS.md`](docs/EGRESS.md)): the engine joins your VPC and reaches third parties only through a proxy with a host allowlist | the engine calling a host you did not list | Google APIs (limited by IAM only), data sent to an allowed host, inbound calls |
| **Helpers are bounded and cannot publish**: only the orchestrator holds the GitHub write tools, e-mail lives in the mailer, each gated; helpers stop after `max_helper_model_calls` | a looping or manipulated helper acting alone | |
| **IAP + verified e-mail + CSRF header + CSP** (web backend) | anonymous callers, forged user ids, cross-site requests, remote images used to leak data | a compromised Google account |
| **MCP servers you add**: accepted only inside the private network range (assumed approved) or on the approved host list (public, https only); anything else is refused, the address is re-checked on every call, tokens stored encrypted and never returned, results labelled untrusted, every tool asks first unless you mark it safe | an unexpected server or address | a malicious server placed inside your network, or on the list |
| Untrusted-content labels (`wrap_untrusted`) | naive injection via web results, e-mail, GitHub files | nothing on its own: it is a label, not a lock |

#### Defence in depth: the two input guards

Two guards sit on the way in, one in front of the model and one in front of the shell.

**Prompt guardrail** (`PromptGuard` in `core/guardrails.py`; applied to every user message and every scheduled instruction before a turn starts). A refused message creates a `blocked` turn with the
rule that caught it and never reaches the model. It checks, after Unicode normalisation and removal of zero-width characters:

* empty or too long (`MAX_PROMPT_CHARS`, 8000);
* **secrets pasted into the prompt** (Google API key, GitHub token, Slack token, private key, `sk-` style keys, Tavily key, AWS key);
* **injection patterns**: "ignore previous instructions", asking for the system prompt, jailbreak personas, asking to print keys or the environment, role-tag spoofing (`<system>`, `[INST]`), `curl | sh`.

Text that comes **from outside** (web results, e-mail, GitHub files, MCP tool results, saved-file listings) is labelled `<untrusted_content>` so the model treats it as data
(`wrap_untrusted`). That is a label, not a lock.

**Sandbox guardrail** (`CommandGuard` in `core/guardrails.py`; applied to every command the agent's shell tool runs, in `gcp/sandbox.py`; the sandbox's own internal file operations use a separate raw path). A blocked command returns exit code 126 with the reason
and writes a `command.blocked` line to the audit trail. It tokenises the command with shell quoting rules and **fails closed** on anything it cannot parse. It refuses:

* any command not on a short **allowlist** (`ls cat grep sed awk jq python3 pip tar ...`), and a **never** list in both modes: `sudo ssh curl wget bash sh eval git docker gcloud env xargs kill ...`;
* command substitution (`$(...)`, backticks), process substitution, background execution (`&`), nested shells;
* the metadata server, `/dev/tcp`, `/proc/*/environ`, credential files (`.ssh`, `.aws`, gcloud config, `.git-credentials`), fork bombs, decode-and-execute;
* Python that spawns processes or opens raw sockets (`subprocess`, `os.system`, `socket`, `ctypes`), `find -exec`, `sed` `e`, `awk` `getline`, `jq` environment access, recursive `rm` outside the workspace,
  `pip` from URLs or absolute requirement files, `python -m http.server`.

What it does not stop: a determined `python3 -c` script. In the cloud, `APPROVAL_MODE=execute` also makes a person approve every shell command unless the conversation was switched to "approve automatically".

Secrets: locally in `.env` (git-ignored); in the cloud in Secret Manager, referenced as `secret://NAME` (`SECRETS_BACKEND=gcp`; `deploy.sh setup` copies every `.env` variable whose name looks secret).


### Deploying and operating it

* **One config file.** `deploy/config.yaml` holds the defaults and `deploy/config.local.yaml` (git-ignored) overrides them. Change `name` and every resource name follows (`<name>-repo`, `<name>-runtime`, `<name>-pg`, `<name>-housekeeping`, ...).
  `deploy.sh`, `deploy/runtime.py` and Terraform all read the same file, and tests keep them consistent. Your own addresses live in `config/policy.local.yaml`.
* **Stages, each safe to repeat, each with `DRY_RUN=1`:** `preflight` (read-only) → `setup` (APIs, registry, service accounts, secrets) → `sql` → `build` (Cloud Build) → `deploy` (the runtime) → `scheduler` → `smoke --all`;
  `web-build`, `web`, `web-smoke` for the Cloud Run tier; `deploy/network.sh up` for the private network. The image is pinned by **digest**, so a moving `:latest` tag cannot serve a stale copy.
* **Updates in place, moves by blue/green.** A new image updates the runtime in place. A change that cannot be applied to an existing runtime (adding a network interface) is done blue/green: `deploy.sh deploy --state-file deploy/.state-new.json`
  creates the new runtime next to the old, you test it, then `deploy.sh scheduler` and `deploy.sh web` repoint the callers, and the old one is deleted when you are happy.
* **Acceptance tests run against the real thing.** `smoke --all` calls the deployed engine (models, housekeeping, a chat turn through the worker, durable state, a web search, a shell approval with a real sandbox). `scripts/verify_real.py` runs the agent against real Gemini and the real sandbox service
  (17 scenarios: streaming, approvals, restart, cancel, artifacts, e-mail, GitHub, parallel helpers, MCP, model choice). `scripts/smoke_web.py` tests the Cloud Run tier from outside with an IAP-signed request.
* **Local first.** The whole product runs on your computer with scripted models and a fake sandbox (`scripts/dev_stack.py`), so most changes are tested before they cost anything. Tests pin the default behaviour (`tests/engine/test_default_path_golden.py`), so a new feature has to be opt-in.

What bills while it is deployed:

| Piece | Bills | Control |
|---|---|---|
| Agent Runtime | per instance-time; `min_instances: 1` keeps one warm | `min_instances`, `max_instances` in `deploy/config.yaml` |
| Cloud SQL | while the instance exists | `deploy.sh down --sql` |
| Secure Web Proxy + Cloud NAT | per hour while they exist | `deploy/network.sh down` |
| Sandboxes | while a sandbox exists | lazy creation, 20-minute idle sweep, capped warm pool |
| Cloud Run web backend | only while handling requests | scales to zero |
| Cloud Build, Cloud Scheduler, Secret Manager | pennies | |

## What is in place, and what is not

| In place | Evidence |
|---|---|
| State survives restarts, lost workers and parked approvals | Tests that restart the engine in the middle of a pause, on real Postgres; a lease and fencing design |
| One-command, repeatable, dry-runnable deploys, image pinned by digest, in-place updates | `deploy/deploy.sh` (every stage idempotent, `DRY_RUN=1`), `deploy/network.sh` |
| Moves between runtimes with a rollback (blue/green) | Used for the network migration: the new engine was tested next to the old one, then the callers were switched, and the old one was kept as a rollback |
| Infrastructure as code | `terraform/` (validated; see below) |
| Acceptance tests against the live system | `smoke --all`: models, housekeeping, a chat turn through the worker, durable state, a web search, a shell approval with a cloud sandbox (6/6) |
| Identity-based access, least privilege, secrets in Secret Manager | IAP, one service account per role, secrets by name |
| Egress control and a private database | PSC interface + proxy allowlist + private IP (see above) |
| Human approval and an audit trail for every side effect, with policy checks inside the tools | Gates, `config/policy.yaml`, an audit table |
| Cost controls | Lazy and capped sandboxes, scale-to-zero web tier, idle sweeps, `max_concurrent_turns`, per-turn caps on helper model calls and automatic approvals |
| A large automated test suite, and a test that pins the default behaviour | Over 1,200 backend tests (a real Postgres variant included) and 149 frontend tests; `tests/engine/test_default_path_golden.py`; important tests are checked by breaking the code and watching them fail |

**Not in place, or not verified:**

* **Inbound is IAM only.** Callers reach the engine through the Vertex AI API; nothing restricts them to your network. VPC Service Controls would, and we have not set it up. A gateway ingress is incompatible with the PSC interface.
* **Google APIs are open to whatever IAM allows**, and data can leave to a host on the allowlist. The network control reduces the attack surface; it is not a data-loss guarantee.
* **No monitoring, alerting or dashboards.** There are logs (Cloud Logging) and audit rows, but no alert policies, uptime checks or SLOs.
* **No CI/CD pipeline.** Builds and deploys are run by hand with the scripts.
* **Single region, one small database** (`db-f1-micro`, no high availability set up). Backups and recovery have not been exercised.
* **No load or soak test**, and the agent's behaviour under many concurrent users has not been measured.
* **The Terraform is validated, not applied** to a project here; `deploy.sh` is what actually ran.
* **No measured evaluation of the guards.** The prompt and command guards are heuristics, described as defence in depth; there is no red-team suite with block rates yet.
* **The sandbox's "internet off" is Google's documented default,** not something we tested.
* **The web app is not hosted:** the React app runs on your computer against the cloud engine, and the Cloud Run web image is older than this code (rebuild with `deploy.sh web-build` then `deploy.sh web`).
* **A custom agent** written to the contract runs through the real engine in tests, but has not been deployed to Agent Runtime.

## How to run it

You need Python 3.13 with [`uv`](https://docs.astral.sh/uv/), Node 20+ (for the web app), and for the cloud the `gcloud` CLI and a project with billing. Keys: a [Tavily](https://tavily.com) key for web search;
AgentMail and a GitHub App are optional (a connector without credentials is simply switched off).

### 1. Locally, in your terminal

```bash
uv sync --extra dev
python scripts/init_env.py                 # creates .env from example.env (never overwrites)
# edit .env: TAVILY_API_KEY, GCP_PROJECT ...
gcloud auth application-default login
uv run pinboard ping                       # creates a sandbox runtime on first use and checks it
uv run pinboard run "Build a short market brief and save it to out/brief.md"
```

### 2. Locally, with the web app (the engine and the backend on your computer, state in memory)

```bash
export PINBOARD_API_TOKEN=$(openssl rand -hex 32)
uv run pinboard serve --port 8080                                                  # terminal 1: the engine
WEB_AUTH=dev DEV_USER_EMAIL=you@gmail.com RUNTIME_URL=http://127.0.0.1:8080 RUNTIME_TOKEN=$PINBOARD_API_TOKEN \
  uv run pinboard web --port 8081                                                  # terminal 2: the web backend (dev sign-in is refused on Cloud Run)
cd frontend && npm install && npm run dev                                          # terminal 3: http://localhost:5173
```

No cloud and no cost at all: `uv run python scripts/dev_stack.py` instead of terminals 1 and 2 (a scripted demo model and a fake sandbox).
The app can also run against your **deployed** engine, with real Google sign-in: see [frontend/README.md](frontend/README.md) and `scripts/run_local_ui.sh`.
More detail on the local CLI: [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md).

### 3. In your Google Cloud project

1. Copy `config/policy.local.example.yaml` to `config/policy.local.yaml` (git-ignored) and put **your** addresses in it: `allowed_users`, `email.from_inbox`, `email.allowed_recipients`. The shipped `config/policy.yaml` holds placeholders, and the local file is merged over it (the deploy tools and the image build read both). Rules, models and limits are in `config/policy.yaml`. The policy is baked into the image, and with the placeholders nobody can sign in and nothing can be e-mailed.
2. Copy your settings to `deploy/config.local.yaml` (git-ignored; keys as in `deploy/config.yaml`: `name`, `project`, `region`, `artifacts_bucket`). Every resource name derives from `name`.
3. Run the stages, each safe to repeat (`DRY_RUN=1` prints what would happen):

```bash
deploy/deploy.sh preflight     # read-only: who you are, what exists
deploy/deploy.sh setup         # APIs, Artifact Registry, service accounts, secrets from .env          (free)
deploy/deploy.sh sql           # a small Cloud SQL Postgres                                            (BILLED while it exists)
deploy/deploy.sh build         # Cloud Build builds the engine image
deploy/deploy.sh deploy        # create or update the Agent Runtime                                     (BILLED while it exists)
deploy/deploy.sh scheduler     # the 5-minute housekeeping job
deploy/deploy.sh smoke --all   # call every method and run real tasks through the deployed engine
deploy/deploy.sh web-build && deploy/deploy.sh web    # the web backend on Cloud Run behind IAP (`web-smoke` tests it with an IAP-signed request)
deploy/deploy.sh down          # delete the runtime and the job (--sql also deletes the database)
```

4. **Optional, recommended for production: private networking and egress control** ([docs/EGRESS.md](docs/EGRESS.md)). `deploy/network.sh up` builds the VPC, the proxy with its host allowlist, the MCP subnet and the private DNS zone, and prints three lines for `deploy/config.local.yaml` plus the command that gives the database a private address; then `deploy/deploy.sh deploy --state-file deploy/.state-new.json` creates the engine on that network next to the old one (blue/green), and `smoke --all --runtime ...` tests it before you switch.

`terraform/` creates the same resources (see its README; use one or the other per project). Details: [deploy/README.md](deploy/README.md).
Costs while deployed: see the table in [Deploying and operating it](#deploying-and-operating-it).

### Tests

```bash
uv run pytest                                  # everything except the Postgres variants
docker run -d --name pbtest-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=pbtest -p 55432:5432 postgres:16
export PINBOARD_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/pbtest
uv run pytest                                  # also runs the same contract tests on real Postgres, incl. a simulated restart
cd frontend && npm test                        # the web app's unit tests; `npx playwright test` drives it in a real browser
```

Tests use scripted models and fake services (`tests/turn_harness.py` runs the real agent against a fake sandbox platform). `tests/engine/test_default_path_golden.py` pins the exact behaviour of
the default configuration. Anything that needs the real cloud is a script: `scripts/verify_real.py` (the agent against real services, from your laptop), `scripts/smoke_runtime.py` and
`scripts/smoke_web.py` (a deployed engine / web backend, from outside).

### Layout

```
src/pinboard/
  core/          settings, the policy file, the two guards                      (no cloud, no LangGraph)
  state/         records, the store interface, memory + Postgres stores, LangGraph checkpointer setup
  engine/        what runs in Agent Runtime: service (turns, approvals), sessions, warm pool, schedules, worker, events, api
  web/           the Cloud Run backend: IAP / Google sign-in, the engine client, routes, live stream (SSE)
  agent/         everything LangGraph: prompts, the team of helpers, tools, approval gates, model selection, builder, contract
  gcp/           Google Cloud pieces: sandbox, skills, artifacts, spare sandboxes, secret vault
  integrations/  plain clients for AgentMail and the GitHub App
  mcp/           bring-your-own MCP servers (HTTP only)
  cli.py         `pinboard ...` commands
frontend/        the React app (privai)               config/    policy.yaml
deploy/          deploy.sh, runtime.py, entrypoint    terraform/ the same resources as deploy.sh
scripts/         real-service checks, dev stack       tests/     core/ state/ engine/ agent/ gcp/ mcp/ web/ deploy/
spikes/          throw-away experiments kept as a record (platform facts, Agent Gateway, helper-model choice): see spikes/README.md
docs/            HOW_TO_RUN.md, BRING_YOUR_OWN_AGENT.md, notebooks/ (the Agent Gateway lab)
```

## How to add your own agent

The engine (sessions, streaming, approvals, cancel, resume after a restart, schedules, the web app, the deploy scripts) works on **any compiled LangGraph graph**.
Pinboard's own agent is only the default one. To run yours, write one function and name it:

```bash
AGENT_BUILDER=my_package.my_module:build        # in .env, or an environment variable of the runtime
```

```python
# my_package/my_module.py
from langchain.agents import create_agent
from pinboard.agent.contract import AgentHandle, BuildContext

def build(ctx: BuildContext) -> AgentHandle:
    graph = create_agent(my_model, tools=[...], checkpointer=ctx.checkpointer)   # the checkpointer is required
    return AgentHandle(graph)                                                    # a bare compiled graph also works
```

* **Approvals come for free.** Anything that pauses your graph, a human-in-the-loop tool or a plain `interrupt(...)`, becomes a card (approve / edit / reject) or a question in the UI, and the
  answer is fed back into the graph. A tool the engine does not recognise gets a generic card, and "approve all" never covers it unless the user lists it.
* **Switch off what you do not need** in `config/policy.yaml`: `features: {sandbox: false}` removes the shell and files workspace (and with it artifacts and the warm pool; deploy then stops asking for a sandbox runtime).
  Both switches default to on, which is how Pinboard runs.
* **Deploy** with the same scripts; put the module in the image (see the `Dockerfile`) and pass `AGENT_BUILDER` through `.env` or Terraform's `extra_env`.
* Helper blocks in the UI appear for deepagents' `task` / `delegate` tools; an agent without them shows its steps as ordinary tool calls. Choosing a model per message needs Pinboard's model router.

Full guide, with what is and is not verified: **[docs/BRING_YOUR_OWN_AGENT.md](docs/BRING_YOUR_OWN_AGENT.md)** and the contract itself in `src/pinboard/agent/contract.py`.
A second agent written only against the contract (no deepagents, no sandbox) runs through the real engine in `tests/engine/test_custom_builder.py`; it has **not** been deployed to Agent Runtime yet.

## License

MIT, see [LICENSE](LICENSE).
