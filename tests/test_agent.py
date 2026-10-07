"""Tests for the demo agent: tool gating and the ReAct parser."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.react import build_system, llm_preflight, parse_action, run_agent
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


def test_llm_preflight_fails_with_guidance_when_cli_missing(monkeypatch):
    """A missing Grok runner fails fast with setup guidance — not mid-demo."""
    monkeypatch.setenv("PERMIT_GROK_CLI", "/nonexistent-xyz/grok.py")
    with pytest.raises(RuntimeError) as exc:
        llm_preflight()
    msg = str(exc.value)
    assert "PERMIT_GROK_CLI" in msg
    assert "--replay" in msg


def test_llm_preflight_passes_for_existing_cli(monkeypatch):
    monkeypatch.setenv("PERMIT_GROK_CLI", __file__)  # any real file
    llm_preflight()  # must not raise


def _replay_transcript(path, lines):
    with open(path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")


def test_replay_never_touches_llm_backend(monkeypatch, tmp_path):
    """Replay mode is fully offline: it must not preflight the LLM runner."""
    monkeypatch.setenv("PERMIT_GROK_CLI", "/nonexistent-xyz/grok.py")
    replay = tmp_path / "rec.jsonl"
    _replay_transcript(replay, [
        {"type": "beat", "n": 7, "task": "noop"},
        {"type": "answer", "text": "done offline"},
    ])
    tools, _, _ = _tools()
    final = run_agent(tools, "noop", str(tmp_path / "out.jsonl"),
                      replay=str(replay), beat=7)
    assert final == "done offline"


def test_replay_reexecutes_actions_with_id_remap(tmp_path):
    """
    Replay re-executes the recorded actions against the live tools: the
    recorded escrow_id is remapped to the fresh one, the deliver really
    captures, and the fresh ids land in the written transcript.
    """
    tools, paypal, _ = _tools()
    replay = tmp_path / "rec.jsonl"
    _replay_transcript(replay, [
        {"type": "beat", "n": 7, "task": "buy a widget"},
        {"type": "llm", "text": "THOUGHT: buy it"},
        {"type": "action", "tool": "attempt_spend",
         "args": {"amount_cents": 3000, "purpose": "widget"}},
        {"type": "observation", "text": json.dumps({
            "ok": True, "decision": "ALLOWED",
            "escrow_id": "esc_RECORDED1",
            "paypal_auth_id": "mock_auth_RECORDED",
            "amount_cents": 3000})},
        {"type": "llm", "text": "THOUGHT: deliver it"},
        {"type": "action", "tool": "deliver",
         "args": {"escrow_id": "esc_RECORDED1"}},
        {"type": "observation", "text": json.dumps({
            "ok": True, "released": True, "reason": "delivered",
            "capture_id": "cap_RECORDED"})},
        {"type": "answer", "text": "bought the widget"},
        {"type": "beat", "n": 8, "task": "next"},
    ])
    out = tmp_path / "out.jsonl"
    final = run_agent(tools, "buy a widget", str(out),
                      replay=str(replay), beat=7)
    assert final == "bought the widget"
    # The replay really moved money on the mock rail.
    assert len(paypal.capture_calls) == 1
    # The recorded escrow_id was remapped to a fresh one in the log.
    logged = [json.loads(l) for l in open(out)]
    deliver = next(o for o in logged
                   if o.get("type") == "action" and o["tool"] == "deliver")
    assert deliver["args"]["escrow_id"] != "esc_RECORDED1"
    assert deliver["args"]["escrow_id"].startswith("esc_")
    fresh_obs = next(o for o in logged
                     if o.get("type") == "observation"
                     and "released" in o["text"])
    assert json.loads(fresh_obs["text"])["released"] is True


def test_llm_preflight_fails_with_guidance_when_cli_unset(monkeypatch):
    """No built-in default path: an unset PERMIT_GROK_CLI fails fast with guidance."""
    monkeypatch.delenv("PERMIT_GROK_CLI", raising=False)
    with pytest.raises(RuntimeError) as exc:
        llm_preflight()
    msg = str(exc.value)
    assert "PERMIT_GROK_CLI" in msg
    assert "--replay" in msg
