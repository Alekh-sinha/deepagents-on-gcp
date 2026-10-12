#!/usr/bin/env python3
"""A complete local privai for developing and testing the UI: the REAL engine and web backend, but a scripted demo model, a fake sandbox that runs
commands in a temporary folder, in-memory state, and a small MCP server you can connect to. Costs nothing and needs no cloud.

  uv run python scripts/dev_stack.py                  # web on http://127.0.0.1:8081 (signed in as dev@example.com); then:  cd frontend && npm run dev
  uv run python scripts/dev_stack.py --auth google-fake   # the sign-in page, with a fake "Sign in with Google" (credential: fake-google-credential-ok)

Things to type to see each part of the UI work (the demo model reads keywords in your message):
  hello / anything      a streamed markdown answer          todo          a progress list
  shell                 a command that asks for approval    plan          a plan to approve or edit
  ask                   a question to answer                parallel      two helpers, two approvals at once
  research              a helper on a cheaper model         file          a saved file (artifact)
  mcp                   uses a connected tool (connect the MCP server printed at start-up first)
  slow                  a long answer you can stop          fail          an error
For the real thing (real models, your cloud engine) use `uv run pinboard web` with RUNTIME_RESOURCE set instead.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "mcp"))
sys.path.insert(0, str(ROOT / "tests" / "core"))   # turn_harness imports test_policy

import uvicorn  # noqa: E402
import yaml  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402

DEV_USER = "dev@example.com"

DEMO_ANSWER = (
    "Here is a short overview of how I work, so you can see streaming in action.\n\n"
    "**What I do**\n\n- I break a request into steps and keep a progress list.\n- I use tools, and ask you before anything that matters.\n"
    "- I can hand parts of the work to helpers, each on a model that fits the job.\n\n"
    "```python\ndef greet(name):\n    return f\"Hello, {name}!\"\n```\n\n"
    "Try the other keywords listed at the top of `scripts/dev_stack.py` to see approvals, plans, questions and files."
)
SLOW_ANSWER = " ".join(f"Sentence number {i} of a deliberately long answer, so that you can press stop." for i in range(1, 60))


def _call(name: str, args: dict, cid: str) -> dict:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


class DemoModel(BaseChatModel):
    """Reads the keywords in the latest user message and plays a scripted scene. Stateless: where it is in the scene is worked out from the conversation."""

    name: str = "demo"
    delay: float = 0.02

    @property
    def _llm_type(self) -> str:
        return "demo"

    def bind_tools(self, tools, **kw):
        return self

    # ── deciding what to say ──
    def _plan(self, messages) -> tuple[str, list[dict]]:
        """Returns (text, tool_calls) for this step."""
        system = next((str(m.content) for m in messages if getattr(m, "type", "") == "system"), "")
        helper = "You are Pinboard" not in system
        humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
        last_human = messages[humans[-1]] if humans else HumanMessage("")
        text = str(last_human.content).lower()
        after = messages[humans[-1] + 1:] if humans else []
        rounds = sum(1 for m in after if isinstance(m, AIMessage))      # how many steps of this scene came before
        results = [m for m in after if isinstance(m, ToolMessage)]

        if helper:
            if rounds == 0 and "command" in text:
                return "", [_call("execute", {"command": "echo helper was here"}, f"h-{abs(hash(text)) % 9999}")]
            return f"(from the helper, on {self.name}) Here is what I found: the answer is 42, with two sources.", []
        if "fail" in text and rounds == 0:
            raise RuntimeError("the demo model was asked to fail")
        if "slow" in text:
            return SLOW_ANSWER, []
        if "todo" in text:
            if rounds == 0:
                return "", [_call("write_todos", {"todos": [{"content": "Gather the facts", "status": "completed"}, {"content": "Draft the summary", "status": "in_progress"}, {"content": "Review and send", "status": "pending"}]}, "td1")]
            return "I've set up a progress list above and will tick items off as I go.", []
        if "shell" in text:
            if rounds == 0:
                return "I'll check the folder first.", [_call("execute", {"command": "echo hello from the sandbox && ls"}, "sh1")]
            return "The command ran. Output: " + (str(results[-1].content)[:120] if results else ""), []
        if "plan" in text:
            if rounds == 0:
                return "", [_call("submit_plan", {"summary": "Write a short report and save it", "tasks": [
                    {"title": "Collect the key points", "detail": "From what you told me", "assignee": "assistant"},
                    {"title": "Write the report", "detail": "One page, markdown", "assignee": "coder"}]}, "pl1")]
            return "Plan approved. Working through it now.", []
        if "ask" in text:
            if rounds == 0:
                return "", [_call("ask_user", {"question": "Which format do you want the report in?", "options": ["Markdown", "PDF"]}, "q1")]
            return "Thanks, I'll use that: " + (str(results[-1].content)[:80] if results else ""), []
        if "parallel" in text:
            if rounds == 0:
                return "I'll start two helpers at the same time.", [
                    _call("delegate", {"description": "Run a command: part one", "subagent_type": "coder", "model": "gemini-2.5-flash-lite"}, "p1"),
                    _call("delegate", {"description": "Run a command: part two", "subagent_type": "coder", "model": "gemini-2.5-flash"}, "p2")]
            return "Both helpers are done.", []
        if "research" in text:
            if rounds == 0:
                return "I'll ask the researcher, on a cheaper model, since this is a simple lookup.", [
                    _call("delegate", {"description": "Find the headline figure and two sources.", "subagent_type": "researcher", "model": "gemini-2.5-flash-lite"}, "r1")]
            return "The researcher found it: " + (str(results[-1].content)[:150] if results else ""), []
        if "file" in text:
            if rounds == 0:
                cwd = re.search(r"working directory is (\S+?)\.\s", system)   # the sandbox only accepts paths under its own folder, which the system prompt names
                folder = cwd.group(1) if cwd else "/workspace"
                return "", [_call("write_file", {"file_path": f"{folder}/out/report.md", "content": "# Report\n\nThis file was written by the demo model.\n"}, "f1")]
            return "I saved the report. You can download it below.", []
        if "mcp" in text:
            if rounds == 0:
                return "I'll use your connected calculator.", [_call("mcp__calc__add", {"a": 40, "b": 2}, "m1")]
            body = re.search(r"<untrusted_content[^>]*>\n(.*?)\n</untrusted_content>", str(results[-1].content), re.S) if results else None   # results of connected tools arrive wrapped
            return "The connected tool answered: " + (body.group(1) if body else str(results[-1].content)[:80] if results else ""), []
        return DEMO_ANSWER, []

    # ── producing it ──
    def _message(self, text: str, calls: list[dict]) -> AIMessage:
        return AIMessage(content=text, tool_calls=calls, response_metadata={"model_name": self.name},
                         usage_metadata={"input_tokens": 1200, "output_tokens": max(len(text) // 4, 20), "total_tokens": 1300})

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        text, calls = self._plan(messages)
        return ChatResult(generations=[ChatGeneration(message=self._message(text, calls))])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        text, calls = self._plan(messages)
        words = text.split(" ")
        for i in range(0, len(words), 3):
            time.sleep(self.delay)
            piece = " ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "")
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="", response_metadata={"model_name": self.name}, usage_metadata={"input_tokens": 1200, "output_tokens": max(len(text) // 4, 20), "total_tokens": 1300},
            tool_call_chunks=[{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i} for i, c in enumerate(calls)]))


def make_policy(user: str):
    data = yaml.safe_load((ROOT / "config" / "policy.yaml").read_text())
    data["allowed_users"] = [user]
    data["email"]["allowed_recipients"] = [user]
    data["mcp"] = {**data.get("mcp", {}), "enabled": True, "allow_private_hosts": True}
    data["schedules"] = {**data.get("schedules", {}), "enabled": True}
    data["warm_sandbox"] = {"mode": "off"}
    from pinboard.core.policy import parse_policy

    return parse_policy(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8081, help="the web backend (what Vite proxies to)")
    ap.add_argument("--engine-port", type=int, default=8080)
    ap.add_argument("--auth", choices=["dev", "google-fake"], default="dev")
    ap.add_argument("--user", default=DEV_USER)
    ap.add_argument("--no-mcp", action="store_true", help="do not start the demo MCP server")
    args = ap.parse_args()

    from helpers import make_settings
    from turn_harness import Farm

    from pinboard.agent import build_agent
    from pinboard.agent.models import ModelRouter
    from pinboard.engine.api import create_app as create_engine
    from pinboard.engine.service import RunService
    from pinboard.engine.worker import ThreadExecutor
    from pinboard.gcp.vault import Vault
    from pinboard.mcp import generate_key
    from pinboard.state import make_state
    from pinboard.web.app import create_app as create_web
    from pinboard.web.auth import DevAuthenticator, GoogleAuthenticator
    from pinboard.web.runtime_client import HttpRuntimeClient

    work = Path(tempfile.mkdtemp(prefix="privai-dev-"))
    os.environ.setdefault("MCP_ENCRYPTION_KEY", generate_key())
    settings = make_settings(work / "ws", sandbox_name=None, sandbox_runtime="rt-dev", approval_mode="execute", sandbox_ttl_seconds=3600,
                             approval_ttl_seconds=1200, output_dir=str(work / "outputs"), artifacts_uri=str(work / "artifacts"), state_backend="memory")
    (work / "ws").mkdir()
    (work / "sb").mkdir()
    farm = Farm(work / "sb")
    policy, vault = make_policy(args.user), Vault("env")
    state = make_state(settings, vault)
    router_holder = ModelRouter(policy, lambda spec: DemoModel(name=spec.id))

    def build(settings, vault, *, checkpointer, adopt_sandbox, on_block, storage_client, artifacts=None, user_id=None, session_id=None, lazy_sandbox=False,
              on_sandbox_created=None, claim_sandbox=None, on_sandbox_claimed=None, mcp=None, **_):
        folder = work / "sb" / (adopt_sandbox or farm.next_name())
        folder.mkdir(parents=True, exist_ok=True)
        return build_agent(dataclasses.replace(settings, sandbox_cwd=str(folder)), vault, tools=[], checkpointer=checkpointer, on_block=on_block, adopt_sandbox=adopt_sandbox,
                           client=farm, lazy_sandbox=lazy_sandbox, on_sandbox_created=on_sandbox_created, claim_sandbox=claim_sandbox, on_sandbox_claimed=on_sandbox_claimed,
                           artifacts=artifacts, user_id=user_id, session_id=session_id, storage_client=storage_client, mcp=mcp, policy=policy, router=router_holder)

    svc = RunService(settings, vault, state.store, state.checkpointer, build=build, sandbox_deleter=farm.delete, sandbox_probe=farm.probe, policy=policy,
                     executor=ThreadExecutor(4), ledger=state.ledger)
    token = "dev-" + "x" * 40
    engine = create_engine(svc, token)
    threading.Thread(target=uvicorn.Server(uvicorn.Config(engine, host="127.0.0.1", port=args.engine_port, log_level="warning")).run, daemon=True).start()

    import httpx

    runtime = HttpRuntimeClient(httpx.Client(base_url=f"http://127.0.0.1:{args.engine_port}", timeout=httpx.Timeout(120.0, read=330.0)), token)
    if args.auth == "dev":
        auth = DevAuthenticator(args.user)
    else:
        def verify(credential, client_id):
            if credential == "fake-google-credential-ok":
                return {"iss": "https://accounts.google.com", "email": args.user, "email_verified": True}
            if credential == "fake-google-credential-stranger":   # a real Google account that is not on the allow-list
                return {"iss": "https://accounts.google.com", "email": "stranger@example.com", "email_verified": True}
            raise ValueError("not a demo credential")
        auth = GoogleAuthenticator("demo-client.apps.googleusercontent.com", "s" * 40, verify=verify)

    def tick():   # what Cloud Scheduler does in the cloud: housekeeping, which also fires due schedules
        while True:
            time.sleep(15)
            try:
                svc.maintenance()
            except Exception as exc:  # noqa: BLE001
                print("housekeeping failed:", exc)
    threading.Thread(target=tick, daemon=True).start()

    if not args.no_mcp:
        from mcp_fixtures import running_server
        cm = running_server()
        url, _seen = cm.__enter__()
        print(f"demo MCP server: {url}\n  paste this in Connectors:  " + json.dumps({"mcpServers": {"calc": {"url": url}}}))
        (ROOT / "frontend" / ".dev-stack.json").write_text(json.dumps({"mcp_url": url}))   # the browser tests read it

    print(f"\nprivai dev stack: web http://127.0.0.1:{args.port}  (engine :{args.engine_port}, user {args.user}, auth {args.auth})\nFrontend:  cd frontend && npm run dev   -> http://localhost:5173\n")
    uvicorn.run(create_web(runtime, auth), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
