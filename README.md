# Pinboard

A personal AI assistant, built to learn how to run an agent in production on Google Cloud. It can research, write files, send an e-mail
and push code to GitHub, and **every side effect waits for a human decision**. Built on deepagents / LangGraph with Gemini on Vertex AI.

What is in here, in the order of this page: [architecture](#architecture) · [Agent Gateway, and why this project does not use it](#agent-gateway-googles-governed-path-and-the-trouble-we-hit) ·
[the sandbox: cost and files](#the-sandbox-how-cost-and-files-are-managed) · [guardrails](#guardrails) · [how to run it](#how-to-run-it) · [how to add your own agent](#how-to-add-your-own-agent)

Current state, decisions and the full history: **[CLAUDE.md](CLAUDE.md)** (the working log) and **[PLAN.md](PLAN.md)** (original architecture plan).

## Architecture

```
  You, in a browser
  React app: frontend/            runs on your computer today (Vite, http://localhost:5173)
        │  /api   same origin, CSRF header
        ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ WEB BACKEND · Cloud Run behind IAP (Google sign-in)                      src/pinboard/web/    │
 │   auth.py  IAP / Google sign-in      app.py  routes + live stream (SSE)                       │
 │   runtime_client.py  calls the engine      main.py  starts it                                 │
 └───────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                 │ :query / :streamQuery with its own Google credentials;
                                                 │ the verified e-mail is the user id (the browser never reaches the engine)
                                                 ▼
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

Also around the engine:

| Piece | Google Cloud service | Files |
|---|---|---|
| Housekeeping every 5 minutes (expire approvals, find lost workers, sweep idle sandboxes, fire schedules) | **Cloud Scheduler** calls the engine's `expire` method | `deploy/deploy.sh scheduler`, `engine/service.py` (`maintenance`) |
| Build and ship | **Cloud Build**, **Artifact Registry**, **IAM** | `Dockerfile`, `deploy/`, `terraform/` |
| Who may open the app | **IAP** on Cloud Run (identity, not network) | `web/auth.py`, `deploy/deploy.sh web` |
| Web search | Tavily (outside Google) | `agent/tools/web_search.py` |
| E-mail | AgentMail (outside Google) | `integrations/agentmail.py`, `agent/tools/email_tools.py` |
| Code on GitHub | a GitHub App (outside Google) | `integrations/github.py`, `agent/tools/github_tools.py` |
| Your own tools | remote MCP servers you paste in (HTTPS only) | `mcp/`, `engine/mcp_servers.py` |
| Rules that cannot be talked around | `config/policy.yaml`, checked inside the tools | `core/policy.py` |

How it behaves:

* A **turn** (one user message) runs in a background worker and writes small events to a log; clients follow the log (live stream, resumable from any position).
* A **side effect** (shell command, e-mail, GitHub write, a plan) is **parked** as an approval; the decision can come hours later, from a different process;
  the agent resumes from a durable checkpoint in Postgres.
* **Policy** (`config/policy.yaml`) is enforced inside the tools before anything is asked: who may use the app, who may be e-mailed, which repos and branches,
  which models, limits. It fails closed (a missing or invalid file stops the app).
* The browser never talks to the engine: only the web backend does, and it tells the engine who the user is from the verified sign-in, never from the request.

State of the deployment (see CLAUDE.md for the details): the engine and the web backend are deployed (Cloud Run behind IAP; the web image there is older than this code,
rebuild with `deploy.sh web-build` and `deploy.sh web`). The React app is **not hosted**: it runs on your computer against the cloud engine.

## Agent Gateway: Google's governed path, and the trouble we hit

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

**What this project does instead, for now:** no gateway on the engine. The controls live where the agent acts: policy checks inside the tools, human approvals, the sandbox and its guards,
least-privilege IAM, and IAP in front of the web app (see [Guardrails](#guardrails)). If you want a gateway you have three real options:
(1) ingress-only governance; (2) keep egress governance and move the checkpointer to an HTTP-based store (the Firestore saver in the notebook, or a thin HTTPS service in front of Postgres);
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
  [Memory Bank](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank)

## The sandbox: how cost and files are managed

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

## Guardrails

Two guards sit on the way in, one in front of the model and one in front of the shell. They are **defence in depth, not the security boundary**: heuristic filters can always be evaded.
The real protections are the sandbox that holds no secrets, least-privilege connectors, policy inside the tools, and a human approving consequential actions.

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

**The other layers**

| Layer | Stops | Does not stop |
|---|---|---|
| Remote sandbox (no secrets, disposable) | host compromise, secret theft | abuse inside the sandbox |
| `CommandGuard` | `sudo`, `curl \| sh`, env dumps, nested shells, `$(...)` | a determined `python3 -c` script |
| `PromptGuard` | obvious injection, secrets pasted into prompts | clever paraphrases (a speed bump) |
| Untrusted-content labels | naive injection via web results | nothing on its own |
| Policy inside the tools | e-mail to anyone else, pushes to other repos / branches / the default branch, models and limits outside the list | a human approving carelessly |
| Approvals + audit trail | side effects happening unseen; "what happened?" | |
| Helper agents cannot publish | only the orchestrator holds the GitHub write tools; e-mail lives only in the mailer; each is gated | |
| Bounded helpers | a helper that loops stops after `max_helper_model_calls` | |
| IAP + verified e-mail + CSRF header + CSP (web backend) | anonymous callers, forged user ids, cross-site requests, remote images used to leak data | a compromised Google account |
| MCP servers you add | HTTPS only, public addresses only (optional host allowlist), tokens stored encrypted and never returned, results labelled untrusted, every tool asks first unless you mark it safe | a malicious server you chose to trust |

Secrets: locally in `.env` (git-ignored); in the cloud in Secret Manager, referenced as `secret://NAME` (`SECRETS_BACKEND=gcp`; `deploy.sh setup` copies every `.env` variable whose name looks secret).

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

1. Edit `config/policy.yaml`: `allowed_users`, `email.allowed_recipients`, GitHub rules, models, limits. It is baked into the image.
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

`terraform/` creates the same resources (see its README; use one or the other per project). Details: [deploy/README.md](deploy/README.md).
Costs while deployed: the Cloud SQL instance, and the engine (`min_instances: 1` keeps one instance warm); the web backend scales to zero.

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
spikes/          throw-away experiments kept for reference (platform facts, Agent Gateway)
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
