"""
Permit end-to-end demo (mock mode, no credentials, no network).

Runs the whole spend pipeline and prints the receipt chain:
  1. grant a permit (cap $50, one merchant, 1h expiry)
  2. ALLOWED spend: $30 to the allowed merchant -> hold -> release -> capture
  3. BLOCKED spend: $30 over the remaining cap -> receipt, PayPal never called
  4. E-stop on a second permit mid-hold -> escrow voided, release refused

Every step prints its ledger receipts. Nothing here is faked: the chain
verifies at the end, and the mock PayPal client records every call.

Sandbox mode (real rail) is wired the same way - see --sandbox, which needs
PERMIT_PAYPAL_CLIENT_ID and PERMIT_PAYPAL_CLIENT_SECRET, plus interactive
payer approval of each order in a browser (the NeedsPayerApproval path).
"""

from __future__ import annotations

import argparse
import hashlib
import os
from datetime import datetime, timedelta, timezone

from permit.ledger import Ledger
from permit.permit import PermitStore
from permit.flow import SpendPipeline
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import Evidence, PredicateType, ReleaseVerifier, sign_acceptance


def _dollars(cents) -> str:
    """Format integer cents as a dollar string ($30, or $30.50 when not round)."""
    try:
        cents = int(cents)
    except (TypeError, ValueError):
        return "$?"
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


# One plain-English line per receipt event type, so a first-time reader knows
# what just happened. Payload fields are interpolated when present.
EXPLANATIONS = {
    "GRANTED": lambda p: (
        f"A spending permit was issued: {_dollars(p.get('cap_cents'))} cap for "
        f"{p.get('agent_id', 'the agent')}, allowlist {p.get('allowlist', [])}."
    ),
    "ALLOWED": lambda p: (
        f"Agent tried to spend {_dollars(p.get('amount_cents'))} at "
        f"{p.get('merchant_id', 'the merchant')}; all four permit clauses passed "
        f"({_dollars(p.get('remaining_cents'))} left)."
    ),
    "AUTHORIZED": lambda p: (
        f"Money for the {_dollars(p.get('amount_cents'))} spend was held on the "
        "permit (not yet captured); PayPal authorization created."
    ),
    "CAPTURED": lambda p: f"Payment captured: {_dollars(p.get('amount_cents'))} moved.",
    "BLOCKED": lambda p: (
        f"Spend blocked ({p.get('reason', 'not authorized')}): "
        f"{_dollars(p.get('remaining_cents'))} left, requested "
        f"{_dollars(p.get('amount_cents'))}."
    ),
    "E-STOP": lambda p: (
        "E-stop revoked the permit and voided its in-flight authorization "
        f"({_dollars(p.get('reserved_cents_released'))} released)."
    ),
    "VOIDED": lambda p: "A held authorization was voided; no money moved.",
}


def _format_explanation(event_type: str, payload: dict) -> str:
    """Format a one-line plain-English explanation for the event type."""
    tmpl_func = EXPLANATIONS.get(event_type)
    if not tmpl_func:
        return f"No explanation for {event_type}."
    try:
        return tmpl_func(payload)
    except Exception:
        return f"No explanation for {event_type}."


def show(receipts, prefix=""):
    for r in receipts:
        p = r.payload
        detail = p.get("reason", p.get("permit_id", ""))
        print(f"{prefix}#{r.seq:02d} {r.event_type:10s} {detail}")
        print(f"{prefix}     -> {_format_explanation(r.event_type, p)}")


def build_pipeline():
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)
    return flow, paypal, permits, ledger


def demo_mock():
    flow, paypal, permits, ledger = build_pipeline()
    merchant = "demo_merchant"

    print("== 1. grant ==")
    permit, receipt = permits.grant(
        agent_id="demo_agent",
        cap_cents=5000,
        allowlist=[merchant],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    show((receipt,))

    print("== 2. ALLOWED spend $30, release with valid evidence ==")
    artifact = b"demo deliverable"
    digest = hashlib.sha256(artifact).hexdigest()
    attempt = flow.spend(permit.permit_id, 3000, merchant, PredicateType.D, digest)
    show(attempt.receipts)
    result = flow.release(attempt.escrow_id, Evidence(delivered_bytes=artifact))
    show((ledger.receipts()[-1],))
    print(f"   released={result.released} capture={result.capture.capture_id}")

    print("== 3. BLOCKED spend $30 (only $20 left of the $50 cap) ==")
    attempt = flow.spend(permit.permit_id, 3000, merchant, PredicateType.D, digest)
    show(attempt.receipts)
    print(f"   PayPal authorize calls after block: {len(paypal.authorizations)} (unchanged)")

    print("== 4. e-stop mid-hold on a second permit ==")
    permit2, _ = permits.grant(
        agent_id="demo_agent",
        cap_cents=5000,
        allowlist=[merchant],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    attempt2 = flow.spend(permit2.permit_id, 1000, merchant, PredicateType.D, digest)
    receipt, voided = flow.estop(permit2.permit_id)
    show((receipt,) + tuple(ledger.receipts()[-len(voided):]))
    refused = flow.release(attempt2.escrow_id, Evidence(delivered_bytes=artifact))
    print(f"   post-e-stop release: released={refused.released} reason={refused.reason}")

    ok, reason = ledger.verify_chain()
    print(f"== ledger chain: {'VERIFIED' if ok else 'BROKEN: ' + reason} ({len(ledger)} receipts)")


def main():
    ap = argparse.ArgumentParser(description="Permit end-to-end demo")
    ap.add_argument("--sandbox", action="store_true", help="use the real sandbox rail")
    args = ap.parse_args()
    if args.sandbox:
        print("Sandbox mode needs PERMIT_PAYPAL_CLIENT_ID/SECRET and interactive")
        print("payer approval per order - run from a session with a browser tap.")
        print("Use mock mode for the scripted camera run.")
        return
    demo_mock()


if __name__ == "__main__":
    main()
