"""
Permit: the authorization layer. Decides what an agent is ALLOWED to spend.

Authority check (Grok review fix — cumulative reservation):
    amount(a) <= remaining(P)
    AND merchant(a) IN allowlist(P)
    AND now < expiry(P)
    AND NOT revoked(P)

    remaining(P) = cap(P) − reserved(P) − captured(P)

An authorization hold RESERVES cap; capture moves reserved→captured;
void releases the reservation. Parallel attempts serialize on the permit
(per-permit lock), so concurrent attempts cannot exceed the cap (test C1).

A failed check writes a BLOCKED receipt and never reaches PayPal.
This module MUST NOT import any PayPal client (enforced by import test).

E-stop: revoke() flips the permit to revoked and returns the in-flight
authorization ids the settlement layer must void. The e-stop is itself
a receipted event.

Expiry semantics (authority definition — binding on all workstreams):
    Expiry is evaluated at BOTH boundaries, never just one:
    1. Check time — check()/eligible() block any attempt on an expired
       (or missing, or revoked) permit.
    2. Release/capture-admission time — the settlement layer MUST re-admit
       the permit via permits.get() immediately before capture and refuse
       when the permit is missing, revoked, or expired at that moment.
    Release after expiry therefore fails closed: an escrow authorized
    against a then-valid permit cannot capture once the permit has
    expired or been revoked. This module defines the rule; the settlement
    workstream enforces the admission check at capture time.

Amount discipline (P1-3 witness fix): amounts are positive integers.
_validate_amount() guards the authority boundary (check() and grant());
an invalid amount never reserves, never raises through check() — it is
recorded as a BLOCKED receipt with reason "invalid_amount".
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ledger import Ledger, Receipt


def _validate_amount(amount_cents) -> int:
    """
    Amount guard at the authority boundary: amount_cents must be an int
    (bool excluded) and strictly positive. Raises ValueError otherwise.
    """
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise ValueError(
            "amount_cents must be a positive int, "
            f"got {type(amount_cents).__name__}: {amount_cents!r}"
        )
    if amount_cents <= 0:
        raise ValueError(f"amount_cents must be > 0, got {amount_cents}")
    return amount_cents


@dataclass
class CheckResult:
    allowed: bool
    reason: str
    # None for read-only evaluations (eligible()): no receipt is written.
    receipt: Receipt | None = None


@dataclass
class Permit:
    permit_id: str
    agent_id: str
    cap_cents: int
    allowlist: tuple[str, ...]
    expiry: datetime
    revoked: bool = False
    reserved_cents: int = 0
    captured_cents: int = 0
    # Authorization ids currently holding a reservation on this permit.
    in_flight: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def remaining_cents(self) -> int:
        return self.cap_cents - self.reserved_cents - self.captured_cents


class PermitStore:
    """Issues permits, evaluates spend attempts, handles e-stop."""

    def __init__(self, ledger: Ledger | None = None):
        # NOTE: explicit None check — an empty Ledger is falsy via __len__,
        # so `ledger or Ledger()` would silently discard a passed empty ledger.
        self.ledger = ledger if ledger is not None else Ledger()
        self._permits: dict[str, Permit] = {}
        self._store_lock = threading.Lock()

    def grant(
        self,
        agent_id: str,
        cap_cents: int,
        allowlist: list[str],
        expiry: datetime,
    ) -> tuple[Permit, Receipt]:
        _validate_amount(cap_cents)
        permit = Permit(
            permit_id=f"prm_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            cap_cents=cap_cents,
            allowlist=tuple(allowlist),
            expiry=expiry,
        )
        with self._store_lock:
            self._permits[permit.permit_id] = permit
        receipt = self.ledger.append(
            "GRANTED",
            {
                "permit_id": permit.permit_id,
                "agent_id": agent_id,
                "cap_cents": cap_cents,
                "allowlist": list(allowlist),
                "expiry": expiry.isoformat(),
            },
        )
        return permit, receipt

    def get(self, permit_id: str) -> Permit | None:
        with self._store_lock:
            return self._permits.get(permit_id)

    @staticmethod
    def _evaluate(permit: Permit, amount_cents: int, merchant_id: str, now: datetime) -> str | None:
        """
        The 4-clause authority evaluation. Returns the block reason, or
        None when the attempt is allowed. Caller must hold permit._lock.
        Amount is assumed pre-validated by _validate_amount.
        """
        if permit.revoked:
            return "revoked"
        if now >= permit.expiry:
            return "expired"
        if merchant_id not in permit.allowlist:
            return "merchant_not_allowed"
        if amount_cents > permit.remaining_cents():
            return "over_remaining_cap"
        return None

    def eligible(
        self,
        permit_id: str,
        amount_cents: int,
        merchant_id: str,
        now: datetime | None = None,
    ) -> CheckResult:
        """
        READ-ONLY authority evaluation: the same 4-clause logic as check()
        plus amount validation, but it reserves nothing and writes no
        receipt. The server's /check route uses this. receipt is None.
        """
        now = now or datetime.now(timezone.utc)
        try:
            _validate_amount(amount_cents)
        except ValueError:
            return CheckResult(False, "invalid_amount")

        permit = self.get(permit_id)
        if permit is None:
            return CheckResult(False, "unknown_permit")

        with permit._lock:
            reason = self._evaluate(permit, amount_cents, merchant_id, now)
        if reason is not None:
            return CheckResult(False, reason)
        return CheckResult(True, "allowed")

    def check(
        self,
        permit_id: str,
        amount_cents: int,
        merchant_id: str,
        now: datetime | None = None,
    ) -> CheckResult:
        """
        Evaluate a spend attempt: validate → eligible() → reserve + ALLOWED
        receipt. On BLOCKED, writes a BLOCKED receipt and returns — PayPal
        is never touched. Invalid amounts do NOT raise: they are recorded
        as BLOCKED receipts with reason "invalid_amount".
        """
        now = now or datetime.now(timezone.utc)
        try:
            _validate_amount(amount_cents)
        except ValueError:
            receipt = self.ledger.append(
                "BLOCKED",
                {
                    "permit_id": permit_id,
                    "merchant_id": merchant_id,
                    "amount_cents": (
                        amount_cents
                        if isinstance(amount_cents, int)
                        and not isinstance(amount_cents, bool)
                        else repr(amount_cents)
                    ),
                    "reason": "invalid_amount",
                },
            )
            return CheckResult(False, "invalid_amount", receipt)

        permit = self.get(permit_id)
        if permit is None:
            receipt = self.ledger.append(
                "BLOCKED",
                {"permit_id": permit_id, "reason": "unknown_permit"},
            )
            return CheckResult(False, "unknown_permit", receipt)

        # Read-only authority evaluation first (no reservation, no receipt).
        # This is the shared logic the server's /check route also uses.
        probe = self.eligible(permit_id, amount_cents, merchant_id, now=now)

        with permit._lock:
            # Authoritative re-evaluation under the per-permit lock: the
            # probe is read-only, so the lock serializes reservation
            # against concurrent attempts (C1) and against e-stop. The
            # lock's view wins over the probe's (reservations may have
            # been released between the two).
            reason = self._evaluate(permit, amount_cents, merchant_id, now)
            if reason is not None:
                receipt = self.ledger.append(
                    "BLOCKED",
                    {
                        "permit_id": permit.permit_id,
                        "agent_id": permit.agent_id,
                        "amount_cents": amount_cents,
                        "merchant_id": merchant_id,
                        "reason": reason,
                        "remaining_cents": permit.remaining_cents(),
                    },
                )
                return CheckResult(False, reason, receipt)

            # ALLOWED — reserve the amount against the cap.
            permit.reserved_cents += amount_cents
            auth_id = f"auth_{uuid.uuid4().hex[:12]}"
            permit.in_flight[auth_id] = amount_cents
            receipt = self.ledger.append(
                "ALLOWED",
                {
                    "permit_id": permit.permit_id,
                    "agent_id": permit.agent_id,
                    "amount_cents": amount_cents,
                    "merchant_id": merchant_id,
                    "auth_id": auth_id,
                    "remaining_cents": permit.remaining_cents(),
                },
            )
            return CheckResult(True, "allowed", receipt)

    def settle_capture(self, permit_id: str, auth_id: str) -> Receipt:
        """Move a reservation to captured (called by the settlement layer)."""
        permit = self.get(permit_id)
        assert permit is not None, "unknown permit"
        with permit._lock:
            amount = permit.in_flight.pop(auth_id, None)
            assert amount is not None, "unknown auth_id"
            permit.reserved_cents -= amount
            permit.captured_cents += amount
            return self.ledger.append(
                "CAPTURED",
                {
                    "permit_id": permit_id,
                    "auth_id": auth_id,
                    "amount_cents": amount,
                    "remaining_cents": permit.remaining_cents(),
                },
            )

    def settle_void(self, permit_id: str, auth_id: str) -> Receipt:
        """Release a reservation (called by the settlement layer on void)."""
        permit = self.get(permit_id)
        assert permit is not None, "unknown permit"
        with permit._lock:
            amount = permit.in_flight.pop(auth_id, None)
            assert amount is not None, "unknown auth_id"
            permit.reserved_cents -= amount
            return self.ledger.append(
                "VOIDED",
                {
                    "permit_id": permit_id,
                    "auth_id": auth_id,
                    "amount_cents": amount,
                    "remaining_cents": permit.remaining_cents(),
                },
            )

    def estop(self, permit_id: str) -> tuple[Receipt, list[str]]:
        """
        Emergency stop: revoke the permit immediately. Returns the e-stop
        receipt and the in-flight authorization ids the settlement layer
        must void. No further check() can pass after this.
        """
        permit = self.get(permit_id)
        assert permit is not None, "unknown permit"
        with permit._lock:
            permit.revoked = True
            in_flight = list(permit.in_flight.keys())
            receipt = self.ledger.append(
                "E-STOP",
                {
                    "permit_id": permit_id,
                    "agent_id": permit.agent_id,
                    "in_flight_auth_ids": in_flight,
                    "reserved_cents_released": permit.reserved_cents,
                },
            )
            return receipt, in_flight
