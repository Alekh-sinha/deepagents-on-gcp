# Agent Gateway spikes (kept as a record)

**Question:** can Pinboard run with all outbound traffic forced through Agent Gateway (default deny, an exact-hostname allowlist), keeping its Postgres checkpointer?

**Status (November 2026): run in four rounds. Answer: not with a Postgres checkpointer.** An egress gateway and a direct database connection did not work together in any configuration we tried.
The full results table, the documentation links and a notebook that reproduces the cases are in the main README ("Problems we ran into"). What we built and run instead is [`docs/EGRESS.md`](../../docs/EGRESS.md)
(a PSC interface, a proxy with a host allowlist, a private database address; no gateway). This folder is a record of how the tests were done, **not a recipe**: its scripts create billed resources, several steps assume names
from an earlier layout, and they have not been re-run since. They never modified the working runtime (a second runtime and its own database were used), and `down` deletes what they created.

| Round | What was tried | What happened |
|---|---|---|
| 1 | Our engine behind an egress gateway with 19 registered hostnames, an agent identity and the Cloud SQL Auth Proxy on a **public** address | Google API calls worked after fixes (Secret Manager over REST because gRPC does not pass the gateway's proxy; the `*.mtls.` host twins; permissions granted to the agent identity). The proxy's mutual-TLS connection to the database then failed (`tls: handshake failure`) |
| 2 | The gateway's `networkConfig` into a VPC, a **private-IP** database, the proxy with `--private-ip` | Google API calls first got the gateway's own default certificate. Retried with `GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES=false` (which Google's page sets): the database socket failed again |
| 3 | A PSC interface on the runtime **and** the gateway | The API refuses it: both cannot be set on one runtime |
| 4 | [`doc_agent/`](doc_agent/spike.py): Google's own documented LangGraph agent with its Cloud SQL checkpointer, with and without a gateway | Plain: works. With an egress gateway: fails at the database socket (`SSLV3_ALERT_HANDSHAKE_FAILURE`), i.e. the same failure in Google's own example |

**The unknowns we started with, answered:**

1. *Cloud SQL proxy to the instance's address under a hostname allowlist* → it does not work (rounds 1, 2 and 4).
2. *What an `AGENT_IDENTITY` needs, and its principal string* → permissions go to the identity, not a service account; a project-wide `principalSet` grant worked once the trust domain was right (`agents.global.org-<ORG_NUMBER>.system.id.goog` for a project inside an organization), and the identity pool only exists after a first successful deploy.
3. *Does a container with `agent_framework=langgraph` accept `agent_gateway_config`?* → yes: a bootstrap deploy without a database came up behind the gateway.
4. *Per-host `iap.egressor` grants (endpoint ids)* → the endpoint id is the last path segment of the registry service's `registryResource`.

**Files:** `gateway.sh` (stages `apis`, `vpc`, `sql`, `gateway`, `hosts`, `cert`, `build`, `deploy bootstrap`, `grant`, `deploy recreate`, `smoke`, `enforce`, `down-vpc`, `down`; its header explains each round),
`doc_agent/` (`agent.py` Google's documented agent, `spike.py` deploys it with and without a gateway, `run.sh` runs it in a throwaway environment with exactly the packages Google's page lists). Set `PB_PROJECT` for the doc agent.
The gateway starts in DRY_RUN (it logs what it would block) so a missing hostname shows up in logs instead of as a failure; `enforce` switches it on.
