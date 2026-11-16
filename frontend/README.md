# privai — the web app

A light, chat-first interface for the assistant: conversations with live streaming, **approvals as cards** (with "approve automatically" when you want it),
helpers shown with the model they ran on, **scheduled tasks**, and **connectors** (bring your own MCP servers). React 18 + TypeScript + Tailwind, built with Vite.

It runs **on your computer** and talks to the privai web backend, which talks to the engine in Google Cloud. Nothing here is deployed.

```
browser (this app, http://localhost:5173)
   │  /api  (Vite forwards it, so the browser only ever talks to its own origin)
   ▼
privai web backend, on your computer   uv run pinboard web --port 8081
   │  your Google credentials (gcloud auth application-default login)
   ▼
the engine (Agent Runtime in your Google Cloud project)  →  Gemini, sandbox, Cloud SQL, MCP servers, schedules
```

## Run it

You need Node 20+ (`npm install` once in this folder) and, for the cloud, `gcloud auth application-default login`.

**A. Try it with no cloud and no cost** (a scripted demo model, a fake sandbox, a demo MCP server; the real engine and web backend):

```bash
uv run python scripts/dev_stack.py        # terminal 1: web backend on :8081, signed in as dev@example.com
cd frontend && npm run dev                # terminal 2: http://localhost:5173
```
The demo model reads keywords in your message (`shell`, `plan`, `ask`, `parallel`, `research`, `file`, `todo`, `mcp`, `slow`, `fail`; the list is at the top of
`scripts/dev_stack.py`). The MCP server's address is printed at start-up: paste it in **Connectors**.

**B. Against your real deployment** (the engine must be deployed: see [../deploy/README.md](../deploy/README.md); `scripts/run_local_ui.sh start|stop|status|logs` runs this for you from the settings in `.env.local-ui`):

```bash
# terminal 1: the web backend on your computer, talking to the cloud engine as you
WEB_AUTH=dev DEV_USER_EMAIL=you@gmail.com \
RUNTIME_RESOURCE=projects/PROJECT_NUMBER/locations/us-central1/reasoningEngines/ENGINE_ID \
uv run pinboard web --host 127.0.0.1 --port 8081
cd frontend && npm run dev                # terminal 2
```
`DEV_USER_EMAIL` must be on `allowed_users` in your policy (`config/policy.local.yaml`, merged over `config/policy.yaml`). `WEB_AUTH=dev` signs you in as that one user, which is fine on your own computer.

**C. With "Sign in with Google"** (real sign-in; what you would use if the backend were hosted):

1. In the Cloud console: *APIs & Services > Credentials > Create credentials > OAuth client ID > Web application*. Add `http://localhost:5173` under
   *Authorised JavaScript origins* (and the address you will use later, if any). Configure the consent screen first if asked.
2. Run the backend with the client id and a session secret:
   ```bash
   WEB_AUTH=google GOOGLE_OAUTH_CLIENT_ID=123-abc.apps.googleusercontent.com SESSION_SECRET=$(openssl rand -hex 32) \
   RUNTIME_RESOURCE=... uv run pinboard web --host 127.0.0.1 --port 8081
   ```
Only accounts on `allowed_users` can sign in (the engine decides, from the same policy). The browser gets a signed, HttpOnly cookie, never a token it could read.

To serve the built app from the backend instead of Vite: `npm run build`, then run the backend with `STATIC_DIR=frontend/dist` (deep links and reloads work).

## Tests

```bash
npm test                                                    # unit and component tests (vitest)
npm run typecheck
npx playwright test                                         # the whole UI in a real browser against the dev stack (starts it by itself)
npx playwright test -c playwright.login.config.ts           # the Google sign-in flow against a second dev stack, with a stub in place of Google
```
Screenshots of every page are written to `frontend/screenshots/` by the browser tests.

## How it is put together

| Folder | What is in it |
|---|---|
| `src/api` | the typed client (CSRF header, readable errors), the live-event stream (reconnects by itself), the shapes the backend returns |
| `src/state` | `transcript.ts` turns the engine's event log into what the chat shows (pure functions, replay-safe); `blocks.ts` groups helper steps per helper run; `chat.ts` is one conversation (load, stream, send, answer, stop); `auth.tsx`, `app.tsx`, `nav.tsx` |
| `src/components/approvals` | one card per kind of request (command, email, GitHub, plan, question, free-text pause, connected tool, other) with edit, reject and "approve all" for a batch |
| `src/components/chat` | transcript, tool rows, helper blocks, progress panel, composer with the model picker and the automatic-approval menu |
| `src/pages` | sign-in, scheduled tasks, connectors, files, settings |

The backend's contract is in `src/pinboard/web/app.py`; every route there is a thin relay to one engine method.

**Connectors and where servers may live.** When the policy limits MCP servers (`mcp.scope: vpc_or_allowlist`), the Connectors page shows the rule in plain words, taken from the engine's `get_config`: only servers inside the private network range, or on the approved list, can be added;
anything else is refused with a message. See [../docs/EGRESS.md](../docs/EGRESS.md#mcp-servers-where-they-can-live).
