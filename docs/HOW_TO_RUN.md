# How to run Pinboard locally (CLI and API)

> This is the **local** walkthrough: the CLI and a laptop-hosted API. For the **web app** (and a no-cloud demo of it) see [../frontend/README.md](../frontend/README.md); for the cloud, [../deploy/README.md](../deploy/README.md); for the overview, [../README.md](../README.md).


Pinboard runs **on your laptop**. It uses **GCP** for two things: the sandbox where commands execute, and Gemini (via Vertex AI).
Tavily provides web search.

## 0. What you need
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- `gcloud` CLI, logged in, with a GCP project that has billing enabled
- A [Tavily](https://tavily.com) API key
- Docker (optional, only for the Postgres option in step 4)

## 1. One-time GCP setup
```bash
gcloud auth application-default login          # the code authenticates with these credentials (no key files)
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com
```
Your account needs the role **Vertex AI User** (`roles/aiplatform.user`). That covers both Gemini and the sandbox.

## 2. Install
```bash
cd pinboard-agent
uv sync --extra dev
python3 scripts/init_env.py        # creates .env from example.env (never overwrites an existing .env)
```

## 3. Fill in `.env`
Open `.env` and set at least these (placeholders like `your-...-here` count as "not set"):

| Setting | Value |
|---|---|
| `GCP_PROJECT` | your project id |
| `GCP_LOCATION` | `us-central1` (or your region) |
| `TAVILY_API_KEY` | your Tavily key |
| `MODEL` | a Gemini model your project can use, e.g. `google_genai:gemini-2.5-flash` |
| `GOOGLE_USE_VERTEX` | `true` (no Gemini API key needed) |

`SANDBOX_RUNTIME` stays empty: the first run creates it and writes it into `.env` for you.

## 4. Check the sandbox works
```bash
uv run pinboard ping
```
Expect a few minutes the first time, then `SANDBOX READY`. If it says `SANDBOX DID NOT START`, read the message and see Troubleshooting.

## 5A. Easiest way: the CLI (no server, no database)
```bash
uv run pinboard run "Build a short market brief and save it to out/brief.md"
```
Files the agent saves in `/workspace/out` are downloaded to `./outputs/<run id>/` before the sandbox is deleted.

## 5B. Server mode with approvals

**Pick where state lives**

- *No database (quickest):* in `.env` set `STATE_BACKEND=memory`. Works fully, but a restart loses parked approvals.
- *Postgres (approvals survive a restart):*
  ```bash
  docker run -d --name pinboard-db -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pinboard postgres:16
  ```
  and in `.env`:
  ```
  STATE_BACKEND=postgres
  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pinboard
  ```
  then `uv run pinboard db-setup`.

**Turn on approvals and set an API token.** The token is a password you invent; every request must send it.
```bash
openssl rand -hex 32        # no openssl? python3 -c "import secrets; print(secrets.token_hex(32))"
```
In `.env`:
```
APPROVAL_MODE=execute
PINBOARD_API_TOKEN=<paste the generated value>
```

**Start the server** (leave this terminal open; logs appear here):
```bash
uv run pinboard serve --port 8080
```

**In a second terminal:**
```bash
export PINBOARD_API_TOKEN=<same value as in .env>
H="Authorization: Bearer $PINBOARD_API_TOKEN"

# 1) Start a run. This request BLOCKS for a minute or two while the sandbox is created.
curl -s -m 900 -X POST localhost:8080/v1/runs -H "$H" -H 'content-type: application/json' \
  -d '{"prompt":"Run ls -la in the workspace and tell me what you see"}' | python3 -m json.tool
#    expect "status": "awaiting_approval"  -> nothing has executed yet

# 2) See exactly what it wants to run
curl -s "localhost:8080/v1/approvals?status=pending" -H "$H" | python3 -m json.tool

# 3) Approve it (use the approval "id" from step 2) ... or reject
curl -s -m 900 -X POST localhost:8080/v1/approvals/<id>/decision -H "$H" -H 'content-type: application/json' \
  -d '{"decision":"approve"}' | python3 -m json.tool
#    reject:  -d '{"decision":"reject","message":"not now"}'

# 4) Audit trail (what happened, including blocked commands)
curl -s localhost:8080/v1/runs/<run id>/audit -H "$H" | python3 -m json.tool
```
**Restart test (Postgres only):** after step 1, stop the server with Ctrl+C, start it again, then do step 3. The approval and the
paused agent come back from the database.

## 6. Run the tests (optional)
```bash
uv run pytest                      # no database needed; the Postgres tests are skipped
docker run -d --name pbtest-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=pbtest -p 127.0.0.1:55432:5432 postgres:16
export PINBOARD_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/pbtest
uv run pytest                      # also runs the Postgres tests
```
The Postgres tests drop and recreate the `pb_*` tables: point the URL at a throwaway database (a separate container on its own port, as above), never at one you care about, including the `pinboard-db` container of step 5B.

## 7. Clean up
```bash
uv run pinboard runtimes                       # list Agent Platform runtimes created in your project
uv run pinboard runtimes --delete <full name>  # delete one by exact name (asks you to type 'delete')
docker rm -f pinboard-db                       # remove the local Postgres
```
A one-off run's sandbox is deleted when the run finishes; a conversation's sandbox when it has been idle for `limits.sandbox_idle_minutes`; and every sandbox expires on its own after `SANDBOX_TTL_SECONDS` (default 1 hour).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `pinboard: command not found` | use `uv run pinboard ...`, or `source .venv/bin/activate` first |
| `DefaultCredentialsError` | run `gcloud auth application-default login` |
| `PERMISSION_DENIED` | your account lacks `roles/aiplatform.user`, or the Vertex AI API isn't enabled |
| `401` from the API | token missing or different from the one in `.env` |
| Server won't start: "Secret ... is not set" | `DATABASE_URL` or `PINBOARD_API_TOKEN` is still a placeholder |
| Gemini "model not found" | change `MODEL`, or set `MODEL_LOCATION=global` |
| Run `"status": "failed"` | read its `error` field |
| `command blocked ... parse` in logs | the shell guard refused a malformed command; the agent normally recovers by itself |
| New runtimes keep appearing | `SANDBOX_RUNTIME` is empty in `.env`: copy the name the first run printed |

## What has and hasn't been tested
Tested with scripted models and fakes (`uv run pytest`): guards, vault, skills, the sandbox class, the whole agent loop, both state stores on real Postgres, park/approve/reject/edit/expire including a simulated restart, the HTTP API, and a real `pinboard serve` process.
Tested against the real services: `scripts/verify_real.py` runs the agent with real Gemini and the real sandbox service (streaming, approvals, restart while waiting, cancel, artifacts, e-mail, GitHub, parallel helpers, MCP); `scripts/smoke_runtime.py` tests a **deployed** engine;
the Docker image, Cloud SQL, the Agent Runtime contract routes and the Cloud Run web backend are all deployed and smoke-tested (see [../README.md](../README.md), "How close to production it is", for what is not).
This page covers only the local CLI and API.

## 8. Conversations (sessions and turns)

A **session** is one conversation. Each message you send starts a **turn**: it returns at once and the agent works in the background. You follow the
turn through its **event log**; a turn that needs your approval stops and waits (it can wait up to `APPROVAL_TTL_SECONDS`, and survives a restart).
The session keeps its own sandbox between messages, so files you created earlier are still there; an idle sandbox is deleted after
`limits.sandbox_idle_minutes` (see `config/policy.yaml`), and the agent is told if it has been replaced.

```bash
H="Authorization: Bearer $PINBOARD_API_TOKEN"
S=$(curl -s -X POST localhost:8080/v1/sessions -H "$H" -H 'content-type: application/json' -d '{"title":"my chat"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
T=$(curl -s -X POST localhost:8080/v1/sessions/$S/messages -H "$H" -H 'content-type: application/json' -d '{"text":"Prepare a short market brief"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -sN "localhost:8080/v1/turns/$T/stream" -H "$H"          # one JSON event per line; ends when the turn finishes or needs you
curl -s "localhost:8080/v1/approvals?status=pending&session_id=$S" -H "$H"   # what is waiting for you (plan, question, email, commit, command)
curl -s -X POST localhost:8080/v1/approvals/<id>/decision -H "$H" -H 'content-type: application/json' -d '{"decision":"approve"}'
curl -s -X POST localhost:8080/v1/turns/$T/cancel -H "$H"      # stop a turn
curl -s localhost:8080/v1/models -H "$H"                       # models you may choose (pass "model" when creating a session or sending a message)
```
Answer a question the assistant asked with `-d '{"decisions":[{"type":"respond","message":"markdown please"}]}'`. If the server restarts mid-turn, run
`curl -s -X POST localhost:8080/v1/maintenance -H "$H"` (a scheduler does this every few minutes in the cloud): lost turns are found and resumed once.

## 9. Saved files (artifacts)

Whatever the assistant saves in its output folder is kept for **you** (not for one conversation or sandbox) at the end of every turn, so you can
find it days later. Locally the files go to `ARTIFACTS_URI` (default `./artifacts`); in the cloud set it to `gs://your-bucket/prefix`.
The assistant can also list your saved files and bring one back into a fresh sandbox when you ask for it.
```bash
curl -s "localhost:8080/v1/artifacts?session_id=$S" -H "$H"             # files from one conversation (omit session_id for all of yours)
curl -s localhost:8080/v1/artifacts/<id>/content -H "$H" -o report.md    # download one
```
A conversation's sandbox is created only when a tool first needs it, so a chat-only reply is immediate; the first command or file operation
waits for it to start (30 to 130 seconds; the warm pool in the web app hides most of that).
