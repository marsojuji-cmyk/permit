"""Server entrypoint tests: the HTTP verbs over the real pipeline."""

import base64
import hashlib
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import server
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import ReleaseVerifier
from permit.flow import SpendPipeline
from permit.ledger import Ledger
from permit.permit import PermitStore


@pytest.fixture()
def live_server(monkeypatch):
    # Fresh in-memory state per test; the server's globals get rebound.
    ledger = Ledger()
    permits = PermitStore(ledger=ledger)
    paypal = MockPayPalClient()
    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)
    monkeypatch.setattr(server, "ledger", ledger)
    monkeypatch.setattr(server, "permits", permits)
    monkeypatch.setattr(server, "paypal", paypal)
    monkeypatch.setattr(server, "verifier", verifier)
    monkeypatch.setattr(server, "flow", flow)
    monkeypatch.setattr(server, "MOCK_MODE", True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, paypal
    srv.shutdown()


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_issue_check_spend_release(live_server):
    base, _ = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 10000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    _, check = call(base, "POST", f"/api/permits/{pid}/check",
                    {"amount_cents": 1000, "merchant_id": "m"})
    assert check["allowed"]
    artifact = b"bytes"
    digest = hashlib.sha256(artifact).hexdigest()
    _, spend = call(base, "POST", f"/api/permits/{pid}/spend",
                    {"amount_cents": 1000, "merchant_id": "m",
                     "predicate": "delivery_hash", "artifact_hash": digest})
    assert spend["allowed"] and spend["escrow_id"]
    _, rel = call(base, "POST", f"/api/escrows/{spend['escrow_id']}/release",
                  {"delivered_bytes_b64": base64.b64encode(artifact).decode()})
    assert rel["released"] and rel["capture_id"]


def test_blocked_never_touches_paypal(live_server):
    base, paypal = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 1000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    before = dict(paypal.authorizations)
    _, blocked = call(base, "POST", f"/api/permits/{pid}/spend",
                      {"amount_cents": 9999, "merchant_id": "m",
                       "predicate": "delivery_hash", "artifact_hash": "x" * 64})
    assert not blocked["allowed"] and blocked["reason"] == "over_remaining_cap"
    assert blocked["escrow_id"] is None
    assert paypal.authorizations == before


def test_estop_voids_in_flight(live_server):
    base, paypal = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 5000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    _, spend = call(base, "POST", f"/api/permits/{pid}/spend",
                    {"amount_cents": 1000, "merchant_id": "m",
                     "predicate": "delivery_hash", "artifact_hash": "y" * 64})
    escrow_id = spend["escrow_id"]
    _, estop = call(base, "POST", f"/api/permits/{pid}/estop", {})
    assert estop["revoked"] and escrow_id in estop["voided_escrows"]
    _, refused = call(base, "POST", f"/api/escrows/{escrow_id}/release",
                      {"delivered_bytes_b64": base64.b64encode(b"nope").decode()})
    assert not refused["released"]
    _, check = call(base, "POST", f"/api/permits/{pid}/check",
                    {"amount_cents": 100, "merchant_id": "m"})
    assert not check["allowed"] and check["reason"] == "revoked"
    assert len(paypal.voids) == 1


def test_ledger_chain_verifies(live_server):
    base, _ = live_server
    call(base, "POST", "/api/permits",
         {"agent_id": "a", "cap_cents": 1000, "allowlist": ["m"], "expiry_hours": 1})
    _, led = call(base, "GET", "/api/ledger")
    assert led["chain"]["ok"]
    assert led["receipts"][0]["event"] == "GRANTED"


def call_expect_error(base, method, path, body, want_status):
    """Like call(), but for routes that must answer with an HTTP error."""
    import urllib.error
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10):
            raise AssertionError(f"expected HTTP {want_status}, got 2xx")
    except urllib.error.HTTPError as e:
        assert e.code == want_status, f"expected {want_status}, got {e.code}"
        return json.loads(e.read().decode())


def test_check_is_read_only(live_server):
    """P1-3: /check evaluates authority without reserving or receipting."""
    base, paypal = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 5000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    _, led_before = call(base, "GET", "/api/ledger")
    _, check = call(base, "POST", f"/api/permits/{pid}/check",
                    {"amount_cents": 1000, "merchant_id": "m"})
    assert check["allowed"] and check["reason"] == "allowed"
    _, led_after = call(base, "GET", "/api/ledger")
    assert len(led_after["receipts"]) == len(led_before["receipts"]), \
        "check must write no receipt"
    _, state = call(base, "GET", f"/api/permits/{pid}")
    assert state["reserved_cents"] == 0, "check must not reserve"
    assert paypal.authorizations == {}


def test_http_rejects_non_positive_amounts(live_server):
    """P1-3: the HTTP adapter rejects non-positive amounts with 400."""
    base, _ = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 5000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    err = call_expect_error(base, "POST", f"/api/permits/{pid}/check",
                            {"amount_cents": -1000, "merchant_id": "m"}, 400)
    assert "positive integer" in err["error"]
    err = call_expect_error(base, "POST", f"/api/permits/{pid}/spend",
                            {"amount_cents": 0, "merchant_id": "m",
                             "predicate": "delivery_hash",
                             "artifact_hash": "x" * 64}, 400)
    assert "positive integer" in err["error"]
    _, state = call(base, "GET", f"/api/permits/{pid}")
    assert state["remaining_cents"] == 5000, "rejected amounts gain no capacity"


def test_reconcile_route_converges_unknown(live_server):
    """Timeout → UNKNOWN → POST /reconcile → captured, one capture."""
    base, paypal = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 5000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    artifact = b"goods"
    digest = hashlib.sha256(artifact).hexdigest()
    _, spend = call(base, "POST", f"/api/permits/{pid}/spend",
                    {"amount_cents": 2500, "merchant_id": "m",
                     "predicate": "delivery_hash", "artifact_hash": digest})
    escrow_id = spend["escrow_id"]
    paypal.inject_capture_timeout = "after_apply"  # response lost on the wire
    _, rel = call(base, "POST", f"/api/escrows/{escrow_id}/release",
                  {"delivered_bytes_b64": base64.b64encode(artifact).decode()})
    paypal.inject_capture_timeout = None
    assert not rel["released"] and rel["reason"] == "unknown_after_timeout"
    _, rec = call(base, "POST", f"/api/escrows/{escrow_id}/reconcile", {})
    assert rec["resolved"] and rec["outcome"] == "captured"
    assert rec["capture_id"] is not None
    assert len(paypal.capture_calls) == 1, "exactly one capture on the rail"
    _, state = call(base, "GET", f"/api/permits/{pid}")
    assert state["captured_cents"] == 2500 and state["reserved_cents"] == 0


def test_retry_cleanup_route_clears_stranded_void(live_server):
    """P1-4: void timeout strands cleanup → /retry-cleanup clears it."""
    base, paypal = live_server
    _, grant = call(base, "POST", "/api/permits",
                    {"agent_id": "a", "cap_cents": 5000,
                     "allowlist": ["m"], "expiry_hours": 1})
    pid = grant["permit_id"]
    digest = hashlib.sha256(b"goods").hexdigest()
    _, spend = call(base, "POST", f"/api/permits/{pid}/spend",
                    {"amount_cents": 3000, "merchant_id": "m",
                     "predicate": "delivery_hash", "artifact_hash": digest})
    escrow_id = spend["escrow_id"]
    paypal.inject_void_timeout = True
    _, rel = call(base, "POST", f"/api/escrows/{escrow_id}/release",
                  {"delivered_bytes_b64": base64.b64encode(b"tampered").decode()})
    paypal.inject_void_timeout = False
    assert not rel["released"] and rel["reason"] == "predicate:hash_mismatch"
    _, state = call(base, "GET", f"/api/permits/{pid}")
    assert state["reserved_cents"] == 3000, "reservation held while void unconfirmed"
    _, retry = call(base, "POST", f"/api/escrows/{escrow_id}/retry-cleanup", {})
    assert retry["cleanup_cleared"] is True
    _, state = call(base, "GET", f"/api/permits/{pid}")
    assert state["reserved_cents"] == 0 and state["remaining_cents"] == 5000
