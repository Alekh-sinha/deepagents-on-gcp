> **Historical.** This was the runbook for the first spike in October 2026, written for a session in which each command was run by a person and the output pasted back to an assistant (hence "send me").
> The spike has been run, its resources deleted and its answers are in [README.md](README.md). The commands are kept as a record of how it was done: they have not been re-run since, and steps 3 and 5
> create billed resources (Cloud SQL, an Agent Runtime) until `down`. Sections 3 and 4 were instructions for that session. For a deployment you actually want, use `deploy/deploy.sh` (see the main README).

# Spike runbook (you run it, then paste the output back)

All commands run from `pinboard-agent/`. Total time about 30-40 min (Cloud SQL creation is the slow part). Paste back **everything the
`probe` step prints** plus anything that errors.

## 0. Before you start
```bash
gcloud config list                              # confirm account + project are the ones you want to spend on
gcloud auth application-default login           # the SDK and probe use ADC, so it must be the SAME account as above
gcloud auth application-default set-quota-project <PROJECT_ID>
uv sync --extra dev
```
If the project is wrong, add `--project <ID>` before the subcommand (e.g. `python spikes/verify.py --project my-proj setup`).
Your account needs Owner (or Editor + Service Account Admin + Project IAM Admin) for `setup` to grant roles.

## 1. Steps
| # | Command | What it does | Time | Billed? |
|---|---|---|---|---|
| 1 | `uv run python spikes/verify.py preflight` | read-only: shows account, project, which APIs are enabled | secs | no |
| 2 | `uv run python spikes/verify.py setup` | enables APIs, Artifact Registry repo, runtime service account + roles | ~2 min | no |
| 3 | `uv run python spikes/verify.py sql-up` | Cloud SQL Postgres 16 `db-f1-micro`, db + user, password -> Secret Manager | 5-10 min | **yes, until `down`** |
| 4 | `uv run python spikes/verify.py build` | Cloud Build builds the probe image from `spikes/probe/` | ~2 min | pennies |
| 5 | `uv run python spikes/verify.py deploy` | creates the Agent Runtime from that image (1 instance) | 3-10 min | **yes, until `down`** |
| 6 | `uv run python spikes/verify.py probe` | runs every check, prints PASS/FAIL, writes `spikes/results.json` | ~12 min | small |
| 7 | `uv run python spikes/verify.py probe --long` | OPTIONAL: also holds requests open 600/900/1800 s. Only if the normal probe shows 300 s PASSED | ~50 min | small |
| 8 | `uv run python spikes/verify.py down` | lists everything it will delete, asks you to type `delete` | ~5 min | stops billing |

Run `down` as soon as you have the results (you can re-run `deploy`/`probe` later, state is in `spikes/.state.json`).
If a step fails, send me the error: steps are safe to re-run (`setup`, `build` are idempotent; if `sql-up` or `deploy` fails halfway, say so before retrying).

## 2. What each probe line means
| Probe | Answers | If it FAILS, it probably means |
|---|---|---|
| Q1 `:query` | the invoke URL + our contract routes + `class_methods` format work | wrong REST shape or `class_methods` format: I adjust `api.py`/deploy config |
| Q1b `:streamQuery` | streaming route works | same, for the stream route |
| Q2 identity | which service account / project number the container sees | should show `pbspike-runtime@...`; if the default platform SA, `service_account` was ignored |
| Q3 unary 30/60/120/300 s | longest single request that survives | the first FAILED duration is the timeout; decides streaming vs polling |
| Q3 streaming 300 s | same for streams (heartbeat every 10 s) | if unary fails but streaming passes, we stream turns |
| Q4 background work | a thread keeps ticking between requests, same instance | FAIL = no work after the response; async "return run id" needs another design (stream or Cloud Run Jobs/Tasks) |
| Q5 egress | reach api.github.com, api.agentmail.to, api.tavily.com, example.com, secretmanager, api.ipify.org (shows egress IP) | blocked egress: connectors need a NAT/PSC-I path before Agent Gateway even matters |
| Q6 Secret Manager | runtime SA reads a secret (length + hash only) | IAM role missing, or no route to the API |
| Q7 Gemini | runtime SA calls `gemini-2.5-flash` on Vertex | model access/region problem |
| Q8 Cloud SQL | connector reaches the DB, creates a table, inserts, reads back (shows connect time) | the key one: FAIL means we need PSC-I/private IP or a different state store |

## 3. What to send me
1. The full `probe` output (the `[PASS]/[FAIL]/[INFO]` lines and the final `N/M ok`).
2. Any error from steps 2-5 verbatim.
3. Answers to: was the `deploy` step quick (<3 min) or slow? Did you see anything in the Cloud Console under Agent Platform > Agent Runtime /
   Agent Registry for `pbspike-probe` (auto-registered or not)?
4. Confirmation you ran `down` (or that you are leaving it up).

## 4. Known soft spots in the spike itself (so you don't chase ghosts)
- `class_methods` use `{"name", "api_mode"}` only (from the Terraform docs); the platform may want fuller OpenAPI-style entries.
- REST bodies use `classMethod` (camelCase) per the reasoningEngines REST reference; if Q1 returns 400 about unknown fields I will switch to `class_method`.
- Q8 uses the public-IP connector path, not private IP; a FAIL there still leaves PSC-I (`psc_interface_config`) as the next thing to test.
- The `deploy` step creates the Runtime with `min_instances=1`; real cold-start behaviour is not measured here.
