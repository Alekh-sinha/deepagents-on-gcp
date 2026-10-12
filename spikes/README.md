# Spike: verify Agent Runtime + Cloud SQL assumptions before Phase 2

Throwaway code. It deploys a tiny probe container as a real Agent Runtime and measures the things the Phase 2 design depends on.
Nothing here imports the `pinboard` package.

| Q | Question | Why it matters |
|---|---|---|
| Q1 | Do `:query` / `:streamQuery` reach our `/api/reasoning_engine` routes with declared `class_methods`? | the whole API shape |
| Q2 | Which service account does the container run as? | IAM for Secret Manager, Vertex, Cloud SQL |
| Q3 | How long can one unary / streaming request stay open? | decides streaming turns vs. async polling |
| Q4 | Does a background thread keep running between requests? | whether "return a run id, work in background" is possible |
| Q5 | Can it reach api.github.com, api.agentmail.to, Tavily, Secret Manager, the internet? | connectors, and what Agent Gateway must allow later |
| Q6 | Can it read Secret Manager? | the `SECRETS_BACKEND=gcp` vault path |
| Q7 | Can it call Gemini on Vertex? | model access with the runtime identity |
| Q8 | Can it reach Cloud SQL Postgres (Cloud SQL Python Connector, public IP, no VPC)? | durable state + LangGraph checkpoints |

Run (from `pinboard-agent/`): `preflight` -> `setup` -> `sql-up` -> `build` -> `deploy` -> `probe` -> `down`. See the docstring in `verify.py`.

Cost: a `db-f1-micro` Cloud SQL instance and one Agent Runtime instance (`min_instances=1`) bill while they exist. Run `down` as soon as
you have the results (`spikes/results.json`). Not covered here (separate follow-ups): Agent Gateway egress allowlist, PSC-I private networking
to Cloud SQL, Terraform `secret_env`.
