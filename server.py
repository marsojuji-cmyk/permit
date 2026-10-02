#!/usr/bin/env python3
"""
Permit service: a minimal HTTP server exposing the core verbs.

    issue permit      POST /api/permits            {agent_id, cap_cents, allowlist, expiry_hours}
    permit state      GET  /api/permits/<permit_id>
    check authority   POST /api/permits/<permit_id>/check   {amount_cents, merchant_id}
    spend (allowed)   POST /api/permits/<permit_id>/spend   {amount_cents, merchant_id, predicate, artifact_hash}
    release escrow    POST /api/escrows/<escrow_id>/release {delivered_bytes_b64} | {acceptance_signature}
    e-stop            POST /api/permits/<permit_id>/estop   {}
    ledger            GET  /api/ledger
    debug (mock only) GET  /debug/paypal -> mock call counts

Runs in mock mode by default (no network, no credentials). --sandbox wires
the real SandboxPayPalClient against PayPal's sandbox REST API; it needs
PERMIT_PAYPAL_CLIENT_ID and PERMIT_PAYPAL_CLIENT_SECRET, and each order
still needs interactive payer approval (NeedsPayerApproval) — so scripted
camera runs stay in mock mode.

Stdlib only. JSON in, JSON out.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from permit.flow import SpendPipeline
from permit.ledger import Ledger
from permit.permit import PermitStore
from settlement.paypal_client import MockPayPalClient
from settlement.verifier import (
    Evidence,
    PredicateType,
    ReleaseVerifier,
    sign_acceptance,
)

# ---------------------------------------------------------------- state

ledger = Ledger()
permits = PermitStore(ledger=ledger)
paypal = None          # set in main()
verifier = None        # set in main()
flow = None            # set in main()
MOCK_MODE = True


def receipt_summary(r):
    return {"seq": r.seq, "event": r.event_type, "hash": r.hash[:12], "payload": r.payload}


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "Permit/0.1"

    # -- plumbing ----------------------------------------------------
    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def log_message(self, fmt, *args):  # keep server logs quiet-ish
        pass

    # -- routing -----------------------------------------------------
    def do_GET(self):
        m = re.fullmatch(r"/api/permits/([\w-]+)", self.path)
        if m:
            p = permits.get(m.group(1))
            if p is None:
                return self._error(404, "unknown_permit")
            return self._send(200, {
                "permit_id": p.permit_id,
                "agent_id": p.agent_id,
                "cap_cents": p.cap_cents,
                "allowlist": list(p.allowlist),
                "expiry": p.expiry.isoformat(),
                "revoked": p.revoked,
                "reserved_cents": p.reserved_cents,
                "captured_cents": p.captured_cents,
                "remaining_cents": p.remaining_cents(),
                "in_flight": len(p.in_flight),
            })
        if self.path == "/api/ledger":
            ok, reason = ledger.verify_chain()
            return self._send(200, {
                "chain": {"ok": ok, "reason": reason},
                "receipts": [receipt_summary(r) for r in ledger.receipts()],
            })
        if self.path == "/debug/paypal":
            if not MOCK_MODE:
                return self._error(404, "debug only available in mock mode")
            return self._send(200, {
                "authorizations": len(paypal.authorizations),
                "captures": len(paypal.captures),
                "voids": len(paypal.voids),
            })
        return self._error(404, "not_found")

    def do_POST(self):
        body = self._read_json()

        # issue permit
        if self.path == "/api/permits":
            try:
                agent_id = body["agent_id"]
                cap_cents = int(body["cap_cents"])
                allowlist = list(body["allowlist"])
                hours = float(body.get("expiry_hours", 1))
            except (KeyError, TypeError, ValueError):
                return self._error(400, "need agent_id, cap_cents, allowlist, expiry_hours")
            permit, receipt = permits.grant(
                agent_id=agent_id,
                cap_cents=cap_cents,
                allowlist=allowlist,
                expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
            )
            return self._send(201, {"permit_id": permit.permit_id, "receipt": receipt_summary(receipt)})

        m = re.fullmatch(r"/api/permits/([\w-]+)/check", self.path)
        if m:
            try:
                amount = int(body["amount_cents"]); merchant = body["merchant_id"]
            except (KeyError, TypeError, ValueError):
                return self._error(400, "need amount_cents, merchant_id")
            check = permits.check(m.group(1), amount, merchant)
            return self._send(200, {
                "allowed": check.allowed, "reason": check.reason,
                "receipt": receipt_summary(check.receipt),
            })

        m = re.fullmatch(r"/api/permits/([\w-]+)/spend", self.path)
        if m:
            try:
                amount = int(body["amount_cents"]); merchant = body["merchant_id"]
                predicate = PredicateType(body.get("predicate", "delivery_hash"))
                artifact_hash = body["artifact_hash"]
            except (KeyError, TypeError, ValueError):
                return self._error(400, "need amount_cents, merchant_id, predicate, artifact_hash")
            attempt = flow.spend(m.group(1), amount, merchant, predicate, artifact_hash)
            return self._send(200, {
                "allowed": attempt.allowed, "reason": attempt.reason,
                "escrow_id": attempt.escrow_id,
                "receipts": [receipt_summary(r) for r in attempt.receipts],
            })

        m = re.fullmatch(r"/api/permits/([\w-]+)/estop", self.path)
        if m:
            try:
                receipt, voided = flow.estop(m.group(1))
            except AssertionError:
                return self._error(404, "unknown_permit")
            return self._send(200, {
                "revoked": True, "voided_escrows": voided,
                "receipt": receipt_summary(receipt),
            })

        m = re.fullmatch(r"/api/escrows/([\w-]+)/release", self.path)
        if m:
            escrow_id = m.group(1)
            delivered_b64 = body.get("delivered_bytes_b64")
            sig = body.get("acceptance_signature")
            delivered = None
            if delivered_b64 is not None:
                try:
                    delivered = base64.b64decode(delivered_b64, validate=True)
                except (binascii.Error, ValueError):
                    return self._error(400, "delivered_bytes_b64 is not valid base64")
            evidence = Evidence(delivered_bytes=delivered, acceptance_signature=sig)
            result = flow.release(escrow_id, evidence)
            return self._send(200, {
                "released": result.released, "reason": result.reason,
                "capture_id": result.capture.capture_id if result.capture else None,
            })

        return self._error(404, "not_found")


# ---------------------------------------------------------------- main

def main():
    global paypal, verifier, flow, MOCK_MODE
    ap = argparse.ArgumentParser(description="Permit service")
    ap.add_argument("--port", type=int, default=8741)
    ap.add_argument("--sandbox", action="store_true",
                    help="use the real sandbox rail (needs env creds + interactive payer approval)")
    args = ap.parse_args()

    if args.sandbox:
        from settlement.sandbox_client import SandboxPayPalClient
        client_id = os.environ.get("PERMIT_PAYPAL_CLIENT_ID")
        client_secret = os.environ.get("PERMIT_PAYPAL_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SystemExit("sandbox mode needs PERMIT_PAYPAL_CLIENT_ID and PERMIT_PAYPAL_CLIENT_SECRET")
        paypal = SandboxPayPalClient(client_id, client_secret)
        MOCK_MODE = False
        print("permit: sandbox rail armed")
    else:
        paypal = MockPayPalClient()
        print("permit: mock rail (no network, no credentials)")

    verifier = ReleaseVerifier(paypal, permits, ledger=ledger)
    flow = SpendPipeline(permits, paypal, verifier, ledger=ledger)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"permit: listening on 127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
