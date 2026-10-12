"""The agent: everything LangGraph / deepagents. Read in this order:

    prompts.py     what the model is told
    subagents.py   the team (researcher, coder, mailer) and the tools each holds
    tools/         the tools themselves, one file per family
    gates.py       which calls wait for a person (approval gates)
    models.py      model allowlist + per-request model switching
    builder.py     wires model + sandbox + skills + tools + team + gates into one agent
    runner.py      run / resume an agent and read its pending approvals
    approvals.py   turn a pending call into a readable card (kind, title, preview)
"""
from pinboard.agent.builder import AgentRuntime, build_agent, build_model
from pinboard.agent.prompts import SYSTEM_PROMPT
from pinboard.agent.runner import RECURSION_LIMIT, Approver, RunResult, drive, existing_messages, final_text, resume, run, stream_segment
from pinboard.agent.subagents import build_subagents, model_aware

__all__ = ["AgentRuntime", "Approver", "RECURSION_LIMIT", "RunResult", "SYSTEM_PROMPT", "build_agent", "build_model", "build_subagents",
           "drive", "existing_messages", "final_text", "model_aware", "resume", "run", "stream_segment"]
