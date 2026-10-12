# Run your own agent in this engine

The engine (sessions, turns, streaming events, approvals, cancel, resume after a restart, schedules, the web app, deploy scripts) works on **any compiled LangGraph graph**.
Pinboard's own agent (deepagents + a sandbox + email/GitHub tools) is just the default one. To run another:

```bash
AGENT_BUILDER=my_package.my_module:build      # in .env, or an env var of the runtime
```

```python
# my_package/my_module.py
from langchain.agents import create_agent
from pinboard.agent.contract import AgentHandle, BuildContext

def build(ctx: BuildContext) -> AgentHandle:
    graph = create_agent(my_model, tools=[...], checkpointer=ctx.checkpointer)   # the checkpointer is required
    return AgentHandle(graph)
```

That is the whole contract (`src/pinboard/agent/contract.py` documents every field). Returning the bare compiled graph also works.

## What you get without writing anything else
| | |
|---|---|
| Streaming | model tokens, tool calls and results, todos, usage, as small events a UI can replay from any position |
| Approvals | anything that pauses the graph, a human-in-the-loop tool or a plain `interrupt(...)`, becomes a card (approve / edit / reject) or a question (free-text answer); the answer is fed back into your graph |
| Durability | a restart, a lost worker or a closed browser tab does not lose a turn; the conversation lives in your checkpointer |
| Control | cancel, resume, per-user sessions, schedules that run an instruction unattended, an allow-list of users |
| Deploy | `deploy/deploy.sh` (or Terraform) builds the image and the runtime; set `AGENT_BUILDER` through `.env` / `extra_env` |

## Switching off what you do not need
`config/policy.yaml`:

```yaml
features:
  sandbox: false      # no shell / file workspace: nothing is created, cleaned up or billed
  artifacts: false    # (follows `sandbox` unless set) no files are kept for the user
warm_sandbox: {mode: off}   # must be off without a sandbox: a start-up check refuses the other combinations
```

With `sandbox: false` the deploy tools also stop asking for `SANDBOX_RUNTIME`. Both switches default to **on**, which is how Pinboard runs, and with the defaults the
engine takes exactly the same path as before (`tests/engine/test_default_path_golden.py` pins it).

## Things to know
* `build` runs for every segment of a turn (first run, and again after each approval): keep it cheap, cache heavy objects.
* Helper blocks in the UI appear for the tools `task` and `delegate` (deepagents). An agent without them simply shows its steps as ordinary tool calls.
* The engine package still imports deepagents (Pinboard's default agent lives next to it), so it must be installed in the image even if your agent does not use it.
* Choosing the model per message (`model` in a request) needs Pinboard's `ModelRouter`; an agent that returns `AgentHandle(graph)` without one uses its own model.
* Your module must be importable in the container: add it to the image (the `Dockerfile` is the place) and set `PYTHONPATH` if needed.

## What is verified, and what is not
Verified (tests, local, with fakes): a second agent written only against this contract, with no deepagents, no sandbox and no `task`/`delegate`, runs through the real engine on the memory
and Postgres stores: chat, tool calls, approve / reject, a plain `interrupt`, cancel, restart in the middle of a pause, legacy runs, housekeeping, and every sandbox/artifact entry point
answers as "off" without touching the platform (`tests/engine/test_custom_builder.py`, `tests/engine/toy_agent.py`).
**Not verified:** a custom agent deployed to Agent Runtime, or against a real model. Treat the first deployment of your own agent as a test.
