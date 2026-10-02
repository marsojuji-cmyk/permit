"""
PayPal client interface + mock adapter.

The interface is the contract the release-verifier programs against.
The mock adapter implements the same interface with the same receipt
shapes, so the full demo runs without sandbox credentials — judges and
"run it" verification use mock mode; the recorded video uses sandbox mode.

The real sandbox client (built during the Oct 6–8 spike) implements this
same interface against PayPal's REST API. The verifier never knows which
one it holds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid


@dataclass(frozen=True)
class Authorization:
    auth_id: str
    amount_cents: int
    merchant_id: str
    status: str  # AUTHORIZED
    created_at: str


@dataclass(frozen=True)
class Capture:
    capture_id: str
    auth_id: str
    amount_cents: int
    status: str  # COMPLETED
    idempotency_key: str


@dataclass(frozen=True)
class Void:
    auth_id: str
    status: str  # VOIDED


class PayPalClient(ABC):
    """Contract for authorization hold + gated capture."""

    @abstractmethod
    def authorize(self, amount_cents: int, merchant_id: str) -> Authorization:
        ...

    @abstractmethod
    def capture(
        self, auth_id: str, amount_cents: int, idempotency_key: str
    ) -> Capture:
        ...

    @abstractmethod
    def void(self, auth_id: str) -> Void:
        ...


class MockPayPalClient(PayPalClient):
    """
    In-memory PayPal stand-in. Same interface, same receipt shapes,
    deterministic ids. No network, no credentials.

    Also records every call, so tests can assert "capture was never
    called" — the negative-test backbone (N1, N2).
    """

    def __init__(self):
        self.authorizations: dict[str, Authorization] = {}
        self.captures: dict[str, Capture] = {}  # by idempotency key
        self.voids: dict[str, Void] = {}
        self.capture_calls: list[tuple[str, int, str]] = []

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def authorize(self, amount_cents: int, merchant_id: str) -> Authorization:
        auth = Authorization(
            auth_id=f"mock_auth_{uuid.uuid4().hex[:12]}",
            amount_cents=amount_cents,
            merchant_id=merchant_id,
            status="AUTHORIZED",
            created_at=self._now(),
        )
        self.authorizations[auth.auth_id] = auth
        return auth

    def capture(
        self, auth_id: str, amount_cents: int, idempotency_key: str
    ) -> Capture:
        self.capture_calls.append((auth_id, amount_cents, idempotency_key))
        # Idempotent: same key returns the same capture, no double charge.
        if idempotency_key in self.captures:
            return self.captures[idempotency_key]
        auth = self.authorizations.get(auth_id)
        assert auth is not None, "unknown auth_id"
        assert auth_id not in self.voids, "cannot capture a voided authorization"
        capture = Capture(
            capture_id=f"mock_cap_{uuid.uuid4().hex[:12]}",
            auth_id=auth_id,
            amount_cents=amount_cents,
            status="COMPLETED",
            idempotency_key=idempotency_key,
        )
        self.captures[idempotency_key] = capture
        return capture

    def void(self, auth_id: str) -> Void:
        assert auth_id in self.authorizations, "unknown auth_id"
        void = Void(auth_id=auth_id, status="VOIDED")
        self.voids[auth_id] = void
        return void
