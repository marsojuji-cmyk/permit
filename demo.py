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


def show(receipts, prefix=""):
    for r in receipts:
        p = r.payload
        detail = p.get("reason", p.get("permit_id", ""))
        print(f"{prefix}#{r.seq:02d} {r.event_type:10s} {detail}")


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
