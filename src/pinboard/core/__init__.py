"""The rules and settings everything else relies on, with no cloud or LangGraph code:

    config.py       Settings read from .env / the environment (no secrets in here)
    policy.py       config/policy.yaml: who may use the app, who may be e-mailed, which repos and models; fails closed
    guardrails.py   CommandGuard (shell allowlist), PromptGuard (prompt-injection check), wrap_untrusted
    envfile.py      reading and writing .env safely
"""
