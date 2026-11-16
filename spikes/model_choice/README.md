# Helper-model prototype (now part of the product)

**Question:** can the orchestrator choose WHICH model a helper runs on, per call, without changing the library?

**Status: proved, and absorbed into the product.** The product version is `src/pinboard/agent/delegation.py` (`ModelChoiceSubAgentMiddleware`), wired in `agent/builder.py` and switched by the policy
(`models.helper_model_choice: on|off`; each model in `models.allowed` has a `rank`, a `use_for` hint and a `location`). It differs from this prototype by keeping each helper's approval gates and the product's sandbox backend,
and by taking the pool from the policy instead of `models.yaml`. It was also checked on real Gemini (`scripts/verify_real.py`, scenario S16) and every pool model passes `scripts/qualify_models.py`.
This folder is the original prototype, kept as a record; its 11 offline tests still pass (`uv run pytest spikes/model_choice -q`).

**What the prototype established** (scripted and real):

* A compiled graph switches model per call from `context={"model": id}` passed to its own `invoke` (the same mechanism the product's `ModelSelectionMiddleware` uses for the main agent).
* The library's `task` tool runs helpers with the **parent's** ambient context, so every helper gets the same model, and overriding that from a wrapper did not work. A per-call model therefore needs our own tool body (`delegate`)
  that invokes our own helper graphs, built with the library's public `create_sub_agent` plus the same middlewares it adds privately.
* The library's `task` is hidden from the model (and a hallucinated `task` call is refused), the prompt lists only the models at or below the user's chosen rank, and a helper never gets a model above that ceiling.
* One real run each (Apple's financials, Tavily): with the ceiling off, a `gemini-2.5-flash` orchestrator ran its researcher on `gemini-3.5-flash` (better and more recent answer, about 4 times as long); with it on, the request was refused and the helper
  ran on `gemini-2.5-flash`. One sample each: it shows the trade-off, not a benchmark. The ranks in `models.yaml` were a guess; the policy's ranks are the user's to set.

**Files:** `models.yaml` (the pool), `pool.py` (`PoolSelectionMiddleware`), `delegating.py` (the `delegate` tool), `test_offline.py` (11 scripted checks, no network),
`run_real.py` (needs `.env`: `GCP_PROJECT`, `TAVILY_API_KEY`; `--no-ceiling` lets a `gemini-2.5-flash` orchestrator run the helper on `gemini-3.5-flash`).
