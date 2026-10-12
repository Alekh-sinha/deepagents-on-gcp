"""AgentMail API client: a thin wrapper over the SDK that returns plain dicts. No LangChain, no policy: just the service."""
from __future__ import annotations

from typing import Any


class AgentMailGateway:
    """Thin wrapper over the AgentMail SDK that returns plain dicts (so tools and fakes stay simple)."""

    def __init__(self, api_key: str, client=None):
        if client is None:
            from agentmail import AgentMail

            client = AgentMail(api_key=api_key)
        self._c = client

    def send(self, inbox_id: str, *, to: list[str], subject: str, text: str, cc: list[str] | None = None,
             attachments: list[dict] | None = None, idempotency_key: str | None = None) -> dict:
        kwargs: dict[str, Any] = {"to": to, "subject": subject, "text": text, "idempotency_key": idempotency_key}
        if cc:
            kwargs["cc"] = cc
        if attachments:
            from agentmail import SendAttachment

            kwargs["attachments"] = [SendAttachment(filename=a["filename"], content=a["content_b64"], content_type=a["content_type"])
                                     for a in attachments]
        r = self._c.inboxes.messages.send(inbox_id, **kwargs)
        return {"message_id": getattr(r, "message_id", None), "thread_id": getattr(r, "thread_id", None)}

    def list(self, inbox_id: str, limit: int = 10) -> list[dict]:
        r = self._c.inboxes.messages.list(inbox_id, limit=limit)
        return [{"message_id": m.message_id, "from": m.from_, "subject": m.subject or "", "timestamp": str(m.timestamp),
                 "preview": (m.preview or "")[:200]} for m in (r.messages or [])]

    def get(self, inbox_id: str, message_id: str) -> dict:
        m = self._c.inboxes.messages.get(inbox_id, message_id)
        return {"message_id": m.message_id, "from": m.from_, "to": list(m.to or []), "subject": m.subject or "",
                "timestamp": str(m.timestamp), "text": m.extracted_text or m.text or "",
                "attachments": [getattr(a, "filename", None) or "attachment" for a in (m.attachments or [])]}
