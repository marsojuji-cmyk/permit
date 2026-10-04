"""Delegation tests: sub-permits, carve accounting, cascade revocation."""

import hashlib
from datetime import datetime, timedelta, timezone

from permit.ledger import Ledger
from permit.permit import PermitStore
from permit.flow import SpendPipeline
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import Evidence, PredicateType, ReleaseVerifier


def _expiry(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _store(cap_cents=5000, merchants=("m",)):
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    expiry = _expiry()
    parent, _ = permits.grant(
        agent_id="buyer", cap_cents=cap_cents,
        allowlist=list(merchants), expiry=expiry,
    )
    return permits, parent, expiry


def _pipeline(cap_cents=5000, merchants=("m",)):
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)
    expiry = _expiry()
    parent, _ = permits.grant(
        agent_id="buyer", cap_cents=cap_cents,
        allowlist=list(merchants), expiry=expiry,
    )
    return flow, paypal, permits, parent, expiry


def _artifact():
    b = b"delegation work product"
    return b, hashlib.sha256(b).hexdigest()


# -- delegate() constraints -------------------------------------------------

def test_delegate_carves_and_reserves_parent():
    permits, parent, EXP = _store()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    assert res.ok and res.permit is not None
    assert res.permit.parent_id == parent.permit_id
    assert parent.reserved_cents == 2000
    assert parent.remaining_cents() == 3000
    assert res.permit.remaining_cents() == 2000
    delegated = [r for r in permits.ledger.receipts()
                 if r.event_type == "DELEGATED"]
    assert len(delegated) == 1


def test_delegate_over_parent_remaining_blocked():
    permits, parent, EXP = _store()
    res = permits.delegate(parent.permit_id, "researcher", 6000, ["m"], EXP)
    assert not res.ok and res.reason == "over_parent_remaining"
    assert permits.children_of(parent.permit_id) == []


def test_delegate_allowlist_escalation_blocked():
    permits, parent, EXP = _store()
    res = permits.delegate(
        parent.permit_id, "researcher", 1000, ["m", "evil_mart"], EXP)
    assert not res.ok and res.reason == "allowlist_escalation"


def test_delegate_expiry_beyond_parent_blocked():
    permits, parent, EXP = _store()
    res = permits.delegate(
        parent.permit_id, "researcher", 1000, ["m"],
        datetime.now(timezone.utc) + timedelta(hours=5))
    assert not res.ok and res.reason == "expiry_beyond_parent"


def test_delegate_revoked_parent_blocked():
    permits, parent, EXP = _store()
    permits.estop(parent.permit_id)
    res = permits.delegate(parent.permit_id, "researcher", 1000, ["m"], EXP)
    assert not res.ok and res.reason == "parent_revoked"


def test_delegate_unknown_parent_blocked():
    permits, _, EXP = _store()
    res = permits.delegate("prm_nope", "researcher", 1000, ["m"], EXP)
    assert not res.ok and res.reason == "unknown_parent"


def test_delegate_invalid_amount_blocked():
    permits, parent, EXP = _store()
    res = permits.delegate(parent.permit_id, "researcher", 0, ["m"], EXP)
    assert not res.ok and res.reason == "invalid_amount"


# -- carve accounting --------------------------------------------------------

def test_child_spend_and_capture_roll_up_to_parent():
    flow, paypal, permits, parent, EXP = _pipeline()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    _, digest = _artifact()
    attempt = flow.spend(child.permit_id, 1200, "m", PredicateType.D, digest)
    assert attempt.allowed
    released = flow.release(
        attempt.escrow_id, Evidence(delivered_bytes=b"delegation work product"))
    assert released.released
    # Child books.
    assert permits.get(child.permit_id).captured_cents == 1200
    assert permits.get(child.permit_id).remaining_cents() == 800
    # Parent roll-up: the carve converts reserved -> captured.
    assert parent.reserved_cents == 800
    assert parent.captured_cents == 1200
    assert parent.remaining_cents() == 3000


def test_child_void_does_not_touch_parent_carve():
    flow, paypal, permits, parent, EXP = _pipeline()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    _, digest = _artifact()
    attempt = flow.spend(child.permit_id, 1200, "m", PredicateType.D, digest)
    assert attempt.allowed
    assert permits.get(child.permit_id).reserved_cents == 1200
    # Void the child's hold directly (settlement path).
    escrow_id = attempt.escrow_id
    assert flow.verifier.void(escrow_id)
    assert permits.get(child.permit_id).reserved_cents == 0
    assert permits.get(child.permit_id).remaining_cents() == 2000
    # Parent carve untouched: the void stayed within the child's pool.
    assert parent.reserved_cents == 2000
    assert parent.captured_cents == 0


def test_child_cannot_exceed_own_cap():
    flow, paypal, permits, parent, EXP = _pipeline()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    _, digest = _artifact()
    attempt = flow.spend(child.permit_id, 2500, "m", PredicateType.D, digest)
    assert not attempt.allowed
    assert attempt.reason == "over_remaining_cap"
    assert paypal.authorizations == {}


def test_nested_delegation_rolls_up_two_levels():
    flow, paypal, permits, parent, EXP = _pipeline()
    c1 = permits.delegate(
        parent.permit_id, "manager", 2000, ["m"], EXP).permit
    c2 = permits.delegate(
        c1.permit_id, "worker", 800, ["m"], EXP).permit
    _, digest = _artifact()
    attempt = flow.spend(c2.permit_id, 500, "m", PredicateType.D, digest)
    assert attempt.allowed
    released = flow.release(
        attempt.escrow_id, Evidence(delivered_bytes=b"delegation work product"))
    assert released.released
    assert permits.get(c2.permit_id).captured_cents == 500
    assert permits.get(c1.permit_id).captured_cents == 500
    assert parent.captured_cents == 500
    # Middle permit: carved 800 to child, 500 captured through it.
    assert permits.get(c1.permit_id).reserved_cents == 300
    # Root: carved 2000, 500 captured through the chain.
    assert parent.reserved_cents == 1500
    assert parent.remaining_cents() == 3000


# -- cascade revocation ------------------------------------------------------

def test_cascade_revoke_releases_unspent_carves():
    flow, paypal, permits, parent, EXP = _pipeline()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    _, digest = _artifact()
    attempt = flow.spend(child.permit_id, 1200, "m", PredicateType.D, digest)
    released = flow.release(
        attempt.escrow_id, Evidence(delivered_bytes=b"delegation work product"))
    assert released.released

    receipt, voided = flow.revoke_cascade(parent.permit_id)
    assert receipt.event_type == "E-STOP"
    assert permits.get(child.permit_id).revoked
    # Unspent carve (2000 - 1200 captured) released to the parent.
    assert parent.reserved_cents == 0
    assert parent.captured_cents == 1200
    assert parent.remaining_cents() == 3800
    cascaded = [r for r in permits.ledger.receipts()
                if r.event_type == "REVOKED_CASCADE"]
    assert len(cascaded) == 1
    released_receipts = [r for r in permits.ledger.receipts()
                         if r.event_type == "CARVE_RELEASED"]
    assert len(released_receipts) == 1
    assert released_receipts[0].payload["released_cents"] == 800


def test_cascade_revoke_voids_child_inflight_hold():
    flow, paypal, permits, parent, EXP = _pipeline()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    _, digest = _artifact()
    attempt = flow.spend(child.permit_id, 500, "m", PredicateType.D, digest)
    assert attempt.allowed
    escrow_id = attempt.escrow_id

    receipt, voided = flow.revoke_cascade(parent.permit_id)
    assert escrow_id in voided
    # The child's hold was voided: no capture possible afterwards.
    assert parent.reserved_cents == 0
    assert parent.captured_cents == 0
    assert parent.remaining_cents() == 5000


def test_release_carve_idempotent():
    permits, parent, EXP = _store()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    permits.revoke_subtree(parent.permit_id)
    first = permits.release_carve(child.permit_id)
    assert first is not None
    assert parent.reserved_cents == 0
    second = permits.release_carve(child.permit_id)
    assert second is None
    assert parent.reserved_cents == 0


def test_single_child_revoke_reclaims_carve():
    permits, parent, EXP = _store()
    res = permits.delegate(parent.permit_id, "researcher", 2000, ["m"], EXP)
    child = res.permit
    permits.revoke_subtree(child.permit_id)
    permits.release_carve(child.permit_id)
    # Parent untouched and whole again; child's spend is now refused.
    assert not parent.revoked
    assert parent.remaining_cents() == 5000
    assert permits.children_of(parent.permit_id) == []
