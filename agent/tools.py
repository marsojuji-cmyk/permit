"""
Tools the demo agent may call. Each tool is a thin, honest wrapper around
the SpendPipeline: the agent decides WHAT to attempt, Permit decides
whether it happens. The agent can never bypass the permit check, touch
PayPal directly, or release an escrow itself.
"""

from __future__ import annotations

import hashlib
import json

from permit.flow import SpendPipeline
from settlement.verifier import Evidence, PredicateType


class SpendTools:
    """
    catalog: purpose -> artifact bytes the marketplace precommits.
    The escrow's artifact_hash is committed at spend time from the catalog;
    deliver() submits the matching bytes (honest merchant), while the
    harness may submit tampered bytes to demonstrate REFUSED.
    """

    def __init__(
        self,
        flow: SpendPipeline,
        permit_id: str,
        merchant_id: str,
        catalog: dict[str, bytes],
    ):
        self.flow = flow
        self.permit_id = permit_id
        self.merchant_id = merchant_id
        self.catalog = catalog
        self._escrow_purpose: dict[str, str] = {}

    # -- tools the LLM may call -------------------------------------------

    def attempt_spend(self, amount_cents: int, purpose: str) -> str:
        if purpose not in self.catalog:
            return json.dumps({"ok": False, "error": f"unknown purpose {purpose!r}"})
        artifact_hash = hashlib.sha256(self.catalog[purpose]).hexdigest()
        attempt = self.flow.spend(
            self.permit_id, amount_cents, self.merchant_id,
            PredicateType.D, artifact_hash,
        )
        if not attempt.allowed:
            return json.dumps({
                "ok": False,
                "decision": "BLOCKED",
                "reason": attempt.reason,
                "paypal_called": False,
            })
        self._escrow_purpose[attempt.escrow_id] = purpose
        auth_receipt = attempt.receipts[1]
        return json.dumps({
            "ok": True,
            "decision": "ALLOWED",
            "escrow_id": attempt.escrow_id,
            "paypal_auth_id": auth_receipt.payload["paypal_auth_id"],
            "amount_cents": amount_cents,
        })

    def deliver(self, escrow_id: str) -> str:
        """The merchant delivers the bytes matching the precommitment."""
        purpose = self._escrow_purpose.get(escrow_id)
        if purpose is None:
            return json.dumps({"ok": False, "error": "unknown escrow_id"})
        result = self.flow.release(
            escrow_id, Evidence(delivered_bytes=self.catalog[purpose]))
        return json.dumps({
            "ok": result.released,
            "released": result.released,
            "reason": result.reason,
            "capture_id": result.capture.capture_id if result.capture else None,
        })

    def check_permit(self) -> str:
        permit = self.flow.permits.get(self.permit_id)
        if permit is None:
            return json.dumps({"ok": False, "error": "permit not found"})
        return json.dumps({
            "ok": True,
            "permit_id": permit.permit_id,
            "cap_cents": permit.cap_cents,
            "reserved_cents": permit.reserved_cents,
            "captured_cents": permit.captured_cents,
            "remaining_cents": permit.remaining_cents(),
            "revoked": permit.revoked,
        })

    # -- harness-only (NOT exposed to the LLM) ------------------------------

    def deliver_tampered(self, escrow_id: str) -> str:
        """Simulate a compromised worker submitting wrong bytes."""
        result = self.flow.release(
            escrow_id, Evidence(delivered_bytes=b"tampered-bytes"))
        return json.dumps({
            "ok": result.released,
            "released": result.released,
            "reason": result.reason,
            "capture_id": result.capture.capture_id if result.capture else None,
        })
