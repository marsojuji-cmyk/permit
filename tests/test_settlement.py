"""Settlement tests: N1, N2, replay, idempotency, chain-break, void, happy path."""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from permit.ledger import Ledger
from permit.permit import PermitStore
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import (
    Escrow,
    Evidence,
    PredicateType,
    ReleaseVerifier,
    sign_acceptance,
)


def _setup(predicate=PredicateType.D, amount_cents=3000):
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    permit, _ = permits.grant(
        agent_id="agent_1",
        cap_cents=5000,
        allowlist=["merchant_1"],
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    check = permits.check(permit.permit_id, amount_cents, "merchant_1")
    assert check.allowed
    auth_id = check.receipt.payload["auth_id"]
    pp_auth = paypal.authorize(amount_cents, "merchant_1")
    artifact = b"delivered work product"
    artifact_hash = hashlib.sha256(artifact).hexdigest()
    escrow = Escrow(
        escrow_id="esc_1",
        permit_id=permit.permit_id,
        auth_id=auth_id,
        paypal_auth_id=pp_auth.auth_id,
        amount_cents=amount_cents,
        merchant_id="merchant_1",
        predicate_type=predicate,
        artifact_hash=artifact_hash,
    )
    verifier.register(escrow)
    return verifier, paypal, permits, escrow, artifact


def test_happy_path_type_d():
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.D)
    result = verifier.verify_and_capture("esc_1", Evidence(delivered_bytes=artifact))
    assert result.released
    assert result.capture is not None
    assert result.capture.status == "COMPLETED"
    assert len(paypal.capture_calls) == 1
    # Permit accounting: reserved moved to captured.
    permit = permits.get(escrow.permit_id)
    assert permit.captured_cents == 3000
    assert permit.reserved_cents == 0


def test_happy_path_type_a():
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.A)
    sig = sign_acceptance("esc_1", escrow.artifact_hash, 3000)
    result = verifier.verify_and_capture("esc_1", Evidence(acceptance_signature=sig))
    assert result.released
    assert len(paypal.capture_calls) == 1


def test_n1_hash_mismatch_no_capture():
    """N1: mismatched artifact hash → no capture → REFUSED receipt."""
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.D)
    result = verifier.verify_and_capture(
        "esc_1", Evidence(delivered_bytes=b"tampered work product")
    )
    assert not result.released
    assert result.reason == "predicate:hash_mismatch"
    assert result.capture is None
    assert paypal.capture_calls == [], "capture must NEVER be called on N1"
    # REFUSED receipt exists.
    refused = [r for r in verifier.ledger.receipts() if r.event_type == "REFUSED"]
    assert len(refused) == 1
    # Permit reservation untouched (still reserved, not captured).
    permit = permits.get(escrow.permit_id)
    assert permit.reserved_cents == 3000
    assert permit.captured_cents == 0


def test_n2_worker_signature_without_acceptance_no_capture():
    """N2: worker signature without valid acceptance → no capture → REFUSED."""
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.A)
    # A worker-signed value that is NOT the acceptance key's signature.
    fake_sig = hashlib.sha256(b"worker says trust me").hexdigest()
    result = verifier.verify_and_capture(
        "esc_1", Evidence(acceptance_signature=fake_sig)
    )
    assert not result.released
    assert result.reason == "predicate:invalid_acceptance_signature"
    assert paypal.capture_calls == [], "capture must NEVER be called on N2"
    refused = [r for r in verifier.ledger.receipts() if r.event_type == "REFUSED"]
    assert len(refused) == 1


def test_replay_signature_on_other_escrow_fails():
    """Acceptance signature is bound to escrow id; replay fails."""
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.A)
    sig_for_esc1 = sign_acceptance("esc_1", escrow.artifact_hash, 3000)
    # Attacker replays esc_1's signature against a different escrow id.
    escrow.escrow_id = "esc_2"
    result = verifier.verify_and_capture(
        "esc_2", Evidence(acceptance_signature=sig_for_esc1)
    )
    assert not result.released
    assert paypal.capture_calls == []


def test_idempotent_capture_single_charge():
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.D)
    r1 = verifier.verify_and_capture("esc_1", Evidence(delivered_bytes=artifact))
    r2 = verifier.verify_and_capture("esc_1", Evidence(delivered_bytes=artifact))
    assert r1.released and r2.released
    assert r2.reason == "already_captured"
    # Only one capture call reached PayPal (second was short-circuited).
    assert len(paypal.capture_calls) == 1


def test_broken_chain_refuses_capture():
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.D)
    receipts = verifier.ledger.receipts()
    tampered = receipts[1]
    object.__setattr__(tampered, "payload", {**tampered.payload, "amount_cents": 1})
    verifier.ledger._receipts[1] = tampered
    result = verifier.verify_and_capture("esc_1", Evidence(delivered_bytes=artifact))
    assert not result.released
    assert result.reason == "broken_chain"
    assert paypal.capture_calls == []


def test_void_releases_permit_reservation():
    verifier, paypal, permits, escrow, artifact = _setup(PredicateType.D)
    assert verifier.void("esc_1")
    assert paypal.capture_calls == []
    assert escrow.paypal_auth_id in paypal.voids
    permit = permits.get(escrow.permit_id)
    assert permit.remaining_cents() == 5000
    # Capture after void is refused.
    result = verifier.verify_and_capture("esc_1", Evidence(delivered_bytes=artifact))
    assert not result.released


def test_only_verifier_calls_capture():
    import ast

    import settlement.verifier as v
    import settlement.paypal_client as pc

    tree = ast.parse(open(v.__file__).read())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "capture"
    ]
    assert len(calls) == 1, "exactly one capture call site must exist (the verifier)"
