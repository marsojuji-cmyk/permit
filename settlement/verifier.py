"""
Release-verifier: the ONLY module that may call PayPal capture.

For an authorized escrow, the verifier evaluates the release predicate
against submitted evidence. Predicate passes → single-flight idempotent
capture. Predicate fails → REFUSED receipt, and capture is NEVER called.

Predicate types:
    Type D (delivery): sha256(delivered_bytes) == precommitted artifact_hash.
    Type A (acceptance): a SEPARATE acceptance key signs
        (escrow_id, artifact_hash, amount_cents).
        A worker's signature alone releases nothing. The signature is
        bound to the escrow id (replay on another escrow fails).

Fail-closed rules (adversary-required):
    - Ledger chain broken → refuse (no capture).
    - Escrow not in AUTHORIZED state → refuse.
    - Predicate fails → REFUSED receipt, no capture call.
    - AI dispute arbitration is advisory only: a model saying "looks good"
      is not evidence and cannot authorize capture.

Idempotency: capture is keyed by idempotency_key = f"{escrow_id}:release".
Retries return the same capture; the money moves at most once.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass, field
from enum import Enum

from permit.ledger import Ledger
from permit.permit import PermitStore

from .paypal_client import PayPalClient, Capture


class PredicateType(str, Enum):
    D = "delivery_hash"
    A = "acceptance_signature"


@dataclass
class Escrow:
    escrow_id: str
    permit_id: str
    auth_id: str  # permit-level reservation id
    paypal_auth_id: str  # PayPal authorization id
    amount_cents: int
    merchant_id: str
    predicate_type: PredicateType
    artifact_hash: str  # precommitted at escrow creation
    state: str = "AUTHORIZED"  # AUTHORIZED -> CAPTURED | VOIDED | REFUSED


@dataclass(frozen=True)
class Evidence:
    """What the worker submits to request release."""

    delivered_bytes: bytes | None = None  # Type D
    acceptance_signature: str | None = None  # Type A (hex HMAC)


@dataclass(frozen=True)
class VerifyResult:
    released: bool
    reason: str
    capture: Capture | None = None


# Demo acceptance key. Hardcoded and labeled as such — no key-management scope.
# In a real deployment this is the counterparty's key, never the worker's.
DEMO_ACCEPTANCE_KEY = b"demo-acceptance-key-NOT-FOR-PRODUCTION"


def sign_acceptance(escrow_id: str, artifact_hash: str, amount_cents: int) -> str:
    """What the counterparty (acceptance key holder) does off-camera."""
    msg = f"{escrow_id}|{artifact_hash}|{amount_cents}".encode()
    return hmac.new(DEMO_ACCEPTANCE_KEY, msg, hashlib.sha256).hexdigest()


class ReleaseVerifier:
    def __init__(
        self,
        paypal: PayPalClient,
        permits: PermitStore,
        ledger: Ledger | None = None,
    ):
        self.paypal = paypal
        self.permits = permits
        # NOTE: explicit None check — an empty Ledger is falsy via __len__.
        self.ledger = ledger if ledger is not None else Ledger()
        self._escrows: dict[str, Escrow] = {}
        self._lock = threading.Lock()

    def register(self, escrow: Escrow) -> Receipt:
        """Register an escrow hold. Returns the AUTHORIZED receipt."""
        with self._lock:
            self._escrows[escrow.escrow_id] = escrow
        return self.ledger.append(
            "AUTHORIZED",
            {
                "escrow_id": escrow.escrow_id,
                "permit_id": escrow.permit_id,
                "paypal_auth_id": escrow.paypal_auth_id,
                "amount_cents": escrow.amount_cents,
                "predicate": escrow.predicate_type.value,
                "artifact_hash": escrow.artifact_hash,
            },
        )

    def _predicate_passes(self, escrow: Escrow, evidence: Evidence) -> tuple[bool, str]:
        if escrow.predicate_type is PredicateType.D:
            if evidence.delivered_bytes is None:
                return False, "missing_delivered_bytes"
            digest = hashlib.sha256(evidence.delivered_bytes).hexdigest()
            if not hmac.compare_digest(digest, escrow.artifact_hash):
                return False, "hash_mismatch"
            return True, "hash_match"

        if escrow.predicate_type is PredicateType.A:
            if evidence.acceptance_signature is None:
                return False, "missing_acceptance_signature"
            expected = sign_acceptance(
                escrow.escrow_id, escrow.artifact_hash, escrow.amount_cents
            )
            if not hmac.compare_digest(evidence.acceptance_signature, expected):
                return False, "invalid_acceptance_signature"
            return True, "acceptance_valid"

        return False, "unknown_predicate"

    def verify_and_capture(self, escrow_id: str, evidence: Evidence) -> VerifyResult:
        """
        Single-flight, fail-closed release. The ONLY path to capture.
        """
        with self._lock:
            escrow = self._escrows.get(escrow_id)
            if escrow is None:
                receipt = self.ledger.append(
                    "REFUSED", {"escrow_id": escrow_id, "reason": "unknown_escrow"}
                )
                return VerifyResult(False, "unknown_escrow")

            # Already resolved → return the recorded outcome, never re-capture.
            if escrow.state == "CAPTURED":
                return VerifyResult(True, "already_captured")
            if escrow.state in ("VOIDED", "REFUSED"):
                return VerifyResult(False, f"already_{escrow.state.lower()}")

            # Ledger chain must be intact before any money moves.
            ok, chain_reason = self.ledger.verify_chain()
            if not ok:
                escrow.state = "REFUSED"
                self.ledger.append(
                    "REFUSED",
                    {"escrow_id": escrow_id, "reason": f"broken_chain:{chain_reason}"},
                )
                return VerifyResult(False, "broken_chain")

            passes, pred_reason = self._predicate_passes(escrow, evidence)
            if not passes:
                escrow.state = "REFUSED"
                self.ledger.append(
                    "REFUSED",
                    {"escrow_id": escrow_id, "reason": f"predicate:{pred_reason}"},
                )
                # NOTE: self.paypal.capture is NEVER called on this path.
                return VerifyResult(False, f"predicate:{pred_reason}")

            # Predicate passed — single-flight idempotent capture.
            capture = self.paypal.capture(
                escrow.paypal_auth_id,
                escrow.amount_cents,
                idempotency_key=f"{escrow_id}:release",
            )
            escrow.state = "CAPTURED"
            self.permits.settle_capture(escrow.permit_id, escrow.auth_id)
            self.ledger.append(
                "CAPTURED",
                {
                    "escrow_id": escrow_id,
                    "paypal_capture_id": capture.capture_id,
                    "amount_cents": escrow.amount_cents,
                },
            )
            return VerifyResult(True, "released", capture)

    def void(self, escrow_id: str) -> bool:
        """Void an in-flight authorization (e-stop path)."""
        with self._lock:
            escrow = self._escrows.get(escrow_id)
            if escrow is None or escrow.state != "AUTHORIZED":
                return False
            self.paypal.void(escrow.paypal_auth_id)
            escrow.state = "VOIDED"
            self.permits.settle_void(escrow.permit_id, escrow.auth_id)
            self.ledger.append("VOIDED", {"escrow_id": escrow_id})
            return True
