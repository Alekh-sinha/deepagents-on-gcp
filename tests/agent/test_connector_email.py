import base64
from datetime import datetime, timedelta, timezone

import pytest

from pinboard.agent.tools.email_tools import make_email_tools
from pinboard.agent.tools.effects import InMemoryLedger
from pinboard.core.policy import parse_policy

from helpers import FakeMail
from test_policy import BASE
import copy


def pol(**email):
    d = copy.deepcopy(BASE)
    d["email"] = {**d["email"], "max_per_day": 5, **email}
    return parse_policy(d)


class Env:
    def __init__(self, files=None, **email):
        self.mail, self.ledger, self.events = FakeMail(), InMemoryLedger(), []
        self.files = files or {}
        self.now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.ledger = InMemoryLedger(clock=lambda: self.now)
        self.tools, self.checks = make_email_tools(
            self.mail, pol(**email), "bot@agentmail.to", read_file=lambda p: self.files[p], ledger=self.ledger,
            audit=lambda e, d: self.events.append((e, d)), clock=lambda: self.now)
        self.by = {t.name: t for t in self.tools}

    def call(self, name, args, cid="c1", thread="T"):
        msg = self.by[name].invoke({"name": name, "args": args, "id": cid, "type": "tool_call"}, config={"configurable": {"thread_id": thread}})
        return msg.content


OK = {"to": ["me@example.com"], "subject": "Report", "body": "Here it is."}


def test_send_goes_to_the_gateway_once_with_normalised_fields():
    e = Env()
    out = e.call("send_email", {**OK, "to": ["ME@Example.com"], "subject": "  Report  "})
    assert "Email sent to me@example.com" in out
    s = e.mail.sent[0]
    assert (s["inbox_id"], s["to"], s["cc"], s["subject"], s["text"]) == ("bot@agentmail.to", ["me@example.com"], None, "Report", "Here it is.")
    assert s["key"] and len(s["key"]) == 40
    assert e.events[-1][0] == "connector.email.sent" and e.events[-1][1]["to"] == ["me@example.com"]
    assert "body" not in e.events[-1][1]  # audit never stores the body


def test_model_cannot_see_or_set_injected_arguments_and_there_is_no_bcc():
    schema = Env().by["send_email"].args
    assert set(schema) == {"to", "subject", "body", "cc", "attachments"}


@pytest.mark.parametrize("args", [
    {**OK, "to": ["evil@x.com"]},
    {**OK, "to": ["me@example.com", "evil@x.com"]},
    {**OK, "cc": ["evil@x.com"]},
    {**OK, "to": ["evil@x.com <me@example.com>"]},
    {**OK, "to": ["me@example.com;evil@x.com"]},
    {**OK, "to": []},
    {**OK, "subject": "  "},
    {**OK, "body": ""},
])
def test_refused_sends_never_reach_the_gateway(args):
    e = Env()
    out = e.call("send_email", args)
    assert "Blocked by policy" in out and e.mail.sent == []
    assert e.events and e.events[0][0] == "policy.denied"


def test_duplicate_tool_call_is_skipped_but_a_new_call_sends():
    e = Env()
    e.call("send_email", OK, cid="c1")
    assert "already sent" in e.call("send_email", OK, cid="c1")  # a resumed turn replays the same call id
    e.call("send_email", OK, cid="c2")
    assert len(e.mail.sent) == 2
    e.call("send_email", OK, cid="c1", thread="OTHER")  # same call id in another conversation is a different effect
    assert len(e.mail.sent) == 3


def test_daily_quota_resets_the_next_day():
    e = Env(max_per_day=2)
    for i in range(2):
        e.call("send_email", OK, cid=f"c{i}")
    assert "daily" in e.call("send_email", OK, cid="c9") and len(e.mail.sent) == 2
    e.now += timedelta(days=1)
    e.call("send_email", OK, cid="c10")
    assert len(e.mail.sent) == 3


def test_attachments_come_only_from_the_output_folder_and_respect_the_size_limit():
    e = Env(files={"/workspace/out/report.md": b"# hi", "/workspace/out/big.bin": b"x" * (1024 * 1024 + 1), "/etc/passwd": b"root"}, max_attachment_mb=1)
    ok = e.call("send_email", {**OK, "attachments": ["/workspace/out/report.md"]})
    att = e.mail.sent[0]["attachments"][0]
    assert "sent" in ok and att["filename"] == "report.md" and base64.b64decode(att["content_b64"]) == b"# hi" and att["content_type"].startswith("text/")
    for bad in ("/etc/passwd", "/workspace/out/../../etc/passwd", "report.md", "/workspace/outside/x"):
        assert "Blocked by policy" in e.call("send_email", {**OK, "attachments": [bad]}, cid=bad)
    assert "larger than" in e.call("send_email", {**OK, "attachments": ["/workspace/out/big.bin"]}, cid="big")
    assert "NOT sent" in e.call("send_email", {**OK, "attachments": ["/workspace/out/missing.md"]}, cid="miss")
    assert len(e.mail.sent) == 1


def test_a_definite_rejection_can_be_retried_but_an_ambiguous_failure_cannot():
    class Rejected(Exception):
        status_code = 422

    e = Env()
    e.mail.fail = Rejected("bad request")
    assert "NOT sent" in e.call("send_email", OK, cid="c1")
    e.mail.fail = None
    assert "Email sent" in e.call("send_email", OK, cid="c1")  # claim was released: retry allowed

    e.mail.fail = TimeoutError("read timed out")
    assert "UNKNOWN" in e.call("send_email", OK, cid="c2")
    e.mail.fail = None
    assert "already sent" in e.call("send_email", OK, cid="c2")  # might have gone out: never auto-resend
    assert len(e.mail.sent) == 1


def test_inbound_mail_is_labelled_untrusted_and_cannot_break_out():
    e = Env()
    lst = e.call("list_emails", {})
    assert "untrusted_content" in lst and "boss@example.com" in lst
    msg = e.call("read_email", {"message_id": "m1"})
    assert msg.count("</untrusted_content>") == 1  # the attacker's closing tag was neutralised
    assert 'source="email:Boss <x> <boss@example.com>"' not in msg and 'source="email:' in msg and '"<' not in msg.split("\n")[0]
    assert "Attachments: a.pdf" in msg
    assert "could not read" in e.call("read_email", {"message_id": "nope"}).lower()


def test_static_check_is_exposed_for_the_approval_gate():
    from pinboard.core.policy import PolicyDenied

    e = Env()
    e.checks["send_email"](**OK)
    with pytest.raises(PolicyDenied):
        e.checks["send_email"](**{**OK, "to": ["evil@x.com"]})
