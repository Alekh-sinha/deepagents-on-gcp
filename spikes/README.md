# Spikes: throw-away experiments, kept as a record

Each folder answered a question **before** we built on the answer. They are not part of the product (nothing in `src/` imports them), most of them create billed resources, and several assume names from an
earlier layout: read them as a record of how a result was obtained, not as recipes. Run anything here only in a throwaway project.

| Folder | Question | Status |
|---|---|---|
| `verify.py`, `probe/`, [`RUNBOOK.md`](RUNBOOK.md) | What does Agent Runtime + Cloud SQL actually do? (Q1 to Q8 below) | **Run in October 2026; answers below.** Its resources were deleted afterwards |
| [`gateway/`](gateway/README.md) | Can the engine run behind Agent Gateway (egress allowlist)? | **Run in four rounds: not with a Postgres checkpointer.** The working alternative is [`docs/EGRESS.md`](../docs/EGRESS.md); the README's "Problems we ran into" has the results |
| [`model_choice/`](model_choice/README.md) | Can the orchestrator choose the model a helper runs on? | **Proved and absorbed into the product** (`src/pinboard/agent/delegation.py`) |

## The first spike: what we found

The spike deployed a tiny probe container (`probe/`) as a real Agent Runtime and measured what the design depended on. Results:

| Q | Question | Answer |
|---|---|---|
| Q1 | Do `:query` / `:streamQuery` reach our `/api/reasoning_engine` routes with declared `class_methods`? | **Yes.** The container receives the body JSON-encoded **twice** (a JSON string containing the object), so the app parses until it has an object. A container deploy also needs `agent_framework: langgraph` |
| Q2 | Which service account does the container run as? | **The one we set** (`service_account`). The container sees a Google-managed project number, not ours |
| Q3 | How long can one request stay open? | Unary calls of 30, 60, 120, 300 and **600 s worked; 900 s was killed** with an empty error. Streaming for 300 s worked (the product streams through Cloud Run and IAP for longer, with reconnects every 270 s) |
| Q4 | Does a background thread keep running between requests? | **Yes** (the same instance, with gaps of at most 1 s). That is what makes "return at once, work in the background" possible |
| Q5 | Can it reach GitHub, AgentMail, Tavily, Secret Manager and the internet? | **Yes, by default.** Outbound traffic is open unless you add a gateway or your own network. This is why egress control is a separate design ([docs/EGRESS.md](../docs/EGRESS.md)) |
| Q6 | Can it read Secret Manager? | **Yes**, with the runtime's service account |
| Q7 | Can it call Gemini on Vertex? | **Yes** (`gemini-2.5-flash` then; newer models need the `global` location) |
| Q8 | Can it reach Cloud SQL Postgres? | **Yes**: the Cloud SQL Python Connector works over a public IP with no VPC. Q9, added later: **psycopg 3 + a connection pool + LangGraph's `PostgresSaver` work through the Cloud SQL Auth Proxy run inside the container**, which is what the product uses |

`results.json` holds only the **last** probe run (the Q9 check); the full Q1 to Q8 output was not kept, so the table above is the record.

Run it (from `pinboard-agent/`): `preflight` → `setup` → `sql-up` → `build` → `deploy` → `probe` → `down`; see the docstring in `verify.py` and the [runbook](RUNBOOK.md).
Cost: a `db-f1-micro` Cloud SQL instance and one Agent Runtime instance (`min_instances=1`) bill while they exist, so run `down` as soon as you have the results.

**What this spike did not cover, and where it went:** Agent Gateway egress → [`gateway/`](gateway/README.md); private networking to Cloud SQL (a PSC interface) → [`docs/EGRESS.md`](../docs/EGRESS.md), built and tested later;
Terraform `secret_env` → still not tested (the product passes secrets by name, `secret://NAME`, and reads them itself).
