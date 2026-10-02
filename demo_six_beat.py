"""
Permit six-beat demo (mock mode: no credentials, no network).

The human principal grants the permit; a REAL LLM agent (Grok, ReAct loop)
decides what to spend. The dashboard (http://127.0.0.1:8471) shows live
state: remaining budget, in-flight escrows, the receipt chain, e-stop.

Beats:
  1. Human grants a $50 permit: one merchant, one hour.
  2. Agent buys the $30 dataset license -> ALLOWED -> hold -> deliver ->
     capture id appears.
  3. Agent tries the $60 premium tier -> BLOCKED ($20 left). No PayPal id.
  4. New permit, agent holds $10 in flight -> human hits E-STOP ->
     permit revoked, escrow voided -> release refused.
  5. Agent holds $15 -> worker submits tampered bytes -> REFUSED, no capture.
  6. Agent holds $25 -> the capture applies at PayPal but the response is
     dropped -> escrow goes UNKNOWN (never optimistically claimed) ->
     reconcile() converges to the provider truth: CAPTURED, exactly one
     capture on the rail, no double charge.

Usage:
  python3 demo_six_beat.py [--replay TRANSCRIPT] [--fast] [--port 8471]
  --replay re-runs a saved agent transcript without calling the LLM.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from agent.react import run_agent
from agent.tools import SpendTools
from dashboard.server import Dashboard
from permit.flow import SpendPipeline
from permit.ledger import Ledger
from permit.permit import PermitStore
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import PredicateType, ReleaseVerifier

MERCHANT = "dataset_mart"
CATALOG = {
    "dataset license": b"dataset-license-v1::dataset_mart",
    "premium tier": b"dataset-license-premium::dataset_mart",
    "api credits": b"api-credits-1000::dataset_mart",
    "report": b"quarterly-report-q3::dataset_mart",
}


def beat(n: int, title: str, fast: bool):
    print(f"\n{'='*60}\nBEAT {n}: {title}\n{'='*60}")
    if not fast:
        time.sleep(1.5)


def show_receipts(ledger, since: int):
    for r in ledger.receipts()[since:]:
        p = r.payload
        detail = p.get("reason", "")
        if r.event_type in ("ALLOWED", "BLOCKED"):
            detail = f"${p.get('amount_cents', 0)/100:.2f} {detail}"
        elif r.event_type == "AUTHORIZED":
            detail = f"${p['amount_cents']/100:.2f} paypal {p['paypal_auth_id'][:14]}..."
        elif r.event_type == "CAPTURED":
            if "paypal_capture_id" in p:  # settlement layer: the money moved
                detail = (f"${p['amount_cents']/100:.2f} capture "
                          f"{p['paypal_capture_id'][:14]}...")
            else:  # permit layer: reserved -> captured bookkeeping
                detail = (f"${p['amount_cents']/100:.2f} settled "
                          f"(remaining ${p['remaining_cents']/100:.2f})")
        elif r.event_type == "UNKNOWN":
            detail = (f"escrow {p['escrow_id'][:12]}... "
                      f"reason={p.get('reason', '')}")
        elif r.event_type == "CLEANUP_PENDING":
            detail = f"escrow {p['escrow_id'][:12]}... void unconfirmed"
        elif r.event_type == "VOIDED":
            if "escrow_id" in p:  # settlement layer
                detail = f"escrow {p['escrow_id'][:12]}..."
            else:  # permit layer: reservation released
                detail = (f"${p['amount_cents']/100:.2f} released "
                          f"(remaining ${p['remaining_cents']/100:.2f})")
        print(f"  #{r.seq:02d} {r.event_type:10s} {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default=None)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--port", type=int, default=8471)
    args = ap.parse_args()

    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)
    dash = Dashboard(permits, ledger, flow, verifier, port=args.port).start()
    print(f"dashboard: http://127.0.0.1:{args.port}")
    transcript = "demo_transcript.jsonl"
    open(transcript, "w").close()  # fresh transcript per run

    # -- beat 1: the human grants -----------------------------------------
    beat(1, "human grants a $50 permit (one merchant, one hour)", args.fast)
    mark = len(ledger)
    permit, _ = permits.grant(
        agent_id="demo_agent", cap_cents=5000, allowlist=[MERCHANT],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    show_receipts(ledger, mark)
    print(f"  remaining: ${permit.remaining_cents()/100:.2f}")
    tools = SpendTools(flow, permit.permit_id, MERCHANT, CATALOG)

    # -- beat 2: agent buys the $30 license --------------------------------
    beat(2, "agent buys the $30 dataset license", args.fast)
    mark = len(ledger)
    answer = run_agent(
        tools, "Buy the dataset license for $30.00 from dataset_mart.",
        transcript, replay=args.replay, beat=2)
    show_receipts(ledger, mark)
    print(f"  agent: {answer}")
    print(f"  PayPal authorize calls so far: {len(paypal.authorizations)}")

    # -- beat 3: agent tries to overspend -----------------------------------
    beat(3, "agent tries the $60 premium tier (only $20 left)", args.fast)
    mark = len(ledger)
    before = len(paypal.authorizations)
    answer = run_agent(
        tools, "The team wants the premium tier dataset license for $60.00 "
               "from dataset_mart. Buy it if you can.",
        transcript, replay=args.replay, beat=3)
    show_receipts(ledger, mark)
    print(f"  agent: {answer}")
    print(f"  PayPal authorize calls: {len(paypal.authorizations)} "
          f"(unchanged: {len(paypal.authorizations) == before})")

    # -- beat 4: e-stop mid-hold --------------------------------------------
    beat(4, "e-stop: revoke mid-hold, void the authorization", args.fast)
    mark = len(ledger)
    permit2, _ = permits.grant(
        agent_id="demo_agent", cap_cents=5000, allowlist=[MERCHANT],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    tools2 = SpendTools(flow, permit2.permit_id, MERCHANT, CATALOG)
    answer = run_agent(
        tools2, "Buy api credits for $10.00 from dataset_mart. Attempt the "
                "spend only - do NOT call deliver; the merchant delivers "
                "separately.",
        transcript, replay=args.replay, beat=4)
    # find the in-flight escrow
    escrow_id = next(e["escrow_id"] for e in dash.state()["escrows"]
                     if e["permit_id"] == permit2.permit_id
                     and e["state"] == "AUTHORIZED")
    print(f"  escrow {escrow_id[:14]}... in flight; human hits E-STOP")
    receipt, voided = flow.estop(permit2.permit_id)
    show_receipts(ledger, mark)
    # the agent tries to deliver anyway -> refused
    obs = json.loads(tools2.deliver(escrow_id))
    print(f"  post-e-stop deliver: released={obs['released']} "
          f"reason={obs['reason']}")

    # -- beat 5: tampered evidence -------------------------------------------
    beat(5, "mismatched evidence is REFUSED (no capture)", args.fast)
    mark = len(ledger)
    answer = run_agent(
        tools, "Buy the quarterly report for $15.00 from dataset_mart. "
                "Attempt the spend only - do NOT call deliver; the merchant "
                "delivers separately.",
        transcript, replay=args.replay, beat=5)
    escrow_id = next(e["escrow_id"] for e in dash.state()["escrows"]
                     if e["permit_id"] == permit.permit_id
                     and e["state"] == "AUTHORIZED")
    print(f"  worker submits bytes that do NOT match the precommitment...")
    obs = json.loads(tools.deliver_tampered(escrow_id))
    show_receipts(ledger, mark)
    print(f"  released={obs['released']} reason={obs['reason']} "
          f"capture_id={obs['capture_id']}")
    print(f"  PayPal capture calls total: {len(paypal.capture_calls)} "
          f"(expected 1: only the honest $30 delivery)")

    # -- beat 6: dropped provider response ------------------------------------
    beat(6, "dropped capture response: UNKNOWN -> reconcile -> consistent",
         args.fast)
    mark = len(ledger)
    permit3, _ = permits.grant(
        agent_id="demo_agent", cap_cents=5000, allowlist=[MERCHANT],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    tools3 = SpendTools(flow, permit3.permit_id, MERCHANT, CATALOG)
    obs = json.loads(tools3.attempt_spend(2500, "api credits"))
    assert obs["ok"], obs
    escrow_id = obs["escrow_id"]
    print(f"  escrow {escrow_id[:14]}... authorized; merchant delivers honestly")
    # The capture applies at PayPal, but the response is lost on the wire.
    # The escrow must go UNKNOWN — never optimistically claimed either way.
    paypal.inject_capture_timeout = "after_apply"
    obs = json.loads(tools3.deliver(escrow_id))
    paypal.inject_capture_timeout = None
    print(f"  released={obs['released']} reason={obs['reason']} "
          f"(escrow state: UNKNOWN — not captured, not failed)")
    print(f"  reconciling against the provider's truth...")
    rec = verifier.reconcile(escrow_id)
    cap = rec.capture.capture_id[:14] + "..." if rec.capture else None
    print(f"  reconcile: resolved={rec.resolved} outcome={rec.outcome} "
          f"capture={cap}")
    show_receipts(ledger, mark)
    print(f"  PayPal capture calls total: {len(paypal.capture_calls)} "
          f"(exactly one capture reached PayPal — the idempotency key "
          f"prevented a second charge)")

    # -- close-out ------------------------------------------------------------
    ok, reason = ledger.verify_chain()
    print(f"\n{'='*60}\nledger chain: "
          f"{'VERIFIED' if ok else 'BROKEN: ' + reason} "
          f"({len(ledger)} receipts)\n"
          f"transcript: {transcript}\n"
          f"dashboard: http://127.0.0.1:{args.port} (still live)")
    dash.stop()


if __name__ == "__main__":
    main()
