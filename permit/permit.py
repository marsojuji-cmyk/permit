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
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ledger import Ledger, Receipt


@dataclass
class CheckResult:
    allowed: bool
    reason: str
    receipt: Receipt


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
        self.ledger = ledger or Ledger()
        self._permits: dict[str, Permit] = {}
        self._store_lock = threading.Lock()

    def grant(
        self,
        agent_id: str,
        cap_cents: int,
        allowlist: list[str],
        expiry: datetime,
    ) -> tuple[Permit, Receipt]:
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

    def check(
        self,
        permit_id: str,
        amount_cents: int,
        merchant_id: str,
        now: datetime | None = None,
    ) -> CheckResult:
        """
        Evaluate a spend attempt. On ALLOWED, reserves the amount against
        the cap (serialized per-permit). On BLOCKED, writes a BLOCKED
        receipt and returns — PayPal is never touched.
        """
        now = now or datetime.now(timezone.utc)
        permit = self.get(permit_id)
        if permit is None:
            receipt = self.ledger.append(
                "BLOCKED",
                {"permit_id": permit_id, "reason": "unknown_permit"},
            )
            return CheckResult(False, "unknown_permit", receipt)

        with permit._lock:
            if permit.revoked:
                reason = "revoked"
            elif now >= permit.expiry:
                reason = "expired"
            elif merchant_id not in permit.allowlist:
                reason = "merchant_not_allowed"
            elif amount_cents > permit.remaining_cents():
                reason = "over_remaining_cap"
            else:
                reason = None

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
