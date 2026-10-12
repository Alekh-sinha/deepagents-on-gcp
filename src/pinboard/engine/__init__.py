"""The engine: the service that runs inside Agent Runtime (or locally with `pinboard serve`).

    service.py      RunService: turns, approvals, the lease-and-reap machinery, housekeeping (built from the two mixins below)
    sessions.py     SessionsMixin: conversations, rename / archive, the transcript, config for a frontend
    spare_pool.py   SparePoolMixin: the warm pool (one spare sandbox kept ready)
    errors.py       the errors the API maps to HTTP statuses
    worker.py       background workers and the lease that proves a worker still owns its turn
    events.py       turns the agent's stream into small, capped events for clients to follow
    api.py          the HTTP surface: Agent Runtime contract methods (the list the platform is told about) and the /v1 REST mirror
"""
