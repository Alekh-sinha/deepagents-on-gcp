# Agent Gateway spike (M9)

Question: can Pinboard run with ALL outbound traffic forced through Agent Gateway (default deny, exact-hostname allowlist)?
Run the stages of `gateway.sh` in order (`apis, gateway, hosts, cert, db, build, grant, deploy, smoke`) and paste the output of each.
The gateway starts in DRY_RUN (logs what it would block, blocks nothing) so a missing hostname shows up in logs instead of as a failure; then `enforce`.
Everything is a hypothesis from the docs; nothing here has been run. The working runtime is never modified. `down` deletes the spike.
Biggest unknowns: (1) Cloud SQL proxy to the instance IP:3307 under a hostname allowlist, (2) what permissions an AGENT_IDENTITY needs and its principal string,
(3) whether a BYOC container with `agent_framework=langgraph` accepts `agent_gateway_config`, (4) per-host `iap.egressor` grants (endpoint ids).
