"""Event log: turn what the agent stream emits into SMALL, size-capped events for the UI. Raw stream data is never stored.

Two kinds of stream data arrive (``stream_mode=["updates", "messages"]``, ``subgraphs=True``):
  * ``messages``: tiny text chunks while the model writes. They are batched into ``token`` events (flush at ~300 ms or ~800 chars).
  * ``updates``: whole steps, which can be huge (a 40,000-token tool result, a long answer). Each is reduced to a clipped event:
    ``assistant_message``, ``tool_call``, ``tool_result``, ``todos``. Everything else is dropped.
The full content stays in the LangGraph checkpoint (and in sandbox files); events are for display only.

Control outputs start with an underscore and are not events: ``_compact`` (delete the token batches of a finished message) and
``_interrupt`` (the stream paused on an approval; the worker turns it into an approval record and an ``interrupt`` event).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

MAX_EVENT_BYTES = 32_768
ARG_PREVIEW_CHARS = 2_000
RESULT_PREVIEW_CHARS = 2_000
ASSISTANT_MESSAGE_MAX_CHARS = 64_000
TODOS_MAX = 50
TODO_TEXT_MAX = 300
TOKEN_FLUSH_CHARS = 800
TOKEN_FLUSH_SECONDS = 0.3

EVENT_TYPES = ("user_message", "turn_started", "token", "assistant_message", "tool_call", "tool_result", "todos",
               "interrupt", "approval_decided", "artifact", "usage", "notice", "heartbeat", "reconnect", "turn_done", "turn_error")


@dataclass(frozen=True)
class Emit:
    type: str
    payload: dict


def clip(text: Any, limit: int) -> tuple[str, bool]:
    """(text clipped to ``limit`` characters, whether anything was cut)."""
    s = "" if text is None else str(text)
    return (s, False) if len(s) <= limit else (s[:limit], True)


def _clip_value(v: Any, depth: int = 0) -> Any:
    if isinstance(v, str):
        s, cut = clip(v, ARG_PREVIEW_CHARS)
        return s + f" [... {len(v) - ARG_PREVIEW_CHARS} more characters]" if cut else s
    if depth > 4:
        return str(v)[:200]
    if isinstance(v, (list, tuple)):
        return [_clip_value(x, depth + 1) for x in v[:20]] + ([f"[... {len(v) - 20} more items]"] if len(v) > 20 else [])
    if isinstance(v, dict):
        return {str(k): _clip_value(x, depth + 1) for k, x in list(v.items())[:30]}
    return v


def _size(payload: Any) -> int:
    return len(json.dumps(payload, default=str, ensure_ascii=False).encode())


def _longest_string(node: Any, path: tuple = ()) -> tuple[int, tuple]:
    best = (0, ())
    if isinstance(node, str):
        return len(node), path
    if isinstance(node, dict):
        for k, v in node.items():
            cand = _longest_string(v, (*path, k))
            best = max(best, cand, key=lambda t: t[0])
    elif isinstance(node, list):
        for i, v in enumerate(node):
            cand = _longest_string(v, (*path, i))
            best = max(best, cand, key=lambda t: t[0])
    return best


def cap_payload(payload: dict, max_bytes: int = MAX_EVENT_BYTES) -> dict:
    """Defensive last line: no matter what the mapper produced, one event never exceeds ``max_bytes``. Halves the longest string until it fits."""
    if _size(payload) <= max_bytes:
        return payload
    out = json.loads(json.dumps(payload, default=str))  # deep copy that is JSON-safe
    for _ in range(80):
        if _size(out) <= max_bytes:
            out["_capped"] = True
            return out
        length, path = _longest_string(out)
        if length <= 40 or not path:
            break
        node = out
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = node[path[-1]][: max(length // 2, 20)] + " [clipped]"
    return {"_capped": True, "note": "payload too large to store"}


def args_signature(args: Any) -> str:
    """Short stable fingerprint of a tool call's full arguments (stored on the event so a later segment can recognise the same call)."""
    return hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]


def text_of(content: Any) -> str:
    """Plain text of a message content (a string, or a list of blocks such as Gemini's)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type", "text") == "text":
                parts.append(str(b.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


HELPER_TOOLS = ("task", "delegate")  # tools that start a helper: the stream names the helper after the call that started it


class EventMapper:
    """Stateful per turn. ``feed()`` takes one item of the agent stream and returns zero or more ``Emit``s."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buf: dict[Any, list[str]] = {}           # message id -> pending text
        self._buf_meta: dict[Any, str] = {}            # message id -> agent label
        self._tokens_sent: set[Any] = set()            # message ids that have token events in the log
        self._last_flush = clock()
        self._pending_tasks: list[tuple[str, str]] = []  # (task tool_call id, subagent type) waiting for their namespace
        self._ns_label: dict[tuple, str] = {}
        self._seen_calls: dict[str, str] = {}          # tool_call_id -> json of args already emitted
        self._seen_results: set[str] = set()
        self._seen_messages: set[Any] = set()
        self._seen_interrupts: set[Any] = set()

    def prime(self, events: list[dict]) -> "EventMapper":
        """Learn what an earlier segment of this turn already announced (a resume re-sends the paused call; a new process starts empty)."""
        for e in events:
            p, kind = e.get("payload") or {}, e.get("type")
            if kind == "tool_call" and p.get("tool_call_id"):
                self._seen_calls[p["tool_call_id"]] = p.get("args_sig", "")
            elif kind == "tool_result" and p.get("tool_call_id") is not None:
                self._seen_results.add(p["tool_call_id"])
            elif kind == "assistant_message" and p.get("message_id") is not None:
                self._seen_messages.add(p["message_id"])
            if p.get("ns") and p.get("agent") not in (None, "assistant", "helper"):
                self._ns_label[tuple(p["ns"].split("|"))] = p["agent"]  # the helper keeps its name across approvals and restarts
        return self

    def prime_messages(self, messages: list) -> "EventMapper":
        """Learn the conversation as it stood BEFORE this segment. The agent's start-up steps (for example repairing a call that was cut off
        by a cancel) report the whole message list again; without this, every earlier step would be announced again as if it were new."""
        for m in messages or []:
            kind = getattr(m, "type", "")
            if kind == "ai":
                if getattr(m, "id", None) is not None:
                    self._seen_messages.add(m.id)
                for tc in getattr(m, "tool_calls", None) or []:
                    self._seen_calls[tc.get("id") or ""] = args_signature(tc.get("args") or {})
            elif kind == "tool":
                self._seen_results.add(getattr(m, "tool_call_id", "") or "")
        return self

    # ── labelling ────────────────────────────────────────────────────────
    def agent_label(self, ns: tuple) -> str:
        if not ns:
            return "assistant"
        if ns not in self._ns_label:  # best effort: namespaces carry no name, so pair them with `task` calls in order
            self._ns_label[ns] = self._pending_tasks.pop(0)[1] if self._pending_tasks else "helper"
        return self._ns_label[ns]

    # ── entry point ──────────────────────────────────────────────────────
    def feed(self, ns: tuple, mode: str, data: Any) -> list[Emit]:
        try:
            if mode == "messages":
                return self._on_chunk(ns, data)
            if mode == "updates":
                return self._on_update(ns, data)
        except Exception:  # noqa: BLE001  a malformed chunk must never kill a turn; the checkpoint still has the truth
            return []
        return []

    # ── live text ────────────────────────────────────────────────────────
    def _on_chunk(self, ns: tuple, data: Any) -> list[Emit]:
        msg, meta = data if isinstance(data, tuple) and len(data) == 2 else (data, {})
        if getattr(msg, "type", "") != "AIMessageChunk" or (meta or {}).get("langgraph_node") not in (None, "model"):
            return []
        text = text_of(getattr(msg, "content", ""))
        if not text:
            return []
        mid = getattr(msg, "id", None)
        self._buf.setdefault(mid, []).append(text)
        self._buf_meta[mid] = self.agent_label(ns)
        pending = sum(len(t) for t in self._buf[mid])
        if pending >= TOKEN_FLUSH_CHARS or self._clock() - self._last_flush >= TOKEN_FLUSH_SECONDS:
            return self._flush_one(mid)
        return []

    def _flush_one(self, mid: Any) -> list[Emit]:
        parts = self._buf.pop(mid, [])
        agent = self._buf_meta.get(mid, "assistant")
        self._last_flush = self._clock()
        text = "".join(parts)
        if not text:
            return []
        self._tokens_sent.add(mid)
        return [Emit("token", {"message_id": mid, "text": text, "agent": agent})]

    def flush(self) -> list[Emit]:
        """Emit whatever text is still buffered (call at the end of a stream segment)."""
        out: list[Emit] = []
        for mid in list(self._buf):
            out += self._flush_one(mid)
        return out

    # ── finished steps ───────────────────────────────────────────────────
    def _on_update(self, ns: tuple, data: Any) -> list[Emit]:
        nsid = "|".join(ns)
        # `ns` on each event lets a LATER segment (after an approval or a restart) recover which helper this namespace belonged to
        return [Emit(e.type, {**e.payload, "ns": nsid}) if nsid and not e.type.startswith("_") and "agent" in e.payload else e for e in self._on_update_raw(ns, data)]

    def _on_update_raw(self, ns: tuple, data: Any) -> list[Emit]:
        out: list[Emit] = []
        if not isinstance(data, dict):
            return out
        for node, upd in data.items():
            if node == "__interrupt__":
                # A helper's pause is reported twice: by the helper and again, with the same id, bubbled up through its parent.
                fresh = [i for i in (upd if isinstance(upd, (list, tuple)) else [upd]) if self._interrupt_key(i) not in self._seen_interrupts]
                for i in fresh:
                    self._seen_interrupts.add(self._interrupt_key(i))
                if fresh:
                    out += self.flush()
                    out.append(Emit("_interrupt", {"raw": tuple(fresh), "agent": self.agent_label(ns)}))
                continue
            if not isinstance(upd, dict):
                continue
            agent = self.agent_label(ns)
            msgs = upd.get("messages")
            if msgs is not None:
                out += self._messages(agent, msgs if isinstance(msgs, list) else [msgs])
            todos = upd.get("todos")
            if isinstance(todos, list):
                items = [{"content": clip(t.get("content", ""), TODO_TEXT_MAX)[0], "status": t.get("status", "pending")}
                         for t in todos[:TODOS_MAX] if isinstance(t, dict)]
                out.append(Emit("todos", {"todos": items, "agent": agent}))
        return out

    @staticmethod
    def _interrupt_key(i: Any) -> Any:
        return getattr(i, "id", None) or repr(getattr(i, "value", i))

    def _messages(self, agent: str, msgs: list) -> list[Emit]:
        out: list[Emit] = []
        for m in msgs:
            kind = getattr(m, "type", "")
            if kind == "ai":
                out += self._ai(agent, m)
            elif kind == "tool":
                tcid = getattr(m, "tool_call_id", "") or ""
                if tcid in self._seen_results:
                    continue
                self._seen_results.add(tcid)
                self._pending_tasks = [t for t in self._pending_tasks if t[0] != tcid]
                body = text_of(getattr(m, "content", ""))
                preview, cut = clip(body, RESULT_PREVIEW_CHARS)
                out.append(Emit("tool_result", {"tool_call_id": tcid, "name": getattr(m, "name", None), "status": getattr(m, "status", "success"),
                                                "chars": len(body), "preview": preview, "truncated": cut, "agent": agent}))
        return out

    def _ai(self, agent: str, m: Any) -> list[Emit]:
        out: list[Emit] = []
        mid = getattr(m, "id", None)
        text = text_of(getattr(m, "content", "")).strip()
        if mid not in self._seen_messages or mid is None:
            usage = getattr(m, "usage_metadata", None)
            if usage:  # what this model call cost: lets a client show tokens and explain slow steps
                out.append(Emit("usage", {"message_id": mid, "agent": agent, "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                                          "total_tokens": usage.get("total_tokens"),
                                          "reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning"),
                                          "cached_tokens": (usage.get("input_token_details") or {}).get("cache_read"),
                                          "model": (getattr(m, "response_metadata", None) or {}).get("model_name")}))  # which model made this call (helpers can differ)
            self._buf.pop(mid, None)  # the final message supersedes any batch not yet written
            if text:
                body, cut = clip(text, ASSISTANT_MESSAGE_MAX_CHARS)
                out.append(Emit("assistant_message", {"message_id": mid, "text": body, "truncated": cut, "chars": len(text), "agent": agent}))
                if mid in self._tokens_sent:
                    out.append(Emit("_compact", {"message_id": mid}))
            if mid is not None:
                self._seen_messages.add(mid)
        for tc in getattr(m, "tool_calls", None) or []:
            tcid, args = tc.get("id") or "", tc.get("args") or {}
            sig = args_signature(args)
            if self._seen_calls.get(tcid) == sig:
                continue  # the same call re-appears in later updates (e.g. after an approval); show it once
            edited = tcid in self._seen_calls
            self._seen_calls[tcid] = sig
            out.append(Emit("tool_call", {"tool_call_id": tcid, "name": tc.get("name"), "args": _clip_value(args), "edited": edited, "agent": agent,
                                          "args_sig": args_signature(args)}))
            if tc.get("name") in HELPER_TOOLS and not edited:  # the library's `task` or our `delegate` (same job, plus a model argument)
                self._pending_tasks.append((tcid, str((args or {}).get("subagent_type", "helper"))))
        return out
