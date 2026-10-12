# PLAN: Pinboard Phase 2 (connectors, sessions, Agent Runtime, frontend)

> **2026-10-09:** module files named below (`service.py`, `api.py`, `state.py`, `policy.py`, ...) were moved into packages `core/`, `state/`, `engine/`, `web/`; see the layout in CLAUDE.md. The design itself is unchanged.


Status: **plan only, no product code written yet.** Written 2026-10-07 after the GCP spike (findings in CLAUDE.md "Verified facts").
Milestones M0-M5 are local (docker Postgres + fakes, real GCP only for the sandbox in M5). M6 is the first real deployment.

## 1. Target architecture
```
 browser ──IAP (Google login, allowlisted emails)──► Cloud Run: React UI + BFF (own service account)
                                                         │  REST :query / :streamQuery  (identity token, user_id = IAP email)
                                                         ▼
 Agent Runtime (reasoning engine, container, runs as runtime SA, min_instances=1)
   one image = pinboard app + Cloud SQL Auth Proxy
   contract routes /api/reasoning_engine, /api/stream_reasoning_engine  →  RunService
        │ worker thread per turn (agent.stream → events)         │ tools run in this trusted process:
        ▼                                                         ▼ web_search, send_email, github_*, submit_plan, ask_user
   Cloud SQL Postgres (sessions, turns, events, approvals, audit, LangGraph checkpoints)
        │
        ├─ Agent Platform shell sandbox (per session, lazy, 1 h TTL, no secrets, no internet)
        ├─ GCS: skills (gs://…/skills) and session artifacts (gs://…/sessions/<id>/out)
        ├─ Secret Manager (AGENTMAIL_API_KEY, GITHUB_APP_PRIVATE_KEY, TAVILY_API_KEY, DB password, …)
        └─ Vertex AI (Gemini; model chosen per session/message from an allowlist)
```
Local dev uses the same RunService behind the existing `/v1/*` REST routes (bearer token) with `STATE_BACKEND=postgres` on docker.

## 2. Design decisions
**D1. A turn is a background worker plus an event log; clients tail the log.**
`send_message` (fast, unary) creates a turn and starts a worker thread that runs `agent.stream(...)` and appends every event to `pb_events`.
`stream_turn` (streaming) tails `pb_events` from a sequence number and sends heartbeats; any instance can serve it, a dropped connection or the ~10 min
unary ceiling never loses work, and the UI can reconnect. Spike facts this rests on: unary survives 600 s and dies at 900 s; streaming survived 300 s
(longer untested); a background thread kept running between requests (Q4). Risk: the worker lives in one instance. Mitigation: `pb_turns.heartbeat_at`
every few seconds; a reaper (in `expire`, also lazily on `get_*`) marks turns with a stale heartbeat `interrupted`; LangGraph checkpoints make
"resume" (`invoke(None, config)`) possible. Cap concurrent turns per instance (setting).
*Stream lifetime rule (discussed with user):* we do not know whether the platform limit is idle time or total time (unary died at 900 s but sent no bytes; streaming proven only to 300 s). So the design must not depend on it:
(a) `stream_turn` emits a server-side `heartbeat` every 10-15 s, independent of agent activity (tool-call events alone are too sparse: sandbox creation or a long command is silent for minutes);
(b) the server ends each stream cleanly after ~4-5 min with `{"type":"reconnect","after_seq":N}`; the BFF reconnects behind a single SSE stream to the browser;
(c) tool-call / todo / "sandbox warming" / token events are for UX, not keepalive. A turn waiting on an approval holds no connection. An open stream during a turn also keeps the instance's CPU allocated.
*Worker safety (discussed with user):* a cut stream never affects the worker, and a throttled/slow worker only delays the turn. But a throttled worker is indistinguishable from a dead one, so:
(a) **lease/fencing**: each worker holds `pb_runs.lease_id` (random token + `lease_expires_at`, renewed with the heartbeat); every event/state write is `... WHERE lease_id = :mine`, and a worker that loses its lease stops itself on its next write, so a resumed worker and a woken stale one can never both act;
(b) resume = `invoke(None, config)` from the **last LangGraph checkpoint** (not the exact point): the step in flight repeats, so side-effecting tools are idempotent: record `(turn_id, tool_call_id)` in `pb_audit` / a `pb_effects` table BEFORE the effect and skip if already present;
(c) **auto-resume policy**: the reaper (`expire`, Cloud Scheduler every few min) resumes a turn whose worker was lost, once (`limits.max_auto_resumes: 1`), if it was not waiting on a human gate. A second loss leaves it `interrupted` with the error visible; nothing is deleted (session, checkpoint stay; sandbox expires normally) and the user can press Resume (`resume_turn`) or send a new message (dangling tool calls are repaired by deepagents' `patch_tool_calls`). Agent-level errors (model error, policy refusal) mark the turn `failed` and are never auto-resumed.
Optional M6 test: stream with 10 s heartbeats held open 900 and 1800 s to raise the 5-minute cap and reduce reconnects.

**D2. Every gate is a deepagents `interrupt_on` tool, one resume protocol.** Verified in installed code: decisions are `approve | edit | reject | respond`.
| Gate (approval `kind`) | Tool | Allowed decisions | What the UI shows |
|---|---|---|---|
| shell | `execute` | approve, edit, reject | exact command |
| email | `send_email` | approve, edit, reject | to / cc / subject / body / attachments |
| github | `github_create_branch`, `github_commit_files`, `github_open_pr` | approve, edit, reject | repo, branch, file diffs, PR title/body |
| plan | `submit_plan(tasks[{title, detail, assignee}])` (no-op tool, returns "plan approved") | approve, edit (user edits tasks), reject (+feedback) | the board; approving also authorizes delegation to the named subagents |
| question | `ask_user(question, options?)` (no-op tool) | respond | question + input box |
Reads (`list_emails`, `read_email`, `github_read_file`, `github_list_repos`, `web_search`) never interrupt. The subagent tool is `task` (verified name); it is NOT gated
(the plan gate covers delegation). One `decide` method resumes any gate; multiple simultaneous interrupts are resolved by index like today's `decisions` list.
Do not gate `write_todos` (called repeatedly to tick items); the UI renders the board from todo state/events.

**D3. Sandbox = per-session disposable resource.** Session ↔ LangGraph `thread_id` (durable in Postgres). Sandbox is created in the background at `create_session`
(warm, takes minutes) and re-created when `sandbox_expires_at` passes; chat-only turns never wait for it. Skills: use `CompositeBackend` (verified present) routing
`/skills/` to a local read-only copy so the skills middleware (`before_agent` lists skills through the backend) doesn't force a sandbox; the same skills are also synced
into the sandbox for script execution. After each turn, `/workspace/out` is copied to `gs://…/sessions/<id>/out/`; a new sandbox restores it. System prompt tells the agent its
sandbox may be fresh and where deliverables live. Parked approvals no longer keep a sandbox alive unless the pending action is `execute`.
*Verify during M5:* the path mapping between the composite skills route and the in-sandbox copy; fallback = eager warm sandbox and the first sandbox tool call waits.

**D4. Policy file** `config/policy.yaml` (loaded by `policy.py`, path via `PINBOARD_POLICY`; shipped in the image and overridable from GCS later). Enforced **inside tools before an approval
is requested**, so a disallowed action is refused immediately and the human never sees it:
```yaml
email:  {from_inbox: privai-agent-0909@agentmail.to, allowed_recipients: [alekh.sinha09@gmail.com], max_per_day: 20, max_attachment_mb: 5}
github: {allowed_repos: [], branch_prefix: "agent/", never_default_branch: true}   # [] = every repo the installation can see
models: {default: gemini-2.5-flash, allowed: [{id: gemini-2.5-flash, provider: google_genai, label: "Gemini 2.5 Flash"}]}
limits: {max_turn_minutes: 30, max_concurrent_turns: 4, max_auto_resumes: 1, sandbox_idle_minutes: 20}
```

**D5. Models.** `list_models` returns the allowlist from policy; `create_session(model=)` / `send_message(model=)` select one (default gemini-2.5-flash); anything not in the list is rejected.
The snippet "`invoke(..., context={"model": ...})`" is not built in. Implement with `context_schema` + an `AgentMiddleware.wrap_model_call` that reads `request.runtime.context["model"]`
and returns `handler(request.override(model=<cached chat model>))` (`ModelRequest.override` verified to exist). Non-Gemini models (Claude on Vertex etc.) are untested: list them only after a test.

**D6. Auth.** Only the BFF's service account (and you, for debugging) may call the engine (IAM). The BFF takes `user_id` from the verified IAP header and passes it in every call; the
engine trusts `user_id` only because of that IAM boundary. The policy file also holds an `allowed_users` list checked in the BFF and the engine. Single human today, but the data model is multi-user.

**D7. One image, two modes.** `start.sh` starts `cloud-sql-proxy` when `SQL_CONNECTION` is set, then `pinboard serve`. `DATABASE_URL=postgresql://…@127.0.0.1:5432/pinboard?sslmode=disable`.
`SECRETS_BACKEND=gcp` in the cloud; `.env` locally. `agent_framework="langgraph"` at deploy.

**D8. Body parsing.** The platform delivers the contract body JSON-encoded twice. `api.py` must read the raw body and `json.loads` until it is a dict (replace `ContractIn`). First thing in M0.

## 3. API
### Agent Runtime `class_methods` (declare exactly these; `api_mode ""` = unary, `stream` = streaming)
| Method | Mode | Input → Output |
|---|---|---|
| `create_session` | unary | `{user_id, title?, model?}` → session; kicks off sandbox warm-up |
| `list_sessions` | unary | `{user_id, limit?}` → sessions |
| `get_session` | unary | `{user_id, session_id, after_seq?}` → session + messages + todos/plan + pending approvals + artifacts + active turn |
| `send_message` | unary | `{user_id, session_id, text, model?}` → turn (status running); 409 if a turn/approval is already active |
| `stream_turn` | **stream** | `{user_id, turn_id, after_seq?}` → ndjson events (below), ends on `turn_done`/`turn_error`/`awaiting_input` |
| `list_approvals` | unary | `{user_id, status?, session_id?}` → approvals (kind, preview, expires_at) |
| `decide` | unary | `{user_id, approval_id, decisions[]}` → turn (resumes in background; client then calls `stream_turn`) |
| `resume_turn` | unary | `{user_id, turn_id}` → turn (manual resume of an `interrupted` turn from its last checkpoint; not capped by `max_auto_resumes`) |
| `cancel_turn` | unary | `{user_id, turn_id}` → turn |
| `get_turn` | unary | `{user_id, turn_id}` → turn (+ audit optional) |
| `list_models` | unary | `{}` → allowed models + default |
| `list_artifacts` | unary | `{user_id, session_id}` → files with short-lived signed URLs |
| `expire` | unary | `{}` → expired approvals, reaped turns, deleted sandboxes (Cloud Scheduler, every few minutes, OAuth to `:query`) |
Events: `turn_started, token, tool_call, tool_result, todos, interrupt{approval_id, kind, preview}, artifact, heartbeat, turn_done, turn_error`. Each has `seq`, `ts`.
Aliases `query`/`stream_query` are optional (SDK/Playground only). Local `/v1/*` REST mirrors the same service calls.

### BFF (Cloud Run), thin: `POST/GET /api/sessions`, `GET /api/sessions/{id}`, `POST /api/sessions/{id}/messages`, `GET /api/turns/{id}/events` (SSE ← `stream_turn`),
`GET /api/approvals`, `POST /api/approvals/{id}/decision`, `GET /api/sessions/{id}/artifacts`, `GET /api/models`. Static React build served from the same service.

## 4. Data model (Postgres; existing `pb_*` tables kept, migrated idempotently in `state.setup()`)
- `pb_sessions`(id, user_id, title, thread_id, model, sandbox_name, sandbox_expires_at, created_at, last_active_at, archived)
- `pb_runs` becomes the **turn** table: add `session_id`, `model`, `heartbeat_at`, `lease_id`, `lease_expires_at`, `resume_count`, statuses `+interrupted, cancelled` (keep name to preserve tests; expose as "turn" in the API)
- `pb_effects`(turn_id, tool_call_id, tool, status, created_at) primary key (turn_id, tool_call_id): idempotency for email/GitHub writes
- `pb_events`(id bigserial, turn_id, seq int, type, payload jsonb, ts) index (turn_id, seq)
- `pb_approvals`: add `kind`, `preview jsonb`, `session_id`
- `pb_audit`: unchanged; add events `connector.email.sent`, `connector.github.commit`, `policy.denied`, `plan.approved`
- `pb_email_log`(day, count) or derive from audit for `max_per_day`

## 5. Code changes by module
*(Paths in this table pre-date the 2026-10-07 restructure into `agent/`, `gcp/`, `integrations/`; the current layout is in CLAUDE.md. New M4 modules: `events.py`, `worker.py`, `gcp/sessions.py`; M5: `gcp/artifacts.py`.)*
| Module | Change |
|---|---|
| `api.py` | raw-body parse (D8); new contract methods; keep `/v1` mirror |
| `service.py` | sessions, turn workers, event append, heartbeat/reaper, resume, cancel, per-gate approval building (kind + preview) |
| `agent.py` | `interrupt_on` for all gated tools; subagents (`researcher`, `coder`, `mailer`); `context_schema` + model middleware; new system prompt; CompositeBackend |
| `state.py` | tables above, both Memory and Postgres stores, same contract tests |
| `policy.py` (new) | YAML load/validate, helpers `check_recipient`, `check_repo`, `check_model`, daily email cap |
| `connectors/agentmail.py` (new) | `send_email`, `list_emails`, `read_email`; API key via vault; inbound content wrapped untrusted; attachments only from `/workspace/out` via the sandbox backend |
| `connectors/github_app.py` (new) | JWT (from `test_github_app.py`), installation token cached ~55 min, minted with `repositories=` and `permissions={contents:write, pull_requests:write}`; tools `github_list_repos/read_file/create_branch/commit_files/open_pr`; refuses default branch and non-`agent/` branches |
| `tools.py` | add `submit_plan`, `ask_user` no-op tools |
| `sandbox.py` | lazy/warm wrapper, expiry re-create, artifact backup/restore |
| `config.py`, `vault.py` | `SECRET_NAMES += AGENTMAIL_API_KEY, GITHUB_APP_PRIVATE_KEY`; settings `GITHUB_APP_ID, GITHUB_INSTALLATION_ID, AGENTMAIL_INBOX_ID, PINBOARD_POLICY, ARTIFACTS_GCS_URI` |
| `guardrails.py` | `wrap_untrusted` for email/GitHub content; PromptGuard on inbound mail text before it reaches the model |
| `Dockerfile`, `start.sh` | proxy + app (recipe proven in `spikes/probe`) |
| `pyproject.toml` | `agentmail`, `pyjwt`, `cryptography`, `requests`, `pyyaml` |

## 6. Milestones (order matters; each ends with tests passing and a short manual check)
| # | Deliverable | Verified by | Needs cloud? |
|---|---|---|---|
| M0 | Hygiene: root `.gitignore` (`*.pem`, `.env*`, `outputs/`, `spikes/.state.json`), move PEM to Secret Manager, delete local copy/rotate, first commit. Fix `api.py` double-encoded body. | new test posting a double-encoded body to both contract routes | no |
| M1 ✅ | `policy.py` + `config/policy.yaml`; model list + per-request model middleware (DONE; subagents need `model_aware`) | unit tests; scripted-model test that the chosen model is used and a disallowed id is rejected | no |
| M2 ✅ | AgentMail + GitHub connectors with policy checks, fake clients, vault wiring (DONE, verified on real services) | unit tests; manual: send one mail to the allowlisted address; list repos; create `agent/test` branch + PR on the installed repo, then delete | AgentMail + GitHub only |
| M3 ✅ | Gates: `interrupt_on` for all write tools, `submit_plan`, `ask_user`, subagents, prompt; approvals get `kind/preview` | scripted-model e2e per kind (approve/edit/reject/respond), "disallowed recipient never reaches approval" | no |
| M4 ✅ | (a) ✅ data layer + events, (b) ✅ worker + service + per-session sandbox, (c) ✅ contract methods + `/v1` mirror: Sessions + turns + events + worker/tail/reaper | Memory and Postgres contract tests; restart-recovery test (kill worker mid-turn → `interrupted` → resume) | docker Postgres |
| M5 ✅ | Lazy sandbox, CompositeBackend skills, artifacts (user-scoped, blob store + index, list/restore tools) — DONE; verified only with fakes | real run: create session, chat-only turn is instant, file-making turn works, wait/force expiry, new sandbox restores `out/` | GCP sandbox + GCS |
| M6 | Container + `deploy.sh` stage 1 + Cloud Scheduler `expire`; deploy to Agent Runtime; `scripts/smoke_runtime.py` calls every method through `:query`/`:streamQuery` | smoke script green on the real engine; one real "write a report and email it" with approval | yes |
| M7 | `terraform/` mirroring `deploy.sh` (APIs, AR, SA/IAM, Cloud SQL, secrets (values out of state), engine with `secret_env`, scheduler, bucket) | `terraform plan` clean against a fresh project or an import of the live one | yes |
| M8 | (+ warm spare sandbox: pool table, `warm_sandbox` method, typing trigger; see CLAUDE.md) Cloud Run BFF + React UI behind IAP: chat, plan board, approval cards per kind, artifacts, model picker | manual walkthrough; IAP denies a non-allowlisted account | yes |
| M9 | Hardening: Registry check, Agent Gateway egress allowlist (Vertex, Secret Manager, sqladmin + 3307, GitHub, AgentMail, Tavily, GCS), cost caps/alerts, runbooks. Optional: Memory Bank tools, inbound email trigger | deploy behind gateway; all smoke tests still pass; a blocked host is blocked | yes |
`deploy.sh` is written incrementally from M6 so Terraform (M7) has a tested reference.

## 7. Test strategy
Keep the existing style: scripted model + fakes (`tests/helpers.py`), Memory/Postgres parameterized contract tests, API tests through `TestClient`. New: policy tests, per-kind gate tests,
double-encoded body tests, worker/tail/reaper tests with a fake clock, connector tests against recorded fake HTTP. Real-service checks are scripts under `scripts/` (not pytest): `smoke_connectors.py`, `smoke_runtime.py`.

## 8. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Worker dies, or is throttled so hard it looks dead | heartbeat + lease/fencing + reaper + auto-resume once from checkpoint; `min_instances=1`; idempotent connector calls (`pb_effects` written before sending) |
| Unary ceiling 600-900 s; streaming ceiling unknown beyond 300 s | tail design; heartbeats; BFF reconnects with `after_seq` |
| Skills/sandbox laziness vs skills middleware | CompositeBackend; fallback eager-warm (M5) |
| Prompt injection via inbound email/GitHub text | untrusted wrapping, subagent least privilege, policy allowlists, human gates, no send/commit tool reachable from `researcher` |
| Email double-send on resume | log `(turn_id, tool_call_id)` before sending; skip if present |
| Agent Gateway default-deny breaks proxy/connectors | M9 only, with a tested allowlist; change bindings carefully (archives revisions) |
| Cloud SQL cost/IP exposure | instance has no authorized networks; proxy uses IAM (`cloudsql.client`); revisit private IP + PSC-I later |
| Terraform provider gaps for runtime fields | check `google-beta`; keep `deploy.sh` as the fallback path for the engine resource |

## 9. Needed from you
1. **Approve D1** (background worker + tail) vs. the simpler "one streamed request per turn". D1 costs more code in M4 but is the only design immune to the timeouts and dropped connections.
2. **GCP project:** keep `automation-ai-507911` (gcloud account `sanataniarushi1@gmail.com`) or use a dedicated project? Region stays `us-central1` unless you say otherwise.
3. Put `AGENTMAIL_API_KEY` in `pinboard-agent/.env` (never in chat), and tell me the GitHub App id and installation id (non-secret) when M2 starts.
4. Confirm the bucket/naming prefix (default `pinboard-<project>`).

## 10. M4 design (walkthrough agreed before coding)

**Finding that shapes the UI:** deepagents 0.7.22 does not include `write_todos` by default. `TodoListMiddleware` is now added explicitly; the stream carries `todos` state updates. Board = the approved plan (`submit_plan`) + live todo statuses.

**One execution path.** `RunService._execute` becomes the worker body, using `agent.stream(..., stream_mode=["updates","messages"], subgraphs=True)` (verified shapes: `messages` = AI/Tool message chunks, `updates` per node with a `todos` key for todo changes, `__interrupt__` for approvals, subagent events carry a `tools:<id>` namespace). Every event is appended to `pb_events`. Legacy synchronous `/v1/runs` stays (executor = inline) so the CLI and existing tests keep working.

**Pieces.** *(M4 code goes in: state additions in `state.py`; `events.py`, `worker.py`, the service/API changes at top level; the per-session sandbox provider in `gcp/sessions.py`.)*
1. `state.py`: `pb_sessions`; turn columns on `pb_runs` (`session_id, model, heartbeat_at, lease_id, lease_expires_at, resume_count, cancel_requested`, statuses `+interrupted, cancelled`); `pb_events` (append with lease fencing, read after seq); `pb_effects` + a Postgres `EffectLedger`. Memory + Postgres implementations behind the same contract tests.
2. `events.py`: maps LangGraph stream chunks to events `turn_started, token, assistant_message, tool_call, tool_result, todos, interrupt, artifact, heartbeat, reconnect, turn_done, turn_error`. Token deltas are coalesced (~250 ms / ~200 chars) before persisting.
3. `worker.py`: `TurnWorker` (claim lease -> build runtime -> stream -> append events -> finish/park), lease renewal + heartbeat thread, cancel check between chunks, `Executor` (thread pool sized by `limits.max_concurrent_turns`; inline executor for tests).
4. `service.py`: `create_session, list_sessions, get_session (rebuilt from events, no agent needed), send_message, tail_turn (generator), decide (resumes in background), cancel_turn, resume_turn, get_turn, list_models, list_artifacts, expire (reaper + approval expiry + auto-resume)`.
5. `api.py`: new contract methods (12 unary + `stream_turn`) and `/v1` mirrors; `parse_contract` already fixed.
6. **Sandbox is per SESSION in M4 (decided with the user; conversation continuity is the checkpoint, FILE continuity is the sandbox).** A `RuntimeProvider.acquire(session)` attaches to `pb_sessions.sandbox_name` while `sandbox_expires_at` is in the future (readiness-checked), otherwise creates a new one (eagerly, on the first turn that needs it), syncs skills only on creation, and records name + expiry (created + `SANDBOX_TTL_SECONDS`). A finished or parked turn DETACHES (never deletes). If the old sandbox is gone, the next turn gets a one-line note that its files are gone. `expire()` deletes sandboxes of sessions idle longer than `limits.sandbox_idle_minutes` (new, default 20) and expired ones. One active turn per session keeps the sandbox single-user. The legacy synchronous `/v1/runs` keeps its own throwaway sandbox that is deleted at the end.
   M5 then adds (artifacts are USER-scoped in a bucket, see CLAUDE.md): `gcp/artifacts.py` + `pb_artifacts` + agent tools `list_artifacts` / `restore_artifact`; lazy/background-warm creation (chat-only turns never wait), `CompositeBackend` for skills, and GCS backup/restore of `/workspace/out` across a sandbox replacement.
7. Subagents share the parent's thread and checkpointer (their pending approvals persist and resume; verified by tests). Their intermediate steps are NOT in the parent's messages, so the worker records them from the stream (`subgraphs=True`, namespace `tools:<id>`) as events. The checkpointer can also be read directly (no agent needed) as a fallback transcript.
8. New tests in M4: resume a subagent's pending approval after a simulated restart on real Postgres; sandbox reuse across turns; replaced-sandbox note; idle sandbox deletion.

**Turn state machine.** `queued? -> running -> awaiting_approval -> running -> completed | failed | cancelled`; reaper: `running` with expired lease -> `interrupted` (auto-resume once, then wait for the user). `failed` = the agent/model errored; `interrupted` = the worker was lost.

**Source of truth for the transcript** is `pb_events` (what the UI shows); the LangGraph checkpoint is only for the agent. Reads therefore never build an agent or a sandbox.

## M9b — State service on Cloud Run (so the runtime can sit behind the EGRESS gateway)  [planned 2026-10-09, not started]
Why: every documented configuration (public IP, private IP + gateway networkConfig, with `GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES=false`) fails at the Cloud SQL socket handshake behind the egress gateway (see CLAUDE.md). The gateway stays (central egress rules); only the Postgres hop changes to HTTPS.
Design: runtime (AGENT_IDENTITY, egress gateway, env var) --HTTPS, ID token--> Cloud Run `pinboard-state` (IAM required) --Cloud SQL built-in connector--> `pinboard-pg`. The container no longer needs the Cloud SQL proxy.
Auth: runtime gets an OIDC ID token for an invoker service account (`pinboard-state-invoker`) via IAM Credentials (`generateIdToken`); the agent identity needs `roles/iam.serviceAccountOpenIdTokenCreator` on it (pattern from Google's egress codelab); Cloud Run grants `roles/run.invoker` to that SA only. Gateway side: register the service's `*.run.app` host in Agent Registry + `roles/iap.egressor` for the agent principalSet.
Steps (each ends with a check; stop and report if a check fails):
 S0 SPIKE (decisive, small): trivial Cloud Run service (IAM-required, `/healthz`) + a tiny managed agent whose method fetches the ID token and calls it, deployed behind `my-egress-gw` with the env var. PASS = a 200 through the gateway. Also checks that a call to the iamcredentials host works. If S0 fails, STOP (the design is wrong) and report.
 S1 PROTOCOL: list every `StateStore` method + checkpointer method (`get_tuple, list, put, put_writes, delete_thread, get_delta_channel_history, get_next_version`); JSON-RPC style `POST /rpc/<method>`; checkpoint bytes via LangGraph `dumps_typed/loads_typed` base64 with an allowlist (`with_allowlist`) on the service; non-idempotent writes (`append_event`, `put_writes`) carry a client request id so a retry after a timeout cannot double-write.
 S2 SERVICE + CLIENT: `src/pinboard/state_service/` (FastAPI, real PostgresStore + PostgresSaver) and `RemoteStore` / `RemoteCheckpointer` in the app; `STATE_BACKEND=remote`, `STATE_SERVICE_URL`, `STATE_SERVICE_AUDIENCE`. 
 S3 VERIFY WITHOUT CLOUD: run the EXISTING state/turn/API test suites against a fourth backend `remote` (TestClient transport) so they must pass unchanged; add a checkpointer equivalence test vs `PostgresSaver` (same operations, same answers) incl. pause-for-approval and resume; mutation checks (drop request-id dedupe, drop fencing).
 S4 DEPLOY: `deploy.sh state` (build, Cloud Run, SA, invoker IAM, secret for DB URL via Secret Manager), Terraform resources, `smoke_runtime.py` unchanged.
 S5 INTEGRATE: runtime config (AGENT_IDENTITY, gateway, env var, remote state), registry host + egressor grant, new image without the proxy; `smoke --all` through the gateway; measure per-turn latency added.
 S6 HARDEN: caller must be the invoker SA; payload size caps; logs; gateway DRY_RUN -> ENFORCED; delete spike leftovers.
Open risks: ID-token minting by an agent identity (documented for the codelab, untested here); latency (~2 extra HTTP hops per graph step); BYOC image must trust the gateway CA (already in the Dockerfile via `deploy/extra-ca`); gRPC clients ignore the CA (we use REST everywhere that matters); Cloud Run cold start (min-instances 1 costs a little).
Decisions needed from the user: Cloud Run min instances (0 = cheaper, cold starts; 1 = always warm); clean up spike resources now (private SQL `pbgw-pg` is billed); order (S0 first, nothing else before it passes).
