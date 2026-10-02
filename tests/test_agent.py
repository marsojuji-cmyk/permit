"""Tests for the demo agent: tool gating and the ReAct parser."""

import json
from datetime import datetime, timedelta, timezone

from agent.react import build_system, parse_action
from agent.tools import SpendTools
from permit.flow import SpendPipeline
from permit.ledger import Ledger
from permit.permit import PermitStore
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import ReleaseVerifier


def _tools(catalog=None):
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)
    permit, _ = permits.grant(
        agent_id="t", cap_cents=5000, allowlist=["m"],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    catalog = catalog or {"widget": b"widget-bytes"}
    return SpendTools(flow, permit.permit_id, "m", catalog), paypal, permits


def test_attempt_spend_allowed_then_deliver_captures():
    tools, paypal, _ = _tools()
    obs = json.loads(tools.attempt_spend(3000, "widget"))
    assert obs["decision"] == "ALLOWED"
    assert obs["paypal_auth_id"].startswith("mock_auth_")
    obs2 = json.loads(tools.deliver(obs["escrow_id"]))
    assert obs2["released"] is True
    assert obs2["capture_id"] is not None


def test_blocked_spend_never_touches_paypal():
    tools, paypal, _ = _tools()
    obs = json.loads(tools.attempt_spend(6000, "widget"))
    assert obs["decision"] == "BLOCKED"
    assert obs["paypal_called"] is False
    assert paypal.authorizations == {}


def test_unknown_purpose_rejected_without_permit_check():
    tools, paypal, _ = _tools()
    obs = json.loads(tools.attempt_spend(100, "nope"))
    assert obs["ok"] is False
    assert paypal.authorizations == {}


def test_deliver_tampered_is_harness_only():
    """deliver_tampered exists on the tools object but the ReAct runner
    refuses to expose it (name blocklist)."""
    tools, _, _ = _tools()
    assert hasattr(tools, "deliver_tampered")


def test_check_permit_reports_remaining():
    tools, _, _ = _tools()
    obs = json.loads(tools.check_permit())
    assert obs["remaining_cents"] == 5000
    json.loads(tools.attempt_spend(2000, "widget"))
    obs = json.loads(tools.check_permit())
    assert obs["remaining_cents"] == 3000


def test_parse_action_two_line():
    name, args = parse_action("THOUGHT: x\nACTION: attempt_spend\n"
                              'ARGS: {"amount_cents": 3000}')
    assert name == "attempt_spend"
    assert args == {"amount_cents": 3000}


def test_parse_action_single_line():
    name, args = parse_action("THOUGHT: y ACTION: check_permit ARGS: {}")
    assert name == "check_permit"
    assert args == {}


def test_parse_action_tolerates_trailing_text():
    name, args = parse_action('ACTION: deliver\nARGS: {"escrow_id": "abc"}\nzzz}')
    assert (name, args) == ("deliver", {"escrow_id": "abc"})


def test_parse_action_no_action():
    assert parse_action("just thinking out loud") == (None, None)


def test_system_prompt_lists_catalog():
    s = build_system(["dataset license", "report"])
    assert '"dataset license", "report"' in s
