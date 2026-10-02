"""
Permit dashboard: live view of permits, escrows, and the receipt chain.

Stdlib only. The server holds references to the SAME PermitStore, Ledger,
SpendPipeline and ReleaseVerifier objects the demo drives, so the page
reflects live state (polls /api/state every second). The e-stop button
calls flow.estop() for real.

Run standalone for dev: python3 dashboard/server.py  (empty demo state)
The five-beat demo starts it in-process with live objects.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


class Dashboard:
    def __init__(self, permits=None, ledger=None, flow=None, verifier=None,
                 port: int = 8471):
        self.permits = permits
        self.ledger = ledger
        self.flow = flow
        self.verifier = verifier
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    # -- state ------------------------------------------------------------

    def state(self) -> dict:
        permits = []
        if self.permits:
            for p in self.permits._permits.values():
                permits.append({
                    "permit_id": p.permit_id,
                    "agent_id": p.agent_id,
                    "cap_cents": p.cap_cents,
                    "reserved_cents": p.reserved_cents,
                    "captured_cents": p.captured_cents,
                    "remaining_cents": p.remaining_cents(),
                    "revoked": p.revoked,
                    "allowlist": list(p.allowlist),
                })
        receipts = []
        if self.ledger:
            for r in self.ledger.receipts():
                receipts.append({
                    "seq": r.seq,
                    "event": r.event_type,
                    "payload": r.payload,
                    "timestamp": r.timestamp,
                    "hash": r.hash[:12],
                })
        escrows = []
        if self.verifier:
            for e in self.verifier._escrows.values():
                escrows.append({
                    "escrow_id": e.escrow_id,
                    "permit_id": e.permit_id,
                    "amount_cents": e.amount_cents,
                    "merchant_id": e.merchant_id,
                    "predicate": e.predicate_type.value,
                    "state": e.state,
                    "paypal_auth_id": e.paypal_auth_id,
                })
        chain_ok, chain_reason = self.ledger.verify_chain() if self.ledger else (True, "")
        return {
            "permits": permits,
            "escrows": escrows,
            "receipts": receipts,
            "chain_ok": chain_ok,
            "chain_reason": chain_reason,
        }

    def estop(self, permit_id: str) -> dict:
        if not self.flow:
            return {"ok": False, "error": "no flow bound"}
        receipt, voided = self.flow.estop(permit_id)
        return {"ok": True, "receipt_seq": receipt.seq, "voided": voided}

    # -- http ---------------------------------------------------------------

    def _handler(self):
        dash = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/":
                    with open(os.path.join(HERE, "index.html"), "rb") as f:
                        self._send(200, f.read(), "text/html")
                elif self.path == "/api/state":
                    self._send(200, json.dumps(dash.state()))
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                if self.path == "/api/estop":
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    self._send(200, json.dumps(
                        dash.estop(body.get("permit_id", ""))))
                else:
                    self._send(404, b'{"error":"not found"}')

        return H

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port),
                                           self._handler())
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()


if __name__ == "__main__":
    Dashboard(port=8471).start()
    print("dashboard on http://127.0.0.1:8471 (empty state)")
    threading.Event().wait()
