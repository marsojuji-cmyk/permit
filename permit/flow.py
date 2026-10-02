"""
Spend pipeline: the only path from a spend attempt to money movement.

    spend():   permit.check -> BLOCKED? write receipt, STOP. PayPal is never
               touched (not even the authorize call).
               ALLOWED?  reserve cap, take a PayPal AUTHORIZE hold,
               register the escrow with the release-verifier.
    release(): release-verifier's verify_and_capture - the sole capture path.
    estop():   revoke the permit, void every in-flight escrow.

Invariant: no PayPal authorize or capture is reachable without an ALLOWED
receipt on the same permit for the same attempt. The pipeline is the only
caller of paypal.authorize() and verifier.register().

The permit package's isolation still holds: this module may import from
settlement; permit/permit.py itself MUST NOT import any PayPal client
(enforced by import test).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .ledger import Ledger, Receipt
from .permit import PermitStore
from settlement.verifier import Escrow


@dataclass(frozen=True)
class SpendAttempt:
    allowed: bool
    reason: str
    escrow_id: str | None
    receipts: tuple[Receipt, ...]


class SpendPipeline:
    """Wires the permit check to the PayPal hold and the release-verifier."""

    def __init__(self, permits: PermitStore, paypal, verifier, ledger: Ledger | None = None):
        self.permits = permits
        self.paypal = paypal
        self.verifier = verifier
        # NOTE: explicit None check - an empty Ledger is falsy via __len__.
        self.ledger = ledger if ledger is not None else permits.ledger
        # permit-level auth_id -> escrow_id, so e-stop can void in-flight holds.
        self._auth_to_escrow: dict[str, str] = {}

    def spend(
        self,
        permit_id: str,
        amount_cents: int,
        merchant_id: str,
        predicate_type,
        artifact_hash: str,
    ) -> SpendAttempt:
        """
        One spend attempt. Returns ALLOWED + escrow_id on success, or
        BLOCKED with the receipt and zero PayPal traffic.

        With the real sandbox client, paypal.authorize() may raise
        NeedsPayerApproval: the cap reservation stays held and the caller
        retries after the payer approves - but must NOT call spend() again
        (that would double-reserve the cap). Use register_escrow() to
        resume after the hold exists.
        """
        check = self.permits.check(permit_id, amount_cents, merchant_id)
        if not check.allowed:
            # BLOCKED: receipt written by check(). PayPal is never called.
            return SpendAttempt(False, check.reason, None, (check.receipt,))

        auth_id = check.receipt.payload["auth_id"]
        # The ONLY paypal.authorize call path in the codebase.
        pp_auth = self.paypal.authorize(amount_cents, merchant_id)

        escrow_id, authorized_receipt = self.register_escrow(
            permit_id=permit_id,
            auth_id=auth_id,
            paypal_auth_id=pp_auth.auth_id,
            amount_cents=amount_cents,
            merchant_id=merchant_id,
            predicate_type=predicate_type,
            artifact_hash=artifact_hash,
        )
        return SpendAttempt(
            True, "allowed", escrow_id, (check.receipt, authorized_receipt)
        )

    def register_escrow(
        self,
        permit_id: str,
        auth_id: str,
        paypal_auth_id: str,
        amount_cents: int,
        merchant_id: str,
        predicate_type,
        artifact_hash: str,
    ) -> tuple[str, Receipt]:
        """
        Register an escrow for an already-authorized permit hold.
        Sandbox-mode resume path after NeedsPayerApproval: the PayPal hold
        exists but spend() raised before registration, so register here
        instead of re-spending.
        """
        escrow_id = f"esc_{uuid.uuid4().hex[:12]}"
        escrow = Escrow(
            escrow_id=escrow_id,
            permit_id=permit_id,
            auth_id=auth_id,
            paypal_auth_id=paypal_auth_id,
            amount_cents=amount_cents,
            merchant_id=merchant_id,
            predicate_type=predicate_type,
            artifact_hash=artifact_hash,
        )
        authorized_receipt = self.verifier.register(escrow)
        self._auth_to_escrow[auth_id] = escrow_id
        return escrow_id, authorized_receipt

    def release(self, escrow_id: str, evidence):
        """Evidence in, money out - or a REFUSED receipt. Pass-through."""
        return self.verifier.verify_and_capture(escrow_id, evidence)

    def estop(self, permit_id: str) -> tuple[Receipt, list[str]]:
        """
        E-stop: revoke the permit, then void every in-flight escrow on it.
        Returns the e-stop receipt and the voided escrow ids.
        """
        receipt, in_flight_auth_ids = self.permits.estop(permit_id)
        voided: list[str] = []
        for auth_id in in_flight_auth_ids:
            escrow_id = self._auth_to_escrow.get(auth_id)
            if escrow_id is not None and self.verifier.void(escrow_id):
                voided.append(escrow_id)
        return receipt, voided
