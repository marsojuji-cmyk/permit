"""
Principal-approval thresholds: spends above a permit's threshold need a
human word before any reservation is made. No approval -> no reservation,
no PayPal traffic. Approval re-runs the authority check at completion
(fail-closed if the budget moved).
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from permit.flow import SpendPipeline
from permit.ledger import Ledger
from permit.permit import PermitStore
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import Evidence, PredicateType, ReleaseVerifier


def make_store(cap=10000, threshold=2500):
    ledger = Ledger()
    store = PermitStore(ledger)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    permit, _ = store.grant(
        "buyer", cap, ["m1"], expiry,
        approval_threshold_cents=threshold,
    )
    return store, ledger, permit


def make_flow(store, ledger):
    paypal = MockPayPalClient()
    return SpendPipeline(store, paypal, ReleaseVerifier(paypal, store, ledger=ledger)), paypal


def ah():
    return hashlib.sha256(b"artifact").hexdigest()


def deliver_all(flow, escrow_id):
    return flow.release(escrow_id, Evidence(delivered_bytes=b"artifact"))


def test_below_threshold_is_normally_allowed():
    store, ledger, permit = make_store()
    flow, paypal = make_flow(store, ledger)
    a = flow.spend(permit.permit_id, 2000, "m1", PredicateType.D, ah())
    assert a.allowed and a.approval_id is None
    assert store.get(permit.permit_id).reserved_cents == 2000
    assert len(paypal.authorize_calls) == 1


def test_no_threshold_means_no_gate():
    ledger = Ledger()
    store = PermitStore(ledger)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    permit, _ = store.grant("buyer", 10000, ["m1"], expiry)
    assert store.approval_threshold(permit.permit_id) is None
    flow, paypal = make_flow(store, ledger)
    a = flow.spend(permit.permit_id, 9000, "m1", PredicateType.D, ah())
    assert a.allowed  # backwards compatible: no threshold, no PENDING


def test_at_threshold_is_allowed_not_pending():
    store, ledger, permit = make_store(threshold=2500)
    flow, paypal = make_flow(store, ledger)
    a = flow.spend(permit.permit_id, 2500, "m1", PredicateType.D, ah())
    assert a.allowed and a.approval_id is None


def test_above_threshold_pends_without_reservation_or_paypal():
    store, ledger, permit = make_store()
    flow, paypal = make_flow(store, ledger)
    a = flow.spend(permit.permit_id, 4000, "m1", PredicateType.D, ah())
    assert not a.allowed
    assert a.reason == "pending_principal_approval"
    assert a.approval_id
    # Nothing reserved, PayPal never touched.
    assert store.get(permit.permit_id).reserved_cents == 0
    assert paypal.authorize_calls == []
    kinds = [r.event_type for r in ledger.receipts()]
    assert "APPROVAL_REQUESTED" in kinds


def test_approval_fails_closed_when_budget_moved():
    store, ledger, permit = make_store(cap=5000, threshold=2500)
    flow, paypal = make_flow(store, ledger)

    # 1. Spend above threshold -> PENDING
    a = flow.spend(permit.permit_id, 4000, "m1", PredicateType.D, ah())
    assert not a.allowed
    assert a.approval_id

    # 2. In the meantime, another spend consumes the budget
    a2 = flow.spend(permit.permit_id, 3000, "m1", PredicateType.D, ah())
    assert a2.allowed

    # 3. Now principal approves the first spend -> authority check re-runs and fails
    complete_res = store.complete_approved_spend(a.approval_id)
    assert not complete_res.allowed
    assert complete_res.reason == "tightened_cap_exceeded"
    assert paypal.authorize_calls == []
    assert not store.has_approval_been_consumed(a.approval_id)


def test_approval_fails_closed_when_parent_revoked():
    store, ledger, permit = make_store(cap=10000, threshold=2500)
    flow, paypal = make_flow(store, ledger)

    # 1. Spend above threshold -> PENDING
    a = flow.spend(permit.permit_id, 4000, "m1", PredicateType.D, ah())
    assert not a.allowed
    assert a.approval_id

    # 2. Parent permit is revoked after principal approved / before completion
    store.revoke(permit.permit_id, "principal e-stop")

    # 3. Complete approved spend -> authority recheck fails closed due to revocation
    complete_res = store.complete_approved_spend(a.approval_id)
    assert not complete_res.allowed
    assert complete_res.reason == "revoked"
    assert paypal.authorize_calls == []
    assert not store.has_approval_been_consumed(a.approval_id)
