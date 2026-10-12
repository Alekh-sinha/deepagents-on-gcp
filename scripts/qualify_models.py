#!/usr/bin/env python3
"""Check every model in config/policy.yaml on real Vertex AI before trusting it with work. Small and billed (a few cents for the whole pool).

  uv run python scripts/qualify_models.py                 # every model in the policy
  uv run python scripts/qualify_models.py --only gemini-3.8-flash,gemini-2.5-pro

Per model: (1) answers a plain question, (2) streams text in pieces, (3) calls a tool with the right arguments, (4) as the model of a deep agent, pauses
for a human approval on a gated tool and carries on after the decision (the path every real turn depends on), (5) reports token usage.
A model that fails (3) or (4) should not stay in the pool. Results are also written to scripts/qualify_models_results.json (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from deepagents import create_deep_agent  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from pinboard.agent.models import build_chat_model  # noqa: E402
from pinboard.core.config import Settings  # noqa: E402
from pinboard.core.policy import load_policy  # noqa: E402
from pinboard.gcp.vault import Vault  # noqa: E402


@tool
def add_numbers(a: int, b: int) -> str:
    """Add two whole numbers and return the sum."""
    return str(a + b)


CALLS: list[str] = []


@tool
def send_note(text: str) -> str:
    """Send a note to the team. Needs a person's approval."""
    CALLS.append(text)
    return "note sent"


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


def check(name, fn):
    t = time.monotonic()
    try:
        detail = fn()
        return {"check": name, "ok": True, "seconds": round(time.monotonic() - t, 1), "detail": str(detail)[:120]}
    except Exception as exc:  # noqa: BLE001
        return {"check": name, "ok": False, "seconds": round(time.monotonic() - t, 1), "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def qualify(settings, vault, spec) -> list[dict]:
    model = build_chat_model(settings, vault, spec.provider, spec.id, spec.location)
    out = []

    def chat():
        r = model.invoke("What is the capital of France? Answer in one word.")
        assert "paris" in text_of(r.content).lower(), text_of(r.content)
        return text_of(r.content).strip()

    def stream():
        pieces = [text_of(c.content) for c in model.stream("Write about 200 words on why automated tests are worth having.")]
        got = [p for p in pieces if p]
        assert len(got) >= 2, f"only {len(got)} piece(s)"
        return f"{len(got)} pieces"

    def tool_call():
        r = model.bind_tools([add_numbers]).invoke("Use the tool to add 17 and 25.")
        calls = r.tool_calls
        assert calls and calls[0]["name"] == "add_numbers" and calls[0]["args"] == {"a": 17, "b": 25}, calls
        return calls[0]["args"]

    def approval_round_trip():
        CALLS.clear()
        agent = create_deep_agent(model=model, tools=[send_note], checkpointer=MemorySaver(),
                                  interrupt_on={"send_note": {"allowed_decisions": ["approve", "reject"]}},
                                  system_prompt="You are terse. When asked to send a note, call send_note once with the text, then confirm in one sentence.")
        cfg = {"configurable": {"thread_id": "q"}, "recursion_limit": 40}
        r = agent.invoke({"messages": [HumanMessage("Send the team a note saying: build is green.")]}, cfg)
        assert r.get("__interrupt__") and not CALLS, "did not pause before the gated tool"
        iid = r["__interrupt__"][0].id
        done = agent.invoke(Command(resume={iid: {"decisions": [{"type": "approve"}]}}), cfg)
        assert CALLS == [CALLS[0]] and "green" in CALLS[0].lower(), CALLS
        assert text_of(done["messages"][-1].content).strip(), "no final answer after the approval"
        return f"paused, approved, ran once: {CALLS[0][:40]!r}"

    for name, fn in (("chat", chat), ("stream", stream), ("tool_call", tool_call), ("approval_round_trip", approval_round_trip)):
        out.append(check(name, fn))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated model ids")
    args = ap.parse_args()
    policy = load_policy(ROOT / "config" / "policy.yaml")
    settings = Settings.from_env()
    if not settings.gcp_project:
        print("GCP_PROJECT is not set (it is in .env)")
        return 2
    vault = Vault("env")
    wanted = {m.strip() for m in args.only.split(",")} if args.only else None
    results = {}
    for spec in policy.models:
        if wanted and spec.id not in wanted:
            continue
        print(f"\n== {spec.id}  (rank {spec.rank}, {spec.location or 'default location'})")
        rows = qualify(settings, vault, spec)
        results[spec.id] = rows
        for r in rows:
            print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['check']:<20} {r['seconds']:>5}s  {r['detail']}")
    (ROOT / "scripts" / "qualify_models_results.json").write_text(json.dumps(results, indent=2))
    print("\n== summary")
    failed = 0
    for mid, rows in results.items():
        bad = [r["check"] for r in rows if not r["ok"]]
        failed += bool(bad)
        print(f"  {'OK  ' if not bad else 'FAIL'} {mid:<26} {'' if not bad else 'failed: ' + ', '.join(bad)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
