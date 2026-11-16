# CLAUDE.md: working on Pinboard

Pinboard ("privai" in the UI) is a personal AI assistant on Google Cloud: a deepagents/LangGraph agent on **Agent Runtime**, state in **Cloud SQL Postgres**, a React app behind **IAP** on Cloud Run, and
side effects (shell, e-mail, GitHub) behind human approval. What it is and how it fits together: [README.md](README.md).
Other docs: [docs/EGRESS.md](docs/EGRESS.md) (network egress control, MCP placement), [docs/BRING_YOUR_OWN_AGENT.md](docs/BRING_YOUR_OWN_AGENT.md), [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md), [PLAN.md](PLAN.md) (the original plan).

## Commands
```bash
uv sync --extra dev && uv run pytest                      # backend; Postgres tests are skipped unless PINBOARD_TEST_DATABASE_URL is set
docker run -d --name pbtest-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=pbtest -p 127.0.0.1:55432:5432 postgres:16
PINBOARD_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/pbtest uv run pytest     # everything; remove the container afterwards
cd frontend && npx tsc --noEmit && npx vitest run         # frontend
uvx ruff check --select F,E9 src scripts deploy           # lint
uv run python scripts/dev_stack.py                        # whole product on this machine, no cloud (then: cd frontend && npm run dev)
scripts/run_local_ui.sh start|stop|status|logs            # local UI against the deployed engine (settings: .env.local-ui)
deploy/deploy.sh <preflight|setup|sql|build|deploy|scheduler|web-build|web|smoke|status|down>      # DRY_RUN=1 prints without changing anything
deploy/network.sh <up|allowlist|status|hosts|down>        # private network, egress proxy, MCP subnet (docs/EGRESS.md)
# terraform: validate with the whole project mounted:
docker run --rm --entrypoint sh -v "$PWD":/app -w /app/terraform hashicorp/terraform:1.9 -c 'terraform init -backend=false && terraform validate'
```

## Layout
`src/pinboard/`: `core/` (settings, **policy**, guardrails) · `state/` (records, Memory + Postgres stores) · `engine/` (what runs in Agent Runtime: service, sessions, events, worker, API, schedules, MCP) ·
`web/` (Cloud Run backend: IAP/Google sign-in, relay, SSE) · `agent/` (everything LangGraph/deepagents) · `gcp/` (sandbox, artifacts, vault, spares) · `integrations/` (AgentMail, GitHub clients) · `mcp/`.
`config/policy.yaml` (limits and rules) · `deploy/` (deploy.sh, runtime.py, network.sh, config.yaml) · `terraform/` · `frontend/` · `tests/` (grouped like `src/`) · `spikes/` (records of platform experiments).

## Your own values (all git-ignored; the shipped files hold placeholders)
`.env` (secrets and settings) · `deploy/config.local.yaml` (project, bucket, network) · **`config/policy.local.yaml`** (your addresses; merged over `policy.yaml`, see `policy.local.example.yaml`) ·
`.env.local-ui` · `deploy/.state*.json` (ids of what the deploy created). Tests never read them (`PINBOARD_POLICY_LOCAL=off` in `tests/conftest.py`; deploy tests use a non-existent local file).

## Rules that matter
* **Do not commit or push unless asked.** Take a backup (tarball) before moves or deletes.
* **The default path is pinned.** `tests/engine/test_default_path_golden.py` + `golden/default_path.json` record exactly what the engine does with default settings. Never regenerate the golden file
  (`PINBOARD_UPDATE_GOLDEN=1`) unless a behaviour change is intended and agreed. A new feature must be opt-in and leave that test untouched.
* **Cloud:** run billed or mutating commands only when asked; read-only diagnostics are fine; delete only what you created. Keep secrets out of chat, logs and files (secrets live in Secret Manager / `.env`).
* **Never touch** the user's own Docker container `pinboard-db` (port 5432) or their notebooks. Tests use the throwaway Postgres on port 55432.
* **Tests:** mutation-check important tests (break the code, see them fail, restore). Make a test fail for the RIGHT reason (`pytest.raises(..., match=...)`: one of ours passed for ages on an unrelated error).
  Never aim a tamper or mutation test at real repo folders (`skills_example/market-brief/` must stay byte-identical; a test checks it).
* **Edit scripts must assert their anchors** (a silent `str.replace` that matched nothing once skipped a migration). **Validation regexes end in `\Z`**, not `$` (`$` accepts a trailing newline).
* Do not `pkill -f` a pattern that also appears in your own command (it kills your shell); start background servers with a PID file.

## Platform facts that cost time (verified on the real services; the README's "Problems we ran into" has the stories)
* Agent Runtime delivers the request body **JSON-encoded twice**, and **every JSON number as a float** (`5` arrives as `5.0`): parse accordingly (`parse_call`, `_num`, `auto_approve.normalise`).
* A container deploy needs `agent_framework: langgraph`. A runtime update with a new image works in place; a **PSC interface cannot be added later**, so moving onto a network means a new runtime (blue/green: `deploy.sh deploy --state-file ...`).
* Unary calls up to 600 s work, 900 s are killed; streaming works past 5 minutes through Cloud Run and IAP. Cloud Storage calls from the runtime need `serviceusage.serviceUsageConsumer` on its service account.
* **Agent Gateway egress and a Postgres connection do not mix** (README, "Agent Gateway"); a PSC interface and a gateway cannot be set together. The working route is `docs/EGRESS.md`.
* `httpx` ignores address ranges in `NO_PROXY` (requests and Go honour them); Tavily's async client ignores proxy settings; both are handled in code and covered by `tests/agent/test_egress_proxy.py`.
* Sandbox creation takes 30 to 130 s: the warm pool (`warm_sandbox`) hides it. Gemini models newer than 2.5 are served only from the `global` location.

## State of the deployment
What is deployed and what is pending changes often: run `deploy/deploy.sh status` (and `deploy/network.sh status`), and read "How close to production it is" in the README for what is and is not in place.
