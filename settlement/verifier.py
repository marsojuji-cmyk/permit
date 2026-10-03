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
    - Ledger chain broken → refuse (no capture); the hold IS voided
      (P1-4b fix: a broken ledger must not leave money encumbered).
    - Escrow not in AUTHORIZED state → refuse.
    - Permit missing / revoked / expired at release time → refuse;
      the hold IS voided (admission recheck, P1 fix).
    - Predicate fails → REFUSED receipt, no capture call. The hold is
      voided; if the void times out the escrow goes CLEANUP_PENDING
      with the reservation still held (P1-4 fix).
    - AI dispute arbitration is advisory only: a model saying "looks good"
      is not evidence and cannot authorize capture.
    - Any PayPalTimeout → escrow goes UNKNOWN. A timed-out call has
      unknown provider state, so the verifier NEVER optimistically marks
      captured/failed — reconcile() queries provider truth first.

Idempotency: capture is keyed by
    idempotency_key = f"{escrow.permit_id}:{escrow.auth_id}:capture",
derived from the permit/claim ids so it is stable across retries.
Retries return the same capture; the money moves at most once.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from permit.ledger import Ledger, Receipt
from permit.permit import PermitStore

from .paypal_client import PayPalClient, PayPalTimeout, Capture


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
    # Indeterminate states (P1 fixes):
    #   UNKNOWN          - provider state unknown after a timeout or a
    #                      PENDING capture; reconcile() resolves it.
    #   CLEANUP_PENDING  - capture forbidden, void not yet confirmed;
    #                      retry_cleanup() resolves it.


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


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of reconcile(): resolving an UNKNOWN escrow."""

    resolved: bool
    outcome: str  # captured | voided | still_unknown | already_resolved
    capture: Capture | None = None
    receipt: Receipt | None = None


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

    def get_escrow(self, escrow_id: str) -> Escrow | None:
        """Read-only accessor for one escrow (e-stop classification)."""
        with self._lock:
            return self._escrows.get(escrow_id)

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

    @staticmethod
    def _capture_key(escrow: Escrow) -> str:
        """
        Idempotency key derived from the permit/claim ids — stable across
        retries and process restarts, unlike the old escrow-id key.
        """
        return f"{escrow.permit_id}:{escrow.auth_id}:capture"

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
                self.ledger.append(
                    "REFUSED", {"escrow_id": escrow_id, "reason": "unknown_escrow"}
                )
                return VerifyResult(False, "unknown_escrow")

            # Already resolved → return the recorded outcome, never re-capture.
            if escrow.state == "CAPTURED":
                return VerifyResult(True, "already_captured")
            if escrow.state in ("VOIDED", "REFUSED"):
                return VerifyResult(False, f"already_{escrow.state.lower()}")
            # Indeterminate states need an explicit operator step first:
            # reconcile() for UNKNOWN, retry_cleanup() for CLEANUP_PENDING.
            # NO PayPal calls on these paths.
            if escrow.state == "UNKNOWN":
                return VerifyResult(False, "unknown_reconcile_first")
            if escrow.state == "CLEANUP_PENDING":
                return VerifyResult(False, "cleanup_pending")

            # Admission recheck: the permit may have been revoked or
            # expired after the hold was registered (e-stop race). Never
            # capture into a dead permit — void the hold instead.
            now = datetime.now(timezone.utc)
            permit = self.permits.get(escrow.permit_id)
            if permit is None or permit.revoked or now >= permit.expiry:
                admit_reason = (
                    "unknown_permit"
                    if permit is None
                    else "revoked"
                    if permit.revoked
                    else "expired"
                )
                self.ledger.append(
                    "REFUSED", {"escrow_id": escrow_id, "reason": admit_reason}
                )
                try:
                    self.paypal.void(escrow.paypal_auth_id)
                except PayPalTimeout:
                    # The void may or may not have applied: cleanup is
                    # pending, retry_cleanup() finishes it.
                    escrow.state = "CLEANUP_PENDING"
                    self.ledger.append(
                        "CLEANUP_PENDING",
                        {
                            "escrow_id": escrow_id,
                            "reason": admit_reason,
                            "detail": "void_timeout",
                        },
                    )
                    return VerifyResult(False, admit_reason)
                escrow.state = "VOIDED"
                if permit is not None:
                    # settle_void appends the VOIDED receipt (shared ledger).
                    self.permits.settle_void(escrow.permit_id, escrow.auth_id)
                return VerifyResult(False, admit_reason)

            # Ledger chain must be intact before any money moves.
            ok, chain_reason = self.ledger.verify_chain()
            if not ok:
                self.ledger.append(
                    "REFUSED",
                    {"escrow_id": escrow_id, "reason": f"broken_chain:{chain_reason}"},
                )
                # A broken ledger must not leave the money encumbered:
                # void the hold (fail closed on void timeout).
                try:
                    self.paypal.void(escrow.paypal_auth_id)
                except PayPalTimeout:
                    escrow.state = "CLEANUP_PENDING"
                    self.ledger.append(
                        "CLEANUP_PENDING",
                        {
                            "escrow_id": escrow_id,
                            "reason": "broken_chain",
                            "detail": "void_timeout",
                        },
                    )
                    return VerifyResult(False, "broken_chain")
                escrow.state = "VOIDED"
                self.permits.settle_void(escrow.permit_id, escrow.auth_id)
                return VerifyResult(False, "broken_chain")

            passes, pred_reason = self._predicate_passes(escrow, evidence)
            if not passes:
                self.ledger.append(
                    "REFUSED",
                    {"escrow_id": escrow_id, "reason": f"predicate:{pred_reason}"},
                )
                # Cleanup: a refused release must not leave the money
                # encumbered. Void the PayPal hold and release the permit
                # reservation together — the reservation always mirrors the
                # PayPal hold lifecycle, so both are released as one step.
                # NOTE: self.paypal.capture is NEVER called on this path.
                try:
                    self.paypal.void(escrow.paypal_auth_id)
                except PayPalTimeout:
                    # The hold may still be live and the reservation is
                    # STILL HELD: mark cleanup pending, release nothing
                    # yet. retry_cleanup() finishes it.
                    escrow.state = "CLEANUP_PENDING"
                    self.ledger.append(
                        "CLEANUP_PENDING",
                        {
                            "escrow_id": escrow_id,
                            "reason": f"predicate:{pred_reason}",
                            "detail": "void_timeout",
                        },
                    )
                    return VerifyResult(False, f"predicate:{pred_reason}")
                escrow.state = "REFUSED"
                self.permits.settle_void(escrow.permit_id, escrow.auth_id)
                return VerifyResult(False, f"predicate:{pred_reason}")

            # Predicate passed — single-flight idempotent capture.
            key = self._capture_key(escrow)
            try:
                capture = self.paypal.capture(
                    escrow.paypal_auth_id,
                    escrow.amount_cents,
                    idempotency_key=key,
                )
            except PayPalTimeout:
                # The response was lost: the capture may or may not have
                # applied. NEVER guess — mark UNKNOWN; reconcile() queries
                # provider truth before anything else happens.
                escrow.state = "UNKNOWN"
                self.ledger.append(
                    "UNKNOWN",
                    {
                        "escrow_id": escrow_id,
                        "reason": "capture_timeout",
                        "idempotency_key": key,
                    },
                )
                return VerifyResult(False, "unknown_after_timeout")

            if capture.status == "PENDING":
                # Provider accepted the capture but has not completed it:
                # the obligation is retained (reservation still held)
                # until reconcile() sees completed provider truth.
                escrow.state = "UNKNOWN"
                self.ledger.append(
                    "UNKNOWN",
                    {
                        "escrow_id": escrow_id,
                        "reason": "capture_pending",
                        "paypal_capture_id": capture.capture_id,
                        "idempotency_key": key,
                    },
                )
                return VerifyResult(False, "capture_pending")

            if capture.status != "COMPLETED":
                # Provider refused the capture: unwind the hold fail-closed.
                self.ledger.append(
                    "FAILED",
                    {
                        "escrow_id": escrow_id,
                        "paypal_capture_id": capture.capture_id,
                        "status": capture.status,
                    },
                )
                try:
                    self.paypal.void(escrow.paypal_auth_id)
                except PayPalTimeout:
                    escrow.state = "CLEANUP_PENDING"
                    self.ledger.append(
                        "CLEANUP_PENDING",
                        {
                            "escrow_id": escrow_id,
                            "reason": "capture_failed",
                            "detail": "void_timeout",
                        },
                    )
                    return VerifyResult(False, "capture_failed")
                escrow.state = "VOIDED"
                self.permits.settle_void(escrow.permit_id, escrow.auth_id)
                return VerifyResult(False, "capture_failed")

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

    def reconcile(self, escrow_id: str) -> ReconcileResult:
        """
        Resolve an UNKNOWN escrow against provider truth. The operator
        calls this after a capture timeout or a PENDING capture; the demo
        calls it on the timeout beat.

        Only UNKNOWN escrows are eligible — anything else returns
        already_resolved with NO side effects (idempotent).

        Rules:
          - get_authorization raises PayPalTimeout → stay UNKNOWN
            ("still_unknown", retryable).
          - Truth CAPTURED, or a capture recorded under the escrow's
            idempotency key → record the capture even if the permit is
            revoked/expired (a completed capture can't be voided).
          - Otherwise (AUTHORIZED/no capture, VOIDED, DENIED, FAILED) →
            fail closed: attempt void; void success → VOIDED; void
            timeout → stay UNKNOWN ("still_unknown").
        """
        with self._lock:
            escrow = self._escrows.get(escrow_id)
            if escrow is None or escrow.state != "UNKNOWN":
                return ReconcileResult(False, "already_resolved")

            try:
                truth = self.paypal.get_authorization(escrow.paypal_auth_id)
            except PayPalTimeout:
                return ReconcileResult(False, "still_unknown")

            key = self._capture_key(escrow)
            # Mock-only provider-side record: the sandbox client has no
            # .captures attribute; getattr keeps the interface uniform.
            recorded = getattr(self.paypal, "captures", {}) or {}

            if truth.status == "CAPTURED" or key in recorded:
                # The money moved. Record truth — even against a dead
                # permit, a completed capture cannot be voided.
                escrow.state = "CAPTURED"
                self.permits.settle_capture(escrow.permit_id, escrow.auth_id)
                capture = recorded.get(key)
                receipt = self.ledger.append(
                    "CAPTURED",
                    {
                        "escrow_id": escrow_id,
                        "paypal_capture_id": (
                            capture.capture_id if capture else truth.auth_id
                        ),
                        "amount_cents": escrow.amount_cents,
                        "reconciled": True,
                    },
                )
                return ReconcileResult(True, "captured", capture, receipt)

            # Fail closed: nothing captured, so unwind whatever may be live.
            try:
                self.paypal.void(escrow.paypal_auth_id)
            except PayPalTimeout:
                return ReconcileResult(False, "still_unknown")
            escrow.state = "VOIDED"
            receipt = self.permits.settle_void(escrow.permit_id, escrow.auth_id)
            return ReconcileResult(True, "voided", receipt=receipt)

    def retry_cleanup(self, escrow_id: str) -> bool:
        """
        Retry the void for a CLEANUP_PENDING escrow. True on success
        (escrow VOIDED, reservation released); False on PayPalTimeout
        (stays pending, retryable). No-ops on any other state.
        """
        with self._lock:
            escrow = self._escrows.get(escrow_id)
            if escrow is None or escrow.state != "CLEANUP_PENDING":
                return False
            try:
                self.paypal.void(escrow.paypal_auth_id)
            except PayPalTimeout:
                return False
            escrow.state = "VOIDED"
            self.permits.settle_void(escrow.permit_id, escrow.auth_id)
            return True

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
