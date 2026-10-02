"""Spend-pipeline tests: the check->hold->escrow path, e-stop wiring, resume."""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from permit.ledger import Ledger
from permit.permit import PermitStore
from permit.flow import SpendPipeline
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import Evidence, PredicateType, ReleaseVerifier


def _pipeline(cap_cents=5000, merchants=("merchant_1",)):
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)
    permit, _ = permits.grant(
        agent_id="agent_1",
        cap_cents=cap_cents,
        allowlist=list(merchants),
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return flow, paypal, permits, permit


def _artifact():
    b = b"delivered work product"
    return b, hashlib.sha256(b).hexdigest()


def test_blocked_spend_never_touches_paypal():
    flow, paypal, permits, permit = _pipeline()
    attempt = flow.spend(permit.permit_id, 99999, "merchant_1", PredicateType.D, "hash")
    assert not attempt.allowed
    assert attempt.reason == "over_remaining_cap"
    assert attempt.escrow_id is None
    assert attempt.receipts[0].event_type == "BLOCKED"
    # Not even the authorize call happened.
    assert len(paypal.authorizations) == 0
    assert len(paypal.capture_calls) == 0


def test_blocked_wrong_merchant_never_touches_paypal():
    flow, paypal, permits, permit = _pipeline()
    attempt = flow.spend(permit.permit_id, 1000, "evil_merchant", PredicateType.D, "hash")
    assert not attempt.allowed
    assert attempt.reason == "merchant_not_allowed"
    assert len(paypal.authorizations) == 0


def test_allowed_spend_registers_escrow():
    flow, paypal, permits, permit = _pipeline()
    _, digest = _artifact()
    attempt = flow.spend(permit.permit_id, 3000, "merchant_1", PredicateType.D, digest)
    assert attempt.allowed
    assert attempt.escrow_id is not None
    assert [r.event_type for r in attempt.receipts] == ["ALLOWED", "AUTHORIZED"]
    assert permit.remaining_cents() == 2000
    ok, _ = flow.ledger.verify_chain()
    assert ok


def test_happy_path_release_captures():
    flow, paypal, permits, permit = _pipeline()
    artifact, digest = _artifact()
    attempt = flow.spend(permit.permit_id, 3000, "merchant_1", PredicateType.D, digest)
    result = flow.release(attempt.escrow_id, Evidence(delivered_bytes=artifact))
    assert result.released
    assert result.capture.status == "COMPLETED"
    assert permit.remaining_cents() == 2000  # reserved -> captured
    assert permit.captured_cents == 3000


def test_bad_evidence_refuses_and_never_captures():
    flow, paypal, permits, permit = _pipeline()
    _, digest = _artifact()
    attempt = flow.spend(permit.permit_id, 3000, "merchant_1", PredicateType.D, digest)
    result = flow.release(attempt.escrow_id, Evidence(delivered_bytes=b"wrong bytes"))
    assert not result.released
    assert result.reason.startswith("predicate:")
    assert len(paypal.capture_calls) == 0
    assert permit.remaining_cents() == 2000  # reservation still held


def test_estop_voids_in_flight_escrow():
    flow, paypal, permits, permit = _pipeline()
    _, digest = _artifact()
    attempt = flow.spend(permit.permit_id, 3000, "merchant_1", PredicateType.D, digest)
    receipt, voided = flow.estop(permit.permit_id)
    assert receipt.event_type == "E-STOP"
    assert voided == [attempt.escrow_id]
    assert permit.revoked
    # Release after e-stop is refused: the escrow is VOIDED.
    artifact, _ = _artifact()
    result = flow.release(attempt.escrow_id, Evidence(delivered_bytes=artifact))
    assert not result.released
    assert result.reason == "already_voided"
    assert len(paypal.capture_calls) == 0


def test_spend_after_estop_blocked():
    flow, paypal, permits, permit = _pipeline()
    flow.estop(permit.permit_id)
    attempt = flow.spend(permit.permit_id, 1000, "merchant_1", PredicateType.D, "hash")
    assert not attempt.allowed
    assert attempt.reason == "revoked"
    assert len(paypal.authorizations) == 0


def test_register_escrow_resume_does_not_double_reserve():
    """Sandbox-mode resume path after NeedsPayerApproval."""
    flow, paypal, permits, permit = _pipeline()
    _, digest = _artifact()
    # Simulate: check() passed, PayPal hold taken, spend() raised before
    # registration. Resume registers the escrow directly.
    check = permits.check(permit.permit_id, 3000, "merchant_1")
    auth_id = check.receipt.payload["auth_id"]
    pp_auth = paypal.authorize(3000, "merchant_1")
    escrow_id, receipt = flow.register_escrow(
        permit_id=permit.permit_id,
        auth_id=auth_id,
        paypal_auth_id=pp_auth.auth_id,
        amount_cents=3000,
        merchant_id="merchant_1",
        predicate_type=PredicateType.D,
        artifact_hash=digest,
    )
    assert receipt.event_type == "AUTHORIZED"
    assert permit.reserved_cents == 3000  # single reservation, not doubled
    # E-stop still voids it through the mapping.
    _, voided = flow.estop(permit.permit_id)
    assert voided == [escrow_id]
