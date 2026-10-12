# Helper-model prototype

Question: can the orchestrator choose WHICH model a helper runs on, without changing the library?

- `models.yaml`: the pool (id, rank, location, what each is for). Ranks decide the ceiling: a helper never gets a model ranked above the one the user selected.
- `pool.py`: loads the pool; `PoolSelectionMiddleware` swaps a graph's model from its run context (`context={"model": id}`).
- `delegating.py`: `ModelChoiceSubAgentMiddleware` = a `delegate(description, subagent_type, model?)` tool + a prompt section + hiding the library's `task`.
  Helpers are compiled once (library's public `create_sub_agent`); each call passes the chosen model as the helper's run context.
- `test_offline.py`: 11 scripted checks, no network (`uv run pytest spikes/model_choice -q`).
- `run_real.py`: Apple's financials through a `researcher` helper on real Gemini (needs `.env`: GCP_PROJECT, TAVILY_API_KEY).
  `--no-ceiling` lets a gemini-2.5-flash orchestrator run the helper on gemini-3.5-flash.

Nothing here is imported by the product yet.
